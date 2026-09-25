from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from ..config import get_settings
from ..db import transaction
from ..worker import format_phone, materialize_cadence
from .review import hand_over_number
from .sheet_sync import enqueue_sheet_update

TERMINAL_START_STATUSES = {
    "booked",
    "declined",
    "transferred_human",
    "booking_link_sent",
    "do_not_contact",
    "invalid_phone",
    "closed_no_response",
}


@dataclass(frozen=True)
class ActionExecution:
    status_code: int
    body: dict[str, Any]


class LeadActionError(RuntimeError):
    def __init__(self, status_code: int, code: str, detail: str, *, lead_id: str | None = None):
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.lead_id = lead_id


def _request_hash(
    action: str,
    lead_id: UUID | None,
    lead: dict[str, Any],
    *,
    require_new: bool = False,
    replaces_lead_id: UUID | None = None,
) -> str:
    request: dict[str, Any] = {
        "action": action,
        "lead_id": str(lead_id) if lead_id else None,
        "lead": lead,
    }
    if require_new:
        request["require_new"] = True
    if replaces_lead_id:
        request["replaces_lead_id"] = str(replaces_lead_id)
    canonical = json.dumps(
        request,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _name_parts(full_name: str) -> tuple[str, str | None]:
    parts = full_name.split()
    if not parts:
        raise LeadActionError(422, "invalid_name", "Name is required")
    return parts[0], " ".join(parts[1:]) or None


def _response_envelope(http_status: int, body: dict[str, Any]) -> dict[str, Any]:
    """Persist status inside jsonb because live lead_action_requests has no http_status column."""
    return {"http_status": http_status, "body": body}


def _replay_stored_response(stored: Any) -> ActionExecution | None:
    if not isinstance(stored, dict):
        return None
    if "http_status" in stored and "body" in stored and isinstance(stored["body"], dict):
        return ActionExecution(int(stored["http_status"]), dict(stored["body"]))
    return None


def _complete_request(
    conn,
    request_id: UUID,
    *,
    lead_id: str | None,
    status: str,
    http_status: int,
    body: dict[str, Any],
    error_category: str | None = None,
) -> None:
    del error_category  # live schema stores failure detail inside response_body only
    conn.execute(
        "update lead_action_requests set lead_id=%s,status=%s,response_body=%s,"
        "completed_at=now() where request_id=%s",
        (lead_id, status, Jsonb(_response_envelope(http_status, body)), str(request_id)),
    )


def _update_lead_profile(conn, lead_id: str | UUID, lead_data: dict[str, Any]) -> None:
    """Refresh Sheet-owned lead details without changing phone identity or consent."""
    full_name = str(lead_data.get("full_name") or "").strip() or None
    first_name: str | None = None
    last_name: str | None = None
    if full_name:
        first_name, last_name = _name_parts(full_name)
    lead_type = str(
        lead_data.get("lead_type") or lead_data.get("title") or ""
    ).strip() or None
    email = str(lead_data.get("email") or "").strip().lower() or None
    location = str(lead_data.get("location") or "").strip() or None
    conn.execute(
        "update leads set full_name=coalesce(%s,full_name),"
        "first_name=coalesce(%s,first_name),"
        "last_name=case when %s::text is not null then %s else last_name end,"
        "email=coalesce(%s,email),date_of_birth=coalesce(%s,date_of_birth),"
        "location=coalesce(%s,location),lead_type=coalesce(%s,lead_type),updated_at=now() "
        "where id=%s",
        (
            full_name,
            first_name,
            full_name,
            last_name,
            email,
            lead_data.get("date_of_birth"),
            location,
            lead_type,
            lead_id,
        ),
    )


def _start_cadence(
    conn,
    *,
    request_id: UUID,
    practice: dict[str, Any],
    lead_data: dict[str, Any],
    require_new: bool = False,
    replaces_lead_id: UUID | None = None,
) -> ActionExecution:
    phone = format_phone(lead_data.get("phone"))
    if not phone:
        raise LeadActionError(422, "invalid_phone", "Phone number is not valid")
    conn.execute(
        "select pg_advisory_xact_lock(hashtext(%s))",
        (f"sheet-lead:{practice['id']}:{phone}",),
    )
    replaced_lead = None
    if replaces_lead_id:
        replaced_lead = conn.execute(
            "select id,status,phone_e164 from leads where id=%s and practice_id=%s for update",
            (replaces_lead_id, practice["id"]),
        ).fetchone()
        if not replaced_lead:
            raise LeadActionError(404, "lead_not_found", "Lead was not found")
        if replaced_lead["phone_e164"] == phone:
            raise LeadActionError(
                409,
                "phone_unchanged",
                "The phone number has not changed",
                lead_id=str(replaces_lead_id),
            )
    first_name, last_name = _name_parts(str(lead_data["full_name"]).strip())
    stored_lead_type = str(
        lead_data.get("lead_type") or lead_data.get("title") or ""
    ).strip() or None
    # Same rule as dashboard intake: while the deployment runs in test mode
    # every new lead is a test lead (accelerated cadence, no calling-hours
    # gate). Flipping TEST_MODE off makes Sheet leads real patients again.
    settings = get_settings()
    synthetic = settings.test_mode and settings.app_env.lower() in {"development", "test"}
    inserted = conn.execute(
        "insert into leads(practice_id,source_system,first_name,last_name,full_name,phone_e164,"
        "phone_original,email,date_of_birth,timezone,line_type,consent_captured_at,"
        "consent_source,consent_reference,consent_text_version,status,cadence_state,"
        "lead_type,location,is_test) "
        "values(%s,'google_sheets',%s,%s,%s,%s,%s,%s,%s,%s,'unknown',"
        "now(),'dashboard_staff_attestation',%s,'google-sheet-staff-attestation-v1',"
        "'new','pending',%s,%s,%s) returning id",
        (
            practice["id"],
            first_name,
            last_name,
            str(lead_data["full_name"]).strip(),
            phone,
            lead_data["phone"],
            str(lead_data["email"]).strip().lower() if lead_data.get("email") else None,
            lead_data["date_of_birth"],
            practice["timezone"],
            f"n8n:{request_id}",
            stored_lead_type,
            str(lead_data["location"]).strip(),
            synthetic,
        ),
    ).fetchone()
    lead_id = str(inserted["id"])
    previous_status = "new"
    created = True

    event_count = materialize_cadence(
        conn, lead_id, practice["id"], datetime.now(UTC).date()
    )
    if not event_count:
        raise LeadActionError(
            409,
            "cadence_not_configured",
            "No active cadence is configured",
            lead_id=lead_id,
        )
    if replaced_lead:
        reason = f"Replaced by Sheet lead {lead_id} after phone number change"
        conn.execute(
            "update outreach_events set status='skipped',failure_reason=%s,updated_at=now() "
            "where lead_id=%s and status='planned'",
            (reason, replaces_lead_id),
        )
        conn.execute(
            "update leads set status='needs_attention',cadence_state='terminated',"
            "needs_review=true,review_reason=%s,review_flagged_at=now(),"
            "status_reason=%s,status_changed_at=now(),updated_at=now() where id=%s",
            (reason, reason, replaces_lead_id),
        )
        conn.execute(
            "insert into lead_status_history(lead_id,from_status,to_status,source,reason) "
            "values(%s,%s,'needs_attention','n8n_sheet',%s)",
            (replaces_lead_id, replaced_lead["status"], reason),
        )
    conn.execute(
        "insert into lead_status_history(lead_id,from_status,to_status,source,reason) "
        "values(%s,%s,'in_progress','n8n_sheet','cadence started from Google Sheets')",
        (lead_id, previous_status),
    )
    warning = hand_over_number(conn, practice_id=practice["id"], phone=phone, lead_id=lead_id)
    enqueue_sheet_update(
        conn,
        lead_id=lead_id,
        event_type="lead_link_ready",
        source_key=str(request_id),
    )
    # Intake/recovery owns Lead ID + Action Status; this snapshot only adds the link.
    # The AWS webhook omits those intake-owned fields, so it cannot overwrite them.
    return ActionExecution(
        201 if created else 200,
        {
            "request_id": str(request_id),
            "lead_id": lead_id,
            "action": "start_cadence",
            "result": "cadence_started",
            "created": created,
            "cadence_event_count": event_count,
            **({"warning": warning} if warning else {}),
        },
    )


def _locked_lead(conn, practice_id: int, lead_id: UUID, phone: str) -> dict[str, Any]:
    normalized = format_phone(phone)
    if not normalized:
        raise LeadActionError(422, "invalid_phone", "Phone number is not valid")
    lead = conn.execute(
        "select id,practice_id,status,cadence_state,call_opt_out,sms_opt_out,phone_e164 "
        "from leads where id=%s and practice_id=%s for update",
        (lead_id, practice_id),
    ).fetchone()
    if not lead:
        raise LeadActionError(404, "lead_not_found", "Lead was not found")
    if lead["phone_e164"] != normalized:
        raise LeadActionError(
            409,
            "lead_phone_mismatch",
            "Lead ID and phone number do not match",
            lead_id=str(lead_id),
        )
    return lead


def _restart_cadence(
    conn,
    *,
    request_id: UUID,
    practice: dict[str, Any],
    lead_id: UUID,
    phone: str,
    lead_data: dict[str, Any] | None = None,
) -> ActionExecution:
    lead = _locked_lead(conn, practice["id"], lead_id, phone)
    if lead["status"] in {"do_not_contact", "invalid_phone"}:
        raise LeadActionError(
            409,
            "restart_not_allowed",
            "A Do Not Contact or invalid-phone lead cannot be restarted",
            lead_id=str(lead_id),
        )
    # Booked may be restarted: a mis-clicked Booked needs an undo, and marking
    # Booked again is the undo for a mis-clicked restart.
    was_booked = lead["status"] == "booked"
    if lead["call_opt_out"] and lead["sms_opt_out"]:
        raise LeadActionError(
            409, "all_channels_blocked", "This lead opted out of every channel", lead_id=str(lead_id)
        )
    unresolved = conn.execute(
        "select exists(select 1 from outreach_events where lead_id=%s "
        "and status in ('in_flight','attempted')) as unresolved",
        (lead_id,),
    ).fetchone()
    if unresolved["unresolved"]:
        raise LeadActionError(
            409,
            "outreach_unresolved",
            "Wait for the current call or SMS result before restarting",
            lead_id=str(lead_id),
        )
    if lead_data:
        _update_lead_profile(conn, lead_id, lead_data)

    # Restart always creates a fresh run from Day 0, including when the old
    # cadence was paused. Completed history remains intact; only unfinished
    # planned/skipped rows are replaced.
    conn.execute(
        "delete from outreach_events where lead_id=%s and status in ('planned','skipped')",
        (lead_id,),
    )
    conn.execute(
        "update leads set status='new',cadence_state='pending',needs_review=false,"
        "review_reason=null,review_flagged_at=null,last_call_outcome=null,status_reason=null,"
        "callback_requested_at=null,callback_notes=null,call_attempts=0,status_changed_at=now() "
        "where id=%s",
        (lead_id,),
    )
    event_count = materialize_cadence(
        conn, str(lead_id), practice["id"], datetime.now(UTC).date()
    )
    if not event_count:
        raise LeadActionError(
            409,
            "cadence_not_configured",
            "No active cadence is configured",
            lead_id=str(lead_id),
        )
    conn.execute(
        "insert into lead_status_history(lead_id,from_status,to_status,source,reason) "
        "values(%s,%s,'in_progress','n8n_sheet','cadence restarted from Google Sheets')",
        (lead_id, lead["status"]),
    )
    warning = hand_over_number(
        conn, practice_id=practice["id"], phone=lead["phone_e164"], lead_id=str(lead_id)
    )
    enqueue_sheet_update(
        conn,
        lead_id=str(lead_id),
        event_type="lead_link_ready",
        source_key=str(request_id),
    )
    # Intake/recovery writes Lead ID + Action Status for restart.
    return ActionExecution(
        200,
        {
            "request_id": str(request_id),
            "lead_id": str(lead_id),
            "action": "restart_cadence",
            "result": "cadence_restarted",
            "created": False,
            "cadence_event_count": event_count,
            **({"warning": warning} if warning else {}),
            **({"was_booked": True} if was_booked else {}),
        },
    )


def _do_not_contact(
    conn,
    *,
    request_id: UUID,
    practice: dict[str, Any],
    lead_id: UUID,
    phone: str,
) -> ActionExecution:
    lead = _locked_lead(conn, practice["id"], lead_id, phone)
    if lead["status"] != "do_not_contact":
        conn.execute(
            "update leads set call_opt_out=true,sms_opt_out=true,status='do_not_contact',"
            "cadence_state='terminated',last_call_outcome='do_not_contact',"
            "status_reason='staff selected Do Not Contact in Google Sheets',"
            "status_changed_at=now() where id=%s",
            (lead_id,),
        )
        conn.execute(
            "update outreach_events set status='skipped',failure_reason=%s,updated_at=now() "
            "where lead_id=%s and status='planned'",
            ("Do Not Contact selected in Google Sheets", lead_id),
        )
        conn.execute(
            "insert into suppressed_numbers(phone_e164,reason,source,list_type) "
            "values(%s,'staff selected Do Not Contact','n8n_sheet','internal') "
            "on conflict(phone_e164) do update set reason=excluded.reason,source=excluded.source,"
            "last_verified_at=now()",
            (lead["phone_e164"],),
        )
        conn.execute(
            "insert into lead_status_history(lead_id,from_status,to_status,source,reason) "
            "values(%s,%s,'do_not_contact','n8n_sheet','Do Not Contact selected in Google Sheets')",
            (lead_id, lead["status"]),
        )
    # Intake/recovery writes Action Status for the Sheet DNC command.
    return ActionExecution(
        200,
        {
            "request_id": str(request_id),
            "lead_id": str(lead_id),
            "action": "do_not_contact",
            "result": "do_not_contact_applied",
            "created": False,
            "cadence_event_count": 0,
        },
    )


def _mark_booked(
    conn,
    *,
    request_id: UUID,
    practice: dict[str, Any],
    lead_id: UUID,
    phone: str,
) -> ActionExecution:
    """Accept the Sheet team's manual booking decision without inventing an appointment."""
    lead = _locked_lead(conn, practice["id"], lead_id, phone)
    if lead["status"] != "booked":
        conn.execute(
            "update leads set status='booked',cadence_state='completed',needs_review=false,"
            "review_reason=null,review_resolved_at=case when needs_review then now() "
            "else review_resolved_at end,status_reason=%s,status_changed_at=now() where id=%s",
            ("staff marked Booked in Google Sheets", lead_id),
        )
        conn.execute(
            "update outreach_events set status='skipped',failure_reason=%s,updated_at=now() "
            "where lead_id=%s and status='planned'",
            ("Booked selected in Google Sheets", lead_id),
        )
        conn.execute(
            "insert into lead_status_history(lead_id,from_status,to_status,source,reason) "
            "values(%s,%s,'booked','n8n_sheet','Booked selected in Google Sheets')",
            (lead_id, lead["status"]),
        )
    enqueue_sheet_update(
        conn,
        lead_id=str(lead_id),
        event_type="lead_booked",
        source_key=str(request_id),
    )
    return ActionExecution(
        200,
        {
            "request_id": str(request_id),
            "lead_id": str(lead_id),
            "action": "booked",
            "result": "booked_applied",
            "created": False,
            "cadence_event_count": 0,
        },
    )


def execute_lead_action(
    *,
    request_id: UUID,
    action: str,
    lead_id: UUID | None,
    lead: dict[str, Any],
    require_new: bool = False,
    replaces_lead_id: UUID | None = None,
) -> ActionExecution:
    """Apply one signed n8n command with a durable, replayable response."""
    request_hash = _request_hash(
        action,
        lead_id,
        lead,
        require_new=require_new,
        replaces_lead_id=replaces_lead_id,
    )
    with transaction() as conn:
        practice = conn.execute(
            "select id,timezone from practices where slug=%s and is_active=true",
            (get_settings().n8n_practice_slug,),
        ).fetchone()
        if not practice:
            return ActionExecution(503, {"detail": "Configured practice is not available"})

        inserted = conn.execute(
            "insert into lead_action_requests(request_id,practice_id,action,request_hash,status) "
            "values(%s,%s,%s,%s,'processing') on conflict(request_id) do nothing "
            "returning request_id",
            (str(request_id), str(practice["id"]), action, request_hash),
        ).fetchone()
        if not inserted:
            existing = conn.execute(
                "select practice_id,action,request_hash,status,response_body "
                "from lead_action_requests where request_id=%s for update",
                (str(request_id),),
            ).fetchone()
            if (
                not existing
                or str(existing["practice_id"]) != str(practice["id"])
                or existing["action"] != action
                or existing["request_hash"] != request_hash
            ):
                return ActionExecution(
                    409, {"request_id": str(request_id), "detail": "Request ID was reused"}
                )
            if existing["status"] in {"completed", "failed"} and existing["response_body"]:
                replayed = _replay_stored_response(existing["response_body"])
                if replayed is not None:
                    return replayed
            return ActionExecution(
                409,
                {"request_id": str(request_id), "detail": "Request is still processing"},
            )

        try:
            with conn.transaction():
                if action == "start_cadence":
                    result = _start_cadence(
                        conn,
                        request_id=request_id,
                        practice=practice,
                        lead_data=lead,
                        require_new=require_new,
                        replaces_lead_id=replaces_lead_id,
                    )
                elif action == "restart_cadence":
                    if lead_id is None:
                        raise LeadActionError(422, "lead_id_required", "Lead ID is required")
                    result = _restart_cadence(
                        conn,
                        request_id=request_id,
                        practice=practice,
                        lead_id=lead_id,
                        phone=str(lead.get("phone") or ""),
                        lead_data=lead,
                    )
                elif action == "do_not_contact":
                    if lead_id is None:
                        raise LeadActionError(422, "lead_id_required", "Lead ID is required")
                    result = _do_not_contact(
                        conn,
                        request_id=request_id,
                        practice=practice,
                        lead_id=lead_id,
                        phone=str(lead.get("phone") or ""),
                    )
                elif action == "booked":
                    if lead_id is None:
                        raise LeadActionError(422, "lead_id_required", "Lead ID is required")
                    result = _mark_booked(
                        conn,
                        request_id=request_id,
                        practice=practice,
                        lead_id=lead_id,
                        phone=str(lead.get("phone") or ""),
                    )
                else:
                    raise LeadActionError(422, "invalid_action", "Action is not supported")
        except LeadActionError as exc:
            persisted_lead_id = None
            if exc.lead_id:
                persisted = conn.execute(
                    "select id from leads where id=%s",
                    (exc.lead_id,),
                ).fetchone()
                persisted_lead_id = str(persisted["id"]) if persisted else None
            body = {
                "request_id": str(request_id),
                "lead_id": persisted_lead_id,
                "action": action,
                "code": exc.code,
                "detail": exc.detail,
            }
            _complete_request(
                conn,
                request_id,
                lead_id=persisted_lead_id,
                status="failed",
                http_status=exc.status_code,
                body=body,
                error_category=exc.code,
            )
            return ActionExecution(exc.status_code, body)

        _complete_request(
            conn,
            request_id,
            lead_id=result.body.get("lead_id"),
            status="completed",
            http_status=result.status_code,
            body=result.body,
        )
        return result


def sync_sheet_lead(
    *, request_id: UUID, lead_id: UUID, lead: dict[str, Any]
) -> ActionExecution:
    """Update one linked Sheet lead; changed phones require staff review."""
    phone = format_phone(lead.get("phone"))
    if not phone:
        return ActionExecution(422, {"code": "invalid_phone", "detail": "Phone is invalid"})

    with transaction() as conn:
        practice = conn.execute(
            "select id from practices where slug=%s and is_active=true",
            (get_settings().n8n_practice_slug,),
        ).fetchone()
        if not practice:
            return ActionExecution(503, {"detail": "Configured practice is not available"})
        current = conn.execute(
            "select id,phone_e164 from leads where id=%s and practice_id=%s for update",
            (lead_id, practice["id"]),
        ).fetchone()
        if not current:
            return ActionExecution(404, {"code": "lead_not_found", "detail": "Lead was not found"})
        if current["phone_e164"] == phone:
            _update_lead_profile(conn, lead_id, lead)
            return ActionExecution(
                200,
                {
                    "request_id": str(request_id),
                    "lead_id": str(lead_id),
                    "previous_lead_id": str(lead_id),
                    "result": "profile_updated",
                    "created": False,
                },
            )
        return ActionExecution(
            409,
            {
                "request_id": str(request_id),
                "lead_id": str(lead_id),
                "previous_lead_id": str(lead_id),
                "result": "phone_changed_needs_review",
                "code": "phone_changed_needs_review",
                "detail": "Phone number changed; needs review",
                "created": False,
            },
        )
