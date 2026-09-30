# ruff: noqa: F811
"""Console settings (encrypted store with environment fallback) and the Settings page."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from africasignal import admin as admin_cli
from africasignal import settings_store as ss
from africasignal import storage
from africasignal.models import AuditLog, Setting
from africasignal.settings_store import SettingError
from tests.integration.test_admin_console import (  # noqa: F401  (fixtures and helpers)
    ORIGIN,
    client,
    make_operator,
    signed_in,
)

SECRET = "sk-ant-api03-very-secret-value-0123456789"


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for d in ss.REGISTRY.values():
        monkeypatch.delenv(d.env_name, raising=False)


def raw_value(session: Session, key: str) -> object:
    return session.execute(
        select(Setting.value).where(Setting.key == f"config.{key}")
    ).scalar_one_or_none()


def audit_rows(session: Session) -> list[AuditLog]:
    return list(session.scalars(select(AuditLog).order_by(AuditLog.id)))


# --- the store ----------------------------------------------------------------------------------


def test_resolution_order_console_then_environment_then_default(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = make_operator(session).operator
    assert ss.resolve(session, "llm_daily_budget_usd") == ss.Resolved("10", "default")
    assert ss.resolve(session, "email_from") == ss.Resolved(None, "unset")

    monkeypatch.setenv("LLM_DAILY_BUDGET_USD", "4")
    assert ss.resolve(session, "llm_daily_budget_usd") == ss.Resolved("4", "environment")

    ss.set_value(session, operator, "llm_daily_budget_usd", "7.5")
    assert ss.resolve(session, "llm_daily_budget_usd") == ss.Resolved("7.5", "console")
    assert ss.get_float(session, "llm_daily_budget_usd") == 7.5

    ss.clear_value(session, operator, "llm_daily_budget_usd")
    assert ss.resolve(session, "llm_daily_budget_usd") == ss.Resolved("4", "environment")
    assert ss.get_int(session, "llm_per_job_max_tokens") == 20000


def test_a_secret_is_encrypted_at_rest_and_round_trips(session: Session) -> None:
    operator = make_operator(session).operator
    ss.set_value(session, operator, "anthropic_api_key", SECRET)
    stored = raw_value(session, "anthropic_api_key")
    assert SECRET not in json.dumps(stored) and "enc" in stored  # type: ignore[operator]
    assert ss.get(session, "anthropic_api_key") == SECRET
    # the whole table, dumped as text, never holds the plain secret
    dump = session.execute(text("SELECT string_agg(value::text, ' ') FROM setting")).scalar_one()
    assert SECRET not in dump


def test_non_secret_values_are_stored_plainly(session: Session) -> None:
    ss.set_value(session, make_operator(session).operator, "s3_bucket", "evidence-bucket")
    assert raw_value(session, "s3_bucket") == {"v": "evidence-bucket"}


def test_unreadable_secret_falls_back_to_the_environment(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    session.add(Setting(key="config.anthropic_api_key", value={"enc": "not-a-fernet-token"}))
    session.flush()
    assert ss.resolve(session, "anthropic_api_key") == ss.Resolved(None, "unreadable")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-the-environment")
    assert ss.resolve(session, "anthropic_api_key") == ss.Resolved(
        "from-the-environment", "environment"
    )


def test_audit_rows_record_non_secret_values_but_never_a_secret(session: Session) -> None:
    operator = make_operator(session).operator
    ss.set_value(session, operator, "s3_bucket", "first-bucket")
    ss.set_value(session, operator, "s3_bucket", "second-bucket")
    ss.set_value(session, operator, "anthropic_api_key", SECRET)
    ss.set_value(session, operator, "anthropic_api_key", SECRET + "-rotated")
    ss.clear_value(session, operator, "anthropic_api_key")
    rows = audit_rows(session)
    assert [r.action for r in rows] == ["setting.set"] * 4 + ["setting.clear"]
    assert rows[1].before == {"key": "s3_bucket", "value": "first-bucket"}
    assert rows[1].after == {"key": "s3_bucket", "value": "second-bucket"}
    assert rows[2].target_kind == "setting:anthropic_api_key"
    assert rows[2].before == {"key": "anthropic_api_key", "configured": False}
    assert rows[2].after == {"key": "anthropic_api_key", "configured": True}
    everything = json.dumps([(r.before, r.after) for r in rows])
    assert SECRET not in everything and "rotated" not in everything


def test_setting_the_same_value_or_clearing_nothing_writes_no_audit_row(session: Session) -> None:
    operator = make_operator(session).operator
    ss.set_value(session, operator, "s3_bucket", "same-bucket")
    before = len(audit_rows(session))
    ss.set_value(session, operator, "s3_bucket", "same-bucket")
    ss.clear_value(session, operator, "email_from")
    assert len(audit_rows(session)) == before


def test_apply_changes_is_all_or_nothing(session: Session) -> None:
    operator = make_operator(session).operator
    with pytest.raises(SettingError):
        ss.apply_changes(
            session, operator, {"s3_bucket": "good-bucket", "s3_endpoint_url": "ftp://nope"}
        )
    assert raw_value(session, "s3_bucket") is None
    assert audit_rows(session) == []


def test_other_setting_rows_are_untouched_and_unknown_keys_refused(session: Session) -> None:
    operator = make_operator(session).operator
    session.add(Setting(key="publication_suspended", value=True))
    session.flush()
    ss.set_value(session, operator, "s3_bucket", "some-bucket")
    assert raw_value_plain(session, "publication_suspended") is True
    with pytest.raises(KeyError):
        ss.set_value(session, operator, "publication_suspended", "false")


def raw_value_plain(session: Session, key: str) -> object:
    return session.execute(select(Setting.value).where(Setting.key == key)).scalar_one()


def test_storage_config_and_get_store_follow_the_console(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = make_operator(session).operator
    monkeypatch.setenv("S3_BUCKET", "env-bucket")
    assert ss.storage_config(session).bucket == "env-bucket"
    ss.apply_changes(
        session,
        operator,
        {
            "s3_endpoint_url": "http://minio:9000",
            "s3_bucket": "console-bucket",
            "s3_access_key_id": "console-key-id",
            "s3_secret_access_key": "console-secret-key",
        },
    )
    config = ss.storage_config(session)
    assert config == ss.StorageConfig(
        "http://minio:9000", "console-bucket", "console-key-id", "console-secret-key"
    )

    @contextmanager
    def scope() -> Iterator[Session]:
        yield session

    monkeypatch.setattr(storage, "session_scope", scope)
    storage._store_for.cache_clear()
    store = storage.get_store()
    assert store._bucket == "console-bucket"
    ss.set_value(session, operator, "s3_bucket", "another-bucket")
    assert storage.get_store()._bucket == "another-bucket"  # no restart needed
    storage._store_for.cache_clear()


def test_missing_expected_is_empty_in_development_and_lists_gaps_in_production(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert ss.missing_expected(session) == []
    monkeypatch.setattr(ss, "get_settings", lambda: _Env("production"))
    monkeypatch.setattr(ss, "_environment", lambda defn: None)
    missing = {d.key for d in ss.missing_expected(session)}
    assert {"anthropic_api_key", "email_api_key", "s3_bucket", "public_base_url"} <= missing
    operator = make_operator(session).operator
    ss.set_value(session, operator, "s3_bucket", "some-bucket")
    assert "s3_bucket" not in {d.key for d in ss.missing_expected(session)}


class _Env:
    def __init__(self, env: str) -> None:
        self.env = env


# --- the page -----------------------------------------------------------------------------------


def post(client: TestClient, group: str, data: dict[str, str]) -> object:
    return client.post(f"/admin/settings/{group}", data=data, headers=ORIGIN)


def test_settings_are_admin_only(client: TestClient, session: Session) -> None:
    assert client.get("/admin/settings").status_code == 303  # signed out
    signed_in(client, make_operator(session, "editor@example.org", "editor"))
    assert client.get("/admin/settings").status_code == 403
    assert post(client, "llm", {"anthropic_api_key": SECRET}).status_code == 403  # type: ignore[attr-defined]
    assert raw_value(session, "anthropic_api_key") is None
    assert "/admin/settings" not in client.get("/admin/sources").text  # no nav link


def test_admin_sees_the_settings_nav_link_and_page(client: TestClient, session: Session) -> None:
    signed_in(client, make_operator(session))
    assert "/admin/settings" in client.get("/admin/sources").text
    page = client.get("/admin/settings")
    assert page.status_code == 200
    for label in ("Anthropic API key", "Public address", "Sender address", "Backup storage"):
        assert label in page.text


def test_saving_a_secret_stores_it_and_never_shows_it_again(
    client: TestClient, session: Session
) -> None:
    signed_in(client, make_operator(session))
    response = post(client, "llm", {"anthropic_api_key": SECRET, "llm_daily_budget_usd": "10"})
    assert response.status_code == 303  # type: ignore[attr-defined]
    assert ss.get(session, "anthropic_api_key") == SECRET
    for path in ("/admin/settings", "/admin/audit"):
        page = client.get(path)
        assert SECRET not in page.text and SECRET[:12] not in page.text
    page = client.get("/admin/settings").text
    assert "configured" in page and "Leave empty to keep the saved key" in page
    assert ss.get(session, "llm_daily_budget_usd") == "10"
    assert raw_value(session, "llm_daily_budget_usd") is None  # prefilled default: no override


def test_a_blank_secret_field_keeps_the_saved_secret(client: TestClient, session: Session) -> None:
    signed_in(client, make_operator(session))
    post(client, "llm", {"anthropic_api_key": SECRET})
    before = len(audit_rows(session))
    response = post(client, "llm", {"anthropic_api_key": "", "llm_daily_budget_usd": "10"})
    assert response.headers["location"].endswith("notice=unchanged")  # type: ignore[attr-defined]
    assert ss.get(session, "anthropic_api_key") == SECRET
    assert len(audit_rows(session)) == before


def test_clear_checkbox_removes_the_console_value(client: TestClient, session: Session) -> None:
    signed_in(client, make_operator(session))
    post(client, "llm", {"anthropic_api_key": SECRET})
    post(client, "llm", {"clear__anthropic_api_key": "on"})
    assert ss.resolve(session, "anthropic_api_key").source == "unset"


def test_emptying_a_plain_field_falls_back_to_the_environment(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("S3_BUCKET", "env-bucket")
    signed_in(client, make_operator(session))
    post(client, "storage", {"s3_bucket": "console-bucket"})
    assert ss.get(session, "s3_bucket") == "console-bucket"
    post(client, "storage", {"s3_bucket": ""})
    assert ss.resolve(session, "s3_bucket") == ss.Resolved("env-bucket", "environment")


def test_submitting_the_prefilled_environment_value_does_not_create_an_override(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("S3_BUCKET", "env-bucket")
    signed_in(client, make_operator(session))
    page = client.get("/admin/settings").text
    assert 'value="env-bucket"' in page and "From environment variable" in page
    post(client, "storage", {"s3_bucket": "env-bucket"})
    assert raw_value(session, "s3_bucket") is None


def test_invalid_value_shows_an_error_keeps_the_input_and_saves_nothing(
    client: TestClient, session: Session
) -> None:
    signed_in(client, make_operator(session))
    response = post(
        client,
        "email",
        {"email_provider": "postmark", "email_from": "not-an-email", "email_api_key": SECRET},
    )
    assert response.status_code == 400  # type: ignore[attr-defined]
    assert "Sender address: enter an email address" in response.text  # type: ignore[attr-defined]
    assert "not-an-email" in response.text  # type: ignore[attr-defined]
    assert SECRET not in response.text  # type: ignore[attr-defined]
    assert ss.get(session, "email_provider") is None and ss.get(session, "email_api_key") is None


def test_unknown_group_is_404_and_cross_origin_post_is_refused(
    client: TestClient, session: Session
) -> None:
    signed_in(client, make_operator(session))
    assert post(client, "nope", {}).status_code == 404  # type: ignore[attr-defined]
    response = client.post(
        "/admin/settings/llm",
        data={"anthropic_api_key": SECRET},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert ss.get(session, "anthropic_api_key") is None


def test_a_group_form_cannot_write_a_key_from_another_group(
    client: TestClient, session: Session
) -> None:
    signed_in(client, make_operator(session))
    post(client, "llm", {"s3_bucket": "smuggled-bucket", "email_from": "a@b.co"})
    assert ss.get(session, "s3_bucket") is None and ss.get(session, "email_from") is None


def test_hostile_values_are_escaped_on_the_page(client: TestClient, session: Session) -> None:
    session.add(Setting(key="config.s3_bucket", value={"v": '"><script>alert(1)</script>'}))
    session.flush()
    signed_in(client, make_operator(session))
    page = client.get("/admin/settings").text
    assert "<script>alert(1)" not in page


def test_settings_changes_appear_in_the_audit_page(client: TestClient, session: Session) -> None:
    signed_in(client, make_operator(session))
    post(client, "storage", {"s3_bucket": "audited-bucket"})
    page = client.get("/admin/audit").text
    assert "setting.set" in page and "setting:s3_bucket" in page and "audited-bucket" in page


# --- CLI ----------------------------------------------------------------------------------------


def test_get_setting_cli(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    @contextmanager
    def scope() -> Iterator[Session]:
        yield session

    monkeypatch.setattr(admin_cli, "session_scope", scope)
    operator = make_operator(session).operator
    ss.set_value(session, operator, "backup_s3_bucket", "backups-bucket")
    ss.set_value(session, operator, "backup_s3_secret_access_key", "backup-secret-value")
    assert admin_cli.main(["get-setting", "backup_s3_bucket"]) == 0
    assert capsys.readouterr().out.strip() == "backups-bucket"
    assert admin_cli.main(["get-setting", "backup_s3_secret_access_key"]) == 1  # needs --reveal
    assert "backup-secret-value" not in capsys.readouterr().out
    assert admin_cli.main(["get-setting", "backup_s3_secret_access_key", "--reveal"]) == 0
    assert capsys.readouterr().out.strip() == "backup-secret-value"
    assert admin_cli.main(["get-setting", "email_from"]) == 2  # unset: prints nothing
    assert capsys.readouterr().out == ""
    assert admin_cli.main(["get-setting", "database_url"]) == 1  # not a console setting
