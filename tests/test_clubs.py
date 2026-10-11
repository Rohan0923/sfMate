import pytest
import requests

from app import clubs, config, prompts, router
from tests.test_app import sse_events

PAGE = open("tests/fixtures/clubs_page.html", encoding="utf-8").read()   # sfhs.com/clubs-affinity-groups, 10/2026
URL = "https://www.sfhs.com/clubs-affinity-groups"
GROUPS = ["Activity Clubs", "Trial Clubs", "Leadership Groups", "Performance Groups", "Spirit Groups",
          "Affinity Groups", "Honor Societies"]


@pytest.fixture
def loaded(monkeypatch):
    class Resp:
        text = PAGE
        def raise_for_status(self): pass
    monkeypatch.setattr(clubs.requests, "get", lambda url, **kw: Resp())
    monkeypatch.setattr(config, "CLUBS_URL", URL)
    clubs.refresh()
    return Resp


def test_all_seven_dropdowns_are_read():
    groups, found, intro = clubs.parse_page(PAGE)
    assert groups == GROUPS
    counts = {g: sum(c["group"] == g for c in found) for g in groups}
    assert counts == {"Activity Clubs": 57, "Trial Clubs": 28, "Leadership Groups": 17, "Performance Groups": 19,
                      "Spirit Groups": 7, "Affinity Groups": 14, "Honor Societies": 3}
    asb = next(c for c in found if c["name"] == "ASB")
    assert asb["group"] == "Leadership Groups" and asb["subtitle"] == "(Associated Student Body)"
    assert asb["moderator"] == "Ms. Grace Savinovich" and asb["emails"] == ["gracesavinovich@sfhs.com"]
    assert asb["description"].startswith("Saint Francis ASB (Associated Student Body) is a student leadership organization")
    assert sum(1 for c in found if c["moderator"]) == 144


def test_moderators_and_links():
    found = clubs.parse_page(PAGE)[1]
    chess = next(c for c in found if c["name"] == "Chess Club")
    assert chess["moderator"] == "Mr. Terry O'Donnell" and chess["emails"] == ["terryodonnell@sfhs.com"]
    assert chess["links"] == [("Website", "https://docs.google.com/document/d/1L4epgZlpADF2OgkR8XfQ5mXEIQr4uQGe6dYAdf6sfUg/edit?usp=sharing")]
    assert chess["description"] == "We provide the opportunity to play and learn more about chess."   # no moderator/link text in it
    asl = next(c for c in found if c["name"] == "ASL Club")
    assert asl["moderator"] == "Ms. Sarah McBride, Mr. Patrick Kelly"
    assert asl["emails"] == ["sarahmcbride@sfhs.com", "patrickkelly@sfhs.com"]


def test_a_dropdown_without_cards_is_kept_whole():
    html = ("<table class='site-table-accordion'><tr><th>Spirit Groups</th></tr>"
            "<tr><td><p>Pom Squad</p><p>Rally Board</p></td></tr></table>")
    groups, found, _ = clubs.parse_page(html)
    assert found == [{"group": "Spirit Groups", "name": "Spirit Groups", "subtitle": "", "moderator": "", "emails": [],
                      "links": [], "description": "Pom Squad\nRally Board"}]


def test_the_full_list_is_always_there(loaded):
    text, sources = clubs.context("is there a chess club?")
    assert sources == [{"n": 1, "title": "Clubs & Affinity Groups", "url": URL}]
    listing = text.split("Every club and group on the page, by dropdown (this list is complete):")[1].split("Full details")[0]
    for g in GROUPS:
        assert f"  {g} (" in listing
    assert "Spirit Groups (7): Cage Captains; Cannon Crew;" in listing
    assert "ASB (Associated Student Body)" in listing


def test_details_for_named_clubs_and_whole_dropdowns(loaded):
    details = lambda q: clubs.context(q)[0].split("Full details for the clubs most related to the question:")[1]
    chess = details("is there a chess club?")
    assert "- Chess Club — Activity Clubs\n  Moderator: Mr. Terry O'Donnell (terryodonnell@sfhs.com)\n  Links: Website: https://" in chess
    assert "- Chess Club — Activity Clubs" in details("what club does Mr. O'Donnell moderate?")   # by moderator
    affinity = details("what affinity groups are there?")
    entries = [line for line in affinity.splitlines() if line.startswith("- ")]
    assert all("— Affinity Groups" in line for line in entries[:14])   # every entry in the dropdown it names, first
    assert affinity.count("— Affinity Groups") == 14
    assert "- Black Student Union (BSU) — Affinity Groups" in details("when does BSU meet")   # by acronym
    assert "- NHS (National Honor Society) — Honor Societies" in details("how do I get into NHS?")
    assert "- Pom Squad — Spirit Groups" in details("how do I join pom squad")


def test_details_stay_within_budget(loaded):
    text, _ = clubs.context("what activity clubs and trial clubs are there?")   # 87 clubs named at once
    assert len(text.split("Full details")[1]) <= clubs.DETAIL_CHARS + 500


def test_refresh_keeps_the_last_copy_if_the_page_breaks(loaded, monkeypatch):
    good = clubs.clubs
    loaded.text = "<html><body>Page moved</body></html>"
    with pytest.raises(ValueError):
        clubs.refresh()
    monkeypatch.setattr(clubs.requests, "get", lambda url, **kw: (_ for _ in ()).throw(requests.ConnectionError("down")))
    with pytest.raises(requests.ConnectionError):
        clubs.refresh()
    assert clubs.clubs is good


def test_chat_uses_the_clubs_page(loaded, monkeypatch, client, make_user, fake_llm):
    monkeypatch.setattr(prompts, "school_now", lambda: __import__("tests.test_router", fromlist=["NOW"]).NOW)
    h = make_user()
    fake_llm.category = "clubs"
    events = sse_events(client.post("/chat", json={"message": "is there a chess club?"}, headers=h).text)
    assert events[0]["sources"] == [{"n": 1, "title": "Clubs & Affinity Groups", "url": URL}]
    draft = fake_llm.calls_for("draft")[0]
    assert clubs.NOTES in draft[0]["content"] and "- Chess Club — Activity Clubs" in draft[-1]["content"]
    assert "Handbook" not in draft[-1]["content"]


def test_not_on_the_page_falls_back_to_the_rest_of_sfhs_com(loaded, client, make_user, fake_llm):
    h = make_user()
    fake_llm.queue = [["clubs"], ["NOT FOUND"]]
    events = sse_events(client.post("/chat", json={"message": "how do I apply for admission"}, headers=h).text)
    assert [e["sources"][0]["title"] for e in events if "sources" in e] == ["Clubs & Affinity Groups", "Admissions"]


def test_page_not_loaded_yet_uses_club_pages_on_sfhs_com():
    ctx = router.build(["clubs"], "how do I join a club", [], __import__("tests.test_router", fromlist=["NOW"]).NOW, [])[0]
    assert ctx.sources[0]["url"] == "https://www.sfhs.com/clubs-and-activities" and "over 80 clubs" in ctx.text
