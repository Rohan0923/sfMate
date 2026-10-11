from datetime import date

import pytest

from app.dates import resolve

MON = date(2026, 10, 5)    # Monday
THU = date(2026, 10, 8)
SAT = date(2026, 10, 10)
SUN = date(2026, 10, 11)
NYE = date(2026, 12, 30)   # Wednesday


def d(month, day, year=2026):
    return date(year, month, day)


@pytest.mark.parametrize("today,question,start,end", [
    (MON, "what's going on today?", d(10, 5), d(10, 5)),
    (MON, "anything tonight", d(10, 5), d(10, 5)),
    (MON, "what time does school end tomorrow", d(10, 6), d(10, 6)),
    (MON, "is there school tmrw", d(10, 6), d(10, 6)),
    (MON, "the day after tomorrow", d(10, 7), d(10, 7)),
    (MON, "what happened yesterday", d(10, 4), d(10, 4)),
    (MON, "schedule for monday", d(10, 5), d(10, 5)),           # today is Monday
    (MON, "what's on friday", d(10, 9), d(10, 9)),
    (MON, "this Friday", d(10, 9), d(10, 9)),
    (MON, "next friday", d(10, 16), d(10, 16)),                 # Friday of next week
    (MON, "is there school next Monday?", d(10, 12), d(10, 12)),
    (MON, "last friday", d(10, 2), d(10, 2)),
    (MON, "what's happening this weekend", d(10, 10), d(10, 11)),
    (MON, "next weekend", d(10, 17), d(10, 18)),
    (MON, "last weekend", d(10, 3), d(10, 4)),
    (MON, "events this week", d(10, 5), d(10, 11)),
    (MON, "what's next week look like", d(10, 12), d(10, 18)),
    (MON, "last week", d(9, 28), d(10, 4)),
    (MON, "what's on oct 12", d(10, 12), d(10, 12)),
    (MON, "October 12th?", d(10, 12), d(10, 12)),
    (MON, "the 12th of october", d(10, 12), d(10, 12)),
    (MON, "10/12", d(10, 12), d(10, 12)),
    (MON, "10/12/26", d(10, 12), d(10, 12)),
    (MON, "Oct 12, 2026", d(10, 12), d(10, 12)),
    (MON, "jan 8", d(1, 8, 2027), d(1, 8, 2027)),               # already long past this year: means next year
    (MON, "what happened on sept 20", d(9, 20), d(9, 20)),      # recently past: this year
    (MON, "events in november", d(11, 1), d(11, 30)),
    (MON, "anything in may", d(5, 1, 2027), d(5, 31, 2027)),
    (MON, "may 14", d(5, 14, 2027), d(5, 14, 2027)),
    (MON, "the next 3 days", d(10, 5), d(10, 7)),
    (MON, "next few days", d(10, 5), d(10, 7)),
    (MON, "in 3 days", d(10, 8), d(10, 8)),
    (MON, "the next two weeks", d(10, 5), d(10, 18)),
    (MON, "this month", d(10, 5), d(10, 31)),
    (MON, "next month", d(11, 1), d(11, 30)),
    (MON, "in december", d(12, 1), d(12, 31)),
    (THU, "this tuesday", d(10, 6), d(10, 6)),                  # this week's, already past
    (THU, "tuesday", d(10, 13), d(10, 13)),                     # the next one
    (THU, "next tuesday", d(10, 13), d(10, 13)),
    (THU, "is there school Thursday", d(10, 8), d(10, 8)),      # today
    (SAT, "this weekend", d(10, 10), d(10, 11)),
    (SAT, "next weekend", d(10, 17), d(10, 18)),
    (SAT, "monday", d(10, 12), d(10, 12)),
    (SAT, "next monday", d(10, 12), d(10, 12)),
    (SAT, "this week", d(10, 10), d(10, 18)),                   # weekend: the coming school week too
    (SUN, "upcoming events this week?", d(10, 11), d(10, 18)),
    (SUN, "this weekend", d(10, 11), d(10, 11)),
    (SUN, "last weekend", d(10, 3), d(10, 4)),
    (SUN, "next week", d(10, 12), d(10, 18)),
    (NYE, "next month", d(1, 1, 2027), d(1, 31, 2027)),
    (NYE, "jan 4", d(1, 4, 2027), d(1, 4, 2027)),
    (NYE, "this weekend", d(1, 2, 2027), d(1, 3, 2027)),
    (NYE, "tomorrow", d(12, 31), d(12, 31)),
])
def test_time_frames(today, question, start, end):
    r = resolve(question, today)
    assert r is not None, question
    assert (r.start, r.end) == (start, end), (question, r)


@pytest.mark.parametrize("question", [
    "what's the dress code", "how do I prep for the SAT", "may I leave campus at lunch?", "when is junior retreat",
    "what's on feb 30", "who's the dean", "is 3 days of absence excused",
])
def test_no_time_frame(question):
    assert resolve(question, MON) is None


def test_long_ranges_are_capped():
    r = resolve("the next 400 days", MON)
    assert (r.end - r.start).days == 61


def test_description_spells_out_the_dates():
    assert resolve("next friday", MON).describe() == '"next friday" = Friday, October 16, 2026'
    assert resolve("this weekend", MON).describe() == '"this weekend" = Saturday, October 10, 2026 through Sunday, October 11, 2026'
