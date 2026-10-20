"""Keap sync, phase 2A: decide what each person's Keap contact should look like.

Reads only our own database (leads + the Stride mirror) and writes one JSON document
per person into keap_sync.desired, with a hash. When the hash changes the row is
queued for the keap-worker (phase 2B). Nothing here talks to Keap.

Everything is recomputed from current table state, so a crash, a re-run or an
out-of-order Stride file cannot leave a person half planned. Never log names,
emails, phones or birthdays: log counts and ids only.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from ..config import get_settings
from .stride_import import CANCELLED_STATUSES

DOC_VERSION = 1
CHECKED_IN_STATUSES = {"checked in", "c"}
# Lead statuses that mean "our follow-up ended without a booking". Do Not Contact,
# invalid phone and transferred-to-human are deliberately NOT here: the client has
# not chosen their Keap tags yet, so those people are left unchanged.
ENDED_LEAD_STATUSES = {"closed_no_response", "declined"}
# People who asked us to stop, or whose number is wrong, never go to Keap: a Pending tag there can
# start marketing emails or texts. Already in Keap -> left exactly as it is (spec 0.4 / R4).
KEEP_OUT_LEAD_STATUSES = {"do_not_contact", "invalid_phone"}
# A repeat patient comes back as a new lead on the same number; the newest lead owns the
# person's Keap contact (keap_sync.claim_contact hands it over).
REPLACED_SKIP = "replaced by a newer referral: the newer lead owns the Keap contact"
# Same person = same number (the join) and same first name: family members share phones.
_SAME_FIRST_NAME = (
    "lower(coalesce(nullif(n.first_name,''),split_part(n.full_name,' ',1)))="
    "lower(coalesce(nullif(l.first_name,''),split_part(l.full_name,' ',1)))"
)
# More than this many people queued in one tick is a bulk first load: patient-only
# rows are spread out so new leads never wait behind the backlog.
BULK_THRESHOLD = 200
# Keap silently cuts given and family names to 40 characters (checked in the sandbox). Plan
# the clipped value, or every sync would see a 'difference' and a name search would never match.
KEAP_NAME_MAX = 40

# Our appointments.state in Stride's words, so one set of tag rules covers both.
_OUR_STATE_AS_STRIDE = {"scheduled": "Unmarked", "unknown": "Unmarked", "completed": "Checked In",
                        "cancelled": "Cancel", "no_show": "No Show"}

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


@dataclass
class Built:
    desired: dict[str, Any] | None = None
    skip_reason: str | None = None
    freeze: bool = False  # leave the row exactly as it is


# --- settings and mapping ---------------------------------------------------


def load_settings(conn, practice_id: int) -> dict[str, Any]:
    row = conn.execute(
        "select keap_pending_tag_id,keap_gave_up_tag_id,keap_contact_type,keap_sync_start_at,"
        "keap_include_existing_patients,keap_include_existing_leads,keap_remove_pending_when_booked,"
        "keap_remove_booked_on_cancel,stride_location_timezone from practice_settings "
        "where practice_id=%s",
        (practice_id,),
    ).fetchone()
    return dict(row) if row else {}


def load_mapping(conn, practice_id: int) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "select * from keap_appointment_types where practice_id=%s and is_active "
            "order by stride_appointment_type_id",
            (practice_id,),
        )
    ]


def desired_hash(desired: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(desired, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# --- pure planning (no database) -------------------------------------------


def split_name(full_name: str | None) -> tuple[str, str]:
    parts = (full_name or "").split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def _digits(phone: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def _first_valid_email(*candidates: str | None) -> str | None:
    for value in candidates:
        value = (value or "").strip()
        if value and _EMAIL.fullmatch(value):
            return value
    return None


def _first_phone(*candidates: str | None) -> str | None:
    for value in candidates:
        value = (value or "").strip()
        if value and len(_digits(value)) >= 10:
            return value
    return None


def _address(patient: dict[str, Any] | None) -> dict[str, Any] | None:
    if not patient or not (patient.get("address_1") or "").strip():
        return None
    if not ((patient.get("city") or "").strip() or (patient.get("zip") or "").strip()):
        return None
    state = (patient.get("state") or "").strip().upper()
    address = {
        "line1": patient["address_1"].strip(),
        "line2": (patient.get("address_2") or "").strip() or None,
        "locality": (patient.get("city") or "").strip() or None,
        "region_code": f"US-{state}" if len(state) == 2 else None,
        "postal_code": (patient.get("zip") or "").strip() or None,
        "country_code": "USA",
    }
    return address


def _local_wall_time(appt: dict[str, Any], tz_name: str) -> datetime:
    """Clinic wall time of an appointment (Stride's own value, else UTC converted)."""
    local = appt.get("starts_at_local")
    if local is not None:
        return local
    from zoneinfo import ZoneInfo

    return appt["start_utc"].astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)


def _format_time(wall: datetime) -> str:
    return f"{wall.hour % 12 or 12}:{wall.minute:02d} {'AM' if wall.hour < 12 else 'PM'}"


def compute_desired(
    *,
    lead: dict[str, Any] | None,
    patient: dict[str, Any] | None,
    patient_id: int | None,
    appointments: list[dict[str, Any]],
    users: dict[int, str],
    settings: dict[str, Any],
    mapping: list[dict[str, Any]],
    synced_before: bool,
    now: datetime,
) -> Built:
    start_at = settings.get("keap_sync_start_at")
    if start_at is None:
        return Built(skip_reason="keap sync not started")
    patient_deleted = bool(patient and patient.get("deleted_at"))
    if patient_deleted:
        if lead is None:
            # Deletes in Stride never delete in Keap: stop changing the row.
            return Built(freeze=True) if synced_before else Built(
                skip_reason="deleted in Stride before sync")
        patient = None
    if lead is None and patient is None:
        return Built(skip_reason="no lead or patient left")
    if lead is not None and lead.get("status") in KEEP_OUT_LEAD_STATUSES:
        return Built(freeze=True) if synced_before else Built(
            skip_reason="do not contact / wrong number: kept out of Keap")
    if lead is not None and lead.get("superseded"):
        return Built(skip_reason=REPLACED_SKIP)

    # --- contact: Stride wins, except the phone (the lead's is the one we call) ---
    lead = lead or {}
    p = patient or {}
    lead_first, lead_last = (lead.get("first_name") or "", lead.get("last_name") or "")
    if not lead_first and not lead_last:
        lead_first, lead_last = split_name(lead.get("full_name"))
    given = (p.get("first_name") or lead_first or "").strip()[:KEAP_NAME_MAX].strip()
    family = (p.get("last_name") or lead_last or "").strip()[:KEAP_NAME_MAX].strip()
    email = _first_valid_email(
        p.get("preferred_email"), p.get("personal_email"), p.get("work_email"), lead.get("email")
    )
    phone = _first_phone(
        lead.get("phone_e164"), p.get("preferred_phone"), p.get("mobile_phone"),
        p.get("home_phone"), p.get("work_phone"),
    )
    birth = p.get("date_of_birth") or lead.get("date_of_birth")
    address = None if (email or phone) else _address(patient)
    if not (email or phone or address):
        return Built(skip_reason="no contact details")

    contact = {
        "given_name": given,
        "family_name": family,
        "email": email,
        "phone": phone,
        "birth_date": birth.isoformat() if birth else None,
        "contact_type": settings.get("keap_contact_type") or "Lead",
        "address": address,
    }
    match = {
        "email": email,
        "phone": phone,
        "phone_digits": _digits(phone) or None,
        "given_name": given,
        "family_name": family,
    }

    # --- appointments that count ---
    by_type = {int(m["stride_appointment_type_id"]): m for m in mapping}
    remove_booked_on_cancel = settings.get("keap_remove_booked_on_cancel", True)
    live: list[dict[str, Any]] = []
    cancelled_pool: list[dict[str, Any]] = []
    for a in appointments:
        if a.get("appointment_type_id") not in by_type or a.get("start_utc") is None:
            continue
        if (a.get("booked_at") or a["start_utc"]) < start_at:
            continue  # history before go-live never tags anyone (our bookings: when made)
        status = (a.get("status") or "").strip().lower()
        if a.get("deleted_at") is None and status not in CANCELLED_STATUSES:
            live.append(a)
        else:
            cancelled_pool.append(a)
    tag_pool = live if remove_booked_on_cancel else live + cancelled_pool

    # --- tags ---
    pending_tag = settings.get("keap_pending_tag_id")
    gave_up_tag = settings.get("keap_gave_up_tag_id")
    is_male = (p.get("gender") or "").strip().lower() in {"m", "male"}
    tags: set[str] = set()
    for a in tag_pool:
        m = by_type[a["appointment_type_id"]]
        tags.add(str((is_male and m.get("male_booked_tag_id")) or m["booked_tag_id"]))
    for a in live:
        completed = by_type[a["appointment_type_id"]].get("completed_tag_id")
        if completed and (a.get("status") or "").strip().lower() in CHECKED_IN_STATUSES:
            tags.add(str(completed))
    if lead:
        status = lead.get("status")
        ended = status in ENDED_LEAD_STATUSES and gave_up_tag and not live
        if ended:
            tags.add(str(gave_up_tag))
        elif pending_tag:
            if settings.get("keap_remove_pending_when_booked", True):
                if status != "booked" and not live:
                    tags.add(str(pending_tag))
            else:
                tags.add(str(pending_tag))
    owned_tags = {str(t) for t in (pending_tag, gave_up_tag) if t}
    for m in mapping:
        owned_tags.add(str(m["booked_tag_id"]))
        if m.get("male_booked_tag_id"):
            owned_tags.add(str(m["male_booked_tag_id"]))
        if m.get("completed_tag_id"):
            owned_tags.add(str(m["completed_tag_id"]))

    # --- one shared date/time/clinician set: next upcoming, else latest past ---
    tz = settings.get("stride_location_timezone") or "America/Los_Angeles"
    chosen = None
    upcoming = [a for a in live if a["start_utc"] >= now]
    if upcoming:
        chosen = min(upcoming, key=lambda a: a["start_utc"])
    elif live:
        chosen = max(live, key=lambda a: a["start_utc"])
    values = {"date": None, "time": None, "text": None}
    if chosen:
        wall = _local_wall_time(chosen, tz)
        values["date"] = wall.date().isoformat()
        values["time"] = _format_time(wall)
        values["text"] = (
            (chosen.get("clinician_name") or "").strip()
            or users.get(chosen.get("clinician_id") or -1)
            or None
        )
    fields: dict[str, Any] = {}
    for m in mapping:
        for key, kind in (("date_field_id", "date"), ("time_field_id", "time"),
                          ("clinician_field_id", "text")):
            if m.get(key):
                fields[str(m[key])] = {"kind": kind, "value": values[kind]}

    desired = {
        "v": DOC_VERSION,
        "contact": contact,
        "match": match,
        "tags": sorted(tags),
        "owned_tags": sorted(owned_tags),
        "fields": fields,
        "owned_fields": sorted(fields),
        "sources": {
            "lead_id": str(lead["id"]) if lead.get("id") else None,
            "stride_patient_id": patient_id,
        },
    }
    return Built(desired=desired)


def mask_desired(desired: dict[str, Any]) -> dict[str, Any]:
    """A copy safe to print: names, email, phone and birthday hidden; ids kept."""
    masked = copy.deepcopy(desired)

    def hide(value: str | None) -> str | None:
        return value[:1] + "***" if value else value

    contact, match = masked["contact"], masked["match"]
    for key in ("given_name", "family_name"):
        contact[key], match[key] = hide(contact[key]), hide(match[key])
    for container in (contact, match):
        if container.get("email"):
            container["email"] = hide(container["email"].split("@")[0]) + "@***"
    contact["phone"] = ("***" + contact["phone"][-2:]) if contact["phone"] else None
    match["phone_digits"] = ("***" + match["phone_digits"][-2:]) if match["phone_digits"] else None
    match["phone"] = ("***" + match["phone"][-2:]) if match.get("phone") else None
    contact["birth_date"] = "****-**-**" if contact["birth_date"] else None
    if contact.get("address"):
        contact["address"] = {k: ("***" if v else v) for k, v in contact["address"].items()}
    # Appointment date/time/clinician name are still patient data in this context.
    for field in masked["fields"].values():
        if field["kind"] == "text" and field["value"]:
            field["value"] = hide(field["value"])
    masked["sources"]["lead_id"] = (masked["sources"]["lead_id"] or "")[:8] or None
    return masked


# --- attach people ----------------------------------------------------------


def _link_pair(conn, pair, counts: dict[str, int]) -> None:
    """Give a linked Stride patient to its lead's keap_sync row (merge or flag duplicates)."""
    if pair["pat_row"] is None:
        conn.execute(
            "update keap_sync set stride_patient_id=%s where id=%s", (pair["pid"], pair["lead_row"])
        )
        return
    lead_contact, pat_contact = pair["lead_contact"], pair["pat_contact"]
    if lead_contact and pat_contact and lead_contact != pat_contact:
        if pair["lead_status"] != "needs_review" or pair["pat_status"] != "needs_review":
            conn.execute(
                "update keap_sync set status='needs_review',"
                "last_error='duplicate Keap contacts for one person' where id=any(%s)",
                ([pair["lead_row"], pair["pat_row"]],),
            )
            counts["duplicates"] += 1
        return
    conn.execute("delete from keap_sync where id=%s", (pair["pat_row"],))
    conn.execute(
        "update keap_sync set stride_patient_id=%s,keap_contact_id=coalesce(keap_contact_id,%s) "
        "where id=%s",
        (pair["pid"], pat_contact, pair["lead_row"]),
    )
    counts["merged"] += 1


def attach_people(conn, practice_id: int) -> dict[str, int]:
    """Make sure every in-scope person has exactly one keap_sync row."""
    settings = load_settings(conn, practice_id)
    include_tests = get_settings().keap_include_test_leads
    counts = {"leads_added": 0, "patients_added": 0, "merged": 0, "duplicates": 0}

    # Leads created before keap_sync_start_at join only if the clinic wants its existing leads in
    # Keap (keap_include_existing_leads); a lead that already has a row is never dropped.
    counts["leads_added"] = conn.execute(
        "insert into keap_sync(practice_id,lead_id) select l.practice_id,l.id from leads l "
        "where l.practice_id=%s and (not l.is_test or %s) "
        "and (%s or %s::timestamptz is null or l.created_at>=%s::timestamptz) "
        "and not exists(select 1 from keap_sync k where k.lead_id=l.id)",
        (practice_id, include_tests, bool(settings.get("keap_include_existing_leads")),
         settings.get("keap_sync_start_at"), settings.get("keap_sync_start_at")),
    ).rowcount

    # A linked patient belongs to its lead's row; never two Keap contacts for one person.
    pairs = conn.execute(
        "select kl.id as lead_row,kl.keap_contact_id as lead_contact,kl.status as lead_status,"
        "p.stride_id as pid,kp.id as pat_row,kp.keap_contact_id as pat_contact,"
        "kp.status as pat_status from keap_sync kl "
        "join stride_patients p on p.lead_id=kl.lead_id and p.practice_id=kl.practice_id "
        " and p.deleted_at is null "
        "left join keap_sync kp on kp.practice_id=p.practice_id and kp.stride_patient_id=p.stride_id "
        " and kp.id<>kl.id "
        "where kl.practice_id=%s and kl.stride_patient_id is distinct from p.stride_id",
        (practice_id,),
    ).fetchall()
    for pair in pairs:
        # One savepoint per person: a link that breaks a unique index (e.g. one lead tied to
        # two patients) must flag that row, not fail the whole planning tick every minute
        # (review of PR #13, 2026-10-14).
        try:
            with conn.transaction():
                _link_pair(conn, pair, counts)
        except psycopg.errors.UniqueViolation:
            conn.execute(
                "update keap_sync set status='needs_review',last_error=%s where id=%s",
                ("could not link this person's Stride patient (duplicate link)", pair["lead_row"]),
            )
            counts["link_conflicts"] = counts.get("link_conflicts", 0) + 1

    if settings.get("keap_sync_start_at") is not None:
        counts["patients_added"] = conn.execute(
            "insert into keap_sync(practice_id,stride_patient_id) "
            "select p.practice_id,p.stride_id from stride_patients p "
            "where p.practice_id=%s and p.deleted_at is null and p.lead_id is null "
            "and (%s or coalesce(p.stride_created_at,p.created_at)>=%s) "
            "and not exists(select 1 from keap_sync k where k.practice_id=p.practice_id "
            " and k.stride_patient_id=p.stride_id)",
            (
                practice_id,
                settings["keap_include_existing_patients"],
                settings["keap_sync_start_at"],
            ),
        ).rowcount
    return counts


# --- loading and planning ---------------------------------------------------


def _load_context(conn, practice_id, rows, settings, mapping):
    lead_ids = [r["lead_id"] for r in rows if r["lead_id"]]
    patient_ids = [r["stride_patient_id"] for r in rows if r["stride_patient_id"]]
    leads = {
        r["id"]: dict(r)
        for r in conn.execute(
            "select id,full_name,first_name,last_name,email,phone_e164,date_of_birth,status,"
            "is_test,exists(select 1 from leads n where n.practice_id=l.practice_id "
            " and n.phone_e164=l.phone_e164 and n.id<>l.id and n.created_at>l.created_at "
            " and " + _SAME_FIRST_NAME + ") as superseded from leads l where id=any(%s)",
            (lead_ids,),
        )
    }
    patients = {
        r["stride_id"]: dict(r)
        for r in conn.execute(
            "select stride_id,first_name,last_name,date_of_birth,personal_email,work_email,"
            "preferred_email,mobile_phone,home_phone,work_phone,preferred_phone,address_1,"
            "address_2,city,state,zip,gender,deleted_at from stride_patients "
            "where practice_id=%s and stride_id=any(%s)",
            (practice_id, patient_ids),
        )
    }
    appointments: dict[int, list[dict[str, Any]]] = {}
    type_ids = [int(m["stride_appointment_type_id"]) for m in mapping]
    if patient_ids and type_ids:
        for r in conn.execute(
            "select stride_id,patient_stride_id,appointment_type_id,status,start_utc,starts_at_local,"
            "clinician_id,clinician_name,deleted_at from stride_appointments "
            "where practice_id=%s and patient_stride_id=any(%s) and appointment_type_id=any(%s) "
            "and start_utc>=%s",
            (practice_id, patient_ids, type_ids, settings["keap_sync_start_at"]),
        ):
            appointments.setdefault(r["patient_stride_id"], []).append(dict(r))
    # Our own confirmed bookings (Elliot, staff) count at once, keyed ("lead", id): Keap shows the
    # booking the moment it is made, not only after Stride's next export. Before this, a lead
    # booked by Elliot lost Pending and had no booked tag until the export (forever on the demo,
    # whose Stride never exports, 2026-10-14). Stride's copy replaces ours once imported.
    if lead_ids and type_ids:
        for r in conn.execute(
            "select a.lead_id,a.stride_appointment_id,ct.stride_appointment_type_id as appointment_type_id,"
            "a.state,a.start_utc,a.created_at as booked_at,a.clinician_id,c.display_name as clinician_name "
            "from appointments a join case_types ct on ct.id=a.case_type_id "
            "left join clinicians c on c.practice_id=a.practice_id and c.stride_user_id=a.clinician_id "
            "where a.practice_id=%s and a.lead_id=any(%s) and a.start_utc is not null "
            "and a.state in ('scheduled','unknown','completed','cancelled','no_show') "
            "and ct.stride_appointment_type_id=any(%s)",
            (practice_id, lead_ids, type_ids),
        ):
            row = dict(r)
            row.update(status=_OUR_STATE_AS_STRIDE[row.pop("state")], starts_at_local=None,
                       deleted_at=None, stride_id=row["stride_appointment_id"])
            appointments.setdefault(("lead", row["lead_id"]), []).append(row)
    # An upcoming booking the same person still has through an OLDER lead (a repeat referral
    # came in before the visit) counts for the newest lead, so Keap never flips a booked patient
    # back to Pending (decision 2026-10-14: an upcoming booking wins). Keyed ("carried", lead id).
    if lead_ids and type_ids:
        carried_sql = (
            "select n.id as for_lead,{cols} from leads n "
            "join leads l on l.practice_id=n.practice_id and l.phone_e164=n.phone_e164 "
            " and l.id<>n.id and l.created_at<n.created_at and " + _SAME_FIRST_NAME + " {join} "
            "where n.practice_id=%s and n.id=any(%s) and {where}"
        )
        ours = carried_sql.format(
            cols="a.stride_appointment_id as stride_id,ct.stride_appointment_type_id as "
                 "appointment_type_id,a.start_utc,a.created_at as booked_at,a.clinician_id,"
                 "c.display_name as clinician_name,'Unmarked' as status",
            join="join appointments a on a.lead_id=l.id join case_types ct on ct.id=a.case_type_id "
                 "left join clinicians c on c.practice_id=a.practice_id "
                 "and c.stride_user_id=a.clinician_id",
            # Once Stride has its own copy, that copy decides (cancelled, moved): ours is stale.
            where="a.state in ('scheduled','unknown') and a.start_utc>now() "
                  "and ct.stride_appointment_type_id=any(%s) and not exists(select 1 from "
                  "stride_appointments sa where sa.practice_id=a.practice_id "
                  "and sa.stride_id=a.stride_appointment_id)",
        )
        stride = carried_sql.format(
            cols="sa.stride_id,sa.appointment_type_id,sa.start_utc,null::timestamptz as booked_at,"
                 "sa.clinician_id,sa.clinician_name,sa.status",
            join="join stride_patients sp on sp.practice_id=l.practice_id and sp.lead_id=l.id "
                 "and sp.deleted_at is null join stride_appointments sa on "
                 "sa.practice_id=sp.practice_id and sa.patient_stride_id=sp.stride_id",
            where="sa.deleted_at is null and sa.start_utc>now() and sa.start_utc>=%s "
                  "and sa.appointment_type_id=any(%s)",
        )
        for sql, params in (
            (ours, (practice_id, lead_ids, type_ids)),
            (stride, (practice_id, lead_ids, settings["keap_sync_start_at"], type_ids)),
        ):
            for r in conn.execute(sql, params):
                row = dict(r)
                if (row["status"] or "").strip().lower() in CANCELLED_STATUSES:
                    continue
                row.update(starts_at_local=None, deleted_at=None)
                appointments.setdefault(("carried", row.pop("for_lead")), []).append(row)
    clinician_ids = {
        a["clinician_id"] for appts in appointments.values() for a in appts if a["clinician_id"]
    }
    users = {
        r["stride_id"]: f"{r['first_name'] or ''} {r['last_name'] or ''}".strip()
        for r in conn.execute(
            "select stride_id,first_name,last_name from stride_users "
            "where practice_id=%s and stride_id=any(%s)",
            (practice_id, list(clinician_ids)),
        )
    } if clinician_ids else {}
    return leads, patients, appointments, users


def _build_row(row, leads, patients, appointments, users, settings, mapping, now) -> Built:
    patient_id = row["stride_patient_id"]
    stride = appointments.get(patient_id, []) if patient_id else []
    seen = {a["stride_id"] for a in stride}
    ours = [a for a in appointments.get(("lead", row["lead_id"]), []) if a["stride_id"] not in seen]
    seen |= {a["stride_id"] for a in ours}
    carried = [a for a in appointments.get(("carried", row["lead_id"]), [])
               if a["stride_id"] is None or a["stride_id"] not in seen]
    return compute_desired(
        lead=leads.get(row["lead_id"]) if row["lead_id"] else None,
        patient=patients.get(patient_id) if patient_id else None,
        patient_id=patient_id,
        appointments=stride + ours + carried,
        users=users,
        settings=settings,
        mapping=mapping,
        synced_before=row["synced_hash"] is not None,
        now=now,
    )


def build_desired(conn, practice_id: int, row: dict, settings: dict, mapping: list[dict]) -> Built:
    """The desired document for one keap_sync row (or why there is none)."""
    if settings.get("keap_sync_start_at") is None:
        return Built(skip_reason="keap sync not started")
    leads, patients, appointments, users = _load_context(conn, practice_id, [row], settings, mapping)
    return _build_row(row, leads, patients, appointments, users, settings, mapping,
                      datetime.now(UTC))


# updated_at is stamped when a writer's transaction STARTS, but the row is visible only when it
# commits: a change stamped before our last plan can land after it, and was then never planned
# (a Stride booking left a Keap contact with no booked tag, demo 2026-10-14). So changes are
# compared with the plan time minus a grace period; long Stride imports also clear desired_at
# after they commit (stride_import_worker.replan_keap_since).
REPLAN_GRACE = "interval '2 minutes'"
_SELECT_ROWS = (
    "select k.* from keap_sync k "
    "left join leads l on l.id=k.lead_id "
    "left join stride_patients sp on sp.practice_id=k.practice_id and sp.stride_id=k.stride_patient_id "
    "where k.practice_id=%s and ("
    " %s or k.desired_at is null or l.updated_at>k.desired_at-" + REPLAN_GRACE + " "
    " or sp.updated_at>k.desired_at-" + REPLAN_GRACE + " "
    " or exists(select 1 from stride_appointments a where a.practice_id=k.practice_id "
    "   and a.patient_stride_id=k.stride_patient_id "
    "   and (a.updated_at>k.desired_at-" + REPLAN_GRACE + " "
    "        or (a.start_utc>k.desired_at and a.start_utc<=now()))) "
    " or exists(select 1 from appointments ap where ap.lead_id=k.lead_id "
    "   and ap.updated_at>k.desired_at-" + REPLAN_GRACE + ")) "
    "and (%s::timestamptz is null or k.desired_at is null or k.desired_at<%s) "
    "order by k.desired_at nulls first,k.id limit %s"
)


def plan_changes(conn, practice_id: int, *, full: bool = False, limit: int | None = None) -> dict[str, int]:
    """Recompute desired for changed people and queue the rows whose hash changed."""
    batch = limit or get_settings().keap_plan_batch
    counts = {"recomputed": 0, "queued": 0, "skipped": 0, "failed": 0, "frozen": 0}
    counts.update(attach_people(conn, practice_id))
    settings = load_settings(conn, practice_id)
    if not settings:
        return counts
    mapping = load_mapping(conn, practice_id)
    run_started = conn.execute("select now() as t").fetchone()["t"] if full else None
    now = datetime.now(UTC)
    queued_patient_rows = 0
    while True:
        rows = conn.execute(
            _SELECT_ROWS, (practice_id, full, run_started, run_started, batch)
        ).fetchall()
        if not rows:
            break
        leads, patients, appointments, users = _load_context(
            conn, practice_id, rows, settings, mapping
        ) if settings["keap_sync_start_at"] is not None else ({}, {}, {}, {})
        for row in rows:
            try:
                with conn.transaction():
                    built = _build_row(
                        row, leads, patients, appointments, users, settings, mapping, now
                    )
                    outcome = _apply(conn, row, built, queued_patient_rows)
            except Exception as error:  # noqa: BLE001 - one bad person must not stop the rest
                conn.execute(
                    "update keap_sync set status='failed',last_error=%s,desired_at=now() where id=%s",
                    (f"planning error: {type(error).__name__}", row["id"]),
                )
                outcome = "failed"
            counts["recomputed"] += 1
            if outcome == "queued":
                counts["queued"] += 1
                if row["lead_id"] is None:
                    queued_patient_rows += 1
            elif outcome in counts:
                counts[outcome] += 1
        if not full or len(rows) < batch:
            break
    return counts


def _apply(conn, row, built: Built, queued_patient_rows: int) -> str:
    if built.freeze:
        conn.execute("update keap_sync set desired_at=now() where id=%s", (row["id"],))
        return "frozen"
    if built.skip_reason:
        conn.execute(
            "update keap_sync set status=case when status='syncing' then status else 'skipped' end,"
            "skip_reason=%s,desired_at=now() where id=%s",
            (built.skip_reason, row["id"]),
        )
        return "skipped"
    new_hash = desired_hash(built.desired)
    if new_hash == row["desired_hash"]:
        conn.execute(
            "update keap_sync set desired_at=now(),skip_reason=null,"
            "status=case when status='skipped' then 'pending' else status end where id=%s",
            (row["id"],),
        )
        return "unchanged"
    # A bulk first load goes out slowly so new leads never wait behind it.
    delay_minutes = (queued_patient_rows // BULK_THRESHOLD) if row["lead_id"] is None else 0
    conn.execute(
        "update keap_sync set desired=%s,desired_hash=%s,desired_at=now(),skip_reason=null,"
        "last_error=case when status='needs_review' then last_error end,status=case when status in ('syncing','needs_review') then status "
        " when %s=synced_hash then 'synced' else 'pending' end,"
        "attempts=case when status in ('syncing','needs_review') then attempts else 0 end,"
        "next_attempt_at=case when status in ('syncing','needs_review') then next_attempt_at "
        " else now()+make_interval(mins=>%s) end where id=%s",
        (json.dumps(built.desired), new_hash, new_hash, delay_minutes, row["id"]),
    )
    return "queued"
