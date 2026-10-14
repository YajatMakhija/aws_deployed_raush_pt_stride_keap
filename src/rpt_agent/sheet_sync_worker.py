from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
from psycopg.types.json import Jsonb

from .config import Settings, get_settings
from .db import get_pool, record_integration_event, transaction
from .observability import WorkflowTrace, configure_logging
from .retry import retry_delay_seconds
from .security import n8n_sheet_headers
from .services import sheet_direct
from .services.google_sheet import SheetBackend, SheetLayoutError, flush, parse_grid
from .services.sheet_sync import build_sheet_snapshot


class SheetDeliveryError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        http_status: int | None = None,
        retry_after_seconds: int | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds


def _delivery_id(event_ids: list[str]) -> str:
    digest = hashlib.sha256("|".join(sorted(event_ids)).encode()).hexdigest()[:32]
    return f"sheet-delivery:{digest}"


def _retry_after(response: httpx.Response) -> int | None:
    value = response.headers.get("retry-after", "").strip()
    try:
        return max(1, int(value)) if value else None
    except ValueError:
        return None


def _post_snapshot(
    client: httpx.Client,
    settings: Settings,
    delivery_id: str,
    snapshot: dict[str, Any],
) -> int:
    payload = {
        "event_id": delivery_id,
        "occurred_at": datetime.now(UTC).isoformat(),
        **snapshot,
    }
    body = json.dumps(payload, separators=(",", ":"), default=str).encode()
    headers = n8n_sheet_headers(body, settings=settings)
    headers["X-Event-ID"] = delivery_id
    try:
        response = client.post(settings.n8n_sheet_webhook_url, content=body, headers=headers)
    except (httpx.TimeoutException, httpx.HTTPError) as exc:
        raise SheetDeliveryError(
            "n8n request failed", retryable=True
        ) from exc
    if 200 <= response.status_code < 300:
        return response.status_code
    retryable = response.status_code in {404, 408, 425, 429, 500, 502, 503, 504}
    raise SheetDeliveryError(
        f"n8n returned HTTP {response.status_code}",
        retryable=retryable,
        http_status=response.status_code,
        retry_after_seconds=_retry_after(response),
    )


def _claim_sheet_work(settings: Settings) -> list[dict[str, Any]]:
    with transaction() as conn:
        conn.execute(
            "update integration_outbox set status='pending',next_attempt_at=now(),"
            "last_error='Sheet worker stopped during delivery; retrying the same event',"
            "updated_at=now() where destination='n8n' and status='sending' "
            "and updated_at<now()-interval '15 minutes'"
        )
        rows = conn.execute(
            "select id,event_id,aggregate_id,attempts,payload from integration_outbox "
            "where destination='n8n' and status='pending' and next_attempt_at<=now() "
            "order by id limit %s for update skip locked",
            (settings.sheet_sync_batch_size,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "update integration_outbox set status='sending',attempts=attempts+1,updated_at=now() "
                "where id=%s",
                (row["id"],),
            )
    return rows


def _mark_delivered(rows: list[dict[str, Any]]) -> None:
    ids = [row["id"] for row in rows]
    with transaction() as conn:
        conn.execute(
            "update integration_outbox set status='delivered',delivered_at=now(),last_error=null,"
            "updated_at=now() where id=any(%s)",
            (ids,),
        )


def _mark_failed(
    rows: list[dict[str, Any]], error: SheetDeliveryError, settings: Settings
) -> dict[str, int]:
    outcomes = {"retried": 0, "dead": 0}
    with transaction() as conn:
        for row in rows:
            attempt = int(row["attempts"]) + 1
            if error.retryable and attempt < settings.retry_max_attempts:
                delay = retry_delay_seconds(attempt, error.retry_after_seconds, settings)
                conn.execute(
                    "update integration_outbox set status='pending',last_error=%s,"
                    "next_attempt_at=now()+make_interval(secs=>%s),updated_at=now() where id=%s",
                    (str(error)[:500], delay, row["id"]),
                )
                outcomes["retried"] += 1
            else:
                conn.execute(
                    "update integration_outbox set status='dead',last_error=%s,updated_at=now() "
                    "where id=%s",
                    (str(error)[:500], row["id"]),
                )
                outcomes["dead"] += 1
    return outcomes


def run_sheet_sync_tick(client: httpx.Client | None = None) -> dict[str, int]:
    settings = get_settings()
    counts = {"claimed": 0, "leads": 0, "delivered": 0, "retried": 0, "dead": 0}
    if not settings.sheet_sync_enabled:
        return counts
    trace = WorkflowTrace("sheet_sync_tick", "sheet-worker", uuid4().hex)
    rows = _claim_sheet_work(settings)
    counts["claimed"] = len(rows)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["aggregate_id"])].append(row)
    counts["leads"] = len(grouped)
    owns_client = client is None
    client = client or httpx.Client(timeout=settings.sheet_sync_http_timeout_seconds)
    try:
        for lead_id, lead_rows in grouped.items():
            delivery_id = _delivery_id([str(row["event_id"]) for row in lead_rows])
            try:
                with transaction() as conn:
                    snapshot = build_sheet_snapshot(
                        conn,
                        lead_id,
                        practice_slug=settings.n8n_practice_slug,
                        reasons=[str((row.get("payload") or {}).get("reason", "")) for row in lead_rows],
                    )
                http_status = _post_snapshot(client, settings, delivery_id, snapshot)
                _mark_delivered(lead_rows)
                record_integration_event(
                    delivery_id,
                    "outbound",
                    "n8n",
                    "sheet_update",
                    "accepted",
                    http_status=http_status,
                )
                counts["delivered"] += len(lead_rows)
                trace.log(
                    "integration_delivery_completed",
                    provider="n8n",
                    lead_id=lead_id,
                    outbox_count=len(lead_rows),
                )
            except LookupError as exc:
                error = SheetDeliveryError(str(exc), retryable=False, http_status=404)
                outcomes = _mark_failed(lead_rows, error, settings)
                counts["retried"] += outcomes["retried"]
                counts["dead"] += outcomes["dead"]
                record_integration_event(
                    delivery_id,
                    "outbound",
                    "n8n",
                    "sheet_update",
                    "rejected",
                    http_status=404,
                    error_category="lead_not_found",
                )
            except SheetDeliveryError as exc:
                outcomes = _mark_failed(lead_rows, exc, settings)
                counts["retried"] += outcomes["retried"]
                counts["dead"] += outcomes["dead"]
                record_integration_event(
                    delivery_id,
                    "outbound",
                    "n8n",
                    "sheet_update",
                    "failed",
                    http_status=exc.http_status,
                    error_category="temporary" if exc.retryable else "permanent",
                )
                trace.log(
                    "integration_delivery_failed",
                    provider="n8n",
                    lead_id=lead_id,
                    outcome="retry" if outcomes["retried"] else "dead",
                    http_status=exc.http_status,
                )
            except Exception as exc:  # noqa: BLE001 - isolate one lead and retry durably
                error = SheetDeliveryError(
                    f"unexpected Sheet sync error: {type(exc).__name__}", retryable=True
                )
                outcomes = _mark_failed(lead_rows, error, settings)
                counts["retried"] += outcomes["retried"]
                counts["dead"] += outcomes["dead"]
                trace.log(
                    "integration_delivery_failed",
                    provider="n8n",
                    lead_id=lead_id,
                    outcome="retry" if outcomes["retried"] else "dead",
                    error_category=type(exc).__name__,
                )
    finally:
        if owns_client:
            client.close()
    trace.complete(**counts)
    return counts


def write_heartbeat(**details: Any) -> None:
    """Best effort: a heartbeat failure must never stop the sync."""
    try:
        with transaction() as conn:
            conn.execute(
                "insert into service_heartbeats(service,last_seen_at,details) "
                "values('sheet-worker',now(),%s) on conflict(service) do update "
                "set last_seen_at=now(),details=excluded.details",
                (Jsonb(details),),
            )
    except Exception:  # noqa: BLE001 - monitoring only
        logging.getLogger(__name__).warning("sheet_heartbeat_failed",
                                            extra={"event": "sheet_heartbeat_failed"})


DIRECT_LOCK = "rpt:sheet-direct"


def run_direct_tick(backend: SheetBackend) -> dict[str, int]:
    """SHEET_SYNC_MODE=direct: one read of the Sheet, commands, profile edits, results.

    At most two writes per poll: the "Processing" claims (they must be in the Sheet before a
    command runs, so a crash leaves a row recovery can retry), then everything else. Only one
    process may poll at a time (advisory lock): two would each claim the same new row.
    """
    settings = get_settings()
    counts: dict[str, int] = {"claimed": 0, "leads": 0, "delivered": 0, "retried": 0, "dead": 0,
                              "deferred": 0}
    if not settings.sheet_sync_enabled:
        return counts
    with get_pool().connection() as lock:
        lock.autocommit = True
        if not lock.execute("select pg_try_advisory_lock(hashtext(%s)) as ok",
                            (DIRECT_LOCK,)).fetchone()["ok"]:
            counts["locked"] = 1
            return counts
        try:
            return _direct_tick(backend, settings, counts)
        finally:
            lock.execute("select pg_advisory_unlock(hashtext(%s))", (DIRECT_LOCK,))
            lock.autocommit = False  # hand the pooled connection back as we found it


def _direct_tick(backend: SheetBackend, settings: Settings, counts: dict[str, int]) -> dict[str, int]:
    trace = WorkflowTrace("sheet_direct_tick", "sheet-worker", uuid4().hex)
    try:
        grid = parse_grid(backend.read_values(), settings.google_sheet_header_row)
    except SheetLayoutError as exc:
        trace.log("sheet_layout_invalid", logging.ERROR, reason=str(exc))
        write_heartbeat(mode="direct", ok=False, error=str(exc))
        return counts
    poll = sheet_direct.PollResult()
    claims = sheet_direct.claim_commands(grid, trace)
    claimed = flush(backend, grid)
    counts["deferred"] += len(claimed.deferred_rows)
    sheet_direct.run_claims(grid, claims, claimed.deferred_rows, poll, trace)
    sheet_direct.recover_stuck(grid, poll, settings.sheet_stuck_seconds, trace)
    sheet_direct.sync_profiles(grid, poll, trace)

    rows = _claim_sheet_work(settings)
    counts["claimed"] = len(rows)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["aggregate_id"])].append(row)
    counts["leads"] = len(grouped)
    placed: dict[str, int] = {}  # lead id -> Sheet row its snapshot was queued on
    for lead_id, lead_rows in grouped.items():
        try:
            with transaction() as conn:
                snapshot = build_sheet_snapshot(
                    conn, lead_id, practice_slug=settings.n8n_practice_slug,
                    reasons=[str((r.get("payload") or {}).get("reason", "")) for r in lead_rows],
                )
            found = sheet_direct.apply_snapshot(grid, snapshot)
        except LookupError as exc:
            _count(counts, _mark_failed(lead_rows, SheetDeliveryError(str(exc), retryable=False),
                                        settings))
            continue
        except Exception as exc:  # noqa: BLE001 - one lead must not strand the rest in 'sending'
            _count(counts, _mark_failed(lead_rows, SheetDeliveryError(
                f"unexpected Sheet sync error: {type(exc).__name__}", retryable=True), settings))
            continue
        if not found:
            # Not in the Sheet yet (or the row was deleted): retried with backoff, then dead.
            _count(counts, _mark_failed(lead_rows, SheetDeliveryError(
                "Lead ID not found in the Sheet", retryable=True, http_status=404), settings))
            continue
        placed[lead_id] = next(r.number for r in grid.rows if r.get("lead_id") == lead_id)
    try:
        written = flush(backend, grid)
    except Exception as exc:  # noqa: BLE001 - Google unavailable: every placed lead retries
        trace.log("sheet_write_failed", logging.WARNING, error_category=type(exc).__name__)
        for lead_id in placed:
            _count(counts, _mark_failed(grouped[lead_id], SheetDeliveryError(
                f"Google Sheets write failed: {type(exc).__name__}", retryable=True), settings))
        write_heartbeat(mode="direct", ok=False, error=f"write failed: {type(exc).__name__}")
        return counts
    counts["deferred"] += len(written.deferred_rows)
    poll.save_baselines(written.deferred_rows)
    for lead_id, number in placed.items():
        if number in written.deferred_rows:
            _count(counts, _mark_failed(grouped[lead_id], SheetDeliveryError(
                "Sheet row moved during the write; retrying", retryable=True), settings))
            continue
        _mark_delivered(grouped[lead_id])
        counts["delivered"] += len(grouped[lead_id])
    counts.update(poll.counts)
    write_heartbeat(mode="direct", ok=True, rows=len(grid.rows), **counts)
    trace.complete(**counts)
    return counts


def _count(counts: dict[str, int], outcomes: dict[str, int]) -> None:
    counts["retried"] += outcomes["retried"]
    counts["dead"] += outcomes["dead"]


def main() -> None:
    configure_logging("sheet-worker")
    settings = get_settings()
    errors = settings.runtime_errors("sheet-worker")
    if errors:
        raise RuntimeError("; ".join(errors))
    logging.getLogger(__name__).info(
        "sheet_worker_started",
        extra={"event": "sheet_worker_started", "details": {
            "enabled": settings.sheet_sync_enabled, "mode": settings.sheet_sync_mode}},
    )
    backend: SheetBackend | None = None
    while True:
        started = time.monotonic()
        try:
            if settings.sheet_sync_mode == "direct":
                if backend is None:
                    from .services.google_sheet import GoogleSheetBackend

                    backend = GoogleSheetBackend(settings)
                run_direct_tick(backend)
            else:
                run_sheet_sync_tick()
        except Exception:
            logging.getLogger(__name__).exception(
                "sheet_worker_tick_failed", extra={"event": "sheet_worker_tick_failed"}
            )
        time.sleep(max(0, settings.sheet_sync_poll_seconds - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
