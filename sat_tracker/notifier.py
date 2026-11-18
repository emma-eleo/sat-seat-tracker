"""Notification channels. Each channel is enabled simply by setting its env vars in .env."""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("sat_tracker.notifier")


def _telegram(title: str, text: str) -> bool:
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return False
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": f"{title}\n\n{text}"[:4000],  # Telegram hard limit is 4096
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    r.raise_for_status()
    return True


def _discord(title: str, text: str) -> bool:
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if not url:
        return False
    r = requests.post(url, json={"content": f"**{title}**\n{text}"[:1900]}, timeout=15)
    r.raise_for_status()
    return True


def _desktop(title: str, text: str) -> bool:
    if os.getenv("DESKTOP_NOTIFY", "").lower() not in ("1", "true", "yes"):
        return False
    from plyer import notification  # imported lazily: optional dependency

    notification.notify(title=title, message=text[:250], timeout=20)
    return True


CHANNELS = {"telegram": _telegram, "discord": _discord, "desktop": _desktop}


def notify(title: str, text: str) -> None:
    """Send to every configured channel. One channel failing never blocks the others."""
    print(f"\n{'=' * 60}\n{title}\n{text}\n{'=' * 60}\n", flush=True)  # console always
    sent = []
    for name, fn in CHANNELS.items():
        try:
            if fn(title, text):
                sent.append(name)
        except Exception as exc:  # noqa: BLE001 - alerting must be best-effort
            log.error("Notification via %s failed: %s", name, exc)
    if not sent:
        log.warning("No external notification channel is configured or working (console only).")
    else:
        log.info("Notification sent via: %s", ", ".join(sent))
