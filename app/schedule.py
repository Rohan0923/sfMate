"""Scheduling questions ("is there school Monday?", "what's happening this week?", "when is junior retreat?").

They're answered from the school calendars only. The dates are worked out in code (app/dates.py), every calendar
entry in that time frame is handed over (not just the best few search hits), and the model double-checks the agenda
in a first pass before writing the answer in a second."""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import calendars, dates, prompts
from .calendars import _day, _clock, day_label

SCHEDULE_SIGNS = re.compile(
    r"\b(schedules?|bell|calendar|events?|happening|going on|no school|days? off|holidays?|breaks?|vacation|"
    r"late start|early (?:dismissal|release|out)|minimum day|half day|school (?:start|starts|end|ends|out|over|day)|"
    r"is there (?:school|class)|do we have (?:school|class)|have school|open house|rally|rallies|assembl(?:y|ies)|"
    r"mass|retreats?|concerts?|dances?|homecoming|prom|finals?|musical|tailgate|info(?:rmation)? night|"
    r"[bgl][12](?: day)?|what day is|upcoming|when(?:'s| is| are| does| do| will| was)|what time|what date|"
    r"how (?:long|many days) until)\b", re.I)
POLICY_SIGNS = re.compile(   # rules questions that happen to mention an event go to the handbook route instead
    r"\b(allowed|policy|policies|rules?|dress code|dress|wear|uniform|prerequisites?|credits?|graduat\w*|"
    r"requirements?|handbook|detention|discipline|consequences?|tardy|tardies|absen\w*|excused|"
    r"can i (?:bring|wear|use|leave|go|have)|am i able|is it ok)\b", re.I)
FOLLOW_UP_WORDS = 6
SEARCH_EXTRA = 8      # calendar search hits added when the question names no dates ("when is junior retreat?")
DETAIL_DAYS = 7       # for time frames up to a week, include event details (bell schedules, descriptions)
DEFAULT_DAYS = 14


@dataclass
class Plan:
    time_frame: dates.DateRange | None   # None: the question names an event, not dates
    agenda: str
    sources: list


def wants_schedule(text, today):
    return bool((dates.resolve(text, today) or SCHEDULE_SIGNS.search(text)) and not POLICY_SIGNS.search(text))


def plan(question, history, now):
    """A Plan if this is a scheduling question, else None (answer it from all sources, as usual)."""
    today = now.date()
    prev = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    follow_up = len(question.split()) <= FOLLOW_UP_WORDS and prev and wants_schedule(prev, today)
    if not (wants_schedule(question, today) or (follow_up and not POLICY_SIGNS.search(question))):
        return None
    time_frame = dates.resolve(question, today) or (dates.resolve(prev, today) if follow_up else None)
    agenda, sources = build_agenda(question, time_frame, now)
    return Plan(time_frame, agenda, sources)


# ---------- the agenda the model reads ----------
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


def build_agenda(question, time_frame, now):
    today = now.date()
    start, end = (time_frame.start, time_frame.end) if time_frame else (today, today + timedelta(days=DEFAULT_DAYS - 1))
    detailed = (end - start).days < DETAIL_DAYS
    in_window = calendars.events_between(start, end)
    shown = {id(ev) for ev in in_window}
    extra = [] if time_frame else [ev for ev in calendars.search(question, SEARCH_EXTRA) if id(ev) not in shown]

    labels = sorted({ev["calendar"] for ev in in_window + extra})
    num = {label: n for n, label in enumerate(labels, 1)}
    sources = [{"n": n, "title": label, "url": ""} for label, n in num.items()]

    out = [f"Calendar entries from {day_label(start)} through {day_label(end)}, day by day:"]
    d = start
    while d <= end:
        todays = [ev for ev in in_window if _day(ev["start"]) <= d <= _day(ev["end"] or ev["start"])]
        day_entries = [ev for ev in todays if calendars.is_day_schedule(ev)]
        others = [ev for ev in todays if not calendars.is_day_schedule(ev)]
        if day_entries or others or (end - start).days <= 31:   # long ranges: skip empty days
            out.append(f"\n{d:%A, %B} {d.day}, {d.year}{_day_tag(d, today)}")
            if any("no school" in ev["title"].lower() for ev in day_entries):
                status = "NO SCHOOL"
            elif day_entries:
                status = "school day: " + ", ".join(ev["title"] for ev in day_entries)
            elif d.weekday() >= 5:
                status = "weekend (no school day listed)"
            else:
                status = "no school-day entry on the calendar"
            sched_ref = " ".join(sorted({f"[{num[ev['calendar']]}]" for ev in day_entries}))
            out.append(f"  {status} {sched_ref}".rstrip())
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
        d += timedelta(days=1)

    if extra:
        out.append("\nOther calendar entries that match the question (outside those two weeks):")
        for ev in sorted(extra, key=lambda e: _day(e["start"])):
            past = " (already past)" if _ended(ev, now) else ""
            line = f"  - {day_label(_day(ev['start']))}, {_span(ev)}: {ev['title']}"
            if ev["location"]:
                line += f" @ {ev['location']}"
            out.append(f"{line} [{num[ev['calendar']]}]{past}{_details(ev)}")
    return "\n".join(out), sources


# ---------- the two passes ----------
CHECK_INSTRUCTIONS = (
    "You are double-checking the school calendar before a student gets an answer. You're given the current date "
    "and time, the time frame the question is about (already worked out, trust it), and the calendar agenda.\n"
    "1. Write the exact date(s) the question is about.\n"
    "2. Go through the agenda for those dates, day by day, and copy out EVERY entry that helps answer the question, "
    "with its weekday, date, time, and [source number] exactly as written. If the question is about whether there's "
    "school or what the schedule is, include each day's school-day status and hours.\n"
    "3. Note anything that has already ended or already happened compared to the current time.\n"
    "Use only the agenda; don't guess and don't answer the student yet. Short notes, no chit-chat. "
    "If nothing in the agenda is relevant to the question, reply with exactly: NONE"
)

ANSWER_INSTRUCTIONS = (
    "Answer the student's question about the school schedule using ONLY the checked notes and the calendar agenda "
    "below, which come from the official Saint Francis calendars. Trust the dates in them; don't do your own date "
    "math. Always give the actual weekday and date (like \"Monday, Oct 12\"), not just \"tomorrow\" or \"next week\". "
    "If something already happened or already ended today, say so. If the calendar doesn't show what they asked "
    "about, say it isn't on the school calendar and suggest checking sfhs.com or the Daily Announcements. "
    "Cite sources inline like [1]. Keep it short and easy to scan."
)


def _now_line(now):
    return f"Right now: {dates.long_day(now.date())}, {_clock(now)} (school time)."


def _frame_line(p):
    if p.time_frame:
        return f"Time frame of the question: {p.time_frame.describe()}."
    return "Time frame of the question: no dates given, so the agenda covers the next two weeks plus matching entries."


def check_messages(p, question, now):
    return [{"role": "system", "content": CHECK_INSTRUCTIONS},
            {"role": "user", "content": f"{_now_line(now)}\n{_frame_line(p)}\nQuestion: {question}\n\n{p.agenda}"}]


def found_nothing(notes):
    return notes.strip().strip(".*").upper().startswith("NONE")


def answer_messages(p, notes, question, history, profile, now):
    system = prompts.SFMATE_PERSONA + "\n\n" + ANSWER_INSTRUCTIONS + prompts.student_blurb(profile)
    context = (f"{_now_line(now)}\n{_frame_line(p)}\n\n{p.agenda}\n\n"
               f"Checked notes:\n{'Nothing on the calendar matches.' if found_nothing(notes) else notes.strip()}")
    return [{"role": "system", "content": system}, *history,
            {"role": "user", "content": f"{context}\n\nQuestion: {question}"}]
