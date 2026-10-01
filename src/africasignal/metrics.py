"""Product metrics (plan AS-034): the events the site records, and the demand-test numbers of
plan D1 worked out from them.

What is stored. An event is a row in ``event``: a timestamp, the visitor's random ``anon_id`` (a
first-party cookie, never derived from anything about the person), the signed-in user when there is
one, a name, the situation, and a ``ref`` from a short allow-list. No IP address, user agent or
full URL is stored, here or anywhere else. Events older than 13 months are deleted by
``prune_events``.

What is counted. A *visitor* is a browser (an ``anon_id``) that viewed a page. All weeks are ISO
weeks in Africa/Lagos time (Monday 00:00 to Monday 00:00).
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.models import Event, Operator, Setting

LAGOS = ZoneInfo("Africa/Lagos")

ANON_COOKIE = "anon_id"
ANON_COOKIE_MAX_AGE = 365 * 24 * 3600

EVENT_NAMES = (
    "page_view",
    "situation_view",
    "follow",
    "unfollow",
    "feedback",
    "digest_open",
    "share_click",
)
# Where a visit came from (``?ref=``). Anything else is dropped rather than stored.
ALLOWED_REFS = ("wa", "x", "email", "share")
RETENTION = timedelta(days=13 * 31)  # 13 months, a little generous

_ANON_RE = re.compile(r"[A-Za-z0-9_-]{16,40}")
_BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|preview|facebookexternalhit|whatsapp|curl|wget|python-requests|httpx",
    re.IGNORECASE,
)


def new_anon_id() -> str:
    return secrets.token_urlsafe(16)


def valid_anon_id(value: str | None) -> bool:
    return bool(value) and _ANON_RE.fullmatch(value or "") is not None


def looks_like_a_bot(user_agent: str | None) -> bool:
    """Link previews (WhatsApp, Facebook) and crawlers fetch pages without a person; they are not
    counted as visitors. A missing user agent counts as a bot."""
    return not user_agent or _BOT_RE.search(user_agent) is not None


def clean_ref(value: str | None) -> str | None:
    return value if value in ALLOWED_REFS else None


def record_event(
    session: Session,
    name: str,
    *,
    anon_id: str | None = None,
    user_id: int | None = None,
    situation_id: int | None = None,
    ref: str | None = None,
    props: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    """Add one event. The caller commits."""
    if name not in EVENT_NAMES:
        raise ValueError(f"unknown event {name!r}")
    row = Event(
        anon_id=anon_id if valid_anon_id(anon_id) else None,
        user_id=user_id,
        name=name,
        situation_id=situation_id,
        ref=clean_ref(ref),
        props=props or {},
    )
    if now is not None:
        row.ts = now
    session.add(row)


def prune_events(session: Session, now: datetime) -> int:
    """Delete events past their retention. Returns how many. The caller commits."""
    result = session.execute(delete(Event).where(Event.ts < now - RETENTION))
    return int(result.rowcount)  # type: ignore[attr-defined]


# --- manual inputs ------------------------------------------------------------------------------
# Two of the D1 numbers come from outside the app: WhatsApp channel followers (read off the
# channel by hand) and the monthly infrastructure bill. They live in the ``setting`` table.

WA_KEY = "metrics.whatsapp_followers"  # {"2026-W40": 312, ...}
INFRA_KEY = "metrics.infra_monthly_usd"  # {"v": "24.50"}


def whatsapp_followers(session: Session) -> dict[str, int]:
    value = session.scalar(select(Setting.value).where(Setting.key == WA_KEY))
    if not isinstance(value, dict):
        return {}
    return {str(k): int(v) for k, v in value.items() if isinstance(v, int) and v >= 0}


def infra_monthly_usd(session: Session) -> Decimal:
    value = session.scalar(select(Setting.value).where(Setting.key == INFRA_KEY))
    try:
        return Decimal(str(value["v"])) if isinstance(value, dict) else Decimal(0)
    except ArithmeticError:
        return Decimal(0)


def _put_setting(session: Session, key: str, value: Any) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = func.now()
    session.flush()


def record_whatsapp_followers(session: Session, operator: Operator, week: str, count: int) -> None:
    """Save the channel's follower count for an ISO week (``2026-W40``). Audited."""
    if not re.fullmatch(r"\d{4}-W(0[1-9]|[1-4]\d|5[0-3])", week):
        raise ValueError("the week must look like 2026-W40")
    if count < 0 or count > 100_000_000:
        raise ValueError("the follower count must be a whole number of 0 or more")
    current = whatsapp_followers(session)
    _put_setting(session, WA_KEY, {**current, week: count})
    audit.record(
        session,
        operator,
        "metrics.whatsapp_followers",
        "setting:metrics",
        None,
        before={"week": week, "count": current.get(week)},
        after={"week": week, "count": count},
    )


def record_infra_cost(session: Session, operator: Operator, monthly_usd: Decimal) -> None:
    if monthly_usd < 0 or monthly_usd > Decimal(1_000_000):
        raise ValueError("the monthly cost must be between 0 and 1,000,000")
    before = infra_monthly_usd(session)
    _put_setting(session, INFRA_KEY, {"v": str(monthly_usd)})
    audit.record(
        session,
        operator,
        "metrics.infra_cost",
        "setting:metrics",
        None,
        before={"monthly_usd": str(before)},
        after={"monthly_usd": str(monthly_usd)},
    )


# --- the D1 report ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """A D1 threshold pair. Numbers are the plan's proposals; Tunde confirms them."""

    label: str
    continue_at: float
    rethink_at: float
    higher_is_better: bool
    unit: str  # "count", "percent", "usd"


TARGETS: dict[str, Target] = {
    "wa_followers": Target("WhatsApp channel followers", 1000, 300, True, "count"),
    "return_rate": Target("Visitors who return within 14 days", 20, 8, True, "percent"),
    "followers_with_follow": Target(
        "Signed-in users with at least one follow", 150, 40, True, "count"
    ),
    "useful_rate": Target("“Was this useful?” yes rate", 70, 50, True, "percent"),
    "cost_per_wau": Target("Cost per weekly active visitor", 0.05, 0.25, False, "usd"),
}


def verdict(target: Target, value: float | None) -> str:
    """``continue``, ``rethink``, ``between``, or ``unknown`` when there is no number yet."""
    if value is None:
        return "unknown"
    if target.higher_is_better:
        if value >= target.continue_at:
            return "continue"
        return "rethink" if value < target.rethink_at else "between"
    if value <= target.continue_at:
        return "continue"
    return "rethink" if value > target.rethink_at else "between"


@dataclass
class WeekRow:
    week: str  # "2026-W40"
    start: date  # the Monday, Lagos
    visitors: int = 0
    page_views: int = 0
    situation_views: int = 0
    follows: int = 0
    unfollows: int = 0
    feedback: int = 0
    share_clicks: int = 0
    digest_opens: int = 0
    visits_by_ref: dict[str, int] = field(default_factory=dict)
    new_visitors: int = 0  # first seen this week
    return_measured: int = 0  # of those, how many have had their full 14 days
    return_returned: int = 0
    users_with_follow: int = 0  # signed-in users whose first follow is before this week ended
    useful_yes: int = 0
    useful_no: int = 0
    llm_cost_usd: Decimal = Decimal(0)
    infra_cost_usd: Decimal = Decimal(0)
    wa_followers: int | None = None

    @property
    def return_rate(self) -> float | None:
        if not self.return_measured:
            return None
        return 100 * self.return_returned / self.return_measured

    @property
    def useful_rate(self) -> float | None:
        total = self.useful_yes + self.useful_no
        return 100 * self.useful_yes / total if total else None

    @property
    def cost_per_wau(self) -> float | None:
        if not self.visitors:
            return None
        return float((self.llm_cost_usd + self.infra_cost_usd) / self.visitors)


@dataclass
class D1Report:
    now: datetime
    weeks: list[WeekRow]  # oldest first; the last one is the current, unfinished week
    infra_monthly_usd: Decimal

    @property
    def latest_complete(self) -> WeekRow | None:
        return self.weeks[-2] if len(self.weeks) >= 2 else None

    def summary(self) -> list[dict[str, Any]]:
        """Each D1 number for the latest complete week, with its verdict."""
        week = self.latest_complete
        values: dict[str, float | None] = {
            "wa_followers": None if week is None else _opt(week.wa_followers),
            "return_rate": None if week is None else week.return_rate,
            "followers_with_follow": None if week is None else float(week.users_with_follow),
            "useful_rate": None if week is None else week.useful_rate,
            "cost_per_wau": None if week is None else week.cost_per_wau,
        }
        return [
            {
                "key": key,
                "target": target,
                "value": values[key],
                "verdict": verdict(target, values[key]),
            }
            for key, target in TARGETS.items()
        ]


def _opt(value: int | None) -> float | None:
    return None if value is None else float(value)


def iso_week_label(moment: datetime) -> str:
    year, week, _ = moment.astimezone(LAGOS).isocalendar()
    return f"{year}-W{week:02d}"


def week_start(moment: datetime) -> date:
    """The Monday (Lagos date) of the ISO week containing ``moment``."""
    local = moment.astimezone(LAGOS).date()
    return local - timedelta(days=local.weekday())


def _lagos_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=LAGOS).astimezone(UTC)


_COUNTS = """
SELECT date_trunc('week', ts AT TIME ZONE 'Africa/Lagos')::date AS week,
       name, ref, count(*) AS n
FROM event
WHERE ts >= :since AND ts < :until
GROUP BY 1, 2, 3
"""

_COHORTS = """
WITH first_seen AS (
  SELECT anon_id, min(ts) AS first_ts
  FROM event WHERE name = 'page_view' AND anon_id IS NOT NULL GROUP BY anon_id
)
SELECT date_trunc('week', f.first_ts AT TIME ZONE 'Africa/Lagos')::date AS week,
       count(*) AS cohort,
       count(*) FILTER (WHERE f.first_ts + interval '14 days' <= :now) AS measured,
       count(*) FILTER (
         WHERE f.first_ts + interval '14 days' <= :now AND EXISTS (
           SELECT 1 FROM event e
           WHERE e.name = 'page_view' AND e.anon_id = f.anon_id
             AND (e.ts AT TIME ZONE 'Africa/Lagos')::date
                 > (f.first_ts AT TIME ZONE 'Africa/Lagos')::date
             AND e.ts <= f.first_ts + interval '14 days')
       ) AS returned
FROM first_seen f
WHERE f.first_ts >= :since AND f.first_ts < :until
GROUP BY 1
"""

_FIRST_FOLLOWS = """
SELECT min(created_at) AS first_follow FROM follow GROUP BY user_id
"""


def d1_report(session: Session, now: datetime, weeks: int = 12) -> D1Report:
    """The demand-test numbers for the last ``weeks`` ISO weeks, ending with the current one."""
    current = week_start(now)
    starts = [current - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    rows = {
        start: WeekRow(week=iso_week_label(_lagos_midnight(start)), start=start) for start in starts
    }
    since, until = _lagos_midnight(starts[0]), _lagos_midnight(current + timedelta(weeks=1))
    params = {"since": since, "until": until, "now": now}

    for week, name, ref, n in session.execute(text(_COUNTS), params):
        row = rows.get(week)
        if row is None:
            continue
        if name == "page_view":
            row.page_views += n
            if ref:
                row.visits_by_ref[ref] = row.visits_by_ref.get(ref, 0) + n
        elif name == "situation_view":
            row.situation_views += n
        elif name == "follow":
            row.follows += n
        elif name == "unfollow":
            row.unfollows += n
        elif name == "feedback":
            row.feedback += n
        elif name == "share_click":
            row.share_clicks += n
        elif name == "digest_open":
            row.digest_opens += n

    visitors_by_week = {
        week: int(n)
        for week, n in session.execute(
            text(
                "SELECT date_trunc('week', ts AT TIME ZONE 'Africa/Lagos')::date, "
                "count(DISTINCT anon_id) FROM event "
                "WHERE name = 'page_view' AND anon_id IS NOT NULL "
                "AND ts >= :since AND ts < :until GROUP BY 1"
            ),
            params,
        )
    }
    for start, row in rows.items():
        row.visitors = visitors_by_week.get(start, 0)

    for week, cohort, measured, returned in session.execute(text(_COHORTS), params):
        row = rows.get(week)
        if row is not None:
            row.new_visitors, row.return_measured, row.return_returned = cohort, measured, returned

    first_follows = [r[0] for r in session.execute(text(_FIRST_FOLLOWS))]
    for start, row in rows.items():
        end = _lagos_midnight(start + timedelta(weeks=1))
        row.users_with_follow = sum(1 for moment in first_follows if moment < end)

    for start, kind, n in session.execute(
        text(
            "SELECT date_trunc('week', created_at AT TIME ZONE 'Africa/Lagos')::date, kind, "
            "count(*) FROM feedback WHERE kind IN ('useful_yes', 'useful_no') "
            "AND created_at >= :since AND created_at < :until GROUP BY 1, 2"
        ),
        params,
    ):
        row = rows.get(start)
        if row is not None:
            if kind == "useful_yes":
                row.useful_yes = n
            else:
                row.useful_no = n

    for start, cost in session.execute(
        text(
            "SELECT date_trunc('week', ts AT TIME ZONE 'Africa/Lagos')::date, sum(cost_usd) "
            "FROM llm_call WHERE ts >= :since AND ts < :until GROUP BY 1"
        ),
        params,
    ):
        row = rows.get(start)
        if row is not None:
            row.llm_cost_usd = Decimal(cost or 0)

    monthly = infra_monthly_usd(session)
    weekly_infra = (monthly * 12 / 52).quantize(Decimal("0.0001"))
    wa = whatsapp_followers(session)
    for row in rows.values():
        row.infra_cost_usd = weekly_infra
        row.wa_followers = wa.get(row.week)

    return D1Report(now=now, weeks=[rows[s] for s in starts], infra_monthly_usd=monthly)
