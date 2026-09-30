"""Run one case through the real assessment code (AS-040).

Nothing here re-implements a rule. A case's documents are stored and given reporting origins
(``evidence.origins``), the model's claims are validated and stored (``extract``), places are
resolved (``places``), measurements are stored with their revision rules (``sources.nbs``), then
the situation is assessed (``publish.situations`` or ``publish.policy_situations``) and the
publication policy decides (``publish.versions``). The harness compares what comes out with the
labels. Each case runs in its own transaction, which is rolled back, so cases cannot affect each
other and a database is left as it was found.

The model is reached only through ``LlmAdapter``. Where the answers come from is decided by
``ModelMode``: hand-written stand-ins (offline, the default), recordings of real answers, or the
real model (an explicit opt-in; ``harness.py`` refuses politely without a key).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.assess.explain import build_input, generate_explanation, needs_explanation
from africasignal.assess.publication_policy import Decision, precision_of
from africasignal.catalog import Item, load_items
from africasignal.evidence.origins import assign_origin
from africasignal.evidence.simhash import simhash
from africasignal.extract.claims import (
    Extraction,
    extract_claims,
    extractor_version,
    keyword_hits,
    store_claims,
)
from africasignal.jobs.handlers.resolve_places import dominant_state
from africasignal.llm import LlmAdapter, LlmProvider
from africasignal.llm.fake import FakeProvider
from africasignal.models import (
    AssessmentInput,
    AssessmentVersion,
    Claim,
    EvidenceDocument,
    Measurement,
    MeasurementReview,
    Place,
    Series,
    Source,
)
from africasignal.places.load import load_aliases, load_boundaries
from africasignal.places.resolve import resolve_candidates
from africasignal.publish.policy_situations import ensure_policy_situations
from africasignal.publish.situations import (
    assess_situation,
    ensure_situations,
    place_and_descendants,
)
from africasignal.publish.versions import apply_policy, set_publication_suspended
from africasignal.sources.gdelt import (
    EXP_ACTION_COUNTRY,
    EXP_ACTOR1_COUNTRY,
    EXP_ACTOR2_COUNTRY,
    EXPORT_COLUMNS,
    is_nigeria_event,
)
from africasignal.sources.nbs import store_measurement
from africasignal.sources.nerc import reconcile_tariff_claims
from eval.cases import Case, DocumentSpec
from eval.sources import CODE_READ_KINDS, SOURCES

log = logging.getLogger("eval.pipeline")

PLACES_DIR = Path(__file__).resolve().parent / "data" / "places"
_RANK = {"national": 0, "state": 1, "lga": 2, "city": 3, "unknown": 4}

ModelMode = Literal["stand_in", "recorded", "live"]
Provenance = Literal["none", "stand_in", "recorded", "live"]


# --- results -----------------------------------------------------------------------------------


@dataclass
class ClaimOutcome:
    doc: str
    passage: str
    valid: bool
    reason: str | None
    place: str | None  # a place code, "ambiguous" or "unknown"; None when it has no place text


@dataclass
class VersionSnapshot:
    evidence_state: str
    severity: str
    headline: str
    period_label: str
    scope_label: str
    facts: list[dict[str, Any]]
    unknowns: list[str]
    possible_factors: list[str]  # codes of the supported factors


@dataclass
class ExplanationOutcome:
    outcome: Literal["accepted", "accepted_after_retry", "none"]
    attempts: int
    problems: list[list[str]]
    text: str | None


@dataclass
class CaseOutcome:
    case_id: str
    decision: str = "none"
    rules: list[str] = field(default_factory=list)
    scope_place: str | None = None
    version: VersionSnapshot | None = None
    claims: list[ClaimOutcome] = field(default_factory=list)
    unexpected_valid_claims: int = 0  # claims the model made that the labels do not list
    explanation: ExplanationOutcome | None = None
    gdelt: list[bool] = field(default_factory=list)  # did the Nigeria filter keep each row
    finer_than_evidence: list[str] = field(default_factory=list)  # problems, empty when fine
    provenance: Provenance = "none"  # where the model's answers came from
    model_calls: int = 0
    error: str | None = None


# --- where the model's answers come from -------------------------------------------------------


class Recorder:
    """Collects the raw replies of a live run so a later offline run can replay them."""

    def __init__(self) -> None:
        self.documents: dict[str, list[str]] = {}
        self.explanations: dict[str, list[str]] = {}

    def to_json(self, meta: dict[str, Any]) -> dict[str, Any]:
        return {"meta": meta, "documents": self.documents, "explanations": self.explanations}


class _RecordingProvider:
    """Wraps a real provider and notes every reply under ``bucket[key]``."""

    def __init__(self, inner: LlmProvider, bucket: dict[str, list[str]], key: str) -> None:
        self.inner, self.bucket, self.key = inner, bucket, key

    def complete(self, **kwargs: Any) -> Any:
        reply = self.inner.complete(**kwargs)
        self.bucket.setdefault(self.key, []).append(reply.text)
        return reply


@dataclass
class Models:
    """How one run obtains answers from the model.

    ``recorded`` holds replies saved by an earlier live run (``--answers``); a document or case
    it does not cover falls back to the case's hand-written stand-in.
    """

    mode: ModelMode = "stand_in"
    live_provider: Callable[[Session], LlmProvider] | None = None
    recorded: dict[str, Any] = field(default_factory=dict)
    recorder: Recorder | None = None
    max_usd: float = 5.0
    # Live runs explain every assessment that could be explained; offline runs only those a case
    # has stand-in answers for.
    explain_all: bool = False

    def _adapter(self, session: Session, provider: LlmProvider, now: datetime) -> LlmAdapter:
        return LlmAdapter(
            session,
            provider,
            daily_budget_usd=self.max_usd if self.mode == "live" else 1_000.0,
            per_job_max_tokens=1_000_000,
            clock=lambda: now,
        )

    def for_document(
        self, session: Session, case: Case, doc: DocumentSpec
    ) -> tuple[LlmAdapter, Provenance]:
        key = f"{case.id}/{doc.key}"
        if self.mode == "live":
            assert self.live_provider is not None
            provider: LlmProvider = self.live_provider(session)
            if self.recorder is not None:
                provider = _RecordingProvider(provider, self.recorder.documents, key)
            return self._adapter(session, provider, case.now), "live"
        replies = self.recorded.get("documents", {}).get(key)
        if self.mode == "recorded" and replies is not None:
            return self._adapter(session, FakeProvider(list(replies)), case.now), "recorded"
        answer = {"claims": [c.model_dump() for c in doc.answer]}
        return self._adapter(session, FakeProvider(json.dumps(answer)), case.now), "stand_in"

    def for_explanation(self, session: Session, case: Case) -> tuple[LlmAdapter, Provenance] | None:
        if self.mode == "live":
            assert self.live_provider is not None
            provider: LlmProvider = self.live_provider(session)
            if self.recorder is not None:
                provider = _RecordingProvider(provider, self.recorder.explanations, case.id)
            return self._adapter(session, provider, case.now), "live"
        replies = self.recorded.get("explanations", {}).get(case.id)
        if self.mode == "recorded" and replies is not None:
            return self._adapter(session, FakeProvider(list(replies)), case.now), "recorded"
        if case.explanation is None:
            return None
        answers = [json.dumps({"explanation": a}) for a in case.explanation.answers]
        # An answer list that runs out raises in FakeProvider; the pipeline records that as an
        # error for the case, which is what a mislabelled stand-in deserves.
        return self._adapter(session, FakeProvider(answers), case.now), "stand_in"


# --- setting up a case -------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _boundary_files() -> dict[str, Any]:
    return {
        level: json.loads((PLACES_DIR / f"adm{i}.geojson").read_text(encoding="utf-8"))
        for i, level in enumerate(("ADM0", "ADM1", "ADM2"))
    }


def load_places(session: Session) -> dict[str, int]:
    """The country, 17 states and 22 LGAs of the fixture cut (see ``eval/data/places``), with
    the aliases of ``config/place_aliases.yaml``. Returns place id by code."""
    load_boundaries(session, _boundary_files(), "eval-fixture", expected_counts=None)
    load_aliases(session)
    session.flush()
    return {code: pid for pid, code in session.execute(select(Place.id, Place.code)) if code}


def _source(session: Session, cache: dict[str, Source], key: str) -> Source:
    if key not in cache:
        if key not in SOURCES:
            raise KeyError(f"unknown source {key!r}; known: {sorted(SOURCES)}")
        d = SOURCES[key]
        src = Source(
            slug=key,
            name=d.name,
            owner=d.owner,
            kind=d.kind,
            adapter=d.adapter,
            schedule_minutes=60,
        )
        session.add(src)
        session.flush()
        cache[key] = src
    return cache[key]


def _store_documents(
    session: Session, case: Case, sources: dict[str, Source]
) -> dict[str, EvidenceDocument]:
    """Store every document, oldest first (the order a crawler would see them), and cluster."""
    stored: dict[str, EvidenceDocument] = {}
    order = sorted(
        case.documents, key=lambda d: (d.published, [x.key for x in case.documents].index(d.key))
    )
    for spec in order:
        text = case.document_text(spec.key)
        url = f"https://{spec.source}.example/{case.id}/{spec.key}"
        doc = EvidenceDocument(
            source_id=_source(session, sources, spec.source).id,
            url=url,
            canonical_url=url,
            retrieved_at=spec.published,
            published_at=spec.published,
            content_sha256=hashlib.sha256(text.encode()).hexdigest(),
            storage_key=f"eval/{case.id}/{spec.key}",
            mime="text/html",
            title=spec.title,
            text_content=text,
            simhash=simhash(text),
        )
        session.add(doc)
        session.flush()
        assign_origin(session, doc)
        stored[spec.key] = doc
    return stored


def _series(session: Session, source: Source, item: Item) -> Series:
    series = session.scalars(
        select(Series).where(Series.item_code == item.code, Series.source_id == source.id)
    ).one_or_none()
    if series is None:
        series = Series(
            item_code=item.code,
            topic=item.topic,
            source_id=source.id,
            unit=item.unit,
            currency=item.currency,
            frequency=item.frequency,
        )
        session.add(series)
        session.flush()
    return series


def _store_measurements(
    session: Session,
    case: Case,
    docs: dict[str, EvidenceDocument],
    places: dict[str, int],
    sources: dict[str, Source],
) -> None:
    catalog = load_items()
    for m in sorted(case.measurements, key=lambda m: (m.vintage, m.period, m.place)):
        year, month = (int(p) for p in m.period.split("-"))
        start = date(year, month, 1)
        doc = docs[m.document]
        source = sources[next(d.source for d in case.documents if d.key == m.document)]
        series = _series(session, source, catalog.item(m.item))
        if m.review_pending:
            nxt = date(year + (month == 12), month % 12 + 1, 1)
            session.add(
                MeasurementReview(
                    series_id=series.id,
                    place_id=places[m.place],
                    period_start=start,
                    period_end=date.fromordinal(nxt.toordinal() - 1),
                    value=Decimal(str(m.value)),
                    vintage=m.vintage,
                    evidence_document_id=doc.id,
                    reference_value=Decimal(str(m.value)) / 3,
                    reason="outside the accepted range of the previous month's median (eval case)",
                )
            )
            session.flush()
            continue
        store_measurement(
            session, series, places[m.place], start, Decimal(str(m.value)), m.vintage, doc
        )


def _extract_and_place(
    session: Session,
    case: Case,
    docs: dict[str, EvidenceDocument],
    models: Models,
    out: CaseOutcome,
) -> None:
    """What the ``extract_claims`` and ``resolve_places`` jobs do, for every document the model
    reads: ask (or replay), validate, store, resolve the claims' places."""
    provenances: set[str] = set()
    version = extractor_version()
    for spec in case.documents:
        source_kind = SOURCES[spec.source].kind
        wants_model = (
            spec.extract if spec.extract is not None else source_kind not in CODE_READ_KINDS
        )
        doc = docs[spec.key]
        text = case.document_text(spec.key)
        if not wants_model or not keyword_hits(text):
            continue  # the job does not extract a document without topic keywords
        adapter, provenance = models.for_document(session, case, spec)
        provenances.add(provenance)
        extraction = extract_claims(adapter, doc, text)
        out.model_calls += 1
        stored = store_claims(
            session, doc, Extraction(claims=extraction.claims, window=extraction.window), version
        )
        reconcile_tariff_claims(session, doc, text)  # code, not the model, decides a tariff
        session.flush()
        for claim in stored:
            session.refresh(claim)

        valid = [c for c in stored if c.valid and c.place_id is None]
        context_state = dominant_state(session, valid)
        placed: dict[int, str] = {}
        for claim in valid:
            if not claim.place_candidates:
                continue
            answer = resolve_candidates(
                session,
                [str(c) for c in claim.place_candidates],
                document_state_id=context_state,
            )
            if answer.status == "resolved" and answer.place is not None:
                claim.place_id = answer.place.id
                claim.place_precision = answer.precision
                placed[claim.id] = answer.place.code or "unknown"
            else:
                placed[claim.id] = answer.status  # "ambiguous" or "unknown"
        session.flush()
        for claim in stored:
            out.claims.append(
                ClaimOutcome(
                    doc=spec.key,
                    passage=claim.passage,
                    valid=claim.valid,
                    reason=claim.invalid_reason,
                    place=placed.get(claim.id),
                )
            )
    if provenances:
        order = ("live", "recorded", "stand_in")
        out.provenance = next(p for p in order if p in provenances)  # type: ignore[assignment]


def _withdraw(session: Session, case: Case, docs: dict[str, EvidenceDocument]) -> None:
    for spec in case.documents:
        if spec.withdrawn:
            docs[spec.key].status = "withdrawn"
    session.flush()


# --- assessing ---------------------------------------------------------------------------------


def _snapshot(version: AssessmentVersion) -> VersionSnapshot:
    return VersionSnapshot(
        evidence_state=version.evidence_state,
        severity=version.severity,
        headline=version.headline,
        period_label=version.period_label,
        scope_label=version.scope_label,
        facts=list(version.facts),
        unknowns=[str(u) for u in version.unknowns],
        possible_factors=[
            str(f.get("code")) for f in version.possible_factors if f.get("status") == "supported"
        ],
    )


def _decision_name(decision: Decision | None) -> str:
    if decision is None:
        return "none"
    match decision.status:
        case "published":
            return "publish_insufficient" if decision.insufficient_card else "publish"
        case "held":
            return "hold"
        case "withheld":
            return "withhold"
        case _:
            return "none"  # "unchanged": a first assessment cannot be that


def _finer_than_evidence(
    session: Session, version: AssessmentVersion, scope: Place, template: str
) -> list[str]:
    """Reasons a version that readers can see says more about a place than its evidence does."""
    problems: list[str] = []
    scope_rank = _RANK[precision_of(scope.code or "")]
    for fact in version.facts:
        code = str(fact.get("place_code", ""))
        if _RANK[precision_of(code)] > scope_rank:
            problems.append(f"fact {fact.get('label')!r} is about {code}, finer than {scope.code}")
    inside = place_and_descendants(session, scope)
    inputs = session.execute(
        select(AssessmentInput.input_kind, AssessmentInput.input_id).where(
            AssessmentInput.assessment_version_id == version.id
        )
    ).all()
    for kind, input_id in inputs:
        if kind == "measurement":
            m = session.get(Measurement, input_id)
            if m is not None and m.place_id not in inside:
                problems.append(f"measurement {input_id} is for a place outside {scope.code}")
        elif kind == "claim" and template == "T1":
            c = session.get(Claim, input_id)
            if c is not None and c.place_id not in inside:
                problems.append(f"claim {input_id} is not about {scope.code} or a place inside it")
    return problems


def _assess(
    session: Session, case: Case, places: dict[str, int], out: CaseOutcome
) -> AssessmentVersion | None:
    spec = case.situation
    scope_id = places[spec.place]
    if spec.item is not None:
        situations = ensure_situations(session, {(spec.item, scope_id)})
    else:
        assert spec.series is not None
        situations = [
            s for s in ensure_policy_situations(session, {spec.series}) if s.place_id == scope_id
        ]
    if not situations:
        return None
    outcome = assess_situation(session, situations[0].id, case.now)
    if outcome.version is None:
        return None
    if case.publication_suspended:
        set_publication_suspended(session, True, case.now)
    decision = apply_policy(session, outcome.version.id, case.now)
    out.decision = _decision_name(decision)
    out.rules = list(decision.reasons) if decision else []
    return outcome.version


def _explain(
    session: Session, case: Case, version: AssessmentVersion, models: Models, out: CaseOutcome
) -> None:
    if not needs_explanation(version):
        return
    if case.explanation is None and not models.explain_all:
        return
    got = models.for_explanation(session, case)
    if got is None:
        return
    adapter, provenance = got
    result = generate_explanation(adapter, build_input(session, version))
    out.model_calls += result.attempts
    outcome: Literal["accepted", "accepted_after_retry", "none"]
    if result.text is None:
        outcome = "none"
    else:
        outcome = "accepted" if result.attempts == 1 else "accepted_after_retry"
    out.explanation = ExplanationOutcome(outcome, result.attempts, result.problems, result.text)
    if out.provenance == "none" or provenance == "live":
        out.provenance = provenance


def _gdelt(case: Case, out: CaseOutcome) -> None:
    for row in case.gdelt_rows:
        cells = [""] * EXPORT_COLUMNS
        cells[EXP_ACTION_COUNTRY] = row.action_geo_country
        cells[EXP_ACTOR1_COUNTRY] = row.actor1_country
        cells[EXP_ACTOR2_COUNTRY] = row.actor2_country
        out.gdelt.append(is_nigeria_event(cells))


def run_case(session: Session, case: Case, models: Models) -> CaseOutcome:
    """Run one case inside ``session`` (the caller rolls back). Errors are caught by the caller."""
    out = CaseOutcome(case_id=case.id)
    places = load_places(session)
    sources: dict[str, Source] = {}
    docs = _store_documents(session, case, sources)
    _store_measurements(session, case, docs, places, sources)
    _extract_and_place(session, case, docs, models, out)
    _withdraw(session, case, docs)
    _gdelt(case, out)

    version = _assess(session, case, places, out)
    scope = session.scalars(select(Place).where(Place.code == case.situation.place)).one()
    out.scope_place = scope.code
    if version is not None:
        session.refresh(version)
        out.version = _snapshot(version)
        if out.decision in ("publish", "publish_insufficient", "hold"):
            out.finer_than_evidence = _finer_than_evidence(session, version, scope, case.template)
        _explain(session, case, version, models, out)
    return out


def case_error(case: Case, exc: BaseException) -> CaseOutcome:
    return CaseOutcome(case_id=case.id, error=f"{type(exc).__name__}: {exc}")


__all__ = [
    "CaseOutcome",
    "ClaimOutcome",
    "ExplanationOutcome",
    "ModelMode",
    "Models",
    "Recorder",
    "VersionSnapshot",
    "case_error",
    "load_places",
    "run_case",
]
