"""Builds sfMate's search index: crawls sfhs.com, splits the Student-Parent Handbook and the Course Scheduling Guide
into sections, embeds everything, and writes data/chunks.json + data/embeddings.npy (+ meta.json).

Run it from the sfmate/ folder whenever the school's site or PDFs change (or before a new school year), then commit
the data/ folder and push. Render redeploys with the new index.

    python -m scripts.build_index
"""
import html
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import numpy as np
import pymupdf
import requests
import trafilatura

from app import config
from app.rag import chunk_text

SFHS_SITEMAP = "https://www.sfhs.com/sitemap.xml"
HANDBOOK_URL = "https://www.sfhs.com/ams/SFWeb_DeansOffice_StudentParentHandbook202627_06172026-2134.pdf"
HANDBOOK_LABEL = "Student-Parent Handbook 2026-27"
GUIDE_URL = "https://www.sfhs.com/ams/SFWeb_Course_Scheduling_Guide_202627_Final-1887.pdf"
GUIDE_LABEL = "Course Scheduling Guide 2026-27"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; sfMate-RAG/1.0)"}
CACHE_DIR = config.ROOT / ".build-cache"   # downloaded PDFs + crawled pages (not committed)


# ---------- sfhs.com ----------
def fetch_page(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"skip {url}: {e}")
        return None
    text = trafilatura.extract(r.text, include_tables=True, favor_recall=True)
    if not text:
        return None
    m = re.search(r"<title>(.*?)</title>", r.text, re.S | re.I)
    title = html.unescape(m.group(1)).strip() if m else url
    return {"url": url, "title": title, "text": text}


def crawl_site(refresh):
    cache = CACHE_DIR / "sfhs_pages.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    sitemap = ET.fromstring(requests.get(SFHS_SITEMAP, headers=HEADERS, timeout=30).content)
    urls = sorted({loc.text.strip() for loc in sitemap.findall(".//sm:loc", ns)})
    print(f"crawling {len(urls)} pages from the sitemap…")
    with ThreadPoolExecutor(max_workers=4) as pool:   # keep it polite
        pages = [p for p in pool.map(fetch_page, urls) if p]
    cache.write_text(json.dumps(pages))
    return pages


def download(url, name, refresh):
    path = CACHE_DIR / name
    if refresh or not path.exists():
        r = requests.get(url, headers=HEADERS, timeout=60)
        r.raise_for_status()
        path.write_bytes(r.content)
    return path


# ---------- Student-Parent Handbook: split on its numbered sections (1.1 Administration ... 11.13) ----------
def split_handbook(path, label, url):
    text = "\n".join(p.get_text() for p in pymupdf.open(path)).replace(chr(0x200B), " ")   # strip zero-width spaces
    parts = re.split(r"(?m)^(\d+\.\d+)\.\s+(\S[^\n]*?)\s*$", text)   # [preamble, num, title, body, num, title, body, ...]
    return [{"url": url, "title": f"{label} §{num} {title}", "text": body.strip()}
            for num, title, body in zip(parts[1::3], parts[2::3], parts[3::3])]


# ---------- Course Scheduling Guide: one section per course (code + name), plus page-titled sections ----------
SMALL_WORDS = {"and", "of", "the", "to", "in", "for", "an", "on", "with", "or", "&"}   # not "a": "Computer Science A"
def course_title(caps):
    """'AP FRENCH LANGUAGE' -> 'AP French Language', keeping acronyms, roman numerals and short codes like (AP CSA)."""
    out = []
    for i, w in enumerate(caps.split()):
        core = w.strip("():,–-")
        if re.fullmatch(r"AP|CS|CSA|UC|CSU|VPA|US|USA|STEM|3D|2D|PE|[IVX]{1,4}", core) or (w.startswith("(") and len(core) <= 5):
            out.append(w)
        elif i and w.lower() in SMALL_WORDS:
            out.append(w.lower())
        elif w.startswith("("):
            out.append(w[:2] + w[2:].lower())
        else:
            out.append("/".join(p[:1] + p[1:].lower() for p in w.split("/")))
    return " ".join(out)

COURSE_NAME = re.compile(r"[A-Z0-9][A-Z0-9 &:,/'’().+\-–—]{2,}")
GUIDE_HEADING = re.compile(r"Section \d+\s*[-–]\s*.+|[A-Z][A-Za-z&,' ]+ Departments?|Academic Support")

def split_course_guide(path, label, url):
    docs, cur = [], None
    def flush():
        if cur and (cur["kind"] == "course" or len("\n".join(cur["lines"])) > 40):   # short course entries still count
            docs.append({"url": f"{url}#page={cur['pdf_page']}", "title": cur["title"], "text": "\n".join(cur["lines"]).strip()})
    def topic(pages, pdf_page, first_line):
        hint = f": {first_line}" if len(first_line) <= 60 and not first_line.startswith("•") else ""
        return {"title": f"{label} · p. {pages}{hint}", "lines": [], "kind": "topic", "pdf_page": pdf_page}

    for page in pymupdf.open(path):
        raw = page.get_text()
        if "Table of Contents" in raw:
            continue   # lists every heading; it would only confuse retrieval
        printed = re.findall(r"(?m)^(\d{1,3})(?: Scheduling Guide)? *$", raw)   # each PDF page is a 2-page spread: "40", "41"
        pages, pdf_page = "–".join(dict.fromkeys(printed[:2])) or str(page.number + 1), page.number + 1
        lines, bullet = [], False   # (text, had_tab): course codes always sit on a line with a tab
        for line in raw.splitlines():
            s = line.strip()
            if not s or ("\t" not in line and re.fullmatch(r"\d{1,3}( Scheduling Guide)?|Scheduling Guide", s)):
                continue   # blank, or running header / page number
            if s == "»":
                bullet = True
                continue
            lines.append((("• " if bullet else "") + " ".join(s.split()), "\t" in line))
            bullet = False
        if cur is None or cur["kind"] == "topic":   # a course in progress continues onto the next page
            flush()
            cur = None
        i = 0
        while i < len(lines):
            text, had_tab = lines[i]
            nxt = lines[i + 1][0] if i + 1 < len(lines) else ""
            if had_tab and re.fullmatch(r"\d{3}[A-Z]?", text) and COURSE_NAME.fullmatch(nxt):   # "713 ⇥" then "ENTREPRENEURSHIP"
                flush()
                cur = {"title": f"{label} · {text} {course_title(nxt)} (p. {pages})", "lines": [], "kind": "course", "pdf_page": pdf_page}
                i += 2
                continue
            if GUIDE_HEADING.fullmatch(text):   # department / section heading ends the previous course
                flush()
                cur = topic(pages, pdf_page, text)
            else:
                if cur is None:
                    cur = topic(pages, pdf_page, text)
                cur["lines"].append(text)
            i += 1
    flush()
    return docs


def main():
    refresh = "--refresh" in sys.argv   # re-crawl / re-download instead of using .build-cache
    CACHE_DIR.mkdir(exist_ok=True)
    config.DATA_DIR.mkdir(exist_ok=True)
    started = time.time()

    pages = crawl_site(refresh)
    handbook = split_handbook(download(HANDBOOK_URL, "handbook.pdf", refresh), HANDBOOK_LABEL, HANDBOOK_URL)
    guide = split_course_guide(download(GUIDE_URL, "course_guide.pdf", refresh), GUIDE_LABEL, GUIDE_URL)
    print(f"{len(pages)} sfhs.com pages, {len(handbook)} handbook sections, "
          f"{len(guide)} course guide sections ({sum('· p.' not in d['title'] for d in guide)} courses)")

    chunks, seen = [], set()
    for doc in pages + handbook + guide:
        for c in chunk_text(doc["text"]):
            if c not in seen:   # site-wide boilerplate repeats across pages
                seen.add(c)
                chunks.append({"url": doc["url"], "title": doc["title"], "text": c})

    from sentence_transformers import SentenceTransformer
    print(f"embedding {len(chunks)} chunks with {config.EMBED_MODEL_ID} (a few minutes on a laptop)…")
    model = SentenceTransformer(config.EMBED_MODEL_ID)
    embs = model.encode([f"{c['title']}\n{c['text']}" for c in chunks], batch_size=32,
                        normalize_embeddings=True, show_progress_bar=True).astype(np.float32)

    (config.DATA_DIR / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=False))
    np.save(config.DATA_DIR / "embeddings.npy", embs)
    (config.DATA_DIR / "meta.json").write_text(json.dumps({
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "embed_model": config.EMBED_MODEL_ID,
        "chunks": len(chunks),
        "sources": {"sfhs.com pages": len(pages), "handbook sections": len(handbook), "course guide sections": len(guide)},
    }, indent=2))
    print(f"wrote {config.DATA_DIR}/ in {time.time() - started:.0f}s. Commit data/ and push to deploy it.")


if __name__ == "__main__":
    main()
