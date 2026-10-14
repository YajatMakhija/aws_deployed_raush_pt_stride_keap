"""Find and book appointment slots for the voice agent.

find_slots answers "when can I come in?" from the local slot cache: the lead's
location and case type pick the clinicians (the client's assignment list), the
caller's wish picks the date range, and the three nearest open times are
offered and remembered for the call.

book_slot books one of those times. It follows the usual pattern for booking
against a calendar we do not own:

1. Hold the slot locally (one active hold per slot, enforced by a unique index),
   so two calls can never book the same time.
2. Re-check that one slot live in Stride; the cache only decides what to offer.
3. Create patient, case, appointment, saving each Stride id as soon as it
   exists, so a retry resumes instead of duplicating.
4. Stride's own overlap check (is_pending=true) is the final guard.
5. An unclear result (a timeout after Stride may have saved) is never retried
   blindly: the booking is marked unknown and staff are asked to confirm.

Every reply is one plain line for the voice model. A live call cannot wait, so
Stride is called with a short timeout and no retries, and the whole tool stops
before booking_tool_deadline_seconds.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from ..config import Settings, get_settings
from ..db import transaction
from ..observability import WorkflowTrace
from ..providers import ProviderClients, ProviderError
from .availability_sync import booking_now, sync_target, sync_targets
from .lead_status import mark_booked
from .review import flag_lead_for_review
from .sheet_sync import enqueue_sheet_update

WHEN_VALUES = (
    "earliest", "today", "tomorrow", "this_week", "next_week", "in_two_weeks", "specific_date",
)
TIME_OF_DAY = {
    "morning": (clock_time(0, 0), clock_time(12, 0)),
    "afternoon": (clock_time(12, 0), clock_time(17, 0)),
    "evening": (clock_time(17, 0), clock_time(23, 59, 59)),
}
OFFER_COUNT = 3
# A booking may use any slot this call was offered within this window.
OFFER_MEMORY = timedelta(minutes=60)
NAME_PREFIXES = {"dr", "doctor", "mr", "mrs", "ms", "miss"}


class BookingToolError(Exception):
    """A failure the caller cannot fix; sent to Vapi as the tool's error."""


@dataclass
class BookingContext:
    lead: dict[str, Any]
    location: dict[str, Any]
    case_type: dict[str, Any]
    clinicians: list[dict[str, Any]]
    now_local: datetime
    settings: Settings = field(repr=False)
    sync_state: dict[str, Any] | None = None
    booking_enabled: bool = False

    @property
    def timezone(self) -> str:
        return self.location["timezone"]


# ---------------------------------------------------------------------------
# Context: lead -> location -> case type -> clinicians
# ---------------------------------------------------------------------------

# One round trip loads everything a tool needs: the database is ~170 ms away
# from the API, so separate queries add up to seconds on a live call.
CONTEXT_SQL = """
with lead as (
  select id,practice_id,status,location,lead_type,email,phone_e164,stride_patient_id,
         stride_case_id,is_test
  from leads where id=%(lead_id)s
), loc as (
  select bl.id,bl.name,bl.stride_location_id,bl.timezone from booking_locations bl, lead
  where bl.practice_id=lead.practice_id and bl.is_active
    and lower(bl.name)=lower(coalesce(nullif(%(location)s,''),lead.location,''))
), wanted as (
  select lower(trim(coalesce(nullif(%(case_type)s,''),lead.lead_type,''))) as name,
         %(case_type)s<>'' as spoken from lead
), kind as (
  -- The service said on the call wins, else the lead's case from the Sheet. Each matches the name
  -- or one of its aliases ("Ortho", "pelvic floor"), else a unique partial match ("pelvic"); only
  -- the Sheet's case may fall back to the practice default (seen on demo 2026-10-14: "Pelvic"
  -- in the Sheet used to get the default case).
  select id,name,stride_appointment_type_id,duration_minutes from (
    select ct.id,ct.name,ct.stride_appointment_type_id,ct.duration_minutes,
      case when lower(ct.name)=wanted.name
                or wanted.name=any(select lower(alias) from unnest(ct.aliases) alias) then 0
           when wanted.name<>'' and strpos(lower(ct.name),wanted.name)>0
                and (select count(*) from case_types other
                      where other.practice_id=ct.practice_id and other.is_active
                        and strpos(lower(other.name),wanted.name)>0)=1 then 1
           when ct.is_default and not wanted.spoken then 2
      end as rank
    from case_types ct, lead, wanted
    where ct.practice_id=lead.practice_id and ct.is_active
  ) ranked where rank is not null order by rank limit 1
)
select row_to_json(lead) as lead,
       (select row_to_json(loc) from loc) as location,
       (select row_to_json(kind) from kind) as case_type,
       (select coalesce(json_agg(json_build_object('id',c.id,'stride_user_id',c.stride_user_id,
                'display_name',c.display_name,'credentials',c.credentials,'bio',c.bio)
                order by c.display_name),'[]'::json)
          from clinician_assignments ca join clinicians c on c.id=ca.clinician_id and c.is_active
         where ca.is_active and ca.booking_location_id=(select id from loc)
           and ca.case_type_id=(select id from kind)) as clinicians,
       (select json_build_object('last_success_at',s.last_success_at,'window_start',s.window_start)
          from availability_sync_state s
         where s.booking_location_id=(select id from loc)
           and s.duration_minutes=(select duration_minutes from kind)) as sync_state,
       (select string_agg(bl.name, ', ' order by bl.name) from booking_locations bl
         where bl.practice_id=lead.practice_id and bl.is_active) as location_names,
       (select string_agg(ct.name, ', ' order by ct.name) from case_types ct
         where ct.practice_id=lead.practice_id and ct.is_active) as case_type_names,
       (select string_agg(ct.name||' = '||array_to_string(ct.aliases, ', '), '; ' order by ct.name)
          from case_types ct where ct.practice_id=lead.practice_id and ct.is_active
           and cardinality(ct.aliases)>0) as service_aliases,
       coalesce((select stride_booking_enabled from practice_settings ps
                  where ps.practice_id=lead.practice_id), false) as booking_enabled
from lead
"""


def load_context(
    conn, lead_id: str, *, location_name: str | None = None, case_type_name: str | None = None,
    settings: Settings | None = None,
) -> BookingContext:
    settings = settings or get_settings()
    wanted_case = (case_type_name or "").strip()
    row = conn.execute(
        CONTEXT_SQL,
        {"lead_id": lead_id, "location": (location_name or "").strip(), "case_type": wanted_case},
    ).fetchone()
    if not row:
        raise BookingToolError("I could not find this patient's record.")
    lead, names = row["lead"], row["location_names"] or ""
    wanted = (location_name or lead["location"] or "").strip()
    if not wanted:
        raise ValueError(f"NEED_LOCATION: ask which clinic the caller prefers: {names}.")
    location = row["location"]
    if not location:
        raise ValueError(
            f"UNKNOWN_LOCATION: we have no clinic called {wanted}. Our clinics are {names}."
        )
    case_type = row["case_type"]
    if not case_type and wanted_case:
        raise ValueError(
            f"UNKNOWN_SERVICE: we don't book {wanted_case}. Our services are "
            f"{row['case_type_names'] or 'none'}. Ask which one they need."
        )
    if not case_type:
        raise BookingToolError("No case type is configured for booking.")
    if not row["clinicians"]:
        raise ValueError(
            f"NO_CLINICIANS: no therapist is set up for {case_type['name']} at {location['name']}. "
            "Offer to text the booking link or transfer to the team."
        )
    sync_state = row["sync_state"]
    if sync_state:
        sync_state = {
            "last_success_at": datetime.fromisoformat(sync_state["last_success_at"])
            if sync_state["last_success_at"] else None,
            "window_start": date.fromisoformat(sync_state["window_start"])
            if sync_state["window_start"] else None,
        }
    return BookingContext(
        lead=lead, location=location, case_type=case_type, clinicians=row["clinicians"],
        now_local=booking_now(settings, location["timezone"]), settings=settings,
        sync_state=sync_state, booking_enabled=row["booking_enabled"],
    )


def call_case_variables(conn, lead_id: str) -> dict[str, str]:
    """Call variables for the booking agent: the lead's service and every service we book."""
    row = conn.execute(CONTEXT_SQL, {"lead_id": lead_id, "location": "", "case_type": ""}).fetchone()
    if not row:
        return {"case_type": "", "case_types_offered": "", "service_aliases": ""}
    return {
        "case_type": (row["case_type"] or {}).get("name", ""),
        "case_types_offered": row["case_type_names"] or "",
        # Everyday words per service ("Physical Therapy = ortho, orthopedic; ..."): the agent maps
        # "it's for ortho" to a service instead of asking the caller to pick between names.
        "service_aliases": row["service_aliases"] or "",
    }


def _name_tokens(value: str) -> list[str]:
    tokens = re.findall(r"[a-z]+", value.lower())
    return [token for token in tokens if token not in NAME_PREFIXES]


def match_clinicians(clinicians: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """Every word the caller said must appear in the clinician's name ('Dr. Nguyen', 'Thao')."""
    wanted = _name_tokens(name)
    if not wanted:
        return []
    return [
        clinician for clinician in clinicians
        if all(token in _name_tokens(clinician["display_name"]) for token in wanted)
    ]


# ---------------------------------------------------------------------------
# Date windows
# ---------------------------------------------------------------------------

def date_window(
    when: str, now_local: datetime, horizon_days: int, specific: date | None = None,
) -> tuple[date, date]:
    today = now_local.date()
    last = today + timedelta(days=horizon_days - 1)
    monday = today - timedelta(days=today.weekday())
    if when == "earliest":
        return today, last
    if when == "today":
        return today, today
    if when == "tomorrow":
        return today + timedelta(days=1), today + timedelta(days=1)
    if when == "this_week":
        return today, monday + timedelta(days=6)
    if when == "next_week":
        return monday + timedelta(days=7), monday + timedelta(days=13)
    if when == "in_two_weeks":
        return monday + timedelta(days=14), monday + timedelta(days=20)
    if when == "specific_date":
        if specific is None:
            raise ValueError("NEED_DATE: specific_date needs the date as YYYY-MM-DD.")
        if specific < today:
            raise ValueError(
                f"PAST_DATE: {_spoken_date(specific)} has already passed. Ask for another day."
            )
        return specific, specific
    raise ValueError(f"BAD_WHEN: when must be one of {', '.join(WHEN_VALUES)}.")


def _spoken_date(value: date) -> str:
    return value.strftime("%A, %B %d").replace(" 0", " ")


def _spoken_time(value: clock_time) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def _describe(slot: dict[str, Any]) -> str:
    return (
        f"{_spoken_date(slot['local_date'])} ({slot['local_date'].isoformat()}) at "
        f"{_spoken_time(slot['local_time'])} with {slot['display_name']}"
    )


# ---------------------------------------------------------------------------
# Cache freshness and search
# ---------------------------------------------------------------------------

def _fast_providers(settings: Settings) -> ProviderClients:
    """Stride client for a live call: short timeout, no retries."""
    return ProviderClients(settings.model_copy(update={
        "request_timeout_seconds": settings.booking_stride_timeout_seconds,
        "http_retry_attempts": 1,
    }))


def cache_is_fresh(context: BookingContext) -> bool:
    state = context.sync_state
    return bool(
        state
        and state["last_success_at"] is not None
        and datetime.now(UTC) - state["last_success_at"]
        < timedelta(seconds=context.settings.booking_cache_max_age_seconds)
        and state["window_start"] == context.now_local.date()
    )


def refresh_cache(
    trace: WorkflowTrace, context: BookingContext, providers: ProviderClients,
) -> None:
    """Refresh this location's cache inline when the background sync is late."""
    with transaction() as conn:
        targets = [
            target for target in sync_targets(conn, context.location["id"])
            if target["duration_minutes"] == context.case_type["duration_minutes"]
        ]
    if not targets:
        return
    try:
        sync_target(trace, targets[0], providers=providers, settings=context.settings)
    except ProviderError:
        # Offer from the older cache; the booking re-checks live anyway.
        trace.log("availability_inline_sync_failed", booking_location_id=context.location["id"])
        if not context.sync_state or context.sync_state["last_success_at"] is None:
            raise


def search_slots(
    conn,
    context: BookingContext,
    *,
    start: date,
    end: date,
    clinician_ids: list[int],
    time_of_day: str | None = None,
    limit: int = OFFER_COUNT,
) -> list[dict[str, Any]]:
    earliest = context.now_local + timedelta(minutes=context.settings.booking_min_notice_minutes)
    window = TIME_OF_DAY.get(time_of_day or "")
    rows = conn.execute(
        "select s.id,s.clinician_id,s.local_date,s.local_time,s.start_utc,s.end_utc,s.timezone,"
        "c.display_name,c.stride_user_id from availability_slots s "
        "join clinicians c on c.id=s.clinician_id "
        "where s.booking_location_id=%s and s.duration_minutes=%s and s.status='open' "
        "and s.clinician_id = any(%s) and s.start_utc>=%s and s.local_date between %s and %s "
        "and (%s::time is null or s.local_time>=%s::time) "
        "and (%s::time is null or s.local_time<%s::time) "
        "and not exists(select 1 from slot_holds h where h.slot_id=s.id and h.status='active' "
        "and h.expires_at>now()) "
        "order by s.start_utc,c.display_name limit 60",
        (
            context.location["id"], context.case_type["duration_minutes"], clinician_ids,
            earliest.astimezone(UTC), start, end,
            window[0] if window else None, window[0] if window else None,
            window[1] if window else None, window[1] if window else None,
        ),
    ).fetchall()
    # Several clinicians often share a start time; offer distinct times.
    seen: set[datetime] = set()
    picked = []
    for row in rows:
        if row["start_utc"] in seen:
            continue
        seen.add(row["start_utc"])
        picked.append(row)
        if len(picked) == limit:
            break
    return picked


def _remember_offers(conn, call_key: str, context: BookingContext, slots: list[dict]) -> None:
    if slots:
        conn.execute(
            "insert into slot_offers(call_id,lead_id,slot_id,case_type_id) "
            "select %s,%s,unnest(%s::bigint[]),%s",
            (call_key, context.lead["id"], [slot["id"] for slot in slots], context.case_type["id"]),
        )


def _offer_text(slots: list[dict[str, Any]]) -> str:
    return "; ".join(f"({index}) {_describe(slot)}" for index, slot in enumerate(slots, 1))


BOOK_HINT = (
    "Offer these times in plain words, without the numbers in brackets or the dates in "
    "parentheses. When the caller picks one, call book_appointment with that date (YYYY-MM-DD) "
    "and time."
)


def _today_note(context: BookingContext) -> str:
    """The booking calendar's today, which the agent must use for dates the caller says."""
    return f"Calendar today: {context.now_local.strftime('%A, %B %d, %Y').replace(' 0', ' ')}."


def find_slots(
    trace: WorkflowTrace,
    *,
    lead_id: str,
    call_id: str | None,
    when: str,
    specific_date: date | None = None,
    time_of_day: str | None = None,
    clinician_name: str | None = None,
    location_name: str | None = None,
    case_type_name: str | None = None,
    settings: Settings | None = None,
    providers: ProviderClients | None = None,
) -> str:
    settings = settings or get_settings()
    providers = providers or _fast_providers(settings)
    if time_of_day and time_of_day not in TIME_OF_DAY:
        raise ValueError("BAD_TIME_OF_DAY: time_of_day must be morning, afternoon or evening.")
    call_key = call_id or f"direct:{lead_id}"
    # Normal case (fresh cache): load, search and remember offers in one transaction.
    with transaction() as conn:
        context = load_context(conn, lead_id, location_name=location_name,
                               case_type_name=case_type_name, settings=settings)
        start, end = date_window(when, context.now_local, settings.booking_horizon_days,
                                 specific_date)
        pool = context.clinicians
        if clinician_name:
            pool = match_clinicians(context.clinicians, clinician_name)
            if not pool:
                names = ", ".join(c["display_name"] for c in context.clinicians)
                return (
                    f"NO_SUCH_THERAPIST: no therapist named {clinician_name} sees "
                    f"{context.case_type['name']} patients at {context.location['name']}. "
                    f"Therapists there: {names}. Ask if one of them is fine, or offer the earliest "
                    "time with any therapist."
                )
        if cache_is_fresh(context):
            return _offer(trace, conn, context, call_key, start, end, pool, time_of_day,
                          clinician_name, when)
    refresh_cache(trace, context, providers)
    with transaction() as conn:
        return _offer(trace, conn, context, call_key, start, end, pool, time_of_day,
                      clinician_name, when)


def _offer(
    trace: WorkflowTrace, conn, context: BookingContext, call_key: str, start: date, end: date,
    pool: list[dict[str, Any]], time_of_day: str | None, clinician_name: str | None, when: str,
) -> str:
    settings = context.settings
    ids = [clinician["id"] for clinician in pool]
    where = f"{context.case_type['name']} at {context.location['name']}"
    who = f" with {pool[0]['display_name']}" if clinician_name and len(pool) == 1 else ""
    slots = search_slots(conn, context, start=start, end=end, clinician_ids=ids,
                         time_of_day=time_of_day)
    if slots:
        _remember_offers(conn, call_key, context, slots)
        trace.log("slots_offered", count=len(slots), when=when)
        return f"OPENINGS for {where}{who}: {_offer_text(slots)}. {BOOK_HINT} {_today_note(context)}"
    # Nothing in the wished-for range: offer the next times after it.
    horizon_end = context.now_local.date() + timedelta(days=settings.booking_horizon_days - 1)
    later = search_slots(conn, context, start=end + timedelta(days=1), end=horizon_end,
                         clinician_ids=ids, time_of_day=time_of_day)
    if not later and time_of_day:
        later = search_slots(conn, context, start=start, end=horizon_end, clinician_ids=ids)
    if later:
        _remember_offers(conn, call_key, context, later)
        trace.log("slots_offered", count=len(later), when=when, fallback=True)
        return (
            f"NOTHING_IN_RANGE: no {where}{who} openings for that time. "
            f"Next openings: {_offer_text(later)}. {BOOK_HINT} {_today_note(context)}"
        )
    if clinician_name and len(ids) < len(context.clinicians):
        return (
            f"NO_OPENINGS_FOR_THERAPIST: {pool[0]['display_name']} has no openings in the next "
            f"{settings.booking_horizon_days} days. Ask if any other therapist is fine, then search "
            "again without a therapist name."
        )
    return (
        f"NO_OPENINGS: no {where} openings in the next {settings.booking_horizon_days} days. "
        "Offer to text the booking link or transfer to the team."
    )


def list_providers(
    *, lead_id: str, location_name: str | None = None, case_type_name: str | None = None,
    settings: Settings | None = None,
) -> str:
    """Who the caller can see: clinicians for this service at this clinic, from our own tables."""
    with transaction() as conn:
        context = load_context(conn, lead_id, location_name=location_name,
                               case_type_name=case_type_name, settings=settings)
    people = []
    for clinician in context.clinicians:
        details = "; ".join(
            part for part in (clinician.get("credentials"), clinician.get("bio")) if part
        )
        people.append(f"{clinician['display_name']}" + (f" ({details})" if details else ""))
    return (
        f"PROVIDERS for {context.case_type['name']} at {context.location['name']}: "
        f"{'; '.join(people)}. Answer only from this list; do not add anything else about them. "
        "Then ask if they would like one of them, or whoever is available first."
    )


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------

@dataclass
class Patient:
    first_name: str
    last_name: str
    date_of_birth: date
    email: str | None = None


def clean_spelled_name(value: str) -> str:
    """Rejoin a name the voice model split while copying spelled letters ('Chaub h ary').

    Only when a stray single letter is present: real multi-word names ('De La Cruz')
    have no one-letter parts and are left alone.
    """
    parts = value.split()
    if len(parts) > 1 and any(len(part) == 1 for part in parts):
        joined = "".join(parts)
        return joined[:1].upper() + joined[1:].lower()
    return value


def validate_patient(arguments: dict[str, Any], today: date) -> Patient:
    first = clean_spelled_name(str(arguments.get("first_name") or "").strip())
    last = clean_spelled_name(str(arguments.get("last_name") or "").strip())
    if not first or not last:
        raise ValueError("MISSING_NAME: confirm the caller's first and last name, then try again.")
    if len(first) > 100 or len(last) > 100:
        raise ValueError("BAD_NAME: the name is too long. Confirm it with the caller.")
    raw_dob = str(arguments.get("date_of_birth") or "").strip()
    try:
        dob = date.fromisoformat(raw_dob)
    except ValueError:
        raise ValueError(
            "BAD_DATE_OF_BIRTH: send date_of_birth as YYYY-MM-DD, for example 1990-04-26."
        ) from None
    if dob > today or dob.year < today.year - 120:
        raise ValueError("BAD_DATE_OF_BIRTH: that date of birth is not possible. Confirm it again.")
    email = str(arguments.get("email") or "").strip().lower() or None
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("BAD_EMAIL: that email address is not valid. Confirm it or skip it.")
    return Patient(first, last, dob, email)


def parse_clock(value: str) -> clock_time:
    text = re.sub(r"\s+", " ", str(value).strip().upper()).replace(".", "")
    for pattern in ("%I %p", "%I:%M %p", "%I%p", "%I:%M%p", "%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=UTC).time()
        except ValueError:
            continue
    raise ValueError("BAD_TIME: send the time like 9:00 AM or 14:30.")


def _stride_phone(phone: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


class _Deadline:
    def __init__(self, settings: Settings):
        self.started = time.monotonic()
        self.limit = settings.booking_tool_deadline_seconds
        self.step = settings.booking_stride_timeout_seconds

    def allows_another_call(self) -> bool:
        return time.monotonic() - self.started + self.step <= self.limit


def _find_slot_for_booking(
    conn, context: BookingContext, call_key: str, wanted_date: date, wanted_time: clock_time,
    clinician_name: str | None,
    slot_id: int | None = None,
) -> dict[str, Any] | None:
    pool = context.clinicians
    if clinician_name:
        pool = match_clinicians(context.clinicians, clinician_name) or []
        if not pool:
            return None
    rows = conn.execute(
        "select s.id,s.clinician_id,s.local_date,s.local_time,s.start_utc,s.end_utc,s.timezone,"
        "s.status,c.display_name,c.stride_user_id,"
        "exists(select 1 from slot_offers o where o.slot_id=s.id and o.call_id=%s "
        "and o.offered_at>now()-%s) as offered "
        "from availability_slots s join clinicians c on c.id=s.clinician_id "
        "where s.booking_location_id=%s and s.duration_minutes=%s and s.clinician_id = any(%s) "
        "and s.local_date=%s and s.local_time=%s and s.status='open' "
        "and (%s::bigint is null or s.id=%s) "
        "order by offered desc,c.display_name",
        (call_key, OFFER_MEMORY, context.location["id"], context.case_type["duration_minutes"],
         [c["id"] for c in pool], wanted_date, wanted_time, slot_id, slot_id),
    ).fetchall()
    earliest = context.now_local + timedelta(minutes=context.settings.booking_min_notice_minutes)
    for row in rows:
        if row["start_utc"] >= earliest.astimezone(UTC):
            return row
    return None


def _alternatives(trace: WorkflowTrace, context: BookingContext, call_key: str, day: date) -> str:
    horizon_end = context.now_local.date() + timedelta(days=context.settings.booking_horizon_days - 1)
    start = max(day, context.now_local.date())
    with transaction() as conn:
        slots = search_slots(conn, context, start=start, end=horizon_end,
                             clinician_ids=[c["id"] for c in context.clinicians])
        if slots:
            _remember_offers(conn, call_key, context, slots)
    if not slots:
        return "There are no other openings soon. Offer to text the booking link or transfer."
    trace.log("slots_offered", count=len(slots), alternatives=True)
    return f"Other openings: {_offer_text(slots)}. {BOOK_HINT}"


def _existing_booking(conn, lead_id: str) -> dict[str, Any] | None:
    return conn.execute(
        "select a.id,a.state,a.start_utc,a.stride_appointment_id,bl.name as location_name,"
        "bl.timezone from appointments a left join booking_locations bl "
        "on bl.id=a.booking_location_id where a.lead_id=%s "
        "and a.state in ('booking','scheduled','unknown') order by a.id desc limit 1",
        (lead_id,),
    ).fetchone()


def _close_attempt(appointment_id: int, hold_id: int, *, state: str, error: str,
                   slot_id: int | None = None, slot_gone: bool = False) -> None:
    with transaction() as conn:
        conn.execute(
            "update appointments set state=%s,stride_error=%s,needs_staff_review=%s,"
            "updated_at=now() where id=%s",
            (state, error[:500], state == "unknown", appointment_id),
        )
        conn.execute(
            "update slot_holds set status='released',released_at=now() "
            "where id=%s and status='active'",
            (hold_id,),
        )
        if slot_gone and slot_id is not None:
            conn.execute(
                "update availability_slots set status='gone',updated_at=now() "
                "where id=%s and status='open'",
                (slot_id,),
            )


# Who booked: the voice agent on a call, or staff from the dashboard calendar. It is stored on the
# appointment and in the lead's history, so reports can tell them apart.
# appointments.booking_source has a check constraint (link, call, sms, manual, voice_agent): a staff
# booking from the dashboard is stored as 'manual'. (A first version stored 'staff' and every
# calendar booking failed on demo, 2026-10-14; migration 038 now puts the constraint in our files.)
BOOKING_SOURCES = {"voice_agent": ("voice_agent", "voice_booking", "voice booking"),
                   "staff": ("manual", "staff_booking", "staff booking")}


def _booked_elsewhere(conn, lead_id: str) -> str | None:
    """A lead booked from Stride or the Sheet has no row in `appointments`; still never book twice.

    Without this a staff (calendar) or voice booking made a second real Stride appointment for a
    lead the Stride import had already marked booked (found in the PR #14 review, 2026-10-14).
    """
    row = conn.execute(
        "select l.status,(select min(s.start_utc) from stride_patients p join stride_appointments s "
        " on s.practice_id=p.practice_id and s.patient_stride_id=p.stride_id and s.deleted_at is null "
        " and s.start_utc>now() and lower(coalesce(s.status,'')) not in "
        " ('cancel','cancelled','late cancel','no show','a','l','n') where p.lead_id=l.id) as next_visit "
        "from leads l where l.id=%s",
        (lead_id,),
    ).fetchone()
    if not row:
        return None
    if row["next_visit"] is not None:
        return "an upcoming appointment in Stride"
    if row["status"] == "booked":
        return "a booking on record"
    return None


def _flag(lead_id: str, reason: str) -> None:
    with transaction() as conn:
        flag_lead_for_review(conn, lead_id, reason)


def book_slot(
    trace: WorkflowTrace,
    *,
    lead_id: str,
    call_id: str | None,
    arguments: dict[str, Any],
    settings: Settings | None = None,
    providers: ProviderClients | None = None,
    source: str = "voice_agent",
) -> str:
    stored_source, history_source, label = BOOKING_SOURCES[source]
    settings = settings or get_settings()
    providers = providers or _fast_providers(settings)
    deadline = _Deadline(settings)
    call_key = call_id or f"direct:{lead_id}"
    raw_date = str(arguments.get("date") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
        raise ValueError("BAD_DATE: send the appointment date as YYYY-MM-DD.")
    wanted_date = date.fromisoformat(raw_date)
    wanted_time = parse_clock(str(arguments.get("time") or ""))
    clinician_name = str(arguments.get("clinician_name") or "").strip() or None
    raw_slot = arguments.get("slot_id")
    if raw_slot not in (None, "") and not str(raw_slot).isdigit():
        raise ValueError("BAD_SLOT: the slot id must be a number.")
    slot_id = int(raw_slot) if raw_slot not in (None, "") else None

    with transaction() as conn:
        context = load_context(conn, lead_id, location_name=arguments.get("location"),
                               case_type_name=arguments.get("case_type"), settings=settings)
        patient = validate_patient(arguments, context.now_local.date())
        if not context.booking_enabled:
            raise BookingToolError("Booking is switched off for this practice.")
        existing = _existing_booking(conn, lead_id)
        if existing:
            if existing["state"] == "scheduled":
                when = existing["start_utc"]
                text = "an appointment"
                if when is not None and existing["timezone"]:
                    local = when.astimezone(ZoneInfo(existing["timezone"]))
                    text = f"{_spoken_date(local.date())} at {_spoken_time(local.time())}"
                return f"ALREADY_BOOKED: this patient already has {text}. Do not book again."
            if existing["state"] == "booking":
                return "IN_PROGRESS: a booking for this patient is already being made. Wait a moment."
            return (
                "UNCONFIRMED: an earlier booking for this patient needs staff to confirm it. "
                "Tell the caller our team will confirm their appointment by text."
            )
        elsewhere = _booked_elsewhere(conn, lead_id)
        if elsewhere:
            return f"ALREADY_BOOKED: this patient already has {elsewhere}. Do not book again."
        slot = _find_slot_for_booking(conn, context, call_key, wanted_date, wanted_time,
                                      clinician_name, slot_id=slot_id)
    if slot is None:
        return (
            f"NOT_AVAILABLE: {_spoken_time(wanted_time)} on {_spoken_date(wanted_date)} is not "
            f"an open time. {_alternatives(trace, context, call_key, wanted_date)}"
        )

    # 1. Hold the slot and open the booking record (one transaction).
    with transaction() as conn:
        conn.execute(
            "update slot_holds set status='expired',released_at=now() "
            "where slot_id=%s and status='active' and expires_at<=now()",
            (slot["id"],),
        )
        hold = conn.execute(
            "insert into slot_holds(slot_id,lead_id,call_id,expires_at) "
            "values(%s,%s,%s,now()+%s) on conflict do nothing returning id",
            (slot["id"], lead_id, call_id, timedelta(seconds=settings.booking_hold_seconds)),
        ).fetchone()
        if not hold:
            taken = True
        else:
            taken = False
            start_utc = slot["start_utc"]
            booking_key = f"{lead_id}:{start_utc.isoformat()}"
            reuse = conn.execute(
                "select id from appointments where booking_key=%s and state='failed' for update",
                (booking_key,),
            ).fetchone()
            values = (
                slot["id"], hold["id"], context.case_type["id"], context.location["id"], call_id,
                slot["stride_user_id"], context.location["stride_location_id"],
                context.case_type["stride_appointment_type_id"],
            )
            if reuse:
                conn.execute(
                    "update appointments set state='booking',stride_error=null,"
                    "needs_staff_review=false,slot_id=%s,slot_hold_id=%s,case_type_id=%s,"
                    "booking_location_id=%s,call_id=%s,clinician_id=%s,location_id=%s,"
                    "appointment_type_id=%s,updated_at=now() where id=%s",
                    (*values, reuse["id"]),
                )
                appointment_id = reuse["id"]
            else:
                created = conn.execute(
                    "insert into appointments(lead_id,practice_id,booking_source,state,booking_key,"
                    "slot_id,slot_hold_id,case_type_id,booking_location_id,call_id,clinician_id,"
                    "location_id,appointment_type_id,start_utc,end_utc) "
                    "values(%s,%s,%s,'booking',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "on conflict do nothing returning id",
                    (lead_id, context.lead["practice_id"], stored_source, booking_key, *values,
                     slot["start_utc"], slot["end_utc"]),
                ).fetchone()
                if not created:
                    conn.execute(
                        "update slot_holds set status='released',released_at=now() where id=%s",
                        (hold["id"],),
                    )
                    return "IN_PROGRESS: a booking for this patient is already being made. Wait a moment."
                appointment_id = created["id"]
            # The date the patient confirmed on the call; compared with the Sheet's for staff.
            conn.execute("update appointments set patient_dob=%s where id=%s",
                         (patient.date_of_birth, appointment_id))
    if taken:
        return (
            f"TAKEN: someone is booking {_spoken_time(wanted_time)} on {_spoken_date(wanted_date)} "
            f"right now. {_alternatives(trace, context, call_key, wanted_date)}"
        )
    hold_id = hold["id"]
    trace.log("slot_held", appointment_id=appointment_id, slot_id=slot["id"])

    def give_up(state: str, error: str, *, gone: bool = False) -> None:
        _close_attempt(appointment_id, hold_id, state=state, error=error,
                       slot_id=slot["id"], slot_gone=gone)

    team_follow_up = (
        "FOLLOW_UP: I could not finish the booking just now. Tell the caller our team will "
        "call them back to book."
    )
    stage = "availability"
    try:
        # 2. Live re-check of this one slot.
        live = providers.stride_availability(
            trace,
            location=context.location["stride_location_id"],
            duration=context.case_type["duration_minutes"],
            clinician_ids=str(slot["stride_user_id"]),
            start_date=slot["local_date"],
            end_date=slot["local_date"],
        )
        wanted_clock = slot["local_time"].strftime("%H:%M:%S")
        if not any(item.local_date == slot["local_date"].isoformat()
                   and item.local_time == wanted_clock for item in live):
            give_up("failed", "slot no longer open in Stride", gone=True)
            return (
                "TAKEN: that time was just taken. "
                f"{_alternatives(trace, context, call_key, wanted_date)}"
            )

        # 3a. Patient (reused when an earlier attempt already created it).
        lead = context.lead
        patient_id = lead["stride_patient_id"]
        if not patient_id:
            if not deadline.allows_another_call():
                give_up("failed", "tool deadline reached before patient creation")
                return team_follow_up
            stage = "patient"
            patient_id = providers.stride_create(trace, "patients", {
                "first_name": patient.first_name,
                "last_name": patient.last_name,
                "date_of_birth": patient.date_of_birth.isoformat(),
                "contact_info": {
                    "mobile_phone_number": _stride_phone(lead["phone_e164"]),
                    "personal_email": patient.email or lead["email"] or "",
                    "preferred_contact_method": "P",
                    "appointment_reminder_text": True,
                },
                "primary_address": {
                    "address_1": settings.booking_default_address_1,
                    "city": settings.booking_default_city,
                    "state": settings.booking_default_state,
                    "zip_code": settings.booking_default_zip,
                    "address_type": "H",
                },
            })
            with transaction() as conn:
                conn.execute("update leads set stride_patient_id=%s where id=%s",
                             (patient_id, lead_id))

        # 3b. Case. Stride allows one Initial Evaluation per case, so a case is
        # reused only while no appointment has been made on it.
        case_id = lead["stride_case_id"]
        if case_id:
            with transaction() as conn:
                used = conn.execute(
                    "select 1 from appointments where stride_case_id=%s "
                    "and stride_appointment_id is not null limit 1",
                    (case_id,),
                ).fetchone()
            if used:
                case_id = None

        def create_case() -> int:
            title = (lead["lead_type"] or context.case_type["name"]).strip().title()
            new_id = providers.stride_create(trace, "cases",
                                             {"patient_id": patient_id, "title": title})
            with transaction() as conn:
                conn.execute("update leads set stride_case_id=%s where id=%s", (new_id, lead_id))
                conn.execute("update appointments set stride_case_id=%s where id=%s",
                             (new_id, appointment_id))
            return new_id

        if not case_id:
            if not deadline.allows_another_call():
                give_up("failed", "tool deadline reached before case creation")
                return team_follow_up
            stage = "case"
            case_id = create_case()
        else:
            with transaction() as conn:
                conn.execute("update appointments set stride_case_id=%s where id=%s",
                             (case_id, appointment_id))

        # 3c. Appointment. is_pending makes Stride itself refuse an overlap.
        appointment_payload = {
            "case_id": case_id,
            "primary_attendee": slot["stride_user_id"],
            "location": context.location["stride_location_id"],
            "appointment_type": context.case_type["stride_appointment_type_id"],
            "start_date_utc": slot["start_utc"].astimezone(UTC).isoformat(),
            "end_date_utc": slot["end_utc"].astimezone(UTC).isoformat(),
            "is_pending": True,
            "appointment_status": "O",
            "place_of_service": "O",
        }
        if not deadline.allows_another_call():
            give_up("failed", "tool deadline reached before appointment creation")
            return team_follow_up
        stage = "appointment"
        try:
            stride_appointment_id = providers.stride_create(trace, "appointments",
                                                            appointment_payload)
        except ProviderError as exc:
            # The case already has an evaluation: make a fresh case and try once more.
            if "initial evaluation" in str(exc) and deadline.allows_another_call():
                stage = "case"
                case_id = create_case()
                if not deadline.allows_another_call():
                    give_up("failed", "tool deadline reached after replacing the case")
                    return team_follow_up
                stage = "appointment"
                stride_appointment_id = providers.stride_create(
                    trace, "appointments", {**appointment_payload, "case_id": case_id}
                )
            else:
                raise
    except ProviderError as exc:
        detail = str(exc).lower()
        if exc.ambiguous:
            give_up("unknown", f"{stage}: {exc}")
            _flag(lead_id, f"Stride {stage} result unclear during {label}; confirm in Stride")
            trace.log("booking_unknown", stage=stage)
            return (
                "UNCONFIRMED: the booking system did not answer clearly. Do not try again. Tell "
                "the caller our team will confirm their appointment by text."
            )
        if stage == "patient" and "already exists" in detail:
            give_up("failed", "Stride: patient already exists")
            _flag(lead_id, "Stride says this patient already exists; staff must book manually")
            return (
                "PATIENT_EXISTS: this patient is already in our system. Tell the caller our team "
                "will call them back shortly to finish booking."
            )
        if stage == "appointment" and ("overlap" in detail or "already exists" in detail):
            give_up("failed", f"Stride refused the slot: {exc}", gone=True)
            return (
                "TAKEN: that time was just taken. "
                f"{_alternatives(trace, context, call_key, wanted_date)}"
            )
        give_up("failed", f"{stage}: {exc}")
        trace.log("booking_failed", stage=stage, error_code=exc.code)
        return team_follow_up
    except Exception as exc:  # noqa: BLE001 - Stride may have saved something
        give_up("unknown", f"{stage}: unexpected {type(exc).__name__}")
        _flag(lead_id, f"Unexpected error during {label}; confirm in Stride")
        return (
            "UNCONFIRMED: something went wrong while booking. Do not try again. Tell the caller "
            "our team will confirm their appointment by text."
        )

    # 4. Record the booking. Stride is the source of truth from here on.
    raw_event = str(arguments.get("outreach_event_id") or "").strip()
    _finish_booking(trace, context, slot, appointment_id, hold_id, stride_appointment_id,
                    patient_id, case_id, patient,
                    event_id=int(raw_event) if raw_event.isdigit() else None,
                    history_source=history_source)
    duration = context.case_type["duration_minutes"]
    return (
        f"BOOKED: {_spoken_date(slot['local_date'])} at {_spoken_time(slot['local_time'])} with "
        f"{slot['display_name']} at our {context.location['name']} clinic, {duration} minutes. "
        "Confirm this to the caller, say a confirmation text is on its way, and remind them "
        "to call if they will be more than 20 minutes late."
    )


def _finish_booking(
    trace: WorkflowTrace, context: BookingContext, slot: dict[str, Any], appointment_id: int,
    hold_id: int, stride_appointment_id: int, patient_id: int, case_id: int, patient: Patient,
    *, event_id: int | None = None, history_source: str = "voice_booking",
) -> None:
    lead = context.lead
    lead_id = str(lead["id"])
    try:
        with transaction() as conn:
            conn.execute(
                "update appointments set state='scheduled',stride_appointment_id=%s,"
                "stride_case_id=%s,confirmed_at=now(),updated_at=now() where id=%s",
                (stride_appointment_id, case_id, appointment_id),
            )
            conn.execute("update slot_holds set status='booked' where id=%s", (hold_id,))
            conn.execute("update availability_slots set status='booked',updated_at=now() "
                         "where id=%s", (slot["id"],))
            conn.execute("update leads set stride_patient_id=%s,stride_case_id=%s where id=%s",
                         (patient_id, case_id, lead_id))
            mark_booked(conn, lead_id, history_source)
            if event_id is not None:
                # Settle this call as booked now, so a later end-of-call summary
                # that reads it differently cannot undo the booking.
                conn.execute(
                    "update outreach_events set status='delivered',settled_at=now(),"
                    "settled_by='tool',outcome='booked' where id=%s and lead_id=%s "
                    "and channel='call' and status not in ('delivered','failed','skipped')",
                    (event_id, lead_id),
                )
            conn.execute(
                "insert into notification_log(lead_id,appointment_id,notification_type,channel,"
                "status,payload) values(%s,%s,'sms_appointment_booked','sms','queued',%s) "
                "on conflict do nothing",
                (lead_id, appointment_id,
                 Jsonb({"start_utc": slot["start_utc"].astimezone(UTC).isoformat()})),
            )
            enqueue_sheet_update(conn, lead_id=lead_id, event_type="lead_booked",
                                 source_key=f"appointment:{appointment_id}")
            # Test leads are synthetic; they must never reach the client's CRM.
            if not lead["is_test"]:
                handoff_id = str(uuid4())
                conn.execute(
                    "insert into integration_outbox(event_id,event_type,aggregate_id,payload,"
                    "status,destination) values(%s,'appointment.booked.v1',%s,%s,'pending','keap') "
                    "on conflict(event_id) do nothing",
                    (handoff_id, str(appointment_id), Jsonb({
                        "event_type": "appointment.booked.v1",
                        "event_id": handoff_id,
                        "lead_id": lead_id,
                        "first_name": patient.first_name,
                        "last_name": patient.last_name,
                        "email": patient.email or lead["email"],
                        "phone": lead["phone_e164"],
                        "birthday": patient.date_of_birth.isoformat(),
                        "appointment_type_id": context.case_type["stride_appointment_type_id"],
                        "appointment_start_utc": slot["start_utc"].astimezone(UTC).isoformat(),
                        "provider_id": slot["stride_user_id"],
                        "stride_appointment_id": stride_appointment_id,
                    })),
                )
    except Exception as exc:  # noqa: BLE001 - the appointment exists in Stride
        trace.log("booking_local_sync_failed", appointment_id=appointment_id,
                  error_category=type(exc).__name__)
        _flag(lead_id, f"Booked in Stride (appointment {stride_appointment_id}) but our records "
                       "did not update; reconcile")
        return
    trace.log("booking_confirmed", appointment_id=appointment_id,
              stride_appointment_id=stride_appointment_id)
