"""School calendars from public iCal (.ics) feeds: refreshed in the background, every event searchable, and the next
two weeks handed straight to the model (search alone can't answer "what's happening this week?")."""
import logging
import threading
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import icalendar
import numpy as np
import recurring_ical_events
import requests

from . import config, rag
from .announcements import clean_text, html_to_text

log = logging.getLogger("sfmate.calendars")

SCHOOL_TZ = ZoneInfo(config.SCHOOL_TIMEZONE)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; sfMate/1.0)"}
KEEP_PAST_DAYS, KEEP_FUTURE_DAYS = 7, 400
DESCRIPTION_CHARS = 600

events = []        # every event from every feed, sorted by start
_feeds = {}        # url -> (label, events): a feed that fails to refresh keeps its last good copy
_emb_cache = {}    # event text -> embedding, so a refresh only embeds new or changed events
_stop = threading.Event()


# ---------- reading a feed ----------
def _local(value):
    """iCal start/end -> a date (all-day) or a datetime in school time."""
    if isinstance(value, datetime):
        return value.astimezone(SCHOOL_TZ) if value.tzinfo else value.replace(tzinfo=SCHOOL_TZ)
    return value


def _day(value):
    return value.date() if isinstance(value, datetime) else value


def parse_feed(raw, today, fallback_label):
    """Returns (calendar name, events between a week ago and about a year out), recurring events expanded."""
    cal = icalendar.Calendar.from_ical(raw)
    label = str(cal.get("X-WR-CALNAME", "")).strip() or fallback_label
    window_start, window_end = today - timedelta(days=KEEP_PAST_DAYS), today + timedelta(days=KEEP_FUTURE_DAYS)
    out = []
    for ev in recurring_ical_events.of(cal).between(window_start, window_end):
        title = " ".join(str(ev.get("SUMMARY", "")).split())
        if not title or str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        start = _local(ev.decoded("DTSTART"))
        all_day = not isinstance(start, datetime)
        end = _local(ev.decoded("DTEND")) if ev.get("DTEND") else None
        if all_day:   # iCal all-day end dates are exclusive: Oct 1 -> Oct 3 means Oct 1 and Oct 2
            end = (end - timedelta(days=1)) if isinstance(end, date) and end > start else start
        description = str(ev.get("DESCRIPTION", ""))
        description = clean_text(html_to_text(description) if "<" in description else description)
        out.append({"calendar": label, "title": title, "start": start, "end": end, "all_day": all_day,
                    "location": " ".join(str(ev.get("LOCATION", "")).split()),
                    "description": description[:DESCRIPTION_CHARS]})
    return label, out


# ---------- describing an event ----------
def _clock(dt):
    return f"{dt:%I:%M %p}".lstrip("0")


def day_label(d):
    return f"{d:%a %b} {d.day}, {d.year}"


def when(ev):
    first, last = ev["start"], ev["end"]
    if ev["all_day"]:
        return day_label(first) if last == first else f"{day_label(first)} – {day_label(last)}"
    text = f"{day_label(first)}, {_clock(first)}"
    if isinstance(last, datetime) and last > first:
        text += f" – {_clock(last)}" if last.date() == first.date() else f" – {day_label(last)}, {_clock(last)}"
    return text


def event_text(ev):
    lines = [ev["title"], f"When: {when(ev)}"]
    if ev["location"]:
        lines.append(f"Where: {ev['location']}")
    if ev["description"]:
        lines.append(ev["description"])
    return "\n".join(lines)


def upcoming_text(now, days=None, max_events=None):
    """The next two weeks, one line per event, for the model's instructions. "" if there's nothing."""
    days = days or config.CALENDAR_PROMPT_DAYS
    max_events = max_events or config.CALENDAR_PROMPT_MAX_EVENTS
    today = now.date()
    soon = [ev for ev in events if _day(ev["end"] or ev["start"]) >= today and _day(ev["start"]) < today + timedelta(days=days)]
    lines = []
    for ev in soon[:max_events]:
        line = f"- {when(ev)}: {ev['title']}"
        if ev["location"]:
            line += f" @ {ev['location']}"
        line += f" [{ev['calendar']}]"
        if _day(ev["start"]) <= today and ev["description"]:   # today's details (like the bell schedule) in full
            line += "".join(f"\n  {d}" for d in ev["description"][:300].split("\n") if d.strip())
        lines.append(line)
    if len(soon) > max_events:
        lines.append(f"- …and {len(soon) - max_events} more (ask about a specific date or event)")
    return "\n".join(lines)


# ---------- refreshing ----------
def _index(items):
    chunks = [{"url": "", "title": f"{ev['calendar']} · {ev['title']} · {day_label(_day(ev['start']))}",
               "text": event_text(ev)} for ev in items]
    texts = [f"{c['title']}\n{c['text']}" for c in chunks]
    new = [t for t in dict.fromkeys(texts) if t not in _emb_cache]
    if new:
        _emb_cache.update(zip(new, rag.embed(new)))
    for stale in set(_emb_cache) - set(texts):
        del _emb_cache[stale]
    embs = np.stack([_emb_cache[t] for t in texts]) if texts else None
    rag.EXTRA_SOURCES["calendars"] = (chunks, embs)


def school_today():
    return datetime.now(SCHOOL_TZ).date()


def refresh():
    global events
    today = school_today()
    for n, url in enumerate(config.CALENDAR_ICS_URLS, 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            _feeds[url] = parse_feed(r.content, today, f"School calendar {n}")
        except Exception as e:
            log.error("calendar %d failed to refresh (keeping the last copy): %s: %s", n, type(e).__name__, e)
    merged, seen = [], set()
    for _, feed_events in _feeds.values():
        for ev in feed_events:
            key = (ev["title"].lower(), ev["start"], ev["end"])
            if key not in seen:   # the same event on two calendars shows up once
                seen.add(key)
                merged.append(ev)
    merged.sort(key=lambda ev: (_day(ev["start"]), not ev["all_day"],
                                ev["start"] if isinstance(ev["start"], datetime) else datetime.combine(ev["start"], time(), SCHOOL_TZ)))
    _index(merged)
    events = merged
    log.info("calendars: %d events from %d feed(s)", len(merged), len(_feeds))


def _refresher(stop):
    while True:
        try:
            refresh()
        except Exception as e:
            log.error("calendar refresh failed: %s: %s", type(e).__name__, e)
        if stop.wait(config.CALENDAR_REFRESH_MINUTES * 60):
            return


def start():
    if not config.CALENDAR_ICS_URLS:
        log.warning("school calendars are off: set CALENDAR_ICS_URLS")
        return
    _stop.clear()
    threading.Thread(target=_refresher, args=(_stop,), daemon=True, name="calendars").start()


def stop():
    _stop.set()
