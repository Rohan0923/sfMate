"""Test setup: a throwaway SQLite database, a stand-in search model, a stand-in LLM, and captured emails."""
import os
import re
import tempfile
import zlib

_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ.update(DATABASE_URL=f"sqlite:///{_db.name}", APP_ENV="development",
                  GMAIL_ADDRESS="", GMAIL_APP_PASSWORD="", GOOGLE_CLIENT_ID="", DAILY_MESSAGE_LIMIT="5",
                  CALENDAR_ICS_URLS="", MENU_URL="", CLUBS_URL="")   # no network in tests; the calendar and menu tests feed in their own

import numpy as np   # noqa: E402
import pytest   # noqa: E402
import sqlalchemy as sa   # noqa: E402

from app import announcements, auth, calendars, clubs, db, llm, mailer, menu, rag, router, site   # noqa: E402


class FakeEmbedder:
    """Bag-of-words hashing: enough to test the search plumbing without loading the real model."""
    def encode(self, texts, normalize_embeddings=True, **kw):
        out = np.zeros((len(texts), 768), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in re.findall(r"[a-z]+", t.lower()):
                out[i, zlib.crc32(w.encode()) % 768] += 1   # not hash(): that changes every run
        return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)


class FakeLLM:
    """Answers each step of the router: the classifier gets `category`, the draft step `draft`, everything else
    (the check step, the welcome message) `reply`. `queue` overrides the next calls, in order."""
    def __init__(self):
        self.calls, self.fail, self.queue = [], False, []
        self.category, self.draft = "rules", "Phones stay off the dance floor [1]."
        self.reply = ["Phones ", "stay off ", "the dance floor [1]."]

    def step(self, messages):
        system = messages[0]["content"]
        if system == router.CLASSIFY_INSTRUCTIONS:
            return "classify"
        return "draft" if system.startswith(router.DRAFT_INSTRUCTIONS) else "answer"

    def calls_for(self, step):
        return [m for m in self.calls if self.step(m) == step]

    def stream_chat(self, messages, max_tokens=512, temperature=0.3):
        self.calls.append(messages)
        if self.fail:
            raise llm.LLMError("sfMate's brain didn't respond. Try again in a minute.")
        if self.queue:
            yield from self.queue.pop(0)
            return
        step = self.step(messages)
        yield from ([self.category] if step == "classify" else [self.draft] if step == "draft" else self.reply)


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    with db.engine.begin() as conn:   # empty every table between tests
        for table in reversed(db.metadata.sorted_tables):
            conn.execute(table.delete())
    auth.login_throttle.hits.clear()
    auth.signup_throttle.hits.clear()
    rag.set_embedder(FakeEmbedder())
    rag.docs = [{"url": "https://x/hb.pdf", "title": "Student-Parent Handbook 2026-27 §4.4 Dances",
                 "text": "phones dance floor tables bleachers"},
                {"url": "https://x/guide.pdf#page=30", "title": "Course Scheduling Guide 2026-27 · 653 AP Chemistry",
                 "text": "chemistry prerequisite honors"},
                {"url": "https://www.sfhs.com/clubs-and-activities", "title": "Clubs and Activities | Students",
                 "text": "join a club at the club fair, over 80 clubs"},
                {"url": "https://www.sfhs.com/admissions", "title": "Admissions | Saint Francis High School",
                 "text": "apply for admission tuition financial aid deadline"}]
    rag.doc_embs = rag.embed([f"{d['title']}\n{d['text']}" for d in rag.docs])
    rag.EXTRA_SOURCES.clear()
    calendars.events, calendars._feeds, calendars._emb_cache, calendars._searchable = [], {}, {}, ([], None)
    menu.sections, menu.fetched_at = [], None
    clubs.clubs, clubs.groups, clubs.intro, clubs.fetched_at, clubs._embs, clubs._emb_cache = [], [], "", None, None, {}
    announcements.announcements, announcements.last_checked, announcements._last_attempt, announcements._parsed = [], None, 0.0, {}
    monkeypatch.setattr(site, "live_text", site.saved_text)   # no live sfhs.com fetches in tests
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
