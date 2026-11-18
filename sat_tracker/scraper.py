"""Browser automation and result parsing for the SAT test-center search page.

Design notes
------------
* The search form is JavaScript-rendered, so we drive a real browser (Playwright).
* The form's exact DOM is configured in config.yaml (`steps`), not hard-coded, so
  you can re-calibrate without touching Python if College Board changes the page.
* "Could not read the counter" is treated as an ERROR, never as "0 seats". Silently
  reporting 0 when the page changed is the worst failure mode for a tracker.
* If a captcha / "access denied" page appears we stop and back off. We do not try
  to defeat bot protection.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

log = logging.getLogger("sat_tracker.scraper")
DEBUG_DIR = Path("debug")

TRAFFIC = {"bytes": 0}          # network bytes received since last reset (see main.run_forever)
SESSION_DONE: set[int] = set()  # indexes of `once_per_session` steps already done in this browser session

BLOCK_MARKERS = re.compile(
    r"access denied|pardon our interruption|verify you are (a )?human|just a moment|"
    r"unusual traffic|request blocked|attention required",
    re.I,
)


class BlockedError(Exception):
    """Bot protection / rate limiting detected. Back off; do not retry immediately."""


class ScrapeError(Exception):
    """Page loaded but we couldn't drive it or read it (timeouts, changed layout, ...)."""


@dataclass
class Center:
    name: str
    details: str = ""


@dataclass
class DateResult:
    date_id: str
    date_label: str
    seats_count: int
    centers: list[Center] = field(default_factory=list)
    matched_text: str = ""


# --------------------------------------------------------------------------- helpers
def _alts(value: str) -> re.Pattern:
    """'A|B|C' -> case-insensitive regex matching any literal alternative."""
    parts = [re.escape(p.strip()) for p in value.split("|") if p.strip()]
    if not parts:
        raise ScrapeError("A step has an empty value/name; check config.yaml")
    return re.compile("|".join(parts), re.I)


def _locate(page, spec: dict, fmt: dict):
    def f(key: str) -> str:
        return spec[key].format(**fmt)

    if "label" in spec:
        return page.get_by_label(_alts(f("label"))).first
    if "placeholder" in spec:
        return page.get_by_placeholder(_alts(f("placeholder"))).first
    if "role" in spec:
        return page.get_by_role(spec["role"], name=_alts(f("name"))).first
    if "text" in spec:
        return page.get_by_text(_alts(f("text"))).first
    if "css" in spec:
        return page.locator(f("css")).first
    raise ScrapeError(f"Step has no locator (label/placeholder/role/text/css): {spec}")


def _select_native(loc, wanted: re.Pattern) -> None:
    # College Board hides the real <select> behind a styled widget (class "cb-replaced"),
    # so we only require it to exist in the DOM and set it with force=True.
    opts = loc.locator("option").evaluate_all(
        "els => els.map(e => ({v: e.value, t: e.textContent.trim()}))"
    )
    for o in opts:
        if wanted.search(o["t"]):
            loc.select_option(value=o["v"], force=True)
            return
    raise ScrapeError(
        f"No <option> matching {wanted.pattern!r}. Options seen: {[o['t'] for o in opts][:20]}"
    )


def _settle(page) -> None:
    """Wait for network quiet (best effort) plus a short human-ish pause."""
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    page.wait_for_timeout(random.randint(1200, 2200))


def _save_debug(page, tag: str) -> None:
    """Screenshot + page text on failure, so you can see what the script saw."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        page.screenshot(path=str(DEBUG_DIR / f"{tag}-{stamp}.png"), full_page=True)
        (DEBUG_DIR / f"{tag}-{stamp}.txt").write_text(page.inner_text("body"), encoding="utf-8")
        log.info("Saved debug files to %s/%s-%s.*", DEBUG_DIR, tag, stamp)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- steps
def _run_step(page, step: dict, fmt: dict, timeout: int) -> None:
    action = step["action"]

    if action == "wait":
        page.wait_for_timeout(int(step.get("ms", 1000)))
        return
    if action == "wait_text":
        page.get_by_text(_alts(step["text"].format(**fmt))).first.wait_for(
            timeout=int(step.get("timeout_ms", timeout))
        )
        return

    loc = _locate(page, step, fmt)
    value = step.get("value", "").format(**fmt)

    if action == "click":
        loc.click(timeout=timeout)
    elif action == "fill":
        loc.fill(value, timeout=timeout)
    elif action == "select":
        loc.wait_for(state="attached", timeout=timeout)  # attached, NOT visible (see _select_native)
        _select_native(loc, _alts(value))
    elif action == "choose":
        loc.click(timeout=timeout)
        if step.get("type"):
            page.keyboard.type(step["type"].format(**fmt), delay=random.randint(40, 90))
        opt = page.get_by_role("option", name=_alts(value)).first
        try:
            opt.click(timeout=5000)
        except PWTimeout:  # not an ARIA listbox; fall back to any visible text
            page.get_by_text(_alts(value)).first.click(timeout=timeout)
    else:
        raise ScrapeError(f"Unknown step action {action!r}")

    page.wait_for_timeout(random.randint(300, 900))


def run_steps(page, steps: list[dict], fmt: dict, timeout: int) -> None:
    for i, step in enumerate(steps, 1):
        optional = bool(step.get("optional"))
        once = bool(step.get("once_per_session"))
        if once and i in SESSION_DONE:
            continue  # e.g. cookie consent: already handled earlier in this browser session
        log.debug("step %d%s: %s", i, " (optional)" if optional else "", step)
        try:
            _run_step(page, step, fmt, int(step.get("timeout_ms", 3000)) if optional else timeout)
            if once:
                SESSION_DONE.add(i)
        except (PWTimeout, ScrapeError) as exc:
            if not optional:
                raise ScrapeError(f"Step {i} ({step.get('action')}) failed: {str(exc).splitlines()[0]}") from exc
            log.debug("optional step %d skipped: %s", i, str(exc).splitlines()[0])


# --------------------------------------------------------------------------- parsing
def parse_text_for_count(body: str, results_cfg: dict) -> tuple[int, str]:
    """Pure function (easy to unit-test): page text -> (seat count, matched snippet)."""
    for pat in results_cfg["count_regexes"]:
        m = re.search(pat, body, re.I | re.S)
        if m:
            return int(m.group(1)), " ".join(m.group(0).split())
    for pat in results_cfg.get("zero_regexes", []):
        if re.search(pat, body, re.I):
            return 0, f"zero-pattern: {pat}"
    raise ScrapeError("Could not find the 'test centers with available seats' counter on the page.")


CARD_JS = """els => els.map(e => {
  const t = sel => { const n = e.querySelector(sel); return n ? n.innerText.trim() : ''; };
  return {name: t('.cb-card-title'), code: t('.test-center-code'),
          address: t('address'), status: t('.seat-availability')};
})"""

NEXT_PAGE = "a[id$='_next']"  # the pager's "Go to next page" link


def centers_from_cards(cards: list[dict], unavailable_regex: str | None, seen: set | None = None) -> list[Center]:
    """Pure function (unit-testable): raw card dicts -> centers that are NOT marked unavailable."""
    skip = re.compile(unavailable_regex or r"(?!)", re.I)  # (?!) never matches
    seen = set() if seen is None else seen
    out: list[Center] = []
    for card in cards:
        key = card["code"] or card["name"]
        if key in seen:
            continue
        seen.add(key)
        if skip.search(card["status"]):
            continue
        lines = [ln.strip() for ln in card["address"].splitlines() if ln.strip()]
        if lines and lines[0].lower() == card["name"].lower():
            lines = lines[1:]  # the address link repeats the center name
        details = " | ".join(x for x in (card["code"], ", ".join(lines), card["status"]) if x)
        out.append(Center(name=card["name"], details=details))
    return out


def collect_centers(page, res_cfg: dict) -> list[Center]:
    """Read result cards, following the pager (the site shows 10 per page)."""
    seen: set = set()
    centers: list[Center] = []
    for _ in range(int(res_cfg.get("max_pages", 5))):
        cards = page.locator(res_cfg["center_card_selector"]).evaluate_all(CARD_JS)
        centers += centers_from_cards(cards, res_cfg.get("unavailable_regex"), seen)
        nxt = page.locator(NEXT_PAGE).first
        if nxt.count() == 0 or "cb-disabled" in (nxt.get_attribute("class") or "") \
                or nxt.get_attribute("aria-disabled") == "true":
            break
        try:
            nxt.click(timeout=5000)
        except PWError as exc:
            log.warning("Could not go to the next results page: %s", str(exc).splitlines()[0])
            break
        page.wait_for_timeout(random.randint(800, 1500))
    return centers


def parse_results(page, cfg: dict, date: dict) -> DateResult:
    res_cfg = cfg["results"]
    body = page.inner_text("body")

    if BLOCK_MARKERS.search(page.title()) or (BLOCK_MARKERS.search(body) and len(body) < 2500):
        raise BlockedError("Bot-protection or access-denied page detected.")

    count, matched = parse_text_for_count(body, res_cfg)
    result = DateResult(date["id"], date["label"].split("|")[0], count, matched_text=matched)

    if res_cfg.get("center_card_selector") and count > 0:
        result.centers = collect_centers(page, res_cfg)
    return result


# --------------------------------------------------------------------------- one check
def check_date(context, cfg: dict, date: dict) -> DateResult:
    poll = cfg["poll"]
    timeout = poll["page_timeout_ms"]
    fmt = {
        "date_id": date["id"],
        "date_label": date["label"],
        "country": cfg["country"],
        "region": cfg["region"],
    }
    page = context.new_page()
    page.set_default_timeout(timeout)
    try:
        resp = page.goto(cfg["url"], wait_until="domcontentloaded")
        if resp is not None and resp.status in (401, 403, 429):
            raise BlockedError(f"HTTP {resp.status} from the site")
        _settle(page)
        run_steps(page, cfg["steps"], fmt, timeout)
        _settle(page)
        return parse_results(page, cfg, date)
    except BlockedError:
        _save_debug(page, f"blocked-{date['id']}")
        raise
    except ScrapeError:
        _save_debug(page, f"scrape-{date['id']}")
        raise
    except PWTimeout as exc:
        _save_debug(page, f"timeout-{date['id']}")
        raise ScrapeError(f"Timed out: {str(exc).splitlines()[0]}") from exc
    except PWError as exc:
        _save_debug(page, f"browser-{date['id']}")
        raise ScrapeError(f"Browser error: {str(exc).splitlines()[0]}") from exc
    finally:
        page.close()


def check_with_retries(context, cfg: dict, date: dict) -> DateResult:
    poll = cfg["poll"]
    last: Exception | None = None
    for attempt in range(1, poll["max_retries"] + 1):
        try:
            return check_date(context, cfg, date)
        except BlockedError:
            raise  # never hammer a site that just blocked us
        except ScrapeError as exc:
            last = exc
            log.warning("[%s] attempt %d/%d failed: %s", date["id"], attempt, poll["max_retries"], exc)
            if attempt < poll["max_retries"]:
                time.sleep(poll["retry_backoff_seconds"] * attempt * random.uniform(0.8, 1.3))
    raise ScrapeError(f"{date['id']}: all {poll['max_retries']} attempts failed. Last error: {last}")


# --------------------------------------------------------------------------- browser
def _install_traffic_controls(ctx, b: dict) -> None:
    """Skip heavy resources the seat counter doesn't need, and count bytes received."""
    blocked = set(b.get("block_resources", ["image", "media", "font"]))
    if blocked:
        ctx.route(
            "**/*",
            lambda route: route.abort() if route.request.resource_type in blocked else route.continue_(),
        )

    def count(req) -> None:
        try:
            sz = req.sizes()
            TRAFFIC["bytes"] += sz["responseBodySize"] + sz["responseHeadersSize"]
        except Exception:  # noqa: BLE001 - stats only, never break a check
            pass

    ctx.on("requestfinished", count)


@contextmanager
def browser_context(cfg: dict, headed: bool = False):
    """One browser session. Reuse it across checks so cached scripts/styles aren't re-downloaded."""
    b = cfg["browser"]
    SESSION_DONE.clear()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False if headed else b.get("headless", True),
            channel=b.get("channel") or None,
        )
        ctx = browser.new_context(
            locale=b.get("locale", "en-US"),
            timezone_id=b.get("timezone") or None,  # None = this computer's timezone
            viewport={"width": 1366, "height": 850},
        )
        _install_traffic_controls(ctx, b)
        try:
            yield ctx
        finally:
            for closer in (ctx.close, browser.close):
                try:
                    closer()
                except Exception:  # noqa: BLE001 - window may already be closed by the user
                    pass


# --------------------------------------------------------------------------- discovery
def discover(cfg: dict) -> None:
    """Open a visible browser and record every XHR/fetch call while YOU run a search by hand.

    Output goes to ./discovery/ :
      network_log.jsonl   one line per XHR/fetch: url, method, status, POST body,
                          request header NAMES (never values), and the saved-body file
      responses/NNN.json  the FULL body of every JSON response
      page.png / page.txt / page.html   the final results page
    Use it to find the JSON endpoint behind the search so it can be polled directly.
    """
    out = Path("discovery")
    (out / "responses").mkdir(parents=True, exist_ok=True)
    log_path = out / "network_log.jsonl"
    log_path.write_text("", encoding="utf-8")
    counter = {"n": 0}

    def on_response(resp) -> None:
        try:
            req = resp.request
            if req.resource_type not in ("xhr", "fetch"):
                return
            ctype = resp.headers.get("content-type", "")
            body_file = ""
            if "json" in ctype:
                counter["n"] += 1
                body_file = f"responses/{counter['n']:03d}.json"
                (out / body_file).write_text(resp.text()[:2_000_000], encoding="utf-8")
            entry = {
                "url": resp.url,
                "method": req.method,
                "status": resp.status,
                "content_type": ctype,
                "post_data": (req.post_data or "")[:1000],
                "request_header_names": sorted(req.headers.keys()),  # names only, no secrets
                "body_file": body_file,
            }
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 - never let logging break the page
            pass

    with browser_context(cfg, headed=True) as ctx:
        page = ctx.new_page()
        page.on("response", on_response)
        page.goto(cfg["url"])
        print(
            "\nA browser window is open. Do the search by hand "
            f"({cfg['region']} / {cfg['country']} / one of your dates).\n"
            "When the results are visible, come back here and press Enter..."
        )
        input()
        page.screenshot(path=str(out / "page.png"), full_page=True)
        (out / "page.txt").write_text(page.inner_text("body"), encoding="utf-8")
        (out / "page.html").write_text(page.content(), encoding="utf-8")
    print(f"Saved to {out.resolve()}")
