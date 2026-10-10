"""Everything the model is told: the persona, the answering rules, and the student's profile."""
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from . import announcements, config

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
    """What the welcome message depends on (changing announcement preferences doesn't rewrite it)."""
    return json.dumps({k: v for k, v in (profile or {}).items() if k != "announcements"}, sort_keys=True)


def announcement_prefs(profile):
    """The topics the student wants in announcement summaries (all of them unless they've chosen)."""
    chosen = (profile or {}).get("announcements")
    return [k for k in announcements.TOPIC_KEYS if k in chosen] if isinstance(chosen, list) else list(announcements.TOPIC_KEYS)


def tidy_welcome(text):
    text = " ".join(text.split()).strip().strip('"“”').strip()
    return " ".join(re.split(r"(?<=[.!?])\s+", text)[:2])   # the model sometimes runs long; keep two sentences


# ---------- Daily Announcements summary ----------
ANNOUNCE_PROMPT = "Daily announcements are out. Want me to summarize?"
