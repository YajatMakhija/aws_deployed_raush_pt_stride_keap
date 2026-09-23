from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

from ..db import transaction
from ..observability import WorkflowTrace
from .sheet_sync import enqueue_sheet_update

CALLBACK_MIN_MINUTES = 5
CALLBACK_MAX_MINUTES = 240


def _practice_clock(conn, lead_id: str) -> tuple[ZoneInfo, dict, list]:
    row = conn.execute(
        "select coalesce(l.timezone,p.timezone) as tz,ps.business_hours,ps.holidays "
        "from leads l join practices p on p.id=l.practice_id "
        "join practice_settings ps on ps.practice_id=l.practice_id where l.id=%s",
        (lead_id,),
    ).fetchone()
    if not row:
        raise ValueError("lead not found")
    return ZoneInfo(row["tz"] or "America/Los_Angeles"), row["business_hours"] or {}, row["holidays"] or []


def resolve_callback_time(
    tz: ZoneInfo,
    *,
    callback_requested_at: datetime | None,
    callback_type: str | None,
    delay_minutes: str | int | None,
    callback_datetime_iso: str | None,
    now: datetime | None = None,
) -> datetime:
    """Turn the voice tool's callback fields into one absolute UTC moment.

    The model supplies a duration or a wall-clock string; the arithmetic and the
    timezone stay here so a mis-calculated offset cannot reach the database.
    """
    now = now or datetime.now(UTC)
    kind = (callback_type or "").strip().lower()
    if kind == "relative" or (not kind and delay_minutes not in (None, "")):
        try:
            minutes = int(float(str(delay_minutes).strip()))
        except (TypeError, ValueError) as exc:
            raise ValueError("delay_minutes must be a whole number of minutes") from exc
        minutes = max(CALLBACK_MIN_MINUTES, min(minutes, CALLBACK_MAX_MINUTES))
        return now + timedelta(minutes=minutes)
    raw = callback_datetime_iso if kind == "absolute" else (callback_datetime_iso or None)
    if raw:
        try:
            parsed = datetime.fromisoformat(str(raw).strip())
        except ValueError as exc:
            raise ValueError("callback_datetime_iso must be an ISO 8601 timestamp") from exc
        # A bare wall-clock reading is the practice's local time, not UTC.
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)).astimezone(UTC)
    if callback_requested_at is not None:
        if callback_requested_at.tzinfo is None:
            return callback_requested_at.replace(tzinfo=tz).astimezone(UTC)
        return callback_requested_at.astimezone(UTC)
    raise ValueError("callback_scheduled requires a callback time")


def _window_for(hours: dict, holidays: list, day) -> tuple[dtime, dtime] | None:
    closed = set()
    overrides = {}
    for item in holidays or []:
        if isinstance(item, str):
            closed.add(item)
        elif isinstance(item, dict) and item.get("date"):
            if item.get("close"):
                overrides[item["date"]] = item["close"]
            else:
                closed.add(item["date"])
    key = day.isoformat()
    if key in closed:
        return None
    window = hours.get(str(day.isoweekday()))
    if not isinstance(window, dict):
        return None
    close_raw = overrides.get(key) or window["close"]
    return (
        dtime.fromisoformat(window["open"][:5]),
        dtime.fromisoformat(str(close_raw)[:5]),
    )


def clamp_to_business_hours(when_utc: datetime, tz: ZoneInfo, hours: dict, holidays: list) -> datetime:
    """Move a callback to the next moment the practice is actually open to call."""
    if not hours:
        return when_utc
    local = when_utc.astimezone(tz)
    for _ in range(21):
        window = _window_for(hours, holidays, local.date())
        if window:
            open_t, close_t = window
            if local.time() < open_t:
                return datetime.combine(local.date(), open_t, tzinfo=tz).astimezone(UTC)
            if local.time() < close_t:
                return when_utc
        local = (local + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    return when_utc

VALID_OUTCOMES = {
    "booked", "not_interested", "no_answer", "voicemail", "callback", "transferred", "manual",
    "booking_link",
    "call_opt_out", "do_not_contact",
}

def record_status(conn, lead_id: str, old: str | None, new: str, source: str, reason: str) -> None:
    conn.execute(
        "insert into lead_status_history (lead_id,from_status,to_status,source,reason) "
        "values (%s,%s,%s,%s,%s)",
        (lead_id, old, new, source, reason),
    )


def mark_booked(conn, lead_id: str, source: str) -> None:
    lead = conn.execute("select status from leads where id=%s for update", (lead_id,)).fetchone()
    if not lead:
        raise ValueError("lead not found")
    conn.execute(
        "update outreach_events set status='skipped',updated_at=now() "
        "where lead_id=%s and status='planned'",
        (lead_id,),
    )
    conn.execute(
        "update leads set status='booked',cadence_state='completed',last_call_outcome='booked',"
        "status_changed_at=now() where id=%s",
        (lead_id,),
    )
    if lead["status"] != "booked":
        record_status(conn, lead_id, lead["status"], "booked", source, "appointment confirmed")


def _schedule_callback(conn, lead_id: str, callback_utc: datetime) -> None:
    """Put the callback first and preserve the remaining cadence's spacing."""
    # A lead has at most one pending callback. A second one is the patient
    # correcting the time (ten, then "no, nine"), so it replaces the first rather
    # than booking a call at both times. Only never-dispatched standalone
    # callbacks go; cadence steps and anything already sent are untouched.
    conn.execute(
        "delete from outreach_events where lead_id=%s and status='planned' "
        "and cadence_step_id is null and day_offset is null",
        (lead_id,),
    )
    row = conn.execute(
        "select min(scheduled_for) as earliest from outreach_events "
        "where lead_id=%s and status='planned'",
        (lead_id,),
    ).fetchone()
    earliest = row["earliest"] if row else None
    if earliest is not None and earliest <= callback_utc:
        conn.execute(
            "update outreach_events set scheduled_for=scheduled_for+%s,updated_at=now() "
            "where lead_id=%s and status='planned'",
            (callback_utc + timedelta(seconds=1) - earliest, lead_id),
        )
    conn.execute(
        "insert into outreach_events(lead_id,channel,scheduled_for,status) "
        "values(%s,'call',%s,'planned')",
        (lead_id, callback_utc),
    )


def report_lead_status(
    trace: WorkflowTrace,
    *,
    lead_id: str,
    status: str,
    call_id: str | None,
    tool_call_id: str | None = None,
    event_id: int | None = None,
    notes: str | None = None,
    callback_requested_at: datetime | None = None,
    callback_type: str | None = None,
    delay_minutes: str | int | None = None,
    callback_datetime_iso: str | None = None,
    source: str = "tool",
) -> str:
    """Apply the direct Vapi lead-status contract with per-call/status idempotency.

    ``source`` labels the lead's history: "tool" when the agent reported during
    the call, "call summary" when the post-call summary settled it instead.
    """
    normalized = {
        "not_interested": "declined",
        "callback": "callback_scheduled",
        "transferred": "transferred_human",
        "manual": "wrong_person",
    }.get(status.strip().lower(), status.strip().lower())
    note = (notes or "").strip()[:500]
    trace.log("validation_started", lead_id=lead_id, event_id=event_id, reported_status=normalized)
    with transaction() as conn:
        lead = conn.execute(
            "select id,status,cadence_state,phone_e164,sms_opt_out from leads where id=%s for update",
            (lead_id,),
        ).fetchone()
        if not lead:
            raise ValueError(
                "I could not find that patient record. Please flag the call for staff follow-up."
            )
        if event_id is not None:
            event = conn.execute(
                "select id,lead_id,vapi_call_id,outcome from outreach_events where id=%s for update",
                (event_id,),
            ).fetchone()
        elif call_id:
            event = conn.execute(
                "select id,lead_id,vapi_call_id,outcome from outreach_events "
                "where vapi_call_id=%s for update",
                (call_id,),
            ).fetchone()
        else:
            event = conn.execute(
                "select id,lead_id,vapi_call_id,outcome from outreach_events where lead_id=%s "
                "and channel='call' and status in ('in_flight','attempted','delivered') "
                "order by executed_at desc nulls last,id desc limit 1 for update",
                (lead_id,),
            ).fetchone()
        if event and str(event["lead_id"]) != lead_id:
            raise ValueError("lead and outreach event do not match")
        stable_call_id = call_id or (str(event["vapi_call_id"]) if event and event["vapi_call_id"] else None)
        if not stable_call_id:
            raise ValueError("call_id or outreach_event_id is required")
        # The same tool call arriving twice is a duplicate; a new tool call is a
        # new decision, even one the patient already made earlier in the call.
        # Keying on call and status alone dropped every correction: link, then
        # "not interested", then link again lost the final link, and a callback
        # moved from ten to nine kept ten. Reports with no tool call id (the
        # post-call backup, direct requests) keep the per-call key.
        receipt_id = (
            f"tool:{tool_call_id}:{lead_id}:{normalized}"
            if tool_call_id
            else f"{stable_call_id}:{lead_id}:{normalized}"
        )
        receipt = conn.execute(
            "insert into provider_events(provider,event_id,event_type,payload) "
            "values('vapi',%s,'lead-status',%s) on conflict(provider,event_id) do nothing returning id",
            (receipt_id, json.dumps({"lead_id": lead_id, "status": normalized})),
        ).fetchone()
        if not receipt:
            trace.log("state_transition_skipped", reason="duplicate_lead_status")
            return "already recorded"
        if lead["status"] == "booked" and normalized != "booked":
            conn.execute("update provider_events set processed_at=now() where id=%s", (receipt["id"],))
            trace.log("state_transition_skipped", reason="confirmed_booking_is_terminal")
            return "already booked"

        outcome = {
            "booked": "booked",
            "declined": "not_interested",
            "callback_scheduled": "callback",
            "transferred_human": "transferred",
            "no_answer": "no_answer",
            "call_opt_out": "call_opt_out",
            "do_not_contact": "do_not_contact",
            "booking_link_sent": "booking_link",
        }.get(normalized, "manual")
        if event:
            conn.execute(
                "update outreach_events set status='delivered',settled_at=now(),settled_by=%s,"
                "outcome=%s,updated_at=now() where id=%s",
                # settled_by only allows worker/tool/webhook/sweeper; the post-call
                # summary arrives on the end-of-call webhook.
                ("tool" if source == "tool" else "webhook", outcome, event["id"]),
            )

        if normalized == "booked":
            appointment = conn.execute(
                "select id from appointments where lead_id=%s and state='scheduled' limit 1",
                (lead_id,),
            ).fetchone()
            if not appointment:
                raise ValueError("booked requires a confirmed appointment")
            mark_booked(conn, lead_id, source)
        elif normalized == "declined":
            conn.execute(
                "update leads set status='declined',cadence_state='terminated',"
                "last_call_outcome='not_interested',status_reason=%s,status_changed_at=now() where id=%s",
                (note or "patient declined scheduling", lead_id),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() "
                "where lead_id=%s and status='planned'",
                (lead_id,),
            )
            record_status(conn, lead_id, lead["status"], "declined", source, note or "declined")
        elif normalized == "callback_scheduled":
            tz, hours, holidays = _practice_clock(conn, lead_id)
            now = datetime.now(UTC)
            callback_utc = clamp_to_business_hours(
                resolve_callback_time(
                    tz,
                    callback_requested_at=callback_requested_at,
                    callback_type=callback_type,
                    delay_minutes=delay_minutes,
                    callback_datetime_iso=callback_datetime_iso,
                    now=now,
                ),
                tz,
                hours,
                holidays,
            )
            if callback_utc <= now or callback_utc > now + timedelta(days=30):
                raise ValueError("callback time must be in the next 30 days")
            conn.execute(
                "update leads set status='callback_scheduled',cadence_state='active',"
                "last_call_outcome='callback',callback_requested_at=%s,callback_notes=%s,"
                "callback_reschedule_count=callback_reschedule_count+1,"
                "status_changed_at=now() where id=%s",
                (callback_utc, note or None, lead_id),
            )
            _schedule_callback(conn, lead_id, callback_utc)
            record_status(
                conn, lead_id, lead["status"], "callback_scheduled", source, note or "callback requested"
            )
        elif normalized == "booking_link":
            settings = conn.execute(
                "select booking_link_url from practice_settings ps join leads l "
                "on l.practice_id=ps.practice_id where l.id=%s",
                (lead_id,),
            ).fetchone()
            if not settings or not settings["booking_link_url"]:
                raise ValueError("booking link is not configured")
            suppressed = conn.execute(
                "select 1 from suppressed_numbers where phone_e164=%s",
                (lead["phone_e164"],),
            ).fetchone()
            if lead["sms_opt_out"] or suppressed or not lead["phone_e164"]:
                raise ValueError("the lead cannot receive an SMS booking link")
            # Held for two minutes so we act on the patient's last word on the
            # call: the sender drops the text if the lead has moved off
            # booking_link_sent by then. One link per call, so asking again after
            # a change of mind re-arms this call's text instead of adding another.
            existing = conn.execute(
                "select id,status from notification_log where lead_id=%s "
                "and notification_type='sms_booking_link' and payload->>'call_id'=%s "
                "order by id desc limit 1",
                (lead_id, stable_call_id),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "insert into notification_log(lead_id,notification_type,channel,status,payload,"
                    "next_attempt_at) values(%s,'sms_booking_link','sms','queued',%s,"
                    "now()+interval '2 minutes')",
                    (lead_id, json.dumps({
                        "booking_link_url": settings["booking_link_url"],
                        "call_id": stable_call_id,
                    })),
                )
            elif existing["status"] == "skipped":
                conn.execute(
                    "update notification_log set status='queued',error=null,"
                    "next_attempt_at=now()+interval '2 minutes',updated_at=now() where id=%s",
                    (existing["id"],),
                )
            # A booking link is an outcome, not proof of a booking. Keep the
            # remaining cadence active until staff explicitly marks the lead booked.
            conn.execute(
                "update leads set status='booking_link_sent',cadence_state='active',"
                "last_call_outcome='manual',status_reason=%s,status_changed_at=now() where id=%s",
                (note or "booking link requested", lead_id),
            )
            record_status(
                conn, lead_id, lead["status"], "booking_link_sent", source, note or "booking link requested"
            )
        elif normalized == "transferred_human":
            # Record the handoff, but only an explicit staff booking closes cadence.
            conn.execute(
                "update leads set status='transferred_human',cadence_state='active',"
                "last_call_outcome='transferred',status_reason=%s,status_changed_at=now() where id=%s",
                (note or "transferred to staff", lead_id),
            )
            record_status(
                conn, lead_id, lead["status"], "transferred_human", source, note or "transferred"
            )
        elif normalized == "no_answer":
            conn.execute(
                "update leads set last_call_outcome='no_answer',status_reason=%s where id=%s",
                (note or None, lead_id),
            )
        elif normalized == "wrong_person":
            reason = note or "wrong phone number"
            conn.execute(
                "update leads set status='invalid_phone',cadence_state='paused',"
                "last_call_outcome='manual',needs_review=true,review_reason=%s,"
                "review_flagged_at=now(),status_changed_at=now() where id=%s",
                (reason, lead_id),
            )
            record_status(conn, lead_id, lead["status"], "invalid_phone", source, reason)
        elif normalized == "call_opt_out":
            conn.execute(
                "update leads set call_opt_out=true,last_call_outcome='call_opt_out',"
                "status_reason=%s,status_changed_at=now() where id=%s",
                (note or "patient opted out of calls", lead_id),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() where lead_id=%s "
                "and channel='call' and status='planned'",
                (lead_id,),
            )
        elif normalized == "do_not_contact":
            conn.execute(
                "update leads set call_opt_out=true,sms_opt_out=true,status='do_not_contact',"
                "cadence_state='terminated',last_call_outcome='do_not_contact',status_reason=%s,"
                "status_changed_at=now() where id=%s",
                (note or "patient requested no further contact", lead_id),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() "
                "where lead_id=%s and status='planned'",
                (lead_id,),
            )
            if lead["phone_e164"]:
                conn.execute(
                    "insert into suppressed_numbers(phone_e164,reason,source,list_type) "
                    "values(%s,'explicit do-not-contact request','tool','internal') "
                    "on conflict(phone_e164) do update set reason=excluded.reason,source=excluded.source,"
                    "last_verified_at=now()",
                    (lead["phone_e164"],),
                )
            record_status(
                conn, lead_id, lead["status"], "do_not_contact", source, note or "do not contact"
            )
        else:
            reason = note or f"unrecognized status: {normalized}"
            conn.execute(
                "update leads set status='needs_attention',cadence_state='paused',"
                "last_call_outcome='manual',needs_review=true,review_reason=%s,"
                "review_flagged_at=now(),status_changed_at=now() where id=%s",
                (reason, lead_id),
            )
            record_status(conn, lead_id, lead["status"], "needs_attention", source, reason)
        conn.execute("update provider_events set processed_at=now() where id=%s", (receipt["id"],))
        enqueue_sheet_update(
            conn,
            lead_id=lead_id,
            event_type="call_settled",
            source_key=receipt_id,
            outreach_event_id=event["id"] if event else None,
        )
    trace.log("state_transition_applied", reported_status=normalized)
    return "recorded"


def apply_call_outcome(
    trace: WorkflowTrace,
    *,
    lead_id: str,
    event_id: int,
    outcome: str,
    source: str = "tool",
    callback_requested_at: datetime | None = None,
    callback_notes: str | None = None,
) -> str:
    trace.log("validation_started", lead_id=lead_id, event_id=event_id)
    if outcome not in VALID_OUTCOMES:
        trace.log("validation_failed", reason="invalid_outcome")
        raise ValueError("invalid call outcome")
    with transaction() as conn:
        trace.log("database_operation_started", operation="lock_lead_event")
        event = conn.execute(
            "select id,lead_id,channel,status,outcome,vapi_call_id from outreach_events "
            "where id=%s for update",
            (event_id,),
        ).fetchone()
        lead = conn.execute(
            "select id,status,phone_e164 from leads where id=%s for update", (lead_id,)
        ).fetchone()
        if not lead or not event or str(event["lead_id"]) != str(lead_id):
            trace.log("validation_failed", reason="lead_event_mismatch")
            raise ValueError("lead and outreach event do not match")
        if event["channel"] != "call":
            trace.log("validation_failed", reason="event_is_not_call")
            raise ValueError("outreach event is not a call")
        if event["status"] in {"delivered", "failed", "skipped"}:
            if event["status"] == "delivered" and event["outcome"] == outcome:
                trace.log("state_transition_skipped", current_status=event["status"])
                return "already recorded"
            trace.log("validation_failed", reason="conflicting_terminal_outcome")
            raise ValueError("outreach event is already settled with a different outcome")
        if event["status"] not in {"in_flight", "attempted"}:
            trace.log("validation_failed", reason="event_not_dispatched")
            raise ValueError("call outcome cannot be recorded before dispatch")
        if outcome == "booked":
            appointment = conn.execute(
                "select id from appointments where lead_id=%s and state='scheduled' limit 1",
                (lead_id,),
            ).fetchone()
            if not appointment:
                trace.log("validation_failed", reason="booked_without_appointment")
                raise ValueError("booked requires a confirmed appointment")
        callback_utc = None
        if outcome == "callback":
            if callback_requested_at is None or callback_requested_at.tzinfo is None:
                trace.log("validation_failed", reason="callback_time_required")
                raise ValueError("callback requires a timezone-aware callback_requested_at")
            now = datetime.now(UTC)
            callback_utc = callback_requested_at.astimezone(UTC)
            if callback_utc <= now or callback_utc > now + timedelta(days=30):
                trace.log("validation_failed", reason="callback_time_out_of_range")
                raise ValueError("callback time must be in the next 30 days")
        conn.execute(
            "update outreach_events set status='delivered',settled_at=now(),settled_by=%s,outcome=%s "
            "where id=%s and status not in ('delivered','failed','skipped')",
            (source, outcome, event_id),
        )
        if outcome == "booked":
            mark_booked(conn, lead_id, source)
        elif outcome == "not_interested":
            conn.execute(
                "update leads set status='declined',cadence_state='terminated',last_call_outcome=%s,"
                "status_changed_at=now() where id=%s", (outcome, lead_id),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() "
                "where lead_id=%s and status='planned'", (lead_id,),
            )
            record_status(conn, lead_id, lead["status"], "declined", source, "not interested")
        elif outcome == "callback":
            conn.execute(
                "update leads set status='callback_scheduled',cadence_state='active',last_call_outcome=%s,"
                "callback_requested_at=%s,callback_notes=%s,status_changed_at=now() where id=%s",
                (outcome, callback_utc, (callback_notes or "")[:500] or None, lead_id),
            )
            _schedule_callback(conn, lead_id, callback_utc)
            record_status(
                conn, lead_id, lead["status"], "callback_scheduled", source, "callback requested"
            )
        elif outcome in {"no_answer", "voicemail"}:
            conn.execute("update leads set last_call_outcome=%s where id=%s", (outcome, lead_id))
        elif outcome == "transferred":
            conn.execute(
                "update leads set status='transferred_human',cadence_state='active',last_call_outcome=%s,"
                "status_changed_at=now() where id=%s", (outcome, lead_id),
            )
            record_status(conn, lead_id, lead["status"], "transferred_human", source, outcome)
        elif outcome == "call_opt_out":
            conn.execute(
                "update leads set call_opt_out=true,last_call_outcome=%s,status_reason=%s,"
                "status_changed_at=now() where id=%s",
                (outcome, "caller explicitly opted out of calls", lead_id),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() "
                "where lead_id=%s and channel='call' and status='planned'",
                (lead_id,),
            )
        elif outcome == "do_not_contact":
            conn.execute(
                "update leads set call_opt_out=true,sms_opt_out=true,status='do_not_contact',"
                "cadence_state='terminated',last_call_outcome=%s,status_reason=%s,"
                "status_changed_at=now() where id=%s",
                (outcome, "caller explicitly requested no contact", lead_id),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() "
                "where lead_id=%s and status='planned'",
                (lead_id,),
            )
            conn.execute(
                "insert into suppressed_numbers(phone_e164,reason,source,list_type) "
                "values(%s,'explicit do-not-contact request',%s,'internal') "
                "on conflict(phone_e164) do update set reason=excluded.reason,source=excluded.source,"
                "last_verified_at=now()",
                (lead["phone_e164"], source),
            )
            record_status(conn, lead_id, lead["status"], "do_not_contact", source, outcome)
        else:
            conn.execute(
                "update leads set status='needs_attention',cadence_state='paused',last_call_outcome=%s,"
                "needs_review=true,review_reason=%s,review_flagged_at=now(),status_changed_at=now() "
                "where id=%s",
                (outcome, f"call outcome: {outcome}", lead_id),
            )
            record_status(conn, lead_id, lead["status"], "needs_attention", source, outcome)
        if event["vapi_call_id"]:
            conn.execute(
                "update test_usage_ledger set outcome=%s where provider='vapi' and provider_ref=%s",
                (outcome, event["vapi_call_id"]),
            )
        enqueue_sheet_update(
            conn,
            lead_id=lead_id,
            event_type="call_settled",
            source_key=f"event:{event_id}:{outcome}",
            outreach_event_id=event_id,
        )
        trace.log("database_operation_completed", operation="settle_call")
    trace.log("state_transition_applied", outcome=outcome)
    return "recorded"


def explicit_opt_out(trace: WorkflowTrace, phone: str, channel: str, source: str) -> None:
    if channel not in {"sms", "call"}:
        raise ValueError("channel must be sms or call")
    with transaction() as conn:
        leads = conn.execute(
            "select id,status,call_opt_out,sms_opt_out from leads where phone_e164=%s for update",
            (phone,),
        ).fetchall()
        for lead in leads:
            column = "sms_opt_out" if channel == "sms" else "call_opt_out"
            other_opted_out = lead["call_opt_out"] if channel == "sms" else lead["sms_opt_out"]
            conn.execute(
                f"update leads set {column}=true,status=case when %s then 'do_not_contact' else status end,"
                "cadence_state=case when %s then 'terminated' else cadence_state end,"
                "status_changed_at=case when %s then now() else status_changed_at end where id=%s",
                (other_opted_out, other_opted_out, other_opted_out, lead["id"]),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() "
                "where lead_id=%s and channel=%s and status='planned'",
                (lead["id"], channel),
            )
            if other_opted_out and lead["status"] != "do_not_contact":
                record_status(
                    conn,
                    str(lead["id"]),
                    lead["status"],
                    "do_not_contact",
                    source,
                    "all outreach channels opted out",
                )
    trace.log("state_transition_applied", transition="explicit_opt_out", channel=channel)
