"""Staff calendar over booking records and the Stride import mirror."""

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from ..db import transaction
from ..observability import WorkflowTrace
from ..services import slot_booking
from ..services.stride_import import CANCELLED_STATUSES, LATE_CANCEL_STATUSES, NO_SHOW_STATUSES
from .dashboard import Actor, _audit

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])
MAX_RECORDS = 2000


def calendar_window(start: date, end: date, timezone: str):
    if end < start or (end - start).days > 62:
        raise HTTPException(400, "Choose a calendar range of at most 63 days.")
    zone = ZoneInfo(timezone)
    return (
        datetime.combine(start, time.min, zone).astimezone(UTC),
        datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(UTC),
    )


def calendar_record(row, *, imported=False):
    start, end = row["start_utc"], row.get("end_utc")
    duration = (end - start).total_seconds() / 60 if start and end else None
    state = str(row.get("state") or "scheduled").strip().lower()
    if imported:
        state = ("late_cancel" if state in LATE_CANCEL_STATUSES else
                 "cancelled" if state in CANCELLED_STATUSES else
                 "no_show" if state in NO_SHOW_STATUSES else
                 "scheduled" if state in {"o", "open"} else state)
    provider_key = row["id"] if imported else row.get("stride_appointment_id")
    return {
        "id": f"stride:{provider_key}" if provider_key is not None else f"booking:{row['id']}",
        "lead_id": row.get("lead_id"),
        "full_name": row.get("full_name") or "Patient name unavailable",
        "start_utc": start,
        "end_utc": end,
        "duration_minutes": duration if duration and duration > 0 else None,
        "practitioner_id": str(row.get("clinician_id") or "unassigned"),
        "practitioner_name": row.get("clinician_name") or "Unassigned practitioner",
        "location": row.get("location") or "Location unavailable",
        "case_title": row.get("case_title") or "Case unavailable",
        "type": row.get("type") or "Appointment",
        "state": state,
        "needs_staff_review": bool(row.get("needs_staff_review")),
        "source": "Stride" if imported else "Booking service",
        "lead_source": row.get("lead_source"),
        "stride_appointment_id": row.get("stride_appointment_id"),
    }


@router.get("/appointment-calendar")
def appointment_calendar(actor: Actor, start: date, end: date, lead_id: UUID | None = None):
    with transaction() as conn:
        settings = conn.execute(
            "select stride_location_timezone,stride_booking_enabled,business_hours "
            "from practice_settings where practice_id=%s", (actor.practice_id,),
        ).fetchone() or {}
        timezone = settings.get("stride_location_timezone") or "America/Los_Angeles"
        lower, upper = calendar_window(start, end, timezone)
        tables = conn.execute(
            "select to_regclass('public.stride_appointments') is not null as imports,"
            "to_regclass('public.clinician_assignments') is not null as booking"
        ).fetchone()
        mirror_filter = (
            "and not exists(select 1 from stride_appointments sa where sa.practice_id=a.practice_id "
            "and sa.stride_id=a.stride_appointment_id) " if tables["imports"] else ""
        )
        native = conn.execute(
            "select a.id,a.lead_id,l.full_name,a.start_utc,a.end_utc,a.clinician_id,"
            "a.location_id,l.location,l.lead_type as case_title,l.source_system as lead_source,"
            "a.state,a.needs_staff_review,a.stride_appointment_id "
            "from appointments a join leads l on l.id=a.lead_id "
            "where a.practice_id=%s and a.start_utc>=%s and a.start_utc<%s "
            "and (%s::uuid is null or a.lead_id=%s) and a.state<>'failed' "
            + mirror_filter + "order by a.start_utc,a.id limit %s",
            (actor.practice_id, lower, upper, lead_id, lead_id, MAX_RECORDS + 1),
        ).fetchall()
        imported = []
        if tables["imports"]:
            imported = conn.execute(
                "select sa.stride_id as id,sp.lead_id,"
                "coalesce(l.full_name,nullif(trim(concat_ws(' ',sp.first_name,sp.last_name)),'')) "
                "as full_name,sa.start_utc,sa.end_utc,sa.clinician_id,sa.clinician_name,"
                "sa.location_id,sa.location_name as location,coalesce(sc.title,l.lead_type) as case_title,"
                "sa.appointment_type as type,"
                "sa.status as state,l.source_system as lead_source,sa.stride_id as stride_appointment_id "
                "from stride_appointments sa "
                "left join stride_patients sp on sp.practice_id=sa.practice_id "
                "and sp.stride_id=sa.patient_stride_id and sp.deleted_at is null "
                "left join stride_cases sc on sc.practice_id=sa.practice_id "
                "and sc.stride_id=sa.case_stride_id and sc.deleted_at is null "
                "left join leads l on l.id=sp.lead_id and l.practice_id=sa.practice_id "
                "where sa.practice_id=%s and sa.deleted_at is null "
                "and sa.start_utc>=%s and sa.start_utc<%s "
                "and (%s::uuid is null or l.id=%s) "
                "order by sa.start_utc,sa.stride_id limit %s",
                (actor.practice_id, lower, upper, lead_id, lead_id, MAX_RECORDS + 1),
            ).fetchall()
        practitioners = []
        if tables["booking"]:
            practitioners = conn.execute(
                "select distinct c.stride_user_id as id,c.display_name as name,bl.name as location,"
                "bl.stride_location_id "
                "from clinicians c join clinician_assignments ca on ca.clinician_id=c.id "
                "join booking_locations bl on bl.id=ca.booking_location_id "
                "where c.practice_id=%s and bl.practice_id=%s "
                "and c.is_active and ca.is_active and bl.is_active order by c.display_name,bl.name",
                (actor.practice_id, actor.practice_id),
            ).fetchall()
        names = {str(p["id"]): p["name"] for p in practitioners}
        location_names = {p["stride_location_id"]: p["location"] for p in practitioners}
        for row in native + imported:
            row["location"] = location_names.get(row.get("location_id")) or row.get("location")
        for row in native:
            row["clinician_name"] = names.get(str(row["clinician_id"]))
        # The import is authoritative once it has seen a booking, including cancellations.
        imported_ids = {r["stride_appointment_id"] for r in imported}
        records = [calendar_record(r, imported=True) for r in imported]
        records += [calendar_record(r) for r in native
                    if r["stride_appointment_id"] not in imported_ids]
        records.sort(key=lambda r: (r["start_utc"], r["id"]))
        truncated = len(records) > MAX_RECORDS
        records = records[:MAX_RECORDS]
        resources = {(str(p["id"]), p["location"]): {
            "id": str(p["id"]), "name": p["name"], "location": p["location"],
        } for p in practitioners}
        for r in records:
            resources.setdefault((r["practitioner_id"], r["location"]), {
                "id": r["practitioner_id"], "name": r["practitioner_name"],
                "location": r["location"],
            })
        last_import = None
        if tables["imports"]:
            last_import = conn.execute(
                "select max(processed_at) as at from stride_import_files "
                "where practice_id=%s and status='processed'", (actor.practice_id,),
            ).fetchone()["at"]
    return {
        "appointments": records, "practitioners": list(resources.values()),
        "timezone": timezone, "business_hours": settings.get("business_hours") or {},
        "booking_enabled": bool(settings.get("stride_booking_enabled") and tables["booking"]),
        "stride_import_enabled": tables["imports"], "last_import_at": last_import,
        "truncated": truncated, "generated_at": datetime.now(UTC),
    }


def _staff_lead(conn, actor, lead_id):
    lead = conn.execute(
        "select id,first_name,last_name,date_of_birth,email,status,call_opt_out,sms_opt_out,"
        "needs_review,phone_e164 from leads where id=%s and practice_id=%s",
        (lead_id, actor.practice_id),
    ).fetchone()
    if not lead:
        raise HTTPException(404, "Lead not found.")
    return lead


@router.get("/appointment-slots")
def appointment_slots(
    actor: Actor, lead_id: UUID, start: date, days: int = Query(7, ge=1, le=14),
    practitioner_id: int | None = Query(None, ge=1),
):
    with transaction() as conn:
        _staff_lead(conn, actor, lead_id)
        try:
            context = slot_booking.load_context(conn, str(lead_id))
        except (ValueError, slot_booking.BookingToolError):
            raise HTTPException(409, "This lead's location and case need booking configuration.") from None
        if not context.booking_enabled:
            raise HTTPException(409, "New bookings are currently switched off for this practice.")
        clinicians = [c["id"] for c in context.clinicians
                      if practitioner_id is None or c["stride_user_id"] == practitioner_id]
        if not clinicians:
            raise HTTPException(409, "This practitioner is not assigned to the lead's location and case.")
        slots = slot_booking.search_slots(
            conn, context, start=start, end=start + timedelta(days=days - 1),
            clinician_ids=clinicians, limit=60,
        )
    return {
        "slots": slots, "location": context.location["name"],
        "case_title": context.case_type["name"], "timezone": context.timezone,
        "duration_minutes": context.case_type["duration_minutes"],
        "cache_fresh": slot_booking.cache_is_fresh(context),
    }


class CalendarBooking(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lead_id: UUID
    slot_id: int = Field(ge=1)


@router.post("/appointments", status_code=201)
def create_calendar_appointment(payload: CalendarBooking, actor: Actor):
    with transaction() as conn:
        lead = _staff_lead(conn, actor, payload.lead_id)
        blocked = conn.execute(
            "select 1 from suppressed_numbers where phone_e164=%s", (lead["phone_e164"],),
        ).fetchone()
        if (blocked or lead["status"] == "do_not_contact"
                or (lead["call_opt_out"] and lead["sms_opt_out"]) or lead["needs_review"]):
            raise HTTPException(409, "Resolve the lead's contact restrictions or review before booking.")
        if lead["status"] == "booked":
            # The booking engine refuses too (it also sees Stride visits); answer early and plainly.
            raise HTTPException(409, _STAFF_MESSAGES["ALREADY_BOOKED"])
        if not lead["first_name"] or not lead["last_name"] or not lead["date_of_birth"]:
            raise HTTPException(400, "The lead needs a first name, last name and date of birth to book.")
        slot = conn.execute(
            "select s.local_date,s.local_time,s.timezone,c.display_name from availability_slots s "
            "join clinicians c on c.id=s.clinician_id where s.id=%s and s.practice_id=%s "
            "and s.status='open'", (payload.slot_id, actor.practice_id),
        ).fetchone()
        if not slot:
            raise HTTPException(409, "That appointment time is no longer available.")
        _audit(conn, actor, actor.practice_id, "appointment.booking_requested", "lead",
               str(payload.lead_id), {"slot_id": payload.slot_id})
    try:
        result = slot_booking.book_slot(
            WorkflowTrace("dashboard_booking", "api"), lead_id=str(payload.lead_id), call_id=None,
            source="staff",
            arguments={
                "date": slot["local_date"].isoformat(), "time": slot["local_time"].isoformat(),
                "clinician_name": slot["display_name"], "first_name": lead["first_name"],
                "last_name": lead["last_name"], "date_of_birth": lead["date_of_birth"].isoformat(),
                "email": lead["email"], "slot_id": payload.slot_id,
            },
        )
    except ValueError as exc:
        # Validation of the lead's own details (name, birthday, email): tell staff what to fix.
        _booking_outcome(actor, payload, "rejected", str(exc).split(":", 1)[0])
        raise HTTPException(400, _staff_message(str(exc))) from None
    except slot_booking.BookingToolError:
        _booking_outcome(actor, payload, "rejected", "BOOKING_UNAVAILABLE")
        raise HTTPException(409, "New bookings are currently switched off for this practice.") from None
    code = result.split(":", 1)[0]
    _booking_outcome(actor, payload, "booked" if code == "BOOKED" else "refused", code)
    if code != "BOOKED":
        raise HTTPException(409, _staff_message(result))
    with transaction() as conn:
        saved = conn.execute(
            "select id,state,start_utc,stride_appointment_id from appointments "
            "where lead_id=%s and practice_id=%s "
            "and state in ('scheduled','booking','unknown') order by id desc limit 1",
            (payload.lead_id, actor.practice_id),
        ).fetchone()
    return {
        "status": "scheduled" if saved and saved["state"] == "scheduled" else "confirmed_sync_pending",
        "id": calendar_record(saved)["id"] if saved else None,
        "lead_id": payload.lead_id,
        "start_day": (saved["start_utc"].astimezone(ZoneInfo(slot["timezone"])).date()
                      if saved and saved["start_utc"] else slot["local_date"]),
    }


# What staff see when a booking does not go through. The booking engine's replies are written for
# the voice agent ("tell the caller ..."), so the dashboard needs its own words.
_STAFF_MESSAGES = {
    "ALREADY_BOOKED": "This patient is already booked, so no new appointment was made.",
    "IN_PROGRESS": "A booking for this patient is already being made. Wait a minute and refresh.",
    "UNCONFIRMED": "Stride did not answer clearly. The lead is flagged; check Stride before booking again.",
    "PATIENT_EXISTS": "This patient already exists in Stride, so book them in Stride directly. "
                      "The lead is flagged for review.",
    "TAKEN": "That time was just taken in Stride. Pick another time.",
    "NOT_AVAILABLE": "That time is no longer open. Pick another time.",
    "FOLLOW_UP": "The booking could not be finished in time. Check Stride before trying again.",
    "BAD_SLOT": "That appointment time is not valid. Refresh and pick a time again.",
    "MISSING_NAME": "The lead needs a first and last name to book.",
    "BAD_NAME": "The lead's name is too long for Stride. Shorten it on the lead first.",
    "BAD_DATE_OF_BIRTH": "The lead's date of birth is not valid. Fix it on the lead first.",
    "BAD_EMAIL": "The lead's email address is not valid. Fix or remove it on the lead first.",
}


def _staff_message(result: str) -> str:
    code = result.split(":", 1)[0].strip()
    return _STAFF_MESSAGES.get(code, "That time could not be booked. Check availability or ask staff to review.")


def _booking_outcome(actor, payload: "CalendarBooking", outcome: str, code: str) -> None:
    """Audit what happened, not only that a booking was asked for (no patient data)."""
    with transaction() as conn:
        _audit(conn, actor, actor.practice_id, f"appointment.booking_{outcome}", "lead",
               str(payload.lead_id), {"slot_id": payload.slot_id, "result": code})

