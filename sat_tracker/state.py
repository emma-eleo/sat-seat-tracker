"""Tiny JSON-backed state so alerts aren't repeated every poll and restarts don't lose context."""
from __future__ import annotations

import json
import time
from pathlib import Path


class State:
    def __init__(self, path: str = "state.json") -> None:
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}
        self.data.setdefault("alerts", {})      # date_id -> {signature, alerted_at}
        self.data.setdefault("failures", {})    # date_id -> consecutive failed cycles
        self.data.setdefault("health_alerted", {})  # key -> bool

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    # --- availability alerts -------------------------------------------------
    def should_alert(self, date_id: str, signature: str, realert_minutes: int) -> bool:
        prev = self.data["alerts"].get(date_id)
        if prev and prev["signature"] == signature:
            return time.time() - prev["alerted_at"] >= realert_minutes * 60
        return True

    def mark_alerted(self, date_id: str, signature: str) -> None:
        self.data["alerts"][date_id] = {"signature": signature, "alerted_at": time.time()}
        self.save()

    def clear_availability(self, date_id: str) -> None:
        """Seats went back to 0, so a future reappearance should alert again."""
        if self.data["alerts"].pop(date_id, None) is not None:
            self.save()

    # --- health tracking -----------------------------------------------------
    def record_failure(self, key: str) -> int:
        self.data["failures"][key] = self.data["failures"].get(key, 0) + 1
        self.save()
        return self.data["failures"][key]

    def record_success(self, key: str) -> bool:
        """Reset failure count. Returns True if we were previously flagged unhealthy."""
        self.data["failures"][key] = 0
        was_flagged = self.data["health_alerted"].pop(key, False)
        self.save()
        return was_flagged

    def flag_health(self, key: str) -> bool:
        """Mark a health alert as sent. Returns False if it was already sent (don't spam)."""
        if self.data["health_alerted"].get(key):
            return False
        self.data["health_alerted"][key] = True
        self.save()
        return True
