"""Bound OS DNS with a killable child process and a bounded number of children."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

MAX_DNS_SECONDS = 5.0
_slots = threading.BoundedSemaphore(8)
_deadline: ContextVar[float | None] = ContextVar("dns_deadline", default=None)
_RESOLVE_SCRIPT = """
import json, socket, sys
try:
    answers = socket.getaddrinfo(sys.argv[1], int(sys.argv[2]), type=socket.SOCK_STREAM)
    addresses = sorted({answer[4][0] for answer in answers})
    print(json.dumps(addresses if len(addresses) <= 128 else []))
except (OSError, UnicodeError, ValueError):
    print('[]')
"""


@contextmanager
def dns_budget(seconds: float) -> Iterator[None]:
    deadline = time.monotonic() + max(0.0, seconds)
    previous = _deadline.get()
    token = _deadline.set(min(deadline, previous) if previous is not None else deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def dns_addresses(host: str, port: int) -> list[str]:
    deadline = min(_deadline.get() or float("inf"), time.monotonic() + MAX_DNS_SECONDS)
    if len(host) > 253 or not _slots.acquire(timeout=max(0.0, deadline - time.monotonic())):
        return []
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return []
        # subprocess.run kills and reaps the child on timeout; no blocked resolver threads
        # accumulate. Arguments never pass through a shell and -I ignores user Python hooks.
        result = subprocess.run(
            [sys.executable, "-I", "-c", _RESOLVE_SCRIPT, host, str(port)],
            capture_output=True,
            text=True,
            timeout=remaining,
            check=True,
        )
        if len(result.stdout) > 16384:
            return []
        addresses = json.loads(result.stdout)
        if not isinstance(addresses, list) or not all(isinstance(ip, str) for ip in addresses):
            return []
        return addresses
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    finally:
        _slots.release()
