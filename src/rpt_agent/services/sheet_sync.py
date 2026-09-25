from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from ..config import Settings, get_settings

CALL_OUTCOME_LABELS = {
    "booked": "Booked",
    "not_interested": "Answered - declined",
    "no_answer": "No answer",
    "voicemail": "Voicemail left",
    "callback": "Callback requested",
    "transferred": "Call transferred",
    "booking_link": "Booking link sent",
    "call_opt_out": "Do not contact",
    "do_not_contact": "Do not contact",
    "manual": "Needs staff review",
}


def dashboard_call_link(lead_id: str, settings: Settings | None = None) -> str | None:
    """Return the stable dashboard conversation page for a lead."""
    dashboard_url = (settings or get_settings()).dashboard_public_url.rstrip("/")
    dashboard_url = dashboard_url.removesuffix("/login")
    if not dashboard_url:
        return None
    return f"{dashboard_url}/leads/{lead_id}/conversations/calls"


def format_callback_time(value: datetime, timezone: str) -> str:
    """Format a callback for staff while preserving the exact timestamp in the database."""
    local = value.astimezone(ZoneInfo(timezone))
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.strftime('%b')} {local.day}, {local.year} at {hour}:{local:%M %p} PT"


def enqueue_sheet_update(
    conn,
    *,
    lead_id: str,
    event_type: str,
    source_key: str,
    outreach_event_id: int | None = None,
) -> None:
    """Add one idempotent, minimal n8n delivery job inside the caller's transaction."""
    identity = f"{event_type}:{source_key}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:32]
    event_id = f"n8n:{event_type}:{digest}"
    payload: dict[str, Any] = {"lead_id": str(lead_id), "reason": event_type}
    if outreach_event_id is not None:
        payload["outreach_event_id"] = int(outreach_event_id)
    conn.execute(
        "insert into integration_outbox(event_id,event_type,aggregate_id,payload,status,"
        "destination,outreach_event_id) select %s,%s,%s,%s,'pending','n8n',%s "
        "where exists(select 1 from leads where id=%s and source_system='google_sheets') "
        "on conflict(event_id) do nothing",
        (
            event_id,
            f"sheet.{event_type}",
            str(lead_id),
            Jsonb(payload),
            outreach_event_id,
            lead_id,
        ),
    )


def _action_label(lead: dict[str, Any]) -> str | None:
    """Action Status for Sheet sync — only system outcomes, never intake command results.

    Intake/recovery already write Lead ID and Cadence started / restarted / DNC.
    Re-sending those from the AWS webhook overwrote the intake row and made it look
    like the status webhook owned start/Lead ID. Only emit labels that outreach can
    set after the fact (completed cadence, voice/tool DNC).
    """
    if lead["status"] == "booked":
        return "Booked"
    if lead["status"] == "do_not_contact":
        return "Do not contact applied"
    if lead["cadence_state"] == "completed":
        return "Cadence completed"
    return None


def _call_label(event: dict[str, Any], lead: dict[str, Any]) -> str:
    if lead["status"] == "invalid_phone":
        return "Wrong number"
    if event.get("status") in {"failed", "unknown"}:
        return "Failed" if event["status"] == "failed" else "Needs staff review"
    return CALL_OUTCOME_LABELS.get(event.get("outcome"), "Needs staff review")


def _sms_label(event: dict[str, Any]) -> str:
    if event.get("delivery_status") == "delivered":
        return "Delivered"
    if event.get("delivery_status") == "sent":
        return "Sent"
    if event.get("status") == "unknown":
        return "Needs staff review"
    return "Not delivered"


def format_cadence_result(
    events: list[dict[str, Any]], lead: dict[str, Any]
) -> tuple[str | None, str | None]:
    """Return the human Sheet labels for one cadence day."""
    event_label, call_outcome, message_outcome, _ = format_cadence_columns(events, lead)
    labels = [
        ("Call", call_outcome),
        ("SMS", message_outcome),
    ]
    populated = [(channel, label) for channel, label in labels if label is not None]
    if not populated:
        return event_label, None
    if len(populated) == 1:
        return event_label, populated[0][1]
    return event_label, ", ".join(f"{channel}: {label}" for channel, label in populated)


def format_cadence_columns(
    events: list[dict[str, Any]], lead: dict[str, Any]
) -> tuple[str | None, str | None, str | None, str | None]:
    """Return separate Sheet outcomes while email outreach remains unsupported."""
    latest_by_channel: dict[str, dict[str, Any]] = {}
    for event in events:
        latest_by_channel[event["channel"]] = event
    call_outcome = _call_label(call, lead) if (call := latest_by_channel.get("call")) else None
    message_outcome = _sms_label(sms) if (sms := latest_by_channel.get("sms")) else None
    channels = [
        channel
        for channel, outcome in (("Call", call_outcome), ("SMS", message_outcome))
        if outcome is not None
    ]
    return " + ".join(channels) or None, call_outcome, message_outcome, None


def build_sheet_snapshot(
    conn, lead_id: str, *, practice_slug: str | None = None
) -> dict[str, Any]:
    """Build the current Sheet view from committed database truth."""
    lead = conn.execute(
        "select l.id,l.status,l.cadence_state,l.needs_review,l.callback_requested_at,"
        "coalesce(l.timezone,p.timezone,'America/Los_Angeles') as timezone "
        "from leads l join practices p on p.id=l.practice_id where l.id=%s "
        "and (%s::text is null or p.slug=%s::text)",
        (lead_id, practice_slug, practice_slug),
    ).fetchone()
    if not lead:
        raise LookupError("lead not found")

    sheet_action = conn.execute(
        "select request_id from lead_action_requests where lead_id=%s and status='completed' "
        "order by completed_at desc nulls last,created_at desc limit 1",
        (lead_id,),
    ).fetchone()
    action_request_id = str(sheet_action["request_id"]) if sheet_action else None

    run_action = conn.execute(
        "select request_id,created_at,"
        "coalesce(response_body#>>'{body,result}',response_body->>'result') as result "
        "from lead_action_requests where lead_id=%s and status='completed' "
        "and coalesce(response_body#>>'{body,result}',response_body->>'result') "
        "in ('cadence_started','cadence_restarted','already_started') "
        "order by completed_at desc nulls last,created_at desc limit 1",
        (lead_id,),
    ).fetchone()
    run_started_at = (
        run_action["created_at"]
        if run_action and run_action["result"] != "already_started"
        else None
    )

    latest = conn.execute(
        "select oe.day_offset,coalesce(sm.delivered_at,oe.settled_at,oe.executed_at,oe.updated_at) "
        "as finished_at from outreach_events oe "
        "left join sms_messages sm on sm.outreach_event_id=oe.id "
        "where oe.lead_id=%s "
        "and (%s::timestamptz is null or oe.created_at>=%s::timestamptz) and ("
        "(oe.channel='call' and oe.status in ('delivered','failed','unknown') "
        "and oe.settled_at is not null) or "
        "(oe.channel='sms' and (sm.delivery_status in ('sent','delivered','failed','undelivered') "
        "or (oe.status in ('failed','unknown') and oe.settled_at is not null)))) "
        "order by finished_at desc nulls last,oe.id desc limit 1",
        (lead_id, run_started_at, run_started_at),
    ).fetchone()

    events: list[dict[str, Any]] = []
    cadence_day = None
    if latest:
        events = conn.execute(
            "select oe.id,oe.channel,oe.status,oe.outcome,sm.delivery_status,"
            "coalesce(sm.delivered_at,oe.settled_at,oe.executed_at,oe.updated_at) as finished_at "
            "from outreach_events oe left join sms_messages sm on sm.outreach_event_id=oe.id "
            "where oe.lead_id=%s and oe.day_offset is not distinct from %s "
            "and (%s::timestamptz is null or oe.created_at>=%s::timestamptz) and ("
            "(oe.channel='call' and oe.status in ('delivered','failed','unknown') "
            "and oe.settled_at is not null) or "
            "(oe.channel='sms' and (sm.delivery_status in ('sent','delivered','failed','undelivered') "
            "or (oe.status in ('failed','unknown') and oe.settled_at is not null)))) "
            "order by finished_at,oe.id",
            (lead_id, latest["day_offset"], run_started_at, run_started_at),
        ).fetchall()
        cadence_day = (
            f"Day {int(latest['day_offset'])}"
            if latest["day_offset"] is not None
            else "Callback"
        )

    cadence, call_outcome, message_outcome, email_outcome = format_cadence_columns(events, lead)
    _, cadence_status = format_cadence_result(events, lead)
    callback_at: str | None = None
    if isinstance(lead.get("callback_requested_at"), datetime):
        callback_at = format_callback_time(
            lead["callback_requested_at"], lead["timezone"]
        )

    # Omit action_status unless outreach changed it — n8n keeps the existing cell.
    sheet: dict[str, Any] = {
        "cadence_day": cadence_day,
        "cadence": cadence,
        "cadence_status": cadence_status,
        "call_outcome": call_outcome,
        "message_outcome": message_outcome,
        "email_outcome": email_outcome,
        "needs_review": "Needs Review" if lead["needs_review"] else "",
        "transcript_link": dashboard_call_link(str(lead["id"])),
        "callback_at": callback_at,
    }
    action_status = _action_label(lead)
    if action_status is not None:
        sheet["action_status"] = action_status
    # Booked ends the lead however it happened (board, Sheet or call), so the
    # Action cell must stop showing the last command staff picked - "Restart
    # cadence" next to "Booked" reads as a pending instruction. Other states
    # omit the key so n8n leaves the cell as staff set it.
    if lead["status"] == "booked":
        sheet["action"] = "Booked"

    return {
        "lead_id": str(lead["id"]),
        "action_request_id": action_request_id,
        "sheet": sheet,
    }
