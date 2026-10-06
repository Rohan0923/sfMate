"""Test setup: a throwaway SQLite database, a stand-in search model, a stand-in LLM, and captured emails."""
import os
import re
import tempfile

_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ.update(DATABASE_URL=f"sqlite:///{_db.name}", APP_ENV="development",
                  GMAIL_ADDRESS="", GMAIL_APP_PASSWORD="", GOOGLE_CLIENT_ID="", DAILY_MESSAGE_LIMIT="5",
                  CALENDAR_ICS_URLS="")   # no network in tests; test_calendars.py feeds in its own

import numpy as np   # noqa: E402
import pytest   # noqa: E402
import sqlalchemy as sa   # noqa: E402

from app import auth, calendars, db, llm, mailer, rag   # noqa: E402


class FakeEmbedder:
    """Bag-of-words hashing: enough to test the search plumbing without loading the real model."""
    def encode(self, texts, normalize_embeddings=True, **kw):
        out = np.zeros((len(texts), 768), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in re.findall(r"[a-z]+", t.lower()):
                out[i, hash(w) % 768] += 1
        return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)


class FakeLLM:
    def __init__(self):
        self.calls, self.reply, self.fail = [], ["Phones ", "stay off ", "the dance floor [1]."], False
        self.queue = []   # replies for the next calls, in order (then `reply` for the rest)

    def stream_chat(self, messages, max_tokens=512, temperature=0.3):
        self.calls.append(messages)
        if self.fail:
            raise llm.LLMError("sfMate's brain didn't respond. Try again in a minute.")
        yield from (self.queue.pop(0) if self.queue else self.reply)


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    with db.engine.begin() as conn:   # empty every table between tests
        for table in reversed(db.metadata.sorted_tables):
            conn.execute(table.delete())
    auth.login_throttle.hits.clear()
    auth.signup_throttle.hits.clear()
    rag.set_embedder(FakeEmbedder())
    rag.docs = [{"url": "https://x/hb.pdf", "title": "Handbook §4.4 Dances", "text": "phones dance floor tables bleachers"},
                {"url": "https://x/guide.pdf#page=30", "title": "Guide · 653 AP Chemistry", "text": "chemistry prerequisite honors"}]
    rag.doc_embs = rag.embed([f"{d['title']}\n{d['text']}" for d in rag.docs])
    rag.EXTRA_SOURCES.clear()
    calendars.events, calendars._feeds, calendars._emb_cache, calendars._searchable = [], {}, {}, ([], None)
    yield


@pytest.fixture
def fake_llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(llm, "stream_chat", fake.stream_chat)
    return fake


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send_verification_code", lambda to, code: sent.append((to, code)))
    return sent


@pytest.fixture
def client(fake_llm, outbox, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(rag, "load_index", lambda *a, **k: None)   # keep the small test index from fresh_state
    with TestClient(app) as c:
        yield c


@pytest.fixture
def make_user(client, outbox):
    """Signs up + verifies a student; returns auth headers."""
    def make(username="rohan", email="rohan@sfhs.com", password="hunter22pw"):
        assert client.post("/auth/signup", json={"username": username, "email": email, "password": password}).status_code == 200
        code = next(c for to, c in reversed(outbox) if to == email)
        r = client.post("/auth/verify", json={"email": email, "code": code})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['token']}"}
    return make


def rows(table):
    with db.engine.begin() as conn:
        return [dict(r) for r in conn.execute(sa.select(table)).mappings()]
