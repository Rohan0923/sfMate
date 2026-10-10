"""The Misc path (and clubs, for now): find the sfhs.com page(s) that best match the question and read them whole,
fetched live so they're current (the saved copy from the index is the backup)."""
import logging
import re
from concurrent.futures import ThreadPoolExecutor

import requests
import trafilatura

from . import rag

log = logging.getLogger("sfmate.site")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; sfMate/1.0)"}
CLUB_PAGES = re.compile(r"club|activit|affinity|intramural|athlet|sport|leadership|announcements-", re.I)
PAGES = 2
PAGE_CHARS = 5500


def _is_page(chunk):
    return chunk["url"].startswith("https://www.sfhs.com") and ".pdf" not in chunk["url"].lower()


def _short_title(title):
    return title.split(" | ")[0].strip() or title


def best_pages(question, only=None):
    """[(url, title)] of the best-matching pages, best first."""
    keep = (lambda c: _is_page(c) and bool(only.search(c["url"] + " " + c["title"]))) if only else _is_page
    pages = {}
    for chunk, _ in rag.retrieve_where(question, 30, keep):
        pages.setdefault(chunk["url"], _short_title(chunk["title"]))
    return list(pages.items())[:PAGES]


def saved_text(url):
    return "\n".join(dict.fromkeys(c["text"] for c in rag.docs if c["url"] == url))


def live_text(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=6)
        r.raise_for_status()
        text = trafilatura.extract(r.text, include_tables=True, include_links=False) or ""
        if len(text) >= 200:
            return text
    except Exception as e:
        log.warning("couldn't fetch %s live (using the saved copy): %s", url, e)
    return saved_text(url)


def read_pages(question, only=None):
    """[(url, title, full page text)] for the best-matching pages."""
    pages = best_pages(question, only)
    with ThreadPoolExecutor(max_workers=PAGES) as pool:
        texts = list(pool.map(live_text, [url for url, _ in pages]))
    return [(url, title, text[:PAGE_CHARS]) for (url, title), text in zip(pages, texts)]
