"""Keap contact sync worker (phase 2B): a separate service, like the Sheet and Stride workers.

Each tick: plan who changed (phase 2A), claim due rows, bring each person's Keap
contact up to date, write a heartbeat. Keap being slow, rate-limited or down only
delays this worker; calls, texts, the Sheet and the Stride import never wait on it.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import psycopg
from psycopg.types.json import Jsonb

from .config import Settings, get_settings
from .db import transaction
from .observability import WorkflowTrace, configure_logging
from .services import keap_desired, keap_oauth
from .services.keap_client import KeapClient, KeapRateLimited
from .services.keap_oauth import KeapAuthError
from .services.keap_sync import sync_row

SERVICE = "keap-worker"
# Session-level advisory lock: only one keap-worker may run, even if a second
# container is started by mistake.
KEAP_WORKER_LOCK_ID = 7_272_054_120
STUCK_AFTER = "15 minutes"
FULL_PLAN_EVERY_SECONDS = 3600

_state: dict[str, Any] = {"last_full": None, "client": None, "stats_day": None}


@contextmanager
def keap_worker_lock(database_url: str, connect_timeout: float):
    with psycopg.connect(
        database_url, autocommit=True, connect_timeout=max(1, int(connect_timeout))
    ) as conn:
        acquired = conn.execute("select pg_try_advisory_lock(%s)", (KEAP_WORKER_LOCK_ID,)).fetchone()[0]
        if not acquired:
            raise RuntimeError("another keap-worker already owns the worker lock")
        try:
            yield conn
        finally:
            conn.execute("select pg_advisory_unlock(%s)", (KEAP_WORKER_LOCK_ID,))


def _practice_id(conn, settings: Settings) -> int:
    row = conn.execute(
        "select id from practices where slug=%s", (settings.stride_practice_slug,)
    ).fetchone()
    if not row:
        raise RuntimeError(f"practice {settings.stride_practice_slug!r} not found")
    return row["id"]


def get_client(practice_id: int, settings: Settings) -> KeapClient:
    if _state["client"] is None or _state["client"].practice_id != practice_id:
        _state["client"] = KeapClient(
            practice_id, settings, httpx.Client(timeout=settings.request_timeout_seconds)
        )
    return _state["client"]


def write_heartbeat(**details: Any) -> None:
    """Best effort: a heartbeat failure must never stop the sync."""
    try:
        with transaction() as conn:
            conn.execute(
                "insert into service_heartbeats(service,last_seen_at,details) values(%s,now(),%s) "
                "on conflict(service) do update set last_seen_at=now(),details=excluded.details",
                (SERVICE, Jsonb(details)),
            )
    except Exception:  # noqa: BLE001 - monitoring only
        logging.getLogger(__name__).warning("keap_heartbeat_failed",
                                            extra={"event": "keap_heartbeat_failed"})


def _queue_counts(conn, practice_id: int) -> dict[str, int]:
    rows = conn.execute(
        "select status,count(*) as n from keap_sync where practice_id=%s group by status",
        (practice_id,),
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def _claim(conn, practice_id: int, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "update keap_sync set status='syncing',attempts=attempts+1 where id in ("
        " select id from keap_sync where practice_id=%s and status in ('pending','retrying') "
        " and next_attempt_at<=now() and desired is not null "
        " order by next_attempt_at,id limit %s for update skip locked) returning *",
        (practice_id, limit),
    ).fetchall()
    return sorted(rows, key=lambda r: (r["next_attempt_at"], r["id"]))


def _release(ids: list[int], retry_after_seconds: int | None) -> None:
    """Rows claimed but not tried because the tick stopped: no attempt was used."""
    if not ids:
        return
    with transaction() as conn:
        conn.execute(
            "update keap_sync set status='pending',attempts=greatest(attempts-1,0),"
            "next_attempt_at=now()+make_interval(secs=>%s) where id=any(%s) and status='syncing'",
            (retry_after_seconds or 0, ids),
        )


def _roll_daily_stats(client: KeapClient) -> None:
    """Today's counters start from zero at 00:00 UTC (the same moment Keap's daily limit resets)."""
    today = datetime.now(UTC).date().isoformat()
    if _state["stats_day"] != today:
        _state["stats_day"] = today
        client.stats.update(created=0, updated=0, tags_applied=0, tags_removed=0)


def run_keap_sync_tick(client: KeapClient | None = None) -> dict[str, Any]:
    settings = get_settings()
    counts: dict[str, Any] = {}
    base = {"enabled": settings.keap_sync_enabled}
    if not settings.keap_sync_enabled:
        write_heartbeat(**base, state="switched_off")
        return counts
    with transaction() as conn:
        practice_id = _practice_id(conn, settings)
    connection = keap_oauth.connection_status(practice_id)
    if connection is None or connection["status"] != "active":
        write_heartbeat(**base, state="needs_connection",
                        connection=(connection or {}).get("status", "missing"))
        return counts

    client = client or get_client(practice_id, settings)
    _roll_daily_stats(client)
    trace = WorkflowTrace("keap_sync_tick", SERVICE, uuid4().hex)

    full = (
        _state["last_full"] is None
        or time.monotonic() - _state["last_full"] >= FULL_PLAN_EVERY_SECONDS
    )
    with transaction() as conn:
        conn.execute(
            "update keap_sync set status='retrying' where practice_id=%s and status='syncing' "
            f"and updated_at<now()-interval '{STUCK_AFTER}'",
            (practice_id,),
        )
        counts.update({f"plan_{k}": v for k, v in
                       keap_desired.plan_changes(conn, practice_id, full=full).items()})
    if full:
        _state["last_full"] = time.monotonic()

    with transaction() as conn:
        rows = _claim(conn, practice_id, settings.keap_sync_batch_size)
    stopped: str | None = None
    for index, row in enumerate(rows):
        try:
            outcome = sync_row(client, row, trace, settings)
        except KeapRateLimited as error:
            stopped = "rate_limited"
            _release([r["id"] for r in rows[index + 1:]], error.retry_after_seconds)
            break
        except KeapAuthError:
            stopped = "reauthorization_required"
            _release([r["id"] for r in rows[index + 1:]], None)
            break
        counts[outcome] = counts.get(outcome, 0) + 1

    with transaction() as conn:
        queue = _queue_counts(conn, practice_id)
    write_heartbeat(
        **base, state=stopped or "idle", connection="active", queue=queue,
        requests_today=client.limiter.used_today,
        requests_budget=settings.keap_max_requests_per_day,
        today=dict(client.stats), last_tick=counts,
    )
    trace.complete(**{k: v for k, v in counts.items() if isinstance(v, int)})
    return counts


def main() -> None:
    configure_logging(SERVICE)
    settings = get_settings()
    errors = settings.runtime_errors(SERVICE)
    if errors:
        raise RuntimeError("; ".join(errors))
    logging.getLogger(__name__).info(
        "keap_worker_started",
        extra={"event": "keap_worker_started", "details": {"enabled": settings.keap_sync_enabled}},
    )
    with keap_worker_lock(settings.supabase_db_url, settings.db_pool_timeout_seconds):
        while True:
            started = time.monotonic()
            try:
                run_keap_sync_tick()
            except Exception:
                logging.getLogger(__name__).exception(
                    "keap_worker_tick_failed", extra={"event": "keap_worker_tick_failed"}
                )
            time.sleep(max(0, settings.keap_sync_poll_seconds - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
