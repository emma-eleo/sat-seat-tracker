"""CLI + polling loop.

Commands:
  python -m sat_tracker run            poll forever (10-15 min, jittered)
  python -m sat_tracker once           single check of every date, then exit
  python -m sat_tracker discover       manual recon session (see scraper.discover)
  python -m sat_tracker test-notify    verify Telegram / Discord / desktop alerts
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .notifier import notify
from .scraper import (
    TRAFFIC,
    BlockedError,
    DateResult,
    ScrapeError,
    browser_context,
    check_with_retries,
    discover,
)
from .state import State

log = logging.getLogger("sat_tracker")

# Politeness guard: this tool must not hammer someone else's website. Checking more often
# than this is refused. Please don't remove it; the site is shared by a lot of students.
MIN_POLL_MINUTES = 5


def validate_config(cfg: dict) -> None:
    poll = cfg["poll"]
    lo, hi = poll["min_minutes"], poll["max_minutes"]
    if lo < MIN_POLL_MINUTES:
        sys.exit(
            f"poll.min_minutes is {lo}, but the minimum allowed is {MIN_POLL_MINUTES}. "
            "Polling faster than that is refused to avoid overloading the site."
        )
    if hi < lo:
        sys.exit(f"poll.max_minutes ({hi}) must be >= poll.min_minutes ({lo}).")
    if not cfg.get("dates"):
        sys.exit("config.yaml has no `dates` to monitor.")


def setup_logging(verbose: bool) -> None:
    Path("logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logfile = RotatingFileHandler("logs/tracker.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    logfile.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(logfile)


# --------------------------------------------------------------------------- alert logic
def format_alert(cfg: dict, res: DateResult) -> tuple[str, str]:
    title = f"SAT seats available: {res.date_label} ({cfg['country']})"
    lines = [f"Test centers with available seats: {res.seats_count}"]
    if res.centers:
        lines.append("")
        lines += [f"- {c.name}" + (f"\n  {c.details}" if c.details else "") for c in res.centers]
    else:
        lines.append("(Center names could not be read. Open the link to see which ones have seats.)")
    lines += ["", f"Register now: {cfg['url']}", f"Checked: {time.strftime('%Y-%m-%d %H:%M:%S')}"]
    return title, "\n".join(lines)


def handle_success(cfg: dict, state: State, res: DateResult) -> None:
    if state.record_success(res.date_id):
        notify("SAT tracker recovered", f"Checks for {res.date_label} are working again.")

    if res.seats_count == 0:
        log.info("[%s] No seats currently available.", res.date_id)
        state.clear_availability(res.date_id)
        return

    log.warning("[%s] SEATS AVAILABLE: %d center(s)  (matched: %r)", res.date_id, res.seats_count, res.matched_text)
    for c in res.centers:
        log.warning("[%s]   %s | %s", res.date_id, c.name, c.details)

    signature = f"{res.seats_count}|" + ",".join(sorted(c.name for c in res.centers))
    if state.should_alert(res.date_id, signature, cfg["alerts"]["realert_minutes"]):
        notify(*format_alert(cfg, res))
        state.mark_alerted(res.date_id, signature)


def handle_failure(cfg: dict, state: State, date: dict, exc: Exception) -> None:
    n = state.record_failure(date["id"])
    log.error("[%s] cycle failed (%d in a row): %s", date["id"], n, exc)
    if n >= cfg["alerts"]["unhealthy_after_failures"] and state.flag_health(date["id"]):
        notify(
            "SAT tracker is failing",
            f"{n} consecutive failed checks for {date['id']}.\nLast error: {exc}\n"
            "Check logs/tracker.log and the debug/ folder. The page layout may have changed.",
        )


def handle_block(state: State, exc: Exception) -> None:
    log.error("Blocked by site protection: %s", exc)
    if state.flag_health("blocked"):
        notify("SAT tracker was blocked", f"{exc}\nBacking off. Raise the poll interval if this repeats.")


# --------------------------------------------------------------------------- loop
def run_cycle(cfg: dict, state: State, ctx) -> bool:
    """Check every date once in the given browser session. Returns True if blocked (back off)."""
    for i, date in enumerate(cfg["dates"]):
        if i:
            time.sleep(random.uniform(*cfg["poll"]["between_dates_seconds"]))
        try:
            res = check_with_retries(ctx, cfg, date)
        except BlockedError as exc:
            handle_block(state, exc)
            return True
        except ScrapeError as exc:
            handle_failure(cfg, state, date, exc)
            continue
        state.record_success("blocked")  # a good read means we're no longer blocked
        handle_success(cfg, state, res)
    return False


def run_forever(cfg: dict, state: State) -> None:
    poll = cfg["poll"]
    recycle = int(cfg["browser"].get("recycle_every_cycles", 24))
    log.info("Tracker started: %d date(s), every %s-%s min.", len(cfg["dates"]), poll["min_minutes"], poll["max_minutes"])
    while True:
        try:
            # One browser session spans many cycles (cache + consent reuse = far less data),
            # and is recycled periodically so a long-running browser can't leak memory.
            with browser_context(cfg) as ctx:
                for _ in range(recycle):
                    TRAFFIC["bytes"] = 0
                    blocked = run_cycle(cfg, state, ctx)
                    log.info("This cycle downloaded ~%.2f MB.", TRAFFIC["bytes"] / 1e6)
                    wait = (
                        poll["blocked_backoff_minutes"] * 60 * random.uniform(1.0, 1.25)
                        if blocked
                        else random.uniform(poll["min_minutes"], poll["max_minutes"]) * 60
                    )
                    log.info("Next check in %.1f min.", wait / 60)
                    time.sleep(wait)
        except KeyboardInterrupt:
            raise
        except Exception:  # noqa: BLE001 - keep the daemon alive no matter what
            log.exception("Browser session failed; starting a fresh one in 60 s")
            time.sleep(60)


# --------------------------------------------------------------------------- CLI
def main() -> None:
    ap = argparse.ArgumentParser(prog="sat_tracker", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "once", "discover", "test-notify"])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--headed", action="store_true", help="show the browser window (debugging)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    load_dotenv()
    setup_logging(args.verbose)
    cfg_path = Path(args.config)
    if not cfg_path.exists():
        sys.exit(f"{cfg_path} not found. Copy config.example.yaml to config.yaml and edit it.")
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    validate_config(cfg)

    if args.command == "test-notify":
        notify("SAT tracker test", "If you can read this, notifications work.")
    elif args.command == "discover":
        discover(cfg)
    elif args.command == "once":
        with browser_context(cfg, headed=args.headed) as ctx:
            run_cycle(cfg, State(), ctx)
            log.info("This check downloaded ~%.2f MB.", TRAFFIC["bytes"] / 1e6)
    else:
        try:
            run_forever(cfg, State())
        except KeyboardInterrupt:
            log.info("Stopped by user.")
