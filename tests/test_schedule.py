from datetime import datetime

import pytest

from app import calendars, db, prompts, schedule
from tests.conftest import rows
from tests.test_app import first, sse_events, statuses
from tests.test_calendars import NOW, TZ, feeds   # noqa: F401  (feeds is a fixture)


@pytest.fixture
def cal(feeds):   # noqa: F811
    calendars.refresh()


@pytest.mark.parametrize("question", [
    "is there school next monday?", "what's the schedule today", "what time does school end tomorrow",
    "what's happening this weekend", "when is the band concert", "any events coming up?", "is it a G2 day?",
    "when's junior retreat", "what's on oct 12",
])
def test_scheduling_questions_take_the_calendar_route(cal, question):
    assert schedule.plan(question, [], NOW) is not None


@pytest.mark.parametrize("question", [
    "what's the dress code", "can I bring a guest to the dance?", "are phones allowed at the dance tomorrow?",
    "what are the prerequisites for AP Chemistry", "how many credits do I need to graduate", "who is the dean",
])
def test_other_questions_dont(cal, question):
    assert schedule.plan(question, [], NOW) is None


def test_short_follow_ups_stay_on_the_calendar(cal):
    history = [{"role": "user", "content": "is there school next monday?"}, {"role": "assistant", "content": "Nope [1]"}]
    p = schedule.plan("what about friday?", history, NOW)
    assert p and (p.time_frame.start, p.time_frame.end) == (datetime(2026, 10, 9).date(),) * 2
    p = schedule.plan("and the band concert?", history, NOW)   # no date of its own: keeps the previous time frame
    assert p and p.time_frame.start == datetime(2026, 10, 12).date()
    assert schedule.plan("what's the dress code", history, NOW) is None


def test_agenda_for_a_single_day(cal):
    p = schedule.plan("is there school next monday?", [], NOW)
    assert p.sources == [{"n": 1, "title": "SFHS School Schedule", "url": ""}]
    assert p.agenda.splitlines() == [
        "Calendar entries from Mon Oct 12, 2026 through Mon Oct 12, 2026, day by day:",
        "",
        "Monday, October 12, 2026",
        "  NO SCHOOL [1]",
    ]


def test_agenda_for_today_has_hours_and_bell_schedule(cal):
    p = schedule.plan("what's the schedule today", [], NOW)
    lines = p.agenda.splitlines()
    i = lines.index("Monday, October 5, 2026 (TODAY)")
    assert lines[i + 1:i + 6] == ["  school day: B2 [1]", "    hours: 8:05 AM – 2:30 PM", "    B2 Day Schedule",
                                  "    7:45 - 8:00 Collaboration", "    8:05 - 2:30 School Day"]


def test_agenda_for_a_week_lists_every_day(cal):
    agenda = schedule.plan("what's happening this week", [], NOW).agenda
    for day in ("Monday, October 5, 2026 (TODAY)", "Tuesday, October 6, 2026 (TOMORROW)", "Wednesday, October 7, 2026",
                "Saturday, October 10, 2026", "Sunday, October 11, 2026"):
        assert day in agenda
    assert agenda.count("Junior Retreat #1") == 2   # Wed + Thu: multi-day events show on every day
    assert "  - all day, Wed Oct 7, 2026 – Thu Oct 8, 2026: Junior Retreat #1 [1]" in agenda
    assert "  - 7:00 PM – 9:00 PM: Fall Band & Choir Concert @ Graham Theater, Mountain View [1]" in agenda
    assert "Saturday, October 10, 2026\n  weekend (no school day listed)" in agenda
    assert "Tuesday, October 6, 2026 (TOMORROW)\n  no school-day entry on the calendar" in agenda
    assert "Robotics practice" in agenda and "Oct 13" not in agenda   # only this week


def test_events_already_over_today_are_marked(cal):
    late = datetime(2026, 10, 8, 21, 30, tzinfo=TZ)   # Thursday 9:30 PM, the concert ended at 9
    agenda = schedule.plan("what's on tonight", [], late).agenda
    assert "Fall Band & Choir Concert @ Graham Theater, Mountain View [1] (already over)" in agenda


def test_named_events_outside_two_weeks_are_found_by_search(cal):
    agenda = schedule.plan("when is robotics practice", [], NOW).agenda
    assert "Other calendar entries that match the question (outside those two weeks):" in agenda
    assert "  - Tue Oct 20, 2026, 3:30 PM – 5:00 PM: Robotics practice [1]" in agenda


def ask(client, h, question, history=()):
    return sse_events(client.post("/chat", json={"message": question, "history": list(history)}, headers=h).text)


@pytest.fixture
def at_now(monkeypatch):
    monkeypatch.setattr(prompts, "school_now", lambda: NOW)


def test_chat_checks_the_calendar_then_answers(cal, at_now, client, make_user, fake_llm):
    h = make_user()
    fake_llm.queue = [["Mon Oct 12, 2026: NO SCHOOL [1]"], ["Nope, ", "no school Monday, Oct 12 [1]."]]
    events = ask(client, h, "is there school next monday?")
    assert first(events, "sources") == [{"n": 1, "title": "SFHS School Schedule", "url": ""}]
    assert statuses(events) == ['Working out the dates: "next monday" = Monday, October 12, 2026',
                                "Checking the school calendars…", "Double-checking the calendar entries…",
                                "Writing the answer…"]
    assert first(events, "checked") == [
        'Worked out the time frame: "next monday" = Monday, October 12, 2026',
        "Read 1 calendar entry from Mon Oct 12, 2026 through Mon Oct 12, 2026 (SFHS School Schedule)",
        "Double-check notes:\nMon Oct 12, 2026: NO SCHOOL [1]"]
    assert "".join(e.get("delta", "") for e in events) == "Nope, no school Monday, Oct 12 [1]."
    assert events[-1] == {"done": True}

    check, answer = fake_llm.calls
    assert check[0]["content"] == schedule.CHECK_INSTRUCTIONS
    assert check[1]["content"].startswith("Right now: Monday, October 5, 2026, 9:30 AM (school time).\n"
                                          'Time frame of the question: "next monday" = Monday, October 12, 2026.')
    assert schedule.ANSWER_INSTRUCTIONS in answer[0]["content"] and "Never call the student dude" in answer[0]["content"]
    assert "Checked notes:\nMon Oct 12, 2026: NO SCHOOL [1]" in answer[-1]["content"]
    assert "Monday, October 12, 2026\n  NO SCHOOL [1]" in answer[-1]["content"]
    assert "Handbook" not in answer[-1]["content"]   # calendars only
    assert rows(db.usage)[0]["count"] == 1           # two model calls, one message


def test_nothing_on_the_calendar_falls_back_to_all_sources(cal, at_now, client, make_user, fake_llm):
    h = make_user()
    fake_llm.queue = [["NONE"]]
    events = ask(client, h, "when is the dance")
    check, general = fake_llm.calls
    assert "Answer questions about Saint Francis High School" in general[0]["content"]
    calendar_sources, general_sources = [e["sources"] for e in events if "sources" in e]
    assert calendar_sources != general_sources and len(general_sources) == 5   # search results replaced them
    assert "Nothing on the calendars, so searching all of sfMate's sources…" in statuses(events)
    assert first(events, "checked")[2] == "Double-check: nothing on the calendars matches, so searched all sources instead"


def test_a_dated_question_with_nothing_on_it_still_answers_from_the_calendar(cal, at_now, client, make_user, fake_llm):
    h = make_user()
    fake_llm.queue = [["NONE"], ["Nothing's on the calendar for Saturday, Oct 10."]]
    ask(client, h, "anything happening saturday?")
    check, answer = fake_llm.calls
    assert "Checked notes:\nNothing on the calendar matches." in answer[-1]["content"]
    assert "Saturday, October 10, 2026\n  weekend (no school day listed)" in answer[-1]["content"]


def test_other_questions_skip_the_check(cal, at_now, client, make_user, fake_llm):
    h = make_user()
    ask(client, h, "are phones allowed at the dance tomorrow?")
    assert len(fake_llm.calls) == 1
    system = fake_llm.calls[0][0]["content"]
    assert "Answer questions about Saint Francis High School" in system
    assert "The question's time frame, already worked out: \"tomorrow\" = Tuesday, October 6, 2026." in system
