"""Answers are written in their own thread, apart from the connection that asked for them.

The iPhone app is suspended a few seconds after the student switches to another app, which drops its connection
mid-answer. So the app sends an X-Request-ID with each question: the answer keeps being written after the connection
drops, and when the student comes back the app picks it up from GET /answers/{id}. Without an ID (the website), a
dropped connection stops the answer, as before. Kept in memory: there's one server instance (render.yaml).
"""
import json
import logging
import threading
import time

from . import llm

log = logging.getLogger("sfmate.answers")

KEEP = 15 * 60   # seconds a finished answer stays available to pick up
PING = 15        # seconds between keep-alive comments while waiting on the model

_answers = {}    # (username, request id) -> Answer
_lock = threading.Lock()


def sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


class Answer:
    """One answer's events ({sources}, {delta}, {error}, then {done}), written by a thread, read by any listener."""

    def __init__(self, sources):
        self.events = [{"sources": sources}] if sources is not None else []
        self.finished_at = None
        self.cancelled = False
        self._changed = threading.Condition()

    def _add(self, event, last=False):
        with self._changed:
            self.events.append(event)
            if last:
                self.finished_at = time.monotonic()
            self._changed.notify_all()

    def write(self, pieces, on_complete):
        """`pieces` yields the answer's text, and may also yield dicts (like {"sources": [...]}) to send as they are."""
        parts = []
        try:
            for piece in pieces:
                if self.cancelled:
                    return   # nobody's listening (Stop, or the website closed): leaving the loop closes the model request
                if isinstance(piece, dict):
                    self._add(piece)
                    continue
                parts.append(piece)
                self._add({"delta": piece})
            if on_complete:
                on_complete("".join(parts).strip())
        except llm.LLMError as e:
            self._add({"error": str(e)})
        except Exception:
            log.exception("answer failed")
            self._add({"error": "Something went wrong on our end. Try again."})
        finally:
            self._add({"done": True}, last=True)

    def follow(self):
        """Server-Sent Events: everything so far, then each event as it's written, until {done}."""
        sent = 0
        while True:
            with self._changed:
                if sent == len(self.events) and self.finished_at is None:
                    self._changed.wait(PING)
                new, finished = self.events[sent:], self.finished_at is not None
            if not new:
                yield ": ping\n\n"   # keeps phone networks and proxies from closing a quiet connection
            for event in new:
                yield sse(event)
            sent += len(new)
            if finished:
                return


def start(username, request_id, sources, pieces, on_complete=None):
    """Starts writing an answer. With a request ID it can be picked up again (find) until KEEP after it's done."""
    answer = Answer(sources)
    now = time.monotonic()
    with _lock:
        for key in [k for k, a in _answers.items() if a.finished_at is not None and now - a.finished_at > KEEP]:
            del _answers[key]
        if request_id:
            _answers[(username, request_id)] = answer
    threading.Thread(target=answer.write, args=(pieces, on_complete), daemon=True).start()
    return answer


def find(username, request_id):
    with _lock:
        return _answers.get((username, request_id))
