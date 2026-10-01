"""Optional external heartbeats for processes whose failure can stop internal monitoring.

Configure a separate Healthchecks-style HTTPS URL in WORKER_HEARTBEAT_URL,
SCHEDULER_HEARTBEAT_URL, or BACKUP_HEARTBEAT_URL. URLs are treated as credentials and never
included in logs. These pings contain no application data.
"""

from __future__ import annotations

import logging
import os
import threading
from urllib.parse import urlsplit, urlunsplit

import httpx

log = logging.getLogger("africasignal.ops_heartbeat")


def _endpoint(base_url: str, state: str) -> str:
    parts = urlsplit(base_url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise ValueError(
            "monitor heartbeat URL must be an HTTPS URL without credentials or fragment"
        )
    path = parts.path.rstrip("/")
    if state:
        path = f"{path}/{state}"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


class ProcessHeartbeat:
    """Send a start ping and periodic successes so external monitoring sees process death."""

    def __init__(
        self,
        variable: str,
        *,
        interval_seconds: float = 60.0,
        url: str | None = None,
    ) -> None:
        self.variable = variable
        self.url = (os.environ.get(variable, "") if url is None else url).strip()
        self.interval_seconds = interval_seconds
        if self.url:
            _endpoint(self.url, "")  # fail startup on an insecure or malformed monitor URL
        self._stop = threading.Event()
        self._healthy = threading.Event()
        self._healthy.set()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def ping(self, state: str = "") -> bool:
        if not self.enabled:
            return True
        if state not in ("", "start", "fail"):
            raise ValueError("heartbeat state must be start, fail, or empty")
        if state == "fail":
            self._healthy.clear()
        elif not self._healthy.is_set():
            return False
        try:
            response = httpx.get(
                _endpoint(self.url, state),
                timeout=3.0,
                trust_env=False,
                follow_redirects=False,
            )
        except Exception as exc:
            log.warning("external %s heartbeat failed (%s)", self.variable, type(exc).__name__)
            return False
        if not 200 <= response.status_code < 300:
            log.warning(
                "external %s heartbeat returned HTTP %d", self.variable, response.status_code
            )
            return False
        return True

    def mark_healthy(self) -> bool:
        """Resume success pings after the monitored process has completed a healthy work cycle."""
        self._healthy.set()
        return self.ping()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.ping()

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self.ping("start")
        self._thread = threading.Thread(
            target=self._run, name=f"heartbeat-{self.variable.lower()}", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=4.0)
        self._thread = None
