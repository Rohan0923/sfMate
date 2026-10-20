"""Talks to the model: vLLM on a RunPod Serverless endpoint, through its OpenAI-compatible API.

The endpoint scales to zero, so the first request after a quiet spell waits while a GPU worker starts and loads the
model (a cold start, often 1-3 minutes). The timeouts and retries below are sized for that.

Qwen3.5 thinks (writes a <think> block) before answering by default; every request turns that off, since the
classifier's few tokens and the draft's budget would go to thinking instead of the answer.
"""
import logging
import threading
import time

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from . import config

log = logging.getLogger("sfmate.llm")

_client = None
_lock = threading.Lock()
_last_activity = float("-inf")   # when the model was last asked something (or warmed up), by time.monotonic()
_warming = False


def client():
    global _client
    if _client is None:
        if not config.LLM_BASE_URL:
            raise LLMError("The model isn't configured (set RUNPOD_ENDPOINT_ID and RUNPOD_API_KEY)")
        _client = OpenAI(
            base_url=config.LLM_BASE_URL,
            api_key=config.RUNPOD_API_KEY or "none",
            timeout=httpx.Timeout(connect=15, read=config.LLM_FIRST_TOKEN_TIMEOUT, write=30, pool=30),
            max_retries=0,   # retries are handled below, only before the first token
        )
    return _client


class LLMError(Exception):
    pass


RETRYABLE = (APIConnectionError, APITimeoutError)
COLD_START_ATTEMPTS = 3


def _should_retry(err):
    if isinstance(err, RETRYABLE):
        return True
    # 502/503/504 while a worker is spinning up, 429 if the queue is momentarily full
    return isinstance(err, APIStatusError) and err.status_code in (429, 502, 503, 504)


def stream_chat(messages, max_tokens=512, temperature=0.3):
    """Yields text pieces as the model writes them. Retries (quietly) if the endpoint fails before the first token."""
    for attempt in range(1, COLD_START_ATTEMPTS + 1):
        started = False
        _touch()
        try:
            stream = client().chat.completions.create(
                model=config.LLM_MODEL, messages=messages, stream=True,
                max_tokens=max_tokens, temperature=temperature, top_p=0.9,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
            for event in stream:
                if not event.choices:
                    continue
                piece = event.choices[0].delta.content
                if piece:
                    started = True
                    yield piece
            _touch()   # the worker's idle timer just restarted
            return
        except LLMError:
            raise   # already a clear message (e.g. the endpoint isn't configured)
        except Exception as e:
            if started or not _should_retry(e) or attempt == COLD_START_ATTEMPTS:
                log.error("model request failed (attempt %d): %s", attempt, e)
                raise LLMError("sfMate's brain didn't respond. Try again in a minute.") from e
            log.warning("model not ready (attempt %d): %s; retrying", attempt, e)
            time.sleep(5 * attempt)


def complete(messages, max_tokens=200, temperature=0.8):
    """Whole answer at once (for short things like the welcome message)."""
    return "".join(stream_chat(messages, max_tokens=max_tokens, temperature=temperature)).strip()


def _touch():
    global _last_activity
    with _lock:
        _last_activity = time.monotonic()


def warm_up():
    """Starts waking the GPU in the background (a one-token request that carries no student data), so the worker
    boots while the student is still reading the screen. Skipped if the model was used (or warmed) within
    LLM_WARMUP_INTERVAL, so a crowd opening the app costs one request. Returns the thread, or None if skipped."""
    global _last_activity, _warming
    with _lock:
        if _warming or time.monotonic() - _last_activity < config.LLM_WARMUP_INTERVAL:
            return None
        _warming = True
        _last_activity = time.monotonic()
    thread = threading.Thread(target=_warm, daemon=True)
    thread.start()
    return thread


def _warm():
    global _warming
    try:
        complete([{"role": "user", "content": "hi"}], max_tokens=1)
    except LLMError:
        pass   # already logged; the first real question just pays the cold start
    except Exception:
        log.exception("warm-up failed")
    finally:
        with _lock:
            _warming = False
