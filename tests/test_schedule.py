from datetime import date, datetime

import pytest

from app import calendars, prompts, router, schedule
from tests.test_app import sse_events
from tests.test_calendars import EVENTS, NOW, SCHEDULE, TZ, feeds, ics   # noqa: F401  (feeds is a fixture)

LATER = ics(   # the rest of the school year on the schedule calendar
    "SFHS School Schedule",
    "DTSTART;VALUE=DATE:20261030\r\nDTEND;VALUE=DATE:20261031\r\nSUMMARY:A1 - Interfaith Prayer Service",
    "DTSTART:20261104T160500Z\r\nDTEND:20261104T223000Z\r\nSUMMARY:L1 - Mass of All Saints\r\n"   # PST by November
    "DESCRIPTION:<b>L1 Day Schedule</b><br>8:05 - 2:30 School Day",
    "DTSTART;VALUE=DATE:20261125\r\nDTEND;VALUE=DATE:20261128\r\nSUMMARY:Thanksgiving Break - NO SCHOOL",
    "DTSTART;VALUE=DATE:20261216\r\nDTEND;VALUE=DATE:20261217\r\nSUMMARY:Final Exams",
    "DTSTART;VALUE=DATE:20270604\r\nDTEND;VALUE=DATE:20270605\r\nSUMMARY:G2",
)


@pytest.fixture
def cal(feeds):   # noqa: F811
    feeds["https://cal/later.ics"] = LATER
    from app import config
    config.CALENDAR_ICS_URLS.append("https://cal/later.ics")
    calendars.refresh()


# sources are numbered by calendar name, the same in every step: Activities & Events = 1, School Schedule = 2

def test_a_single_day(cal):
    text, sources, tf = schedule.context("is there school next monday?", [], NOW)
    assert tf.start == date(2026, 10, 12)
    assert sources == [{"n": 1, "title": "SFHS School Activites & Events", "url": ""},
                       {"n": 2, "title": "SFHS School Schedule", "url": ""}]
    assert text.splitlines() == [
        'Time frame of the question: "next monday" = Monday, October 12, 2026.', "",
        "Calendar entries from Mon Oct 12, 2026 through Mon Oct 12, 2026, day by day:", "",
        "Monday, October 12, 2026", "  NO SCHOOL [2]"]


def test_today_has_hours_and_bell_schedule(cal):
    text, _, _ = schedule.context("what's the schedule today", [], NOW)
    lines = text.splitlines()
    i = lines.index("Monday, October 5, 2026 (TODAY)")
    assert lines[i + 1:i + 6] == ["  school day: B2 [2]", "    hours: 8:05 AM – 2:30 PM", "    B2 Day Schedule",
                                  "    7:45 - 8:00 Collaboration", "    8:05 - 2:30 School Day"]


def test_a_week_lists_every_day(cal):
    text, _, _ = schedule.context("what's happening this week", [], NOW)
    for day in ("Monday, October 5, 2026 (TODAY)", "Tuesday, October 6, 2026 (TOMORROW)", "Sunday, October 11, 2026"):
        assert day in text
    assert text.count("Junior Retreat #1") == 2   # Wed + Thu
    assert "  - 7:00 PM – 9:00 PM: Fall Band & Choir Concert @ Graham Theater, Mountain View [1]" in text
    assert "Saturday, October 10, 2026\n  weekend (no school day listed)" in text


def test_events_already_over_today_are_marked(cal):
    late = datetime(2026, 10, 8, 21, 30, tzinfo=TZ)
    text, _, _ = schedule.context("what's on tonight", [], late)
    assert "Fall Band & Choir Concert @ Graham Theater, Mountain View [1] (already over)" in text


def test_next_liturgy_scans_the_rest_of_the_year(cal):
    text, _, tf = schedule.context("when is the next liturgy?", [], NOW)
    assert tf is None
    assert "Calendar entries from Mon Oct 5, 2026 through Sun Oct 18, 2026, day by day:" in text   # the next 14 days
    matches = text.split("Entries matching the question from Mon Oct 5, 2026 to the end of the school year (Fri Jun 4, 2027)")[1]
    assert "  - Fri Oct 30, 2026, all day: A1 - Interfaith Prayer Service [2]" in matches   # a prayer service
    assert "  - Wed Nov 4, 2026, 8:05 AM – 2:30 PM: L1 - Mass of All Saints [2]" in matches
    assert "Robotics" not in matches and "Final Exams" not in matches


def test_parent_masses_are_not_student_liturgies(cal, monkeypatch):
    parent_mass = dict(next(ev for ev in calendars.events if "Concert" in ev["title"]), title="BRIC Parent Mass")
    monkeypatch.setattr(calendars, "events", calendars.events + [parent_mass])
    matches = schedule.context("when is the next liturgy?", [], NOW)[0].split("to the end of the school year")[1]
    assert "BRIC Parent Mass" not in matches and "L1 - Mass of All Saints" in matches


def test_specific_words_get_first_claim_on_the_slots(cal, monkeypatch):
    monkeypatch.setattr(schedule, "MAX_MATCHES", 1)
    matches = schedule.context("when is thanksgiving break", [], NOW)[0].split("to the end of the school year")[1]
    assert "Thanksgiving Break - NO SCHOOL" in matches and "Mon Oct 12" not in matches   # not the earlier day off


@pytest.mark.parametrize("question,expected,absent", [
    ("when's the next A1 day", "A1 - Interfaith Prayer Service", "L1 - Mass"),
    ("when is the next day off", "Thanksgiving Break - NO SCHOOL", "L1 - Mass"),
    ("when are finals", "Final Exams", "Thanksgiving"),
    ("when is robotics practice", "Tue Oct 27, 2026, 3:30 PM – 5:00 PM: Robotics practice", "Final Exams"),
])
def test_long_term_matches(cal, question, expected, absent):
    matches = schedule.context(question, [], NOW)[0].split("to the end of the school year")[1]
    assert expected in matches and absent not in matches


def test_follow_ups_keep_the_time_frame(cal):
    history = [{"role": "user", "content": "is there school next monday?"}, {"role": "assistant", "content": "Nope [2]"}]
    assert schedule.context("and the band concert?", history, NOW)[2].start == date(2026, 10, 12)
    assert schedule.context("what about friday?", history, NOW)[2].start == date(2026, 10, 9)


def test_answer_dates_and_full_days(cal):
    draft = "The next liturgy is Wednesday, Nov 4 [2].\nANSWER DATES: 2026-11-04, 2026-13-01"
    assert schedule.answer_dates(draft) == [date(2026, 11, 4)]
    assert schedule.without_answer_dates(draft) == "The next liturgy is Wednesday, Nov 4 [2]."
    full = schedule.full_days([date(2026, 11, 4)], NOW, 1)
    assert full.splitlines() == [
        "Full calendar entries for the date(s) the draft is about (re-check the draft against these):", "",
        "Wednesday, November 4, 2026", "  school day: L1 - Mass of All Saints [2]",
        "    hours: 8:05 AM – 2:30 PM", "    L1 Day Schedule", "    8:05 - 2:30 School Day"]
    assert schedule.full_days([], NOW) == ""


@pytest.fixture
def at_now(monkeypatch):
    monkeypatch.setattr(prompts, "school_now", lambda: NOW)


def ask(client, h, question, history=()):
    return sse_events(client.post("/chat", json={"message": question, "history": list(history)}, headers=h).text)


def test_chat_drafts_then_rechecks_the_dates_it_picked(cal, at_now, client, make_user, fake_llm):
    h = make_user()
    fake_llm.category = "calendar"
    fake_llm.draft = "Wednesday, Nov 4 is an L1 day [2].\nANSWER DATES: 2026-11-04"
    fake_llm.reply = ["The next liturgy is ", "Wednesday, Nov 4 [2]."]
    events = ask(client, h, "when is the next liturgy?")
    assert events[0]["sources"] == [{"n": 1, "title": "SFHS School Activites & Events", "url": ""},
                                    {"n": 2, "title": "SFHS School Schedule", "url": ""}]
    assert "".join(e.get("delta", "") for e in events) == "The next liturgy is Wednesday, Nov 4 [2]."
    (draft,), (check,) = fake_llm.calls_for("draft"), fake_llm.calls_for("answer")
    assert schedule.DAY_TYPES in draft[0]["content"] and router.CALENDAR_DRAFT_NOTE in draft[0]["content"]
    assert draft[-1]["content"].startswith("Right now: Monday, October 5, 2026, 9:30 AM (school time).")
    assert "Handbook" not in draft[-1]["content"]   # calendar only
    assert "Full calendar entries for the date(s) the draft is about" in check[-1]["content"]
    assert "Wednesday, November 4, 2026\n  school day: L1 - Mass of All Saints [2]" in check[-1]["content"]
    assert check[-1]["content"].endswith("Draft answer:\nWednesday, Nov 4 is an L1 day [2].")   # ANSWER DATES stripped
    assert router.CALENDAR_DRAFT_NOTE not in check[0]["content"]


def test_undated_and_not_on_the_calendar_falls_back_to_sfhs_com(cal, at_now, client, make_user, fake_llm):
    h = make_user()
    fake_llm.category = "calendar"
    fake_llm.queue = [["calendar"], ["NOT FOUND"]]   # classify, then the calendar draft
    events = ask(client, h, "when is the admissions deadline")
    calendar_sources, site_sources = [e["sources"] for e in events if "sources" in e]
    assert calendar_sources[0]["title"] == "SFHS School Activites & Events"
    assert site_sources[0] == {"n": 1, "title": "Admissions", "url": "https://www.sfhs.com/admissions"}
    second_draft = fake_llm.calls_for("draft")[1][-1]["content"]
    assert "Full text of the sfhs.com pages" in second_draft and "apply for admission" in second_draft


def test_a_dated_question_with_nothing_on_it_stays_on_the_calendar(cal, at_now, client, make_user, fake_llm):
    h = make_user()
    fake_llm.category, fake_llm.draft = "calendar", "NOT FOUND"
    ask(client, h, "anything happening saturday?")
    assert len(fake_llm.calls_for("draft")) == 1   # no fallback
    assert "Saturday, October 10, 2026\n  weekend (no school day listed)" in fake_llm.calls_for("answer")[0][-1]["content"]


def test_two_categories_read_both_sources(cal, at_now, client, make_user, fake_llm):
    h = make_user()
    fake_llm.category = "calendar, rules"
    events = ask(client, h, "can I bring my phone to the dance next monday?")
    titles = [s["title"] for s in events[0]["sources"]]
    assert titles == ["SFHS School Activites & Events", "SFHS School Schedule", "Student-Parent Handbook 2026-27 §4.4 Dances"]
    draft = fake_llm.calls_for("draft")[0]
    assert "[3] Student-Parent Handbook 2026-27 §4.4 Dances" in draft[-1]["content"]
    assert router.RULES_NOTE in draft[0]["content"] and schedule.DAY_TYPES in draft[0]["content"]


def test_search_is_the_backup_when_nothing_matches_by_name(cal, monkeypatch):
    concert = next(ev for ev in calendars.events if "Concert" in ev["title"])
    monkeypatch.setattr(calendars, "search", lambda q, k=8: [concert])
    matches = schedule.context("when can I hear the choir sing", [], NOW)[0].split("to the end of the school year")[1]
    assert "Fall Band & Choir Concert" in matches   # "choir" matches the name directly, search not needed
    matches = schedule.context("when's the big music night", [], NOW)[0].split("to the end of the school year")[1]
    assert "Fall Band & Choir Concert" in matches and "Robotics" not in matches   # nothing by name: search's pick
