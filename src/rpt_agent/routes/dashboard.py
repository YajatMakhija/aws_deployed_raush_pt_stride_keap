from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator

from ..config import get_settings
from ..db import transaction
from ..observability import WorkflowTrace
from ..security import DashboardActor, require_dashboard_auth
from ..services.provider_http import ProviderError
from ..services.twilio_service import TwilioService
from ..worker import format_phone, materialize_cadence

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])
Actor = Annotated[DashboardActor, Depends(require_dashboard_auth)]


class CadenceAction(BaseModel):
    action: Literal["pause", "resume"]


class ContactRules(BaseModel):
    do_not_contact: bool | None = None
    call_opt_out: bool | None = None
    sms_opt_out: bool | None = None


class CadenceModeChange(BaseModel):
    mode: Literal["standard"]


class StageMove(BaseModel):
    stage: Literal["new", "cadence", "attention", "booked", "closed"]


class ReviewAction(BaseModel):
    resolution: str = Field(min_length=2, max_length=500)


class OutreachUpdate(BaseModel):
    scheduled_for: datetime


class CadenceStepUpdate(BaseModel):
    description: str | None = Field(default=None, min_length=2, max_length=300)
    is_active: bool | None = None


class CadenceVersionStepInput(BaseModel):
    day_offset: int = Field(ge=0, le=365)
    channel: Literal["call", "sms"]
    description: str = Field(min_length=2, max_length=300)
    is_active: bool = True
    sms_body: str | None = Field(default=None, max_length=1600)


class CadenceVersionCreate(BaseModel):
    source_version_id: int | None = Field(default=None, ge=1)
    lead_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def optional_name_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("name must not be blank")
        return value


class CadenceVersionUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    steps: list[CadenceVersionStepInput] = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value


class CadenceVersionNameUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=1600)

    @field_validator("name", "body")
    @classmethod
    def required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class TemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    body: str | None = Field(default=None, min_length=1, max_length=1600)

    @field_validator("name", "body")
    @classmethod
    def optional_text_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value


class ManualSmsRequest(BaseModel):
    body: str = Field(min_length=1, max_length=1600)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class LeadCreate(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=10, max_length=32)
    email: str | None = Field(
        default=None,
        max_length=320,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    date_of_birth: date
    referred_by: str | None = Field(default=None, max_length=200)
    lead_type: Literal["Physical Therapy", "Wellness"]
    location: Literal["Dana Point", "Laguna Niguel", "Mission Viejo"]
    owner: str = Field(min_length=1, max_length=200)
    contact_consent: Literal[True]

    @field_validator("first_name", "last_name", "owner")
    @classmethod
    def required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("date_of_birth")
    @classmethod
    def valid_date_of_birth(cls, value: date) -> date:
        if value > datetime.now(UTC).date():
            raise ValueError("date_of_birth cannot be in the future")
        return value


CLOSED_STATUSES = frozenset(
    {
        "declined",
        "transferred_human",
        "booking_link_sent",
        "do_not_contact",
        "closed_no_response",
        "invalid_phone",
    }
)


def _stage(row: dict) -> str:
    """Bucket a lead for the board. 'cadence' means outreach is still running."""
    if row["status"] == "booked":
        return "booked"
    if row["needs_review"] or row["status"] == "needs_attention":
        return "attention"
    # A finished lead is not in cadence. Without this it falls through below and
    # a declined or transferred patient keeps showing as actively worked.
    if row["status"] in CLOSED_STATUSES or row["cadence_state"] in {"completed", "terminated"}:
        return "closed"
    if row["status"] == "new" or row["cadence_state"] == "pending":
        return "new"
    # Every step has run and nothing is awaiting a provider result, so the
    # cadence is spent even though the lead was never explicitly closed.
    if row.get("cadence_total") and not row.get("next_event_id"):
        return "closed"
    return "cadence"


CLOSED_REASON = {
    # 'declined' is only ever reached from a not_interested call outcome, and
    # maps straight back to it. Saying "declined" made staff look for a
    # different event than the one that happened.
    "declined": "Not interested",
    "transferred_human": "Transferred to staff",
    "booking_link_sent": "Booking link sent",
    "do_not_contact": "Do not contact",
    "closed_no_response": "Closed, no response",
    "invalid_phone": "Invalid phone number",
    "booked": "Appointment booked",
}


def _lead(row: dict) -> dict:
    stage = _stage(row)
    next_status = row.get("next_event_status")
    next_step = row.get("next_step")
    if next_status in {"attempted", "in_flight"}:
        next_step = f"Awaiting result: {next_step or row.get('next_channel', 'outreach')}"
    if stage in {"closed", "booked"}:
        # Outreach has stopped, so never advertise a next action that will not run.
        next_step = CLOSED_REASON.get(row["status"], "Outreach complete")
        next_status = None
    return {
        "id": str(row["id"]),
        "display_id": f"RPT-{str(row['id']).split('-')[0].upper()}",
        "full_name": row["full_name"],
        "phone": row.get("phone_e164"),
        "email": row.get("email"),
        "source": row.get("source_system"),
        "status": row["status"],
        "stage": stage,
        "cadence_state": row["cadence_state"],
        "needs_review": row["needs_review"],
        "review_reason": row.get("review_reason"),
        "next_event_id": row.get("next_event_id"),
        "next_event_status": next_status,
        "next_step": next_step,
        "next_channel": row.get("next_channel"),
        "next_scheduled_for": row.get("next_scheduled_for"),
        "cadence_progress": row.get("cadence_progress", 0),
        "cadence_total": row.get("cadence_total", 0),
        "cadence_version_name": row.get("cadence_version_name"),
        "created_at": row["created_at"],
        "last_contacted_at": row.get("last_contacted_at"),
        "date_of_birth": row.get("date_of_birth"),
        "referred_by": row.get("referred_by"),
        "lead_type": row.get("lead_type"),
        "location": row.get("location"),
        "owner": row.get("owner"),
        "is_test": bool(row.get("is_test")),
    }


def _audit(
    conn,
    actor: DashboardActor,
    practice_id: int | None,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict | None = None,
) -> None:
    conn.execute(
        "insert into dashboard_audit_log(practice_id,actor_id,actor_email,action,entity_type,"
        "entity_id,metadata) values(%s,%s,%s,%s,%s,%s,%s)",
        (
            practice_id,
            actor.user_id,
            actor.email,
            action,
            entity_type,
            entity_id,
            Jsonb(metadata or {}),
        ),
    )


def _version_payload(conn, version: dict) -> dict:
    steps = conn.execute(
        "select cs.id,cs.step_order,cs.day_offset,cs.channel,cs.key,cs.description,"
        "cs.is_active,mt.body as sms_body from cadence_steps cs "
        "left join message_templates mt on mt.cadence_step_id=cs.id and mt.is_active "
        "where cs.cadence_version_id=%s order by cs.day_offset,cs.step_order",
        (version["id"],),
    ).fetchall()
    return {
        **dict(version),
        "lead_id": str(version["lead_id"]) if version.get("lead_id") else None,
        "scope": "lead" if version.get("lead_id") else "global",
        "steps": steps,
    }


def _validate_cadence_steps(steps: list[CadenceVersionStepInput]) -> None:
    if not any(step.is_active for step in steps):
        raise HTTPException(status_code=422, detail="at least one cadence step must be enabled")
    for step in steps:
        if not step.description.strip():
            raise HTTPException(status_code=422, detail="cadence descriptions must not be blank")
        if step.channel == "sms" and not (step.sms_body or "").strip():
            raise HTTPException(status_code=422, detail="every SMS step requires message copy")


def _clone_version_steps(
    conn, source_id: int, target_id: int, practice_id: int, lead_id: UUID | None = None
) -> None:
    source_steps = conn.execute(
        "select id,step_order,day_offset,channel,key,description,is_active from cadence_steps "
        "where cadence_version_id=%s order by day_offset,step_order",
        (source_id,),
    ).fetchall()
    for step in source_steps:
        cloned = conn.execute(
            "insert into cadence_steps(practice_id,cadence_version_id,step_order,day_offset,"
            "channel,key,description,is_active) values(%s,%s,%s,%s,%s,%s,%s,%s) returning id",
            (
                practice_id,
                target_id,
                step["step_order"],
                step["day_offset"],
                step["channel"],
                step["key"],
                step["description"],
                step["is_active"],
            ),
        ).fetchone()
        template = conn.execute(
            "select mt.key,mt.channel,coalesce(lmo.body,mt.body) as body,mt.is_active "
            "from message_templates mt left join lead_message_overrides lmo "
            "on lmo.message_template_id=mt.id and lmo.lead_id=%s where mt.cadence_step_id=%s",
            (lead_id, step["id"]),
        ).fetchone()
        if template:
            conn.execute(
                "insert into message_templates(practice_id,cadence_version_id,cadence_step_id,key,"
                "channel,body,is_active) values(%s,%s,%s,%s,%s,%s,%s)",
                (
                    practice_id,
                    target_id,
                    cloned["id"],
                    template["key"],
                    template["channel"],
                    template["body"],
                    template["is_active"],
                ),
            )


@router.get("/snapshot")
def dashboard_snapshot(actor: Actor):
    del actor
    with transaction() as conn:
        rows = conn.execute(
            "select l.id,l.full_name,l.phone_e164,l.email,l.source_system,l.status,l.cadence_state,"
            "l.needs_review,l.review_reason,l.created_at,l.last_contacted_at,l.date_of_birth,"
            "l.referred_by,l.lead_type,l.location,l.owner,l.is_test,"
            "current_version.name as cadence_version_name,"
            "(select count(*) from outreach_events progress where progress.lead_id=l.id "
            "and progress.cadence_version_id=current_version.id "
            "and progress.status<>'planned') as cadence_progress,"
            "(select count(*) from outreach_events total where total.lead_id=l.id "
            "and total.cadence_version_id=current_version.id) as cadence_total,"
            "next_event.id as next_event_id,next_event.status as next_event_status,"
            "next_event.description as next_step,next_event.channel as next_channel,"
            "next_event.scheduled_for as next_scheduled_for from leads l left join lateral ("
            # The version stamped on the lead's own schedule, not whichever is
            # active for the practice. Leads stay pinned to the version they
            # started on, so reading the practice default named a cadence the
            # lead was not on and counted progress against events that did not
            # exist -- every mid-cadence lead read "0 of 0".
            "select cv.id,cv.name from cadence_versions cv where cv.id=coalesce("
            "(select oe2.cadence_version_id from outreach_events oe2 where oe2.lead_id=l.id "
            "and oe2.cadence_version_id is not null order by oe2.created_at desc,oe2.id desc limit 1),"
            "(select cv2.id from cadence_versions cv2 where cv2.practice_id=l.practice_id "
            "and cv2.status='active' and (cv2.lead_id=l.id or cv2.lead_id is null) "
            "order by (cv2.lead_id is not null) desc limit 1))"
            ") current_version on true left join lateral ("
            "select oe.id,cs.description,oe.channel,oe.scheduled_for,oe.status from outreach_events oe "
            "left join cadence_steps cs on cs.id=oe.cadence_step_id where oe.lead_id=l.id "
            "and oe.status in ('planned','in_flight','attempted') order by "
            "case when oe.status='planned' then 0 else 1 end,oe.scheduled_for nulls last,oe.id limit 1"
            ") next_event on true order by l.created_at desc limit 250"
        ).fetchall()
        leads = [_lead(row) for row in rows]
        appointments = conn.execute(
            "select a.id,a.lead_id,l.full_name,a.state,a.start_utc,a.end_utc,a.location_id,"
            "a.appointment_type_id,a.needs_staff_review from appointments a join leads l on l.id=a.lead_id "
            "where a.state in ('booking','scheduled','unknown') order by a.start_utc nulls last limit 100"
        ).fetchall()
        cadence = conn.execute(
            "select cs.id,cs.step_order,cs.day_offset,cs.channel,cs.key,cs.description,cs.is_active "
            "from cadence_steps cs join cadence_versions cv on cv.id=cs.cadence_version_id "
            "join practices p on p.id=cs.practice_id where p.slug='rausch-pt' "
            "and cv.lead_id is null and cv.status='active' "
            "order by cs.day_offset,cs.step_order"
        ).fetchall()
        templates = conn.execute(
            "select mt.id,mt.key,mt.key as name,mt.body,mt.is_active,mt.cadence_step_id,"
            "mt.cadence_version_id,(mt.cadence_step_id is null) as deletable,"
            "cs.day_offset,cs.description,cv.name as version_name "
            "from message_templates mt join practices p on p.id=mt.practice_id "
            "left join cadence_versions cv on cv.id=mt.cadence_version_id "
            "left join cadence_steps cs on cs.id=mt.cadence_step_id where p.slug='rausch-pt' "
            "and mt.channel='sms' and ((mt.cadence_step_id is null and mt.cadence_version_id is null) "
            "or (mt.cadence_step_id is not null and cv.lead_id is null and cv.status='active')) "
            "order by (mt.cadence_step_id is null),cs.day_offset,cs.step_order,mt.id"
        ).fetchall()
        system = conn.execute(
            "select (select count(*) from provider_events where processed_at is null) as provider_queue,"
            "(select count(*) from integration_outbox where status in ('pending','sending')) as handoff_queue,"
            "(select count(*) from outreach_events where status='unknown') as unknown_events,"
            "(select count(*) from leads where needs_review) as review_queue,"
            # Real counters for the analytics page. These replaced hardcoded
            # figures, so every number shown must come from a row somewhere.
            # Delivery must come from sms_messages: an outreach_event marked
            # 'delivered' only means the worker handed the message to Twilio,
            # not that the patient received it.
            "(select count(*) from sms_messages where direction='outbound') as messages_sent,"
            "(select count(*) from sms_messages where direction='outbound' "
            "and delivery_status='delivered') as messages_delivered,"
            "(select count(*) from sms_messages where direction='outbound' "
            "and delivery_status in ('failed','undelivered')) as messages_failed,"
            "(select count(*) from outreach_events where channel='call' and status='delivered') "
            "as calls_completed,"
            "(select count(*) from outreach_events where channel='call' and executed_at is not null) "
            "as calls_attempted,"
            "(select count(*) from outreach_events where channel='call' and outcome in "
            "('booked','not_interested','callback','transferred')) as calls_reached,"
            # Real provider usage. The dashboard previously showed fixed figures
            # here, which cannot be told apart from a genuine reading.
            "(select coalesce(sum(duration_seconds),0) from call_logs) as voice_seconds,"
            "(select coalesce(sum(cost),0) from call_logs) as voice_cost,"
            "(select count(*) from call_logs) as calls_logged,"
            "(select count(*) from appointments) as stride_appointments,"
            "(select count(*) from integration_outbox where status='sent') as keap_handoffs"
        ).fetchone()

    counts = {"new": 0, "cadence": 0, "attention": 0, "booked": 0, "closed": 0}
    for lead in leads:
        counts[lead["stage"]] += 1
    settings = get_settings()
    total_leads = len(leads)

    def _rate(part: int, whole: int) -> float | None:
        # None, not zero: "no data yet" and "zero percent" are different answers.
        return round(part / whole * 100, 1) if whole else None

    # .get keeps the payload well-formed if a counter is ever missing, rather
    # than failing the whole snapshot for the sake of one tile.
    def _count(name: str) -> int:
        return int(system.get(name) or 0)

    metrics = {
        "total_leads": total_leads,
        "messages_sent": _count("messages_sent"),
        "messages_delivered": _count("messages_delivered"),
        "messages_failed": _count("messages_failed"),
        "messages_pending": max(
            _count("messages_sent") - _count("messages_delivered") - _count("messages_failed"), 0
        ),
        "messages_delivery_rate": _rate(_count("messages_delivered"), _count("messages_sent")),
        "calls_completed": _count("calls_completed"),
        "calls_completion_rate": _rate(_count("calls_completed"), _count("calls_attempted")),
        "calls_reached_rate": _rate(_count("calls_reached"), _count("calls_attempted")),
        "voice_minutes": round(_count("voice_seconds") / 60, 1),
        "voice_seconds": _count("voice_seconds"),
        "voice_cost": float(system.get("voice_cost") or 0),
        "calls_logged": _count("calls_logged"),
        "stride_appointments": _count("stride_appointments"),
        "keap_handoffs": _count("keap_handoffs"),
        "review_rate": _rate(_count("review_queue"), total_leads),
        "booked_rate": _rate(counts["booked"], total_leads),
    }
    return {
        "generated_at": datetime.now(UTC),
        "counts": counts,
        "leads": leads,
        "appointments": appointments,
        "cadence": cadence,
        "templates": templates,
        "providers": [
            {"name": "Vapi", "mode": settings.mode("vapi"), "status": "configured", "balance": None},
            {"name": "Twilio", "mode": settings.mode("twilio"), "status": "configured", "balance": None},
            {"name": "Stride", "mode": settings.mode("stride"), "status": "configured", "balance": None},
            {"name": "Keap", "mode": settings.mode("keap"), "status": "configured", "balance": None},
        ],
        "system": system,
        "metrics": metrics,
    }


@router.post("/leads", status_code=201)
def create_dashboard_lead(payload: LeadCreate, actor: Actor):
    phone = format_phone(payload.phone)
    if not phone:
        raise HTTPException(status_code=422, detail="phone must be a valid E.164 or US number")
    settings = get_settings()
    synthetic = settings.test_mode and settings.app_env.lower() in {"development", "test"}
    test_run_id = uuid4() if synthetic else None

    with transaction() as conn:
        practice = conn.execute(
            "select id,timezone from practices where slug='rausch-pt'"
        ).fetchone()
        if not practice:
            raise HTTPException(status_code=503, detail="practice is not configured")

        existing = conn.execute(
            "select id from leads where practice_id=%s and source_system='dashboard' "
            "and external_referral_id=%s",
            (practice["id"], payload.idempotency_key),
        ).fetchone()
        if existing:
            lead_id = existing["id"]
        else:
            inserted = conn.execute(
                "insert into leads(practice_id,source_system,external_referral_id,first_name,"
                "last_name,full_name,phone_e164,phone_original,email,date_of_birth,timezone,"
                "line_type,consent_captured_at,consent_source,consent_reference,"
                "consent_text_version,status,cadence_state,lead_type,referred_by,location,owner,"
                "is_test,test_run_id) "
                "values(%s,'dashboard',%s,%s,%s,%s,%s,%s,%s,%s,%s,'unknown',now(),"
                "%s,%s,'dashboard-manual-v1','new','pending',%s,%s,%s,%s,%s,%s) "
                "returning id",
                (
                    practice["id"],
                    payload.idempotency_key,
                    payload.first_name,
                    payload.last_name,
                    f"{payload.first_name} {payload.last_name}",
                    phone,
                    payload.phone,
                    payload.email.strip().lower() if payload.email else None,
                    payload.date_of_birth,
                    practice["timezone"],
                    "dashboard_staff_attestation",
                    f"dashboard:{payload.idempotency_key}",
                    payload.lead_type,
                    payload.referred_by.strip() if payload.referred_by else None,
                    payload.location,
                    payload.owner,
                    synthetic,
                    test_run_id,
                ),
            ).fetchone()
            lead_id = inserted["id"]
            event_count = materialize_cadence(
                conn, str(lead_id), practice["id"], datetime.now(UTC).date()
            )
            if not event_count:
                raise HTTPException(status_code=409, detail="no active cadence is configured")
            _audit(
                conn,
                actor,
                practice["id"],
                "lead.created",
                "lead",
                str(lead_id),
                {
                    "lead_type": payload.lead_type,
                    "location": payload.location,
                    "cadence_events": event_count,
                },
            )

        row = conn.execute(
            "select l.id,l.full_name,l.phone_e164,l.email,l.source_system,l.status,l.cadence_state,"
            "l.needs_review,l.review_reason,l.created_at,l.last_contacted_at,l.date_of_birth,"
            "l.referred_by,l.lead_type,l.location,l.owner,l.is_test,"
            "(select count(*) from outreach_events progress where progress.lead_id=l.id "
            "and progress.status<>'planned') as cadence_progress,"
            "(select count(*) from outreach_events total where total.lead_id=l.id) as cadence_total,"
            "next_event.id as next_event_id,next_event.status as next_event_status,"
            "next_event.description as next_step,next_event.channel as next_channel,"
            "next_event.scheduled_for as next_scheduled_for from leads l left join lateral ("
            "select oe.id,cs.description,oe.channel,oe.scheduled_for,oe.status from outreach_events oe "
            "left join cadence_steps cs on cs.id=oe.cadence_step_id where oe.lead_id=l.id "
            "and oe.status in ('planned','in_flight','attempted') order by "
            "case when oe.status='planned' then 0 else 1 end,oe.scheduled_for nulls last,oe.id limit 1"
            ") next_event on true where l.id=%s",
            (lead_id,),
        ).fetchone()
    return _lead(row)


@router.get("/leads/{lead_id}")
def dashboard_lead(lead_id: UUID, actor: Actor):
    del actor
    with transaction() as conn:
        row = conn.execute(
            "select id,practice_id,full_name,first_name,last_name,phone_e164,email,date_of_birth,"
            "source_system,status,status_reason,cadence_state,call_opt_out,sms_opt_out,needs_review,"
            "review_reason,created_at,updated_at,last_contacted_at,callback_requested_at,referred_by,"
            "lead_type,location,owner,is_test from leads where id=%s",
            (lead_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="lead not found")
        # What the practice would give a new lead today.
        global_version = conn.execute(
            "select id,name,version_number,status,lead_id from cadence_versions "
            "where practice_id=%s and status='active' and (lead_id=%s or lead_id is null) "
            "order by (lead_id is not null) desc limit 1",
            (row["practice_id"], lead_id),
        ).fetchone()
        # What this lead is actually running. A lead keeps the version it started
        # on, so these differ whenever a new cadence was activated mid-outreach,
        # and the record has to say which one governs this patient.
        current_version = conn.execute(
            "select id,name,version_number,status,lead_id from cadence_versions where id=("
            "select oe.cadence_version_id from outreach_events oe where oe.lead_id=%s "
            "and oe.cadence_version_id is not null order by oe.created_at desc,oe.id desc limit 1)",
            (lead_id,),
        ).fetchone() or global_version
        events = conn.execute(
            # sm.delivery_status is the only honest answer for an SMS step. An
            # outreach_event reaching 'delivered' only means Twilio accepted the
            # message; the carrier can still reject it (30034, unregistered sender)
            # minutes later. Without this join the timeline shows "Completed" for a
            # text the patient never received.
            "select oe.id,oe.cadence_step_id,oe.cadence_version_id,oe.channel,oe.day_offset,"
            "oe.status,oe.scheduled_for,oe.created_at,oe.executed_at,oe.outcome,cs.description,"
            "cv.name as cadence_version_name,sm.delivery_status,sm.failure_reason "
            "from outreach_events oe left join cadence_steps cs on cs.id=oe.cadence_step_id "
            "left join cadence_versions cv on cv.id=oe.cadence_version_id "
            "left join sms_messages sm on sm.outreach_event_id=oe.id "
            "where oe.lead_id=%s order by oe.scheduled_for nulls last,oe.id",
            (lead_id,),
        ).fetchall()
        messages = conn.execute(
            "select id,direction,body,occurred_at,delivered_at,delivery_status,failure_reason from sms_messages "
            "where lead_id=%s order by occurred_at,id",
            (lead_id,),
        ).fetchall()
        calls = conn.execute(
            "select id,dialed_at,ended_at,duration_seconds,answer_state,ended_reason,transcript_text,"
            "summary_text from call_logs where lead_id=%s order by dialed_at desc,id desc",
            (lead_id,),
        ).fetchall()
        appointments = conn.execute(
            "select id,state,start_utc,end_utc,booked_at,location_id,appointment_type_id,"
            "needs_staff_review from appointments where lead_id=%s order by booked_at desc",
            (lead_id,),
        ).fetchall()
        status_history = conn.execute(
            "select from_status,to_status,reason,source,changed_at from lead_status_history "
            "where lead_id=%s order by changed_at desc limit 100",
            (lead_id,),
        ).fetchall()
        # Pause and resume are only in the audit log, but the cadence view has to
        # show them: after a pause every overdue step fires in one tick, and a
        # burst of four steps at one timestamp is unreadable without the reason.
        cadence_actions = conn.execute(
            "select action,created_at from dashboard_audit_log where entity_type='lead' "
            "and entity_id=%s and action in ('cadence.pause','cadence.resume') "
            "order by created_at",
            (str(lead_id),),
        ).fetchall()
        overrides = conn.execute(
            "select lmo.message_template_id,lmo.body,lmo.updated_at from lead_message_overrides lmo "
            "where lmo.lead_id=%s",
            (lead_id,),
        ).fetchall()
        version_payload = _version_payload(conn, current_version) if current_version else None
    detail = dict(row)
    detail["id"] = str(detail["id"])
    detail["display_id"] = f"RPT-{str(row['id']).split('-')[0].upper()}"
    detail["phone"] = detail.get("phone_e164")
    detail["source"] = detail.get("source_system")
    current_events = [
        event for event in events
        if current_version and event.get("cadence_version_id") == current_version["id"]
    ]
    detail["cadence_progress"] = sum(event["status"] != "planned" for event in current_events)
    detail["cadence_total"] = len(current_events)
    detail["cadence_version_name"] = current_version["name"] if current_version else None
    # Only set when the practice has moved on, so the UI can offer a deliberate
    # migration rather than silently implying the lead is on the newest plan.
    detail["global_version_name"] = (
        global_version["name"]
        if global_version and current_version and global_version["id"] != current_version["id"]
        else None
    )
    next_event = next((event for event in events if event["status"] == "planned"), None)
    if not next_event:
        next_event = next(
            (event for event in events if event["status"] in {"in_flight", "attempted"}), None
        )
    detail["next_event_id"] = next_event["id"] if next_event else None
    # Staged after the totals above: _stage needs them to tell an exhausted
    # cadence apart from one that is still running.
    detail["stage"] = _stage(
        {**row, "cadence_total": detail["cadence_total"], "next_event_id": detail["next_event_id"]}
    )
    detail["next_event_status"] = next_event["status"] if next_event else None
    detail["next_channel"] = next_event["channel"] if next_event else None
    detail["next_scheduled_for"] = next_event["scheduled_for"] if next_event else None
    detail["next_step"] = next_event["description"] if next_event else None
    if next_event and next_event["status"] in {"in_flight", "attempted"}:
        detail["next_step"] = f"Awaiting result: {detail['next_step'] or detail['next_channel']}"
    if detail["stage"] in {"closed", "booked"}:
        # Same rule as the board: a finished lead has an outcome, not a next action.
        detail["next_step"] = CLOSED_REASON.get(row["status"], "Outreach complete")
        detail["next_event_id"] = None
        detail["next_event_status"] = None
        detail["next_channel"] = None
        detail["next_scheduled_for"] = None
    return {
        "lead": detail,
        "events": events,
        "messages": messages,
        "calls": calls,
        "appointments": appointments,
        "history": status_history,
        "cadence_actions": cadence_actions,
        "message_overrides": overrides,
        "cadence_version": version_payload,
    }


@router.post("/leads/{lead_id}/cadence")
def update_lead_cadence(lead_id: UUID, payload: CadenceAction, actor: Actor):
    with transaction() as conn:
        lead = conn.execute(
            "select id,practice_id,status,cadence_state from leads where id=%s for update", (lead_id,)
        ).fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail="lead not found")
        if payload.action == "resume" and lead["status"] in {
            "booked", "declined", "do_not_contact", "invalid_phone"
        }:
            raise HTTPException(status_code=409, detail="terminal leads cannot resume cadence")
        new_state = "paused" if payload.action == "pause" else "active"
        shifted = 0
        if payload.action == "resume":
            # Pause has to mean postpone, not suspend. The schedule keeps running
            # while a lead is paused, so without this every step that fell due
            # during the pause is overdue the moment it resumes and they all fire
            # in one tick -- a fortnight of calls and texts inside a minute.
            #
            # Shifting the remainder by however long the pause lasted keeps the
            # spacing the cadence was designed with: day 5 still lands two days
            # after day 3. The pause start comes from the audit trail, which is
            # already the record of when it happened.
            paused_at = conn.execute(
                "select created_at from dashboard_audit_log where entity_type='lead' "
                "and entity_id=%s and action='cadence.pause' order by created_at desc limit 1",
                (str(lead_id),),
            ).fetchone()
            if paused_at:
                shifted = len(
                    conn.execute(
                        "update outreach_events set scheduled_for=scheduled_for+(now()-%s),"
                        "updated_at=now() where lead_id=%s and status='planned' returning id",
                        (paused_at["created_at"], lead_id),
                    ).fetchall()
                )
        conn.execute("update leads set cadence_state=%s where id=%s", (new_state, lead_id))
        _audit(
            conn, actor, lead["practice_id"], f"cadence.{payload.action}", "lead", str(lead_id),
            {"shifted_events": shifted} if shifted else None,
        )
    return {"status": "updated", "cadence_state": new_state, "shifted_events": shifted}


@router.post("/leads/{lead_id}/contact-rules")
def set_contact_rules(lead_id: UUID, payload: ContactRules, actor: Actor):
    """Apply the Do not contact and opt-out switches to the lead itself.

    These were display-only until now: the dashboard flipped a local switch and
    told the user contact was blocked while the worker carried on calling.

    Do not contact is terminal, so it also stops outreach -- leaving planned
    steps alive under a do_not_contact status would be the same lie in a
    different place. Clearing it releases the block but does not restart the
    cadence; staff restart a lead from the board deliberately.
    """
    with transaction() as conn:
        lead = conn.execute(
            "select id,practice_id,status,cadence_state,call_opt_out,sms_opt_out "
            "from leads where id=%s for update",
            (lead_id,),
        ).fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail="lead not found")

        changes: dict[str, object] = {}
        for field in ("call_opt_out", "sms_opt_out"):
            value = getattr(payload, field)
            if value is not None and value != lead[field]:
                conn.execute(f"update leads set {field}=%s where id=%s", (value, lead_id))
                changes[field] = value

        if payload.do_not_contact is not None:
            currently = lead["status"] == "do_not_contact"
            if payload.do_not_contact and not currently:
                conn.execute(
                    "update leads set status='do_not_contact',cadence_state='terminated',"
                    "status_changed_at=now() where id=%s",
                    (lead_id,),
                )
                conn.execute(
                    "update outreach_events set status='skipped',updated_at=now() "
                    "where lead_id=%s and status='planned'",
                    (lead_id,),
                )
                changes["do_not_contact"] = True
            elif not payload.do_not_contact and currently:
                conn.execute(
                    "update leads set status='in_progress',status_changed_at=now() where id=%s",
                    (lead_id,),
                )
                changes["do_not_contact"] = False

        if changes:
            _audit(
                conn, actor, lead["practice_id"], "lead.contact_rules", "lead", str(lead_id), changes
            )
        row = conn.execute(
            "select status,cadence_state,call_opt_out,sms_opt_out from leads where id=%s", (lead_id,)
        ).fetchone()
    return {
        "do_not_contact": row["status"] == "do_not_contact",
        "call_opt_out": row["call_opt_out"],
        "sms_opt_out": row["sms_opt_out"],
        "cadence_state": row["cadence_state"],
    }


@router.post("/leads/{lead_id}/cadence-mode")
def use_standard_cadence(lead_id: UUID, payload: CadenceModeChange, actor: Actor):
    del payload
    with transaction() as conn:
        lead = conn.execute(
            "select id,practice_id,status,cadence_state from leads where id=%s for update",
            (lead_id,),
        ).fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail="lead not found")
        if lead["status"] in CLOSED_STATUSES | {"booked"}:
            raise HTTPException(status_code=409, detail="a terminal lead cannot restart outreach")
        standard = conn.execute(
            "select id from cadence_versions where practice_id=%s and lead_id is null "
            "and status='active'",
            (lead["practice_id"],),
        ).fetchone()
        if not standard:
            raise HTTPException(status_code=409, detail="no active standard cadence is available")
        conn.execute(
            "update cadence_versions set status='archived' where lead_id=%s and status='active'",
            (lead_id,),
        )
        conn.execute(
            "update outreach_events set status='skipped',updated_at=now() "
            "where lead_id=%s and status='planned'",
            (lead_id,),
        )
        created = materialize_cadence(
            conn,
            str(lead_id),
            lead["practice_id"],
            datetime.now(UTC).date(),
            cadence_version_id=standard["id"],
            update_lead=lead["cadence_state"] == "pending",
        )
        _audit(
            conn,
            actor,
            lead["practice_id"],
            "cadence.standard_selected",
            "lead",
            str(lead_id),
            {"cadence_version_id": standard["id"], "events_created": created},
        )
    return {
        "status": "updated",
        "mode": "standard",
        "cadence_version_id": standard["id"],
        "events_created": created,
    }


@router.delete("/leads/{lead_id}")
def delete_dashboard_lead(lead_id: UUID, actor: Actor):
    """Remove a lead and everything belonging to it.

    Six tables cascade from leads(id) already -- outreach events, call logs,
    transcripts, status history, message overrides and any lead-scoped cadence.
    Three do not, and each needs a decision rather than a cascade:

      appointments, dashboard_sms_requests  RESTRICT, so they block the delete
      sms_messages, notification_log        SET NULL, leaving patient message
                                            bodies behind with no owner
      test_usage_ledger                     SET NULL, and kept on purpose: it
                                            records money already spent

    So the first four are removed explicitly and the usage ledger is left to
    null out, preserving the billing trail.
    """
    with transaction() as conn:
        lead = conn.execute(
            "select id,practice_id,full_name,phone_e164 from leads where id=%s for update",
            (lead_id,),
        ).fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail="lead not found")

        # A call already handed to the provider will report back by webhook.
        # Deleting now would leave that result with nothing to attach to, so
        # wait for it to land instead.
        in_flight = conn.execute(
            "select count(*) as total from outreach_events where lead_id=%s "
            "and status in ('in_flight','attempted')",
            (lead_id,),
        ).fetchone()
        if in_flight["total"]:
            raise HTTPException(
                status_code=409,
                detail="a call or message is still in progress for this lead; try again shortly",
            )

        removed = {}
        for table in ("appointments", "dashboard_sms_requests", "sms_messages", "notification_log"):
            cursor = conn.execute(f"delete from {table} where lead_id=%s", (lead_id,))
            removed[table] = cursor.rowcount
        events = conn.execute(
            "select count(*) as total from outreach_events where lead_id=%s", (lead_id,)
        ).fetchone()["total"]

        # A lead with a personalised cadence needs that unwound by hand. Deleting
        # the lead cascades into its cadence_versions row, but three tables hold
        # that row with RESTRICT -- cadence_steps, message_templates and
        # outreach_events -- so the cascade is refused and the whole delete fails.
        # Clearing the events first is what frees the version to go.
        conn.execute("delete from outreach_events where lead_id=%s", (lead_id,))
        lead_versions = "select id from cadence_versions where lead_id=%s"
        for table in ("message_templates", "cadence_steps"):
            conn.execute(
                f"delete from {table} where cadence_version_id in ({lead_versions})", (lead_id,)
            )
        cursor = conn.execute("delete from cadence_versions where lead_id=%s", (lead_id,))
        removed["personal_cadence_versions"] = cursor.rowcount

        # Audit before the row goes: the log keeps ids as text, so it survives.
        _audit(
            conn,
            actor,
            lead["practice_id"],
            "lead.deleted",
            "lead",
            str(lead_id),
            {"full_name": lead["full_name"], "cascaded_events": events, **removed},
        )
        conn.execute("delete from leads where id=%s", (lead_id,))
    return {"deleted": str(lead_id), "cascaded_events": events, **removed}


@router.post("/leads/{lead_id}/stage")
def move_lead_stage(lead_id: UUID, payload: StageMove, actor: Actor):
    """Move a lead between board columns.

    Dropping onto 'new' restarts outreach: the remaining schedule is discarded and
    a fresh cadence is built from today, so the patient is contacted from day zero
    again. Every move is audited because a person, not the system, decided it.
    """
    with transaction() as conn:
        lead = conn.execute(
            "select id,practice_id,status,cadence_state,needs_review,call_opt_out,sms_opt_out "
            "from leads where id=%s for update",
            (lead_id,),
        ).fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail="lead not found")
        if lead["status"] == "do_not_contact" and payload.stage in {"new", "cadence"}:
            raise HTTPException(
                status_code=409, detail="a do-not-contact lead cannot be returned to outreach"
            )

        restarted = 0
        if payload.stage == "new":
            if lead["call_opt_out"] and lead["sms_opt_out"]:
                raise HTTPException(
                    status_code=409, detail="this lead has opted out of every channel"
                )
            # 'planned' and 'skipped' both mean nothing was ever sent, so neither
            # is history worth keeping. Anything dispatched stays untouched.
            conn.execute(
                "delete from outreach_events where lead_id=%s and status in ('planned','skipped')",
                (lead_id,),
            )
            conn.execute(
                "update leads set status='new',cadence_state='pending',needs_review=false,"
                "review_reason=null,review_flagged_at=null,last_call_outcome=null,"
                "status_reason=null,callback_requested_at=null,callback_notes=null,"
                "call_attempts=0,status_changed_at=now() where id=%s",
                (lead_id,),
            )
            restarted = materialize_cadence(
                conn, str(lead_id), lead["practice_id"], datetime.now(UTC).date()
            )
        elif payload.stage == "cadence":
            conn.execute(
                "update leads set status='in_progress',cadence_state='active',needs_review=false,"
                "review_reason=null,review_flagged_at=null,status_changed_at=now() where id=%s",
                (lead_id,),
            )
        elif payload.stage == "attention":
            conn.execute(
                "update leads set needs_review=true,review_reason=coalesce(review_reason,%s),"
                "review_flagged_at=now(),cadence_state='paused',status_changed_at=now() where id=%s",
                ("moved to review from the board", lead_id),
            )
        elif payload.stage == "closed":
            conn.execute(
                "update leads set status='declined',cadence_state='terminated',needs_review=false,"
                "review_reason=null,status_reason=coalesce(status_reason,%s),"
                "status_changed_at=now() where id=%s",
                ("closed from the board", lead_id),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() "
                "where lead_id=%s and status='planned'",
                (lead_id,),
            )
        else:  # booked
            # Booked is a claim about the outside world, so it needs a real appointment.
            appointment = conn.execute(
                "select id from appointments where lead_id=%s and state='scheduled' limit 1",
                (lead_id,),
            ).fetchone()
            if not appointment:
                raise HTTPException(
                    status_code=409,
                    detail="booked requires a confirmed appointment on the lead",
                )
            conn.execute(
                "update leads set status='booked',cadence_state='completed',needs_review=false,"
                "status_changed_at=now() where id=%s",
                (lead_id,),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() "
                "where lead_id=%s and status='planned'",
                (lead_id,),
            )

        # to_status is a lead status, not a board stage. Recording payload.stage
        # put 'cadence', 'closed' and 'attention' into the column -- names that
        # are not lead statuses at all -- so the history disagreed with the lead
        # it described. Read back what the move actually set.
        moved_to = conn.execute("select status from leads where id=%s", (lead_id,)).fetchone()["status"]
        conn.execute(
            "insert into lead_status_history(lead_id,from_status,to_status,source,reason) "
            "values(%s,%s,%s,'dashboard',%s)",
            (lead_id, lead["status"], moved_to, f"moved to {payload.stage} from the board"),
        )
        _audit(
            conn,
            actor,
            lead["practice_id"],
            "lead.stage_move",
            "lead",
            str(lead_id),
            {"from": lead["status"], "to": payload.stage, "events_created": restarted},
        )
    return {"status": "updated", "stage": payload.stage, "events_created": restarted}


@router.patch("/leads/{lead_id}/outreach-events/{event_id}")
def update_outreach_event(
    lead_id: UUID, event_id: int, payload: OutreachUpdate, actor: Actor
):
    if payload.scheduled_for.tzinfo is None or payload.scheduled_for <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail="scheduled_for must be a future timezone-aware value")
    with transaction() as conn:
        event = conn.execute(
            "select oe.id,l.practice_id from outreach_events oe join leads l on l.id=oe.lead_id "
            "where oe.id=%s and oe.lead_id=%s and oe.status='planned' for update",
            (event_id, lead_id),
        ).fetchone()
        if not event:
            raise HTTPException(status_code=409, detail="only this lead's planned events can be edited")
        conn.execute(
            "update outreach_events set scheduled_for=%s where id=%s",
            (payload.scheduled_for, event_id),
        )
        _audit(
            conn, actor, event["practice_id"], "cadence.local_schedule_updated", "outreach_event",
            str(event_id), {"lead_id": str(lead_id)},
        )
    return {"status": "updated", "scheduled_for": payload.scheduled_for}


@router.post("/review/{lead_id}/resolve")
def resolve_review(lead_id: UUID, payload: ReviewAction, actor: Actor):
    with transaction() as conn:
        lead = conn.execute(
            "select id,practice_id,needs_review from leads where id=%s for update", (lead_id,)
        ).fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail="lead not found")
        conn.execute(
            "update leads set needs_review=false,review_resolved_at=now(),review_reason=null where id=%s",
            (lead_id,),
        )
        _audit(
            conn, actor, lead["practice_id"], "review.resolved", "lead", str(lead_id),
            {"resolution": payload.resolution},
        )
    return {"status": "resolved"}


@router.get("/cadence-versions")
def list_cadence_versions(actor: Actor, lead_id: UUID | None = None):
    del actor
    with transaction() as conn:
        practice = conn.execute(
            "select id from practices where slug='rausch-pt'"
        ).fetchone()
        if not practice:
            raise HTTPException(status_code=503, detail="practice is not configured")
        if lead_id:
            lead = conn.execute(
                "select id from leads where id=%s and practice_id=%s",
                (lead_id, practice["id"]),
            ).fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail="lead not found")
            rows = conn.execute(
                "select id,practice_id,lead_id,version_number,name,status,source_version_id,"
                "activated_at,deleted_at,created_at,updated_at from cadence_versions where practice_id=%s "
                "and (lead_id is null or lead_id=%s) order by (lead_id is not null),"
                "version_number desc,id desc",
                (practice["id"], lead_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "select id,practice_id,lead_id,version_number,name,status,source_version_id,"
                "activated_at,deleted_at,created_at,updated_at from cadence_versions where practice_id=%s "
                "and lead_id is null order by version_number desc,id desc",
                (practice["id"],),
            ).fetchall()
        return {"versions": [_version_payload(conn, row) for row in rows]}


@router.post("/cadence-versions", status_code=201)
def create_cadence_version(payload: CadenceVersionCreate, actor: Actor):
    with transaction() as conn:
        practice = conn.execute(
            "select id from practices where slug='rausch-pt' for update"
        ).fetchone()
        if not practice:
            raise HTTPException(status_code=503, detail="practice is not configured")
        if payload.lead_id:
            lead = conn.execute(
                "select id from leads where id=%s and practice_id=%s for update",
                (payload.lead_id, practice["id"]),
            ).fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail="lead not found")
        if payload.source_version_id:
            source = conn.execute(
                "select id from cadence_versions where id=%s and practice_id=%s "
                "and (lead_id is null or lead_id=%s)",
                (payload.source_version_id, practice["id"], payload.lead_id),
            ).fetchone()
        elif payload.lead_id:
            source = conn.execute(
                "select id from cadence_versions where practice_id=%s and status='active' "
                "and (lead_id=%s or lead_id is null) order by (lead_id is not null) desc limit 1",
                (practice["id"], payload.lead_id),
            ).fetchone()
        else:
            source = conn.execute(
                "select id from cadence_versions where practice_id=%s and lead_id is null "
                "and status='active'",
                (practice["id"],),
            ).fetchone()
        if not source:
            raise HTTPException(status_code=409, detail="no source cadence is available")
        if payload.lead_id:
            number = conn.execute(
                "select coalesce(max(version_number),0)+1 as value from cadence_versions "
                "where lead_id=%s",
                (payload.lead_id,),
            ).fetchone()["value"]
        else:
            number = conn.execute(
                "select coalesce(max(version_number),0)+1 as value from cadence_versions "
                "where practice_id=%s and lead_id is null",
                (practice["id"],),
            ).fetchone()["value"]
        name = (payload.name or (
            f"Personalized plan v{number}" if payload.lead_id else f"Standard v{number}"
        )).strip()
        version = conn.execute(
            "insert into cadence_versions(practice_id,lead_id,version_number,name,status,"
            "source_version_id) values(%s,%s,%s,%s,'draft',%s) returning id,practice_id,lead_id,"
            "version_number,name,status,source_version_id,activated_at,deleted_at,created_at,updated_at",
            (practice["id"], payload.lead_id, number, name, source["id"]),
        ).fetchone()
        _clone_version_steps(
            conn, source["id"], version["id"], practice["id"], payload.lead_id
        )
        _audit(
            conn,
            actor,
            practice["id"],
            "cadence.version_created",
            "cadence_version",
            str(version["id"]),
            {"scope": "lead" if payload.lead_id else "global", "source_version_id": source["id"]},
        )
        return _version_payload(conn, version)


@router.put("/cadence-versions/{version_id}")
def update_cadence_version(version_id: int, payload: CadenceVersionUpdate, actor: Actor):
    _validate_cadence_steps(payload.steps)
    with transaction() as conn:
        version = conn.execute(
            "select id,practice_id,lead_id,version_number,name,status,source_version_id,"
            "activated_at,deleted_at,created_at,updated_at from cadence_versions where id=%s for update",
            (version_id,),
        ).fetchone()
        if not version:
            raise HTTPException(status_code=404, detail="cadence version not found")
        if version["status"] != "draft":
            raise HTTPException(status_code=409, detail="only draft cadence versions can be edited")
        conn.execute("delete from cadence_steps where cadence_version_id=%s", (version_id,))
        for index, step in enumerate(payload.steps):
            key = f"step_{index + 1}_{uuid4().hex[:12]}"
            saved = conn.execute(
                "insert into cadence_steps(practice_id,cadence_version_id,step_order,day_offset,"
                "channel,key,description,is_active) values(%s,%s,%s,%s,%s,%s,%s,%s) returning id",
                (
                    version["practice_id"],
                    version_id,
                    index,
                    step.day_offset,
                    step.channel,
                    key,
                    step.description.strip(),
                    step.is_active,
                ),
            ).fetchone()
            if step.channel == "sms":
                conn.execute(
                    "insert into message_templates(practice_id,cadence_version_id,cadence_step_id,"
                    "key,channel,body,is_active) values(%s,%s,%s,%s,'sms',%s,%s)",
                    (
                        version["practice_id"],
                        version_id,
                        saved["id"],
                        key,
                        (step.sms_body or "").strip(),
                        step.is_active,
                    ),
                )
        conn.execute(
            "update cadence_versions set name=%s where id=%s",
            (payload.name.strip(), version_id),
        )
        _audit(
            conn,
            actor,
            version["practice_id"],
            "cadence.version_updated",
            "cadence_version",
            str(version_id),
            {"step_count": len(payload.steps)},
        )
        updated = conn.execute(
            "select id,practice_id,lead_id,version_number,name,status,source_version_id,"
            "activated_at,deleted_at,created_at,updated_at from cadence_versions where id=%s",
            (version_id,),
        ).fetchone()
        return _version_payload(conn, updated)


@router.patch("/cadence-versions/{version_id}/name")
def rename_cadence_version(
    version_id: int, payload: CadenceVersionNameUpdate, actor: Actor
):
    with transaction() as conn:
        version = conn.execute(
            "select id,practice_id,lead_id,version_number,name,status,source_version_id,"
            "activated_at,deleted_at,created_at,updated_at from cadence_versions where id=%s for update",
            (version_id,),
        ).fetchone()
        if not version:
            raise HTTPException(status_code=404, detail="cadence version not found")
        old_name = version["name"]
        conn.execute(
            "update cadence_versions set name=%s where id=%s",
            (payload.name.strip(), version_id),
        )
        _audit(
            conn,
            actor,
            version["practice_id"],
            "cadence.version_renamed",
            "cadence_version",
            str(version_id),
            {"old_name": old_name, "new_name": payload.name.strip()},
        )
        renamed = conn.execute(
            "select id,practice_id,lead_id,version_number,name,status,source_version_id,"
            "activated_at,deleted_at,created_at,updated_at from cadence_versions where id=%s",
            (version_id,),
        ).fetchone()
        return _version_payload(conn, renamed)


@router.delete("/cadence-versions/{version_id}")
def delete_cadence_version(version_id: int, actor: Actor):
    with transaction() as conn:
        version = conn.execute(
            "select id,practice_id,lead_id,version_number,name,status,source_version_id,"
            "activated_at,deleted_at,created_at,updated_at from cadence_versions where id=%s for update",
            (version_id,),
        ).fetchone()
        if not version:
            raise HTTPException(status_code=404, detail="cadence version not found")
        if version["status"] == "active":
            raise HTTPException(
                status_code=409,
                detail="activate another cadence before deleting the active version",
            )
        if version["status"] != "deleted":
            conn.execute(
                "update cadence_versions set status='deleted',deleted_at=now() where id=%s",
                (version_id,),
            )
            _audit(
                conn,
                actor,
                version["practice_id"],
                "cadence.version_deleted",
                "cadence_version",
                str(version_id),
                {"previous_status": version["status"]},
            )
            version = conn.execute(
                "select id,practice_id,lead_id,version_number,name,status,source_version_id,"
                "activated_at,deleted_at,created_at,updated_at from cadence_versions where id=%s",
                (version_id,),
            ).fetchone()
        return _version_payload(conn, version)


@router.delete("/cadence-versions/{version_id}/permanent")
def permanently_delete_cadence_version(version_id: int, actor: Actor):
    with transaction() as conn:
        version = conn.execute(
            "select cv.id,cv.practice_id,cv.lead_id,cv.name,cv.status from cadence_versions cv "
            "join practices p on p.id=cv.practice_id where cv.id=%s and p.slug='rausch-pt' for update",
            (version_id,),
        ).fetchone()
        if not version:
            raise HTTPException(status_code=404, detail="cadence version not found")
        if version["status"] != "deleted":
            raise HTTPException(
                status_code=409,
                detail="only a deleted cadence version can be permanently deleted",
            )
        conn.execute(
            "update outreach_events set cadence_step_id=null,cadence_version_id=null "
            "where cadence_version_id=%s",
            (version_id,),
        )
        conn.execute("delete from message_templates where cadence_version_id=%s", (version_id,))
        conn.execute("delete from cadence_steps where cadence_version_id=%s", (version_id,))
        conn.execute("delete from cadence_versions where id=%s", (version_id,))
        _audit(
            conn,
            actor,
            version["practice_id"],
            "cadence.version_permanently_deleted",
            "cadence_version",
            str(version_id),
            {"name": version["name"], "scope": "lead" if version["lead_id"] else "global"},
        )
        return {"status": "permanently_deleted", "id": version_id}


@router.post("/cadence-versions/{version_id}/activate")
def activate_cadence_version(version_id: int, actor: Actor):
    with transaction() as conn:
        version = conn.execute(
            "select id,practice_id,lead_id,version_number,name,status,source_version_id,"
            "activated_at,deleted_at,created_at,updated_at from cadence_versions where id=%s for update",
            (version_id,),
        ).fetchone()
        if not version:
            raise HTTPException(status_code=404, detail="cadence version not found")
        if version["status"] == "active":
            return {**_version_payload(conn, version), "replanned_leads": 0}
        if version["status"] not in {"draft", "archived"}:
            raise HTTPException(
                status_code=409,
                detail="only a draft or previous cadence can be activated",
            )
        if version["lead_id"]:
            lead = conn.execute(
                "select id,status,cadence_state from leads where id=%s and practice_id=%s for update",
                (version["lead_id"], version["practice_id"]),
            ).fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail="lead not found")
            if lead["status"] in CLOSED_STATUSES | {"booked"}:
                raise HTTPException(status_code=409, detail="a terminal lead cannot start a local cadence")
            conn.execute(
                "update cadence_versions set status='archived' where lead_id=%s and status='active'",
                (version["lead_id"],),
            )
            leads = [lead]
        else:
            conn.execute(
                "update cadence_versions set status='archived' where practice_id=%s and lead_id is null "
                "and status='active'",
                (version["practice_id"],),
            )
            # A lead already in outreach finishes the cadence it started on.
            #
            # This used to replan every active or paused lead in the practice:
            # their remaining steps were skipped and a fresh schedule was built
            # from day 0. A patient on day 13 with one message left would be
            # called again from the beginning, and one click did it to the whole
            # caseload at once.
            #
            # Each event already carries its cadence_version_id, so leads in
            # flight keep running the version stamped on their schedule. The new
            # version applies to leads created from now on, and to any lead an
            # operator deliberately restarts from the board -- a restart
            # materializes against whichever version is active then.
            leads = conn.execute(
                "select l.id,l.status,l.cadence_state from leads l where l.practice_id=%s "
                "and l.cadence_state='pending' and l.status not in "
                "('booked','declined','transferred_human','booking_link_sent','do_not_contact',"
                "'closed_no_response','invalid_phone') and not exists (select 1 from cadence_versions cv "
                "where cv.lead_id=l.id and cv.status='active') for update",
                (version["practice_id"],),
            ).fetchall()
        conn.execute(
            "update cadence_versions set status='active',activated_at=now() where id=%s",
            (version_id,),
        )
        created = 0
        for lead in leads:
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() "
                "where lead_id=%s and status='planned'",
                (lead["id"],),
            )
            created += materialize_cadence(
                conn,
                str(lead["id"]),
                version["practice_id"],
                datetime.now(UTC).date(),
                cadence_version_id=version_id,
                update_lead=lead["cadence_state"] == "pending",
            )
        _audit(
            conn,
            actor,
            version["practice_id"],
            "cadence.version_activated",
            "cadence_version",
            str(version_id),
            {"replanned_leads": len(leads), "events_created": created},
        )
        active = conn.execute(
            "select id,practice_id,lead_id,version_number,name,status,source_version_id,"
            "activated_at,deleted_at,created_at,updated_at from cadence_versions where id=%s",
            (version_id,),
        ).fetchone()
        return {
            **_version_payload(conn, active),
            "replanned_leads": len(leads),
            "events_created": created,
        }


@router.patch("/cadence-steps/{step_id}")
def update_cadence_step(step_id: int, payload: CadenceStepUpdate, actor: Actor):
    if payload.description is None and payload.is_active is None:
        raise HTTPException(status_code=422, detail="no cadence fields supplied")
    with transaction() as conn:
        step = conn.execute(
            "select cs.id,cs.practice_id,cs.cadence_version_id,cv.lead_id,cv.status "
            "from cadence_steps cs "
            "join cadence_versions cv on cv.id=cs.cadence_version_id where cs.id=%s for update",
            (step_id,),
        ).fetchone()
        if not step:
            raise HTTPException(status_code=404, detail="cadence step not found")
        if step["status"] == "deleted" or (
            step["status"] != "draft" and (payload.description is not None or step["lead_id"] is not None)
        ):
            raise HTTPException(
                status_code=409,
                detail="published global versions allow status changes only",
            )
        if payload.is_active is False and not conn.execute(
            "select 1 from cadence_steps where cadence_version_id=%s and id<>%s and is_active limit 1",
            (step["cadence_version_id"], step_id),
        ).fetchone():
            raise HTTPException(status_code=409, detail="at least one cadence step must remain enabled")
        updated = conn.execute(
            "update cadence_steps set description=coalesce(%s,description),"
            "is_active=coalesce(%s,is_active) where id=%s returning id,description,is_active",
            (payload.description, payload.is_active, step_id),
        ).fetchone()
        if payload.is_active:
            conn.execute("update message_templates set is_active=true where cadence_step_id=%s", (step_id,))
        _audit(
            conn, actor, step["practice_id"], "cadence.global_updated", "cadence_step", str(step_id),
            {"is_active": payload.is_active} if payload.is_active is not None else None,
        )
    return {"status": "updated", **dict(updated)}


@router.post("/message-templates", status_code=201)
def create_message_template(payload: TemplateCreate, actor: Actor):
    with transaction() as conn:
        practice = conn.execute(
            "select id from practices where slug='rausch-pt' for update"
        ).fetchone()
        if not practice:
            raise HTTPException(status_code=503, detail="practice is not configured")
        name = payload.name.strip()
        duplicate = conn.execute(
            "select id from message_templates where practice_id=%s and cadence_step_id is null "
            "and cadence_version_id is null and lower(key)=lower(%s)",
            (practice["id"], name),
        ).fetchone()
        if duplicate:
            raise HTTPException(status_code=409, detail="an SMS template with this name already exists")
        template = conn.execute(
            "insert into message_templates(practice_id,cadence_step_id,cadence_version_id,key,channel,body,is_active) "
            "values(%s,null,null,%s,'sms',%s,true) returning id,practice_id,cadence_step_id,"
            "cadence_version_id,key,key as name,body,is_active",
            (practice["id"], name, payload.body.strip()),
        ).fetchone()
        _audit(
            conn,
            actor,
            practice["id"],
            "sms_template.created",
            "message_template",
            str(template["id"]),
            {"name": name},
        )
        return {**dict(template), "day_offset": None, "description": None, "deletable": True}


@router.patch("/message-templates/{template_id}")
def update_message_template(template_id: int, payload: TemplateUpdate, actor: Actor):
    if payload.name is None and payload.body is None:
        raise HTTPException(status_code=422, detail="no template fields supplied")
    with transaction() as conn:
        template = conn.execute(
            "select mt.id,mt.practice_id,mt.cadence_step_id,mt.cadence_version_id from message_templates mt "
            "join practices p on p.id=mt.practice_id where mt.id=%s and mt.channel='sms' "
            "and p.slug='rausch-pt' for update",
            (template_id,),
        ).fetchone()
        if not template:
            raise HTTPException(status_code=404, detail="SMS template not found")
        # A message tied to a cadence step is part of a published version, and
        # published versions are immutable. Editing it here changed what live
        # leads receive without creating a new version or leaving any trace in
        # Cadence Studio. Delete already refused this; update did not.
        if template["cadence_step_id"] is not None:
            raise HTTPException(
                status_code=409,
                detail="cadence messages are edited in a cadence draft, not in Template Studio",
            )
        name = payload.name.strip() if payload.name is not None else None
        if name:
            duplicate = conn.execute(
                "select id from message_templates where practice_id=%s and id<>%s "
                "and cadence_version_id is not distinct from %s and lower(key)=lower(%s)",
                (template["practice_id"], template_id, template["cadence_version_id"], name),
            ).fetchone()
            if duplicate:
                raise HTTPException(status_code=409, detail="an SMS template with this name already exists")
        conn.execute(
            "update message_templates set key=coalesce(%s,key),body=coalesce(%s,body) where id=%s",
            (name, payload.body.strip() if payload.body is not None else None, template_id),
        )
        _audit(
            conn, actor, template["practice_id"], "sms_template.global_updated",
            "message_template", str(template_id), {"renamed": name is not None},
        )
        updated = conn.execute(
            "select mt.id,mt.practice_id,mt.cadence_step_id,mt.cadence_version_id,mt.key,"
            "mt.key as name,mt.body,mt.is_active,cs.day_offset,cs.description "
            "from message_templates mt left join cadence_steps cs on cs.id=mt.cadence_step_id "
            "where mt.id=%s",
            (template_id,),
        ).fetchone()
        return {**dict(updated), "deletable": updated["cadence_step_id"] is None}


@router.delete("/message-templates/{template_id}")
def delete_message_template(template_id: int, actor: Actor):
    with transaction() as conn:
        template = conn.execute(
            "select mt.id,mt.practice_id,mt.cadence_step_id,mt.key from message_templates mt "
            "join practices p on p.id=mt.practice_id where mt.id=%s and mt.channel='sms' "
            "and p.slug='rausch-pt' for update",
            (template_id,),
        ).fetchone()
        if not template:
            raise HTTPException(status_code=404, detail="SMS template not found")
        if template["cadence_step_id"] is not None:
            raise HTTPException(
                status_code=409,
                detail="cadence messages must be removed from a cadence draft",
            )
        conn.execute("delete from message_templates where id=%s", (template_id,))
        _audit(
            conn,
            actor,
            template["practice_id"],
            "sms_template.permanently_deleted",
            "message_template",
            str(template_id),
            {"name": template["key"]},
        )
        return {"status": "permanently_deleted", "id": template_id}


@router.put("/leads/{lead_id}/message-overrides/{template_id}")
def set_message_override(
    lead_id: UUID, template_id: int, payload: TemplateUpdate, actor: Actor
):
    with transaction() as conn:
        lead = conn.execute("select practice_id from leads where id=%s", (lead_id,)).fetchone()
        template = conn.execute(
            "select id from message_templates where id=%s and channel='sms'", (template_id,)
        ).fetchone()
        if not lead or not template:
            raise HTTPException(status_code=404, detail="lead or SMS template not found")
        conn.execute(
            "insert into lead_message_overrides(lead_id,message_template_id,body,updated_by) "
            "values(%s,%s,%s,%s) on conflict(lead_id,message_template_id) do update set "
            "body=excluded.body,updated_by=excluded.updated_by",
            (lead_id, template_id, payload.body, actor.user_id),
        )
        _audit(
            conn, actor, lead["practice_id"], "sms_template.local_updated",
            "lead", str(lead_id), {"template_id": template_id},
        )
    return {"status": "updated"}


@router.delete("/leads/{lead_id}/message-overrides/{template_id}")
def remove_message_override(lead_id: UUID, template_id: int, actor: Actor):
    with transaction() as conn:
        lead = conn.execute("select practice_id from leads where id=%s", (lead_id,)).fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail="lead not found")
        conn.execute(
            "delete from lead_message_overrides where lead_id=%s and message_template_id=%s",
            (lead_id, template_id),
        )
        _audit(
            conn, actor, lead["practice_id"], "sms_template.local_reset",
            "lead", str(lead_id), {"template_id": template_id},
        )
    return {"status": "reset"}


@router.post("/leads/{lead_id}/sms")
def send_manual_sms(
    request: Request, lead_id: UUID, payload: ManualSmsRequest, actor: Actor
):
    trace = WorkflowTrace("dashboard_manual_sms", "api", request.headers.get("x-trace-id", ""))
    with transaction() as conn:
        lead = conn.execute(
            "select id,practice_id,phone_e164,status,sms_opt_out from leads where id=%s for update",
            (lead_id,),
        ).fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail="lead not found")
        blocked = (
            not lead["phone_e164"]
            or lead["sms_opt_out"]
            or lead["status"] == "do_not_contact"
            or conn.execute(
                "select 1 from suppressed_numbers where phone_e164=%s", (lead["phone_e164"],)
            ).fetchone()
        )
        if blocked:
            raise HTTPException(status_code=409, detail="SMS is blocked by contact rules")
        inserted = conn.execute(
            "insert into dashboard_sms_requests(lead_id,idempotency_key,body,status,requested_by) "
            "values(%s,%s,%s,'sending',%s) on conflict(idempotency_key) do nothing returning id",
            (lead_id, payload.idempotency_key, payload.body, actor.user_id),
        ).fetchone()
        if not inserted:
            existing = conn.execute(
                "select status,provider_ref from dashboard_sms_requests where idempotency_key=%s",
                (payload.idempotency_key,),
            ).fetchone()
            return {"status": existing["status"], "provider_ref": existing["provider_ref"], "duplicate": True}
        request_id = inserted["id"]

    try:
        provider_ref = TwilioService().send_sms(trace, lead["phone_e164"], payload.body)
    except ProviderError as exc:
        status = "unknown" if exc.ambiguous else "failed"
        with transaction() as conn:
            conn.execute(
                "update dashboard_sms_requests set status=%s,failure_category=%s where id=%s",
                (status, exc.code, request_id),
            )
            if status == "unknown":
                conn.execute(
                    "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() where id=%s",
                    ("manual SMS result requires provider reconciliation", lead_id),
                )
            _audit(
                conn, actor, lead["practice_id"], f"sms.manual_{status}", "lead", str(lead_id),
                {"request_id": str(request_id)},
            )
        trace.complete(outcome=status)
        raise HTTPException(
            status_code=502, detail="SMS result is unknown and requires review" if status == "unknown" else "SMS was rejected"
        ) from exc

    with transaction() as conn:
        conn.execute(
            "update dashboard_sms_requests set status='sent',provider_ref=%s where id=%s",
            (provider_ref, request_id),
        )
        conn.execute(
            "insert into sms_messages(lead_id,direction,body,occurred_at,delivery_status,provider_message_id) "
            "values(%s,'outbound',%s,now(),'sent',%s)",
            (lead_id, payload.body, provider_ref),
        )
        _audit(
            conn, actor, lead["practice_id"], "sms.manual_sent", "lead", str(lead_id),
            {"request_id": str(request_id)},
        )
    trace.complete(outcome="sent")
    return {"status": "sent", "provider_ref": provider_ref, "duplicate": False}
