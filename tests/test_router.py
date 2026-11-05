from datetime import date, datetime, timedelta

import pytest

from app import announcements, prompts, router
from tests.test_app import EMAIL, fake_announcement, sse_events

TODAY = date(2026, 10, 9)


@pytest.mark.parametrize("model_says,question,expected", [
    ("rules", "can I wear a hoodie?", ["rules"]),
    ("Calendar, Menu.", "is there school friday and what's for lunch", ["calendar", "menu"]),
    ("courses, rules, calendar", "what do I need for AP Chem", ["courses", "rules"]),        # at most two
    ("misc, clubs", "how do I start a club", ["clubs"]),                                     # misc only on its own
    ("calendar", "what's in today's announcements?", ["announcements", "calendar"]),         # knee-jerk: announcements
    ("misc", "what's for lunch", ["menu"]),                                                  # knee-jerk: food
    ("announcements", "summarize the announcements and lunch", ["announcements", "menu"]),
    ("I think this is about dates", "is there school monday?", ["calendar"]),                # unusable: keyword rules
    ("???", "who is the principal", ["misc"]),
    ("general", "wassup, detroit?", ["general"]),
    ("general, menu", "what's good for lunch today?", ["menu"]),                             # general only on its own
    ("general, misc", "any good coffee shops near school?", ["general"]),
])
def test_classify(fake_llm, question, model_says, expected):
    fake_llm.category = model_says
    assert router.classify(question, [], TODAY) == expected


def test_classifier_sees_the_previous_question(fake_llm):
    router.classify("what about friday?", [{"role": "user", "content": "is there school monday?"}], TODAY)
    asked = fake_llm.calls_for("classify")[0][1]["content"]
    assert asked == "Previous question (for context): is there school monday?\nQuestion: what about friday?"


NOW = datetime(2026, 10, 9, 10, 0, tzinfo=announcements.SCHOOL_TZ)


def email_on(day, ann_id="<a@sfhs.com>", text=EMAIL):
    sent = datetime(day.year, day.month, day.day, 7, 30, tzinfo=announcements.SCHOOL_TZ)
    return {"id": ann_id, "sent": sent, "label": f"{sent:%a %b} {sent.day}, {sent.year}", "text": text}


def test_specific_announcement_questions_read_the_whole_email(monkeypatch):
    monkeypatch.setattr(announcements, "announcements", [email_on(TODAY)])
    (ctx,) = router._announcement_context("when is the multicultural showcase potluck?", [], NOW, ["library"], 1)
    assert "Thursday, October 15 at 2:45pm" in ctx.text and "Isla Jordan" in ctx.text   # everything, preferences aside
    assert ctx.note == router.ANNOUNCE_DETAIL_NOTE


def test_summaries_use_the_students_topics_plus_lunch(monkeypatch):
    monkeypatch.setattr(announcements, "announcements", [email_on(TODAY)])
    email, lunch = router._announcement_context("summarize today's announcements", [], NOW, ["athletics", "library"], 1)
    assert "## Athletics" in email.text and "## Library" in email.text
    assert "DECA" not in email.text and "Isla Jordan" not in email.text
    assert email.note == router.ANNOUNCE_SUMMARY_NOTE
    assert lunch.category == "menu" and lunch.sources[0]["n"] == 2


def test_which_email(monkeypatch):
    monkeypatch.setattr(announcements, "announcements", [email_on(TODAY - timedelta(days=1), "<thu>")])
    (ctx,) = router._announcement_context("what's in today's announcements", [], NOW, announcements.TOPIC_KEYS, 1)[:1]
    assert "sent Thu Oct 8, 2026" in ctx.text and "most recent one" in ctx.text   # today's hasn't come in yet
    (ctx,) = router._announcement_context("did wednesday's announcements mention DECA", [], NOW, announcements.TOPIC_KEYS, 1)
    assert ctx.text == ("There's no Daily Announcements email for Wednesday, October 7, 2026 in sfMate's inbox "
                        "(the latest one is from Thu Oct 8, 2026).")
    (ctx,) = router._announcement_context("did thursday's announcements mention DECA", [], NOW, announcements.TOPIC_KEYS, 1)
    assert "sent Thu Oct 8, 2026" in ctx.text and "most recent" not in ctx.text   # last Thursday, not next


def test_courses_and_rules_are_isolated():
    courses = router.build(["courses"], "AP chemistry prerequisite", [], NOW, [])[0]
    assert [s["title"] for s in courses.sources] == ["Course Scheduling Guide 2026-27 · 653 AP Chemistry"]
    rules = router.build(["rules"], "AP chemistry prerequisite", [], NOW, [])[0]   # same words, other source
    assert [s["title"] for s in rules.sources] == ["Student-Parent Handbook 2026-27 §4.4 Dances"]


def test_clubs_read_only_club_pages():
    clubs = router.build(["clubs"], "how do I apply for admission to a club", [], NOW, [])[0]
    assert [s["url"] for s in clubs.sources] == ["https://www.sfhs.com/clubs-and-activities"]
    assert "over 80 clubs" in clubs.text


def test_misc_reads_whole_pages():
    misc = router.build(["misc"], "how do I apply for admission", [], NOW, [])[0]
    assert misc.sources[0] == {"n": 1, "title": "Admissions", "url": "https://www.sfhs.com/admissions"}
    assert "apply for admission tuition financial aid deadline" in misc.text and not misc.falls_back


def test_chat_announcement_question_end_to_end(monkeypatch, client, make_user, fake_llm):
    monkeypatch.setattr(prompts, "school_now", lambda: NOW)
    monkeypatch.setattr(announcements, "announcements", [email_on(TODAY)])
    h = make_user()
    fake_llm.category = "calendar"   # the word "announcement" wins anyway
    events = sse_events(client.post("/chat", json={"message": "what did the announcements say about DECA?"}, headers=h).text)
    assert events[0]["sources"] == [{"n": 1, "title": "Daily Announcements · Fri Oct 9, 2026", "url": ""}]
    draft = fake_llm.calls_for("draft")[0][-1]["content"]
    assert "Applications are due Friday, October 9th!" in draft and "SFHS School" not in draft


def test_courses_not_found_falls_back_to_sfhs_com(monkeypatch, client, make_user, fake_llm):
    monkeypatch.setattr(prompts, "school_now", lambda: NOW)
    h = make_user()
    fake_llm.queue = [["courses"], ["NOT FOUND"]]
    events = sse_events(client.post("/chat", json={"message": "how much is tuition for admission"}, headers=h).text)
    assert [e["sources"][0]["title"] for e in events if "sources" in e] == [
        "Course Scheduling Guide 2026-27 · 653 AP Chemistry", "Admissions"]
    assert len(fake_llm.calls_for("draft")) == 2


def test_general_questions_skip_the_sources(monkeypatch, client, make_user, fake_llm):
    monkeypatch.setattr(prompts, "school_now", lambda: NOW)
    h = make_user()
    client.post("/profile", json={"first_name": "Rohan", "grade": 11}, headers=h)
    fake_llm.category = "general"
    events = sse_events(client.post("/chat", json={"message": "any good coffee shops near saint francis?"}, headers=h).text)
    assert events[0] == {"sources": []}
    assert "".join(e.get("delta", "") for e in events) == "Phones stay off the dance floor [1]."
    assert [fake_llm.step(m) for m in fake_llm.calls] == ["classify", "answer"]   # no draft, no check
    (msgs,) = fake_llm.calls_for("answer")
    assert "Mountain View" in msgs[0]["content"] and "grade 11 (junior)" in msgs[0]["content"]
    assert "Sources:" not in msgs[-1]["content"] and msgs[-1]["content"].endswith("any good coffee shops near saint francis?")


def test_summary_cache_key_depends_on_topics():
    everyone = router.summary_cache_key("<a>", None)
    assert everyone == router.summary_cache_key("<a>", {"skipped": True})
    assert everyone != router.summary_cache_key("<a>", {"announcements": ["athletics"]})


def test_summary_of_the_real_email_has_its_content(monkeypatch):
    monkeypatch.setattr(announcements, "announcements", [email_on(TODAY)])
    email, _ = router._announcement_context("summarize todays daily announcments", [], NOW, announcements.TOPIC_KEYS, 1)
    for must in ("Today is a B2 Schedule", "Isla Jordan and Taegan Miller", "LOST ITEMS!", "Join Chess during both lunches",
                 "Football @ St. Ignatius HS starting at 7:00 p.m.", "Applications are due Friday, October 9th!",
                 "Immersion Applications due October 24th", "PSAT on Wednesday, October 14th", "whiteboard polls"):
        assert must in email.text, must
    assert "never write that a topic had nothing" in email.note


def test_unrecognized_headings_send_the_whole_email(monkeypatch):
    odd = "Good morning!\nToday is a G1 day.\nRally at 2:40 in the gym.\nChess club meets at lunch."
    monkeypatch.setattr(announcements, "announcements", [email_on(TODAY, text=odd)])
    email, _ = router._announcement_context("summarize the announcements", [], NOW, ["athletics"], 1)
    assert "Rally at 2:40 in the gym." in email.text and "The whole email" in email.text


def test_summaries_ignore_earlier_answers_in_the_chat(monkeypatch, client, make_user, fake_llm):
    monkeypatch.setattr(prompts, "school_now", lambda: NOW)
    monkeypatch.setattr(announcements, "announcements", [email_on(TODAY)])
    h = make_user()
    history = [{"role": "user", "content": "summarize the announcements"},
               {"role": "assistant", "content": "No announcements today."}]   # an earlier wrong answer
    fake_llm.category = "announcements"
    client.post("/chat", json={"message": "summarize todays daily announcments", "history": history}, headers=h)
    for step in ("draft", "answer"):
        msgs = fake_llm.calls_for(step)[0]
        assert len(msgs) == 2 and "No announcements today." not in str(msgs)   # system + the question only
    assert "Isla Jordan" in fake_llm.calls_for("draft")[0][-1]["content"]


def test_old_saved_summaries_are_ignored():
    assert "#v2#" in router.summary_cache_key("<a>", None)


def test_summary_uses_the_emails_own_headings(monkeypatch):
    monkeypatch.setattr(announcements, "announcements", [email_on(TODAY)])
    email, _ = router._announcement_context("summarize the announcements", [], NOW, ["counseling", "library"], 1)
    assert "## Guidance & Counseling Announcements" in email.text and "## Library" in email.text
    assert "Counseling Announcements, Library Announcements" not in email.text   # no list of topic names to riff on
    assert "WARNING" not in email.text


def test_a_short_email_comes_with_a_warning(monkeypatch):
    monkeypatch.setattr(announcements, "announcements", [email_on(TODAY, text="for\nFriday\nOctober 9, 2026\nToday is a B2 Schedule")])
    email, _ = router._announcement_context("summarize the announcements", [], NOW, announcements.TOPIC_KEYS, 1)
    assert email.text.count("WARNING: the email sfMate received is only 49 characters") == 1
    assert "Do not fill in anything else." in email.text and "Today is a B2 Schedule" in email.text


def test_a_question_with_no_email_loaded_checks_the_inbox_right_then(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "GMAIL_ADDRESS", "sfmate@gmail.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "abcdefghijklmnop")
    monkeypatch.setattr(announcements, "fetch", lambda: [email_on(TODAY)])
    monkeypatch.setattr(announcements, "_index", lambda items: None)
    assert announcements.announcements == []   # e.g. right after a deploy
    (ctx,) = router._announcement_context("did the announcements mention DECA?", [], NOW, announcements.TOPIC_KEYS, 1)
    assert "Applications are due Friday, October 9th!" in ctx.text
    assert [a["label"] for a in announcements.announcements] == ["Fri Oct 9, 2026"]


def test_inbox_not_read_yet_is_said_plainly(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "GMAIL_ADDRESS", "sfmate@gmail.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "abcdefghijklmnop")
    monkeypatch.setattr(announcements, "fetch", lambda: (_ for _ in ()).throw(TimeoutError("imap")))
    (ctx,) = router._announcement_context("today's announcements", [], NOW, announcements.TOPIC_KEYS, 1)
    assert "hasn't been able to read its Daily Announcements inbox yet" in ctx.text


def test_summary_from_the_real_email(monkeypatch, client, make_user, fake_llm):
    from tests.test_announcements import REAL_EML
    ann, _ = announcements.parse_email(REAL_EML, "1")
    monkeypatch.setattr(prompts, "school_now", lambda: NOW)
    monkeypatch.setattr(announcements, "announcements", [ann])
    h = make_user()
    fake_llm.category = "announcements"
    events = sse_events(client.post("/chat", json={"message": "summarize today's announcments"}, headers=h).text)
    assert events[0]["sources"][0] == {"n": 1, "title": "Daily Announcements · Fri Oct 9, 2026", "url": ""}
    draft = fake_llm.calls_for("draft")[0][-1]["content"]
    for must in ("Today is a B2 Schedule", "Isla Jordan and Taegan Miller", "## Happening Today", "Join Chess",
                 "## Guidance & Counseling Announcements", "PSAT on Wednesday, October 14th", "## Library", "whiteboard polls"):
        assert must in draft, must
    assert "WARNING" not in draft and "<" not in draft.split("Sources:")[1].split("Question:")[0]


def test_other_accounts_never_get_the_announcements_category(fake_llm):
    assert "announcements:" in router.CLASSIFY_INSTRUCTIONS and "announcements" not in router.CLASSIFY_INSTRUCTIONS_PUBLIC
    fake_llm.category = "announcements"   # even a classifier that insists
    assert router.classify("whose birthday is it?", [], TODAY, school=False) == ["misc"]
    assert fake_llm.calls_for("classify")[0][0]["content"] == router.CLASSIFY_INSTRUCTIONS_PUBLIC
    fake_llm.category = "calendar"
    assert router.classify("what's in today's announcements?", [], TODAY, school=False) == ["calendar"]   # no knee-jerk
    assert router.classify("what's in today's announcements?", [], TODAY) == ["announcements", "calendar"]   # school accounts: as before
    assert "announcements" not in " ".join(router.classify("lunch announcements?", [], TODAY, school=False))
