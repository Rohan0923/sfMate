import time

import pytest

from app import auth, config, db, mailer
from tests.conftest import rows


@pytest.fixture
def feedback_mail(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send_feedback", lambda *args: sent.append(args))
    return sent


# ---------- feedback ----------
def test_feedback_is_saved_and_emailed(client, make_user, feedback_mail):
    assert client.post("/feedback", json={"message": "hi"}).status_code == 401
    h = make_user()
    r = client.post("/feedback", json={"message": "  The menu answer was wrong  ", "app_version": "1.0 (3)"}, headers=h)
    assert r.status_code == 200
    saved = rows(db.feedback)
    assert [(f["username"], f["message"], f["app_version"]) for f in saved] == [("rohan", "The menu answer was wrong", "1.0 (3)")]
    assert feedback_mail == [("rohan", "rohan@sfhs.com", "The menu answer was wrong", "1.0 (3)")]


def test_feedback_validation_and_throttle(client, make_user, feedback_mail):
    h = make_user()
    assert client.post("/feedback", json={"message": "   "}, headers=h).status_code == 400
    assert client.post("/feedback", json={"message": "x" * 4001}, headers=h).status_code == 400
    for _ in range(5):
        assert client.post("/feedback", json={"message": "again"}, headers=h).status_code == 200
    assert client.post("/feedback", json={"message": "again"}, headers=h).status_code == 429
    assert len(rows(db.feedback)) == 5


def test_feedback_email_reply_goes_to_the_student(monkeypatch):
    sent = []
    monkeypatch.setattr(config, "GMAIL_ADDRESS", "sfmate@gmail.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "x")
    monkeypatch.setattr(mailer, "_send", sent.append)
    mailer.send_feedback("rohan", "rohan@sfhs.com", "Love it", "1.0 (1)")
    assert sent[0]["To"] == config.SUPPORT_EMAIL and sent[0]["Reply-To"] == "rohan@sfhs.com"
    assert "Love it" in sent[0].get_content() and "1.0 (1)" in sent[0].get_content()


# ---------- deleting an account ----------
def test_delete_account_removes_everything(client, make_user, feedback_mail):
    h = make_user()
    make_user("other", "other@sfhs.com")
    client.post("/feedback", json={"message": "bye"}, headers=h)
    with db.engine.begin() as conn:
        conn.execute(db.usage.insert().values(username="rohan", day="2026-10-13", count=3))
    assert client.delete("/account").status_code == 401
    assert client.delete("/account", headers=h).json() == {"ok": True}
    assert [u["username"] for u in rows(db.users)] == ["other"]
    assert rows(db.feedback) == [] and rows(db.usage) == []
    assert {s["username"] for s in rows(db.sessions)} == {"other"}
    assert client.get("/auth/me", headers=h).status_code == 401
    make_user()   # the same email can sign up again


# ---------- permission to send questions to the AI provider ----------
def test_ai_consent_given_withdrawn_and_deleted_with_the_account(client, make_user):
    h = make_user()
    assert client.post("/consent", json={"accepted": True}).status_code == 401
    assert client.get("/auth/me", headers=h).json()["ai_consent"] is False
    assert client.post("/consent", json={"accepted": True}, headers=h).json() == {"ok": True}
    client.post("/consent", json={"accepted": True}, headers=h)   # twice is fine
    assert client.get("/auth/me", headers=h).json()["ai_consent"] is True
    client.post("/consent", json={"accepted": False}, headers=h)
    assert client.get("/auth/me", headers=h).json()["ai_consent"] is False
    client.post("/consent", json={"accepted": True}, headers=h)
    client.delete("/account", headers=h)
    assert rows(db.ai_consent) == []


# ---------- the App Store review account ----------
@pytest.fixture
def review(monkeypatch):
    monkeypatch.setattr(config, "REVIEW_USERNAME", "appreview")
    monkeypatch.setattr(config, "REVIEW_PASSWORD", "first-password-123")


def test_review_account_logs_in_without_a_code(client, review):
    auth.seed_review_account()
    r = client.post("/auth/login", json={"login": "appreview", "password": "first-password-123"})
    assert r.status_code == 200 and r.json()["setup_done"] is False
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {r.json()['token']}"}).json()
    assert me["email"] == "appreview@review.invalid"


def test_review_account_password_change_and_comes_back_after_delete(client, review, monkeypatch):
    auth.seed_review_account()
    monkeypatch.setattr(config, "REVIEW_PASSWORD", "second-password-456")
    auth.seed_review_account()
    assert client.post("/auth/login", json={"login": "appreview", "password": "first-password-123"}).status_code == 401
    token = client.post("/auth/login", json={"login": "appreview", "password": "second-password-456"}).json()["token"]
    client.delete("/account", headers={"Authorization": f"Bearer {token}"})
    assert rows(db.users) == []
    assert client.post("/auth/login", json={"login": "appreview", "password": "second-password-456"}).status_code == 200


def test_review_account_never_takes_over_a_real_student(client, make_user, review):
    make_user("appreview", "someone@sfhs.com", "their-own-pw")
    auth.seed_review_account()
    assert client.post("/auth/login", json={"login": "appreview", "password": "first-password-123"}).status_code == 401
    assert client.post("/auth/login", json={"login": "appreview", "password": "their-own-pw"}).status_code == 200


def test_no_review_account_unless_both_are_set(client, monkeypatch):
    monkeypatch.setattr(config, "REVIEW_USERNAME", "appreview")
    monkeypatch.setattr(config, "REVIEW_PASSWORD", "")
    auth.seed_review_account()
    assert rows(db.users) == []


# ---------- public pages ----------
def test_privacy_and_support_pages(client):
    for path, marker in (("/privacy", "Privacy Policy"), ("/support", "Frequently asked questions")):
        r = client.get(path)
        assert r.status_code == 200 and marker in r.text, path
        assert config.SUPPORT_EMAIL in r.text and "{{" not in r.text, path
    assert f"{config.DAILY_MESSAGE_LIMIT} messages per day" in client.get("/support").text
