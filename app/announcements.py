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
last_checked = None  # when the inbox was last read successfully (None: not yet, e.g. right after a deploy)
_parsed = {}         # IMAP uid -> announcement (or None if skipped), so each email is downloaded once
_stop = threading.Event()
_poll_lock = threading.Lock()
_last_attempt = 0.0
ON_DEMAND_COOLDOWN = 60   # seconds between inbox checks triggered by questions


def enabled():
    return bool(config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD)


# ---------- reading one email ----------
SHORT_EMAIL = 600   # a real Daily Announcements email is thousands of characters
FORWARD_HEADER = re.compile(r"-{3,}\s*Forwarded message\s*-{3,}.*?\n\s*\n", re.S | re.I)   # if forwarded by hand
BLOCK_TAGS = ["p", "div", "li", "tr", "table", "blockquote", "section", "h1", "h2", "h3", "h4", "h5", "h6"]


INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad\u034f\u180e"), None)


def html_to_text(html_body, email=False):
    """Line breaks only where the email has them (paragraphs, list items, rows, <br>), so bold words stay inline.
    Nothing is removed based on styling: templates put "invisible" styles (like font-size:0) on the cells that hold
    the real content. Newsletter preview-text junk is zero-width characters, which clean_text strips."""
    soup = BeautifulSoup(html_body, "html.parser")
    for tag in soup(["style", "script", "head", "title"]):
        tag.decompose()
    if email:
        for img in soup.find_all("img"):   # section banners drawn as images: keep their heading text
            alt = " ".join((img.get("alt") or "").split())
            if alt and _heading_key(alt):
                img.replace_with(f"\n{alt}\n")
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
    text = text.replace("\r", "").translate(INVISIBLE).replace("\xa0", " ").replace("\t", " ")
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
    # Read every plain-text and HTML part (including an email forwarded as an attachment) and keep the fullest:
    # some senders put only "view this email in your browser" in one of them
    versions = []
    for part in msg.walk():
        kind = part.get_content_type()
        if kind not in ("text/plain", "text/html"):
            continue
        try:
            body = part.get_content()
        except Exception:
            continue
        versions.append(clean_text(html_to_text(body, email=True) if kind == "text/html" else body))
    text = max(versions, key=len, default="")
    if len(text) < 20:
        return None, "empty"
    return {
        "id": str(msg.get("Message-ID") or f"uid-{uid}").strip(),
        "sent": sent,
        "label": f"{sent:%a %b} {sent.day}, {sent.year}",   # e.g. "Fri Oct 3, 2026"
        "subject": subject,
        "sender": sender,
        "text": text[:40000],
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
            try:
                ann, skipped = parse_email(raw, uid.decode()) if raw else (None, "unreadable")
            except Exception as e:   # one odd email must not take the whole inbox down
                log.error("couldn't read email uid %s: %s: %s", uid.decode(), type(e).__name__, e)
                continue
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


def poll(index_in_background=False):
    """Reads the inbox. New emails are usable by the announcement path right away; the search index follows
    (building it loads the search model, which can take minutes right after a deploy)."""
    global announcements, last_checked
    with _poll_lock:
        items = fetch()
        last_checked = time.time()
        if [a["id"] for a in items] == [a["id"] for a in announcements]:
            return
        known = {a["id"] for a in announcements}
        for a in items:
            if a["id"] not in known:
                found = [k for k, _, _ in split_sections(a["text"]) if k]
                log.info("announcement email %s from %s, %r: %d characters, sections found: %s",
                         a["label"], a.get("sender"), a.get("subject"), len(a["text"]), ", ".join(found) or "none")
                if len(a["text"]) < SHORT_EMAIL:
                    log.warning("announcement email %s is only %d characters: %r", a["label"], len(a["text"]), a["text"][:300])
        announcements = items
        log.info("%d new announcement email(s); %d in the inbox", len({a["id"] for a in items} - known), len(items))
    if index_in_background:
        threading.Thread(target=_safe_index, args=(items,), daemon=True, name="announcements-index").start()
    else:
        _index(items)


def _safe_index(items):
    try:
        _index(items)
    except Exception as e:
        log.error("couldn't add announcements to search: %s: %s", type(e).__name__, e)


def refresh_now(reason):
    """An inbox check right now, when a question needs an email we don't have (at most once a minute)."""
    global _last_attempt
    if not enabled() or time.time() - _last_attempt < ON_DEMAND_COOLDOWN:
        return False
    _last_attempt = time.time()
    log.info("checking the inbox now: %s", reason)
    try:
        poll(index_in_background=True)
        return True
    except Exception as e:
        log.error("inbox check failed (%s): %s: %s", reason, type(e).__name__, e)
        return False


def todays():
    """Latest Daily Announcements sent today (school time zone), or None."""
    return fullest([a for a in announcements if a["sent"].date() == datetime.now(SCHOOL_TZ).date()])


def fullest(emails):
    """Of several matching emails on one day (the real one, a reminder, a forward...), the one with the most in it."""
    return max(emails, key=lambda a: len(a["text"]), default=None)


def find(ann_id):
    return next((a for a in announcements if a["id"] == ann_id), None)


# ---------- schedule ----------
def in_check_window(now):
    start, end = config.ANNOUNCE_HOURS
    return (now.weekday() < 5 or not config.ANNOUNCE_WEEKDAYS_ONLY) and start <= now.hour < end


def _wait_seconds(now):
    if last_checked is None:
        return 60   # the inbox has never been read (just deployed, or Gmail was down): keep trying
    if in_check_window(now):
        return config.ANNOUNCE_POLL_MINUTES * 60
    return config.ANNOUNCE_OFF_HOURS_MINUTES * 60   # a late email, or a morning that failed, still gets picked up


def _checker(stop):
    while True:
        try:
            poll()
        except Exception as e:
            log.error("announcements check failed: %s: %s", type(e).__name__, e)
        if stop.wait(_wait_seconds(datetime.now(SCHOOL_TZ))):
            return


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
    same_day = fullest([a for a in announcements if a["sent"].date() == day])
    if same_day:
        return same_day, ""
    if exact:
        return None, ""
    earlier = [a for a in announcements if a["sent"].date() < day]
    if not earlier:
        return None, ""
    latest_day = earlier[-1]["sent"].date()
    best = fullest([a for a in earlier if a["sent"].date() == latest_day])
    return best, f"No Daily Announcements email has come in for that day yet; this is the most recent one, from {best['label']}."


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
