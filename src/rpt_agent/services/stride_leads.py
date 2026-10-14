"""Connect imported Stride patients to our outreach leads.

A lead we are calling may book on their own (SMS link, phone, front desk). We
only learn that when Stride's next export shows the appointment. This module:

1. links a Stride patient to the lead it belongs to;
2. marks the lead booked and stops its cadence when the patient has a future,
   non-cancelled appointment;
3. flags a booked lead for staff when that booking is cancelled, late-cancelled,
   deleted or a no-show in Stride, with a reason that says which. It never puts
   the lead back into calls and texts.

Every step is recomputed from current table state, so a crash, a re-run or an
out-of-order file cannot leave it half done.
"""

from __future__ import annotations

from typing import Any

from .sheet_sync import enqueue_sheet_update
from .stride_import import CANCELLED_STATUSES, LATE_CANCEL_STATUSES, NO_SHOW_STATUSES

# A booking that did not happen: cancelled, late-cancelled or no-show (deletes too).
_MISSED = tuple(sorted(CANCELLED_STATUSES | NO_SHOW_STATUSES))

# Staff-facing reasons. The Sheet sync recognises them by the " in Stride" ending.
REASON_CANCELLED = "Cancelled in Stride"
REASON_LATE_CANCEL = "Late cancel in Stride"
REASON_NO_SHOW = "No show in Stride"
REASON_REMOVED = "Appointment removed in Stride"
REASON_UNCLEAR = "Unclear change in Stride"

# Same person: the names agree AND (same date of birth, or - only when either side
# has no DOB - the lead's phone is one of the patient's phones). Names agree when the
# normalized full names are equal, or the first names are equal and the lead has no
# last name: the Sheet accepts one-word names ("devyansh") while Stride always has a
# first and last name, so a full-name comparison never matched them (seen in testing).
# Two people in one family share a phone and surname but not a first name + DOB.
_CANDIDATES_SQL = """
with p as (
  select p.stride_id, p.date_of_birth, p.mobile_phone, p.home_phone, p.work_phone, p.preferred_phone,
    lower(regexp_replace(trim(coalesce(p.first_name,'')||' '||coalesce(p.last_name,'')),'\\s+',' ','g')) as full_key,
    lower(trim(coalesce(p.first_name,''))) as first_key
  from stride_patients p
  where p.practice_id=%(practice)s and p.lead_id is null and p.deleted_at is null
), l as (
  select l.id, l.phone_e164, l.created_at, l.date_of_birth,
    lower(regexp_replace(trim(l.full_name),'\\s+',' ','g')) as full_key,
    case when nullif(trim(coalesce(l.last_name,'')),'') is null and position(' ' in trim(l.full_name)) = 0
      then lower(trim(coalesce(nullif(trim(l.first_name),''), split_part(trim(l.full_name),' ',1))))
    end as first_key
  from leads l
  where l.practice_id=%(practice)s and l.stride_patient_id is null
), named as (
  -- Two equality joins (hash joins) instead of one OR join: comparing every patient with
  -- every lead took minutes on a full Stride history (50k patients x 3k leads, 2026-10-14).
  select p.*, l.id as lead_id, l.phone_e164, l.created_at, l.date_of_birth as lead_dob
  from p join l on l.full_key = p.full_key
  union
  select p.*, l.id, l.phone_e164, l.created_at, l.date_of_birth
  from p join l on l.first_key is not null and l.first_key = p.first_key
)
select stride_id, lead_id, phone_e164, created_at from named
where (date_of_birth is not null and lead_dob is not null and date_of_birth = lead_dob)
   or ((date_of_birth is null or lead_dob is null) and phone_e164 is not null
       and phone_e164 in (mobile_phone, home_phone, work_phone, preferred_phone))
"""


def link_patients_to_leads(conn, practice_id: int) -> dict[str, int]:
    counts = {"linked": 0, "ambiguous": 0}
    # A lead our own booking flow created in Stride already knows its patient id.
    exact = conn.execute(
        "update stride_patients p set lead_id=l.id,lead_match_status='matched',lead_matched_at=now() "
        "from leads l where p.practice_id=%s and l.practice_id=p.practice_id "
        "and l.stride_patient_id=p.stride_id and p.lead_id is null returning p.stride_id",
        (practice_id,),
    ).fetchall()
    counts["linked"] += len(exact)

    pairs = conn.execute(_CANDIDATES_SQL, {"practice": practice_id}).fetchall()
    leads_per_patient: dict[int, set[Any]] = {}
    patients_per_lead: dict[Any, set[int]] = {}
    lead_info: dict[Any, dict] = {}
    for pair in pairs:
        leads_per_patient.setdefault(pair["stride_id"], set()).add(pair["lead_id"])
        patients_per_lead.setdefault(pair["lead_id"], set()).add(pair["stride_id"])
        lead_info[pair["lead_id"]] = pair

    for stride_id, lead_ids in leads_per_patient.items():
        lead_id = _same_person_newest(lead_ids, lead_info) if len(lead_ids) > 1 else next(iter(lead_ids))
        if lead_id is None or len(patients_per_lead[lead_id]) > 1:
            # Never guess between people: staff can link by hand later.
            conn.execute(
                "update stride_patients set lead_match_status='ambiguous' "
                "where practice_id=%s and stride_id=%s and lead_match_status<>'ambiguous'",
                (practice_id, stride_id),
            )
            counts["ambiguous"] += 1
            continue
        conn.execute(
            "update stride_patients set lead_id=%s,lead_match_status='matched',lead_matched_at=now() "
            "where practice_id=%s and stride_id=%s",
            (lead_id, practice_id, stride_id),
        )
        conn.execute(
            "update leads set stride_patient_id=%s,updated_at=now() where id=%s and stride_patient_id is null",
            (stride_id, lead_id),
        )
        counts["linked"] += 1
    # A once-ambiguous patient whose extra candidate went away is plain unmatched again.
    conn.execute(
        "update stride_patients set lead_match_status='unmatched' where practice_id=%s "
        "and lead_id is null and lead_match_status='ambiguous' and not (stride_id=any(%s))",
        (practice_id, [stride_id for stride_id, ids in leads_per_patient.items()]),
    )
    return counts


def _same_person_newest(lead_ids: set[Any], lead_info: dict[Any, dict]) -> Any:
    """Several leads fit one Stride patient: link the newest only when they are one person.

    The same person added to the Sheet again (repeat referral) gives two leads with the same name,
    birthday and phone; the older one was stopped by hand_over_number. Refusing to choose left the
    patient unlinked, so a booking never stopped the new lead's calls (seen in testing, 2026-10-14).
    Leads with different phones may be different people (same name + birthday): never guess.
    """
    phones = {lead_info[lead_id]["phone_e164"] for lead_id in lead_ids}
    if len(phones) != 1 or None in phones:
        return None
    return max(lead_ids, key=lambda lead_id: lead_info[lead_id]["created_at"])


def mark_leads_booked_from_stride(conn, practice_id: int) -> int:
    """A linked lead with a future appointment that is still on is booked: stop outreach.

    A no-show counts as "not on" too: staff can mark one on a visit that has not
    started yet, and it must not re-book the lead it just flagged.
    """
    rows = conn.execute(
        "select distinct on (l.id) l.id,a.stride_id as appointment_id,a.case_stride_id "
        "from leads l join stride_patients p on p.lead_id=l.id and p.deleted_at is null "
        "join stride_appointments a on a.practice_id=p.practice_id "
        " and a.patient_stride_id=p.stride_id and a.deleted_at is null "
        " and a.start_utc>now() and lower(coalesce(a.status,'')) <> all(%s) "
        # A lead flagged for a Stride problem is booked again only by a visit that changed after
        # the flag (a real rebook), never by one it already had (e.g. the twin of an unclear delete).
        " and (l.status<>'needs_attention' or coalesce(l.review_reason,'') not like '%% in Stride' "
        "  or a.updated_at>l.status_changed_at) "
        "where l.practice_id=%s and l.status<>'booked' order by l.id,a.start_utc",
        (list(_MISSED), practice_id),
    ).fetchall()
    booked = 0
    for row in rows:
        lead = conn.execute(
            "select status,status_changed_at from leads where id=%s for update", (row["id"],)
        ).fetchone()
        if lead is None or lead["status"] == "booked":
            continue
        reason = "appointment found in Stride"
        conn.execute(
            "update leads set status='booked',cadence_state='completed',needs_review=false,"
            "review_reason=null,review_resolved_at=case when needs_review then now() "
            "else review_resolved_at end,status_reason=%s,status_changed_at=now(),"
            "stride_case_id=coalesce(%s,stride_case_id),updated_at=now() where id=%s",
            (reason, row["case_stride_id"], row["id"]),
        )
        conn.execute(
            "update outreach_events set status='skipped',failure_reason=%s,updated_at=now() "
            "where lead_id=%s and status='planned'",
            ("booked in Stride", row["id"]),
        )
        conn.execute(
            "insert into lead_status_history(lead_id,from_status,to_status,source,reason) "
            "values(%s,%s,'booked','stride',%s)",
            (row["id"], lead["status"], reason),
        )
        # The previous status time makes a later re-booking a new Sheet event.
        enqueue_sheet_update(
            conn,
            lead_id=str(row["id"]),
            event_type="lead_booked",
            source_key=f"stride:{row['appointment_id']}:{lead['status_changed_at']}",
        )
        booked += 1
    return booked


def flag_cancelled_bookings(conn, practice_id: int) -> int:
    """A booked lead whose Stride booking fell through goes to staff review.

    "The booking" is any appointment starting after the lead was marked booked;
    older history cannot trigger this. A rebooking in the same window keeps the
    lead booked. The lead stays out of the cadence either way.

    Only a change Stride sent *after* the lead was marked booked counts. Staff
    marking the lead Booked again (Sheet or dashboard) after a cancel must stick:
    re-reading the same old cancel made a lead flip Booked <-> Needs attention
    every two minutes with the Sheet (seen on demo, 2026-10-13).
    """
    rows = conn.execute(
        "select distinct on (l.id) l.id,a.deleted_at is not null as removed,"
        "lower(coalesce(a.status,'')) as appt_status from leads l "
        "join stride_patients p on p.lead_id=l.id "
        "join stride_appointments a on a.practice_id=p.practice_id and a.patient_stride_id=p.stride_id "
        " and a.start_utc>l.status_changed_at and a.updated_at>l.status_changed_at "
        " and (a.deleted_at is not null or lower(coalesce(a.status,''))=any(%s)) "
        "where l.practice_id=%s and l.status='booked' and not l.needs_review "
        "and not exists(select 1 from stride_appointments g where g.practice_id=p.practice_id "
        " and g.patient_stride_id=p.stride_id and g.start_utc>l.status_changed_at "
        " and g.deleted_at is null and lower(coalesce(g.status,'')) <> all(%s)) "
        "order by l.id,a.updated_at desc,a.start_utc desc",
        (list(_MISSED), practice_id, list(_MISSED)),
    ).fetchall()
    flagged = 0
    for row in rows:
        lead = conn.execute(
            "select status,needs_review,status_changed_at from leads where id=%s for update", (row["id"],)
        ).fetchone()
        if lead is None or lead["status"] != "booked" or lead["needs_review"]:
            continue
        reason = _missed_reason(row["removed"], row["appt_status"])
        conn.execute(
            "update leads set status='needs_attention',needs_review=true,review_reason=%s,"
            "review_flagged_at=now(),status_reason=%s,status_changed_at=now(),updated_at=now() "
            "where id=%s",
            (reason, reason, row["id"]),
        )
        conn.execute(
            "insert into lead_status_history(lead_id,from_status,to_status,source,reason) "
            "values(%s,'booked','needs_attention','stride',%s)",
            (row["id"], reason),
        )
        enqueue_sheet_update(
            conn,
            lead_id=str(row["id"]),
            event_type="stride_booking_cancelled",
            source_key=f"stride:cancelled:{row['id']}:{lead['status_changed_at']}",
        )
        flagged += 1
    return flagged


def _missed_reason(removed: bool, status: str) -> str:
    if removed:
        return REASON_REMOVED
    if status in NO_SHOW_STATUSES:
        return REASON_NO_SHOW
    if status in LATE_CANCEL_STATUSES:
        return REASON_LATE_CANCEL
    return REASON_CANCELLED


def flag_unclear_deletes(conn, practice_id: int, patient_ids: set[int]) -> int:
    """A Stride Delete we could not match to one visit: tell staff instead of guessing.

    Real Stride never has two visits in one case at one start time, so this should not happen; when
    it did (Mock Stride allowed it, 2026-10-13) the visit stayed active here, the lead stayed Booked,
    and every later cancel or no-show was hidden behind it. Only booked leads are flagged.
    """
    if not patient_ids:
        return 0
    rows = conn.execute(
        "select l.id,l.status_changed_at from leads l join stride_patients p on p.lead_id=l.id "
        "where p.practice_id=%s and p.stride_id=any(%s) and l.status='booked' and not l.needs_review "
        "for update of l",
        (practice_id, sorted(patient_ids)),
    ).fetchall()
    for row in rows:
        conn.execute(
            "update leads set status='needs_attention',needs_review=true,review_reason=%s,"
            "review_flagged_at=now(),status_reason=%s,status_changed_at=now(),updated_at=now() where id=%s",
            (REASON_UNCLEAR, REASON_UNCLEAR, row["id"]),
        )
        conn.execute(
            "insert into lead_status_history(lead_id,from_status,to_status,source,reason) "
            "values(%s,'booked','needs_attention','stride',%s)",
            (row["id"], REASON_UNCLEAR),
        )
        enqueue_sheet_update(
            conn, lead_id=str(row["id"]), event_type="stride_booking_cancelled",
            source_key=f"stride:unclear:{row['id']}:{row['status_changed_at']}",
        )
    return len(rows)


def reconcile_leads(conn, practice_id: int) -> dict[str, int]:
    counts = link_patients_to_leads(conn, practice_id)
    counts["booked"] = mark_leads_booked_from_stride(conn, practice_id)
    counts["flagged"] = flag_cancelled_bookings(conn, practice_id)
    return counts
