"""Keap sync health for the dashboard, in plain words.

Computed on read from keap_sync, keap_connections and the keap-worker heartbeat, so
an alert disappears by itself once its cause is gone. No patient data: failed rows
are shown as a reason and a time only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

HEARTBEAT_STALE_MINUTES = 5
BACKLOG_ROWS = 500
BACKLOG_HOURS = 1
BUDGET_WARNING_SHARE = 0.8
_LEVEL = {"ok": 0, "info": 0, "warning": 1, "problem": 2}


def _alert(level: str, title: str, detail: str = "") -> dict[str, str]:
    return {"level": level, "title": title, "detail": detail}


def keap_sync_status(conn, practice_id: int, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    settings = conn.execute(
        "select keap_sync_start_at from practice_settings where practice_id=%s", (practice_id,)
    ).fetchone() or {}
    connection = conn.execute(
        "select keap_account,status,access_token_expires_at,last_error from keap_connections "
        "where practice_id=%s",
        (practice_id,),
    ).fetchone()
    beat = conn.execute(
        "select last_seen_at,details from service_heartbeats where service='keap-worker'"
    ).fetchone()
    counts = {
        r["status"]: r["n"]
        for r in conn.execute(
            "select status,count(*) as n from keap_sync where practice_id=%s group by status",
            (practice_id,),
        )
    }
    synced_today = conn.execute(
        "select count(*) as n from keap_sync where practice_id=%s and synced_at>=%s",
        (practice_id, day_start),
    ).fetchone()["n"]
    backlog = conn.execute(
        "select count(*) as n from keap_sync where practice_id=%s and status='pending' "
        "and next_attempt_at<=%s and updated_at<%s",
        (practice_id, now, now - timedelta(hours=BACKLOG_HOURS)),
    ).fetchone()["n"]
    problems = conn.execute(
        "select status,last_error,skip_reason,updated_at from keap_sync where practice_id=%s "
        "and status in ('failed','needs_review') order by updated_at desc limit 10",
        (practice_id,),
    ).fetchall()

    details = (beat or {}).get("details") or {}
    age_minutes = (now - beat["last_seen_at"]).total_seconds() / 60 if beat else None
    enabled = bool(details.get("enabled"))
    reconnect = bool(connection and connection["status"] != "active")
    alerts: list[dict[str, str]] = []

    if beat is None or not enabled:
        overall, headline = "switched_off", "Keap sync is switched off."
    elif settings.get("keap_sync_start_at") is None or connection is None:
        overall, headline = "not_started", "Keap sync is not set up yet."
    else:
        overall, headline = "ok", "Keap is in step with our records."

    if enabled and age_minutes is not None and age_minutes > HEARTBEAT_STALE_MINUTES:
        alerts.append(_alert(
            "problem", "The Keap sync service has stopped",
            f"It last reported {int(age_minutes)} minutes ago. New bookings are not reaching Keap.",
        ))
    if reconnect:
        alerts.append(_alert(
            "problem", "Reconnect Keap",
            "Keap has stopped accepting our login. A super admin needs to connect it again; "
            "changes are waiting and nothing is lost.",
        ))
    if counts.get("failed"):
        alerts.append(_alert(
            "warning", f"{counts['failed']} people could not be sent to Keap",
            "See the reasons below. They are retried automatically if their details change.",
        ))
    if counts.get("needs_review"):
        alerts.append(_alert(
            "warning", f"{counts['needs_review']} people need a human decision",
            "Keap has more than one possible match for them. Staff should merge them in Keap.",
        ))
    if backlog > BACKLOG_ROWS:
        alerts.append(_alert(
            "warning", "Keap updates are running behind",
            f"{backlog} people have been waiting more than {BACKLOG_HOURS} hour.",
        ))
    budget = details.get("requests_budget")
    used = details.get("requests_today")
    if budget and used is not None and used / budget >= BUDGET_WARNING_SHARE:
        alerts.append(_alert(
            "warning", "Keap daily request limit is almost used",
            f"{used} of {budget} requests used today.",
        ))

    worst = max((_LEVEL[a["level"]] for a in alerts), default=0)
    if overall == "ok" and worst:
        overall = "problem" if worst == 2 else "attention"
        headline = ("Keap sync needs attention." if worst == 1
                    else "Keap is not being updated.")

    today = details.get("today") or {}
    return {
        "overall": overall,
        "headline": headline,
        "connection": {
            "connected": bool(connection and connection["status"] == "active"),
            "account": connection["keap_account"] if connection else None,
            "token_valid_until": connection["access_token_expires_at"] if connection else None,
            "reconnect_needed": reconnect,
        },
        "counts": {
            "synced": counts.get("synced", 0),
            "waiting": counts.get("pending", 0) + counts.get("retrying", 0) + counts.get("syncing", 0),
            "failed": counts.get("failed", 0),
            "needs_review": counts.get("needs_review", 0),
            "skipped": counts.get("skipped", 0),
        },
        "today": {
            "people_synced": synced_today,
            "contacts_created": today.get("created", 0),
            "contacts_updated": today.get("updated", 0),
            "tags_applied": today.get("tags_applied", 0),
            "tags_removed": today.get("tags_removed", 0),
        },
        "problems": [
            {"reason": p["last_error"] or p["skip_reason"] or "needs a person", "since": p["updated_at"]}
            for p in problems
        ],
        "alerts": alerts,
        "heartbeat_age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
    }
