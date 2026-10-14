import re
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ..api_schemas import CreateAppointmentBody, documented_body
from ..providers import ProviderError
from ..security import vapi_secret_scheme
from ..services.booking import BookingService
from ..vapi_contract import direct_tool_response
from .tool_request import authenticated_tool_request

router = APIRouter(prefix="/api/v1/tools", tags=["appointments"])


def _booking_message(result: dict) -> str:
    status = result.get("status")
    if status in {"confirmed", "already_booked", "confirmed_sync_pending"}:
        confirmation = result.get("spoken_confirmation") or "Your appointment is already confirmed."
        return f"{confirmation} Would you like me to repeat the details?"
    if status == "slot_unavailable":
        return "That time is no longer open. Would you like me to check the nearest available times?"
    if status == "booking_in_progress":
        return "That appointment is still being confirmed. May I place you on a brief hold?"
    if status == "missing_patient_data":
        return "I am missing required patient details. Would you like staff to complete the booking?"
    if status == "configuration_required":
        return "Online booking is not fully configured yet. May a staff member complete it for you?"
    if status in {"manual_review", "retry_later"}:
        return "I could not safely confirm the appointment. May a staff member follow up with you?"
    return "Stride did not accept that booking. Would you like staff to follow up?"


@router.post(
    "/create-appointment",
    summary="Book a real Stride appointment",
    description=(
        "**Creates real records.** Books the slot in Stride, creating the patient and case "
        "if needed, writes the local copy, and queues a confirmation SMS. The time must be "
        "one returned by check-availability. Re-sending the same booking is safe - it "
        "reports the existing appointment instead of double-booking."
    ),
    dependencies=[Depends(vapi_secret_scheme)],
    openapi_extra=documented_body(CreateAppointmentBody),
)
async def create_appointment(request: Request):
    trace = None
    tool_call_id = None
    try:
        trace, parsed = await authenticated_tool_request(request, "create_appointment")
        tool_call_id = parsed.tool_call_id
        lead_id = parsed.arguments.get("lead_id")
        raw_date = parsed.arguments.get("date")
        raw_time = parsed.arguments.get("time")
        if not lead_id:
            raise ValueError("lead_id is required to book an appointment.")
        if not raw_date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(raw_date)):
            raise ValueError("Please give the date in YYYY-MM-DD format, such as 2026-08-27.")
        if not raw_time:
            raise ValueError("Please provide the appointment time.")
        event_value = parsed.arguments.get("outreach_event_id")
        result = await run_in_threadpool(
            BookingService().book_at,
            trace,
            lead_id=str(lead_id),
            event_id=int(event_value) if event_value not in (None, "") else None,
            appointment_date=date.fromisoformat(str(raw_date)),
            appointment_time=str(raw_time),
            patient_data={
                "first_name": str(parsed.arguments.get("first_name") or ""),
                "last_name": str(parsed.arguments.get("last_name") or ""),
                "date_of_birth": str(
                    parsed.arguments.get("date_of_birth") or parsed.arguments.get("dob") or ""
                ),
            },
        )
        trace.complete(outcome=result.get("status"))
        return direct_tool_response(tool_call_id, _booking_message(result))
    except HTTPException:
        raise
    except (TypeError, ValueError) as exc:
        if trace:
            trace.log("validation_failed", error_category=type(exc).__name__)
        return direct_tool_response(tool_call_id, str(exc))
    except ProviderError as exc:
        if trace:
            trace.fail(exc)
        return direct_tool_response(
            tool_call_id,
            "I could not safely confirm the appointment. May a staff member follow up?",
        )
    except Exception as exc:  # noqa: BLE001 - never terminate a live call on a tool failure
        if trace:
            trace.fail(exc)
        return direct_tool_response(
            tool_call_id,
            "I am sorry, I could not complete the booking. May a staff member follow up?",
        )
