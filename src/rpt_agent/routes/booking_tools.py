"""Voice-agent booking tools: find-slots and book-appointment.

Vapi rules these follow: always HTTP 200, one flat line per tool call, `result`
for anything the model should act on (including "nothing open, ask again") and
`error` only when the tool itself failed. The lead always comes from the call's
private variables, never from what the model typed.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ..providers import ProviderError
from ..security import vapi_secret_scheme
from ..services.slot_booking import (
    WHEN_VALUES,
    BookingToolError,
    book_slot,
    find_slots,
    list_providers,
)
from ..vapi_contract import _single_line, tool_error, tool_success
from .tool_request import authenticated_tool_request

router = APIRouter(prefix="/api/v1/tools", tags=["booking"])

SYSTEM_DOWN = (
    "The scheduling system is not answering right now. Offer to text the booking link or "
    "transfer the caller to the team."
)


def _reply(tool_call_id: str | None, message: str, *, failed: bool = False) -> dict[str, Any]:
    if tool_call_id:
        item = tool_error(tool_call_id, message) if failed else tool_success(tool_call_id, message)
        return {"results": [item]}
    return {"error" if failed else "message": _single_line(message)}


def _lead_id(arguments: dict[str, Any]) -> str:
    lead_id = str(arguments.get("lead_id") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", lead_id):
        raise BookingToolError("This call is not linked to a patient record.")
    return lead_id


@router.post(
    "/find-slots",
    summary="Find the nearest open appointment times for the lead",
    description=(
        "Read-only. Uses the lead's location and case type to pick clinicians, then returns "
        "the three nearest open times from the slot cache and remembers them for the call. "
        "Arguments: `when` (required: " + ", ".join(WHEN_VALUES) + "), `specific_date` "
        "(YYYY-MM-DD, with when=specific_date), `time_of_day` (morning/afternoon/evening), "
        "`clinician_name`, `location`, `case_type` (only if the caller wants another service)."
    ),
    dependencies=[Depends(vapi_secret_scheme)],
)
async def find_slots_tool(request: Request):
    trace = None
    tool_call_id = None
    try:
        trace, parsed = await authenticated_tool_request(request, "find_slots")
        tool_call_id = parsed.tool_call_id
        arguments = parsed.arguments
        when = str(arguments.get("when") or "").strip().lower()
        if when not in WHEN_VALUES:
            raise ValueError(f"BAD_WHEN: when must be one of {', '.join(WHEN_VALUES)}.")
        raw_date = str(arguments.get("specific_date") or arguments.get("date") or "").strip()
        if raw_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
            raise ValueError("BAD_DATE: send specific_date as YYYY-MM-DD.")
        message = await run_in_threadpool(
            find_slots,
            trace,
            lead_id=_lead_id(arguments),
            call_id=parsed.call_id,
            when=when,
            specific_date=date.fromisoformat(raw_date) if raw_date else None,
            time_of_day=str(arguments.get("time_of_day") or "").strip().lower() or None,
            clinician_name=str(arguments.get("clinician_name") or "").strip() or None,
            location_name=str(arguments.get("location") or "").strip() or None,
            case_type_name=str(arguments.get("case_type") or "").strip() or None,
        )
        trace.complete()
        return _reply(tool_call_id, message)
    except HTTPException:
        raise
    except ValueError as exc:
        if trace:
            trace.log("validation_failed", error_category=type(exc).__name__)
        return _reply(tool_call_id, str(exc))
    except BookingToolError as exc:
        if trace:
            trace.log("booking_tool_refused", error_category=type(exc).__name__)
        return _reply(tool_call_id, f"{exc} Offer to transfer the caller to the team.", failed=True)
    except ProviderError as exc:
        if trace:
            trace.fail(exc)
        return _reply(tool_call_id, SYSTEM_DOWN, failed=True)
    except Exception as exc:  # noqa: BLE001 - never end a live call on a tool failure
        if trace:
            trace.fail(exc)
        return _reply(tool_call_id, SYSTEM_DOWN, failed=True)


@router.post(
    "/list-providers",
    summary="List the clinicians the caller can see",
    description=(
        "Read-only. Clinicians assigned to the lead's service at the lead's clinic, with any "
        "credentials and short bio on file. Optional `location` and `case_type` when the caller "
        "changed clinic or service."
    ),
    dependencies=[Depends(vapi_secret_scheme)],
)
async def list_providers_tool(request: Request):
    trace = None
    tool_call_id = None
    try:
        trace, parsed = await authenticated_tool_request(request, "list_providers")
        tool_call_id = parsed.tool_call_id
        arguments = parsed.arguments
        message = await run_in_threadpool(
            list_providers,
            lead_id=_lead_id(arguments),
            location_name=str(arguments.get("location") or "").strip() or None,
            case_type_name=str(arguments.get("case_type") or "").strip() or None,
        )
        trace.complete()
        return _reply(tool_call_id, message)
    except HTTPException:
        raise
    except ValueError as exc:
        if trace:
            trace.log("validation_failed", error_category=type(exc).__name__)
        return _reply(tool_call_id, str(exc))
    except BookingToolError as exc:
        if trace:
            trace.log("booking_tool_refused", error_category=type(exc).__name__)
        return _reply(tool_call_id, f"{exc} Offer to transfer the caller to the team.", failed=True)
    except Exception as exc:  # noqa: BLE001 - never end a live call on a tool failure
        if trace:
            trace.fail(exc)
        return _reply(tool_call_id, SYSTEM_DOWN, failed=True)


@router.post(
    "/book-appointment",
    summary="Book the time the caller chose (creates real Stride records)",
    description=(
        "**Creates real records.** Holds the slot, re-checks it live in Stride, then creates the "
        "patient, case and appointment. Arguments: `date` (YYYY-MM-DD) and `time` the caller "
        "chose from find-slots, `first_name`, `last_name`, `date_of_birth` (YYYY-MM-DD), optional "
        "`email`, `clinician_name`, `location`, `case_type`. Safe to resend: an existing booking is reported, "
        "never duplicated."
    ),
    dependencies=[Depends(vapi_secret_scheme)],
)
async def book_appointment_tool(request: Request):
    trace = None
    tool_call_id = None
    try:
        trace, parsed = await authenticated_tool_request(request, "book_appointment")
        tool_call_id = parsed.tool_call_id
        message = await run_in_threadpool(
            book_slot,
            trace,
            lead_id=_lead_id(parsed.arguments),
            call_id=parsed.call_id,
            arguments=parsed.arguments,
        )
        trace.complete(outcome=message.split(":", 1)[0])
        return _reply(tool_call_id, message)
    except HTTPException:
        raise
    except ValueError as exc:
        if trace:
            trace.log("validation_failed", error_category=type(exc).__name__)
        return _reply(tool_call_id, str(exc))
    except BookingToolError as exc:
        if trace:
            trace.log("booking_tool_refused", error_category=type(exc).__name__)
        return _reply(tool_call_id, f"{exc} Offer to transfer the caller to the team.", failed=True)
    except Exception as exc:  # noqa: BLE001 - never end a live call on a tool failure
        if trace:
            trace.fail(exc)
        return _reply(
            tool_call_id,
            "Something went wrong while booking. Do not try again; tell the caller our team will "
            "call them back to finish booking.",
            failed=True,
        )
