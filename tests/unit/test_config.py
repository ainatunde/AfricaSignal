import pytest

from africasignal.config import REQUIRED_OUTSIDE_DEVELOPMENT, Settings


def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in REQUIRED_OUTSIDE_DEVELOPMENT:
        monkeypatch.delenv(name.upper(), raising=False)


def test_agent_reach_deployment_deny_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_REACH_DENY", raising=False)
    assert Settings(_env_file=None).agent_reach_deny is False
    monkeypatch.setenv("AGENT_REACH_DENY", "true")
    assert Settings(_env_file=None).agent_reach_deny is True


def test_external_agents_deployment_deny_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXTERNAL_AGENTS_DENY", raising=False)
    assert Settings(_env_file=None).external_agents_deny is False
    monkeypatch.setenv("EXTERNAL_AGENTS_DENY", "true")
    assert Settings(_env_file=None).external_agents_deny is True


def test_development_needs_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean(monkeypatch)
    monkeypatch.setenv("ENV", "development")
    Settings(_env_file=None).validate_required()


@pytest.mark.parametrize("env", ["staging", "production"])
def test_missing_secrets_fail_closed(monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    _clean(monkeypatch)
    monkeypatch.setenv("ENV", env)
    with pytest.raises(RuntimeError) as exc:
        Settings(_env_file=None).validate_required()
    for name in REQUIRED_OUTSIDE_DEVELOPMENT:
        assert name.upper() in str(exc.value)


def test_all_secrets_present_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean(monkeypatch)
    monkeypatch.setenv("ENV", "production")
    for name in REQUIRED_OUTSIDE_DEVELOPMENT:
        monkeypatch.setenv(name.upper(), "x")
    monkeypatch.setenv(
        "SECRET_KEY", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    )
    Settings(_env_file=None).validate_required()


def test_one_missing_secret_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean(monkeypatch)
    monkeypatch.setenv("ENV", "staging")
    for name in REQUIRED_OUTSIDE_DEVELOPMENT:
        monkeypatch.setenv(name.upper(), "x")
    monkeypatch.delenv("SECRET_KEY")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        Settings(_env_file=None).validate_required()


@pytest.mark.parametrize(
    "key",
    [
        "x",
        " " * 64,
        "a" * 64,
        "africasignal-development-only-secret-key",
        "change-me-change-me-change-me-change-me",
    ],
)
def test_weak_production_secrets_are_refused(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/app")
    monkeypatch.setenv("SECRET_KEY", key)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        Settings(_env_file=None).validate_required()
