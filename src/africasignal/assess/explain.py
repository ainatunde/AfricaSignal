"""Explanation text for an assessment version (spec B8.4, AS-028).

The language model is given only what code already computed (the facts, the possible factors, the
unknowns, the scope and the period) and writes a "Why this matters" paragraph of at most 90 words.
Nothing it writes is used until ``validate_explanation`` has checked it in code:

* every number in the text must be one the facts state, at the same rounding (``₦1,005.47``,
  ``3.2%``, ``N1,005``), and spelled-out numbers ("two", "half") are refused;
* no place name outside the scope, its parents and the places named in the facts;
* none of the words in ``BANNED_WORDS`` ("confirmed", "will", "caused by", ...) unless some
  possible factor is ``supported``;
* at most ``MAX_WORDS`` words, no links and no markup.

A rejected answer is retried once with the validator's complaints appended. After a second failure
the version simply has no explanation and the page shows the facts alone. Nothing here can stop a
version from being published: ``explain_version`` is called after the publication policy has run.

The same facts, prompt and model give the same cache key in the response cache (``llm_cache``), so
an unchanged version is never explained twice. The plan asks for temperature 0; the provider
interface has no temperature setting (``output_config`` only takes ``effort``), so repeatability
comes from that cache, and the validator does not depend on it.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.llm.adapter import LlmAdapter
from africasignal.llm.errors import (
    JobTokenLimitExceeded,
    ProviderRefused,
    SchemaValidationError,
)
from africasignal.llm.prompt_loader import load_prompt, render
from africasignal.models import AssessmentVersion, Place, PlaceAlias, Situation
from africasignal.publish.factfmt import allowed_numbers

log = logging.getLogger("africasignal.assess.explain")

PURPOSE = "explain"
PROMPT_NAME = "explain_v1"
PROMPT_VERSION = PROMPT_NAME
MAX_WORDS = 90
MAX_OUTPUT_TOKENS = 1_500  # the paragraph is about 130 tokens; the rest is thinking headroom
MAX_ATTEMPTS = 2  # the first answer, then one retry with the validator's complaints

# Claims of certainty or cause. Allowed only when a possible factor is ``supported`` (B8.4). The
# first four are the plan's list; the rest are other ways of stating a cause or a forecast.
BANNED_WORDS: tuple[str, ...] = (
    "confirmed",
    "will",
    "definitely",
    "caused by",
    "certainly",
    "because of",
    "due to",
    "as a result of",
    "driven by",
    "led to",
    "resulted in",
)

# Numbers written as words. "one" is left out on purpose: it is ordinary English ("no one", "one
# of"), and a wrong "one" is not a number the page could show.
_NUMBER_WORDS = (
    "two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred "
    "hundreds thousand thousands million millions billion billions dozen dozens half twice double "
    "doubled triple tripled halved"
).split()

_DIGITS = re.compile(r"(?<!\d)(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?")
_NUMBER_WORD = re.compile(r"(?<!\w)(" + "|".join(_NUMBER_WORDS) + r")(?!\w)", re.IGNORECASE)
_MARKUP = re.compile(r"https?:|www\.|@|[<>`*]|\[[^\]]*\]\(")


@dataclass(frozen=True)
class ExplainInput:
    """Everything the model sees, and everything the validator checks it against."""

    situation_title: str
    template: str
    scope_label: str
    period_label: str
    evidence_state: str
    severity: str
    headline: str
    facts: list[dict[str, Any]]
    possible_factors: list[dict[str, Any]]
    unknowns: list[str]
    parent_places: tuple[str, ...] = ()  # names of the scope's ancestors, for example Nigeria
    known_places: frozenset[str] = frozenset()  # every place name the validator looks for

    def payload(self) -> dict[str, Any]:
        """The JSON the model receives. Evidence ids are dropped: they are numbers that mean
        nothing to a reader and must not be repeated."""
        return {
            "situation": self.situation_title,
            "template": self.template,
            "scope": self.scope_label,
            "period": self.period_label,
            "evidence_state": self.evidence_state,
            "severity": self.severity,
            "headline": self.headline,
            "facts": [
                {
                    "label": f.get("label"),
                    "value": f.get("value"),
                    "unit": f.get("unit"),
                    "period": f.get("period"),
                    "source": f.get("source_label"),
                }
                for f in self.facts
            ],
            "possible_factors": [
                {"factor": f.get("factor"), "status": f.get("status")}
                for f in self.possible_factors
            ],
            "unknowns": list(self.unknowns),
        }

    def payload_text(self) -> str:
        return json.dumps(self.payload(), ensure_ascii=False, sort_keys=True, indent=2)

    @property
    def has_supported_factor(self) -> bool:
        return any(f.get("status") == "supported" for f in self.possible_factors)


# --- the validator -----------------------------------------------------------------------------


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def numbers_written(text: str) -> list[str]:
    """Every number in a text as written, digits only, commas kept: ``N1,005.47`` gives
    ``1,005.47``. Unlike ``factfmt.numbers_in`` this also finds digits glued to letters
    ("N1,020", "12th", "5kg"), because a number must not slip through by its spelling."""
    return [m.group(1) + (m.group(2) or "") for m in _DIGITS.finditer(text)]


def permitted_numbers(data: ExplainInput) -> set[str]:
    """The spellings of every number the facts state, plus the digits of the period, unit, source
    and scope text and of the unknowns (code-written sentences such as "more than 120 days ago")."""
    allowed = allowed_numbers(data.facts)
    text_fields = [
        data.period_label,
        data.scope_label,
        *_strings([{k: f.get(k) for k in ("period", "unit", "source_label")} for f in data.facts]),
        *data.unknowns,
    ]
    for text in text_fields:
        allowed |= set(numbers_written(text))
    return allowed


def _mentions(text: str, names: Iterable[str]) -> set[str]:
    """The names that appear in the text as whole words, matching capitalisation."""
    return {
        name for name in names if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text) is not None
    }


def permitted_places(data: ExplainInput) -> set[str]:
    """The scope's ancestors and every known place named anywhere in the input."""
    in_input = _mentions(" ".join(_strings(data.payload())), data.known_places)
    return in_input | set(data.parent_places)


# Characters other than ASCII that an explanation may contain: the naira sign and typographic
# punctuation. Anything else (Cyrillic or Greek look-alikes, for example) could be used to spell a
# banned word or place name in a way the checks do not see.
_EXTRA_ALLOWED = frozenset("₦£°’‘“”–—…")


def clean_text(text: str) -> str:
    """The text as the validator reads it, and as it is stored: Unicode NFKC (full-width digits
    and letters become plain ones, a no-break space becomes a space), invisible format characters
    removed (zero-width spaces and joiners, soft hyphens, bidi marks), runs of spaces and line
    breaks inside a paragraph collapsed to one space. A blank line between paragraphs is kept so
    the one-paragraph rule can see it."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    paragraphs = [" ".join(p.split()) for p in re.split(r"\n[^\S\n]*\n\s*", text.strip())]
    return "\n\n".join(p for p in paragraphs if p)


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """A banned phrase, matched whole, however its words are separated (spaces, hyphens)."""
    words = r"[\s\-\u2010-\u2015]+".join(re.escape(w) for w in phrase.split())
    return re.compile(rf"(?<!\w){words}(?!\w)", re.IGNORECASE)


_BANNED_PATTERNS = {phrase: _phrase_pattern(phrase) for phrase in BANNED_WORDS}


def validate_explanation(text: str, data: ExplainInput) -> list[str]:
    """Why ``text`` may not be published, as messages the model can act on. Empty = it passes.
    The checks run on ``clean_text(text)``, so spacing, invisible characters and full-width forms
    cannot hide a number, a place or a banned phrase."""
    problems: list[str] = []
    cleaned = clean_text(text)
    if not cleaned:
        return ["The explanation is empty."]
    flat = " ".join(cleaned.split())  # one line: what the word, number and place checks read

    words = len(flat.split())
    if words > MAX_WORDS:
        problems.append(f"It has {words} words; the limit is {MAX_WORDS}.")
    if "\n\n" in cleaned:
        problems.append("It must be one paragraph.")
    if _MARKUP.search(flat):
        problems.append("It must be plain text: no links, markup or @ signs.")
    odd = sorted({ch for ch in flat if ord(ch) > 127 and ch not in _EXTRA_ALLOWED})
    if odd:
        problems.append(
            "It contains unexpected characters ("
            + " ".join(f"U+{ord(ch):04X}" for ch in odd[:5])
            + "); use plain English letters."
        )

    allowed = permitted_numbers(data)
    bad_numbers = [n for n in dict.fromkeys(numbers_written(flat)) if n not in allowed]
    for number in bad_numbers:
        problems.append(
            f"The number {number} is not a figure in the facts (or is rounded differently)."
        )
    for word in dict.fromkeys(m.group(1).lower() for m in _NUMBER_WORD.finditer(flat)):
        problems.append(
            f'The number word "{word}" is not allowed; use only figures from the facts.'
        )

    allowed_places = permitted_places(data)
    outside = sorted(_mentions(flat, data.known_places) - allowed_places)
    for name in outside:
        problems.append(f"The place {name} is outside this situation's scope; do not mention it.")

    if not data.has_supported_factor:
        for phrase, pattern in _BANNED_PATTERNS.items():
            if pattern.search(flat.casefold()):
                problems.append(
                    f'The wording "{phrase}" states certainty or a cause, and no possible factor '
                    "is supported."
                )
    return problems


# --- asking the model --------------------------------------------------------------------------


def explanation_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"explanation": {"type": "string"}},
        "required": ["explanation"],
        "additionalProperties": False,
    }


def system_prompt() -> str:
    return render(load_prompt(PROMPT_NAME), BANNED_WORDS=", ".join(f'"{w}"' for w in BANNED_WORDS))


def _user_message(data: ExplainInput, rejected: tuple[str, list[str]] | None = None) -> str:
    message = f"BEGIN INPUT\n{data.payload_text()}\nEND INPUT"
    if rejected is not None:
        previous, problems = rejected
        message += (
            "\n\nYour previous answer was rejected by the checks.\n"
            f"Previous answer: {previous}\n"
            "Problems:\n" + "\n".join(f"- {p}" for p in problems) + "\n"
            "Write a new paragraph that fixes every problem and follows all the rules."
        )
    return message


@dataclass
class ExplainResult:
    """``text`` is the validated paragraph, or None when both attempts failed."""

    text: str | None
    model_id: str
    attempts: int = 0
    problems: list[list[str]] = field(default_factory=list)  # per failed attempt


def generate_explanation(
    adapter: LlmAdapter, data: ExplainInput, *, job_id: int | None = None
) -> ExplainResult:
    """Ask for an explanation, validate it and retry once. Raises ``BudgetExhausted`` (nothing
    more can be asked today) and lets other setup errors through; a model that answers badly is
    not an error, it gives ``text=None``."""
    result = ExplainResult(text=None, model_id=adapter.model_for(PURPOSE))
    system = system_prompt()
    rejected: tuple[str, list[str]] | None = None
    for _ in range(MAX_ATTEMPTS):
        result.attempts += 1
        try:
            answer = adapter.complete_json(
                PURPOSE,
                PROMPT_VERSION,
                system,
                _user_message(data, rejected),
                explanation_schema(),
                MAX_OUTPUT_TOKENS,
                job_id=job_id,
            )
        except (SchemaValidationError, ProviderRefused) as exc:
            problems = [str(exc)]
            rejected = ("(no usable answer)", problems)
        except JobTokenLimitExceeded as exc:
            result.problems.append([str(exc)])
            return result
        else:
            text = clean_text(str(answer["explanation"]))  # what is checked is what is stored
            problems = validate_explanation(text, data)
            if not problems:
                result.text = text
                return result
            rejected = (text, problems)
        result.problems.append(problems)
    return result


# --- versions in the database ------------------------------------------------------------------

Outcome = Literal["explained", "failed", "skipped"]


def _ancestors(session: Session, place: Place) -> list[str]:
    names: list[str] = []
    seen = {place.id}
    parent_id = place.parent_id
    while parent_id is not None and parent_id not in seen:
        parent = session.get(Place, parent_id)
        if parent is None:
            break
        names.append(parent.name)
        seen.add(parent.id)
        parent_id = parent.parent_id
    return names


def known_place_names(session: Session) -> frozenset[str]:
    """Every place name and alias the validator looks for. Names under four characters and
    lowercase aliases are left out: they would match ordinary words."""
    names = set(session.scalars(select(Place.name)))
    names |= set(session.scalars(select(PlaceAlias.alias)))
    return frozenset(n.strip() for n in names if len(n.strip()) >= 4 and n.strip()[0].isupper())


def build_input(session: Session, version: AssessmentVersion) -> ExplainInput:
    situation = session.get(Situation, version.situation_id)
    assert situation is not None
    place = session.get(Place, situation.place_id)
    assert place is not None
    return ExplainInput(
        situation_title=situation.title,
        template=version.template,
        scope_label=version.scope_label,
        period_label=version.period_label,
        evidence_state=version.evidence_state,
        severity=version.severity,
        headline=version.headline,
        facts=list(version.facts),
        possible_factors=list(version.possible_factors),
        unknowns=[str(u) for u in version.unknowns],
        parent_places=tuple(_ancestors(session, place)),
        known_places=known_place_names(session),
    )


def needs_explanation(version: AssessmentVersion) -> bool:
    """A version is explained once per prompt: R3 cards (insufficient evidence) never are, and a
    version that was tried and failed keeps its ``prompt_version`` so it is not tried forever."""
    return (
        version.evidence_state != "insufficient"
        and version.status in ("draft", "published")
        and version.explanation is None
        and version.prompt_version is None
    )


def explain_version(
    session: Session, adapter: LlmAdapter, version_id: int, *, job_id: int | None = None
) -> Outcome:
    """Write and store the explanation of one version. The caller commits (the adapter commits
    after each model call as well). ``BudgetExhausted`` propagates so the job can be deferred."""
    version = session.get(AssessmentVersion, version_id)
    if version is None or not needs_explanation(version):
        return "skipped"
    result = generate_explanation(adapter, build_input(session, version), job_id=job_id)
    # Stored either way: model and prompt version mark the version as tried (see needs_explanation).
    version.model_id = result.model_id
    version.prompt_version = PROMPT_VERSION
    version.explanation = result.text
    session.flush()
    if result.text is None:
        log.warning(
            "no explanation for version %s after %d attempts: published with facts only",
            version_id,
            result.attempts,
            extra={"job_id": job_id, "problems": result.problems},
        )
        return "failed"
    return "explained"


def versions_missing_explanation(session: Session, limit: int) -> list[int]:
    """Current published versions, and drafts held for review, that have not been explained or
    tried yet: the work to do when a key is added or the budget ran out. Newest first."""
    rows = session.scalars(
        select(AssessmentVersion)
        .outerjoin(Situation, Situation.current_version_id == AssessmentVersion.id)
        .where(
            AssessmentVersion.explanation.is_(None),
            AssessmentVersion.prompt_version.is_(None),
            AssessmentVersion.evidence_state != "insufficient",
            (Situation.id.is_not(None))
            | ((AssessmentVersion.status == "draft") & AssessmentVersion.hold_until.is_not(None)),
        )
        .order_by(AssessmentVersion.id.desc())
        .limit(limit)
    )
    return [v.id for v in rows]
