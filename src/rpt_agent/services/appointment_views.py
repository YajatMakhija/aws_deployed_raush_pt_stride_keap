"""Read-only views of every booking, ours and Stride's, for the staff dashboard.

One record shape feeds the calendar, the lead page booking card, the home page and
the board, so each place tags and words a booking the same way:

- ``elliot``: our voice agent booked it on a call (``appointments.booking_source='voice_agent'``).
- ``sheet_lead``: one of our leads, booked another way (front desk in Stride, the
  text link, or staff from the calendar).
- ``stride_only``: a Stride patient who never was one of our leads.
"""

import re
from datetime import datetime

from .stride_import import CANCELLED_STATUSES, LATE_CANCEL_STATUSES, NO_SHOW_STATUSES

TAG_ELLIOT, TAG_SHEET_LEAD, TAG_STRIDE_ONLY = "elliot", "sheet_lead", "stride_only"
# Stride words (and API letters) for a visit that is still on the books, and for one
# the patient attended. Unmarked is Stride's default before check-in.
BOOKED_STATUSES = {"o", "open", "u", "unmarked", "confirmed"}
COMPLETED_STATUSES = {"checked in", "c"}
ACTIVE_STATES = {"booking", "scheduled", "unknown"}
# Every booking our engine makes is a new patient's first visit.
NATIVE_VISIT_TYPE = "Initial Evaluation"
_CONFIRMATION = re.compile(r"\b(booked|all set|you're set|you are set|confirmed|scheduled you)\b", re.IGNORECASE)
_SPEAKER = re.compile(r"^\s*(ai|assistant|bot|agent|user|customer|patient)\s*:\s*(.+)$", re.IGNORECASE)


def normalise_state(raw, *, imported: bool) -> str:
    state = str(raw or "scheduled").strip().lower()
    if not imported:
        return state
    if state in LATE_CANCEL_STATUSES:
        return "late_cancel"
    if state in CANCELLED_STATUSES:
        return "cancelled"
    if state in NO_SHOW_STATUSES:
        return "no_show"
    if state in COMPLETED_STATUSES:
        return "completed"
    if state in BOOKED_STATUSES:
        return "scheduled"
    return state


def booking_tag(row) -> str:
    if row.get("booking_source") == "voice_agent":
        return TAG_ELLIOT
    return TAG_SHEET_LEAD if row.get("lead_id") else TAG_STRIDE_ONLY


def booked_by(row, *, imported: bool) -> str:
    """Who put the visit on the books: voice_agent, staff, or stride (front desk / link)."""
    source = row.get("booking_source")
    if source == "voice_agent":
        return "voice_agent"
    if source == "manual":
        return "staff"
    return "stride" if imported else "booking_service"


def booking_quote(transcript: str | None) -> str | None:
    """What the patient said just before Elliot confirmed the visit.

    Vapi's stored report has no per-message timings, so the confirmation is found by
    wording: the first assistant line that says the visit is booked, and the last
    patient line before it that says something (not just "yes").
    """
    turns = []
    for line in (transcript or "").splitlines():
        match = _SPEAKER.match(line)
        if match:
            role = "patient" if match[1].lower() in {"user", "customer", "patient"} else "agent"
            turns.append((role, match[2].strip()))
    for index, (role, text) in enumerate(turns):
        if role == "agent" and _CONFIRMATION.search(text):
            for earlier_role, earlier in reversed(turns[:index]):
                if earlier_role == "patient" and len(earlier.split()) >= 4:
                    return earlier if len(earlier) <= 240 else earlier[:237].rstrip() + "…"
            return None
    return None


def record(row, *, imported: bool) -> dict:
    start, end = row["start_utc"], row.get("end_utc")
    duration = (end - start).total_seconds() / 60 if start and end else None
    provider_key = row["id"] if imported else row.get("stride_appointment_id")
    offset = row.get("call_offset_seconds")
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
        "type": row.get("type") or ("Appointment" if imported else NATIVE_VISIT_TYPE),
        "state": normalise_state(row.get("state"), imported=imported),
        "needs_staff_review": bool(row.get("needs_staff_review")),
        "source": "Stride" if imported else "Booking service",
        "lead_source": row.get("lead_source"),
        "stride_appointment_id": row.get("stride_appointment_id"),
        "tag": booking_tag(row),
        "booked_by": booked_by(row, imported=imported),
        "booked_at": row.get("booked_at"),
        "call_log_id": row.get("call_log_id"),
        "call_offset_seconds": int(offset) if offset is not None and offset >= 0 else None,
    }


# Our booking row, when we made the visit, and the call it was made on.
_OURS = (
    "a.booking_source,a.created_at as hold_at,cl.id as call_log_id,"
    "extract(epoch from a.created_at-cl.dialed_at) as call_offset_seconds"
)


def load_records(conn, practice_id: int, *, lower: datetime | None = None,
                 upper: datetime | None = None, lead_ids: list | None = None,
                 limit: int = 2000) -> tuple[list[dict], bool]:
    """Every booking in [lower, upper) (open ends allowed), optionally for some leads.

    Returns (records sorted by start, truncated). Stride's copy of a visit wins over
    ours once imported, because Stride is the record (cancellations included).
    """
    lead_filter = [str(i) for i in lead_ids] if lead_ids is not None else None
    if lead_filter == []:
        return [], False
    tables = optional_tables(conn)
    window = (lower, lower, upper, upper)
    # Once Stride has the visit, its copy (even a removed one) is the truth, not ours.
    mirror_filter = (
        "and not exists(select 1 from stride_appointments sa where sa.practice_id=a.practice_id "
        "and sa.stride_id=a.stride_appointment_id) "
        if tables["imports"] else ""
    )
    native = conn.execute(
        "select a.id,a.lead_id,l.full_name,a.start_utc,a.end_utc,a.clinician_id,"
        "a.location_id,l.location,coalesce(ct.name,l.lead_type) as case_title,"
        "l.source_system as lead_source,a.state,a.needs_staff_review,a.stride_appointment_id,"
        "coalesce(a.confirmed_at,a.booked_at,a.created_at) as booked_at," + _OURS + " "
        "from appointments a join leads l on l.id=a.lead_id "
        "left join case_types ct on ct.id=a.case_type_id "
        "left join call_logs cl on a.call_id is not null and cl.vapi_call_id=a.call_id "
        "where a.practice_id=%s and (%s::timestamptz is null or a.start_utc>=%s) "
        "and (%s::timestamptz is null or a.start_utc<%s) "
        "and (%s::uuid[] is null or a.lead_id=any(%s::uuid[])) and a.state<>'failed' "
        + mirror_filter + "order by a.start_utc,a.id limit %s",
        (practice_id, *window, lead_filter, lead_filter, limit + 1),
    ).fetchall()
    imported = []
    if tables["imports"]:
        imported = conn.execute(
            "select sa.stride_id as id,sp.lead_id,"
            "coalesce(l.full_name,nullif(trim(concat_ws(' ',sp.first_name,sp.last_name)),'')) "
            "as full_name,sa.start_utc,sa.end_utc,sa.clinician_id,sa.clinician_name,"
            "sa.location_id,sa.location_name as location,coalesce(sc.title,l.lead_type) as case_title,"
            "sa.appointment_type as type,sa.status as state,l.source_system as lead_source,"
            "sa.stride_id as stride_appointment_id,"
            "coalesce(a.confirmed_at,a.booked_at,sa.stride_created_at,sa.created_at) as booked_at,"
            + _OURS + " "
            "from stride_appointments sa "
            "left join stride_patients sp on sp.practice_id=sa.practice_id "
            "and sp.stride_id=sa.patient_stride_id and sp.deleted_at is null "
            "left join stride_cases sc on sc.practice_id=sa.practice_id "
            "and sc.stride_id=sa.case_stride_id and sc.deleted_at is null "
            "left join leads l on l.id=sp.lead_id and l.practice_id=sa.practice_id "
            "left join appointments a on a.practice_id=sa.practice_id "
            "and a.stride_appointment_id=sa.stride_id "
            "left join call_logs cl on a.call_id is not null and cl.vapi_call_id=a.call_id "
            "where sa.practice_id=%s and sa.deleted_at is null "
            "and (%s::timestamptz is null or sa.start_utc>=%s) "
            "and (%s::timestamptz is null or sa.start_utc<%s) "
            "and (%s::uuid[] is null or l.id=any(%s::uuid[])) "
            "order by sa.start_utc,sa.stride_id limit %s",
            (practice_id, *window, lead_filter, lead_filter, limit + 1),
        ).fetchall()
    names, location_names = practitioner_names(conn, practice_id, tables["booking"])
    for row in native + imported:
        row["location"] = clinic_name(row.get("location_id"), row.get("location"), location_names)
    for row in native:
        row["clinician_name"] = names.get(str(row["clinician_id"]))
    imported_ids = {r["stride_appointment_id"] for r in imported}
    records = [record(r, imported=True) for r in imported]
    records += [record(r, imported=False) for r in native
                if r["stride_appointment_id"] not in imported_ids]
    records.sort(key=lambda r: (r["start_utc"], r["id"]))
    return records[:limit], len(records) > limit


_TABLES: dict = {}


def optional_tables(conn) -> dict:
    """Which optional tables exist. Once both do they always will, so ask the database once."""
    if _TABLES.get("imports") and _TABLES.get("booking"):
        return _TABLES
    found = conn.execute(
        "select to_regclass('public.stride_appointments') is not null as imports,"
        "to_regclass('public.clinician_assignments') is not null as booking"
    ).fetchone()
    _TABLES.update(found)
    return dict(found)


def clinic_name(location_id, raw: str | None, by_id: dict) -> str | None:
    """Our clinic name for a visit, so the dashboard's location filter matches every source.

    Stride ids map first. Otherwise Stride's own name is used, shortened to our clinic name
    when it contains one ("Rausch PT – Laguna Niguel" -> "Laguna Niguel").
    """
    if location_id in by_id:
        return by_id[location_id]
    if raw:
        for name in sorted(set(by_id.values()), key=len, reverse=True):
            if name and name.lower() in raw.lower():
                return name
    return raw


def practitioner_names(conn, practice_id: int, booking_tables: bool = True):
    """Clinician names by Stride user id and clinic names by Stride location id."""
    if not booking_tables:
        return {}, {}
    rows = conn.execute(
        "select c.stride_user_id as id,c.display_name as name,bl.name as location,"
        "bl.stride_location_id from clinicians c "
        "left join clinician_assignments ca on ca.clinician_id=c.id and ca.is_active "
        "left join booking_locations bl on bl.id=ca.booking_location_id and bl.is_active "
        "where c.practice_id=%s",
        (practice_id,),
    ).fetchall()
    return (
        {str(r["id"]): r["name"] for r in rows},
        {r["stride_location_id"]: r["location"] for r in rows if r["stride_location_id"] is not None},
    )


def status_bucket(state: str) -> str:
    if state in ACTIVE_STATES:
        return "booked"
    if state in {"cancelled", "late_cancel"}:
        return "cancelled"
    return state  # completed, no_show, rescheduled, or a Stride word we do not know


def pick_booking(records: list[dict], now: datetime) -> dict | None:
    """The booking a lead's page should lead with: the next visit still on the books,
    else the latest visit whatever happened to it (cancelled, completed, no show)."""
    upcoming = [r for r in records if status_bucket(r["state"]) == "booked" and r["start_utc"] >= now]
    if upcoming:
        return min(upcoming, key=lambda r: r["start_utc"])
    return max(records, key=lambda r: r["start_utc"]) if records else None


def lead_bookings(conn, practice_id: int, lead_ids: list, now: datetime) -> dict[str, dict]:
    """The booking to show for each lead, keyed by lead id (leads without one are absent)."""
    records, _ = load_records(conn, practice_id, lead_ids=lead_ids, limit=5000)
    by_lead: dict[str, list[dict]] = {}
    for r in records:
        if r["lead_id"]:
            by_lead.setdefault(str(r["lead_id"]), []).append(r)
    return {lead: pick_booking(items, now) for lead, items in by_lead.items()}


def booking_card(conn, practice_id: int, lead_id, now: datetime) -> dict | None:
    """The lead page booking card: the chosen visit plus, for Elliot, the call moment."""
    chosen = lead_bookings(conn, practice_id, [lead_id], now).get(str(lead_id))
    if not chosen:
        return None
    card = {**chosen, "quote": None}
    if chosen["call_log_id"]:
        call = conn.execute(
            "select coalesce(ct.transcript_text,cl.transcript_text) as transcript_text "
            "from call_logs cl left join call_transcripts ct on ct.call_log_id=cl.id where cl.id=%s",
            (chosen["call_log_id"],),
        ).fetchone()
        card["quote"] = booking_quote(call and call["transcript_text"])
    return card
