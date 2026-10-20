"""sfMate web server.

Pages:  /  home    /login  sign up & log in    /setup  first-time profile    /app  the chatbot    /privacy    /support
API:    /auth/*  accounts    /account (DELETE)    /profile    /welcome    /warmup    /chat (streamed)    /announcements/*
        /reminders and /feedback (iPhone app)
"""
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from html import escape

import sqlalchemy as sa
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from . import announcements, auth, calendars, clubs, config, llm, mailer, menu, prompts, rag, reminders, router
from .db import announcement_summaries, engine, feedback, usage, users

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("sfmate")


@asynccontextmanager
async def lifespan(app):
    rag.load_index()
    auth.seed_review_account()
    threading.Thread(target=rag.embedder, daemon=True).start()   # load the search model now, not on the first question
    announcements.start()
    calendars.start()
    menu.start()
    clubs.start()
    yield
    announcements.stop()
    calendars.stop()
    menu.stop()
    clubs.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


def client_ip(request):
    forwarded = request.headers.get("x-forwarded-for", "")   # Render's proxy puts the real client first
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "?")


def sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


def stream_response(sources, pieces, on_complete=None):
    """Server-Sent Events: {sources}, then {delta} text as it's written, then {done} (or {error}).
    `pieces` may also yield dicts (like {"sources": [...]}) to send as they are."""
    def events():
        yield ": ok\n\n"   # comment line: bytes on the wire right away, even while the model is still waking up
        if sources is not None:
            yield sse({"sources": sources})
        parts = []
        try:
            for piece in pieces:
                if isinstance(piece, dict):
                    yield sse(piece)
                    continue
                parts.append(piece)
                yield sse({"delta": piece})
            if on_complete:
                on_complete("".join(parts).strip())
        except llm.LLMError as e:
            yield sse({"error": str(e)})
        except Exception:
            log.exception("stream failed")
            yield sse({"error": "Something went wrong on our end. Try again."})
        yield sse({"done": True})
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def update_user(username, **values):
    with engine.begin() as conn:
        conn.execute(users.update().where(users.c.username == username).values(**values))


# ---------- pages ----------
def page(name, **values):
    """A page from app/static; {{key}} placeholders are filled from `values`."""
    html = (config.STATIC_DIR / name).read_text(encoding="utf-8")
    for key, value in values.items():
        html = html.replace("{{" + key + "}}", escape(str(value)))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/")
def home_page(): return page("home.html")


@app.get("/login")
def login_page(): return page("login.html")


@app.get("/setup")
def setup_page(): return page("setup.html")


@app.get("/app")
def chat_page(): return page("SFmate.html")


@app.get("/privacy")
def privacy_page(): return page("privacy.html", support_email=config.SUPPORT_EMAIL)


@app.get("/support")
def support_page():
    return page("support.html", support_email=config.SUPPORT_EMAIL, daily_limit=config.DAILY_MESSAGE_LIMIT)


@app.get("/healthz")
def healthz():   # Render's health check: is the web server up (doesn't touch the GPU)
    return {"ok": True}


# ---------- accounts ----------
class SignupRequest(BaseModel):
    username: str
    email: str
    password: str


class VerifyRequest(BaseModel):
    email: str
    code: str


class ResendRequest(BaseModel):
    email: str


class LoginRequest(BaseModel):
    login: str      # username or email
    password: str


class GoogleRequest(BaseModel):
    credential: str


@app.get("/auth/config")
def auth_config():
    return {"google_client_id": config.GOOGLE_CLIENT_ID, "email_domain": config.ALLOWED_EMAIL_DOMAIN}


@app.post("/auth/signup")
def signup(req: SignupRequest, request: Request):
    try:
        return auth.start_signup(req.username, req.email, req.password, client_ip(request))
    except auth.mailer.MailError as e:
        raise HTTPException(502, str(e))


@app.post("/auth/resend")
def resend(req: ResendRequest, request: Request):
    try:
        return auth.resend_code(req.email, client_ip(request))
    except auth.mailer.MailError as e:
        raise HTTPException(502, str(e))


@app.post("/auth/verify")
def verify(req: VerifyRequest):
    return auth.verify_signup(req.email, req.code)


@app.post("/auth/login")
def login(req: LoginRequest, request: Request):
    return auth.login(req.login, req.password, client_ip(request))


@app.post("/auth/google")
def google_login(req: GoogleRequest):
    return auth.google_login(req.credential)


@app.post("/auth/logout")
def logout(authorization: str = Header(default="")):
    auth.logout(authorization)
    return {"ok": True}


@app.get("/auth/me")
def me(user=Depends(auth.current_user)):
    return {"name": user["name"], "username": user["username"], "email": user["email"],
            "setup_done": user["profile"] is not None, "profile": user["profile"],
            "ai_consent": auth.has_ai_consent(user["username"])}


class ConsentRequest(BaseModel):
    accepted: bool


@app.post("/consent")
def consent(req: ConsentRequest, user=Depends(auth.current_user)):
    """The app's "sfMate uses AI" screen: agreeing (or withdrawing) to questions being sent to RunPod."""
    auth.set_ai_consent(user["username"], req.accepted)
    log.info("AI consent %s: %s", "given" if req.accepted else "withdrawn", user["username"])
    return {"ok": True}


@app.get("/health")
def health(user=Depends(auth.current_user)):
    return {"ok": True}


@app.delete("/account")
def delete_account(user=Depends(auth.current_user)):
    """Deletes the account and everything tied to it, right away (the app's Delete account)."""
    auth.delete_account(user)
    log.info("account deleted: %s", user["username"])
    return {"ok": True}


# ---------- first-time setup ----------
class ProfileRequest(BaseModel):
    first_name: str = ""
    grade: int | None = None   # 9-12
    hobbies: str = ""
    favorite_food: str = ""
    announcements: list[str] | None = None   # Announcement Preferences: topic keys (None = all of them)
    skipped: bool = False      # "Yo, just get me to the chatbot"


@app.post("/profile")
def save_profile(req: ProfileRequest, user=Depends(auth.current_user)):
    if req.skipped:
        profile = {"skipped": True}
    else:
        if req.grade is not None and req.grade not in prompts.GRADE_NAMES:
            raise HTTPException(400, "Grade has to be 9, 10, 11, or 12")
        if len(req.first_name) > 40 or len(req.favorite_food) > 80 or len(req.hobbies) > 500:
            raise HTTPException(400, "That's a bit long, keep it short and sweet")
        if req.announcements is not None and not set(req.announcements) <= set(announcements.TOPIC_KEYS):
            raise HTTPException(400, "Unknown announcement topic")
        profile = {"first_name": req.first_name.strip(), "grade": req.grade,
                   "hobbies": req.hobbies.strip(), "favorite_food": req.favorite_food.strip(),
                   "announcements": [k for k in announcements.TOPIC_KEYS
                                     if req.announcements is None or k in req.announcements]}
    update_user(user["username"], profile=profile)
    return {"ok": True}


@app.post("/welcome")
def welcome(user=Depends(auth.current_user)):
    """Personalized 1-2 sentence greeting, written once per profile (wakes the GPU only when the profile changes)."""
    profile = user["profile"]
    if not prompts.student_facts(profile):
        return {"message": None}
    key = prompts.profile_key(profile)
    cached = user["welcome"]
    if cached and cached.get("for") == key:
        return {"message": cached["text"]}
    try:
        text = prompts.tidy_welcome(llm.complete(prompts.welcome_messages(profile), max_tokens=90))
    except llm.LLMError:
        return {"message": None}   # the page just keeps its generic welcome
    if text:
        update_user(user["username"], welcome={"for": key, "text": text})
    return {"message": text or None}


@app.post("/warmup")
def warmup(user=Depends(auth.current_user)):
    """The app calls this when a student opens the chat: wakes the sleeping GPU while they read and type.
    Sends the model one word and nothing about the student. Doesn't count toward the daily message limit."""
    return {"warming": llm.warm_up() is not None}


# ---------- chat ----------
class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []   # [{"role": "user"|"assistant", "content": str}, ...]


def use_one_message(username):
    """Counts a message toward today's limit; 429 once it's used up."""
    day = prompts.school_now().date().isoformat()
    with engine.begin() as conn:
        row = conn.execute(sa.select(usage.c.count).where(usage.c.username == username, usage.c.day == day)).first()
        used = row[0] if row else 0
        if used >= config.DAILY_MESSAGE_LIMIT:
            over = True
        else:
            over = False
            if row:
                conn.execute(usage.update().where(usage.c.username == username, usage.c.day == day).values(count=used + 1))
            else:
                conn.execute(usage.insert().values(username=username, day=day, count=1))
    if over:
        raise HTTPException(429, f"You've used all {config.DAILY_MESSAGE_LIMIT} of today's messages. "
                                 "They reset at midnight. See you tomorrow!")


@app.post("/chat")
def chat(req: ChatRequest, user=Depends(auth.current_user)):
    message = req.message.strip()[:2000]
    if not message:
        raise HTTPException(400, "Type a question first")
    use_one_message(user["username"])
    history = [{"role": m["role"], "content": str(m["content"])[:4000]}
               for m in req.history if m.get("role") in ("user", "assistant")][-6:]   # last 3 exchanges
    return stream_response(None, router.answer(message, history, user["profile"]))


# ---------- Daily Announcements ----------
class AnnouncementRequest(BaseModel):
    id: str


@app.get("/announcements/status")
def announcement_status(user=Depends(auth.current_user)):
    a = announcements.todays()
    if not a or user["announcement_answered"] == a["id"]:
        return {"available": False}
    return {"available": True, "id": a["id"], "date": a["label"], "message": prompts.ANNOUNCE_PROMPT}


@app.post("/announcements/dismiss")
def announcement_dismiss(req: AnnouncementRequest, user=Depends(auth.current_user)):
    update_user(user["username"], announcement_answered=req.id)   # Yes or No: either way, don't ask again
    return {"ok": True}


@app.post("/announcements/summary")
def announcement_summary(req: AnnouncementRequest, user=Depends(auth.current_user)):
    a = announcements.find(req.id)
    if not a:
        raise HTTPException(404, "Those announcements aren't available anymore")
    update_user(user["username"], announcement_answered=a["id"])
    key = router.summary_cache_key(a["id"], user["profile"])   # one summary per email per set of chosen topics
    with engine.begin() as conn:
        cached = conn.execute(sa.select(announcement_summaries.c.text)
                              .where(announcement_summaries.c.id == key)).scalar()
    if cached:   # someone with the same topics already asked: instant, no GPU
        sources = [{"n": 1, "title": f"Daily Announcements · {a['label']}", "url": ""},
                   {"n": 2, "title": "Lancer Cafe menu", "url": config.MENU_URL}]
        return stream_response(sources, iter([cached]))

    def save(text):
        if not text:
            return
        try:
            with engine.begin() as conn:
                conn.execute(announcement_summaries.insert().values(id=key, text=text))
        except sa.exc.IntegrityError:
            pass   # two students said yes at the same moment; the first summary saved wins

    return stream_response(None, router.announcement_summary(a, user["profile"]), on_complete=save)


# ---------- liturgy and activity day reminders (the iPhone app schedules them on the phone) ----------
@app.get("/reminders")
def upcoming_reminders(user=Depends(auth.current_user)):
    return {"days": reminders.upcoming(prompts.school_now())}


# ---------- feedback (the iPhone app's Send feedback) ----------
class FeedbackRequest(BaseModel):
    message: str
    app_version: str = ""


@app.post("/feedback")
def submit_feedback(req: FeedbackRequest, background: BackgroundTasks, user=Depends(auth.current_user)):
    message, version = req.message.strip(), req.app_version.strip()[:40]
    if not message:
        raise HTTPException(400, "Write something first")
    if len(message) > 4000:
        raise HTTPException(400, "That's a bit long, keep it under 4000 characters")
    auth.feedback_throttle.check(user["username"])
    with engine.begin() as conn:
        conn.execute(feedback.insert().values(username=user["username"], message=message, app_version=version,
                                              created_at=time.time()))
    background.add_task(mailer.send_feedback, user["username"], user["email"], message, version)   # after replying
    log.info("feedback from %s (%d characters)", user["username"], len(message))
    return {"ok": True}
