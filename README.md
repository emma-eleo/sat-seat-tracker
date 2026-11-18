# SAT Seat Alerts

A small, notify-only tool that watches the public SAT test-center search page and pings you
(Telegram, Discord, desktop, or console) when a test center shows available seats for the
test dates you choose.

> **Unofficial.** Not affiliated with or endorsed by College Board. It only reads the public search
> page and sends you a notification; **registration stays manual**. Automated access may be restricted by
> the site's terms of use, so read them and use this tool at your own risk and responsibility.

## Be a polite client

* Checks run every **10-15 minutes** by default, randomized. The program **refuses intervals under 5 minutes**.
  Please don't change that: the site is shared by a lot of students.
* One browser session is reused across checks, and images, video and fonts are not downloaded.
* If the site shows a captcha or "access denied" page, the tracker alerts you once and **backs off for an hour**.
  It does not try to get around bot protection, and neither should you. If you get blocked, wait longer between checks.

## How it works

1. Opens the search page in a real browser (Playwright), fills in the form steps from `config.yaml`,
   and reads the line `Test centers with available seats (N)`.
2. `N = 0`: logs "No seats currently available." `N > 0`: sends an alert listing the centers it can read.
3. A failed or unreadable page is treated as an **error**, never as "0 seats", and repeated failures
   send you a "tracker is failing" alert so it can't silently stop working.

## Setup

Requires Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1   Git Bash: source .venv/Scripts/activate
pip install -r requirements.txt
playwright install chromium

cp config.example.yaml config.yaml  # Windows cmd: copy config.example.yaml config.yaml
cp .env.example .env                # then add your notification secrets
```

Edit `config.yaml`: set `region`, `country` and `dates` (use dates shown in the site's date dropdown).

### Notifications (`.env`)

Fill in at least one channel. Never commit `.env` (it is git-ignored).

* **Telegram:** create a bot with @BotFather to get `TELEGRAM_BOT_TOKEN`, message your bot once, then open
  `https://api.telegram.org/bot<TOKEN>/getUpdates` to find `TELEGRAM_CHAT_ID`.
* **Discord:** channel settings -> Integrations -> Webhooks -> copy the URL into `DISCORD_WEBHOOK_URL`.
* **Desktop:** set `DESKTOP_NOTIFY=true`.

```bash
python -m sat_tracker test-notify
```

## Usage

```bash
python -m sat_tracker once --headed   # one check with a visible browser (use this first)
python -m sat_tracker run             # poll forever
python -m sat_tracker discover        # record the page's network calls to help fix selectors
python -m sat_tracker test-notify     # test alert channels
```

Logs: `logs/tracker.log`. On failures, screenshots and page text go to `debug/`. State (to avoid duplicate
alerts) is `state.json`. All of these are git-ignored.

Keep the computer awake while it runs; a sleeping PC doesn't poll.

## If the site changes

The form is driven by the `steps` in `config.yaml` and the counter by `results.count_regexes`. If College Board
changes the page, you'll get a "tracker is failing" alert. Run `python -m sat_tracker discover` or
`playwright codegen <url>` to see what changed and update those entries. Pull requests welcome.

## Privacy and security

* Secrets live only in `.env`. Check `git status` before every commit.
* The tool needs no College Board login and stores nothing about you beyond local logs and state.

## License

MIT, emma.
