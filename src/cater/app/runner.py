"""Background polling loop shared by the CLI and the local web server.

The loop never sends anything unless the effective settings permit live sending
and the recipient is inside the active allowlist. It also never re-sends a
message whose delivery is uncertain; those stay available for explicit admin
recheck/confirmation.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from .application import AppService, PollReport


@dataclass
class CycleReport:
    poll: dict[str, Any] | None = None
    sent: list[dict[str, Any]] = field(default_factory=list)
    retried: list[dict[str, Any]] = field(default_factory=list)
    maintenance: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "poll": self.poll,
            "sent": self.sent,
            "retried": self.retried,
            "maintenance": self.maintenance,
            "errors": self.errors,
        }


class PollingService:
    def __init__(
        self,
        service: AppService,
        *,
        interval_seconds: int | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.service = service
        self.interval_seconds = interval_seconds
        self._stop = stop_event or threading.Event()
        self._thread: threading.Thread | None = None
        self.last_report: CycleReport | None = None

    # ------------------------------------------------------------------
    def run_once(self) -> CycleReport:
        report = CycleReport()
        service = self.service
        if not service.config.mail.account:
            report.errors.append("mail.account is not configured; mailbox scan skipped")
        else:
            try:
                poll_result: PollReport = service.poll_once()
                report.poll = poll_result.as_dict()
            except Exception as exc:
                service.logger.exception("poll_cycle_failed", exc)
                report.errors.append(f"poll: {type(exc).__name__}: {exc}")
        try:
            report.sent = service.send_pending(limit=20, actor="poller")
        except Exception as exc:
            service.logger.exception("poll_send_pending_failed", exc)
            report.errors.append(f"send_pending: {type(exc).__name__}: {exc}")
        try:
            report.retried = service.retry_pending_tasks(limit=10, actor="poller")
        except Exception as exc:
            service.logger.exception("poll_retry_failed", exc)
            report.errors.append(f"retry_pending: {type(exc).__name__}: {exc}")
        try:
            report.maintenance = service.maintenance.maybe_daily()
        except Exception as exc:
            service.logger.exception("poll_maintenance_failed", exc)
            report.errors.append(f"maintenance: {type(exc).__name__}: {exc}")
        if report.poll or report.sent or report.retried or report.errors:
            service.logger.info(
                "poll_cycle_completed",
                poll=report.poll,
                sent_count=len(report.sent),
                retried_count=len(report.retried),
                errors=report.errors,
            )
        self.last_report = report
        return report

    def _interval(self) -> int:
        if self.interval_seconds is not None:
            return max(5, int(self.interval_seconds))
        return max(5, int(self.service.settings.current().poll_interval_seconds))

    def run_forever(self) -> None:
        self.service.logger.info(
            "polling_loop_started",
            interval_seconds=self._interval(),
            account_configured=bool(self.service.config.mail.account),
        )
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # last-resort guard; run_once handles stages
                self.service.logger.exception("polling_loop_unhandled", exc)
            self._stop.wait(self._interval())
        self.service.logger.info("polling_loop_stopped")

    # ------------------------------------------------------------------
    def start(self) -> threading.Thread:
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._thread = threading.Thread(
            target=self.run_forever, name="cater-poller", daemon=True
        )
        self._thread.start()
        return self._thread

    def stop(self, timeout: float = 15.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())