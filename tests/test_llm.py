"""The real llm.py client against a local fake of RunPod's OpenAI-compatible vLLM endpoint."""
import json
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app import config, llm

fake = FastAPI()
STATE = {"cold_failures": 0, "seen": [], "auth": [], "die_mid_stream": False}


@fake.post("/v1/chat/completions")
async def completions(request: Request):
    body = await request.json()
    STATE["seen"].append(body)
    STATE["auth"].append(request.headers.get("authorization"))
    if request.headers.get("authorization") != "Bearer rp_test_key":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if STATE["cold_failures"] > 0:   # worker still starting up
        STATE["cold_failures"] -= 1
        return JSONResponse({"error": "no workers ready"}, status_code=503)

    def events():
        for i, word in enumerate(["Hey ", "Lancer", "!"]):
            if STATE["die_mid_stream"] and i == 2:
                raise RuntimeError("worker crashed")
            chunk = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                     "choices": [{"index": 0, "delta": {"content": word}, "finish_reason": None}]}
            yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(events(), media_type="text/event-stream")


@pytest.fixture(scope="module")
def endpoint():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(fake, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/v1"
    server.should_exit = True


@pytest.fixture(autouse=True)
def point_client_at_fake(endpoint, monkeypatch):
    monkeypatch.setattr(config, "LLM_BASE_URL", endpoint)
    monkeypatch.setattr(config, "RUNPOD_API_KEY", "rp_test_key")
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)   # don't actually wait between retries
    STATE.update(cold_failures=0, seen=[], auth=[], die_mid_stream=False)


def test_streams_tokens_with_model_and_key():
    assert list(llm.stream_chat([{"role": "user", "content": "hi"}], max_tokens=50)) == ["Hey ", "Lancer", "!"]
    sent = STATE["seen"][-1]
    assert sent["model"] == config.LLM_MODEL and sent["stream"] is True and sent["max_tokens"] == 50
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}   # Qwen3.5 would otherwise think first
    assert STATE["auth"][-1] == "Bearer rp_test_key"


def test_complete_joins_the_stream():
    assert llm.complete([{"role": "user", "content": "hi"}]) == "Hey Lancer!"


def test_cold_start_is_retried():
    STATE["cold_failures"] = 2   # two 503s while the GPU worker boots, then it answers
    assert "".join(llm.stream_chat([{"role": "user", "content": "hi"}])) == "Hey Lancer!"
    assert len(STATE["seen"]) == 3


def test_gives_up_after_three_tries():
    STATE["cold_failures"] = 5
    with pytest.raises(llm.LLMError):
        list(llm.stream_chat([{"role": "user", "content": "hi"}]))
    assert len(STATE["seen"]) == 3


def test_bad_key_is_not_retried(monkeypatch):
    monkeypatch.setattr(config, "RUNPOD_API_KEY", "wrong")
    with pytest.raises(llm.LLMError):
        list(llm.stream_chat([{"role": "user", "content": "hi"}]))
    assert len(STATE["seen"]) == 1


def test_failure_mid_answer_is_not_retried():
    STATE["die_mid_stream"] = True   # retrying would repeat the words already shown
    got = []
    with pytest.raises(llm.LLMError):
        for piece in llm.stream_chat([{"role": "user", "content": "hi"}]):
            got.append(piece)
    assert got == ["Hey ", "Lancer"] and len(STATE["seen"]) == 1


def test_missing_config_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(config, "LLM_BASE_URL", "")
    with pytest.raises(llm.LLMError, match="RUNPOD_ENDPOINT_ID"):
        list(llm.stream_chat([{"role": "user", "content": "hi"}]))


def test_warm_up_sends_one_token_and_no_student_data():
    llm.warm_up().join()
    (sent,) = STATE["seen"]
    assert sent["max_tokens"] == 1 and sent["messages"] == [{"role": "user", "content": "hi"}]


def test_warm_up_is_skipped_when_the_model_was_just_used():
    llm.warm_up().join()
    assert llm.warm_up() is None   # warmed a moment ago
    assert len(STATE["seen"]) == 1
    llm._last_activity = float("-inf")
    list(llm.stream_chat([{"role": "user", "content": "hi"}]))   # a real question counts as activity too
    assert llm.warm_up() is None and len(STATE["seen"]) == 2


def test_warm_up_failure_is_quiet_and_allows_another_try_later():
    STATE["cold_failures"] = 5
    llm.warm_up().join()   # the worker never came up: no exception escapes
    assert llm._warming is False
    llm._last_activity = float("-inf")
    STATE["cold_failures"] = 0
    assert llm.warm_up() is not None
