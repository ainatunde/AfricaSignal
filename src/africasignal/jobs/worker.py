"""Worker loop: claim a job, run its handler, record the outcome."""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
from types import FrameType

from sqlalchemy.orm import Session, sessionmaker

from africasignal.db import get_engine
from africasignal.jobs import handlers as handler_registry
from africasignal.jobs import queue
from africasignal.jobs.handlers import JobContext
from africasignal.jobs.log import configure_logging
from africasignal.publish.recovery import require_recovery_complete

log = logging.getLogger("africasignal.worker")

POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 60.0


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class _Heartbeat:
    """Extends a running job's lease from a separate connection until stopped."""

    def __init__(
        self, factory: sessionmaker[Session], job_id: int, worker_id: str, interval: float
    ) -> None:
        self._factory = factory
        self._job_id = job_id
        self._worker_id = worker_id
        self._interval = interval
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                with self._factory() as session:
                    renewed = queue.extend_lease(session, self._job_id, self._worker_id)
                    session.commit()
                    if not renewed:
                        self.lost.set()
                        return
            except Exception:  # a missed heartbeat is retried on the next tick
                log.exception("lease extension failed", extra={"job_id": self._job_id})

    def __enter__(self) -> _Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()


class Worker:
    def __init__(
        self,
        factory: sessionmaker[Session] | None = None,
        worker_id: str | None = None,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
    ) -> None:
        self.factory = factory or sessionmaker(bind=get_engine(), expire_on_commit=False)
        self.worker_id = worker_id or default_worker_id()
        self.heartbeat_seconds = heartbeat_seconds
        self._stopping = threading.Event()

    def stop(self) -> None:
        """Finish the current job, then leave the loop."""
        self._stopping.set()

    def run_once(self) -> bool:
        """Claim and run at most one job. Returns True when a job was processed."""
        with self.factory() as session:
            job = queue.claim(session, self.worker_id)
            session.commit()
        if job is None:
            return False

        ctx = {
            "job_id": job.id,
            "kind": job.kind,
            "attempt": job.attempts,
            "worker": self.worker_id,
        }
        log.info("job started", extra=ctx)
        handler = handler_registry.get_handler(job.kind)
        try:
            if handler is None:
                raise LookupError(f"no handler registered for job kind {job.kind!r}")
            with self.factory() as session:
                with _Heartbeat(
                    self.factory, job.id, self.worker_id, self.heartbeat_seconds
                ) as heartbeat:
                    handler(JobContext(session=session, job=job, worker_id=self.worker_id))
                if heartbeat.lost.is_set() or not queue.complete(session, job.id, self.worker_id):
                    session.rollback()
                    log.warning("job lease lost before completion; result discarded", extra=ctx)
                    return True
                session.commit()
            log.info("job done", extra=ctx)
        except Exception as exc:
            log.exception("job failed", extra=ctx)
            with self.factory() as session:
                status = queue.fail(session, job.id, self.worker_id, f"{type(exc).__name__}: {exc}")
                session.commit()
            log.info("job %s after failure", status, extra=ctx)
            if status == "dead":
                hook = handler_registry.DEAD_HOOKS.get(job.kind)
                if hook is not None:
                    try:
                        hook(job)
                    except Exception:  # tidying up must not hide the failure that was recorded
                        log.exception("dead-job cleanup failed", extra=ctx)
        return True

    def run_forever(self) -> None:
        while not self._stopping.is_set():
            if not self.run_once():
                self._stopping.wait(POLL_SECONDS)


def main() -> None:
    configure_logging()
    require_recovery_complete()
    handler_registry.load_all()
    worker = Worker()

    def _handle_signal(signum: int, frame: FrameType | None) -> None:
        log.info("signal %s received; stopping after the current job", signum)
        worker.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    log.info("worker started", extra={"worker": worker.worker_id})
    worker.run_forever()
    log.info("worker stopped", extra={"worker": worker.worker_id})


if __name__ == "__main__":
    main()
