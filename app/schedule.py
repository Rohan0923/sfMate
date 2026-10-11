"""The calendar path: everything the model reads for a question about dates and the school schedule.

Dates are worked out in code (app/dates.py). A question with a time frame gets every calendar entry in it, day by
day. A question without one ("when is the next liturgy?") gets the next 14 days plus every remaining day of the
school year that matches it (day types, words in event names, search). After the draft, the model re-checks the
full entries of the dates it picked (full_days)."""
import re
from datetime import date, datetime, timedelta

from . import calendars, dates
from .calendars import _day, _clock, day_label

SCHEDULE_SIGNS = re.compile(
    r"\b(schedules?|bell|calendar|events?|happening|going on|no school|days? off|holidays?|breaks?|vacation|"
    r"late start|early (?:dismissal|release|out)|minimum day|half day|school (?:start|starts|end|ends|out|over|day)|"
    r"is there (?:school|class)|do we have (?:school|class)|have school|open house|rally|rallies|assembl(?:y|ies)|"
    r"mass|liturg\w*|retreats?|concerts?|dances?|homecoming|prom|finals?|musical|tailgate|info(?:rmation)? night|"
    r"[abgl][1-4](?: day)?|what day is|upcoming|when(?:'s| is| are| does| do| will| was)|what time|what date|"
    r"how (?:long|many days) until)\b", re.I)
POLICY_SIGNS = re.compile(
    r"\b(allowed|policy|policies|rules?|dress code|dress|wear|uniform|prerequisites?|credits?|graduat\w*|"
    r"requirements?|handbook|detention|discipline|consequences?|tardy|tardies|absen\w*|excused|"
    r"can i (?:bring|wear|use|leave|go|have)|am i able|is it ok)\b", re.I)
NEAR_DAYS = 14        # questions without dates always see this many days, day by day
DETAIL_DAYS = 7       # time frames up to a week also get bell schedules and event descriptions
MAX_MATCHES = 40      # long-term matches handed over, earliest first
FOLLOW_UP_WORDS = 6

DAY_TYPES = (
    "Day types on the SFHS School Schedule calendar: G1, B1, G2, B2 are the regular rotation, in that order "
    "(G = Gold day, B = Brown day; each has a different class order). L1 and L2 are liturgy days (a Mass or prayer "
    "service). A1-A4 are special activity schedules that come up at random (A has nothing to do with B). Rare ones "
    "(virtual days, white days, flex days) are listed by name: just tell the student about them. NO SCHOOL means "
    "no classes that day."
)

# words in a question -> which calendar entries count as matches when scanning the rest of the year.
# The last flag: only look at the School Schedule calendar (student liturgies are L days there, not parent Masses).
HINTS = [
    (re.compile(r"liturg|\bmass(es)?\b|prayer service", re.I), re.compile(r"^L\d|\bmass\b|liturg|prayer service", re.I), True),
    (re.compile(r"activit(y|ies) (day|schedule)|\ba days?\b", re.I), re.compile(r"^A\d", re.I), True),
    (re.compile(r"gold days?", re.I), re.compile(r"^G\d", re.I), True),
    (re.compile(r"brown days?", re.I), re.compile(r"^B\d", re.I), True),
    (re.compile(r"no school|days? off|holiday|breaks?\b|vacation|long weekend", re.I),
     re.compile(r"no school|holiday|break", re.I), False),
    (re.compile(r"finals?\b|final exams?|exam week", re.I), re.compile(r"final", re.I), False),
]
STOP = set("when what where which whose next last first school schools there have does will this that with from "
           "about upcoming coming time date dates days week weeks year happening going event events calendar "
           "anything something more much many long until does doing is are was were the and for period periods".split())


def wants_schedule(text, today):
    return bool((dates.resolve(text, today) or SCHEDULE_SIGNS.search(text)) and not POLICY_SIGNS.search(text))


# ---------- describing entries ----------
def _span(ev):
    first, last = _day(ev["start"]), _day(ev["end"] or ev["start"])
    if ev["all_day"]:
        return "all day" if first == last else f"all day, {day_label(first)} – {day_label(last)}"
    text = _clock(ev["start"])
    if isinstance(ev["end"], datetime) and ev["end"] > ev["start"]:
        text += f" – {_clock(ev['end'])}" if ev["end"].date() == ev["start"].date() else f" – {day_label(last)}, {_clock(ev['end'])}"
    return text


def _ended(ev, now):
    end = ev["end"] if isinstance(ev["end"], datetime) else None
    if end:
        return end <= now
    return _day(ev["end"] or ev["start"]) < now.date()


def _details(ev, indent="    "):
    return "".join(f"\n{indent}{line}" for line in ev["description"][:250].split("\n")
                   if line.strip() and "full bell schedule" not in line.lower())   # link text, not information


def _day_tag(d, today):
    if d == today:
        return " (TODAY)"
    if d == today + timedelta(days=1):
        return " (TOMORROW)"
    return " (already past)" if d < today else ""


def _numbering(first_n):
    """Source numbers per calendar, the same in every step (so the re-check cites the same [n] as the draft)."""
    labels = sorted({ev["calendar"] for ev in calendars.events})
    num = {label: n for n, label in enumerate(labels, first_n)}
    return num, [{"n": n, "title": label, "url": ""} for label, n in num.items()]


def _render_day(d, events, num, now, detailed):
    today = now.date()
    todays = [ev for ev in events if _day(ev["start"]) <= d <= _day(ev["end"] or ev["start"])]
    day_entries = [ev for ev in todays if calendars.is_day_schedule(ev)]
    others = [ev for ev in todays if not calendars.is_day_schedule(ev)]
    out = [f"\n{d:%A, %B} {d.day}, {d.year}{_day_tag(d, today)}"]
    if any("no school" in ev["title"].lower() for ev in day_entries):
        status = "NO SCHOOL"
    elif day_entries:
        status = "school day: " + ", ".join(ev["title"] for ev in day_entries)
    elif d.weekday() >= 5:
        status = "weekend (no school day listed)"
    else:
        status = "no school-day entry on the calendar"
    refs = " ".join(sorted({f"[{num[ev['calendar']]}]" for ev in day_entries}))
    out.append(f"  {status} {refs}".rstrip())
    if detailed:
        for ev in day_entries:
            if not ev["all_day"]:
                out.append(f"    hours: {_span(ev)}")
            out[-1] += _details(ev, "    ")
    for ev in others:
        line = f"  - {_span(ev)}: {ev['title']}"
        if ev["location"]:
            line += f" @ {ev['location']}"
        line += f" [{num[ev['calendar']]}]"
        if d == today and _ended(ev, now):
            line += " (already over)"
        if detailed:
            line += _details(ev)
        out.append(line)
    return out, bool(todays)


def _agenda_lines(start, end, events, num, now):
    detailed = (end - start).days < DETAIL_DAYS
    out = [f"Calendar entries from {day_label(start)} through {day_label(end)}, day by day:"]
    d = start
    while d <= end:
        lines, has_entries = _render_day(d, events, num, now, detailed)
        if has_entries or (end - start).days <= 31:   # long ranges: skip empty days
            out += lines
        d += timedelta(days=1)
    return out


# ---------- scanning the rest of the school year ----------
def school_year_end(today):
    days = [_day(ev["start"]) for ev in calendars.events if calendars.is_day_schedule(ev)]
    last = max(days, default=today)
    return last if last > today else today + timedelta(days=300)


def scan(question, today):
    """Every entry from today to the end of the school year that matches the question, in date order."""
    end = school_year_end(today)
    pool = calendars.events_between(today, end)
    title_patterns = [(title, schedule_only) for asks, title, schedule_only in HINTS if asks.search(question)]
    codes = {f"{l.upper()}{n}" for l, n in re.findall(r"\b([abgl])\s?([1-4])\b", question.lower())}
    words = {w.rstrip("s") for w in re.findall(r"[a-z]{4,}", question.lower()) if w not in STOP}

    def by_hint(ev):
        return any(p.search(ev["title"]) and (calendars.is_day_schedule(ev) or not only) for p, only in title_patterns)

    def by_name(ev):   # the question's own words, or a day code like "A1": stronger than a general hint
        title = ev["title"].lower()
        return (any(ev["title"].upper().startswith(c) for c in codes)
                or any(re.search(rf"\b{re.escape(w)}", title) for w in words))

    strong = [ev for ev in pool if by_name(ev)]
    weak = [ev for ev in pool if by_hint(ev) and not by_name(ev)]
    found = (strong + weak)[:MAX_MATCHES]   # "spring break": the "spring" matches get first claim on the slots
    if not found:   # no day type or event name matched: fall back to meaning-based search ("homecoming" -> "Spirit Week")
        upcoming = {id(ev) for ev in pool}
        found = [ev for ev in calendars.search(question, 8) if id(ev) in upcoming]
    return sorted(found, key=lambda ev: _day(ev["start"]))[:MAX_MATCHES], end


# ---------- what the model reads ----------
def time_frame_for(question, history, today):
    prev = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    tf = dates.resolve(question, today)
    if not tf and prev and len(question.split()) <= FOLLOW_UP_WORDS:
        tf = dates.resolve(prev, today)
    return tf


def context(question, history, now, first_n=1):
    """(text, sources, time_frame). With no time frame, the next 14 days plus matches for the rest of the year."""
    today = now.date()
    tf = time_frame_for(question, history, today)
    if tf:
        events = calendars.events_between(tf.start, tf.end)
        num, sources = _numbering(first_n)
        lines = [f"Time frame of the question: {tf.describe()}.", ""] + _agenda_lines(tf.start, tf.end, events, num, now)
        return "\n".join(lines), sources, tf
    near_end = today + timedelta(days=NEAR_DAYS - 1)
    near = calendars.events_between(today, near_end)
    found, year_end = scan(question, today)
    num, sources = _numbering(first_n)
    lines = ["Time frame of the question: no dates given, so this covers the next 14 days, then every matching "
             "entry for the rest of the school year.", ""] + _agenda_lines(today, near_end, near, num, now)
    lines.append(f"\nEntries matching the question from {day_label(today)} to the end of the school year "
                 f"({day_label(year_end)}), in date order:")
    if not found:
        lines.append("  (none)")
    for ev in found:
        line = f"  - {day_label(_day(ev['start']))}, {_span(ev)}: {ev['title']}"
        if ev["location"]:
            line += f" @ {ev['location']}"
        line += f" [{num[ev['calendar']]}]" + (" (already over)" if _ended(ev, now) else "")
        lines.append(line)
    return "\n".join(lines), sources, None


ANSWER_DATES = re.compile(r"ANSWER DATES:\s*(.*)", re.I)


def answer_dates(draft):
    m = ANSWER_DATES.search(draft)
    found = []
    for iso in re.findall(r"\d{4}-\d{2}-\d{2}", m.group(1) if m else ""):
        try:
            found.append(date.fromisoformat(iso))
        except ValueError:
            pass
    return list(dict.fromkeys(found))[:5]


def without_answer_dates(draft):
    return ANSWER_DATES.sub("", draft).strip()


def full_days(days, now, first_n=1):
    """Every entry on the given dates, with details: what the check step re-reads to verify the draft."""
    if not days:
        return ""
    events = [ev for d in days for ev in calendars.events_between(d, d)]
    num, _ = _numbering(first_n)
    out = ["Full calendar entries for the date(s) the draft is about (re-check the draft against these):"]
    for d in sorted(days):
        out += _render_day(d, events, num, now, detailed=True)[0]
    return "\n".join(out)
