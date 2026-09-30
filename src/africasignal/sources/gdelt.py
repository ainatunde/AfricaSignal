"""GDELT 2.0 adapter: discover news URLs about Nigeria (spec B6.7, AS-024).

GDELT is used only to *find* articles. Every 15 minutes it publishes an Events file (one row per
event, 61 tab-separated columns) and a Mentions file (one row per article that mentions an event,
16 columns), both without headers. The poll keeps the events about Nigeria, takes the web mentions
of those events and records each URL in ``gdelt_discovery``. A URL is fetched only when its domain
belongs to a news outlet an operator has approved, and kept only when its ``<title>`` matches the
topic keywords; the domains of every other URL are counted for the console's discovered-domains
report and never fetched.

Country codes: ``ActionGeo_CountryCode`` is FIPS 10-4, where Nigeria is ``NI`` and ``NG`` is Niger.
Actor country codes are CAMEO/ISO-3, where Nigeria is ``NGA``. An event is about Nigeria when its
action took place there or either actor is Nigerian.

The window files follow a fixed naming scheme (``YYYYMMDDHHMMSS.export.CSV.zip`` every 15 minutes
in UTC), so a missed window is replayed by building its URLs from the timestamp rather than by
downloading ``masterfilelist.txt`` (hundreds of megabytes). That is the one departure from the
plan's wording; ``lastupdate.txt`` still supplies the newest window, with the size and MD5 checked.
"""

from __future__ import annotations

import csv
import hashlib
import html as html_lib
import io
import logging
import re
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from urllib.parse import urlsplit

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from africasignal.catalog import load_items
from africasignal.evidence.capture import record_document
from africasignal.evidence.text import HTML_MIMES, detect_mime, extract_text
from africasignal.evidence.urls import canonicalise
from africasignal.jobs import queue
from africasignal.models import EvidenceDocument, GdeltDiscovery, Setting, Source
from africasignal.net.fetch import FetchResult, fetch_document
from africasignal.sources.permissions import current_permission
from africasignal.storage import ObjectStore

log = logging.getLogger("africasignal.sources.gdelt")

GDELT_HOST = "data.gdeltproject.org"
BASE_URL = f"https://{GDELT_HOST}/gdeltv2"
LASTUPDATE_URL = f"{BASE_URL}/lastupdate.txt"
SOURCE_ADAPTER = "gdelt"
LAST_WINDOW_SETTING = "gdelt.last_window"  # the timestamp (YYYYMMDDHHMMSS) of the last window done

WINDOW = timedelta(minutes=15)
MAX_LOOKBACK_WINDOWS = 96  # a day; a longer outage is not replayed
MAX_WINDOWS_PER_POLL = 6  # keeps a catch-up inside the job lease and the rate limit
MISSING_WINDOW_GRACE = timedelta(hours=1)  # a 404 older than this is a gap GDELT never filled
MAX_ZIP_BYTES = 30_000_000
MAX_UNZIPPED_BYTES = 200_000_000
MAX_URL_CHARS = 2000
ARTICLE_MAX_BYTES = 3_000_000
GONE_STATUSES = (404, 410, 451)

# Column positions (0-based) in the headerless GDELT 2.0 files.
EXPORT_COLUMNS = 61
EXP_EVENT_ID, EXP_ACTOR1_COUNTRY, EXP_ACTOR2_COUNTRY = 0, 7, 17
EXP_EVENT_ROOT_CODE, EXP_ACTION_COUNTRY, EXP_ACTION_ADM1 = 28, 53, 54
MENTION_COLUMNS = 16
MEN_EVENT_ID, MEN_MENTION_TS, MEN_TYPE, MEN_IDENTIFIER = 0, 2, 3, 5
WEB_MENTION = "1"  # other types carry a citation or an id, not a URL

NIGERIA_FIPS = "NI"
NIGERIA_CAMEO = "NGA"

Fetcher = Callable[..., FetchResult]


class GdeltError(Exception):
    """The GDELT files are not what they should be. The poll fails and is retried."""


# --- the files ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class FileRef:
    url: str
    size: int | None = None
    md5: str | None = None


@dataclass(frozen=True)
class LastUpdate:
    """The newest window, as listed in ``lastupdate.txt``."""

    timestamp: str
    export: FileRef
    mentions: FileRef


def parse_timestamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def format_timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%d%H%M%S")


def window_urls(timestamp: str) -> tuple[str, str]:
    """The Events and Mentions file URLs of the window that ends at ``timestamp``."""
    return (
        f"{BASE_URL}/{timestamp}.export.CSV.zip",
        f"{BASE_URL}/{timestamp}.mentions.CSV.zip",
    )


def _trusted_file_url(url: str) -> str:
    """``lastupdate.txt`` still lists http:// URLs that redirect to https://. Upgrade them, and
    refuse any other host: the file is fetched input, and this job must not be steered elsewhere."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or parts.hostname != GDELT_HOST or parts.port:
        raise GdeltError(f"lastupdate.txt lists a file outside {GDELT_HOST}: {url!r}")
    return f"https://{GDELT_HOST}{parts.path}"


_FILE_NAME = re.compile(r"/(\d{14})\.(export|mentions|gkg)\.(?:CSV|csv)\.zip$")


def parse_lastupdate(body: str) -> LastUpdate:
    """Read ``lastupdate.txt``: three lines of ``<bytes> <md5> <url>`` (export, mentions, gkg)."""
    files: dict[str, tuple[str, FileRef]] = {}
    for line in body.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        size, md5, url = fields
        match = _FILE_NAME.search(url)
        if match is None or not size.isdigit():
            continue
        files[match.group(2)] = (
            match.group(1),
            FileRef(url=_trusted_file_url(url), size=int(size), md5=md5.lower()),
        )
    if "export" not in files or "mentions" not in files:
        raise GdeltError("lastupdate.txt does not list an export and a mentions file")
    (export_ts, export), (mentions_ts, mentions) = files["export"], files["mentions"]
    if export_ts != mentions_ts:
        raise GdeltError(f"lastupdate.txt lists different windows: {export_ts} and {mentions_ts}")
    return LastUpdate(timestamp=export_ts, export=export, mentions=mentions)


def windows_between(after: str, through: str) -> list[str]:
    """Timestamps of the 15-minute windows after ``after`` up to and including ``through``."""
    moment, end = parse_timestamp(after) + WINDOW, parse_timestamp(through)
    out: list[str] = []
    while moment <= end:
        out.append(format_timestamp(moment))
        moment += WINDOW
    return out


def _unzip_single(data: bytes, what: str) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) != 1:
                raise GdeltError(f"{what}: expected one file in the zip, found {len(members)}")
            if members[0].file_size > MAX_UNZIPPED_BYTES:
                raise GdeltError(f"{what}: the unzipped file is {members[0].file_size} bytes")
            return archive.read(members[0]).decode("utf-8", errors="replace")
    except zipfile.BadZipFile as exc:
        raise GdeltError(f"{what}: not a zip file ({exc})") from exc


def _rows(body: str, width: int, what: str) -> Iterable[list[str]]:
    """Tab-separated rows of the expected width. Other rows are skipped, but a file where none
    has the expected width means the format changed, and that is an error."""
    # Quote characters are ordinary text in these files.
    reader = csv.reader(io.StringIO(body, newline=""), delimiter="\t", quoting=csv.QUOTE_NONE)
    seen = good = 0
    for row in reader:
        if not row:
            continue
        seen += 1
        if len(row) == width:
            good += 1
            yield row
    if seen and not good:
        raise GdeltError(f"{what}: no row has {width} columns; GDELT changed its format")
    if seen != good:
        log.warning("%s: skipped %d of %d malformed rows", what, seen - good, seen)


# --- events and mentions ------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    event_id: int
    action_country: str
    action_adm1: str
    root_code: str


@dataclass(frozen=True)
class Mention:
    event_id: int
    mention_ts: datetime
    url: str


def is_nigeria_event(row: list[str]) -> bool:
    """Action in Nigeria (FIPS ``NI``; ``NG`` is Niger) or a Nigerian actor (CAMEO ``NGA``)."""
    return (
        row[EXP_ACTION_COUNTRY] == NIGERIA_FIPS
        or row[EXP_ACTOR1_COUNTRY] == NIGERIA_CAMEO
        or row[EXP_ACTOR2_COUNTRY] == NIGERIA_CAMEO
    )


def parse_events(body: str) -> dict[int, Event]:
    """The events about Nigeria in an Events file, by event id."""
    events: dict[int, Event] = {}
    for row in _rows(body, EXPORT_COLUMNS, "events file"):
        if not row[EXP_EVENT_ID].isdigit() or not is_nigeria_event(row):
            continue
        event = Event(
            event_id=int(row[EXP_EVENT_ID]),
            action_country=row[EXP_ACTION_COUNTRY],
            action_adm1=row[EXP_ACTION_ADM1],
            root_code=row[EXP_EVENT_ROOT_CODE],
        )
        events[event.event_id] = event
    return events


def clean_url(value: str) -> str | None:
    url = value.strip()
    if not url or len(url) > MAX_URL_CHARS or urlsplit(url).scheme not in ("http", "https"):
        return None
    return url


def parse_mentions(body: str) -> list[Mention]:
    """Every web mention in a Mentions file, whichever event it belongs to."""
    out: list[Mention] = []
    for row in _rows(body, MENTION_COLUMNS, "mentions file"):
        url = clean_url(row[MEN_IDENTIFIER])
        if row[MEN_TYPE] != WEB_MENTION or url is None or not row[MEN_EVENT_ID].isdigit():
            continue
        try:
            moment = parse_timestamp(row[MEN_MENTION_TS])
        except ValueError:
            continue
        out.append(Mention(int(row[MEN_EVENT_ID]), moment, url))
    return out


# --- recording discoveries ----------------------------------------------------------------------


@dataclass
class WindowResult:
    timestamp: str
    events: int = 0  # events about Nigeria in this window's Events file
    mentions: int = 0  # web mentions of Nigeria events
    new: int = 0  # discovery rows inserted (zero on a replay)
    linked: int = 0  # new URLs already captured, linked to their document
    queued: int = 0  # article fetch jobs queued
    unknown_domain: int = 0  # new URLs whose domain is not an approved outlet


def _known_events(session: Session, event_ids: set[int]) -> dict[int, Event]:
    """Nigeria events seen in an earlier window: a mention can arrive windows after its event."""
    if not event_ids:
        return {}
    rows = session.execute(
        text(
            "SELECT DISTINCT ON (global_event_id) global_event_id, action_geo_country, "
            "action_geo_adm1, event_root_code FROM gdelt_discovery "
            "WHERE global_event_id = ANY(:ids) ORDER BY global_event_id, id"
        ),
        {"ids": sorted(event_ids)},
    ).all()
    return {r[0]: Event(r[0], r[1] or "", r[2] or "", r[3] or "") for r in rows}


def _insert_new(session: Session, rows: list[dict[str, object]]) -> list[str]:
    """Insert discovery rows, ignoring the ones already there. Returns the new URLs, in order."""
    new: list[str] = []
    for start in range(0, len(rows), 2000):
        statement = (
            pg_insert(GdeltDiscovery)
            .values(rows[start : start + 2000])
            .on_conflict_do_nothing(index_elements=["global_event_id", "mention_identifier"])
            .returning(GdeltDiscovery.mention_identifier)
        )
        new.extend(session.scalars(statement))
    return new


def process_window(
    session: Session, timestamp: str, events_body: str, mentions_body: str
) -> WindowResult:
    """Record the Nigeria discoveries of one window and queue the article fetches they call for.

    Idempotent: replaying a window inserts nothing and queues nothing, because the discovery rows
    are unique per (event, URL) and each article job is deduplicated on its canonical URL.
    """
    result = WindowResult(timestamp)
    events = parse_events(events_body)
    mentions = parse_mentions(mentions_body)
    earlier = _known_events(session, {m.event_id for m in mentions} - events.keys())
    result.events = len(events)

    rows: list[dict[str, object]] = []
    seen: set[tuple[int, str]] = set()
    for mention in mentions:
        event = events.get(mention.event_id) or earlier.get(mention.event_id)
        if event is None or (mention.event_id, mention.url) in seen:
            continue
        seen.add((mention.event_id, mention.url))
        rows.append(
            {
                "global_event_id": mention.event_id,
                "mention_identifier": mention.url,
                "mention_ts": mention.mention_ts,
                "action_geo_country": event.action_country or None,
                "action_geo_adm1": event.action_adm1 or None,
                "event_root_code": event.root_code or None,
            }
        )
    result.mentions = len(rows)

    new_urls = _insert_new(session, rows)
    result.new = len(new_urls)
    _route_new_urls(session, new_urls, result)
    return result


def _route_new_urls(session: Session, urls: list[str], result: WindowResult) -> None:
    """Decide for each new URL, once per canonical URL: link it to an existing document, queue its
    fetch, or count its domain as unknown."""
    outlets = approved_outlets(session)
    done: set[str] = set()
    for url in urls:
        canonical = canonicalise(url)
        if canonical in done:
            continue
        done.add(canonical)
        document = find_document(session, canonical)
        if document is not None:
            link_discoveries(session, url, document.id)
            result.linked += 1
            continue
        outlet = outlet_for_url(outlets, url)
        if outlet is None:
            result.unknown_domain += 1
            continue
        job = queue.enqueue(
            session,
            "gdelt_fetch_article",
            {"url": url, "source_id": outlet.id},
            dedupe_key=f"gdelt_fetch_article:{canonical}",
        )
        if job is not None:
            result.queued += 1


# --- which domains may be fetched ---------------------------------------------------------------


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


@dataclass(frozen=True)
class Outlet:
    id: int
    slug: str
    domains: frozenset[str]

    def covers(self, host: str) -> bool:
        return any(host == d or host.endswith("." + d) for d in self.domains)


def approved_outlets(session: Session) -> list[Outlet]:
    """Active news outlets with an approved permission to collect, and the domains they cover
    (the hosts of their home and feed URLs)."""
    outlets: list[Outlet] = []
    sources = session.scalars(
        select(Source).where(Source.kind == "news_outlet", Source.active.is_(True))
    )
    for source in sources:
        permission = current_permission(session, source.id)
        if permission is None or not permission.may_collect:
            continue
        domains = {host_of(u) for u in (source.home_url, source.feed_url) if u}
        if domains:
            outlets.append(Outlet(source.id, source.slug, frozenset(domains)))
    return outlets


def outlet_for_url(outlets: list[Outlet], url: str) -> Outlet | None:
    host = host_of(url)
    return next((o for o in outlets if o.covers(host)), None)


@dataclass(frozen=True)
class DiscoveredDomain:
    domain: str
    urls: int
    events: int
    last_seen: datetime


def discovered_domains(
    session: Session, *, since: datetime | None = None, limit: int = 100
) -> list[DiscoveredDomain]:
    """Domains GDELT pointed to that no approved outlet covers, most-linked first: the console's
    "discovered domains" report, from which an operator adds sources. These are never fetched."""
    rows = session.execute(
        text(
            """
            SELECT domain, count(DISTINCT mention_identifier) AS urls,
                   count(DISTINCT global_event_id) AS events, max(mention_ts) AS last_seen
            FROM (
                SELECT regexp_replace(lower(substring(mention_identifier
                           FROM '^https?://([^/:?#]+)')), '^www\\.', '') AS domain,
                       mention_identifier, global_event_id, mention_ts
                FROM gdelt_discovery WHERE (CAST(:since AS timestamptz) IS NULL
                                            OR mention_ts >= CAST(:since AS timestamptz))
            ) d
            WHERE domain IS NOT NULL GROUP BY domain ORDER BY urls DESC, domain
            """
        ),
        {"since": since},
    ).all()
    outlets = approved_outlets(session)
    found = [
        DiscoveredDomain(r[0], r[1], r[2], r[3])
        for r in rows
        if not any(o.covers(r[0]) for o in outlets)
    ]
    return found[:limit]


# --- the topic filter ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TopicMatcher:
    """Whole-word match of the keywords in ``config/items.yaml``. Acronyms (PMS, AGO, LPG, ...) are
    matched in capitals only, so "ago" in "two years ago" is not a hit."""

    acronyms: re.Pattern[str] | None
    words: re.Pattern[str] | None

    def matches(self, title: str | None) -> bool:
        if not title:
            return False
        return any(p.search(title) for p in (self.acronyms, self.words) if p is not None)


def build_matcher(keywords: Iterable[str]) -> TopicMatcher:
    acronyms: list[str] = []
    words: list[str] = []
    for keyword in keywords:
        keyword = keyword.strip()
        if not keyword:
            continue
        if keyword.isupper() and len(keyword) <= 5:
            acronyms.append(re.escape(keyword))
        else:
            words.append(re.escape(keyword))

    def compile_(parts: list[str], flags: int, plural: str) -> re.Pattern[str] | None:
        if not parts:
            return None
        alternatives = "|".join(sorted(parts, key=len, reverse=True))
        return re.compile(rf"(?<![A-Za-z0-9])(?:{alternatives}){plural}(?![A-Za-z0-9])", flags)

    return TopicMatcher(compile_(acronyms, 0, ""), compile_(words, re.IGNORECASE, "(?:e?s)?"))


@lru_cache(maxsize=1)
def topic_matcher() -> TopicMatcher:
    keywords = load_items().keywords
    return build_matcher(word for words in keywords.values() for word in words)


# --- articles -----------------------------------------------------------------------------------


def find_document(session: Session, canonical_url: str) -> EvidenceDocument | None:
    """An active document already captured at this canonical URL, from any source (for example
    the RSS adapter): the same article is never captured twice."""
    return session.scalars(
        select(EvidenceDocument)
        .where(
            EvidenceDocument.canonical_url == canonical_url,
            EvidenceDocument.status == "active",
        )
        .order_by(EvidenceDocument.id)
        .limit(1)
    ).first()


def link_discoveries(session: Session, url: str, document_id: int) -> None:
    session.execute(
        update(GdeltDiscovery)
        .where(
            GdeltDiscovery.mention_identifier == url,
            GdeltDiscovery.evidence_document_id.is_(None),
        )
        .values(evidence_document_id=document_id)
    )


_TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def page_title(html: str, extracted_title: str | None) -> str | None:
    if extracted_title and extracted_title.strip():
        return extracted_title.strip()
    match = _TITLE_TAG.search(html[:200_000])
    if match is None:
        return None
    return re.sub(r"\s+", " ", html_lib.unescape(match.group(1))).strip() or None


class ArticleFetchError(Exception):
    """The article could not be fetched right now; the job is retried with backoff."""


@dataclass
class ArticleOutcome:
    status: str  # captured | already_captured | not_on_topic | not_html | unavailable | skipped
    document_id: int | None = None
    title: str | None = None


def fetch_article(
    session: Session,
    store: ObjectStore,
    url: str,
    source_id: int,
    *,
    fetch: Fetcher | None = None,
    matcher: TopicMatcher | None = None,
    now: datetime | None = None,
) -> ArticleOutcome:
    """Fetch a discovered article for an approved outlet and keep it only if its title matches the
    topic keywords. What is stored (full text or an excerpt) follows the outlet's permission."""
    fetch = fetch or fetch_document
    source = session.get(Source, source_id)
    permission = current_permission(session, source_id) if source else None
    if source is None or not source.active or permission is None or not permission.may_collect:
        return ArticleOutcome("skipped")
    if clean_url(url) is None:
        return ArticleOutcome("skipped")

    existing = find_document(session, canonicalise(url))
    if existing is not None:
        link_discoveries(session, url, existing.id)
        return ArticleOutcome("already_captured", existing.id, existing.title)

    result = fetch(
        url, max_bytes=ARTICLE_MAX_BYTES, use_cache=False,
        max_requests_per_hour=source.max_requests_per_hour,
    )  # fmt: skip
    if result.status_code in GONE_STATUSES:
        return ArticleOutcome("unavailable")
    if not result.success:
        raise ArticleFetchError(result.error or f"HTTP {result.status_code} for {url}")

    content_type = result.headers.get("content-type")
    mime = detect_mime(content_type, result.content, result.url)
    if mime not in HTML_MIMES:
        return ArticleOutcome("not_html")
    html = result.content.decode("utf-8", errors="replace")
    title = page_title(html, extract_text(result.content, mime).title)

    canonical = canonicalise(result.url, html)  # the page may declare a different canonical URL
    existing = find_document(session, canonical)
    if existing is not None:
        link_discoveries(session, url, existing.id)
        return ArticleOutcome("already_captured", existing.id, existing.title)
    if not (matcher or topic_matcher()).matches(title):
        return ArticleOutcome("not_on_topic", title=title)

    document = record_document(
        session, store, source, permission,
        url=url, final_url=result.url, content=result.content,
        content_type=content_type, title=title, now=now,
    )  # fmt: skip
    link_discoveries(session, url, document.id)
    return ArticleOutcome("captured", document.id, title)


# --- polling ------------------------------------------------------------------------------------


@dataclass
class PollResult:
    latest: str | None = None
    windows: list[WindowResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # windows GDELT never published
    stopped_early: str | None = None  # why the poll left windows for the next run


def gdelt_source(session: Session) -> Source | None:
    return session.scalars(
        select(Source).where(Source.adapter == SOURCE_ADAPTER, Source.active.is_(True)).limit(1)
    ).first()


def last_window(session: Session) -> str | None:
    row = session.get(Setting, LAST_WINDOW_SETTING)
    return str(row.value) if row is not None else None


def _set_last_window(session: Session, timestamp: str) -> None:
    row = session.get(Setting, LAST_WINDOW_SETTING)
    if row is None:
        session.add(Setting(key=LAST_WINDOW_SETTING, value=timestamp))
    else:
        row.value = timestamp
    session.flush()


def _download(fetch: Fetcher, url: str, source: Source, ref: FileRef | None = None) -> FetchResult:
    result: FetchResult = fetch(
        url, max_bytes=MAX_ZIP_BYTES, use_cache=False,
        max_requests_per_hour=source.max_requests_per_hour,
    )  # fmt: skip
    if ref is not None and result.success:
        if ref.size is not None and len(result.content) != ref.size:
            return FetchResult(url=url, error=f"{url}: {len(result.content)} bytes, not {ref.size}")
        if ref.md5 and hashlib.md5(result.content, usedforsecurity=False).hexdigest() != ref.md5:
            return FetchResult(url=url, error=f"{url}: MD5 differs from lastupdate.txt")
    return result


def poll(
    session: Session,
    source: Source,
    *,
    fetch: Fetcher | None = None,
    max_windows: int = MAX_WINDOWS_PER_POLL,
) -> PollResult:
    """Process every window published since the last poll, oldest first (spec B6.7).

    The last finished window is kept in ``setting`` so an outage is caught up from where it
    stopped. The first poll starts at the newest window. A window that cannot be downloaded ends
    the poll (it is tried again next time) unless it is a 404 an hour or more behind the newest
    window, which GDELT has evidently skipped. The caller commits.
    """
    fetch = fetch or fetch_document
    outcome = PollResult()
    listing = fetch(
        LASTUPDATE_URL, use_cache=False, max_requests_per_hour=source.max_requests_per_hour
    )
    if not listing.success:
        raise GdeltError(listing.error or f"lastupdate.txt: HTTP {listing.status_code}")
    latest = parse_lastupdate(listing.content.decode("utf-8", errors="replace"))
    outcome.latest = latest.timestamp

    done = last_window(session)
    if done is None:
        todo = [latest.timestamp]
    else:
        todo = windows_between(done, latest.timestamp)
        if len(todo) > MAX_LOOKBACK_WINDOWS:
            log.warning(
                "gdelt: %d windows behind; replaying the newest %d", len(todo), MAX_LOOKBACK_WINDOWS
            )
            todo = todo[-MAX_LOOKBACK_WINDOWS:]
    newest = parse_timestamp(latest.timestamp)

    for timestamp in todo[:max_windows]:
        is_latest = timestamp == latest.timestamp
        export_url, mentions_url = window_urls(timestamp)
        export_ref = latest.export if is_latest else FileRef(export_url)
        mentions_ref = latest.mentions if is_latest else FileRef(mentions_url)
        export = _download(fetch, export_ref.url, source, export_ref if is_latest else None)
        mentions = (
            _download(fetch, mentions_ref.url, source, mentions_ref if is_latest else None)
            if export.success
            else export
        )
        if not (export.success and mentions.success):
            failed = export if not export.success else mentions
            gone = failed.status_code in GONE_STATUSES
            if gone and newest - parse_timestamp(timestamp) >= MISSING_WINDOW_GRACE:
                log.warning("gdelt: window %s was never published; skipping it", timestamp)
                outcome.skipped.append(timestamp)
                _set_last_window(session, timestamp)
                continue
            outcome.stopped_early = f"{timestamp}: {failed.error or f'HTTP {failed.status_code}'}"
            log.warning("gdelt: stopping at window %s (%s)", timestamp, outcome.stopped_early)
            break
        window = process_window(
            session,
            timestamp,
            _unzip_single(export.content, export_url),
            _unzip_single(mentions.content, mentions_url),
        )
        _set_last_window(session, timestamp)
        outcome.windows.append(window)
        log.info(
            "gdelt window %s: %d Nigeria events, %d web mentions, %d new, %d queued, "
            "%d linked, %d from unknown domains",
            timestamp, window.events, window.mentions, window.new, window.queued,
            window.linked, window.unknown_domain,
        )  # fmt: skip
    if outcome.stopped_early and not outcome.windows and not outcome.skipped:
        raise GdeltError(f"no window could be downloaded: {outcome.stopped_early}")
    return outcome
