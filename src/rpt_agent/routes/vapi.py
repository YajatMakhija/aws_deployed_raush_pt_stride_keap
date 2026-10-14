from __future__ import annotations

import json
from datetime import UTC, date, datetime

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ..config import get_settings
from ..db import transaction
from ..observability import WorkflowTrace, trace_id_var
from ..security import require_vapi_auth
from ..services.booking import BookingService
from ..services.delivery import process_vapi_end_report
from ..services.lead_status import apply_call_outcome
from ..vapi_contract import parse_tool_calls, tool_error, tool_success

router = APIRouter(prefix="/api/v1/vapi", tags=["vapi"])


@router.post("/tools", include_in_schema=False)
async def vapi_tools(request: Request):
    trace = WorkflowTrace("vapi_tools", "api", trace_id_var.get())
    try:
        await require_vapi_auth(request)
        trace.log("authentication_passed", provider="vapi")
    except HTTPException:
        trace.log("authentication_failed", provider="vapi", error_category="HTTPException")
        raise
    try:
        body = await request.json()
        calls = parse_tool_calls(body)
        trace.log("request_parsed", tool_count=len(calls))
        if not calls:
            raise HTTPException(status_code=400, detail="no valid Vapi tool calls")
        results = await run_in_threadpool(_run_tool_calls, trace, calls)
        trace.complete(result_count=len(results))
        return {"results": results}
    except HTTPException:
        raise
    except Exception as exc:
        trace.fail(exc)
        raise HTTPException(status_code=400, detail="invalid tool request") from exc


def _run_tool_calls(trace: WorkflowTrace, calls: list) -> list:
    results = []
    service = BookingService()
    for call in calls:
        try:
            if call.name in {"check_availability", "get_available_slots", "availability"}:
                value = service.availability(
                    trace,
                    str(call.arguments["lead_id"]),
                    date.fromisoformat(
                        call.arguments.get("preferred_date")
                        or call.arguments.get("start_date")
                        or datetime.now(UTC).date().isoformat()
                    ),
                    int(call.arguments.get("days", 7)),
                )
            elif call.name in {"create_appointment", "book_appointment", "book"}:
                value = service.book(
                    trace,
                    str(call.arguments["lead_id"]),
                    int(call.arguments["outreach_event_id"])
                    if call.arguments.get("outreach_event_id") else None,
                    str(call.arguments["slot_token"]),
                    patient_data={
                        "first_name": str(call.arguments.get("first_name") or ""),
                        "last_name": str(call.arguments.get("last_name") or ""),
                        "date_of_birth": str(call.arguments.get("date_of_birth") or ""),
                    },
                )
            elif call.name in {"update_lead_status", "record_call_outcome"}:
                callback_value = (
                    call.arguments.get("callback_requested_at")
                    or call.arguments.get("callback_datetime_iso")
                )
                callback_at = (
                    datetime.fromisoformat(str(callback_value)) if callback_value else None
                )
                value = {"status": apply_call_outcome(
                    trace,
                    lead_id=str(call.arguments["lead_id"]),
                    event_id=int(call.arguments["outreach_event_id"]),
                    outcome=str(call.arguments["outcome"]),
                    callback_requested_at=callback_at,
                    callback_notes=str(
                        call.arguments.get("callback_notes")
                        or call.arguments.get("summary")
                        or ""
                    ),
                )}
            else:
                raise ValueError(f"unsupported tool: {call.name}")
            results.append(tool_success(call.tool_call_id, value, call.name))
        except (KeyError, TypeError, ValueError) as exc:
            trace.log("tool_call_failed", tool=call.name, error_category=type(exc).__name__)
            results.append(tool_error(call.tool_call_id, str(exc), call.name))
        except Exception as exc:  # noqa: BLE001 - isolate one failed call in a Vapi batch
            trace.log("tool_call_failed", tool=call.name, error_category=type(exc).__name__)
            results.append(
                tool_error(call.tool_call_id, "The request could not be completed.", call.name)
            )
    return results


@router.post("/webhook", include_in_schema=False)
async def vapi_webhook(request: Request):
    trace = WorkflowTrace("vapi_webhook", "api", trace_id_var.get())
    try:
        await require_vapi_auth(request)
        trace.log("authentication_passed", provider="vapi")
    except HTTPException:
        trace.log("authentication_failed", provider="vapi", error_category="HTTPException")
        raise
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - invalid webhook JSON is durably reported as ignored
        body = {}
    message = body.get("message") if isinstance(body, dict) else {}
    message = message if isinstance(message, dict) else {}
    call = message.get("call") if isinstance(message.get("call"), dict) else {}
    call_id = str(call.get("id") or body.get("id") or "")
    event_type = str(message.get("type") or "unknown")
    if not call_id:
        trace.log("validation_failed", reason="missing_call_id")
        return {"ok": True, "ignored": "missing_call_id"}
    return await run_in_threadpool(_process_webhook, trace, body, call_id, event_type)


def _process_webhook(trace: WorkflowTrace, body: dict, call_id: str, event_type: str) -> dict:
    receipt_id = f"{call_id}:{event_type}"
    with transaction() as conn:
        inserted = conn.execute(
            "insert into provider_events(provider,event_id,event_type,payload) values('vapi',%s,%s,%s) "
            "on conflict(provider,event_id) do nothing returning id",
            (receipt_id, event_type, json.dumps(body)),
        ).fetchone()
    if not inserted:
        trace.complete(outcome="duplicate")
        return {"ok": True, "duplicate": True}
    trace.log("webhook_persisted", provider_event_id=inserted["id"], event_type=event_type)
    try:
        if event_type == "end-of-call-report":
            process_vapi_end_report(trace, body)
            with transaction() as conn:
                conn.execute("update provider_events set processed_at=now() where id=%s", (inserted["id"],))
        else:
            with transaction() as conn:
                conn.execute("update provider_events set processed_at=now() where id=%s", (inserted["id"],))
        trace.complete()
    except Exception as exc:  # noqa: BLE001 - webhook receipt must remain durable on processing failure
        trace.fail(exc, provider_event_id=inserted["id"])
        with transaction() as conn:
            conn.execute(
                "update provider_events set processing_error=%s,processing_attempts=processing_attempts+1,"
                "next_attempt_at=now()+interval '1 minute',"
                "dead_lettered_at=case when processing_attempts+1>=%s then now() else null end "
                "where id=%s",
                (str(exc)[:500], get_settings().retry_max_attempts, inserted["id"]),
            )
    return {"ok": True, "persisted": True}
