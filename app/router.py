"""Every chat message goes through here:

1. classify it into one category (or two): announcements, calendar, courses, rules, menu, clubs, misc
2. read ONLY that category's source (isolated, so two places can't contradict each other)
3. draft an answer; if the source doesn't have it, fall back to the Misc path (the best sfhs.com pages, read whole)
4. check the draft against the sources (for calendar answers, against the full entries of the dates it names)
5. stream the checked answer
"""
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from . import announcements, dates, llm, menu, prompts, rag, schedule, site
from .calendars import _clock

log = logging.getLogger("sfmate.router")

CATEGORIES = {
    "announcements": "the Daily Announcements emails: today's or recent announcements, birthdays, what was announced, "
                     "summaries of the announcements",
    "calendar": "dates and the school schedule: whether there's school, day types (G1, B1, G2, B2, L1, L2, A1-A4), "
                "breaks, finals, liturgies, when school events happen",
    "courses": "classes and course planning: course descriptions, prerequisites, honors and AP, graduation "
               "requirements, choosing or scheduling classes",
    "rules": "school rules and policies (the Student-Parent Handbook): dress code, phones, attendance, tardies, "
             "discipline, academic honesty, rules for dances and events",
    "menu": "food: lunch, breakfast, the Lancer Cafe, snack bar, concessions, prices",
    "clubs": "clubs, activities, affinity groups, intramurals, how to join or start a club",
    "misc": "anything else about Saint Francis: people and offices, admissions, tuition, campus, history, contacts",
}
CLASSIFY_INSTRUCTIONS = (
    "Sort a Saint Francis High School student's question into the category whose source can answer it. "
    "Categories:\n" + "\n".join(f"{name}: {what}" for name, what in CATEGORIES.items()) +
    "\nReply with only the category name, or two names separated by a comma if the question truly needs both. "
    "No other words."
)
ANNOUNCEMENT_WORDS = re.compile(r"\bannouncements?\b", re.I)
MEAL_WORDS = re.compile(r"\b(lunch|lunches|breakfast|menu)\b", re.I)
COURSE_SIGNS = re.compile(r"\b(courses?|class(es)?|prereq\w*|honors|ap|electives?|credits?|graduat\w*)\b", re.I)
RULE_SIGNS = re.compile(r"\b(rules?|polic(y|ies)|dress code|uniform|phones?|allowed|attendance|tard(y|ies)|"
                        r"detention|discipline|absen\w*|handbook)\b", re.I)
CLUB_SIGNS = re.compile(r"\b(clubs?|affinity|intramurals?|activities)\b", re.I)
FALLS_BACK = {"courses", "rules", "clubs", "calendar"}   # if their source doesn't have it, try sfhs.com (Misc)
CONTEXT_CHARS = 14000   # what fits in the model's ~8k-token window with the instructions, draft, and answer
SUMMARY_CHARS = 11000   # an announcement email (or the chosen sections of one)
SECTION_CHARS = 3000    # one section can't crowd out the others
MIN_HEADINGS = 3        # fewer recognized section headings than this: the format changed, send the email whole
HISTORY = 4             # earlier messages kept (for follow-ups), each trimmed
HISTORY_CHARS = 1200


def _heuristic(question, today):
    if ANNOUNCEMENT_WORDS.search(question):
        return ["announcements"]
    if menu.wants_menu(question):
        return ["menu"]
    if schedule.wants_schedule(question, today):
        return ["calendar"]
    for name, signs in (("rules", RULE_SIGNS), ("courses", COURSE_SIGNS), ("clubs", CLUB_SIGNS)):
        if signs.search(question):
            return [name]
    return ["misc"]


def classify(question, history, today):
    prev = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    asked = (f"Previous question (for context): {prev[:300]}\n" if prev else "") + f"Question: {question}"
    reply = llm.complete([{"role": "system", "content": CLASSIFY_INSTRUCTIONS},
                          {"role": "user", "content": asked}], max_tokens=12, temperature=0)
    found = list(dict.fromkeys(w for w in re.findall(r"[a-z]+", reply.lower()) if w in CATEGORIES))
    if not found:
        log.warning("classifier said %r; using keyword rules", reply[:60])
        found = _heuristic(question, today)
    # knee-jerk rules: these words always mean these sources
    if ANNOUNCEMENT_WORDS.search(question):
        found = ["announcements"] + [c for c in found if c != "announcements"]
    if MEAL_WORDS.search(question) and "menu" not in found:
        found = (["announcements", "menu"] if found[:1] == ["announcements"] else ["menu"]) + [c for c in found if c not in ("announcements", "menu")]
    if len(found) > 1:
        found = [c for c in found if c != "misc"] or ["misc"]
    return found[:2]


# ---------- what each category reads ----------
@dataclass
class Context:
    category: str
    text: str
    sources: list
    falls_back: bool = False
    check_dates: bool = False   # calendar: re-check the draft against the full entries of the dates it names
    note: str = ""              # category-specific instructions


def _now_line(now):
    return f"Right now: {dates.long_day(now.date())}, {_clock(now)} (school time)."


def _rag_context(category, question, n, title_prefix, label, note, k=6):
    hits = rag.retrieve_where(question, k, lambda c: c["title"].startswith(title_prefix))
    sources, parts = [], []
    for i, (c, _) in enumerate(hits, n):
        sources.append({"n": i, "title": c["title"], "url": c["url"]})
        parts.append(f"[{i}] {c['title']}\n{c['text']}")
    text = f"{label}, the closest sections:\n\n" + ("\n\n".join(parts) or "(nothing found)")
    return Context(category, text, sources, falls_back=True, note=note)


def _site_context(category, question, n, only=None):
    pages = site.read_pages(question, only)
    sources = [{"n": i, "title": title, "url": url} for i, (url, title, _) in enumerate(pages, n)]
    text = "Full text of the sfhs.com pages most related to the question:\n\n" + (
        "\n\n".join(f"[{i}] {title} ({url})\n{body}" for i, (url, title, body) in enumerate(pages, n)) or "(nothing found)")
    return Context(category, text, sources, falls_back=category != "misc", note=SITE_NOTE)


def _announcement_context(question, history, now, prefs, n, ann=None, summary=None):
    today = now.date()
    note_text = ""
    if ann is None:
        tf = schedule.time_frame_for(question, history, today)
        day = tf.start if tf else today
        if day > today and re.fullmatch(rf"(this |coming )?({dates.WD})", tf.phrase):
            day -= timedelta(days=7)   # announcements are never in the future: "wednesday's" means the last one
        # "today's announcements" before today's email arrives: give the latest one (with a note). Another day: exact.
        ann, note_text = announcements.for_date(day, exact=bool(tf) and day != today)
        if not ann:
            return [Context("announcements", f"There's no Daily Announcements email for {dates.long_day(day)} "
                                             f"in sfMate's inbox.", [], note=ANNOUNCE_DETAIL_NOTE)]
    summary = announcements.wants_summary(question) if summary is None else summary
    title = f"Daily Announcements · {ann['label']}"
    head = f"[{n}] Daily Announcements email, sent {ann['label']}." + (f" ({note_text})" if note_text else "")
    if not summary:
        log.info("announcement question on %s (%d characters)", ann["label"], len(ann["text"]))
        return [Context("announcements", f"{head}\n\n{ann['text'][:SUMMARY_CHARS]}", [{"n": n, "title": title, "url": ""}],
                        note=ANNOUNCE_DETAIL_NOTE)]
    chosen = [k for k in announcements.TOPIC_KEYS if k in prefs]
    sections = announcements.split_sections(ann["text"])
    if sum(1 for k, _, _ in sections if k and k != "daytype_birthdays") < MIN_HEADINGS:
        # the email's headings weren't recognized: never drop content, send it whole and let the model pick topics
        topics = ", ".join(announcements.TOPIC_LABELS[k] for k in chosen)
        body = (f"The student only wants these topics, if the email has them: {topics}. Leave out anything else, "
                f"and never add a topic the email doesn't cover.\n\nThe whole email:\n\n{ann['text'][:SUMMARY_CHARS]}")
        log.warning("announcement email %s: section headings not recognized, sending it whole", ann["label"])
    else:   # split in code: the model sees only the student's sections, under the email's own headings
        kept = [(h, b) for k, h, b in sections if k in chosen]
        body = ("The parts of the email for this student's topics, under the email's own headings:\n\n"
                + "\n\n".join(f"## {h}\n{b[:SECTION_CHARS]}" for h, b in kept)[:SUMMARY_CHARS]) if kept \
            else "None of this student's chosen topics are in this email."
    if len(ann["text"]) < announcements.SHORT_EMAIL:
        body = (f"WARNING: the email sfMate received is only {len(ann['text'])} characters, so the announcements "
                f"probably didn't come through fully. Summarize only what's below and say the full announcements "
                f"may be missing (check your school email). Do not fill in anything else.\n\n{body}")
        log.warning("announcement summary from %s: the email is only %d characters", ann["label"], len(ann["text"]))
    log.info("announcement summary from %s (%d characters): %d of %d sections, %d characters sent",
             ann["label"], len(ann["text"]), sum(1 for k, _, _ in sections if k in chosen), len(sections), len(body))
    contexts = [Context("announcements", f"{head}\n\n{body}", [{"n": n, "title": title, "url": ""}],
                        note=ANNOUNCE_SUMMARY_NOTE)]
    lunch_text, lunch_sources = menu.day_block(ann["sent"].date(), today, n + 1)
    contexts.append(Context("menu", lunch_text, lunch_sources, note="Include that day's lunch from the cafe menu."))
    return contexts


def build(categories, question, history, now, prefs):
    contexts = []
    for cat in categories:
        n = 1 + sum(len(c.sources) for c in contexts)
        if cat == "announcements":
            contexts += _announcement_context(question, history, now, prefs, n)
        elif cat == "menu":
            if any(c.category == "menu" for c in contexts):
                continue   # the announcement summary already has that day's lunch
            text, sources = menu.context(question, history, now, n)
            contexts.append(Context("menu", text, sources, note=menu.NOTES))
        elif cat == "calendar":
            text, sources, tf = schedule.context(question, history, now, n)
            contexts.append(Context("calendar", text, sources, falls_back=tf is None, check_dates=True, note=CALENDAR_NOTE))
        elif cat == "courses":
            contexts.append(_rag_context("courses", question, n, "Course Scheduling Guide",
                                         "The 2026-27 Course Scheduling Guide", COURSES_NOTE))
        elif cat == "rules":
            contexts.append(_rag_context("rules", question, n, "Student-Parent Handbook",
                                         "The 2026-27 Student-Parent Handbook", RULES_NOTE))
        elif cat == "clubs":
            contexts.append(_site_context("clubs", question, n, site.CLUB_PAGES))
        else:
            contexts.append(_site_context("misc", question, n))
    return contexts


# ---------- the draft and the check ----------
DRAFT_INSTRUCTIONS = (
    "DRAFT STEP. You are drafting an answer for sfMate, the Saint Francis High School advisor chatbot; it will be "
    "checked before the student sees it.\n"
    "- Use ONLY the sources below. No outside knowledge, no guessing.\n"
    "- Read the sources carefully and be specific: weekdays and dates, times, places, names, course codes, prices.\n"
    "- Cite sources inline like [1].\n"
    "- If the sources don't contain the answer, reply with exactly: NOT FOUND"
)
CHECK_INSTRUCTIONS = (
    "Below are the sources, the student's question, and a draft answer. First check the draft against the sources: "
    "every date, weekday, time, name, number, and price must match the sources exactly. Fix anything wrong, remove "
    "anything the sources don't support, and add anything important the draft missed. Then write the final answer "
    "for the student: well structured and easy to scan (a short opening line, then bullets or short paragraphs), "
    "citing sources inline like [1]. Output only the final answer; never mention the draft or the checking. If the "
    "draft says NOT FOUND and the sources really don't answer the question, say you couldn't find that in sfMate's "
    "sources and suggest checking sfhs.com or asking the right office."
)
CALENDAR_NOTE = (
    schedule.DAY_TYPES + " The dates were worked out in code: trust them and don't do your own date math. Always give "
    "the weekday and date. For 'next' or 'when is' questions, go through the entries in date order and use the first "
    "one on or after today. If something already ended today, say so."
)
CALENDAR_DRAFT_NOTE = ("End the draft with a line 'ANSWER DATES: YYYY-MM-DD, ...' listing the date(s) your answer is "
                       "about (or 'ANSWER DATES: none').")
ANNOUNCE_SUMMARY_NOTE = (
    "This is this student's own summary of a Daily Announcements email, whose text is in the sources: read all of it. "
    "Start with the day type if the email says it (like 'Today is a B2 Schedule') and that day's lunch. Then, for each "
    "section of the email that's included, a short bold heading with bullets: every event, deadline, sign-up, and "
    "meeting, with its date, time, and place. Use the email's own section headings, and write one ONLY for sections "
    "that are in the email text; never write that a topic had nothing. Every name, event, room, and time must come "
    "from the email word for word: if it isn't in the email, it doesn't go in the summary. Skip filler like 'Check "
    "out the details here!'. 'Today' and 'tomorrow' inside the email mean the email's date."
)
ANNOUNCE_DETAIL_NOTE = (
    "The source is a Daily Announcements email. Answer the specific question from it, quoting details exactly, and "
    "say which day's email it's from. 'Today' and 'tomorrow' inside the email mean the email's date."
)
COURSES_NOTE = ("The only source is the 2026-27 Course Scheduling Guide. Include course names and codes, grade "
                "levels, prerequisites, and credits when they matter.")
RULES_NOTE = "The only source is the 2026-27 Student-Parent Handbook. Say which section a rule is from (like §4.4)."
SITE_NOTE = "The sources are full sfhs.com pages. Read them carefully; if the answer is a link, form, or contact, give it."


def _history(history):
    return [{"role": m["role"], "content": m["content"][:HISTORY_CHARS]} for m in history[-HISTORY:]]


def _sources_text(contexts, extra=""):
    text = "\n\n----------\n\n".join(c.text for c in contexts)[:CONTEXT_CHARS]
    return text + (f"\n\n----------\n\n{extra}" if extra else "")


def _notes(contexts, drafting):
    notes = list(dict.fromkeys(c.note for c in contexts if c.note))
    if drafting and any(c.check_dates for c in contexts):
        notes.append(CALENDAR_DRAFT_NOTE)
    return "\n".join(f"- {n}" for n in notes)


def draft_messages(contexts, question, history, now):
    system = DRAFT_INSTRUCTIONS + "\n" + _notes(contexts, drafting=True)
    return [{"role": "system", "content": system}, *_history(history),
            {"role": "user", "content": f"{_now_line(now)}\n\nSources:\n\n{_sources_text(contexts)}\n\nQuestion: {question}"}]


def check_messages(contexts, extra, draft, question, history, profile, now):
    system = (prompts.SFMATE_PERSONA + "\n\n" + CHECK_INSTRUCTIONS + "\n" + _notes(contexts, drafting=False)
              + prompts.student_blurb(profile))
    user = (f"{_now_line(now)}\n\nSources:\n\n{_sources_text(contexts, extra)}\n\nQuestion: {question}\n\n"
            f"Draft answer:\n{draft}")
    return [{"role": "system", "content": system}, *_history(history), {"role": "user", "content": user}]


def not_found(draft):
    return draft.strip().strip("*.").upper().startswith("NOT FOUND")


def _all_sources(contexts):
    return [s for c in contexts for s in c.sources]


def run(contexts, question, history, profile, now):
    """Draft, fall back to Misc if needed, check, stream. Yields {"sources": ...} then the answer's text."""
    if any(c.note == ANNOUNCE_SUMMARY_NOTE for c in contexts):
        history = []   # a summary stands alone: an earlier answer in the chat shouldn't steer it
    yield {"sources": _all_sources(contexts)}
    draft = llm.complete(draft_messages(contexts, question, history, now), max_tokens=900, temperature=0.2)
    if not_found(draft) and contexts and all(c.falls_back for c in contexts):
        log.info("%s didn't have it; trying sfhs.com for %r", "+".join(c.category for c in contexts), question[:80])
        contexts = [_site_context("misc", question, 1)]
        yield {"sources": _all_sources(contexts)}
        draft = llm.complete(draft_messages(contexts, question, history, now), max_tokens=900, temperature=0.2)
    extra = ""
    calendar = next((c for c in contexts if c.check_dates), None)
    if calendar and calendar.sources:
        extra = schedule.full_days(schedule.answer_dates(draft), now, calendar.sources[0]["n"])
    draft = schedule.without_answer_dates(draft)
    yield from llm.stream_chat(check_messages(contexts, extra, draft, question, history, profile, now),
                               max_tokens=1000, temperature=0.3)


def answer(question, history, profile, now=None):
    now = now or prompts.school_now()
    categories = classify(question, history, now.date())
    log.info("question %r -> %s", question[:80], "+".join(categories))
    yield from run(build(categories, question, history, now, prompts.announcement_prefs(profile)),
                   question, history, profile, now)


def announcement_summary(ann, profile, now=None):
    """The "Want me to summarize?" button: this student's summary of one email, plus that day's lunch."""
    now = now or prompts.school_now()
    question = f"Summarize the Daily Announcements from {ann['label']}."
    contexts = _announcement_context(question, [], now, prompts.announcement_prefs(profile), 1, ann=ann, summary=True)
    yield from run(contexts, question, [], profile, now)


def summary_cache_key(ann_id, profile):
    prefs = ",".join(prompts.announcement_prefs(profile))
    return f"{ann_id}#v2#{hashlib.sha1(prefs.encode()).hexdigest()[:12]}"   # v2: summaries saved before the fix are ignored
