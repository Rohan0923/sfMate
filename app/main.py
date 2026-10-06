"""sfMate web server.

Pages:  /  home    /login  sign up & log in    /setup  first-time profile    /app  the chatbot
API:    /auth/*  accounts    /profile    /welcome    /chat (streamed)    /announcements/*
"""
import json
import logging
import threading
import time
from contextlib import asynccontextmanager

import sqlalchemy as sa
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from . import announcements, auth, calendars, config, llm, prompts, rag
from .db import announcement_summaries, engine, usage, users

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("sfmate")


@asynccontextmanager
async def lifespan(app):
    rag.load_index()
    threading.Thread(target=rag.embedder, daemon=True).start()   # load the search model now, not on the first question
    announcements.start()
    calendars.start()
    yield
    announcements.stop()
    calendars.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


def client_ip(request):
    forwarded = request.headers.get("x-forwarded-for", "")   # Render's proxy puts the real client first
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "?")


def sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


def stream_response(sources, pieces, on_complete=None):
    """Server-Sent Events: {sources}, then {delta} text as it's written, then {done} (or {error})."""
    def events():
        yield sse({"sources": sources})
        parts = []
        try:
            for piece in pieces:
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
def page(name):
    html = (config.STATIC_DIR / name).read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/")
def home_page(): return page("home.html")


@app.get("/login")
def login_page(): return page("login.html")


@app.get("/setup")
def setup_page(): return page("setup.html")


@app.get("/app")
def chat_page(): return page("SFmate.html")


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
            "setup_done": user["profile"] is not None, "profile": user["profile"]}


@app.get("/health")
def health(user=Depends(auth.current_user)):
    return {"ok": True}


# ---------- first-time setup ----------
class ProfileRequest(BaseModel):
    first_name: str = ""
    grade: int | None = None   # 9-12
    hobbies: str = ""
    favorite_food: str = ""
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
        profile = {"first_name": req.first_name.strip(), "grade": req.grade,
                   "hobbies": req.hobbies.strip(), "favorite_food": req.favorite_food.strip()}
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
    messages, hits = prompts.rag_messages(message, history, user["profile"])
    sources = [{"n": i, "title": c["title"], "url": c["url"]} for i, (c, _) in enumerate(hits, 1)]
    return stream_response(sources, llm.stream_chat(messages))


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
    sources = [{"n": 1, "title": f"Daily Announcements · {a['label']}", "url": ""}]
    with engine.begin() as conn:
        cached = conn.execute(sa.select(announcement_summaries.c.text)
                              .where(announcement_summaries.c.id == a["id"])).scalar()
    if cached:   # someone already asked: instant, no GPU
        return stream_response(sources, iter([cached]))

    def save(text):
        if not text:
            return
        try:
            with engine.begin() as conn:
                conn.execute(announcement_summaries.insert().values(id=a["id"], text=text))
        except sa.exc.IntegrityError:
            pass   # two students said yes at the same moment; the first summary saved wins

    return stream_response(sources, llm.stream_chat(prompts.summary_messages(a), max_tokens=350), on_complete=save)
