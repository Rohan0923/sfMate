"""Database tables and connection. SQLAlchemy Core, so the same code runs on Postgres (Render) and SQLite (local)."""
import sqlalchemy as sa

from . import config

metadata = sa.MetaData()

users = sa.Table(
    "users", metadata,
    sa.Column("username", sa.String(255), primary_key=True),       # lowercase; Google accounts use their email
    sa.Column("email", sa.String(255), nullable=False, unique=True),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("password_hash", sa.String(255)),                    # None for Google-only accounts
    sa.Column("google_sub", sa.String(255), unique=True),
    sa.Column("profile", sa.JSON),                                  # setup page answers; {"skipped": true}; or None
    sa.Column("welcome", sa.JSON),                                  # cached greeting: {"for": profile key, "text": ...}
    sa.Column("announcement_answered", sa.String(512)),             # id of the last announcement they said yes/no to
    sa.Column("created_at", sa.Float, nullable=False),
)

sessions = sa.Table(
    "sessions", metadata,
    sa.Column("token_hash", sa.String(64), primary_key=True),       # sha256 of the token; the token itself isn't stored
    sa.Column("username", sa.String(255), sa.ForeignKey("users.username", ondelete="CASCADE"), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)

pending_signups = sa.Table(   # sign-ups waiting for their emailed code
    "pending_signups", metadata,
    sa.Column("email", sa.String(255), primary_key=True),
    sa.Column("username", sa.String(255), nullable=False),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("password_hash", sa.String(255), nullable=False),
    sa.Column("code_hash", sa.String(255), nullable=False),
    sa.Column("expires_at", sa.Float, nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False, default=0),
    sa.Column("last_sent_at", sa.Float, nullable=False),
)

usage = sa.Table(   # messages per student per day, for DAILY_MESSAGE_LIMIT
    "usage", metadata,
    sa.Column("username", sa.String(255), primary_key=True),
    sa.Column("day", sa.String(10), primary_key=True),             # YYYY-MM-DD, school time zone
    sa.Column("count", sa.Integer, nullable=False, default=0),
)

announcement_summaries = sa.Table(   # one shared summary per announcement email
    "announcement_summaries", metadata,
    sa.Column("id", sa.String(512), primary_key=True),
    sa.Column("text", sa.Text, nullable=False),
)


feedback = sa.Table(   # "Send feedback" in the app; each one is also emailed to SUPPORT_EMAIL
    "feedback", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("username", sa.String(255), nullable=False),
    sa.Column("message", sa.Text, nullable=False),
    sa.Column("app_version", sa.String(40), nullable=False, default=""),
    sa.Column("created_at", sa.Float, nullable=False),
)


def _normalize(url):
    # Render hands out postgres:// URLs; SQLAlchemy wants postgresql+psycopg://
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_engine(url=None):
    url = _normalize(url or config.DATABASE_URL)
    if url.startswith("sqlite"):
        engine = sa.create_engine(url, connect_args={"check_same_thread": False})
        sa.event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    else:
        engine = sa.create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    metadata.create_all(engine)   # creates any missing tables; existing data is left alone
    return engine


engine = make_engine()
