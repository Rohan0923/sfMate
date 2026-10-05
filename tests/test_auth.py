import time

import sqlalchemy as sa

from app import auth, config, db
from tests.conftest import rows


def signup(client, username="rohan", email="rohan@sfhs.com", password="hunter22pw"):
    return client.post("/auth/signup", json={"username": username, "email": email, "password": password})


def test_signup_requires_school_email(client, outbox):
    r = signup(client, email="rohan@gmail.com")
    assert r.status_code == 400 and "@sfhs.com" in r.json()["detail"]
    assert signup(client, email="rohan@sfhs.com.evil.com").status_code == 400   # endswith check, not "contains"
    assert signup(client, email="rohan@notsfhs.com").status_code == 400
    assert outbox == []


def test_signup_sends_code_and_creates_no_account_yet(client, outbox):
    r = signup(client, email="  Rohan@SFHS.com ")
    assert r.status_code == 200 and r.json() == {"pending": True, "email": "rohan@sfhs.com"}
    assert len(outbox) == 1 and outbox[0][0] == "rohan@sfhs.com" and len(outbox[0][1]) == 6 and outbox[0][1].isdigit()
    assert rows(db.users) == []                     # nothing until the code is verified
    pending = rows(db.pending_signups)[0]
    assert outbox[0][1] not in str(pending) and "hunter22pw" not in str(pending)   # code + password stored hashed


def test_verify_creates_account_and_session(client, outbox):
    signup(client)
    r = client.post("/auth/verify", json={"email": "rohan@sfhs.com", "code": f" {outbox[0][1][:3]} {outbox[0][1][3:]} "})
    assert r.status_code == 200 and r.json()["setup_done"] is False and r.json()["name"] == "rohan"
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {r.json()['token']}"}).json()
    assert me["email"] == "rohan@sfhs.com" and me["profile"] is None
    assert rows(db.pending_signups) == []
    assert r.json()["token"] not in str(rows(db.sessions))   # only a hash of the token is stored


def test_wrong_codes_are_counted_and_capped(client, outbox):
    signup(client)
    right = outbox[0][1]
    wrong = "000000" if right != "000000" else "111111"
    for left in (4, 3, 2, 1, 0):
        r = client.post("/auth/verify", json={"email": "rohan@sfhs.com", "code": wrong})
        assert r.status_code == 400 and f"{left} tr" in r.json()["detail"]
    r = client.post("/auth/verify", json={"email": "rohan@sfhs.com", "code": right})   # even the right code is refused now
    assert r.status_code == 400 and "Resend" in r.json()["detail"]
    assert rows(db.users) == []


def test_expired_code(client, outbox):
    signup(client)
    with db.engine.begin() as conn:
        conn.execute(db.pending_signups.update().values(expires_at=time.time() - 1))
    r = client.post("/auth/verify", json={"email": "rohan@sfhs.com", "code": outbox[0][1]})
    assert r.status_code == 400 and "expired" in r.json()["detail"]


def test_resend_has_a_cooldown_and_replaces_the_code(client, outbox):
    signup(client)
    assert client.post("/auth/resend", json={"email": "rohan@sfhs.com"}).status_code == 429
    assert signup(client).status_code == 429                         # re-submitting the form is throttled too
    with db.engine.begin() as conn:
        conn.execute(db.pending_signups.update().values(last_sent_at=time.time() - 61))
    assert client.post("/auth/resend", json={"email": "rohan@sfhs.com"}).status_code == 200
    old, new = outbox[0][1], outbox[1][1]
    if old != new:
        assert client.post("/auth/verify", json={"email": "rohan@sfhs.com", "code": old}).status_code == 400
    assert client.post("/auth/verify", json={"email": "rohan@sfhs.com", "code": new}).status_code == 200


def test_duplicates_and_validation(client, make_user):
    make_user()
    assert signup(client, username="ROHAN", email="other@sfhs.com").json()["detail"] == "That username is taken"
    assert signup(client, username="other", email="rohan@sfhs.com").status_code == 409
    assert signup(client, username="ab", email="x@sfhs.com").status_code == 400
    assert signup(client, username="abc", email="x@sfhs.com", password="short").status_code == 400


def test_login_logout(client, make_user):
    make_user()
    assert client.post("/auth/login", json={"login": "ROHAN", "password": "hunter22pw"}).status_code == 200
    r = client.post("/auth/login", json={"login": "rohan@sfhs.com", "password": "hunter22pw"})
    assert r.status_code == 200 and r.json()["setup_done"] is False
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.post("/auth/login", json={"login": "rohan", "password": "nope-nope"}).json()["detail"] == "Wrong username or password"
    assert client.post("/auth/logout", headers=h).status_code == 200
    assert client.get("/auth/me", headers=h).status_code == 401


def test_login_throttle(client, make_user):
    make_user()
    for _ in range(8):
        assert client.post("/auth/login", json={"login": "rohan", "password": "wrong-pass"}).status_code == 401
    assert client.post("/auth/login", json={"login": "rohan", "password": "hunter22pw"}).status_code == 429


def test_sessions_expire(client, make_user):
    h = make_user()
    with db.engine.begin() as conn:
        conn.execute(db.sessions.update().values(created_at=time.time() - (config.SESSION_DAYS + 1) * 86400))
    assert client.get("/auth/me", headers=h).status_code == 401
    assert rows(db.sessions) == []   # the expired session was cleaned up (not rolled back)


def test_google_restricted_to_school_accounts(client, monkeypatch):
    from google.oauth2 import id_token
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "test.apps.googleusercontent.com")
    claims = {"sub": "g1", "email": "kid@gmail.com", "email_verified": True, "name": "Kid"}
    monkeypatch.setattr(id_token, "verify_oauth2_token", lambda token, req, aud: dict(claims))
    r = client.post("/auth/google", json={"credential": "t"})
    assert r.status_code == 403 and "@sfhs.com" in r.json()["detail"]
    claims.update(email="kid@sfhs.com")
    r = client.post("/auth/google", json={"credential": "t"})
    assert r.status_code == 200 and r.json()["setup_done"] is False
    assert rows(db.users)[0]["username"] == "kid@sfhs.com"


def test_password_hashing_roundtrip():
    h = auth.hash_password("hunter22pw")
    assert "hunter22pw" not in h and auth.check_password("hunter22pw", h) and not auth.check_password("nope", h)
