"""Stride sync health for the dashboard: the watchdog, in plain words.

Computed on read from the import history and the stride-worker heartbeat, so
there is no alert state to keep in step: an alert disappears by itself once the
cause is gone. Wording is for clinic staff, not engineers. No patient data.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .lead_status import _window_for

HEARTBEAT_STALE_MINUTES = 5
FILE_WAITING_MINUTES = 15
QUIET_MINUTES = 30  # Stride is expected every ~5 minutes during clinic hours
DISK_WARNING_PERCENT = 20
DISK_PROBLEM_PERCENT = 10

FILE_LABELS = {
    "patients": "Patients",
    "patient_cases": "Cases",
    "appointments": "Appointments",
    "users": "Clinicians",
    "locations": "Locations",
    "notes": "Visit notes",
    "patient_insurances": "Insurance",
    "payers": "Insurance companies",
    "providers": "Referring doctors",
    "unknown": "Unrecognized file",
}
STATUS_LABELS = {
    "processed": "Loaded",
    "pending": "Loading",
    "retrying": "Retrying",
    "rejected": "Not loaded",
}
_LEVEL = {"ok": 0, "info": 0, "warning": 1, "problem": 2}


def plain_reason(error: str | None) -> str:
    """Our internal reasons, rewritten for staff."""
    text = (error or "").strip()
    if text.startswith("missing columns"):
        return "The file was missing information we need (" + text.split(":", 1)[-1].strip() + ")."
    if text.startswith("too many bad rows"):
        counts = re.match(r"too many bad rows: (\d+) of (\d+)", text)
        share = f" ({counts[1]} of {counts[2]} rows)" if counts else ""
        return f"Too many rows in the file had errors{share}, so none of it was loaded."
    if text.startswith("file is larger"):
        return "The file was too large to load safely."
    if text.startswith(("unrecognized file name", "file name")):
        return "The file name was not one Stride normally sends."
    if text.startswith("file is empty"):
        return "The file was empty."
    if text.startswith("CSV could not be parsed"):
        return "The file was damaged and could not be read."
    if text.startswith(("gave up", "import failed")):
        return "It could not be loaded after several tries."
    if text.startswith("another importer"):
        return "Waiting for another load to finish."
    return "It could not be loaded."


def _alert(level: str, title: str, detail: str = "") -> dict[str, str]:
    return {"level": level, "title": title, "detail": detail}


def _in_business_hours(now: datetime, tz: ZoneInfo, hours: dict, holidays: list) -> bool:
    if not hours:
        return True
    local = now.astimezone(tz)
    window = _window_for(hours, holidays, local.date())
    return bool(window) and window[0] <= local.time() < window[1]


def stride_sync_status(conn, practice_id: int, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    clinic = conn.execute(
        "select coalesce(s.stride_location_timezone,p.timezone) as tz,s.business_hours,s.holidays "
        "from practices p left join practice_settings s on s.practice_id=p.id where p.id=%s",
        (practice_id,),
    ).fetchone()
    tz = ZoneInfo(clinic["tz"] if clinic and clinic["tz"] else "America/New_York")
    day_start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    beat = conn.execute(
        "select last_seen_at,details from service_heartbeats where service='stride-worker'"
    ).fetchone()
    files = conn.execute(
        "select max(received_at) as last_received,"
        "max(processed_at) filter (where status='processed') as last_loaded,"
        "count(*) filter (where received_at>=%s) as files_today,"
        "coalesce(sum(issue_count) filter (where received_at>=%s and status='processed'),0) as skipped_today,"
        "count(*) as files_ever from stride_import_files where practice_id=%s",
        (day_start, day_start, practice_id),
    ).fetchone()
    today = conn.execute(
        "select "
        "(select count(*) from stride_patients where practice_id=%(p)s and created_at>=%(d)s) as new_patients,"
        "(select count(*) from stride_patients where practice_id=%(p)s and created_at<%(d)s "
        " and updated_at>=%(d)s) as updated_patients,"
        "(select count(*) from stride_appointments where practice_id=%(p)s and created_at>=%(d)s "
        " and deleted_at is null) as new_appointments,"
        "(select count(*) from stride_appointments where practice_id=%(p)s and updated_at>=%(d)s "
        " and (deleted_at>=%(d)s or lower(coalesce(status,'')) in ('cancel','cancelled','late cancel'))) "
        " as cancelled_appointments,"
        "(select count(*) from stride_appointments where practice_id=%(p)s and updated_at>=%(d)s "
        " and lower(coalesce(status,''))='checked in') as checked_in,"
        "(select count(*) from lead_status_history h join leads l on l.id=h.lead_id where l.practice_id=%(p)s "
        " and h.source='stride' and h.to_status='booked' and h.changed_at>=%(d)s) as leads_booked,"
        "(select count(*) from lead_status_history h join leads l on l.id=h.lead_id where l.practice_id=%(p)s "
        " and h.source='stride' and h.to_status='needs_attention' and h.changed_at>=%(d)s) as bookings_cancelled",
        {"p": practice_id, "d": day_start},
    ).fetchone()
    ambiguous = conn.execute(
        "select count(*) as n from stride_patients where practice_id=%s and lead_match_status='ambiguous' "
        "and deleted_at is null",
        (practice_id,),
    ).fetchone()["n"]
    problem_files = conn.execute(
        "select file_name,entity,status,error,received_at from stride_import_files where practice_id=%s "
        "and status in ('rejected','retrying') and received_at>=%s order by received_at desc limit 5",
        (practice_id, now - timedelta(days=7)),
    ).fetchall()
    recent = conn.execute(
        "select file_name,entity,status,row_count,applied_count,issue_count,error,received_at,processed_at "
        "from stride_import_files where practice_id=%s order by received_at desc,id desc limit 20",
        (practice_id,),
    ).fetchall()
    week = conn.execute(
        "select (received_at at time zone %s)::date as day,count(*) as files,"
        "coalesce(sum(applied_count),0) as changes,count(*) filter (where status='rejected') as problems "
        "from stride_import_files where practice_id=%s and received_at>=%s group by 1 order by 1",
        (str(tz), practice_id, day_start - timedelta(days=6)),
    ).fetchall()

    alerts: list[dict[str, str]] = []
    details = (beat or {}).get("details") or {}
    service = {
        "running": False,
        "last_seen_at": beat["last_seen_at"] if beat else None,
        "state": details.get("state", "idle"),
        "current_file": details.get("current_file"),
        "rows_read": details.get("rows_read"),
        "files_waiting": details.get("inbox_waiting", 0),
        "disk_free_percent": details.get("disk_free_percent"),
    }
    if not beat:
        alerts.append(_alert("info", "The Stride connection has not started yet",
                             "Nothing has been received from Stride so far."))
    elif now - beat["last_seen_at"] > timedelta(minutes=HEARTBEAT_STALE_MINUTES):
        alerts.append(_alert("problem", "The Stride import has stopped",
                             "The service that loads Stride's files has not checked in for a few minutes. "
                             "New files from Stride wait safely and load once it is running again."))
    else:
        service["running"] = True
        if details.get("enabled") is False:
            alerts.append(_alert("warning", "The Stride import is switched off",
                                 "Files from Stride are kept but not loaded until it is switched on."))
    if service["running"] and service["state"] == "importing":
        rows = service["rows_read"] or 0
        alerts.append(_alert("info", "A large file is loading",
                             f"{rows:,} rows read so far. Newer updates will follow right after."))
    waiting_minutes = details.get("oldest_waiting_minutes") or 0
    if beat and service["files_waiting"] and waiting_minutes > FILE_WAITING_MINUTES:
        alerts.append(_alert("warning", f"{service['files_waiting']} file(s) waiting to be loaded",
                             f"The oldest has been waiting about {round(waiting_minutes)} minutes."))
    for row in problem_files:
        label = FILE_LABELS.get(row["entity"], "Stride")
        if row["status"] == "rejected":
            alerts.append(_alert("problem", f"A {label.lower()} file from Stride was not loaded",
                                 plain_reason(row["error"])))
        else:
            alerts.append(_alert("warning", f"A {label.lower()} file is being retried",
                                 "It will be tried again automatically."))
    disk = service["disk_free_percent"]
    if disk is not None and disk < DISK_PROBLEM_PERCENT:
        alerts.append(_alert("problem", "Storage for Stride files is almost full",
                             f"Only {disk:g}% free. New files may not arrive."))
    elif disk is not None and disk < DISK_WARNING_PERCENT:
        alerts.append(_alert("warning", "Storage for Stride files is getting full", f"{disk:g}% free."))
    last_received = files["last_received"]
    if (
        files["files_ever"]
        and last_received
        and now - last_received > timedelta(minutes=QUIET_MINUTES)
        and _in_business_hours(now, tz, (clinic or {}).get("business_hours") or {},
                               (clinic or {}).get("holidays") or [])
    ):
        minutes = int((now - last_received).total_seconds() // 60)
        alerts.append(_alert("warning", "No updates from Stride recently",
                             f"The last file arrived {minutes} minutes ago; Stride usually sends one every "
                             "few minutes during clinic hours."))
    if files["skipped_today"]:
        # A warning, not info: a skipped row is a patient or visit we did not load (marked so staff see it).
        alerts.append(_alert("warning", f"{files['skipped_today']} row(s) skipped today",
                             "Some rows in Stride's files had errors and were left out; the rest loaded. "
                             "Each file lists the row numbers and reasons."))
    if ambiguous:
        alerts.append(_alert("info", f"{ambiguous} patient(s) match more than one lead",
                             "We did not guess. Please check these by hand."))

    worst = max((_LEVEL[a["level"]] for a in alerts), default=0)
    if not beat:
        overall = "not_started"
    else:
        overall = {0: "ok", 1: "attention", 2: "problem"}[worst]
    headline = {
        "ok": "Stride updates are arriving and loading normally.",
        "attention": "Stride updates are loading, but something needs a look.",
        "problem": "There is a problem with Stride updates.",
        "not_started": "The Stride connection has not started yet.",
    }[overall]
    return {
        "overall": overall,
        "headline": headline,
        "last_file_received_at": last_received,
        "last_loaded_at": files["last_loaded"],
        "service": service,
        "alerts": alerts,
        "today": {
            "files_received": files["files_today"],
            "new_patients": today["new_patients"],
            "updated_patients": today["updated_patients"],
            "new_appointments": today["new_appointments"],
            "cancelled_appointments": today["cancelled_appointments"],
            "checked_in": today["checked_in"],
            "leads_marked_booked": today["leads_booked"],
            "bookings_cancelled": today["bookings_cancelled"],
            "rows_skipped": files["skipped_today"],
        },
        "last_7_days": [
            {"date": row["day"], "files": row["files"], "changes": row["changes"], "problems": row["problems"]}
            for row in week
        ],
        "recent_files": [
            {
                "kind": FILE_LABELS.get(row["entity"], "Stride file"),
                "file_name": row["file_name"],
                "received_at": row["received_at"],
                "loaded_at": row["processed_at"],
                "status": STATUS_LABELS.get(row["status"], row["status"]),
                "rows": row["row_count"],
                "changes": row["applied_count"],
                "rows_skipped": row["issue_count"],
                "note": plain_reason(row["error"]) if row["status"] in {"rejected", "retrying"} else "",
            }
            for row in recent
        ],
    }
