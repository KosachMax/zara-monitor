from __future__ import annotations

from datetime import UTC, datetime

from .constants import DATA_FILE
from .utils import html_escape, now_iso


class HealthMonitor:
    def __init__(self, threshold: int) -> None:
        self.threshold = threshold
        self.started_at = datetime.now(UTC)
        self.state = "OK"
        self.consecutive_failed_cycles = 0
        self.last_error: str | None = None
        self.last_success_at: str | None = None
        self.last_check_at: str | None = None

    def record_success(self) -> bool:
        was_degraded = self.state == "DEGRADED"
        self.state = "OK"
        self.consecutive_failed_cycles = 0
        self.last_success_at = now_iso()
        self.last_check_at = now_iso()
        self.last_error = None
        return was_degraded

    def record_failure(self, error: str) -> bool:
        self.consecutive_failed_cycles += 1
        self.last_error = error
        self.last_check_at = now_iso()
        return self.state != "DEGRADED" and self.consecutive_failed_cycles >= self.threshold

    def mark_degraded_alert_sent(self) -> None:
        self.state = "DEGRADED"

    def uptime_seconds(self) -> int:
        return int((datetime.now(UTC) - self.started_at).total_seconds())

    def status_text(self, total_items: int, interval: int) -> str:
        uptime = self.uptime_seconds()
        lines = [
            "<b>Zara Monitor status</b>",
            f"State: <b>{html_escape(self.state)}</b>",
            f"Uptime: <b>{uptime}s</b>",
            f"Tracked subscriptions: <b>{total_items}</b>",
            f"Check interval: <b>{interval}s</b>",
            f"Consecutive failed cycles: <b>{self.consecutive_failed_cycles}</b>",
            f"Last successful cycle: <b>{html_escape(self.last_success_at or 'never')}</b>",
            f"Last check: <b>{html_escape(self.last_check_at or 'never')}</b>",
            f"Storage: <code>{html_escape(DATA_FILE)}</code>",
        ]
        if self.last_error:
            lines.append(f"Last error: <code>{html_escape(self.last_error)}</code>")
        return "\n".join(lines)
