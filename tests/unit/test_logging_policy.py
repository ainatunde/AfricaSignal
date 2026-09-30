"""The notice says our software keeps no IP address and no URL (AS-043 gap G5). Uvicorn's access
log would write both for every request, so the web process must start without it, and nothing
that sits in front of the web service in the compose file may log either."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
REVERSE_PROXY = re.compile(r"caddy|nginx|traefik|haproxy|envoy|apache|httpd", re.IGNORECASE)


def compose() -> dict:  # type: ignore[type-arg]
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())  # type: ignore[no-any-return]


def test_the_web_service_starts_without_an_access_log() -> None:
    command = compose()["services"]["web"]["command"]
    assert command[0] == "uvicorn"
    assert "--no-access-log" in command


def test_the_image_default_also_starts_without_an_access_log() -> None:
    cmd = next(
        line for line in (ROOT / "Dockerfile").read_text().splitlines() if line.startswith("CMD")
    )
    assert "uvicorn" in cmd and "--no-access-log" in cmd


def test_no_reverse_proxy_is_added_without_its_logging_being_reviewed() -> None:
    proxies = [
        name
        for name, service in compose()["services"].items()
        if REVERSE_PROXY.search(str(service.get("image", ""))) or REVERSE_PROXY.search(name)
    ]
    # If this fails, a proxy was added to the compose file. Configure it to write no client
    # addresses or URLs (or to keep them a few days at most), say what it does in the privacy
    # notice's "Technical logs" section and docs/launch.md item S5, then list it in this test.
    assert proxies == []
