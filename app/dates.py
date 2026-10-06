"""Turns the time frame in a question ("tomorrow", "next Friday", "this weekend", "Oct 12") into exact dates, in code,
so the model never has to do date arithmetic. Weeks run Monday-Sunday; "next <weekday>" means that day in next week."""
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta

WEEKDAYS = {"monday": 0, "tuesday": 1, "tues": 1, "wednesday": 2, "weds": 2, "thursday": 3, "thurs": 3,
            "friday": 4, "fri": 4, "saturday": 5, "sunday": 6}   # no "sat"/"sun"/"mon": they collide with SAT, etc.
MONTHS = {"january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
          "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
          "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12}
NUMBER_WORDS = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10,
                "fourteen": 14, "few": 3, "couple": 2, "couple of": 2}
MAX_DAYS = 62   # longest range we hand the model in one go

WD = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
MO = "|".join(sorted(MONTHS, key=len, reverse=True))
NUM = r"\d+|" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))


@dataclass
class DateRange:
    start: date
    end: date
    phrase: str   # what the student wrote, e.g. "next friday"

    def describe(self):
        if self.start == self.end:
            return f'"{self.phrase}" = {long_day(self.start)}'
        return f'"{self.phrase}" = {long_day(self.start)} through {long_day(self.end)}'


def long_day(d):
    return f"{d:%A, %B} {d.day}, {d.year}"


def _num(word):
    word = word.strip()
    return int(word) if word.isdigit() else NUMBER_WORDS[word]


def _week_start(d):
    return d - timedelta(days=d.weekday())


def _year_for(month, day, today):
    """A date without a year: this year, unless that's more than two months ago (then it means next year)."""
    try:
        d = date(today.year, month, day)
    except ValueError:
        return None
    if d < today - timedelta(days=62):
        d = date(today.year + 1, month, day) if not (month == 2 and day == 29) else d
    return d


def _weekday(which, name, today):
    target = WEEKDAYS[name]
    if which == "next":       # that weekday in next week (Mon-Sun)
        return _week_start(today) + timedelta(days=7 + target)
    if which == "last":       # the most recent one before today
        return today - timedelta(days=(today.weekday() - target - 1) % 7 + 1)
    if which == "this" and target < today.weekday():   # "this tuesday" on a Thursday: this week's, already past
        return _week_start(today) + timedelta(days=target)
    return today + timedelta(days=(target - today.weekday()) % 7)   # bare / "this" / "coming": today or the next one


def _whole_month(month, year):
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def _rules(today):
    """(pattern, function of the match -> (start, end)) in priority order: the first match wins."""
    tomorrow = today + timedelta(days=1)
    sat = today + timedelta(days=(5 - today.weekday()) % 7) if today.weekday() < 5 else _week_start(today) + timedelta(days=5)

    def explicit_date(m):
        month = MONTHS[m["mon"]]
        try:
            d = date(int(m["year"]), month, int(m["day"])) if m["year"] else _year_for(month, int(m["day"]), today)
        except ValueError:   # "feb 30"
            return None
        return (d, d) if d else None

    def numeric_date(m):
        month, day = int(m["m"]), int(m["d"])
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        try:
            d = date(int(m["y"]) + (2000 if len(m["y"]) == 2 else 0), month, day) if m["y"] else _year_for(month, day, today)
        except ValueError:
            return None
        return (d, d) if d else None

    def month_only(m):
        month = MONTHS[m["mon"]]
        year = today.year if month >= today.month or (today.month - month) <= 2 else today.year + 1
        start, end = _whole_month(month, year)
        return (max(start, today) if start <= today <= end else start), end

    return [
        (r"\bday after (?:tomorrow|tmrw|tmr)\b", lambda m: (today + timedelta(days=2),) * 2),
        (r"\b(?:tomorrow|tmrw|tmr|tmrrw)\b", lambda m: (tomorrow, tomorrow)),
        (r"\byesterday\b", lambda m: (today - timedelta(days=1),) * 2),
        (r"\b(?:today|tonight|this (?:morning|afternoon|evening)|right now|after school)\b", lambda m: (today, today)),
        (rf"\b(?:next|coming|upcoming|following)\s+(?P<n>{NUM})\s+(?:school\s+)?days\b",
         lambda m: (today, today + timedelta(days=min(_num(m["n"]), MAX_DAYS) - 1))),
        (rf"\bin\s+(?P<n>{NUM})\s+days\b", lambda m: (today + timedelta(days=_num(m["n"])),) * 2),
        (rf"\b(?:next|coming|upcoming)\s+(?P<n>{NUM})\s+weeks\b",
         lambda m: (today, today + timedelta(days=min(7 * _num(m["n"]), MAX_DAYS) - 1))),
        (r"\blast weekend\b", lambda m: (_week_start(today) - timedelta(days=2), _week_start(today) - timedelta(days=1))),
        (r"\bnext weekend\b", lambda m: (sat + timedelta(days=7), sat + timedelta(days=8))),
        (r"\b(?:this )?weekend\b", lambda m: (max(sat, today) if today.weekday() >= 5 else sat, sat + timedelta(days=1))),
        (r"\bnext week\b", lambda m: (_week_start(today) + timedelta(days=7), _week_start(today) + timedelta(days=13))),
        (r"\blast week\b", lambda m: (_week_start(today) - timedelta(days=7), _week_start(today) - timedelta(days=1))),
        (r"\b(?:this|the rest of the) week\b", lambda m: (today, _week_start(today) + timedelta(days=6))),
        (rf"\b(?:(?P<which>next|last|this|coming)\s+)?(?P<wd>{WD})\b",
         lambda m: (_weekday(m["which"], m["wd"], today),) * 2),
        (rf"\b(?P<mon>{MO})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(?P<year>20\d\d))?", explicit_date),
        (rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<mon>{MO})\b(?:,?\s+(?P<year>20\d\d))?", explicit_date),
        (r"\b(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2}|\d{4}))?\b", numeric_date),
        (r"\bthis month\b", lambda m: (today, _whole_month(today.month, today.year)[1])),
        (r"\bnext month\b", lambda m: _whole_month(today.month % 12 + 1, today.year + (today.month == 12))),
        (rf"\b(?:in|during|of|for|throughout)\s+(?P<mon>{MO})\b", month_only),
    ]


def resolve(text, today):
    """The time frame a question asks about, or None if it doesn't name one."""
    t = " ".join(text.lower().replace("’", "'").split())
    for pattern, to_range in _rules(today):
        m = re.search(pattern, t)
        if not m:
            continue
        if "mon" in m.groupdict() and m["mon"] == "may" and not re.search(r"\bmay\s+\d|\d(?:st|nd|rd|th)?\s+(?:of\s+)?may\b|\b(?:in|during|of|for|throughout)\s+may\b", t):
            continue   # "may I ..." is not the month
        found = to_range(m)
        if not found:
            continue
        start, end = found
        return DateRange(start, min(end, start + timedelta(days=MAX_DAYS - 1)), m.group(0))
    return None
