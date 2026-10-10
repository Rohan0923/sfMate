"""Shows exactly what sfMate reads from the Daily Announcements in its Gmail inbox (read-only), so you can see
whether an email arrives the way you expect. From the sfmate/ folder, with the same Gmail values Render uses:

    GMAIL_ADDRESS=... GMAIL_APP_PASSWORD=... .venv/bin/python -m scripts.check_announcements
"""
from app import announcements, config

if __name__ == "__main__":
    if not announcements.enabled():
        raise SystemExit("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD first (the same values as on Render).")
    emails = announcements.fetch()
    print(f"{len(emails)} Daily Announcements email(s) in the last {config.ANNOUNCE_KEEP_DAYS} days\n")
    for a in emails:
        found = [k for k, _, _ in announcements.split_sections(a["text"]) if k]
        print(f"=== {a['label']} {a['sent']:%I:%M %p}  from {a.get('sender')}  subject {a.get('subject')!r}")
        print(f"    {len(a['text'])} characters; sections found: {', '.join(found) or 'none (sent to the model whole)'}")
        print("    " + a["text"][:600].replace("\n", "\n    "))
        print()
    print("For each day, sfMate uses the email with the most text in it.")
