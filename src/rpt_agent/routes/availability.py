import re
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ..api_schemas import CheckAvailabilityBody, documented_body
from ..providers import ProviderError
from ..security import vapi_secret_scheme
from ..services.booking import BookingService
from ..vapi_contract import direct_tool_response
from .tool_request import authenticated_tool_request

router = APIRouter(prefix="/api/v1/tools", tags=["availability"])


@router.post(
    "/check-availability",
    summary="Check open appointment slots in Stride",
    description=(
        "Reads live Stride availability for the lead's practice. Read-only - it creates "
        "nothing and contacts nobody. Omit `date` for the next openings, or give a date "
        "(and optionally a time) to check one day."
    ),
    dependencies=[Depends(vapi_secret_scheme)],
    openapi_extra=documented_body(CheckAvailabilityBody),
)
async def check_availability(request: Request):
    trace = None
    tool_call_id = None
    try:
        trace, parsed = await authenticated_tool_request(request, "check_availability")
        tool_call_id = parsed.tool_call_id
        raw_date = parsed.arguments.get("date") or parsed.arguments.get("preferred_date")
        if raw_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(raw_date)):
            raise ValueError("Please give the date in YYYY-MM-DD format, such as 2026-08-27.")
        requested_date = date.fromisoformat(str(raw_date)) if raw_date else None
        lead_id = parsed.arguments.get("lead_id")
        practice_id = parsed.arguments.get("practice_id")
        message = await run_in_threadpool(
            BookingService().availability_message,
            trace,
            lead_id=str(lead_id) if lead_id else None,
            practice_id=int(practice_id) if practice_id is not None else None,
            requested_date=requested_date,
            requested_time=str(parsed.arguments.get("time"))
            if parsed.arguments.get("time")
            else None,
        )
        trace.complete()
        return direct_tool_response(tool_call_id, message)
    except HTTPException:
        raise
    except ValueError as exc:
        if trace:
            trace.log("validation_failed", error_category=type(exc).__name__)
        return direct_tool_response(tool_call_id, str(exc))
    except ProviderError as exc:
        if trace:
            trace.fail(exc)
        return direct_tool_response(
            tool_call_id,
            "I could not check the schedule just now. Would you like a staff member to follow up?",
        )
    except Exception as exc:  # noqa: BLE001 - never terminate a live call on a tool failure
        if trace:
            trace.fail(exc)
        return direct_tool_response(
            tool_call_id,
            "I am sorry, I could not check availability just now. Would you like staff to follow up?",
        )
