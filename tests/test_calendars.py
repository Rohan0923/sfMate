from datetime import datetime, timedelta

import pytest
import requests

from app import calendars, config, rag

TZ = calendars.SCHOOL_TZ
NOW = datetime(2026, 10, 5, 9, 30, tzinfo=TZ)   # a Monday
TODAY = NOW.date()


def ics(name, *events):
    body = "".join(f"BEGIN:VEVENT\r\nUID:{abs(hash(e))}@test\r\n{e}\r\nEND:VEVENT\r\n" for e in events)
    head = f"X-WR-CALNAME:{name}\r\n" if name else ""
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\n{head}{body}END:VCALENDAR\r\n".encode()


SCHEDULE = ics(
    "SFHS School Schedule",
    "DTSTART:20261005T150500Z\r\nDTEND:20261005T213000Z\r\nSUMMARY:B2\r\n"
    "DESCRIPTION:<b>B2 Day Schedule</b><br>  7:45 - 8:00 Collaboration<br>  8:05 - 2:30 School Day",
    "DTSTART;VALUE=DATE:20261012\r\nDTEND;VALUE=DATE:20261013\r\nSUMMARY:NO SCHOOL",
    "DTSTART;VALUE=DATE:20250901\r\nDTEND;VALUE=DATE:20250902\r\nSUMMARY:Last year's Labor Day",   # outside the window
)
EVENTS = ics(
    "SFHS School Activites & Events",
    "DTSTART;VALUE=DATE:20261007\r\nDTEND;VALUE=DATE:20261009\r\nSUMMARY:Junior Retreat #1",   # Oct 7-8
    "DTSTART:20261009T020000Z\r\nDTEND:20261009T040000Z\r\nSUMMARY:Fall Band & Choir Concert\r\n"
    "LOCATION:Graham Theater\\, Mountain View\r\nDESCRIPTION:Choir\\, Symphonic Band\\, and A Cappella.",
    "DTSTART;TZID=America/Los_Angeles:20261006T153000\r\nDTEND;TZID=America/Los_Angeles:20261006T170000\r\n"
    "RRULE:FREQ=WEEKLY;COUNT=4\r\nSUMMARY:Robotics practice",
    "DTSTART;VALUE=DATE:20261012\r\nDTEND;VALUE=DATE:20261013\r\nSUMMARY:NO SCHOOL",   # also on the other calendar
    "DTSTART:20261010T170000Z\r\nDTEND:20261010T180000Z\r\nSUMMARY:Cancelled fair\r\nSTATUS:CANCELLED",
)


def test_parse_feed_handles_all_day_timed_and_recurring():
    label, evs = calendars.parse_feed(EVENTS, TODAY, "fallback")
    assert label == "SFHS School Activites & Events"
    by_title = {}
    for ev in evs:
        by_title.setdefault(ev["title"], []).append(ev)
    retreat = by_title["Junior Retreat #1"][0]
    assert retreat["all_day"] and calendars.when(retreat) == "Wed Oct 7, 2026 – Thu Oct 8, 2026"   # end date is exclusive
    concert = by_title["Fall Band & Choir Concert"][0]
    assert calendars.when(concert) == "Thu Oct 8, 2026, 7:00 PM – 9:00 PM"   # UTC converted to school time
    assert concert["location"] == "Graham Theater, Mountain View"
    assert [calendars.day_label(e["start"].date()) for e in by_title["Robotics practice"]] == [
        "Tue Oct 6, 2026", "Tue Oct 13, 2026", "Tue Oct 20, 2026", "Tue Oct 27, 2026"]
    assert "Cancelled fair" not in by_title


def test_html_description_and_window():
    label, evs = calendars.parse_feed(SCHEDULE, TODAY, "fallback")
    assert [e["title"] for e in evs] == ["B2", "NO SCHOOL"]   # last year's event is dropped
    assert evs[0]["description"] == "B2 Day Schedule\n7:45 - 8:00 Collaboration\n8:05 - 2:30 School Day"
    assert calendars.parse_feed(ics("", "DTSTART;VALUE=DATE:20261006\r\nSUMMARY:x"), TODAY, "School calendar 1")[0] == "School calendar 1"


@pytest.fixture
def feeds(monkeypatch):
    bodies = {"https://cal/schedule.ics": SCHEDULE, "https://cal/events.ics": EVENTS}

    class Resp:
        def __init__(self, url):
            self.url = url

        def raise_for_status(self):
            if bodies[self.url] is None:
                raise requests.HTTPError("503 Server Error")

        @property
        def content(self):
            return bodies[self.url]

    monkeypatch.setattr(config, "CALENDAR_ICS_URLS", list(bodies))
    monkeypatch.setattr(calendars.requests, "get", lambda url, **kw: Resp(url))
    monkeypatch.setattr(calendars, "school_today", lambda: TODAY)
    return bodies


def test_refresh_merges_dedupes_and_indexes(feeds):
    calendars.refresh()
    titles = [e["title"] for e in calendars.events]
    assert titles.count("NO SCHOOL") == 1   # listed on both calendars, kept once
    assert titles.index("B2") < titles.index("Robotics practice") < titles.index("Junior Retreat #1")   # sorted by date
    chunks, embs = rag.EXTRA_SOURCES["calendars"]
    assert len(chunks) == len(calendars.events) == embs.shape[0]
    assert chunks[0]["title"] == "SFHS School Schedule · B2 · Mon Oct 5, 2026" and chunks[0]["url"] == ""
    hit = rag.retrieve("fall band choir concert", k=1)[0][0]
    assert "When: Thu Oct 8, 2026, 7:00 PM – 9:00 PM" in hit["text"] and "Where: Graham Theater" in hit["text"]


def test_a_failing_feed_keeps_its_last_copy(feeds):
    calendars.refresh()
    feeds["https://cal/events.ics"] = None
    calendars.refresh()
    assert "Fall Band & Choir Concert" in [e["title"] for e in calendars.events]


def test_refresh_only_embeds_new_events(feeds, monkeypatch):
    calendars.refresh()
    embedded = []
    real_embed = rag.embed
    monkeypatch.setattr(rag, "embed", lambda texts, **kw: embedded.extend(texts) or real_embed(texts, **kw))
    calendars.refresh()
    assert embedded == []
