"""Everything the model is told: the persona, the answering rules, and the student's profile."""
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config, rag

SFMATE_PERSONA = ("You are sfMate, high school general advisor. You are to talk like a teenager: "
                  "with slang but still respectful and with nothing too corny. "
                  "Never call the student dude, bro, bruh, man, or any other gendered nickname; "
                  "use their first name or no nickname at all.")

GRADE_NAMES = {9: "freshman", 10: "sophomore", 11: "junior", 12: "senior"}


def school_now():
    return datetime.now(ZoneInfo(config.SCHOOL_TIMEZONE))


def student_facts(profile):
    """What the student told us on the setup page, as short phrases ([] if skipped or blank)."""
    if not profile or profile.get("skipped"):
        return []
    facts = []
    if profile.get("first_name"):    facts.append(f"first name {profile['first_name']}")
    if profile.get("grade"):         facts.append(f"grade {profile['grade']} ({GRADE_NAMES[profile['grade']]})")
    if profile.get("hobbies"):       facts.append(f"hobbies and extracurriculars: {profile['hobbies']}")
    if profile.get("favorite_food"): facts.append(f"favorite food: {profile['favorite_food']}")
    return facts


def student_blurb(profile):
    facts = student_facts(profile)
    if not facts:
        return ""
    return ("\n\nAbout the student you're talking to: " + "; ".join(facts) + ". Use this to personalize answers "
            "(their grade's requirements and deadlines, clubs or sports that fit their interests), "
            "but don't recite it back unprompted.")


def rag_messages(question, history, profile, k=5):
    """System prompt + prior turns + retrieved context. Returns (messages, hits)."""
    prev_user = [m["content"] for m in history if m["role"] == "user"][-1:]   # helps follow-ups like "what about juniors?"
    hits = rag.retrieve(" ".join(prev_user + [question]), k)
    context = "\n\n".join(
        f"[{i}] {c['title']}" + (f" ({c['url']})" if c["url"] else "") + f"\n{c['text']}" for i, (c, _) in enumerate(hits, 1)
    )
    today = school_now()
    system = (
        SFMATE_PERSONA + "\n\n"
        f"Today is {today:%A, %B} {today.day}, {today.year}. "
        "Answer questions about Saint Francis High School using ONLY the context below, which comes from "
        "sfhs.com, the official Student-Parent Handbook, the 2026-27 Course Scheduling Guide, and recent Daily "
        "Announcements emails. For rules and policies, the Handbook is the authority; for courses, prerequisites, "
        "and graduation requirements, the Course Scheduling Guide is. Each Daily Announcements email is dated: "
        "'today' or 'tomorrow' inside one is relative to that email's date, not necessarily to today. "
        "Cite sources inline like [1]. If the context doesn't contain the answer, say you don't know "
        "and suggest checking sfhs.com or asking the Deans' office."
        + student_blurb(profile)
    )
    messages = [{"role": "system", "content": system}, *history,
                {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}]
    return messages, hits


# ---------- welcome message (chat welcome screen) ----------
WELCOME_INSTRUCTIONS = (
    "Write a welcome message for a Saint Francis High School student who just opened sfMate. "
    "1-2 sentences, under 40 words. Work in their grade, one of their hobbies or extracurriculars, and/or their "
    "favorite food naturally (not all of them, don't list them), then invite them to ask you anything about school. "
    "Plain text only: no lists, no hashtags, no quotation marks, no sign-off. "
    "Don't make up facts about the school, its clubs, teams, or events."
)


def welcome_messages(profile):
    return [{"role": "system", "content": SFMATE_PERSONA + "\n\n" + WELCOME_INSTRUCTIONS},
            {"role": "user", "content": "About the student: " + "; ".join(student_facts(profile))}]


def profile_key(profile):
    return json.dumps(profile, sort_keys=True)


def tidy_welcome(text):
    text = " ".join(text.split()).strip().strip('"“”').strip()
    return " ".join(re.split(r"(?<=[.!?])\s+", text)[:2])   # the model sometimes runs long; keep two sentences


# ---------- Daily Announcements summary ----------
ANNOUNCE_PROMPT = "Daily announcements are out. Want me to summarize?"
SUMMARY_INSTRUCTIONS = (
    "Summarize these Daily Announcements for a Saint Francis High School student. Use a short bulleted list "
    "(about 4-8 bullets) of what actually matters: events, deadlines, sign-ups, schedule changes, and who they're for, "
    "with dates, times, and places. Skip greetings, sign-offs, and boilerplate. Don't add anything that isn't in the "
    "email. Under 160 words."
)


def summary_messages(announcement):
    return [{"role": "system", "content": SFMATE_PERSONA + "\n\n" + SUMMARY_INSTRUCTIONS},
            {"role": "user", "content": f"Daily Announcements ({announcement['label']}):\n\n{announcement['text'][:8000]}"}]
