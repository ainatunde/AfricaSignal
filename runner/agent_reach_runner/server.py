"""HTTPS service boundary and bounded worker pool for Agent Reach searches."""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import signal
import ssl
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from agent_reach_runner.backend import BackendFailure, YouTubeSearchBackend
from agent_reach_runner.schemas import (
    PROTOCOL_VERSION,
    HealthResponse,
    TaskSubmission,
)
from agent_reach_runner.store import TaskConflict, TaskStore

log = logging.getLogger("africasignal.agent_reach_runner")
TASK_PATH = re.compile(r"^/v1/tasks/([0-9a-f-]{36})$")
TASK_LOCAL_PATH = re.compile(r"^/v1/tasks/by-task/([0-9]{1,20})$")


def _secret_path(path_env: str) -> str:
    path = os.environ.get(path_env, "")
    if not path or not Path(path).is_file():
        raise RuntimeError(f"{path_env} is required")
    return path


def _secret(path_env: str, *, minimum: int = 1) -> str:
    value = Path(_secret_path(path_env)).read_text(encoding="utf-8").strip()
    if len(value) < minimum or any(ord(ch) < 33 or ord(ch) == 127 for ch in value):
        raise RuntimeError(f"{path_env} has an invalid value")
    return value


def _max_concurrency() -> int:
    try:
        value = int(os.environ.get("AGENT_REACH_MAX_CONCURRENCY", "1"))
    except ValueError:
        raise RuntimeError("AGENT_REACH_MAX_CONCURRENCY must be an integer") from None
    if not 1 <= value <= 4:
        raise RuntimeError("AGENT_REACH_MAX_CONCURRENCY must be between 1 and 4")
    return value


def _request_window_is_valid(request: TaskSubmission) -> bool:
    now = datetime.now(UTC)
    deadline = request.deadline_at.astimezone(UTC)
    return now < deadline <= now + timedelta(minutes=31)


class RunnerService:
    def __init__(self) -> None:
        token = _secret("AGENT_REACH_RUNNER_TOKEN_FILE", minimum=32)
        self._token = token.encode("utf-8")
        self.store = TaskStore(
            os.environ.get("AGENT_REACH_DB_PATH", "/var/lib/agent-reach/tasks.sqlite3")
        )
        self.backend = YouTubeSearchBackend()
        # Invalid operator-supplied ceilings fail closed at startup.
        self.backend.daily_search_limit()
        self.max_concurrency = _max_concurrency()
        self.stop = threading.Event()
        self._last_prune = 0.0
        self._prune_lock = threading.Lock()
        self.workers = [
            threading.Thread(target=self._worker, name=f"reach-worker-{number}", daemon=True)
            for number in range(self.max_concurrency)
        ]

    def healthy_capabilities(self) -> list[str]:
        try:
            self.backend._api_key()
        except BackendFailure:
            return []
        if os.environ.get("AGENT_REACH_YOUTUBE_ENABLED", "false").lower() != "true":
            return []
        return ["public_search_metadata"]

    def authorized(self, header: str | None) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        supplied = header[7:].encode("utf-8")
        return hmac.compare_digest(self._token, supplied)

    def _worker(self) -> None:
        while not self.stop.is_set():
            monotonic_now = time.monotonic()
            if monotonic_now - self._last_prune >= 3600:
                with self._prune_lock:
                    if monotonic_now - self._last_prune >= 3600:
                        try:
                            self.store.purge_expired_results()
                        except Exception as exc:
                            log.error("runner retention purge failed (%s)", type(exc).__name__)
                        self._last_prune = monotonic_now
            claimed = self.store.claim_next()
            if claimed is None:
                self.stop.wait(0.25)
                continue
            external_id, request = claimed
            if self.store.cancel_requested(external_id):
                self.store.fail(external_id, "Task cancelled.")
                continue
            if not _request_window_is_valid(request):
                self.store.expire(external_id, "Task deadline elapsed before search completed.")
                continue
            if "public_search_metadata" not in self.healthy_capabilities():
                self.store.fail(
                    external_id,
                    "Capability was disabled before provider search; "
                    "no provider request was issued.",
                )
                continue
            try:
                limit = self.backend.daily_search_limit()
                if not self.store.reserve_search(limit):
                    self.store.fail(external_id, "Runner search quota reached for the UTC day.")
                    continue
                results = self.backend.search(request)
                if self.store.cancel_requested(external_id):
                    self.store.fail(external_id, "Task cancelled; search results were discarded.")
                elif datetime.now(UTC) >= request.deadline_at.astimezone(UTC):
                    self.store.expire(
                        external_id, "Task deadline elapsed; search results were discarded."
                    )
                else:
                    self.store.complete(external_id, results)
            except BackendFailure as exc:
                self.store.fail(external_id, str(exc))
            except Exception as exc:
                # Do not log exception strings: HTTP client errors can include credentialed URLs.
                log.error("runner search failed (%s)", type(exc).__name__)
                self.store.fail(external_id, "Runner search failed unexpectedly.")

    def start(self) -> None:
        self.store.recover_interrupted()
        for worker in self.workers:
            worker.start()

    def close(self) -> None:
        self.stop.set()
        for worker in self.workers:
            worker.join(timeout=20)


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, *args, **kwargs) -> None:
        self._request_slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address) -> None:
        request.settimeout(10)
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


def handler_for(service: RunnerService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AfricaSignalRunner"
        sys_version = ""

        def log_message(self, format: str, *args) -> None:
            # Requests include search queries and API task IDs; no access log is emitted.
            return

        def _send(self, status: int, value: dict | None = None) -> None:
            body = (
                b"" if value is None else json.dumps(value, separators=(",", ":")).encode("utf-8")
            )
            self.send_response(status)
            if value is not None:
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _authorized(self) -> bool:
            if not service.authorized(self.headers.get("Authorization")):
                self._send(401, {"detail": "Authentication required."})
                return False
            return True

        def _body(self) -> bytes:
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Content-Type must be application/json")
            raw_length = self.headers.get("Content-Length", "")
            if not raw_length.isdigit() or int(raw_length) > 16_384:
                raise ValueError("Request body is missing or too large")
            body = self.rfile.read(int(raw_length))
            if len(body) != int(raw_length):
                raise ValueError("Request body was incomplete")
            return body

        def do_GET(self) -> None:
            if not self._authorized():
                return
            parts = urlsplit(self.path)
            if parts.query or parts.fragment:
                self._send(400, {"detail": "Unexpected query string."})
                return
            if parts.path == "/healthz":
                health = HealthResponse(
                    status="ok",
                    protocol_version=PROTOCOL_VERSION,
                    capabilities=service.healthy_capabilities(),
                    max_concurrency=service.max_concurrency,
                )
                self._send(200, health.model_dump(mode="json"))
                return
            match = TASK_PATH.fullmatch(parts.path)
            if match:
                response = service.store.get(match.group(1))
                if response is None:
                    self._send(404, {"detail": "Task not found."})
                else:
                    self._send(200, response.model_dump(mode="json"))
                return
            local_match = TASK_LOCAL_PATH.fullmatch(parts.path)
            if local_match:
                response = service.store.get_by_task_id(local_match.group(1))
                if response is None:
                    self._send(404, {"detail": "Task not found."})
                else:
                    self._send(200, response.model_dump(mode="json"))
                return
            self._send(404, {"detail": "Not found."})

        def do_POST(self) -> None:
            if not self._authorized():
                return
            if urlsplit(self.path).path != "/v1/tasks" or urlsplit(self.path).query:
                self._send(404, {"detail": "Not found."})
                return
            if "public_search_metadata" not in service.healthy_capabilities():
                self._send(503, {"detail": "The configured search capability is unavailable."})
                return
            try:
                request = TaskSubmission.model_validate_json(self._body())
                if not _request_window_is_valid(request):
                    raise ValueError("deadline_at must be within the next 31 minutes")
                if self.headers.get("Idempotency-Key") != request.idempotency_key:
                    raise ValueError("Idempotency-Key header does not match the request")
                response = service.store.enqueue(request)
            except ValidationError:
                self._send(422, {"detail": "Request does not match the Agent Reach task contract."})
                return
            except (TaskConflict, ValueError) as exc:
                self._send(409, {"detail": str(exc)[:200]})
                return
            except Exception as exc:
                log.error("runner task submission failed (%s)", type(exc).__name__)
                self._send(503, {"detail": "Runner task service is temporarily unavailable."})
                return
            self._send(202, response.model_dump(mode="json"))

        def do_DELETE(self) -> None:
            if not self._authorized():
                return
            parts = urlsplit(self.path)
            match = TASK_PATH.fullmatch(parts.path)
            if not match or parts.query or parts.fragment:
                self._send(404, {"detail": "Not found."})
                return
            response = service.store.request_cancel(match.group(1))
            if response is None:
                self._send(404)
            elif response.status in ("cancelled", "expired"):
                self._send(204)
            elif response.status in ("succeeded", "failed"):
                self._send(200, response.model_dump(mode="json"))
            else:
                self._send(202, response.model_dump(mode="json"))

    return Handler


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s"
    )
    service = RunnerService()
    service.start()
    host = os.environ.get("AGENT_REACH_BIND_HOST", "0.0.0.0")
    port = int(os.environ.get("AGENT_REACH_BIND_PORT", "8443"))
    server = BoundedHTTPServer((host, port), handler_for(service))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    cert = _secret_path("AGENT_REACH_TLS_CERT_FILE")
    key_path = os.environ.get("AGENT_REACH_TLS_KEY_FILE", "")
    if not key_path:
        raise RuntimeError("AGENT_REACH_TLS_KEY_FILE is required")
    context.load_cert_chain(certfile=cert, keyfile=key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)

    def stop(signum: int, frame) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
