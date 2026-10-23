from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from africasignal.backup_alerts import Finding
from africasignal.research.guardian import _debounced, evaluate


def test_disabled_research_has_no_false_overdue_incident():
    with patch("africasignal.research.guardian.readiness", return_value=("not_scheduled", "off")):
        assert evaluate(Mock(), datetime.now(UTC)) == []


def test_enabled_but_incompatible_configuration_is_not_healthy():
    with patch(
        "africasignal.research.guardian.readiness", return_value=("blocked", "invalid_policy")
    ):
        findings = evaluate(Mock(), datetime.now(UTC))
    assert [f.code for f in findings] == ["autonomy_configuration"]
    assert findings[0].detail["reason"] == "invalid_policy"


def test_guardian_alerts_require_two_failures_and_two_recoveries():
    finding = Finding("research_progress", "stalled", {})
    states = {}
    session = Mock()
    session.get.side_effect = lambda _model, key: (
        SimpleNamespace(value=states[key]) if key in states else None
    )
    session.merge.side_effect = lambda value: states.__setitem__(value.key, value.value)
    assert _debounced(session, datetime.now(UTC), [finding]) == []
    assert _debounced(session, datetime.now(UTC), [finding]) == [finding]
    assert _debounced(session, datetime.now(UTC), []) == [finding]
    assert _debounced(session, datetime.now(UTC), []) == []
