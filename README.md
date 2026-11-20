# US visa slot tracker (passive, read-only)

A personal dashboard for OFC (biometrics) and consular interview availability on usvisascheduling.com (India).
It records what **your own browsing** of the portal loads, keeps a history in SQLite, shows it live, and alerts
you (Telegram / email) when a date you saw beats your target.

> **What it will never do:** open, refresh, poll, click, book, reschedule or cancel anything. It sends no request
> to the portal at all. It only reads network responses your own clicks produce, in a Chrome window you control.

## Why passive and not a poller

The original spec was a 15-minute headless poller. That was dropped because:

- **Terms of Use.** Reports say the portal's terms ban automated access, including tools that only *watch* for
  open dates. Applicants report "Access limitation" and "Sorry, you have been blocked" errors. In March 2025 the
  US Embassy in India cancelled about 2,000 bot-booked appointments.
- **View cap.** Applicants report a cap of about 12 appointment-page actions per day (page loads, calendar
  refreshes, consulate changes). It resets around midnight US Eastern, and continuing after the block reportedly
  risks a 72-hour account freeze. A 15-minute poll is 96 views a day.
- **Low yield.** Slots reportedly appear and vanish within minutes, so the few checks a day that would stay under
  the cap would rarely catch one.
- **High cost.** India allows one free reschedule, so a frozen or restricted account costs more than a missed slot.

These are reported behaviours, not published numbers. Treat them as approximate.

## How it works

```
Your Chrome (dedicated profile, DevTools on 127.0.0.1:9222)
   │  you log in and browse the reschedule page by hand
   ▼
session.py  ── CDP Network domain only, per tab, over a local websocket (no Runtime/Page, no injected JS)
   ▼
capture.py  ── discovery log (redacted) · view-budget counter · limit/login detection
   ▼
parser.py   ── days / time-slot JSON → dates + slots
   ▼
db.py (SQLite) ──► notifier.py (Telegram/SMTP, 6 h de-dup)
   ▼
app.py (FastAPI + SSE) ──► static/index.html + app.js (Chart.js, vendored)
```

## Setup

Python 3.11+ and Google Chrome are required.

```powershell
# Windows PowerShell
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

```bash
# macOS / Linux
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## First run

1. **Start the tracker's Chrome**

   ```
   python -m tracker chrome
   ```

   This opens Chrome with a dedicated profile (`.chrome-profile/`) and DevTools listening on 127.0.0.1 only.
   Chrome 136+ refuses remote debugging on your default profile, which is why the profile is dedicated.
   **Log in by hand**, including the security questions and CAPTCHA. The tracker never sees or stores your
   password. The session cookies live in that Chrome profile like any normal login.

2. **Start the dashboard**, in another terminal:

   ```
   python -m tracker
   ```

   Open http://localhost:8000. The header shows "Chrome · N portal tabs" once it has attached.

3. **Browse the portal normally** in that Chrome window: open the reschedule page, pick a location, click a date.
   Each response lands on the dashboard within a second.

To see the UI without Chrome, run `python -m tracker demo`. It uses fake data in `data/demo.db`.

## Discovery mode: mapping the real endpoints

The parser's defaults are **educated guesses**. They are tested only against a stand-in server, not the real
portal. Discovery mode (on by default) writes one line per portal XHR/fetch/page to `data/discovery.jsonl`:

```json
{"type":"Fetch","method":"POST","status":200,
 "url":".../schedule-group/get-family-ofc-schedule-days&cacheString=[REDACTED]",
 "classified_as":"OFC_DAYS",
 "request":"postId=XXXX&__RequestVerificationToken=[REDACTED]",
 "response":{"ScheduleDays":[{"ID":"<str len=36>","Date":"2026-12-15T00:00:00"}, "<+14 more>"]}}
```

The log redacts cookies, tokens, CSRF values, JWTs and query secrets. Response values are masked except dates,
times and location-ID fields. The file is created with mode 600, which has no effect on Windows; keep it out of
synced folders.

To map the real endpoints:

1. **Find the requests.** Browse the reschedule page once. Pick a location, then a date, for both OFC and
   interview. Use 3–4 of your daily views at most.
2. **Set the URL patterns.** In `discovery.jsonl`, find the requests whose responses contain date lists or time
   lists. If `classified_as` is `null` for them, set `PATTERN_OFC_DAYS`, `PATTERN_OFC_TIMES`,
   `PATTERN_INTERVIEW_DAYS` and `PATTERN_INTERVIEW_TIMES` in `.env` to a unique fragment of each URL.
3. **Set the location field.** Look at the `request` field to see which parameter carries the location. Set
   `LOCATION_KEY_PATTERN` if it isn't one of the defaults.
4. **Map location IDs to names.** Unmapped IDs show up on the dashboard as raw IDs. Add them to `LOCATION_MAP`,
   e.g. `LOCATION_MAP=5a1b...=Mumbai;7c2d...=New Delhi`.
5. **Fix the parser if needed.** If dates or times parse wrong, adjust the `TODO(discovery)` spots in
   `tracker/parser.py`, such as key names or the MM/DD vs DD/MM order. Add a redacted copy of the real response
   to `tests/fixtures/` with a test.
6. **Turn discovery off.** Set `DISCOVERY_MODE=false` once everything parses.

Also confirm the exact wording of the portal's limit and blocked messages and update `PAGE_WARNINGS` in
`parser.py` to match.

## Dashboard

- **Location chips:** choose which locations appear. Saved in SQLite.
- **Cards:** per location, the earliest OFC and interview date, open days in the next 90, last seen, and a badge
  (OK / NO SLOTS / NO DATA / LOGIN NEEDED / LIMIT EXCEEDED / BLOCKED …).
- **Heat-map:** the next 90 days. Shade shows the slot count when times were captured. Tap a date for its time slots.
- **Line chart:** earliest available date over time, per location.
- **Portal-view meter:** page loads plus calendar loads in the current US-Eastern day, against your budget
  (default 12). It turns amber at 60% and red at 85%, and a banner tells you to stop.
- **Settings:** target date ("alert only if before"), your booked OFC and interview dates, view budget, alerts on/off.

Everything updates live over SSE. "Data as of" is always the last time *you* looked.

## Alerts

An alert fires when a captured earliest date beats every date you've set (target and/or booked date for that
type). With nothing set, it fires when the date beats the previous best for that location. A date is not
re-alerted within 6 hours (`ALERT_DEDUPE_HOURS`). A failed send is not recorded, so it retries on the next
capture. Limit, block and rate-limit signals also alert, once per state per day.

To set up ntfy, which is the easiest way to get phone push: install the ntfy app, subscribe to a long random
topic (e.g. `visa-<20 random chars>`), and set `NTFY_TOPIC` to the same name. Topics on ntfy.sh are public
to anyone who knows the name, so the tracker treats the topic as a secret and redacts it from logs. Date alerts
go out as urgent priority, portal warnings as high.

To set up Telegram: create a bot with @BotFather, message it once, then get your chat ID from
`https://api.telegram.org/bot<TOKEN>/getUpdates`. Put both in `.env`. Email uses standard SMTP; for Gmail, use an
app password.

Since this is passive, alerts mostly matter when someone else browses in that window, or as a phone record of
what you saw.

## Portal states the dashboard recognises

| State | Seen as | What to do |
|---|---|---|
| LOGIN NEEDED | Redirect to `atlasauth.b2clogin.com`, or HTTP 401 | Sign in again in the tracker's Chrome window. The `/signin-aad-b2c_1` callback counts as *success*. |
| CHALLENGE | Cloudflare "Just a moment" / "Verify you are human" page | Clear it yourself. The tracker never touches it. |
| IN QUEUE | Virtual waiting-room page | Leave the tab alone. Reloading loses your place. |
| MAINTENANCE | "Under maintenance" page | Come back later. |
| LIMIT WARNING / LIMIT EXCEEDED | Daily view-cap messages | Stop for the day. You get an alert. |
| ACCESS LIMITED / BLOCKED / RATE LIMITED | Access limitation, block page, HTTP 429, Cloudflare 1015 | Stop. You get an alert. |
| ERROR | A calendar response contains dates the parser couldn't read | The format changed. Check `data/discovery.jsonl` and the parser TODOs. It is deliberately *not* recorded as NO SLOTS, so it can't hide behind an empty history. |

## Prior art

Several other open-source watchers informed the parser and the state list (ideas only, no code copied). The
most useful was [yalewinters/visa-slot-watcher](https://github.com/yalewinters/visa-slot-watcher) (CC BY-NC
4.0), whose field notes report the following:

- the API is a single `POST /en-US/custom-actions/?route=…` dispatcher, with arguments in a JSON `parameters` field;
- sign-in happens on Azure AD B2C;
- the portal shows queue, challenge and maintenance pages;
- responses should be parsed without trusting fixed field names.

[hamzamilano/usvisascheduling-bot](https://github.com/hamzamilano/usvisascheduling-bot) independently uses the same
`get-family-consular-schedule-days` route and `ScheduleDays` response. Both projects *drive* the portal on a
timer; this tool deliberately does not.

## Security notes

- **The DevTools port gives full control of that Chrome**, including its logged-in session, to any process on
  your machine. It is bound to 127.0.0.1, but still: close that Chrome when you're done and don't run untrusted
  software alongside it.
- **The dashboard binds to 127.0.0.1** and has no auth. Don't expose it.
- **Logs never contain secrets.** A redacting filter masks tokens, cookies and the Telegram/SMTP secrets.
- **Telemetry is never read.** Analytics and telemetry calls (`/v2/track`, `/cdn-cgi/`, Azure Monitor …) carry
  emails and session IDs, so the tracker ignores them.
- **The tracker stores no credentials.** Your login state is Chrome's own, in `.chrome-profile/`, which is git-ignored.

## Tests

```
python -m pytest -q
```

The tests cover the parser (fixtures under `tests/fixtures/`), redaction, alert rules and 6-hour de-dup, and the
capture pipeline (limit detection, login detection, discovery redaction).

## Project layout

```
tracker/
  __main__.py   CLI: run | chrome | demo
  app.py        FastAPI: GET /status /history /slots /settings, POST /settings, GET /events (SSE)
  session.py    passive CDP attachment to your Chrome (replaces Playwright)
  capture.py    pipeline, view budget, state, status payload (replaces poller.py)
  parser.py     response parsing + TODOs + warning detection
  notifier.py   Telegram/email, alert rules, de-dup
  db.py         SQLite: snapshots, time_slots, alerts, view_log, settings
  redact.py     log/discovery redaction
  static/       index.html, app.js, vendor/chart.umd.min.js (Chart.js 4.6.1, MIT)
```

Changes from the original spec: no Playwright, APScheduler, polling, quiet hours, `POST /check-now`, or saved
`storage_state` file. All of those only exist to support automated access.

## Read-only and terms

This tool is for personal use on your own account. It sends nothing to the portal. You are still responsible for
following the portal's terms and daily limits. If the portal shows any limit or block message, stop for the day.
