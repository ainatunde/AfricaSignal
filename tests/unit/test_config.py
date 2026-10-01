import pytest

from africasignal.config import REQUIRED_OUTSIDE_DEVELOPMENT, Settings


def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in REQUIRED_OUTSIDE_DEVELOPMENT:
        monkeypatch.delenv(name.upper(), raising=False)


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
    Settings(_env_file=None).validate_required()


def test_one_missing_secret_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean(monkeypatch)
    monkeypatch.setenv("ENV", "staging")
    for name in REQUIRED_OUTSIDE_DEVELOPMENT:
        monkeypatch.setenv(name.upper(), "x")
    monkeypatch.delenv("SECRET_KEY")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        Settings(_env_file=None).validate_required()
