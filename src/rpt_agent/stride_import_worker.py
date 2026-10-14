"""Import Stride CSV exports dropped into the SFTP inbox.

Each tick: take every finished file (untouched for STRIDE_FILE_SETTLE_SECONDS,
temporary upload names ignored), oldest export first, and apply each one in a
single database transaction, so a file lands completely or not at all. Files
are streamed, never loaded whole, so memory stays flat for any size.

Order is strict: if a file fails for a temporary reason, later files wait
behind it. A file that can never work (bad name, missing column, too many bad
rows, too big, or STRIDE_IMPORT_MAX_ATTEMPTS failures) moves to the error folder
and the queue continues. After the files, Stride patients are reconciled with
our leads (link, booked, cancelled).

The worker also writes a heartbeat with what only it can see (files waiting,
disk space, progress on a long file); the dashboard turns that into alerts.

Separate process from the cadence worker so a large import never delays a call.
"""

from __future__ import annotations

import codecs
import hashlib
import logging
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb

from .config import Settings, get_settings
from .db import transaction
from .observability import WorkflowTrace, configure_logging
from .services.stride_import import (
    StrideFileError,
    apply_file,
    file_sort_key,
    parse_file_name,
)
from .services.stride_leads import flag_unclear_deletes, reconcile_leads

SERVICE = "stride-worker"
# One importer at a time, even if a second container is started by mistake.
LOCK_SQL = "select pg_try_advisory_xact_lock(hashtext('rpt:stride-import')) as locked"
# Names SFTP clients use while a file is still uploading (WinSCP, FileZilla, scripts).
TEMPORARY_SUFFIXES = (".part", ".partial", ".tmp", ".filepart", ".uploading")


def _practice(conn, settings: Settings) -> dict[str, Any]:
    practice = conn.execute(
        "select p.id,coalesce(s.stride_location_timezone,p.timezone) as timezone "
        "from practices p left join practice_settings s on s.practice_id=p.id where p.slug=%s",
        (settings.stride_practice_slug,),
    ).fetchone()
    if not practice:
        raise RuntimeError(f"practice {settings.stride_practice_slug!r} not found")
    return practice


def _inbox_files(settings: Settings) -> list[Path]:
    inbox = settings.stride_inbox_dir
    if not inbox.is_dir():
        return []
    return [
        path for path in inbox.iterdir()
        if path.is_file() and not path.name.startswith(".")
        and not path.name.lower().endswith(TEMPORARY_SUFFIXES)
    ]


def ready_files(settings: Settings, now: float | None = None) -> list[Path]:
    now = time.time() if now is None else now
    files = [
        path for path in _inbox_files(settings)
        if now - path.stat().st_mtime >= settings.stride_file_settle_seconds
    ]
    return sorted(files, key=lambda path: file_sort_key(path.name))


def write_heartbeat(settings: Settings, **details: Any) -> None:
    """Best effort: a heartbeat failure must never stop an import."""
    files = _inbox_files(settings)
    oldest = min((path.stat().st_mtime for path in files), default=None)
    try:
        disk = shutil.disk_usage(settings.stride_inbox_dir)
        disk_free_percent = round(disk.free * 100 / disk.total, 1)
        disk_free_gb = round(disk.free / 1024**3, 2)
    except OSError:
        disk_free_percent = disk_free_gb = None
    payload = {
        "enabled": settings.stride_import_enabled,
        "inbox_waiting": len(files),
        "oldest_waiting_minutes": round((time.time() - oldest) / 60, 1) if oldest else 0,
        "disk_free_percent": disk_free_percent,
        "disk_free_gb": disk_free_gb,
        "state": "idle",
        **details,
    }
    try:
        with transaction() as conn:
            conn.execute(
                "insert into service_heartbeats(service,last_seen_at,details) values(%s,now(),%s) "
                "on conflict(service) do update set last_seen_at=now(),details=excluded.details",
                (SERVICE, Jsonb(payload)),
            )
    except Exception:  # noqa: BLE001 - monitoring only
        logging.getLogger(__name__).warning("stride_heartbeat_failed", extra={"event": "stride_heartbeat_failed"})


def _scan(path: Path) -> tuple[str, str]:
    """Checksum and encoding in one streaming pass (never the whole file in memory)."""
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")()
    utf8 = True
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            if utf8:
                try:
                    decoder.decode(chunk)
                except UnicodeDecodeError:
                    utf8 = False
    if utf8:
        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            utf8 = False
    # Spreadsheet tools sometimes save Windows-1252; better than rejecting the file.
    return digest.hexdigest(), "utf-8-sig" if utf8 else "cp1252"


def _move(path: Path, folder: Path) -> Path:
    target_dir = folder / datetime.now(UTC).strftime("%Y-%m-%d")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    counter = 1
    while target.exists():
        target = target_dir / f"{path.stem}.{counter}{path.suffix}"
        counter += 1
    # A rename when inbox and archive share a disk; otherwise copy then delete.
    shutil.move(str(path), str(target))
    return target


def _start_attempt(
    settings: Settings, name: str, entity: str, exported_at, sha256: str, size: int
) -> dict[str, Any]:
    """Record the attempt before any work, in its own transaction.

    If the import then dies (out of memory, container killed), the attempt is
    already counted, so a poison file cannot block the queue forever.
    """
    with transaction() as conn:
        practice = _practice(conn, settings)
        return conn.execute(
            "insert into stride_import_files(practice_id,file_name,entity,exported_at,sha256,size_bytes,"
            "status,attempts,started_at) values(%s,%s,%s,%s,%s,%s,'pending',1,now()) "
            "on conflict(file_name,sha256) do update set "
            "attempts=stride_import_files.attempts+case when stride_import_files.status='processed' "
            " then 0 else 1 end,"
            "status=case when stride_import_files.status='processed' then 'processed' else 'pending' end,"
            "started_at=now() returning id,status,attempts",
            (practice["id"], name, entity, exported_at, sha256, size),
        ).fetchone()


def _finish(file_id: int, status: str, error: str | None) -> None:
    with transaction() as conn:
        conn.execute(
            "update stride_import_files set status=%s,error=%s,processed_at=case when %s='rejected' "
            "then now() else processed_at end where id=%s",
            (status, error[:500] if error else None, status, file_id),
        )


def _reject_unregistered(settings: Settings, path: Path, sha256: str, size: int, reason: str) -> None:
    """A file we cannot even name gets a record so the dashboard can show it."""
    with transaction() as conn:
        practice = _practice(conn, settings)
        conn.execute(
            "insert into stride_import_files(practice_id,file_name,entity,sha256,size_bytes,status,attempts,"
            "error,started_at,processed_at) values(%s,%s,'unknown',%s,%s,'rejected',1,%s,now(),now()) "
            "on conflict(file_name,sha256) do update set status='rejected',error=excluded.error,"
            "attempts=stride_import_files.attempts+1,processed_at=now()",
            (practice["id"], path.name, sha256, size, reason[:500]),
        )


def replan_keap_since(started_at) -> None:
    """After an import commits: re-plan every Keap row planned while it was running.

    Rows written by the import carry its START time; a Keap plan made before the commit missed
    them and would never look again (keap_desired._SELECT_ROWS). Clearing desired_at forces a
    fresh plan from the committed data.
    """
    if started_at is None:
        return
    try:
        with transaction() as conn:
            conn.execute(
                "update keap_sync set desired_at=null where desired_at>=%s and status<>'syncing'",
                (started_at,),
            )
    except Exception:  # noqa: BLE001 - the planner's grace period still catches most of these
        logging.getLogger(__name__).warning("keap_replan_failed", extra={"event": "keap_replan_failed"})


def process_file(path: Path, settings: Settings, trace: WorkflowTrace) -> str:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return "gone"
    if size > settings.stride_max_file_mb * 1024 * 1024:
        reason = f"file is larger than {settings.stride_max_file_mb} MB"
        _reject_unregistered(settings, path, f"size:{size}", size, reason)
        _move(path, settings.stride_error_dir)
        trace.log("stride_file_rejected", logging.WARNING, file=path.name, reason=reason)
        return "rejected"
    sha256, encoding = _scan(path)
    try:
        entity, exported_at = parse_file_name(path.name)
    except StrideFileError as exc:
        _reject_unregistered(settings, path, sha256, size, str(exc))
        _move(path, settings.stride_error_dir)
        trace.log("stride_file_rejected", logging.WARNING, file=path.name, reason=str(exc))
        return "rejected"

    attempt = _start_attempt(settings, path.name, entity, exported_at, sha256, size)
    if attempt["status"] == "processed":
        _move(path, settings.stride_archive_dir)
        return "duplicate"
    if attempt["attempts"] > settings.stride_import_max_attempts:
        _finish(attempt["id"], "rejected", "gave up after repeated failures")
        _move(path, settings.stride_error_dir)
        return "failed"

    def progress(rows: int) -> None:
        write_heartbeat(settings, state="importing", current_file=path.name, rows_read=rows)

    started_at = None
    try:
        with transaction() as conn:
            if not conn.execute(LOCK_SQL).fetchone()["locked"]:
                _finish(attempt["id"], "retrying", "another importer is running")
                return "locked"
            started_at = conn.execute("select now() as t").fetchone()["t"]
            # A very large file may take minutes; never cut it off half way.
            conn.execute("set local statement_timeout = 0")
            practice = _practice(conn, settings)
            with path.open(encoding=encoding, errors="replace", newline="") as stream:
                result = apply_file(
                    conn,
                    entity,
                    stream,
                    practice_id=practice["id"],
                    import_file_id=attempt["id"],
                    timezone=practice["timezone"],
                    max_bad_row_percent=settings.stride_max_bad_row_percent,
                    min_bad_rows_to_reject=settings.stride_min_bad_rows_to_reject,
                    progress=progress,
                )
            if result.unclear_delete_patients:
                flag_unclear_deletes(conn, practice["id"], result.unclear_delete_patients)
            conn.execute(
                "update stride_import_files set status='processed',row_count=%s,applied_count=%s,"
                "unchanged_count=%s,issue_count=%s,issues=%s,error=null,processed_at=now() where id=%s",
                (result.row_count, result.applied, result.unchanged, len(result.issues),
                 Jsonb(result.issues[:500]), attempt["id"]),
            )
    except StrideFileError as exc:
        # Retrying cannot fix a missing column or a file full of bad rows: park it.
        _finish(attempt["id"], "rejected", str(exc))
        _move(path, settings.stride_error_dir)
        trace.log("stride_file_rejected", logging.WARNING, file=path.name, reason=str(exc))
        return "rejected"
    except Exception as exc:  # noqa: BLE001 - keep the file and retry on the next tick
        # Type only: a database error message can quote patient values.
        error = f"import failed: {type(exc).__name__}"
        final = attempt["attempts"] >= settings.stride_import_max_attempts
        try:
            _finish(attempt["id"], "rejected" if final else "retrying", error)
        except Exception:  # noqa: BLE001 - database still down; the attempt is already counted
            logging.getLogger(__name__).warning(
                "stride_file_status_not_saved", extra={"event": "stride_file_status_not_saved"}
            )
        trace.log("stride_file_failed", logging.ERROR, file=path.name, attempts=attempt["attempts"],
                  error_category=type(exc).__name__)
        if final:
            _move(path, settings.stride_error_dir)
            return "failed"
        return "retry"
    replan_keap_since(started_at)
    trace.log(
        "stride_file_processed",
        file=path.name,
        entity=entity,
        rows=result.row_count,
        applied=result.applied,
        unchanged=result.unchanged,
        issues=len(result.issues),
    )
    _move(path, settings.stride_archive_dir)
    return "processed"


def run_stride_import_tick(now: float | None = None) -> dict[str, int]:
    settings = get_settings()
    counts: dict[str, int] = {}
    if not settings.stride_import_enabled:
        write_heartbeat(settings)
        return counts
    trace = WorkflowTrace("stride_import_tick", SERVICE, uuid4().hex)
    write_heartbeat(settings)
    for path in ready_files(settings, now):
        outcome = process_file(path, settings, trace)
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome in {"retry", "locked"}:
            # Strict order: later exports wait until this one lands or is parked.
            break
    reconciled_at = None
    with transaction() as conn:
        if conn.execute(LOCK_SQL).fetchone()["locked"]:
            reconciled_at = conn.execute("select now() as t").fetchone()["t"]
            for key, value in reconcile_leads(conn, _practice(conn, settings)["id"]).items():
                counts[f"leads_{key}"] = value
    replan_keap_since(reconciled_at)
    write_heartbeat(settings, last_tick=counts)
    trace.complete(**counts)
    return counts


def main() -> None:
    configure_logging(SERVICE)
    settings = get_settings()
    errors = settings.runtime_errors(SERVICE)
    if errors:
        raise RuntimeError("; ".join(errors))
    logging.getLogger(__name__).info(
        "stride_worker_started",
        extra={"event": "stride_worker_started", "details": {"enabled": settings.stride_import_enabled}},
    )
    while True:
        started = time.monotonic()
        try:
            run_stride_import_tick()
        except Exception:
            logging.getLogger(__name__).exception(
                "stride_worker_tick_failed", extra={"event": "stride_worker_tick_failed"}
            )
        time.sleep(max(0, settings.stride_import_poll_seconds - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
