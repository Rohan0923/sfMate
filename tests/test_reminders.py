from datetime import datetime

import pytest

from app import calendars, config, prompts, reminders
from tests.test_calendars import NOW, TZ, feeds, ics   # noqa: F401  (feeds is a fixture)

DAYS = ics(
    "SFHS School Schedule",
    "DTSTART;VALUE=DATE:20261005\r\nDTEND;VALUE=DATE:20261006\r\nSUMMARY:L2",   # today: its reminder was last night
    "DTSTART:20261006T150500Z\r\nDTEND:20261006T213000Z\r\nSUMMARY:L1 - Mass of St. Francis",   # tomorrow, 8:05 PDT
    "DTSTART;VALUE=DATE:20261007\r\nDTEND;VALUE=DATE:20261008\r\nSUMMARY:G1",
    "DTSTART;VALUE=DATE:20261008\r\nDTEND;VALUE=DATE:20261009\r\nSUMMARY:All-School Photo",
    "DTSTART;VALUE=DATE:20261030\r\nDTEND;VALUE=DATE:20261031\r\nSUMMARY:A1",
    "DTSTART:20261104T162000Z\r\nDTEND:20261104T223000Z\r\nSUMMARY:a2 – Yearbook Picture Day",   # 8:20 PST
)
OTHER = ics(   # same-looking title, but not on the School Schedule calendar
    "SFHS School Activites & Events",
    "DTSTART;VALUE=DATE:20261020\r\nDTEND;VALUE=DATE:20261021\r\nSUMMARY:A1 Robotics Showcase",
)


@pytest.fixture
def cal(feeds, monkeypatch):   # noqa: F811
    feeds.clear()
    feeds.update({"https://cal/days.ics": DAYS, "https://cal/other.ics": OTHER})
    monkeypatch.setattr(config, "CALENDAR_ICS_URLS", list(feeds))
    calendars.refresh()


def test_liturgy_and_activity_days_with_6pm_reminders(cal):
    assert reminders.upcoming(NOW) == [
        {"date": "2026-10-06", "kind": "liturgy", "day_type": "L1", "remind_at": "2026-10-05T18:00:00-07:00",
         "title": "Liturgy tomorrow", "body": "Tue, Oct 6 is an L1 day: Mass of St. Francis. School starts at 8:05 AM."},
        {"date": "2026-10-30", "kind": "activity", "day_type": "A1", "remind_at": "2026-10-29T18:00:00-07:00",
         "title": "Activity day tomorrow", "body": "Fri, Oct 30 is an A1 day (special activity schedule)."},
        {"date": "2026-11-04", "kind": "activity", "day_type": "A2", "remind_at": "2026-11-03T18:00:00-08:00",
         "title": "Activity day tomorrow", "body": "Wed, Nov 4 is an A2 day: Yearbook Picture Day. School starts at 8:20 AM."},
    ]


def test_a_reminder_drops_off_once_its_time_passes(cal):
    assert reminders.upcoming(datetime(2026, 10, 5, 18, 0, tzinfo=TZ))[0]["date"] == "2026-10-30"


def test_endpoint_needs_login(client, make_user, cal, monkeypatch):
    assert client.get("/reminders").status_code == 401
    monkeypatch.setattr(prompts, "school_now", lambda: NOW)
    r = client.get("/reminders", headers=make_user())
    assert [d["date"] for d in r.json()["days"]] == ["2026-10-06", "2026-10-30", "2026-11-04"]
