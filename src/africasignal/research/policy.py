"""One revisioned authority record. Pausing never grants permission to send externally."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.jobs.policy import WorkloadSchedule
from africasignal.models import Operator, Setting, WorkloadControl

KEY = "research.policy"
REVISION: Literal["as-research-1"] = "as-research-1"
JOB_KINDS = (
    "plan_research",
    "research_question",
    "judge_research",
    "apply_research",
    "verify_research",
)


class ResearchPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int = Field(default=1, ge=1)
    contract: Literal["as-research-1"] = REVISION
    mode: Literal["off", "shadow", "research", "canary"] = "off"
    decisions_enabled: bool = False
    application_enabled: bool = False
    external_send_enabled: Literal[False] = False
    source_ids: list[int] = Field(default_factory=list, max_length=50)
    engineering_owner: str = Field(default="", max_length=120)
    editorial_owner: str = Field(default="", max_length=120)
    operating_owner: str = Field(default="", max_length=120)
    qualification_ref: str = Field(default="", max_length=200)
    max_questions_per_hour: int = Field(default=10, ge=1, le=10)
    max_fetches_per_hour: int = Field(default=30, ge=1, le=30)
    max_documents: int = Field(default=3, ge=1, le=3)
    max_attempts: int = Field(default=3, ge=1, le=3)
    max_model_calls: int = Field(default=2, ge=1, le=2)
    progress_minutes: int = Field(default=15, ge=5, le=60)

    @model_validator(mode="after")
    def authority(self) -> ResearchPolicy:
        if len(set(self.source_ids)) != len(self.source_ids) or any(
            i <= 0 for i in self.source_ids
        ):
            raise ValueError("source IDs must be unique positive integers")
        if self.application_enabled and (self.mode != "canary" or not self.decisions_enabled):
            raise ValueError("automatic application requires canary mode and validated decisions")
        if self.mode != "off" and not all(
            x.strip() for x in (self.engineering_owner, self.editorial_owner, self.operating_owner)
        ):
            raise ValueError(
                "enabled research requires named engineering, editorial and operating owners"
            )
        if self.mode == "canary" and not self.qualification_ref.strip():
            raise ValueError("canary requires an accepted qualification reference")
        return self


def read_policy(session: Session, *, lock: bool = False) -> ResearchPolicy:
    stmt = select(Setting).where(Setting.key == KEY)
    if lock:
        stmt = stmt.with_for_update(read=True)
    row = session.scalar(stmt)
    return ResearchPolicy.model_validate(row.value) if row else ResearchPolicy()


def configure(session: Session, operator: Operator, policy: ResearchPolicy, expected: int) -> None:
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": KEY})
    old = read_policy(session)
    if old.revision != expected or policy.revision != expected + 1:
        raise ValueError("research policy changed; reload before saving")
    if policy.mode == "canary":
        from africasignal.research.qualification import accepted_release

        row = session.get(Setting, f"research.qualification.{policy.qualification_ref}")
        evaluation = row.value.get("evaluation", {}) if row and isinstance(row.value, dict) else {}

        if (
            not accepted_release(session, policy.qualification_ref)
            or evaluation.get("policy_revision") != policy.revision
            or sorted(evaluation.get("source_ids", [])) != sorted(policy.source_ids)
        ):
            raise ValueError("canary qualification has not passed all release gates")
    stmt = insert(Setting).values(key=KEY, value=policy.model_dump(mode="json"))
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Setting.key], set_={"value": stmt.excluded.value}
        )
    )
    audit.record(
        session,
        operator,
        "research.policy_changed",
        "setting",
        None,
        before=old.model_dump(),
        after=policy.model_dump(),
    )


def readiness(session: Session, now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.now(UTC)
    try:
        policy = read_policy(session)
    except ValueError:
        return "blocked", "invalid_policy"
    if policy.mode == "off":
        return "not_scheduled", "policy_off"
    control = session.scalar(select(WorkloadControl).where(WorkloadControl.name == "processing"))
    if control is None:
        return "blocked", "processing_control_missing"
    if not control.enabled:
        return "not_scheduled", "processing_paused"
    schedule = WorkloadSchedule.model_validate(control.schedule)
    if not schedule.allows(now):
        return "waiting_window", "processing_window_closed"
    if not policy.source_ids:
        return "blocked", "no_approved_source_scope"
    return "ready", policy.mode
