# sfMate

The Saint Francis advisor chatbot, as a real web app. The website runs on **Render**, the model runs on a
**RunPod Serverless** GPU that sleeps when nobody's using it, and accounts live in **Postgres**.

```
 students ──► Render (web server, always on)
                ├─ pages: home, sign up / log in, setup, chat
                ├─ accounts (@sfhs.com + emailed code), profiles, daily limits ──► Postgres
                ├─ every question is classified, then answered from ONLY its source, drafted and checked:
                │    announcements → Daily Announcements emails (summaries follow each student's
                │                    Announcement Preferences, plus that day's lunch)
                │    calendar      → school calendars (Activities & Events, School Schedule; every 30 min)
                │    courses       → Course Scheduling Guide        rules → Student-Parent Handbook
                │    menu          → Lancer Cafe menu (live, every 2 hours)
                │    clubs / misc  → the best sfhs.com pages, read whole (live)
                ├─ reads Daily Announcements / sends sign-up codes ──► sfMate Gmail
                └─ asks the model ──► RunPod Serverless (vLLM, Qwen2.5-32B), scales to zero
```

Monthly cost target: under $80. Roughly $25 (Render web) + ~$6 (Postgres) + ~$4 (RunPod storage) + GPU time
(pay per second). The 40-messages-per-student-per-day limit and RunPod's prepaid credit keep the GPU part capped.

---

## One-time setup

Do these in order. Each one gives you a value that a later step needs.

### 1. sfMate Gmail: app password

You may have done this already for the announcements.

1. Sign in to the **sfMate** Gmail (not your school account).
2. **myaccount.google.com → Security → 2-Step Verification**: turn it on.
3. **myaccount.google.com/apppasswords**: name it `sfMate`, click **Create**, and copy the 16-letter password.

The same password lets sfMate read Daily Announcements and send sign-up codes.
**You'll need:** the Gmail address and the app password.

### 2. RunPod: the GPU endpoint

1. Create an account at runpod.io, then **Billing → add $25–50 of credit**. Leave auto-pay **off**. When the
   credit runs out the GPU simply stops, so this is your spending cap.
2. **Storage → Network Volume → New**: 50 GB, in a data center that has 48 GB GPUs. It costs about $3.50/month and
   means the model downloads once instead of on every wake-up.
3. **Serverless → New Endpoint → vLLM** (the quick-deploy template), with:

   | Setting | Value |
   |---|---|
   | Model | `Qwen/Qwen2.5-32B-Instruct-AWQ` |
   | Max model length | `8192` |
   | GPU | **48 GB** (L40S / A6000 / A40). Leave the bigger, pricier ones unchecked. |
   | Active (min) workers | `0` (scale to zero) |
   | Max workers | `1` |
   | Idle timeout | `120` seconds (stays warm 2 minutes after the last question) |
   | FlashBoot | on |
   | Execution timeout | `600` seconds |
   | Network volume | the one from step 2 |

   Field names in RunPod's form shift around now and then. If one isn't there, look for the closest match.
4. Open the endpoint and copy its **Endpoint ID**.
5. **Settings → API Keys → Create API key** and copy it.

**You'll need:** the Endpoint ID and the API key.

To check it works, open the endpoint's **Requests** tab and send a test request. The first one takes a few
minutes while the model downloads into the volume; later wake-ups are faster.

### 3. GitHub: put the code online

Create a **private** repository (for example `sfmate`) and push **this folder** to it, so that `render.yaml` is
at the top level of the repo. From this folder:

```
git init
git add .
git commit -m "sfMate web app"
git branch -M main
git remote add origin https://github.com/<you>/sfmate.git
git push -u origin main
```

`.env`, the virtual environment and caches are in `.gitignore`. Only code and the `data/` search index get pushed.

### 4. Render: the website and database

1. Sign up at render.com with your GitHub account.
2. **New + → Blueprint →** pick the `sfmate` repo. Render reads `render.yaml` and plans a web service (`sfmate`,
   2 GB) plus a Postgres database (`sfmate-db`).
3. It asks for the secret values: `RUNPOD_API_KEY`, `RUNPOD_ENDPOINT_ID`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`.
   Leave `GOOGLE_CLIENT_ID` blank for now.
4. Click **Apply**. The first build takes 5–10 minutes (mostly downloading PyTorch and the search model).
5. Open the URL Render gives you (like `https://sfmate.onrender.com`) and sign up with your @sfhs.com email.
   The code should arrive in a minute; check spam the first time.

**Test it on the school Wi-Fi.** It blocked ngrok before. If `onrender.com` is blocked too, add a custom domain
(Render → sfmate → Settings → Custom Domains; about $10–15/year from any registrar).

From now on, **every push to `main` redeploys automatically.**

---

## Everyday stuff

**Changing the code:** edit on your Mac, run the tests, push. Render redeploys in a few minutes.

**Running it on your Mac:**

```
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env          # fill in what you have; blank Gmail = codes print in the terminal
.venv/bin/uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000. Without a `DATABASE_URL` it uses a local SQLite file. Point `RUNPOD_*` at your
real endpoint to chat, or leave them blank to test everything except answers.

**Tests:** `.venv/bin/python -m pytest -q`. These cover accounts and codes, chat, limits, announcements, and the
model client against a fake RunPod.

**Refreshing what sfMate knows** (new school year, new handbook, site changes):
1. If the PDFs moved, update `HANDBOOK_URL` / `GUIDE_URL` (and their labels) in `scripts/build_index.py`.
2. Run `.venv/bin/python -m scripts.build_index --refresh` (about 3–5 minutes).
3. Commit `data/` and push.

Daily Announcements, the school calendars, and the cafe menu don't need this: they're read live.

**Settings** (Render → sfmate → Environment; saving restarts the server):

| Variable | Default | What it does |
|---|---|---|
| `DAILY_MESSAGE_LIMIT` | `40` | Questions per student per day. The main GPU-cost lever. |
| `ALLOWED_EMAIL_DOMAIN` | `sfhs.com` | Who can sign up |
| `LLM_MODEL` | `Qwen/Qwen2.5-32B-Instruct-AWQ` | Must match the RunPod endpoint's model |
| `ANNOUNCE_SENDERS` | `@sfhs.com` | Which senders count as real announcements (comma-separated) |
| `ANNOUNCE_HOURS` | `7-11` | When to check for new announcements (school time) |
| `GOOGLE_CLIENT_ID` | blank | Turns on "Continue with Google" (school accounts only) |
| `CALENDAR_ICS_URLS` | the two SFHS calendars | Public iCal (.ics) feeds, comma-separated. Set it to add or swap calendars |
| `MENU_URL` | `https://www.sfhs.com/lancer-cafe` | The cafe page the daily menu is read from; blank turns it off |

**Google sign-in (optional):** create an OAuth client ID ("Web application") in console.cloud.google.com, add your
site's address (for example `https://sfmate.onrender.com`) under **Authorized JavaScript origins**, and set
`GOOGLE_CLIENT_ID`. Only @sfhs.com Google accounts are accepted. Your school's Google admin may need to allow it.

---

## When something's off

- **Render → sfmate → Logs** shows everything: sign-ups, announcement checks, model errors.
- **"Waking up sfMate's brain…" for a long time:** the GPU is cold-starting. Normal for the first question after
  a break (1–3 minutes). If it never answers, check RunPod's endpoint page (are workers starting? any credit left?).
- **Codes not arriving:** check spam. The logs say "couldn't send code" if Gmail rejected the app password.
- **Which path a question took:** the logs say `question '…' -> calendar` (or rules, menu, …) for every message.
- **Announcement answers look wrong:** the logs list each new email ("announcement email Fri Oct 9 … characters, sections found: …") and each summary ("… sections, … characters sent"). To see exactly what sfMate reads from the inbox, run `GMAIL_ADDRESS=... GMAIL_APP_PASSWORD=... .venv/bin/python -m scripts.check_announcements`.
- **Announcements not showing:** the logs say "skipped an email from …" if the sender isn't in `ANNOUNCE_SENDERS`.
- **Calendar events missing:** the logs say "calendar N failed to refresh" if a feed is down; sfMate keeps the
  last copy it got and tries again in 30 minutes.
- **Menu questions say "no menu posted":** either the cafe hasn't posted that day yet, or the page changed shape. The
  logs say "menu refresh failed" in that case; sfMate keeps the last menu it read.
- **Keep the web service at 1 instance.** The announcements checker and login throttling live in memory.

## What's where

```
app/main.py           routes: pages, accounts, profile, welcome, chat, announcements
app/auth.py           sign-up with emailed codes, login, Google, sessions, throttling
app/llm.py            RunPod/vLLM client (streaming, cold-start retries)
app/rag.py            search index + query embedding
app/prompts.py        persona and every instruction the model gets
app/announcements.py  Gmail reader + schedule
app/calendars.py      school calendar feeds
app/router.py         every question: classify -> read only that source -> draft -> check -> answer
app/schedule.py       calendar path: exact dates, the next 14 days, scanning the rest of the year
app/site.py           Misc (and clubs for now): the best sfhs.com pages, read whole and live
app/dates.py          "next Friday", "this weekend", "Oct 12" -> exact dates
app/menu.py           Lancer Cafe menu: read live, answered by day
app/db.py             database tables
app/static/           the four pages
scripts/build_index.py  crawls the site + PDFs, builds data/
data/                 the search index (committed)
tests/                pytest suite
```

The old Colab notebook (`../model.ipynb`) still works as before, but this folder replaces it.
