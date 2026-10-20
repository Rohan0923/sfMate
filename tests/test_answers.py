import threading
import time

from app import answers
from tests.test_app import sse_events


def gated(pieces, gate, seen):
    """Yields pieces, waiting for `gate` before the last one; records what was asked for."""
    for i, piece in enumerate(pieces):
        if i == len(pieces) - 1:
            gate.wait(5)
        seen.append(piece)
        yield piece


def read_all(answer):
    return sse_events("".join(answer.follow()))


def wait_done(answer):
    for _ in range(200):
        if answer.finished_at is not None:
            return
        time.sleep(0.01)
    raise AssertionError("answer never finished")


def test_an_answer_keeps_being_written_with_nobody_listening():
    answer = answers.start("rohan", "r1", [{"n": 1}], iter(["Hey ", "Lancer"]))
    wait_done(answer)   # nobody called follow(): it finished anyway
    assert read_all(answer) == [{"sources": [{"n": 1}]}, {"delta": "Hey "}, {"delta": "Lancer"}, {"done": True}]
    assert answers.find("rohan", "r1") is answer
    assert answers.find("maya", "r1") is None   # only the student who asked can pick it up


def test_picking_up_mid_answer_gets_the_start_then_the_rest_live():
    gate, seen = threading.Event(), []
    answer = answers.start("rohan", "r2", None, gated(["a", "b", "c"], gate, seen))
    for _ in range(200):
        if len(answer.events) == 2:
            break
        time.sleep(0.01)
    threading.Timer(0.1, gate.set).start()
    assert [e.get("delta") for e in read_all(answer)] == ["a", "b", "c", None]


def test_stop_ends_the_answer_early():
    gate, seen = threading.Event(), []
    answer = answers.start("rohan", "r3", None, gated(["a", "b", "c"], gate, seen))
    answer.cancelled = True
    gate.set()
    wait_done(answer)
    assert "c" not in [e.get("delta") for e in answer.events]


def test_without_a_request_id_it_cant_be_picked_up():
    answer = answers.start("rohan", "", None, iter(["x"]))
    wait_done(answer)
    assert answers._answers == {}


def test_old_answers_are_cleared_out():
    old = answers.start("rohan", "old", None, iter(["x"]))
    wait_done(old)
    old.finished_at -= answers.KEEP + 1
    answers.start("rohan", "new", None, iter(["y"]))
    assert answers.find("rohan", "old") is None and answers.find("rohan", "new") is not None


def test_the_app_picks_an_answer_back_up(client, make_user, fake_llm):
    h, maya = make_user(), make_user("maya", "maya@sfhs.com")
    first = sse_events(client.post("/chat", json={"message": "can I use my phone at a dance?"},
                                   headers={**h, "X-Request-ID": "abc-123"}).text)
    again = sse_events(client.get("/answers/abc-123", headers=h).text)
    assert again == first and "".join(e.get("delta", "") for e in again) == "Phones stay off the dance floor [1]."
    assert len(fake_llm.calls_for("answer")) == 1   # picked up, not asked again
    assert client.get("/answers/abc-123", headers=maya).status_code == 404   # someone else's answer
    assert client.get("/answers/nope", headers=h).json()["detail"] == "That answer got lost while you were away. Ask again?"
    assert client.delete("/answers/abc-123", headers=h).json() == {"ok": True}
    assert client.get("/answers/abc-123").status_code == 401


def test_the_website_answers_still_stream(client, make_user, fake_llm):
    events = sse_events(client.post("/chat", json={"message": "hi"}, headers=make_user()).text)
    assert events[-1] == {"done": True} and answers._answers == {}   # no X-Request-ID: nothing kept
