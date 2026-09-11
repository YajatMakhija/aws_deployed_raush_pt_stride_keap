from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from ..api_schemas import UpdateLeadStatusBody, documented_body
from ..security import vapi_secret_scheme
from ..services.lead_status import report_lead_status
from ..vapi_contract import direct_tool_response
from .tool_request import authenticated_tool_request

router = APIRouter(prefix="/api/v1/webhooks/vapi", tags=["leads"])


@router.post(
    "/lead-status",
    summary="Record the outcome of a call on the lead",
    description=(
        "**Changes lead state.** Writes the call outcome, advances or stops the cadence, "
        "and is idempotent per call id and status. `booked` is refused unless a confirmed "
        "appointment exists."
    ),
    dependencies=[Depends(vapi_secret_scheme)],
    openapi_extra=documented_body(UpdateLeadStatusBody),
)
async def lead_status(request: Request):
    trace = None
    tool_call_id = None
    try:
        trace, parsed = await authenticated_tool_request(request, "lead_status")
        tool_call_id = parsed.tool_call_id
        lead_id = parsed.arguments.get("lead_id")
        status = parsed.arguments.get("status") or parsed.arguments.get("outcome")
        if not lead_id or not status:
            raise ValueError("lead_id and status are required.")
        # The voice tool reports callback timing as type + delay/ISO rather than one
        # absolute stamp; the service resolves it against the practice timezone.
        callback_value = parsed.arguments.get("callback_requested_at")
        callback_at = datetime.fromisoformat(str(callback_value)) if callback_value else None
        event_value = parsed.arguments.get("outreach_event_id")
        result = report_lead_status(
            trace,
            lead_id=str(lead_id),
            status=str(status),
            tool_call_id=tool_call_id,
            call_id=parsed.call_id or (
                str(parsed.arguments["call_id"]) if parsed.arguments.get("call_id") else None
            ),
            event_id=int(event_value) if event_value not in (None, "") else None,
            notes=str(
                parsed.arguments.get("notes")
                or parsed.arguments.get("summary")
                or parsed.arguments.get("callback_notes")
                or ""
            ),
            callback_requested_at=callback_at,
            callback_type=parsed.arguments.get("callback_type"),
            delay_minutes=parsed.arguments.get("delay_minutes"),
            callback_datetime_iso=parsed.arguments.get("callback_datetime_iso"),
        )
        trace.complete(outcome=result)
        message = (
            "The lead status was already recorded. Is there anything else before we finish?"
            if result.startswith("already")
            else "The lead status is recorded. Is there anything else before we finish?"
        )
        return direct_tool_response(tool_call_id, message)
    except HTTPException:
        raise
    except (TypeError, ValueError) as exc:
        if trace:
            trace.log("validation_failed", error_category=type(exc).__name__)
        return direct_tool_response(tool_call_id, str(exc))
    except Exception as exc:  # noqa: BLE001 - never terminate a live call on a tool failure
        if trace:
            trace.fail(exc)
        return direct_tool_response(
            tool_call_id,
            "I could not record that status just now. Please flag the call for staff follow-up.",
        )
