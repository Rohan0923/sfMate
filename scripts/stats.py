"""How many people use sfMate: accounts, new sign-ups, and messages. By default it prints only counts.
In Render: open the sfmate service -> Shell, then run

    python -m scripts.stats            counts only
    python -m scripts.stats --users    also lists each account: name, email, when they joined, how much they use it

(Locally it reads whatever DATABASE_URL points at.) The App Store review account isn't counted. The list is
student data: keep it to yourself. It never shows what anyone asked."""
import argparse
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from app import auth, config, db

SCHOOL_TZ = ZoneInfo(config.SCHOOL_TIMEZONE)


def _day(ts):
    return datetime.fromtimestamp(ts, SCHOOL_TZ).date()


def main(show_users=False):
    today = datetime.now(SCHOOL_TZ).date()
    review = config.REVIEW_USERNAME
    with db.engine.connect() as conn:
        users = [r for r in conn.execute(sa.select(db.users.c.username, db.users.c.email, db.users.c.name,
                                                    db.users.c.created_at))
                 if r.username != review]
        consented = {r.username for r in conn.execute(sa.select(db.ai_consent.c.username))}
        usage = [r for r in conn.execute(sa.select(db.usage.c.username, db.usage.c.day, db.usage.c.count))
                 if r.username != review]

    school = [u for u in users if auth.is_school_email(u.email)]
    joined = [_day(u.created_at) for u in users]
    print(f"Accounts: {len(users)}   ({len(school)} @{config.SCHOOL_EMAIL_DOMAIN}, {len(users) - len(school)} other)")
    print(f"Agreed to the AI permission: {sum(u.username in consented for u in users)}")
    print()
    print(f"New accounts   today: {sum(d == today for d in joined)}   last 7 days: "
          f"{sum(d > today - timedelta(7) for d in joined)}   last 30 days: {sum(d > today - timedelta(30) for d in joined)}")

    per_day = Counter(d.isoformat() for d in joined)
    print("\nSign-ups per day (last 14 days):")
    for i in range(13, -1, -1):
        d = (today - timedelta(i)).isoformat()
        print(f"  {d}  {per_day.get(d, 0):>4}  {'#' * min(per_day.get(d, 0), 60)}")

    def active_since(days):
        cutoff = (today - timedelta(days - 1)).isoformat()
        rows = [r for r in usage if r.day >= cutoff and r.count > 0]
        return len({r.username for r in rows}), sum(r.count for r in rows)

    print("\nPeople who sent at least one message, and messages sent:")
    for label, days in (("today", 1), ("last 7 days", 7), ("last 30 days", 30)):
        people, messages = active_since(days)
        print(f"  {label:<13}{people:>5} people   {messages:>6} messages")
    if not usage:
        print("  (no messages recorded yet)")

    if show_users:
        sent = Counter()
        for r in usage:
            sent[r.username] += r.count
        print(f"\nAccounts, newest first ({len(users)}):")
        print(f"  {'joined':<17}{'type':<7}{'AI ok':<7}{'msgs':>5}  {'username':<20}{'name':<22}email")
        for u in sorted(users, key=lambda u: u.created_at, reverse=True):
            joined_at = datetime.fromtimestamp(u.created_at, SCHOOL_TZ).strftime("%b %d %I:%M %p")
            kind = "school" if auth.is_school_email(u.email) else "other"
            ok = "yes" if u.username in consented else "no"
            print(f"  {joined_at:<17}{kind:<7}{ok:<7}{sent[u.username]:>5}  {u.username[:19]:<20}{u.name[:21]:<22}{u.email}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="sfMate usage numbers (counts only unless --users)")
    parser.add_argument("--users", action="store_true", help="also list each account's name, email, and usage")
    main(parser.parse_args().users)
