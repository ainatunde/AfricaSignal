"""The reference set: case schema, loading and the checks that keep the set honest (AS-040).

A case is one line of a ``eval/cases/*.jsonl`` file. It holds the inputs (documents, measurements
and, for documents read by the language model, the answer the model gave) and the gold labels a
person decided on. ``check_set`` refuses a set whose splits leak (near-duplicate documents in
different splits) or whose cases refer to things that do not exist.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from africasignal.evidence.origins import SIMHASH_MAX_DISTANCE
from africasignal.evidence.simhash import hamming_distance, simhash
from eval.sources import OFFICIAL_KINDS, SOURCES

CASES_DIR = Path(__file__).resolve().parent / "cases"

Template = Literal["T1", "T2"]
Split = Literal["dev", "test"]
EvidenceStateName = Literal["reported", "corroborated", "disputed", "insufficient", "none"]
SeverityName = Literal["none", "low", "medium", "high"]
# What the pipeline did with the assessment: published in full, published as an "insufficient
# evidence" card (R3, R5), held for operator review (R7), withheld, or made no assessment at all.
DecisionName = Literal["publish", "publish_insufficient", "hold", "withhold", "none"]
# "synthetic_seed": written by the build, in its own words, as a starting set and a regression
# test. "editorial": labelled by the editorial owner (plan D2) from real documents.
GoldSource = Literal["synthetic_seed", "editorial"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimAnswer(Model):
    """One claim as the language model returns it (the schema in spec B7)."""

    claim_type: Literal["price_statement", "policy_statement", "other"]
    text: str
    passage: str
    item_code: str | None = None
    policy_series: str | None = None
    stated_value: float | None = None
    stated_unit: str | None = None
    direction: Literal["up", "down", "unchanged", "unknown"] = "unknown"
    occurred_from: str | None = None
    occurred_to: str | None = None
    time_precision: Literal["day", "month", "year", "unknown"] = "unknown"
    place_candidates: list[str] = Field(default_factory=list)


class DocumentSpec(Model):
    key: str
    source: str  # a key of ``eval.pipeline.SOURCES``
    title: str
    published: dt.datetime
    text: str | None = None
    copy_of: str | None = None  # the text of another document of the case, plus ``append``
    append: str = ""
    withdrawn: bool = False  # the document was withdrawn after the claims were made (R2)
    # Whether the model reads this document. Official datasets (NBS) are parsed by code, so
    # the default is False for them and True for everything else.
    extract: bool | None = None
    # The stand-in answer: what a model is taken to have said. Used by offline runs only.
    answer: list[ClaimAnswer] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_text(self) -> DocumentSpec:
        if (self.text is None) == (self.copy_of is None):
            raise ValueError(f"document {self.key}: give exactly one of text and copy_of")
        return self


class MeasurementSpec(Model):
    item: str
    place: str  # place code
    period: str  # YYYY-MM
    value: float
    vintage: dt.date  # publication date of the release
    document: str
    review_pending: bool = False  # failed range validation: waits in the review queue (R4)


class SituationSpec(Model):
    place: str
    item: str | None = None  # T1
    series: str | None = None  # T2

    @model_validator(mode="after")
    def _one_kind(self) -> SituationSpec:
        if (self.item is None) == (self.series is None):
            raise ValueError("a situation names an item (T1) or a series (T2)")
        return self


class ExplanationSpec(Model):
    """Stand-in answers the model gave when asked to explain the assessment, in order."""

    answers: list[str]
    expect: Literal["accepted", "accepted_after_retry", "none"]


class GdeltRow(Model):
    """A row of a GDELT events file, reduced to the columns the Nigeria filter reads."""

    action_geo_country: str = ""  # FIPS 10-4: Nigeria is NI, NG is Niger
    actor1_country: str = ""  # CAMEO / ISO-3: Nigeria is NGA
    actor2_country: str = ""
    keep: bool


class ExpectedClaim(Model):
    doc: str
    passage: str  # how a claim is recognised, in the model's answer or the gold set
    valid: bool
    reason: str | None = None  # the invalid_reason the validator gives
    # The place the claim resolves to: a place code, "ambiguous" or "unknown". None = not checked.
    place: str | None = None


class Expected(Model):
    decision: DecisionName
    evidence_state: EvidenceStateName
    severity: SeverityName
    place: str  # the situation's scope
    # T1: first day of the assessed month. T2: the day the current rate takes effect.
    # None: the assessment says no date is stated.
    date: dt.date | None = None
    # Fact label -> value, for every number the case cares about (must all be exact).
    numbers: dict[str, float] = Field(default_factory=dict)
    rules: list[str] | None = None  # publication rule ids that shaped the decision
    # Codes of the possible factors a claim supports (T1). None = not checked.
    supported_factors: list[str] | None = None
    claims: list[ExpectedClaim] = Field(default_factory=list)


class Case(Model):
    id: str
    template: Template
    split: Split
    origin_group: str  # near-duplicate families never cross splits
    tags: list[str] = Field(default_factory=list)
    gold: GoldSource = "synthetic_seed"
    now: dt.datetime
    situation: SituationSpec
    documents: list[DocumentSpec] = Field(default_factory=list)
    measurements: list[MeasurementSpec] = Field(default_factory=list)
    publication_suspended: bool = False
    explanation: ExplanationSpec | None = None
    gdelt_rows: list[GdeltRow] = Field(default_factory=list)
    expected: Expected

    def document_text(self, key: str) -> str:
        docs = {d.key: d for d in self.documents}
        doc = docs[key]
        if doc.text is not None:
            return doc.text
        assert doc.copy_of is not None
        return self.document_text(doc.copy_of) + doc.append


class CaseSetError(ValueError):
    """The reference set is not usable; the message lists every problem."""


def load_cases(directory: Path = CASES_DIR) -> list[Case]:
    """Every case in ``directory/*.jsonl`` (blank lines and ``#`` comment lines are skipped)."""
    cases: list[Case] = []
    for path in sorted(directory.glob("*.jsonl")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            try:
                cases.append(Case.model_validate(json.loads(line)))
            except (ValueError, json.JSONDecodeError) as exc:
                raise CaseSetError(f"{path.name}:{number}: {exc}") from exc
    return cases


def check_set(cases: list[Case]) -> list[str]:
    """Problems that make a set unfit to measure with (empty list: fine)."""
    problems: list[str] = []
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            problems.append(f"duplicate case id {case.id}")
        seen.add(case.id)
        keys = [d.key for d in case.documents]
        if len(keys) != len(set(keys)):
            problems.append(f"{case.id}: duplicate document keys")
        for doc in case.documents:
            if doc.source not in SOURCES:
                problems.append(f"{case.id}: unknown source {doc.source!r}")
            if doc.copy_of is not None and doc.copy_of not in keys:
                problems.append(f"{case.id}: {doc.key} copies unknown document {doc.copy_of}")
        for m in case.measurements:
            if m.document not in keys:
                problems.append(f"{case.id}: measurement refers to unknown document {m.document}")
        for c in case.expected.claims:
            if c.doc not in keys:
                problems.append(f"{case.id}: expected claim refers to unknown document {c.doc}")
        if (case.template == "T1") != (case.situation.item is not None):
            problems.append(f"{case.id}: template {case.template} does not match the situation")

    # A reporting-origin family (for example one wire story and its reprints) lives in one split.
    splits_by_group: dict[str, set[str]] = defaultdict(set)
    for case in cases:
        splits_by_group[case.origin_group].add(case.split)
    for group, splits in sorted(splits_by_group.items()):
        if len(splits) > 1:
            problems.append(f"origin group {group} is in more than one split: {sorted(splits)}")

    # Near-duplicates must not sit in different splits even if nobody put them in one group.
    hashes: list[tuple[int, str, str]] = []  # (simhash, split, case id)
    for case in cases:
        for doc in case.documents:
            if SOURCES[doc.source].kind in OFFICIAL_KINDS:
                continue  # official documents never cluster by text (evidence.origins)
            value = simhash(case.document_text(doc.key))
            if value is not None:
                hashes.append((value, case.split, case.id))
    for i, (h1, s1, c1) in enumerate(hashes):
        for h2, s2, c2 in hashes[i + 1 :]:
            if s1 != s2 and hamming_distance(h1, h2) <= SIMHASH_MAX_DISTANCE:
                problems.append(f"near-duplicate documents in {c1} ({s1}) and {c2} ({s2})")
    return problems
