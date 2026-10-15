"""Accounts: @sfhs.com sign-up with an emailed code, password login, optional Google sign-in, sessions, the App Store
review account, and deleting an account."""
import hashlib
import logging
import re
import secrets
import threading
import time
from collections import defaultdict, deque

import sqlalchemy as sa
from fastapi import Header, HTTPException

from . import config, mailer
from .db import engine, feedback, pending_signups, sessions, usage, users

log = logging.getLogger("sfmate.auth")

CODE_TTL = 15 * 60          # verification codes expire after 15 minutes
CODE_MAX_ATTEMPTS = 5       # wrong guesses before the code is burned
RESEND_COOLDOWN = 60        # seconds between code emails to the same address
USERNAME_RE = re.compile(r"[a-z0-9_.-]{3,32}")


# ---------- passwords & codes ----------
def hash_password(password, salt=None):
    """scrypt with a random salt, stored as "salt$digest" (never the password itself)."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}${digest.hex()}"


def check_password(password, stored):
    if not stored:
        return False   # Google-only account
    salt_hex, _ = stored.split("$")
    return secrets.compare_digest(hash_password(password, bytes.fromhex(salt_hex)), stored)


def _sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


def email_allowed(email):
    return not config.ALLOWED_EMAIL_DOMAIN or email.endswith("@" + config.ALLOWED_EMAIL_DOMAIN)


def domain_message():
    return f"sfMate is only for Saint Francis students: use your @{config.ALLOWED_EMAIL_DOMAIN} email"


# ---------- throttling (one server instance, so in-memory is enough) ----------
class Throttle:
    def __init__(self, limit, window):
        self.limit, self.window, self.hits, self.lock = limit, window, defaultdict(deque), threading.Lock()

    def check(self, key):
        """Raises 429 if `key` has used up its attempts in the window; otherwise records one."""
        now = time.time()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                raise HTTPException(429, "Too many tries. Wait a few minutes and try again.")
            q.append(now)


login_throttle = Throttle(limit=8, window=15 * 60)      # per IP + login name
signup_throttle = Throttle(limit=10, window=60 * 60)    # code emails per IP per hour
feedback_throttle = Throttle(limit=5, window=60 * 60)   # feedback messages per student per hour


# ---------- sessions ----------
def start_session(conn, user):
    token = secrets.token_urlsafe(32)
    conn.execute(sessions.insert().values(token_hash=_sha256(token), username=user["username"], created_at=time.time()))
    return {"token": token, "name": user["name"], "setup_done": user["profile"] is not None}


def bearer(authorization):
    return authorization.removeprefix("Bearer ").strip()


def current_user(authorization: str = Header(default="")):
    """FastAPI dependency: the logged-in user (a dict), or 401."""
    token = bearer(authorization)
    if not token:
        raise HTTPException(401, "Please log in")
    with engine.begin() as conn:
        row = conn.execute(
            sa.select(users, sessions.c.created_at.label("session_created"))
            .join(sessions, sessions.c.username == users.c.username)
            .where(sessions.c.token_hash == _sha256(token))
        ).mappings().first()
        expired = row is not None and time.time() - row["session_created"] > config.SESSION_DAYS * 86400
        if expired:
            conn.execute(sessions.delete().where(sessions.c.token_hash == _sha256(token)))
    if not row or expired:   # raised after the transaction commits, so the cleanup sticks
        raise HTTPException(401, "Please log in")
    return dict(row)


def logout(authorization):
    token = bearer(authorization)
    if token:
        with engine.begin() as conn:
            conn.execute(sessions.delete().where(sessions.c.token_hash == _sha256(token)))


def find_user(conn, login):
    """By username or email (both stored lowercase)."""
    login = login.strip().lower()
    return conn.execute(sa.select(users).where(sa.or_(users.c.username == login, users.c.email == login))).mappings().first()


# ---------- sign-up: step 1, check details and email a code ----------
def _new_code():
    return f"{secrets.randbelow(10**6):06d}"


def start_signup(username_raw, email_raw, password, ip):
    username, email = username_raw.strip().lower(), email_raw.strip().lower()
    if not USERNAME_RE.fullmatch(username):
        raise HTTPException(400, "Usernames are 3–32 characters: letters, numbers, and . _ -")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(400, "Please enter a valid email")
    if not email_allowed(email):
        raise HTTPException(400, domain_message())
    if len(password) < 8:
        raise HTTPException(400, "Passwords need at least 8 characters")
    with engine.begin() as conn:
        if conn.execute(sa.select(users.c.username).where(users.c.username == username)).first():
            raise HTTPException(409, "That username is taken")
        if conn.execute(sa.select(users.c.username).where(users.c.email == email)).first():
            raise HTTPException(409, "An account with that email already exists. Try logging in.")
        pending = conn.execute(sa.select(pending_signups).where(pending_signups.c.email == email)).mappings().first()
        if pending and time.time() - pending["last_sent_at"] < RESEND_COOLDOWN:
            raise HTTPException(429, "We just sent you a code. Check your inbox (and spam), or wait a minute.")
    signup_throttle.check(ip)
    code = _new_code()
    mailer.send_verification_code(email, code)   # before saving, so a failed send doesn't start the cooldown
    now = time.time()
    row = dict(username=username, name=username_raw.strip(), password_hash=hash_password(password),
               code_hash=hash_password(code), expires_at=now + CODE_TTL, attempts=0, last_sent_at=now)
    with engine.begin() as conn:
        conn.execute(pending_signups.delete().where(pending_signups.c.email == email))
        conn.execute(pending_signups.insert().values(email=email, **row))
    return {"pending": True, "email": email}


def resend_code(email_raw, ip):
    email = email_raw.strip().lower()
    with engine.begin() as conn:
        pending = conn.execute(sa.select(pending_signups).where(pending_signups.c.email == email)).mappings().first()
    if not pending:
        raise HTTPException(400, "That sign-up expired. Start again.")
    if time.time() - pending["last_sent_at"] < RESEND_COOLDOWN:
        raise HTTPException(429, "Hang on a minute before asking for another code.")
    signup_throttle.check(ip)
    code = _new_code()
    mailer.send_verification_code(email, code)
    now = time.time()
    with engine.begin() as conn:
        conn.execute(pending_signups.update().where(pending_signups.c.email == email).values(
            code_hash=hash_password(code), expires_at=now + CODE_TTL, attempts=0, last_sent_at=now))
    return {"pending": True, "email": email}


# ---------- sign-up: step 2, check the code and create the account ----------
def verify_signup(email_raw, code_raw):
    email, code = email_raw.strip().lower(), re.sub(r"\D", "", code_raw)
    with engine.begin() as conn:
        pending = conn.execute(sa.select(pending_signups).where(pending_signups.c.email == email)).mappings().first()
        if not pending:
            raise HTTPException(400, "That sign-up expired. Start again.")
        if time.time() > pending["expires_at"]:
            raise HTTPException(400, "That code expired. Tap “Resend code”.")
        if pending["attempts"] >= CODE_MAX_ATTEMPTS:
            raise HTTPException(400, "Too many wrong codes. Tap “Resend code” for a new one.")
        wrong = not check_password(code, pending["code_hash"])
        if wrong:   # count the miss; the error is raised after this transaction commits, or the count would roll back
            conn.execute(pending_signups.update().where(pending_signups.c.email == email)
                         .values(attempts=pending["attempts"] + 1))
    if wrong:
        left = CODE_MAX_ATTEMPTS - pending["attempts"] - 1
        raise HTTPException(400, f"That code isn't right. {left} {'try' if left == 1 else 'tries'} left.")
    with engine.begin() as conn:
        try:
            conn.execute(users.insert().values(
                username=pending["username"], email=email, name=pending["name"], password_hash=pending["password_hash"],
                google_sub=None, profile=None, welcome=None, announcement_answered=None, created_at=time.time()))
        except sa.exc.IntegrityError:
            raise HTTPException(409, "That username or email was just taken. Start again with a different one.")
        conn.execute(pending_signups.delete().where(pending_signups.c.email == email))
        user = conn.execute(sa.select(users).where(users.c.email == email)).mappings().first()
        return start_session(conn, user)


# ---------- login ----------
def login(login_raw, password, ip):
    login_throttle.check(f"{ip}|{login_raw.strip().lower()}")
    if config.REVIEW_USERNAME and login_raw.strip().lower() in (config.REVIEW_USERNAME, review_email()):
        seed_review_account()   # back again if a reviewer tried out Delete account
    with engine.begin() as conn:
        user = find_user(conn, login_raw)
        if user and not user["password_hash"]:
            raise HTTPException(401, "This account signs in with Google")
        if not user or not check_password(password, user["password_hash"]):
            raise HTTPException(401, "Wrong username or password")
        return start_session(conn, user)


# ---------- the App Store review account ----------
def review_email():
    return f"{config.REVIEW_USERNAME}@review.invalid"   # .invalid never delivers, and no one can sign up with it


def seed_review_account():
    """With REVIEW_USERNAME and REVIEW_PASSWORD set, makes sure that account exists with that password (at startup,
    and when someone logs in with it). It skips the emailed code because Apple's reviewers can't get one."""
    username, password = config.REVIEW_USERNAME, config.REVIEW_PASSWORD
    if not (username and password):
        return
    if not USERNAME_RE.fullmatch(username):
        log.error("REVIEW_USERNAME %r isn't a valid username (3–32 of a-z 0-9 . _ -): no review account", username)
        return
    with engine.begin() as conn:
        user = conn.execute(sa.select(users).where(users.c.username == username)).mappings().first()
        if user and user["email"] != review_email():   # a real student has that name: never touch their account
            log.error("REVIEW_USERNAME %r belongs to a real account: pick another name", username)
        elif not user:
            conn.execute(users.insert().values(
                username=username, email=review_email(), name="App Review", password_hash=hash_password(password),
                google_sub=None, profile=None, welcome=None, announcement_answered=None, created_at=time.time()))
            log.info("created the review account %r", username)
        elif not check_password(password, user["password_hash"]):
            conn.execute(users.update().where(users.c.username == username).values(password_hash=hash_password(password)))
            log.info("updated the review account's password")


# ---------- deleting an account ----------
def delete_account(user):
    """Everything tied to the student. Shared announcement summaries are per topic set, not anyone's, so they stay."""
    with engine.begin() as conn:
        for table in (feedback, usage, sessions):
            conn.execute(table.delete().where(table.c.username == user["username"]))
        conn.execute(pending_signups.delete().where(pending_signups.c.email == user["email"]))
        conn.execute(users.delete().where(users.c.username == user["username"]))


# ---------- Google sign-in (optional) ----------
def google_login(credential):
    if not config.GOOGLE_CLIENT_ID:
        raise HTTPException(400, "Google sign-in isn't set up")
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token
    try:   # checks Google's signature, expiry, and that the token was issued for OUR client ID
        info = google_id_token.verify_oauth2_token(credential, google_requests.Request(), config.GOOGLE_CLIENT_ID)
    except ValueError:
        raise HTTPException(401, "Google sign-in failed, please try again")
    email = info.get("email", "").lower()
    if not info.get("email_verified"):
        raise HTTPException(401, "Your Google email isn't verified")
    if not email_allowed(email):
        raise HTTPException(403, domain_message())
    with engine.begin() as conn:
        user = conn.execute(sa.select(users).where(users.c.google_sub == info["sub"])).mappings().first() \
            or find_user(conn, email)
        if user is None:   # first Google sign-in creates the account (username = email; can't clash with sign-up names)
            conn.execute(users.insert().values(
                username=email, email=email, name=info.get("name") or email, password_hash=None,
                google_sub=info["sub"], profile=None, welcome=None, announcement_answered=None, created_at=time.time()))
            user = find_user(conn, email)
        elif not user["google_sub"]:   # same verified email as an existing account: link them
            conn.execute(users.update().where(users.c.username == user["username"]).values(google_sub=info["sub"]))
        return start_session(conn, user)
