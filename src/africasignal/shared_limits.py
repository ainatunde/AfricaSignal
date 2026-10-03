"""Atomic PostgreSQL limits shared across replicas; no raw client address is stored."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from datetime import timedelta
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text

from africasignal.config import get_settings


def shared_enabled() -> bool:
    return get_settings().env != "development"


def private_key(scope: str, client: str) -> str:
    secret = get_settings().secret_key.encode()
    return hmac.new(secret, (scope + "\0" + client).encode(), hashlib.sha256).hexdigest()


@lru_cache
def _limits_engine() -> Engine:
    # A separate, bounded pool cannot exhaust the request/worker session pool.
    # Startup options also cover pre-ping and connection initialization queries.
    return create_engine(
        get_settings().effective_database_url,
        pool_pre_ping=True,
        pool_timeout=2,
        pool_size=5,
        max_overflow=5,
        connect_args={
            "connect_timeout": 2,
            "options": "-c statement_timeout=2000 -c lock_timeout=1000",
        },
    )


class SharedLimits:
    def __init__(self, engine: Engine | None = None) -> None:
        self.engine = engine

    def take(
        self,
        scope: str,
        key_hash: str,
        *,
        rate: float,
        capacity: float,
        window: float | None = None,
    ) -> tuple[bool, float]:
        if not math.isfinite(rate) or not math.isfinite(capacity) or rate <= 0 or capacity < 1:
            raise ValueError("rate and capacity must be finite and positive")
        if window is not None and (not math.isfinite(window) or window <= 0):
            raise ValueError("window must be finite and positive")
        with (self.engine or _limits_engine()).begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '1s'"))
            conn.execute(text("SET LOCAL statement_timeout = '2s'"))
            params = {"scope": scope, "key": key_hash}
            conn.execute(
                text(
                    "INSERT INTO rate_limit_state (scope, key_hash) VALUES (:scope, "
                    ":key) ON CONFLICT DO NOTHING"
                ),
                params,
            )
            row = (
                conn.execute(
                    text(
                        "SELECT * FROM rate_limit_state WHERE scope = :scope AND key_hash "
                        "= :key FOR UPDATE"
                    ),
                    params,
                )
                .mappings()
                .one()
            )
            now = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
            if window is not None:
                hits = [float(hit) for hit in row["hits"] if float(hit) > now.timestamp() - window]
                allowed = len(hits) < int(capacity)
                if allowed:
                    hits.append(now.timestamp())
                retry = 0.0 if allowed else max(0.001, hits[0] + window - now.timestamp())
                conn.execute(
                    text(
                        "UPDATE rate_limit_state SET hits = CAST(:hits AS jsonb), "
                        "touched_at = :now, expires_at = :expires WHERE scope = :scope AND "
                        "key_hash = :key"
                    ),
                    {
                        **params,
                        "hits": json.dumps(hits),
                        "now": now,
                        "expires": now + timedelta(seconds=window),
                    },
                )
                return allowed, retry
            fresh = row["expires_at"] <= now
            # The strictest active policy wins. Changing source configuration never
            # refills a shared domain's bucket or relaxes another source's budget.
            actual_rate = rate if fresh else min(rate, row["rate"])
            actual_capacity = capacity if fresh else min(capacity, row["capacity"])
            available = (
                actual_capacity
                if fresh
                else min(
                    actual_capacity,
                    row["tokens"]
                    + max(0.0, (now - row["touched_at"]).total_seconds()) * actual_rate,
                )
            )
            allowed = available >= 1.0
            retry = 0.0 if allowed else (1.0 - available) / actual_rate
            ttl = max(3600.0, actual_capacity / actual_rate)
            conn.execute(
                text(
                    "UPDATE rate_limit_state SET tokens = :tokens, rate = :rate, "
                    "capacity = :capacity, touched_at = :now, expires_at = :expires "
                    "WHERE scope = :scope AND key_hash = :key"
                ),
                {
                    **params,
                    "tokens": available - 1.0 if allowed else available,
                    "rate": actual_rate,
                    "capacity": actual_capacity,
                    "now": now,
                    "expires": now + timedelta(seconds=ttl),
                },
            )
            return allowed, retry
