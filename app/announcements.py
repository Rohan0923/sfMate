"""Daily Announcements: read sfMate's Gmail inbox over IMAP (read-only), keep the last few weeks searchable,
and check for new ones on a schedule (weekdays 7:00-11:00 school time, every 5 minutes, by default)."""
import email
import email.policy
import imaplib
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from email.utils import parseaddr, parsedate_to_datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from . import config, rag

log = logging.getLogger("sfmate.announcements")

SCHOOL_TZ = ZoneInfo(config.SCHOOL_TIMEZONE)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]   # IMAP dates are English

announcements = []   # recent Daily Announcements, oldest first
_parsed = {}         # IMAP uid -> announcement (or None if skipped), so each email is downloaded once
_stop = threading.Event()


def enabled():
    return bool(config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD)


# ---------- reading one email ----------
FORWARD_HEADER = re.compile(r"-{3,}\s*Forwarded message\s*-{3,}.*?\n\s*\n", re.S | re.I)   # if forwarded by hand
BLOCK_TAGS = ["p", "div", "li", "tr", "table", "blockquote", "section", "h1", "h2", "h3", "h4", "h5", "h6"]


def html_to_text(html_body):
    """Line breaks only where the email has them (paragraphs, list items, rows, <br>), so bold words stay inline."""
    soup = BeautifulSoup(html_body, "html.parser")
    for tag in soup(["style", "script", "head"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert(0, "• ")
    for cell in soup.find_all(["td", "th"]):
        cell.append("  ")   # keep table columns apart ("Mon  Block A")
    for block in soup.find_all(BLOCK_TAGS):
        block.append("\n")
    return soup.get_text()


def clean_text(text):
    text = text.replace("\r", "").replace(chr(0x200B), "").replace("\xa0", " ")
    text = FORWARD_HEADER.sub("", text)
    out = []
    for line in text.split("\n"):
        line = " ".join(line.split())
        if line or (out and out[-1]):   # keep single blank lines between paragraphs, drop runs of them
            out.append(line)
    return "\n".join(out).strip()


def parse_email(raw, uid):
    """Returns (announcement dict, None) or (None, the reason it was skipped)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    subject = str(msg.get("Subject", ""))
    sender = parseaddr(str(msg.get("From", "")))[1].lower()
    if config.ANNOUNCE_SUBJECT.lower() not in subject.lower():
        return None, "subject"
    if not any(sender.endswith(s) for s in config.ANNOUNCE_SENDERS):
        return None, f"sender {sender or '(none)'}"
    try:
        sent = parsedate_to_datetime(str(msg["Date"])).astimezone(SCHOOL_TZ)
    except Exception:
        sent = datetime.now(SCHOOL_TZ)
    # Read both the plain-text and HTML versions and keep the fuller one: some senders put only
    # "view this email in your browser" in the plain-text part
    versions = []
    for kind in ("plain", "html"):
        part = msg.get_body(preferencelist=(kind,))
        if part is not None:
            body = part.get_content()
            versions.append(clean_text(html_to_text(body) if kind == "html" else body))
    text = max(versions, key=len, default="")
    if len(text) < 20:
        return None, "empty"
    return {
        "id": str(msg.get("Message-ID") or f"uid-{uid}").strip(),
        "sent": sent,
        "label": f"{sent:%a %b} {sent.day}, {sent.year}",   # e.g. "Fri Oct 3, 2026"
        "text": text[:20000],
    }, None


# ---------- reading the inbox ----------
def fetch():
    since = datetime.now(SCHOOL_TZ) - timedelta(days=config.ANNOUNCE_KEEP_DAYS)
    imap = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
    try:
        imap.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        imap.select("INBOX", readonly=True)   # read-only: never marks anything read, moves, or deletes
        _, data = imap.uid("SEARCH", None, "SINCE", f"{since.day:02d}-{MONTHS[since.month - 1]}-{since.year}",
                           "SUBJECT", f'"{config.ANNOUNCE_SUBJECT}"')
        for uid in data[0].split():
            if uid in _parsed:
                continue
            _, msg_data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next((part[1] for part in msg_data if isinstance(part, tuple)), None)
            ann, skipped = parse_email(raw, uid.decode()) if raw else (None, "unreadable")
            _parsed[uid] = ann
            if skipped and skipped.startswith("sender"):
                log.warning("skipped an email from %s (add it to ANNOUNCE_SENDERS if it's legit)", skipped[7:])
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    unique = {a["id"]: a for a in _parsed.values() if a and a["sent"] >= since}
    return sorted(unique.values(), key=lambda a: a["sent"])


def _index(items):
    chunks = [{"url": "", "title": f"Daily Announcements · {a['label']}", "text": c}
              for a in items for c in rag.chunk_text(a["text"])]
    embs = rag.embed([f"{c['title']}\n{c['text']}" for c in chunks]) if chunks else None
    rag.EXTRA_SOURCES["announcements"] = (chunks, embs)


def poll():
    global announcements
    items = fetch()
    if [a["id"] for a in items] != [a["id"] for a in announcements]:
        new = len({a["id"] for a in items} - {a["id"] for a in announcements})
        _index(items)
        announcements = items
        log.info("%d new announcement email(s); %d in search", new, len(items))


def todays():
    """Latest Daily Announcements sent today (school time zone), or None."""
    today = datetime.now(SCHOOL_TZ).date()
    todays_items = [a for a in announcements if a["sent"].date() == today]
    return todays_items[-1] if todays_items else None


def find(ann_id):
    return next((a for a in announcements if a["id"] == ann_id), None)


# ---------- schedule ----------
def in_check_window(now):
    start, end = config.ANNOUNCE_HOURS
    return (now.weekday() < 5 or not config.ANNOUNCE_WEEKDAYS_ONLY) and start <= now.hour < end


def _checker(stop):
    try:
        poll()   # load the last few weeks right away (after every deploy / restart)
    except Exception as e:
        log.error("first announcements check failed: %s: %s", type(e).__name__, e)
    last = time.time()
    while not stop.wait(20):
        if in_check_window(datetime.now(SCHOOL_TZ)) and time.time() - last >= config.ANNOUNCE_POLL_MINUTES * 60:
            last = time.time()
            try:
                poll()
            except Exception as e:
                log.error("announcements check failed: %s: %s", type(e).__name__, e)


def start():
    if not enabled():
        log.warning("Daily Announcements are off: set GMAIL_ADDRESS and GMAIL_APP_PASSWORD")
        return
    _stop.clear()
    threading.Thread(target=_checker, args=(_stop,), daemon=True, name="announcements").start()
    log.info("Daily Announcements on for %s", config.GMAIL_ADDRESS)


def stop():
    _stop.set()


# ---------- sections and preferences ----------
# (key, label in Announcement Preferences, how the section's heading looks in the email). Order = email order.
TOPICS = [
    ("daytype_birthdays", "Day type and Birthdays", None),   # whatever comes before the first heading
    ("highlights", "Highlights and school-wide announcements",
     r"(this week'?s )?highlights( for students)?|school ?wide announcements"),
    ("happening_today", "Happening today", r"happening today"),
    ("activities", "Activities Announcements", r"activities announcements"),
    ("clubs_events", "Clubs and Events Announcements", r"clubs (and|&) events( announcements)?"),
    ("affinity", "Affinity Groups", r"affinity groups( announcements)?"),
    ("mission_ministry", "Mission and Ministry", r"mission (and|&) ministry( announcements)?"),
    ("athletics", "Athletics", r"athletics( announcements)?"),
    ("counseling", "Counseling Announcements", r"(guidance (and|&) )?counseling( announcements)?|guidance (and|&) counseling"),
    ("library", "Library Announcements", r"library( announcements)?"),
]
TOPIC_KEYS = [key for key, _, _ in TOPICS]
TOPIC_LABELS = dict((key, label) for key, label, _ in TOPICS)
_HEADINGS = [(key, re.compile(rf"^(?:{pattern})$")) for key, _, pattern in TOPICS if pattern]


def _heading_key(line):
    norm = re.sub(r"[^a-z0-9&' ]+", " ", line.lower().replace("’", "'")).strip()
    norm = " ".join(norm.split())
    return next((key for key, pattern in _HEADINGS if pattern.match(norm)), None)


def split_sections(text):
    """[(topic key, heading as written, body)] in email order. Birthdays and anything else before the first heading
    count as "Day type and Birthdays". If no headings are recognized (the format changed), one (None, "", text)."""
    out, key, heading, body = [], "daytype_birthdays", "Birthdays & day type", []
    found = False
    for line in text.split("\n"):
        k = _heading_key(line) if len(line) <= 60 else None
        if k:
            found = True
            out.append((key, heading, "\n".join(body).strip()))
            key, heading, body = k, line.strip(), []
        else:
            body.append(line)
    out.append((key, heading, "\n".join(body).strip()))
    if not found:
        return [(None, "", text.strip())]
    return [(k, h, b) for k, h, b in out if b]


def for_date(day, exact):
    """(email, note): the latest email sent that day. If there's none and no day was asked for specifically,
    the most recent one before it, with a note saying so."""
    same_day = [a for a in announcements if a["sent"].date() == day]
    if same_day:
        return same_day[-1], ""
    if exact:
        return None, ""
    earlier = [a for a in announcements if a["sent"].date() < day]
    if not earlier:
        return None, ""
    return earlier[-1], f"No Daily Announcements email has come in for that day yet; this is the most recent one, from {earlier[-1]['label']}."


SUMMARY_SIGNS = re.compile(
    r"summar|recap|overview|tl;?dr|rundown|catch me up|what did i miss|anything (new|important|interesting)|"
    r"what'?s (in|on) (the |today'?s |this morning'?s )?announcements|"
    r"what (are|were) (the |today'?s |yesterday'?s )?(daily )?announcements", re.I)


FILLER = set("the a an daily announcement announcements what what's whats are were was is in on for any new show me "
             "give read tell get today today's todays yesterday yesterday's this morning morning's s".split())
DAY_WORDS = re.compile(r"^(mon|tues?|wed(nes)?|thu(rs)?|fri|sat(ur)?|sun)(day)?('s)?$")


def wants_summary(question):
    """Summaries use the student's Announcement Preferences; specific questions read the whole email.
    "today's announcements?" is a summary; "did the announcements mention DECA?" is specific."""
    words = re.findall(r"[a-z']+", question.lower())
    rest = [w for w in words if w not in FILLER and not DAY_WORDS.match(w)]
    return bool(SUMMARY_SIGNS.search(question)) or (any(w.startswith("announcement") for w in words) and not rest)
