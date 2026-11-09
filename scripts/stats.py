"""How many people use sfMate: accounts, new sign-ups, and messages. Prints only counts: no names, emails, or questions.
In Render: open the sfmate service -> Shell, then run

    python -m scripts.stats

(Locally it reads whatever DATABASE_URL points at.) The App Store review account isn't counted."""
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from app import auth, config, db

SCHOOL_TZ = ZoneInfo(config.SCHOOL_TIMEZONE)


def _day(ts):
    return datetime.fromtimestamp(ts, SCHOOL_TZ).date()


def main():
    today = datetime.now(SCHOOL_TZ).date()
    review = config.REVIEW_USERNAME
    with db.engine.connect() as conn:
        users = [r for r in conn.execute(sa.select(db.users.c.username, db.users.c.email, db.users.c.created_at))
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


if __name__ == "__main__":
    main()
