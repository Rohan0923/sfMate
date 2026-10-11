"""The clubs path: sfhs.com/clubs-affinity-groups, read live and split into individual clubs.

The page has 7 dropdowns (Activity Clubs, Trial Clubs, Leadership Groups, Performance Groups, Spirit Groups, Affinity
Groups, Honor Societies). Their contents are all in the page's HTML: each club is a card with a bold name, sometimes
an italic subtitle like "(Associated Student Body)", a "Moderator:" line (linked to their school email), sometimes
Website/Instagram links, and a description. Too much to hand the model at once, so a
question gets the full list of names (complete, for "is there a ... club?") plus the details of the clubs it's about."""
import logging
import re
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import requests
from bs4 import BeautifulSoup

from . import config, rag
from .announcements import clean_text, html_to_text

log = logging.getLogger("sfmate.clubs")

SCHOOL_TZ = ZoneInfo(config.SCHOOL_TIMEZONE)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; sfMate/1.0)"}
MIN_CLUBS = 20          # fewer than this means the page changed shape: keep the last good copy
DETAIL_CHARS = 7000     # full descriptions handed over per question
INTRO_CHARS = 900
GENERIC = set("club clubs student students association union group groups society societies council team the and of for "
              "a an in at to with club's".split())
GROUP_WORDS = [   # words in a question -> a dropdown it's asking about as a whole
    (re.compile(r"activit(y|ies) clubs?", re.I), "Activity Clubs"),
    (re.compile(r"trial clubs?", re.I), "Trial Clubs"),
    (re.compile(r"leadership", re.I), "Leadership Groups"),
    (re.compile(r"performance|performing", re.I), "Performance Groups"),
    (re.compile(r"spirit (group|squad|club)s?", re.I), "Spirit Groups"),
    (re.compile(r"affinity", re.I), "Affinity Groups"),
    (re.compile(r"honou?r (societ(y|ies)|groups?)", re.I), "Honor Societies"),
]

clubs = []        # [{"group", "name", "subtitle", "moderator", "emails", "links", "description"}], page order
groups = []       # dropdown names, page order
intro = ""        # the paragraph above the dropdowns
fetched_at = None
_embs = None      # embeddings of `clubs`, same order
_emb_cache = {}
_stop = threading.Event()


# ---------- reading the page ----------
def _text(tag):
    return clean_text(html_to_text(str(tag)))


def parse_page(html):
    """(groups, clubs, intro). Each dropdown's cards become clubs; a dropdown without cards is kept as one entry."""
    soup = BeautifulSoup(html, "html.parser")
    found_groups, found = [], []
    table = soup.find("table", class_="site-table-accordion")
    for th in (table.find_all("th") if table else []):
        group = " ".join(th.get_text(" ").split())
        row = th.find_parent("tr")
        nxt = row.find_next_sibling("tr") if row else None
        td = nxt.find("td") if nxt else None
        if not td:
            continue
        found_groups.append(group)
        cards = td.find_all("div", class_="acc-card")
        if not cards:   # the dropdown isn't made of cards anymore: keep its text whole rather than lose it
            found.append(_club(group, group, description=_text(td)[:3000]))
            continue
        for card in cards:
            club = _parse_card(group, card)
            if club:
                found.append(club)
    content = soup.find("table", class_="site-table-content")
    return found_groups, found, (_text(content)[:INTRO_CHARS] if content else "")


def _club(group, name, subtitle="", moderator="", emails=(), links=(), description=""):
    return {"group": group, "name": name, "subtitle": subtitle, "moderator": moderator, "emails": list(emails),
            "links": list(links), "description": description}


def _parse_card(group, card):
    """One club card: bold name, optional (subtitle), "Moderator(s): …" with mailto links, link-only lines, description."""
    seen, name, subtitle, moderator, emails, links, description = set(), "", "", "", [], [], []
    for p in card.find_all("p"):
        text = " ".join(p.get_text(" ").split())
        if not text or text in seen:   # nested wrappers repeat paragraphs
            continue
        seen.add(text)
        anchors = [a for a in p.find_all("a") if a.get("href")]
        if not name:
            name = text
        elif not subtitle and text.startswith("(") and text.endswith(")"):
            subtitle = text
        elif re.match(r"moderators?\s*:", text, re.I):
            moderator = re.sub(r"^moderators?\s*:\s*", "", text, flags=re.I).replace(" | ", ", ")
            emails += [a["href"][7:].split("?")[0] for a in anchors if a["href"].lower().startswith("mailto:")]
        elif anchors and text == " ".join(" ".join(a.get_text(" ").split()) for a in anchors):   # a line of just links
            links += [(" ".join(a.get_text(" ").split()), a["href"]) for a in anchors]
        else:
            description.append(text)
    return _club(group, name, subtitle, moderator, emails, links, "\n".join(description)) if name else None


def _entry_text(club):
    return (f"{club['group']}: {club['name']} {club['subtitle']}\nModerator: {club['moderator']}\n"
            f"{club['description']}").strip()


def refresh():
    global clubs, groups, intro, fetched_at, _embs
    r = requests.get(config.CLUBS_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    found_groups, found, found_intro = parse_page(r.text)
    if len(found) < MIN_CLUBS:
        raise ValueError(f"found only {len(found)} clubs on the page")
    texts = [_entry_text(c) for c in found]
    new = [t for t in dict.fromkeys(texts) if t not in _emb_cache]
    if new:
        _emb_cache.update(zip(new, rag.embed(new)))
    embs = np.stack([_emb_cache[t] for t in texts])
    clubs, groups, intro, fetched_at, _embs = found, found_groups, found_intro, datetime.now(SCHOOL_TZ), embs
    log.info("clubs: %d across %s", len(found), ", ".join(f"{g} ({sum(c['group'] == g for c in found)})" for g in found_groups))


def _refresher(stop):
    while True:
        try:
            refresh()
        except Exception as e:
            log.error("clubs refresh failed (keeping the last copy): %s: %s", type(e).__name__, e)
        if stop.wait((config.CLUBS_REFRESH_MINUTES if clubs else 2) * 60):   # not loaded yet: retry soon
            return


def start():
    if not config.CLUBS_URL:
        log.warning("the clubs page is off: set CLUBS_URL")
        return
    _stop.clear()
    threading.Thread(target=_refresher, args=(_stop,), daemon=True, name="clubs").start()


def stop():
    _stop.set()


# ---------- what the model reads ----------
def _words(text):
    return {w for w in re.findall(r"[a-z0-9&]+", text.lower()) if len(w) >= 2 and w not in GENERIC}


TITLES = {"mr", "mrs", "ms", "dr", "br", "fr", "sr", "coach"}


def _by_name(question, club):
    """The question names this club (a distinctive word of its name or subtitle, or its acronym) or its moderator."""
    asked = _words(question)
    name_words = {w for w in _words(f"{club['name']} {club['subtitle']}") if len(w) >= 3 or w.isupper()}
    acronyms = {a.lower() for a in re.findall(r"\(([A-Za-z&]{2,6})\)", club["name"] + club["subtitle"])}
    moderator_names = {w for w in _words(club["moderator"]) if len(w) >= 3 and w not in TITLES}
    return bool((name_words | acronyms | moderator_names) & asked)


def pick(question, k=6):
    """The clubs whose full details to hand over: whole dropdowns the question names, then clubs it names, then
    the closest matches by meaning."""
    named_groups = [g for pattern, g in GROUP_WORDS if pattern.search(question) and g in groups]
    chosen = [c for c in clubs if c["group"] in named_groups]
    chosen += [c for c in clubs if _by_name(question, c) and c not in chosen]
    if _embs is not None and len(clubs):
        scores = _embs @ rag.embed([question], query=True)[0]
        chosen += [clubs[i] for i in np.argsort(-scores)[:k] if clubs[i] not in chosen]
    return chosen


def _detail(club):
    lines = [f"- {club['name']} {club['subtitle']}".rstrip() + f" — {club['group']}"]
    if club["moderator"]:
        lines.append(f"  Moderator: {club['moderator']}" + (f" ({', '.join(club['emails'])})" if club["emails"] else ""))
    if club["links"]:
        lines.append("  Links: " + "; ".join(f"{label}: {url}" for label, url in club["links"]))
    lines.append("  " + (club["description"] or "(no description on the page)").replace("\n", "\n  "))
    return "\n".join(lines)


def context(question, first_n=1):
    """(text, sources). None if the page hasn't been read yet."""
    if not clubs:
        return None
    source = [{"n": first_n, "title": "Clubs & Affinity Groups", "url": config.CLUBS_URL}]
    when = f"{fetched_at:%a %b} {fetched_at.day}" if fetched_at else ""
    out = [f"[{first_n}] The Clubs & Affinity Groups page on sfhs.com (read {when}), which has these dropdowns: "
           + ", ".join(groups) + "."]
    if intro:
        out += ["", intro]
    out += ["", "Every club and group on the page, by dropdown (this list is complete):"]
    for g in groups:
        names = [f"{c['name']} {c['subtitle']}".strip() for c in clubs if c["group"] == g]
        out.append(f"  {g} ({len(names)}): " + "; ".join(names))
    out += ["", "Full details for the clubs most related to the question:"]
    used = 0
    for club in pick(question):
        detail = _detail(club)
        if used + len(detail) > DETAIL_CHARS:
            break
        out.append(detail)
        used += len(detail)
    return "\n".join(out), source


NOTES = (
    "The source is the Clubs & Affinity Groups page on sfhs.com. Its list of every club and group is complete: if a "
    "club isn't on it, say the page doesn't list it (don't guess that it exists). Say which dropdown a club is in (like "
    "Affinity Groups or Trial Clubs). Give the moderator (and their email, if listed) when it helps, like for joining "
    "or contacting a club. Some clubs have no description on the page; say so instead of making one up."
)
