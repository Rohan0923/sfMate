"""All settings come from environment variables (set them in Render's dashboard; locally, use a .env file)."""
import os
from pathlib import Path

try:   # local development convenience; on Render the variables are set in the dashboard
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _env(name, default=""):
    return os.environ.get(name, default).strip()


ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "app" / "static"
DATA_DIR = ROOT / "data"

APP_ENV = _env("APP_ENV", "development")          # "production" on Render
IS_PRODUCTION = APP_ENV == "production"

# Database: Render provides DATABASE_URL for its Postgres; locally this falls back to a SQLite file
DATABASE_URL = _env("DATABASE_URL", f"sqlite:///{ROOT / 'sfmate-dev.db'}")

# The model, served by vLLM on a RunPod Serverless endpoint (OpenAI-compatible API)
RUNPOD_API_KEY = _env("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = _env("RUNPOD_ENDPOINT_ID")
LLM_BASE_URL = _env("LLM_BASE_URL") or (
    f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/openai/v1" if RUNPOD_ENDPOINT_ID else "")
LLM_MODEL = _env("LLM_MODEL", "RedHatAI/Qwen3.5-9B-FP8-dynamic")   # must match the model the endpoint serves
LLM_FIRST_TOKEN_TIMEOUT = int(_env("LLM_FIRST_TOKEN_TIMEOUT", "300"))   # seconds; covers a cold start
LLM_WARMUP_INTERVAL = int(_env("LLM_WARMUP_INTERVAL", "300"))   # seconds; at most one warm-up ping per this long
# Keep a GPU worker running (model loaded) during these school-time hours, like "12-13"; blank = let it sleep. It is billed
# the whole time, idle or not (RunPod "active worker" rate), so every hour here costs real money.
GPU_WARM_HOURS = tuple(int(h) for h in _env("GPU_WARM_HOURS", "").split("-")) if _env("GPU_WARM_HOURS", "") else ()
GPU_WARM_WEEKDAYS_ONLY = _env("GPU_WARM_WEEKDAYS_ONLY", "false").lower() == "true"
GPU_WARM_LEAD_MINUTES = int(_env("GPU_WARM_LEAD_MINUTES", "10"))   # start this early: loading the model takes a few minutes

# Search index built by scripts/build_index.py
EMBED_MODEL_ID = "BAAI/bge-base-en-v1.5"

# Accounts: anyone can sign up with any email. An account whose verified email is at this domain is a "school account":
# it also gets the Daily Announcements, the one source that's internal to the school (everything else sfMate reads is public)
SCHOOL_EMAIL_DOMAIN = _env("SCHOOL_EMAIL_DOMAIN", "sfhs.com").lower().lstrip("@")
SESSION_DAYS = int(_env("SESSION_DAYS", "30"))
DAILY_MESSAGE_LIMIT = int(_env("DAILY_MESSAGE_LIMIT", "40"))   # per school account per day; keeps the GPU bill in check
# Anyone can sign up, so accounts that aren't school accounts get fewer (a throwaway email is a free extra account)
OTHER_DAILY_MESSAGE_LIMIT = int(_env("OTHER_DAILY_MESSAGE_LIMIT", "15"))
GOOGLE_CLIENT_ID = _env("GOOGLE_CLIENT_ID")   # optional; "" turns Google sign-in off

# App Store review account: so Apple's reviewers don't need an inbox for the emailed code, with both set this account
# exists (no email needed) with this password. It isn't a school account, so it never sees the Daily Announcements.
# Blank = no review account.
REVIEW_USERNAME = _env("REVIEW_USERNAME").lower()
REVIEW_PASSWORD = _env("REVIEW_PASSWORD")

# Shown on /support and /privacy; feedback from the app is emailed here
SUPPORT_EMAIL = _env("SUPPORT_EMAIL", "sfmate23@gmail.com")

# The sfMate Gmail account: reads Daily Announcements (IMAP) and sends verification codes (SMTP)
GMAIL_ADDRESS = _env("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = _env("GMAIL_APP_PASSWORD").replace(" ", "")   # Google shows it in groups of 4

# Daily Announcements
ANNOUNCE_SUBJECT = _env("ANNOUNCE_SUBJECT", "Daily Announcements")
ANNOUNCE_SENDERS = [s.strip().lower() for s in _env("ANNOUNCE_SENDERS", "@sfhs.com").split(",") if s.strip()]
ANNOUNCE_KEEP_DAYS = int(_env("ANNOUNCE_KEEP_DAYS", "21"))
ANNOUNCE_POLL_MINUTES = float(_env("ANNOUNCE_POLL_MINUTES", "5"))
ANNOUNCE_HOURS = tuple(int(h) for h in _env("ANNOUNCE_HOURS", "7-11").split("-"))   # check from 7:00 until 11:00
ANNOUNCE_OFF_HOURS_MINUTES = float(_env("ANNOUNCE_OFF_HOURS_MINUTES", "60"))   # how often to check the rest of the day
ANNOUNCE_WEEKDAYS_ONLY = _env("ANNOUNCE_WEEKDAYS_ONLY", "true").lower() == "true"
SCHOOL_TIMEZONE = _env("SCHOOL_TIMEZONE", "America/Los_Angeles")

# School calendars: public iCal (.ics) feeds, comma-separated. Default: SFHS Activities & Events, SFHS School Schedule
_SFHS_CALENDARS = (
    "https://calendar.google.com/calendar/ical/c_f262940ce7d5cc680ca47b0e9632365ec5685a98b219bc6305a4c75287286004"
    "%40group.calendar.google.com/public/basic.ics,"
    "https://calendar.google.com/calendar/ical/c_499267f7da4e582cf1204186abd13af7b0e2369be2d471856f5af64f3a9a0454"
    "%40group.calendar.google.com/public/basic.ics")
CALENDAR_ICS_URLS = [u.strip().replace("webcal://", "https://", 1)
                     for u in _env("CALENDAR_ICS_URLS", _SFHS_CALENDARS).split(",") if u.strip()]
CALENDAR_REFRESH_MINUTES = float(_env("CALENDAR_REFRESH_MINUTES", "30"))

# Lancer Cafe menu page (read live); "" turns it off
MENU_URL = _env("MENU_URL", "https://www.sfhs.com/lancer-cafe")
MENU_REFRESH_MINUTES = float(_env("MENU_REFRESH_MINUTES", "120"))

# Clubs & Affinity Groups page (read live, its 7 dropdowns split into clubs, with moderators); "" turns it off
CLUBS_URL = _env("CLUBS_URL", "https://www.sfhs.com/clubs-affinity-groups")
CLUBS_REFRESH_MINUTES = float(_env("CLUBS_REFRESH_MINUTES", "360"))
