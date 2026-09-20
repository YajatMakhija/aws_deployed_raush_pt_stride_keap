from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..db import record_integration_event
from ..observability import WorkflowTrace, trace_id_var
from ..parsing import parse_flexible_date
from ..security import require_n8n_intake_auth
from ..services.lead_actions import execute_lead_action, sync_sheet_lead

router = APIRouter(prefix="/api/v1/integrations/n8n", tags=["integrations"])


class SheetLeadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, max_length=200)
    phone: str = Field(min_length=7, max_length=32)
    email: str | None = Field(
        default=None,
        max_length=320,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    date_of_birth: date | None = None
    location: str | None = Field(default=None, max_length=120)
    title: str | None = Field(default=None, max_length=200)
    lead_type: str | None = Field(
        default=None,
        max_length=200,
        description="Alias of title; either name may be sent with any non-empty value.",
    )
    @field_validator("full_name", "location", "title", "lead_type")
    @classmethod
    def optional_text_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value.strip() if value is not None else None

    @field_validator("date_of_birth", mode="before")
    @classmethod
    def parse_date_of_birth(cls, value: date | datetime | str | None) -> date | None:
        return parse_flexible_date(value)

    @field_validator("date_of_birth")
    @classmethod
    def date_of_birth_not_future(cls, value: date | None) -> date | None:
        if value and value > datetime.now(UTC).date():
            raise ValueError("date_of_birth cannot be in the future")
        return value

    @model_validator(mode="after")
    def title_and_lead_type_are_aliases(self):
        if self.title and self.lead_type and self.title != self.lead_type:
            raise ValueError("title and lead_type must match when both are provided")
        resolved = self.title or self.lead_type
        if resolved:
            self.title = resolved
            self.lead_type = resolved
        return self


class LeadActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["start_cadence", "restart_cadence", "do_not_contact"]
    lead_id: UUID | None = None
    lead: SheetLeadInput

    @model_validator(mode="after")
    def fields_for_action(self):
        if self.action == "start_cadence":
            required = {
                "full_name": self.lead.full_name,
                "date_of_birth": self.lead.date_of_birth,
                "location": self.lead.location,
                "title": self.lead.title or self.lead.lead_type,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise ValueError(f"start_cadence requires: {', '.join(missing)}")
        elif self.lead_id is None:
            raise ValueError(f"{self.action} requires lead_id")
        return self


class LeadSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lead_id: UUID
    lead: SheetLeadInput


@router.post("/lead-actions")
async def lead_actions(request: Request):
    trace = WorkflowTrace("n8n_lead_action", "api", trace_id_var.get())
    request_id_raw = request.headers.get("x-request-id", "").strip()
    try:
        await require_n8n_intake_auth(request)
        trace.log("authentication_passed", provider="n8n")
    except HTTPException as exc:
        trace.log("authentication_failed", provider="n8n", status_code=exc.status_code)
        if request_id_raw and len(request_id_raw) <= 64:
            record_integration_event(
                request_id_raw,
                "inbound",
                "n8n",
                "lead_action",
                "rejected",
                http_status=exc.status_code,
                error_category="authentication",
            )
        raise

    try:
        request_id = UUID(request_id_raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="X-Request-ID must be a UUID") from exc

    try:
        raw = await request.body()
        parsed = json.loads(raw)
        payload = LeadActionRequest.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError) as exc:
        record_integration_event(
            str(request_id),
            "inbound",
            "n8n",
            "lead_action",
            "rejected",
            http_status=422,
            error_category="validation",
        )
        trace.log("validation_failed", reason="invalid_lead_action")
        detail = (
            exc.errors(include_context=False)
            if isinstance(exc, ValidationError)
            else "Request body must be JSON"
        )
        return JSONResponse(status_code=422, content={"detail": detail})

    trace.log("request_parsed", action=payload.action, request_id=str(request_id))
    result = execute_lead_action(
        request_id=request_id,
        action=payload.action,
        lead_id=payload.lead_id,
        lead=payload.lead.model_dump(mode="python"),
    )
    record_integration_event(
        str(request_id),
        "inbound",
        "n8n",
        payload.action,
        "accepted" if result.status_code < 400 else "rejected",
        http_status=result.status_code,
        error_category=result.body.get("code"),
    )
    trace.complete(action=payload.action, status_code=result.status_code)
    return JSONResponse(status_code=result.status_code, content=result.body)


@router.post("/lead-sync")
async def lead_sync(request: Request):
    """Sync Sheet-owned fields; a changed phone is flagged for staff review."""
    request_id_raw = request.headers.get("x-request-id", "").strip()
    try:
        await require_n8n_intake_auth(request)
    except HTTPException as exc:
        if request_id_raw and len(request_id_raw) <= 64:
            record_integration_event(
                request_id_raw,
                "inbound",
                "n8n",
                "lead_sync",
                "rejected",
                http_status=exc.status_code,
                error_category="authentication",
            )
        raise

    try:
        request_id = UUID(request_id_raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="X-Request-ID must be a UUID") from exc

    try:
        payload = LeadSyncRequest.model_validate(json.loads(await request.body()))
    except (json.JSONDecodeError, ValidationError) as exc:
        detail = (
            exc.errors(include_context=False)
            if isinstance(exc, ValidationError)
            else "Request body must be JSON"
        )
        return JSONResponse(status_code=422, content={"detail": detail})

    result = sync_sheet_lead(
        request_id=request_id,
        lead_id=payload.lead_id,
        lead=payload.lead.model_dump(mode="python"),
    )
    record_integration_event(
        str(request_id),
        "inbound",
        "n8n",
        "lead_sync",
        "accepted" if result.status_code < 400 else "rejected",
        http_status=result.status_code,
        error_category=result.body.get("code"),
    )
    return JSONResponse(status_code=result.status_code, content=result.body)
