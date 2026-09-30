"""Validation of console settings values (no database)."""

from __future__ import annotations

import pytest

from africasignal import settings_store as ss
from africasignal.settings_store import SettingError, normalise


def n(key: str, raw: str) -> str:
    return normalise(ss.definition(key), raw)


def test_registry_is_consistent() -> None:
    assert len(ss.REGISTRY) == len(ss._DEFS)
    groups = {g for g, _ in ss.GROUPS}
    assert {d.group for d in ss.REGISTRY.values()} <= groups
    for d in ss.REGISTRY.values():
        assert d.key == d.key.lower() and d.env_name == d.key.upper()
        if d.kind == "choice":
            assert d.choices
    with pytest.raises(KeyError):
        ss.definition("database_url")  # deliberately not managed here
    with pytest.raises(KeyError):
        ss.definition("secret_key")


def test_secret_rules() -> None:
    assert n("anthropic_api_key", "sk-ant-abcdefgh") == "sk-ant-abcdefgh"
    for bad in ("short", "has a space in it", "x" * 501, "line\nbreak-key"):
        with pytest.raises(SettingError):
            n("anthropic_api_key", bad)


@pytest.mark.parametrize(
    "raw, expected",
    [("https://africasignal.example", "https://africasignal.example"),
     ("https://africasignal.example/", "https://africasignal.example")],
)  # fmt: skip
def test_public_base_url_accepts_a_bare_https_origin(raw: str, expected: str) -> None:
    assert n("public_base_url", raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "africasignal.example",
        "ftp://africasignal.example",
        "javascript:alert(1)",
        "https://user:pw@africasignal.example",
        "https://africasignal.example/path",
        "https://africasignal.example?x=1",
        "https://africasignal.example/#frag",
        "http://africasignal.example",  # https only outside development
    ],
)
def test_public_base_url_rejects_bad_values(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("africasignal.settings_store.get_settings", lambda: _Env("production"))
    with pytest.raises(SettingError):
        n("public_base_url", raw)


class _Env:
    def __init__(self, env: str) -> None:
        self.env = env


def test_http_is_allowed_for_the_public_url_in_development(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("africasignal.settings_store.get_settings", lambda: _Env("development"))
    assert n("public_base_url", "http://localhost:8000") == "http://localhost:8000"


def test_storage_endpoint_may_be_plain_http_and_have_a_path() -> None:
    assert n("s3_endpoint_url", "http://minio:9000") == "http://minio:9000"


def test_email_fields() -> None:
    assert n("email_provider", "postmark") == "postmark"
    assert n("email_provider", "resend") == "resend"
    with pytest.raises(SettingError):
        n("email_provider", "ses")  # needs a key pair and a region; not offered yet
    with pytest.raises(SettingError):
        n("email_provider", "carrier-pigeon")
    assert n("email_from", "AfricaSignal <hello@africasignal.example>")
    for bad in ("hello", "hello@", "a b@c.d", "<>"):
        with pytest.raises(SettingError):
            n("email_from", bad)


def test_numbers_are_range_checked() -> None:
    assert n("llm_daily_budget_usd", "2.5") == "2.5"
    assert n("backup_retain_days", "30") == "30"
    for key, bad in (
        ("llm_daily_budget_usd", "-1"),
        ("llm_daily_budget_usd", "lots"),
        ("llm_per_job_max_tokens", "10"),
        ("llm_per_job_max_tokens", "2.5"),
        ("backup_retain_days", "0"),
        ("backup_retain_days", "99999"),
    ):
        with pytest.raises(SettingError):
            n(key, bad)


def test_bucket_and_prefix_rules() -> None:
    assert n("s3_bucket", "africasignal-evidence") == "africasignal-evidence"
    assert n("backup_s3_prefix", "africasignal/production") == "africasignal/production"
    for key, bad in (
        ("s3_bucket", "Has Caps"),
        ("s3_bucket", "a"),
        ("backup_s3_prefix", "/absolute"),
        ("backup_s3_prefix", "a/../b"),
    ):
        with pytest.raises(SettingError):
            n(key, bad)


def test_control_characters_are_refused_everywhere() -> None:
    with pytest.raises(SettingError):
        n("s3_bucket", "bucket\x00name")
