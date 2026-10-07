"""The Lancer Cafe menu, read live from sfhs.com/lancer-cafe (refreshed every couple of hours), split by day.

Food questions ("what's for lunch today?", "is there pizza Friday?") skip search: the day is worked out in code
(app/dates.py) and the model gets exactly that day's menu, plus the everyday items, hours, and payment info."""
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from . import config, dates, prompts
from .announcements import clean_text, html_to_text

log = logging.getLogger("sfmate.menu")

SCHOOL_TZ = ZoneInfo(config.SCHOOL_TIMEZONE)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; sfMate/1.0)"}
DAY_LINE = re.compile(r"^(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s*\(?\s*(\d{1,2})\s*/\s*(\d{1,2})(?:\s*/\s*\d{2,4})?\s*\)?\s*$", re.I)
HEADER_LINE = re.compile(r"\b(items|features|options|specials|menu)\b|:$", re.I)
INFO_LINE = re.compile(r"^(hours|location|payment)\b", re.I)
MAX_DAYS = 14


@dataclass
class Section:
    name: str                                     # "Breakfast", "Lunch", "After-School Snack Bar", ...
    info: list = field(default_factory=list)      # "Hours — 7:15am-9:00am", "Location — ...", "Payment — ..."
    everyday: list = field(default_factory=list)  # [(heading, [items])]: "Daily Breakfast Items", "Recurring Menu Items"
    days: dict = field(default_factory=dict)      # date -> (heading, [items]): "Chef’s Table Features", or "" for lunch


sections = []        # the last good parse, in page order
fetched_at = None    # when it was read (school time)
_stop = threading.Event()


# ---------- reading the page ----------
def _tidy_item(text):
    text = re.sub(r"\s*-{2,}\s*(?=\$)", " — ", text)   # "Pizza--$5.50" -> "Pizza — $5.50"
    return " ".join(text.split()).rstrip(" -–—")


def _is_header(line):
    return "$" not in line and len(line) <= 45 and bool(HEADER_LINE.search(line))


def parse_section(name, lines, today):
    sec = Section(name)
    current_day, heading = None, ""
    for raw in lines:
        line = raw.lstrip("•·*- ").strip()
        if not line:
            continue
        m = DAY_LINE.match(line)
        if m:
            current_day = dates.year_for(int(m[1]), int(m[2]), today)
            if current_day:
                sec.days.setdefault(current_day, (heading, []))
            continue
        if INFO_LINE.match(line) and not sec.days and not sec.everyday:
            sec.info.append(_tidy_item(line))
        elif _is_header(line):
            heading, current_day = line.rstrip(":"), None
            sec.everyday.append((heading, []))
        elif current_day:
            sec.days[current_day][1].append(_tidy_item(line))
        elif sec.everyday:
            sec.everyday[-1][1].append(_tidy_item(line))
        elif raw.lstrip().startswith("•"):   # a bare list straight after the intro (snack bar, concessions)
            sec.everyday.append(("", [_tidy_item(line)]))
    sec.everyday = [(h, items) for h, items in sec.everyday if items]   # a heading followed by dated days, not items
    return sec


def parse_page(html, today):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for th in soup.find_all("th"):
        row = th.find_parent("tr")
        nxt = row.find_next_sibling("tr") if row else None
        td = nxt.find("td") if nxt else None
        if not td:
            continue
        for lst in td.find_all(["ul", "ol"]):   # "Tues (10/6)<ul>…" : the date gets its own line either way
            lst.insert_before("\n")
        lines = clean_text(html_to_text(str(td))).split("\n")
        out.append(parse_section(" ".join(th.get_text(" ").split()), lines, today))
    return out


def refresh():
    global sections, fetched_at
    now = datetime.now(SCHOOL_TZ)
    r = requests.get(config.MENU_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    parsed = parse_page(r.text, now.date())
    days = sum(len(s.days) for s in parsed)
    if not days:   # the page changed shape: keep what we had rather than forget the menu
        raise ValueError("found no dated menu days on the page")
    sections, fetched_at = parsed, now
    log.info("menu: %d dated days across %s", days, ", ".join(s.name for s in parsed if s.days))


def _refresher(stop):
    while True:
        try:
            refresh()
        except Exception as e:
            log.error("menu refresh failed (keeping the last copy): %s: %s", type(e).__name__, e)
        if stop.wait(config.MENU_REFRESH_MINUTES * 60):
            return


def start():
    if not config.MENU_URL:
        log.warning("the Lancer Cafe menu is off: set MENU_URL")
        return
    _stop.clear()
    threading.Thread(target=_refresher, args=(_stop,), daemon=True, name="menu").start()


def stop():
    _stop.set()


# ---------- answering ----------
FOOD_SIGNS = re.compile(
    r"\b(lunch|lunches|breakfast|menu|food|eat|eating|cafe|café|cafeteria|dining|chef'?s table|pizza|entr[eé]es?|"
    r"vegetarian|vegan|wraps?|burritos?|tacos?|snack bar|snacks?|smoothies?|acai|açaí|fries|meals?|hungry|"
    r"serving|concessions?|milkshakes?|quesadillas?|salads?|sandwich(?:es)?)\b", re.I)
NOT_MENU = re.compile(r"\b(nuts?|allerg\w*|gluten|dairy[- ]free|myschoolbucks|account|refund|balance|deposit)\b", re.I)


def wants_menu(question):
    return bool(FOOD_SIGNS.search(question)) and not NOT_MENU.search(question)


@dataclass
class Plan:
    time_frame: dates.DateRange | None
    agenda: str
    sources: list
    looked_at: str   # for "How I checked this"


def _menu_days():
    return sorted({d for s in sections for d in s.days})


def plan(question, history, now):
    """A Plan if this is a menu question and we have a menu, else None."""
    prev = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    follow_up = len(question.split()) <= 6 and wants_menu(prev) and not NOT_MENU.search(question)
    if not sections or not (wants_menu(question) or follow_up):
        return None
    today = now.date()
    time_frame = dates.resolve(question, today) or (dates.resolve(prev, today) if follow_up else None)
    if time_frame:
        days = [time_frame.start + timedelta(days=i)
                for i in range(min((time_frame.end - time_frame.start).days + 1, MAX_DAYS))]
    else:   # no day named: today, plus the next day with a menu (for "what's for lunch" asked after school)
        days = [today] + [d for d in _menu_days() if d > today][:1]
    posted = {d for s in sections for d in s.days}
    found = ", ".join(f"{d:%a %b} {d.day}" for d in days if d in posted) or "none of those days"
    missing = [f"{d:%a %b} {d.day}" for d in days if d not in posted]
    looked_at = (f"Read the Lancer Cafe menu (page checked {fetched_at:%a %b} {fetched_at.day}, {fetched_at:%I:%M %p}). "
                 .replace(" 0", " ") + f"Menu posted for: {found}" + (f". Not posted: {', '.join(missing)}" if missing else ""))
    return Plan(time_frame, build_agenda(days, today),
                [{"n": 1, "title": "Lancer Cafe menu", "url": config.MENU_URL}], looked_at)


def _tag(d, today):
    return {today: " (TODAY)", today + timedelta(days=1): " (TOMORROW)"}.get(d, " (already past)" if d < today else "")


def build_agenda(days, today):
    out = [f"Lancer Cafe menu [1] (from the cafe's page, checked {fetched_at:%a %b} {fetched_at.day} at "
           f"{fetched_at:%I:%M %p}".replace(" 0", " ") + "):" if fetched_at else "Lancer Cafe menu [1]:"]
    for d in days:
        out.append(f"\n{d:%A, %B} {d.day}, {d.year}{_tag(d, today)}")
        found = False
        for s in sections:
            if d in s.days:
                heading, items = s.days[d]
                if items:
                    found = True
                    out.append(f"  {s.name}" + (f" ({heading})" if heading else "") + ":")
                    out.extend(f"    - {item}" for item in items)
        if not found:
            out.append("  no menu posted for this day" + (" (weekend)" if d.weekday() >= 5 else ""))
    out.append("\nEvery day (not tied to a date):")
    for s in sections:
        out.append(f"  {s.name}: " + "; ".join(s.info) if s.info else f"  {s.name}:")
        for heading, items in s.everyday:
            out.append(f"    {heading or 'Items'}: " + "; ".join(items))
    return "\n".join(out)


INSTRUCTIONS = (
    "Answer the student's question about food at Saint Francis using ONLY the Lancer Cafe menu below, from the "
    "cafe's page on sfhs.com. The dates are already worked out; trust them and don't do your own date math. Always "
    "give the weekday and date (like \"Wednesday, Oct 7\"). Quote dishes and prices as they're written, and keep it "
    "easy to scan (a short list is great). If a day says no menu posted, say the cafe hasn't posted a menu for it "
    "(it may not be a school day) and point them to sfhs.com/lancer-cafe. Don't invent ingredients or allergy info; "
    "for allergies, mention all food is made without nuts and to email lancercafe@sfhs.com. Cite the menu as [1]."
)


def messages(p, question, history, profile, now):
    system = prompts.SFMATE_PERSONA + "\n\n" + INSTRUCTIONS + prompts.student_blurb(profile)
    frame = (f"Time frame of the question: {p.time_frame.describe()}." if p.time_frame
             else "No day named: the menu below covers today and the next day with a menu posted.")
    context = f"Right now: {dates.long_day(now.date())}, {now:%I:%M %p}".replace(" 0", " ") + f" (school time).\n{frame}\n\n{p.agenda}"
    return [{"role": "system", "content": system}, *history,
            {"role": "user", "content": f"{context}\n\nQuestion: {question}"}]
