from datetime import date, datetime

import pytest
import requests

from app import config, menu, prompts
from tests.test_app import sse_events

TZ = menu.SCHOOL_TZ
NOW = datetime(2026, 10, 6, 11, 0, tzinfo=TZ)   # Tuesday

# The same two layouts as sfhs.com/lancer-cafe: breakfast specials as bold dates + lists, lunch as nested lists
PAGE = """<html><body><table><tbody>
<tr><th>Breakfast</th></tr>
<tr><td>
  <p>Whether you're fueling up for class, breakfast has you covered.</p>
  <p><strong>Hours&nbsp;</strong>&mdash; 7:15am-9:00am<br/><strong>Location</strong> &mdash; Welcome Center Dining Commons<br/>
     <strong>Payment</strong> &mdash; MySchoolBucks (ID Card)</p>
  <h6>Daily Breakfast Items</h6><ul><li>Parfait &ndash; $4.50</li><li>Pastry &ndash; $3.50</li></ul>
  <h6>Chef&rsquo;s Table Features</h6>
  <p><b>Tues (10/6)</b></p><ul><li>Stacked Pancakes (three buttermilk pancakes)</li></ul>
  <p><b>Wed (10/7)</b></p><ul><li>Supreme Croissant Breakfast Sandwich</li></ul>
  <p><b>Fri (10/9)</b></p><ul><li>Buttermilk Waffles</li></ul>
  <p>&nbsp;</p>
</td></tr>
<tr><th>Lunch</th></tr>
<tr><td>
  <p>Lunch is fresh, hot, and ready when you are.</p>
  <p><strong>Hours</strong> &mdash; varies depending on day/schedule</p>
  <ul>
    <li><span>Tues (10/6)&nbsp;</span><ul><li><span>Meat Lovers Pizza Individual--$5.50&nbsp;</span></li>
        <li>Main Entr&eacute;e - Gourmet Burger with garlic fries--$8.25</li></ul></li>
    <li>Wed (10/7)<ul><li>Bianca Pizza Individual--$5.50</li><li>Vegetarian- Pesto Cheese Raviolis--$8.25</li></ul></li>
    <li>Fri (10/9)<ul><li>Margarita Pizza Individual--$5.50</li></ul></li>
    <li>Tues (10/13)<ul><li>BBQ Chicken Pizza Individual--$5.50</li></ul></li>
    <li>Recurring Menu Items</li><li>Fries - $3.00</li><li>Acai Bowl - $6.50</li>
  </ul>
</td></tr>
<tr><th>After-School Snack Bar</th></tr>
<tr><td><p><strong>Hours</strong> &mdash; 2:30pm - 6:00pm</p><ul><li>Churro &ndash; $3.00</li></ul>
  <p>Beverage Options Include:</p><ul><li>Milk Carton &ndash; $2.00</li></ul></td></tr>
</tbody></table></body></html>"""


@pytest.fixture
def loaded(monkeypatch):
    menu.sections, menu.fetched_at = menu.parse_page(PAGE, NOW.date()), NOW


def test_parses_both_layouts():
    breakfast, lunch, snacks = menu.parse_page(PAGE, NOW.date())
    assert breakfast.name == "Breakfast"
    assert breakfast.info == ["Hours — 7:15am-9:00am", "Location — Welcome Center Dining Commons", "Payment — MySchoolBucks (ID Card)"]
    assert breakfast.everyday == [("Daily Breakfast Items", ["Parfait – $4.50", "Pastry – $3.50"])]
    assert breakfast.days[date(2026, 10, 6)] == ("Chef’s Table Features", ["Stacked Pancakes (three buttermilk pancakes)"])
    assert lunch.days[date(2026, 10, 6)] == ("", ["Meat Lovers Pizza Individual — $5.50", "Main Entrée - Gourmet Burger with garlic fries — $8.25"])
    assert sorted(lunch.days) == [date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 9), date(2026, 10, 13)]
    assert lunch.everyday == [("Recurring Menu Items", ["Fries - $3.00", "Acai Bowl - $6.50"])]   # not part of 10/13
    assert snacks.days == {} and snacks.everyday == [("", ["Churro – $3.00"]), ("Beverage Options Include", ["Milk Carton – $2.00"])]


def test_dates_without_a_year():
    december = date(2026, 12, 18)
    lunch = menu.parse_page(PAGE.replace("Tues (10/13)", "Mon (1/4)"), december)[1]
    assert date(2027, 1, 4) in lunch.days   # January, seen in December, is next year


def test_no_menu_loaded_says_so():
    text, sources = menu.context("what's for lunch today?", [], NOW)
    assert "not available right now" in text and sources[0]["title"] == "Lancer Cafe menu"


def test_context_for_a_named_day(loaded):
    text, _ = menu.context("is there pizza friday", [], NOW)
    assert text.startswith('Time frame of the question: "friday" = Friday, October 9, 2026.')
    assert "Friday, October 9, 2026\n  Breakfast (Chef’s Table Features):\n    - Buttermilk Waffles\n  Lunch:\n    - Margarita Pizza Individual — $5.50" in text
    assert "Tuesday, October 6" not in text   # only the day asked about
    assert "  Lunch: Hours — varies depending on day/schedule\n    Recurring Menu Items: Fries - $3.00; Acai Bowl - $6.50" in text


def test_no_day_named_means_today_and_the_next_menu_day(loaded):
    text, _ = menu.context("what's for lunch?", [], NOW)
    assert "Tuesday, October 6, 2026 (TODAY)" in text and "Wednesday, October 7, 2026 (TOMORROW)" in text
    assert "Friday, October 9" not in text


def test_days_without_a_menu_say_so(loaded):
    text, _ = menu.context("what's for lunch this week", [], NOW)
    assert "Thursday, October 8, 2026\n  no menu posted for this day" in text
    assert "Saturday, October 10, 2026\n  no menu posted for this day (weekend)" in text


def test_follow_ups_keep_the_day(loaded):
    history = [{"role": "user", "content": "what's for lunch friday?"}, {"role": "assistant", "content": "Pizza [1]"}]
    assert "Friday, October 9, 2026" in menu.context("and breakfast?", history, NOW)[0]


def test_one_day_block_for_summaries(loaded):
    text, sources = menu.day_block(date(2026, 10, 7), NOW.date(), first_n=2)
    assert text.startswith("[2] Lancer Cafe menu") and "Bianca Pizza Individual — $5.50" in text and "Every day" not in text
    assert sources == [{"n": 2, "title": "Lancer Cafe menu", "url": config.MENU_URL}]


def test_chat_answers_from_the_menu_only(loaded, monkeypatch, client, make_user, fake_llm):
    monkeypatch.setattr(prompts, "school_now", lambda: NOW)
    h = make_user()
    fake_llm.category, fake_llm.reply = "menu", ["Wednesday, Oct 7: ", "Bianca Pizza ($5.50) [1]."]
    events = sse_events(client.post("/chat", json={"message": "what's for lunch tomorrow?"}, headers=h).text)
    assert events[0] == {"sources": [{"n": 1, "title": "Lancer Cafe menu", "url": config.MENU_URL}]}
    assert "".join(e.get("delta", "") for e in events) == "Wednesday, Oct 7: Bianca Pizza ($5.50) [1]."
    draft = fake_llm.calls_for("draft")[0]
    assert menu.NOTES in draft[0]["content"]
    assert draft[-1]["content"].startswith("Right now: Tuesday, October 6, 2026, 11:00 AM (school time).")
    assert '"tomorrow" = Wednesday, October 7, 2026' in draft[-1]["content"]
    assert "    - Bianca Pizza Individual — $5.50" in draft[-1]["content"] and "Handbook" not in draft[-1]["content"]


def test_refresh_keeps_the_last_menu_if_the_page_breaks(monkeypatch):
    class Resp:
        status_code, text = 200, PAGE
        def raise_for_status(self): pass
    monkeypatch.setattr(menu.requests, "get", lambda url, **kw: Resp())
    monkeypatch.setattr(config, "MENU_URL", "https://cafe")
    menu.refresh()
    assert menu.sections and menu.fetched_at
    good = menu.sections
    Resp.text = "<html><body>Page moved</body></html>"
    with pytest.raises(ValueError):
        menu.refresh()
    assert menu.sections is good
    monkeypatch.setattr(menu.requests, "get", lambda url, **kw: (_ for _ in ()).throw(requests.ConnectionError("down")))
    with pytest.raises(requests.ConnectionError):
        menu.refresh()
    assert menu.sections is good
