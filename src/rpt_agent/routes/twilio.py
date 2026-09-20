from __future__ import annotations

import json
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..config import get_settings
from ..db import transaction
from ..observability import WorkflowTrace, trace_id_var
from ..security import require_twilio_auth
from ..services.delivery import apply_twilio_message_status
from ..services.lead_status import explicit_opt_out
from ..services.sheet_sync import enqueue_sheet_update

router = APIRouter(prefix="/api/v1/twilio", tags=["twilio"])


@router.post("/inbound-sms", include_in_schema=False)
async def twilio_inbound_sms(request: Request):
    trace = WorkflowTrace("twilio_inbound_sms", "api", trace_id_var.get())
    form_data = {str(k): str(v) for k, v in (await request.form()).items()}
    trace.log("request_parsed", field_count=len(form_data))
    try:
        await require_twilio_auth(request, form_data)
        trace.log("authentication_passed", provider="twilio")
    except HTTPException:
        trace.log("authentication_failed", provider="twilio", error_category="HTTPException")
        raise
    phone = form_data.get("From", "").strip()
    message_text = form_data.get("Body", "").strip()
    sid = form_data.get("MessageSid") or f"missing-{uuid4().hex}"
    if not phone:
        raise HTTPException(status_code=400, detail="missing From")
    with transaction() as conn:
        lead = conn.execute(
            "select id from leads where phone_e164=%s order by created_at desc limit 1", (phone,)
        ).fetchone()
        conn.execute(
            "insert into sms_messages(lead_id,direction,body,occurred_at,delivery_status,provider_message_id) "
            "values(%s,'inbound',%s,now(),'received',%s) on conflict(provider_message_id) do nothing",
            (lead["id"] if lead else None, message_text, sid),
        )
    command = message_text.lower()
    if command in {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}:
        explicit_opt_out(trace, phone, "sms", "twilio_inbound")
    elif command == "call" and lead:
        with transaction() as conn:
            conn.execute(
                "update leads set status='callback_scheduled',callback_requested_at=now(),status_changed_at=now() "
                "where id=%s", (lead["id"],),
            )
            enqueue_sheet_update(
                conn,
                lead_id=str(lead["id"]),
                event_type="callback_updated",
                source_key=f"twilio-inbound:{sid}",
            )
        trace.log("callback_requested", lead_id=str(lead["id"]))
    trace.complete()
    return JSONResponse({"ok": True})


@router.post("/message-status", include_in_schema=False)
async def twilio_message_status(request: Request):
    trace = WorkflowTrace("twilio_message_status", "api", trace_id_var.get())
    form_data = {str(k): str(v) for k, v in (await request.form()).items()}
    trace.log("request_parsed", field_count=len(form_data))
    try:
        await require_twilio_auth(request, form_data)
        trace.log("authentication_passed", provider="twilio")
    except HTTPException:
        trace.log("authentication_failed", provider="twilio", error_category="HTTPException")
        raise
    sid = form_data.get("MessageSid", "")
    status = form_data.get("MessageStatus", "").lower()
    if not sid:
        raise HTTPException(status_code=400, detail="missing MessageSid")
    if status not in {"queued", "sent", "delivered", "undelivered", "failed"}:
        raise HTTPException(status_code=400, detail="invalid MessageStatus")
    with transaction() as conn:
        receipt_id = f"message-status:{sid}:{status}"
        inserted = conn.execute(
            "insert into provider_events(provider,event_id,event_type,payload) "
            "values('twilio',%s,'message-status',%s) on conflict(provider,event_id) do nothing returning id",
            (receipt_id, json.dumps(form_data)),
        ).fetchone()
        if not inserted:
            trace.complete(outcome="duplicate")
            return {"ok": True, "duplicate": True}
        matched = apply_twilio_message_status(conn, form_data)
        if matched:
            conn.execute("update provider_events set processed_at=now() where id=%s", (inserted["id"],))
        else:
            conn.execute(
                "update provider_events set processing_error=%s,processing_attempts=1,"
                "next_attempt_at=now()+interval '1 minute',"
                "dead_lettered_at=case when %s<=1 then now() else null end where id=%s",
                (
                    "Twilio message status arrived before its local send record",
                    get_settings().retry_max_attempts,
                    inserted["id"],
                ),
            )
    trace.complete(message_status=status, matched=bool(matched))
    return {"ok": True}
