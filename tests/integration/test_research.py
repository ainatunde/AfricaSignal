"""Real PostgreSQL research lineage, policy and recovery contracts."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select

from africasignal.jobs.handlers import load_all
from africasignal.models import Job, Place, ResearchQuestion, Setting, Situation, WorkloadControl
from africasignal.publish.factfmt import fact_value_text
from africasignal.research.corrections import observe
from africasignal.research.guardian import check as guardian_check
from africasignal.research.planner import mark, plan
from africasignal.research.policy import KEY, ResearchPolicy, readiness
from africasignal.web.queries import current_situation
from tests.integration.test_insight_candidates import NOW, _assessment, _source


def seed(session):
    processing = session.scalar(select(WorkloadControl).where(WorkloadControl.name == "processing"))
    assert processing is not None
    processing.enabled = True
    country = Place(kind="country", code="NG", name="Nigeria")
    session.add(country)
    session.flush()
    source = _source(session)
    source.home_url = "https://source.example/"
    version = _assessment(session, source, country, item_code="pms_litre", change=12)
    version.unknowns = ["The latest official figure is for September"]
    policy = ResearchPolicy(
        mode="research",
        source_ids=[source.id],
        engineering_owner="engineer",
        editorial_owner="editor",
        operating_owner="operator",
    )
    mark(session, KEY, policy.model_dump())
    session.flush()
    assert readiness(session, NOW)[0] == "ready"
    return source, version, policy


def test_planner_dedupes_and_cancels_old_policy_generation(session):
    source, version, policy = seed(session)
    assert plan(session, NOW) == 1
    plan(session, NOW)
    assert (
        session.scalar(select(func.count()).select_from(Job).where(Job.kind == "research_question"))
        == 1
    )
    question = session.scalar(select(ResearchQuestion))
    assert question.scope["item_code"] == "pms_litre"
    assert question.scope["place_code"] == "NG"
    assert question.state == "queued"
    mark(session, KEY, policy.model_copy(update={"revision": 2}).model_dump())
    plan(session, NOW + timedelta(minutes=5))
    session.refresh(question)
    assert question.state == "cancelled"
    assert session.scalar(select(func.count()).select_from(ResearchQuestion)) == 2


def test_disabled_policy_enqueues_no_research(session):
    seed(session)
    mark(session, KEY, ResearchPolicy().model_dump())
    assert plan(session, NOW) == 0
    assert session.scalar(select(func.count()).select_from(ResearchQuestion)) == 0
    assert (
        session.scalar(select(func.count()).select_from(Job).where(Job.kind.like("%research%")))
        == 0
    )


def test_guardian_stall_incident_opens_and_recovers_with_two_observations(session):
    load_all()
    _, _, _ = seed(session)
    plan(session, NOW)
    question = session.scalar(select(ResearchQuestion))
    assert question is not None
    now = datetime.now(UTC)
    question.updated_at = now - timedelta(minutes=30)
    mark(session, "research.planner_progress", {"at": now.isoformat()})
    guardian_check(session, now)
    alert = session.get(Setting, "ops.alert.research_progress")
    assert alert is None or alert.value.get("state") != "open"
    guardian_check(session, now + timedelta(minutes=1))
    alert = session.get(Setting, "ops.alert.research_progress")
    assert alert is not None and alert.value["state"] == "open"

    question.state = "cancelled"
    question.updated_at = now + timedelta(minutes=2)
    guardian_check(session, now + timedelta(minutes=2))
    alert = session.get(Setting, "ops.alert.research_progress")
    assert alert is not None
    session.refresh(alert)
    assert alert.value["state"] == "open"
    guardian_check(session, now + timedelta(minutes=3))
    session.refresh(alert)
    assert alert.value["state"] == "resolved"


def test_unresolved_generation_reopens_only_after_daily_cooldown(session):
    seed(session)
    plan(session, NOW)
    first = session.scalar(select(ResearchQuestion))
    assert first is not None
    first.state = "unresolved"
    first.reason = "no_current_supported_passage"
    first.updated_at = NOW
    plan(session, NOW + timedelta(hours=23))
    assert session.scalar(select(func.count()).select_from(ResearchQuestion)) == 1
    plan(session, NOW + timedelta(hours=25))
    assert session.scalar(select(func.count()).select_from(ResearchQuestion)) == 2


def test_public_correction_guardian_requires_matching_current_facts_and_history(
    session, monkeypatch: pytest.MonkeyPatch
):
    _, version, _ = seed(session)
    situation = session.scalar(select(Situation).where(Situation.current_version_id == version.id))
    assert situation is not None
    current_view = current_situation(session, situation.slug)
    assert current_view is not None
    expected = [
        [
            " ".join(str(fact.get("label") or "").split()),
            " ".join(fact_value_text(fact).split()),
            " ".join(str(fact.get("period") or "").split()),
            " ".join(str(fact.get("source_label") or "").split()),
        ]
        for fact in current_view.version.facts
    ]
    observed = {
        "guardian_version_id": str(version.id),
        "guardian_status": current_view.effective_status,
        "guardian_fact_rows": expected,
    }
    monkeypatch.setattr(
        "africasignal.research.corrections._read_page",
        lambda _client, _origin, route: (
            "page",
            {"guardian_current_version_id": str(version.id)}
            if route.endswith("/history")
            else observed,
            [],
        ),
    )
    question = SimpleNamespace(situation_id=situation.id)
    action = SimpleNamespace(after_version_id=version.id)
    good, _ = observe(session, question, action, Mock(), "https://public.example")
    assert good
    observed["guardian_fact_rows"] = []
    good, result = observe(session, question, action, Mock(), "https://public.example")
    assert not good and result["facts_match"] is False
