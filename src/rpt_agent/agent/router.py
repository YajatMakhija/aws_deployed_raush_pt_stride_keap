from __future__ import annotations

import json
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator, model_validator

from ..config import get_settings
from ..db import transaction
from ..observability import WorkflowTrace, trace_id_var
from ..security import DashboardActor, require_dashboard_auth
from .service import (
    MAX_CONVERSATION_CHARS,
    MAX_LEADS,
    MAX_MESSAGE_CHARS,
    MAX_MESSAGES,
    AgentConfigurationError,
    LeadContextError,
    PhiApprovalRequired,
    answer_question,
    stream_answer,
)

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])
Actor = Annotated[DashboardActor, Depends(require_dashboard_auth)]


class AgentRateLimitError(RuntimeError):
    pass


def _audit_request(
    actor: DashboardActor,
    lead_ids: list[str],
    message_count: int,
    trace_id: str,
) -> None:
    limit = get_settings().assistant_requests_per_minute
    with transaction() as conn:
        # PostgreSQL keeps this correct across API processes; no in-memory limiter to drift.
        conn.execute(
            "select pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"assistant:{actor.user_id}",),
        )
        recent = conn.execute(
            "select count(*) as count from dashboard_audit_log where actor_id=%s "
            "and action='assistant_request' and created_at>now()-interval '1 minute'",
            (actor.user_id,),
        ).fetchone()
        if recent and recent["count"] >= limit:
            raise AgentRateLimitError("assistant request limit exceeded")
        conn.execute(
            "insert into dashboard_audit_log(practice_id,actor_id,actor_email,action,"
            "entity_type,entity_id,metadata) values(null,%s,%s,'assistant_request',"
            "'assistant',%s,%s)",
            (
                actor.user_id,
                actor.email,
                trace_id,
                Jsonb(
                    {
                        "selected_lead_ids": lead_ids,
                        "selected_lead_count": len(lead_ids),
                        "message_count": message_count,
                    }
                ),
            ),
        )


class AgentMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)

    @field_validator("content")
    @classmethod
    def content_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value.strip()


class AgentRequest(BaseModel):
    messages: list[AgentMessage] = Field(min_length=1, max_length=MAX_MESSAGES)
    lead_ids: list[UUID] = Field(default_factory=list)
    current_path: str = Field(default="terminal", min_length=1, max_length=200)

    @field_validator("lead_ids")
    @classmethod
    def unique_leads(cls, values: list[UUID]) -> list[UUID]:
        unique = list(dict.fromkeys(values))
        if len(unique) > MAX_LEADS:
            raise ValueError(f"at most {MAX_LEADS} leads may be loaded")
        return unique

    @model_validator(mode="after")
    def valid_conversation(self):
        if self.messages[-1].role != "user":
            raise ValueError("the final message must be from the user")
        if sum(len(message.content) for message in self.messages) > MAX_CONVERSATION_CHARS:
            raise ValueError("conversation is too large")
        return self


class AgentResponse(BaseModel):
    answer: str
    trace_id: str


@router.post("/assistant", response_model=AgentResponse)
async def dashboard_assistant(payload: AgentRequest, actor: Actor):
    trace = WorkflowTrace("dashboard_assistant", "api", trace_id_var.get())
    trace.log(
        "request_parsed",
        actor_id=actor.user_id,
        selected_lead_count=len(payload.lead_ids),
        message_count=len(payload.messages),
    )
    try:
        if not get_settings().assistant_enabled:
            raise AgentConfigurationError("the assistant is disabled")
        lead_ids = [str(lead_id) for lead_id in payload.lead_ids]
        _audit_request(actor, lead_ids, len(payload.messages), trace.trace_id)
        answer = await answer_question(
            [message.model_dump() for message in payload.messages],
            lead_ids,
            payload.current_path,
        )
        trace.complete(selected_lead_count=len(payload.lead_ids))
        return AgentResponse(answer=answer, trace_id=trace.trace_id)
    except AgentConfigurationError as exc:
        trace.fail(exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PhiApprovalRequired as exc:
        trace.fail(exc)
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LeadContextError as exc:
        trace.fail(exc)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AgentRateLimitError as exc:
        trace.fail(exc)
        raise HTTPException(
            status_code=429,
            detail="Too many assistant requests. Try again shortly.",
            headers={"Retry-After": "60"},
        ) from exc
    except Exception as exc:
        trace.fail(exc)
        raise HTTPException(status_code=502, detail="The assistant is temporarily unavailable.") from exc


@router.post("/assistant/stream")
async def stream_dashboard_assistant(payload: AgentRequest, actor: Actor):
    trace = WorkflowTrace("dashboard_assistant_stream", "api", trace_id_var.get())
    trace.log(
        "request_parsed",
        actor_id=actor.user_id,
        selected_lead_count=len(payload.lead_ids),
        message_count=len(payload.messages),
    )
    try:
        if not get_settings().assistant_enabled:
            raise AgentConfigurationError("the assistant is disabled")
        lead_ids = [str(lead_id) for lead_id in payload.lead_ids]
        _audit_request(actor, lead_ids, len(payload.messages), trace.trace_id)
        chunks = stream_answer(
            [message.model_dump() for message in payload.messages],
            lead_ids,
            payload.current_path,
        )

        async def events():
            try:
                async for chunk in chunks:
                    yield f"event: delta\ndata: {json.dumps({'content': chunk})}\n\n"
                trace.complete(selected_lead_count=len(payload.lead_ids))
                yield f"event: done\ndata: {json.dumps({'trace_id': trace.trace_id})}\n\n"
            except Exception as exc:  # noqa: BLE001 - headers are already sent; emit a safe error event
                trace.fail(exc)
                yield "event: error\ndata: {}\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Trace-ID": trace.trace_id},
        )
    except AgentConfigurationError as exc:
        trace.fail(exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PhiApprovalRequired as exc:
        trace.fail(exc)
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LeadContextError as exc:
        trace.fail(exc)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AgentRateLimitError as exc:
        trace.fail(exc)
        raise HTTPException(
            status_code=429,
            detail="Too many assistant requests. Try again shortly.",
            headers={"Retry-After": "60"},
        ) from exc
    except Exception as exc:
        trace.fail(exc)
        raise HTTPException(status_code=502, detail="The assistant is temporarily unavailable.") from exc
