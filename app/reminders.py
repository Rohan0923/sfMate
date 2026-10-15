"""Liturgy and activity day reminders for the iPhone app. The app schedules each one itself (local notifications),
so the server only says which days are coming up, when to remind, and what the notification says: 6 PM school time
the calendar day before every L1/L2 (liturgy) and A1-A4 (activity schedule) day on the School Schedule calendar."""
import re
from datetime import datetime, time, timedelta

from . import calendars
from .calendars import SCHOOL_TZ, _clock, _day

SPECIAL_DAY = re.compile(r"^(L[12]|A[1-4])\b[\s\-–—:]*(.*)$", re.I)   # "L1 - Mass of St. Francis", "A1"
REMIND_AT = time(18)   # 6 PM school time, the day before
MAX_DAYS = 50          # iOS keeps at most 64 pending notifications per app
KINDS = {"L": ("liturgy", "Liturgy tomorrow", "liturgy schedule"),
         "A": ("activity", "Activity day tomorrow", "special activity schedule")}


def upcoming(now):
    """Every L/A day whose reminder time hasn't passed yet, earliest first, with the notification's text."""
    out, seen = [], set()
    for ev in calendars.events:
        m = SPECIAL_DAY.match(ev["title"])
        if not m or not calendars.is_day_schedule(ev):
            continue
        day = _day(ev["start"])
        remind_at = datetime.combine(day - timedelta(days=1), REMIND_AT, SCHOOL_TZ)
        if remind_at <= now or day in seen:
            continue
        seen.add(day)
        code, name = m.group(1).upper(), m.group(2).strip()
        kind, title, plain = KINDS[code[0]]
        body = f"{day:%a, %b} {day.day} is an {code} day" + (f": {name}." if name else f" ({plain}).")
        if not ev["all_day"]:
            body += f" School starts at {_clock(ev['start'])}."
        out.append({"date": day.isoformat(), "kind": kind, "day_type": code,
                    "remind_at": remind_at.isoformat(), "title": title, "body": body})
    return out[:MAX_DAYS]
