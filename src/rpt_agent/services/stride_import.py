"""Apply one Stride CSV export file to the stride_* mirror tables.

Stride drops `<entity>_<YYYYMMDDHHMMSS>.csv` files over SFTP: a full load first,
then only rows changed since the previous export. Each row says Upsert or
Delete. This module parses one file and applies it inside the caller's
transaction; the worker owns picking files up and moving them afterwards.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, TextIO
from zoneinfo import ZoneInfo

FILE_NAME = re.compile(r"^(?P<entity>[a-z_]+)_(?P<stamp>\d{14})\.csv$")

# Parents before children, so a file set that arrives together reads naturally.
ENTITY_ORDER = (
    "locations",
    "users",
    "providers",
    "payers",
    "patients",
    "patient_cases",
    "patient_insurances",
    "appointments",
    "notes",
)

# Stride wording plus the API's one-letter codes, in case an export uses them.
CANCELLED_STATUSES = {"cancel", "cancelled", "late cancel", "a", "l"}
LATE_CANCEL_STATUSES = {"late cancel", "l"}
NO_SHOW_STATUSES = {"no show", "n"}


class StrideFileError(ValueError):
    """The whole file cannot be used (bad name, missing columns, unreadable)."""


class RowError(ValueError):
    """One row cannot be used; the rest of the file still applies."""


@dataclass
class ApplyResult:
    row_count: int = 0
    applied: int = 0
    unchanged: int = 0
    issues: list[dict[str, Any]] = field(default_factory=list)

    def issue(self, row_number: int, stride_id: Any, reason: str) -> None:
        # PHI-free on purpose: this lands in the database and the logs.
        self.issues.append({"row": row_number, "stride_id": stride_id, "reason": reason[:200]})


def parse_file_name(name: str) -> tuple[str, datetime]:
    match = FILE_NAME.match(name)
    if not match or match["entity"] not in ENTITY_ORDER:
        raise StrideFileError("unrecognized file name")
    try:
        exported_at = datetime.strptime(match["stamp"], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError as exc:
        raise StrideFileError("file name has an invalid timestamp") from exc
    return match["entity"], exported_at


def file_sort_key(name: str) -> tuple[str, int, str]:
    """Oldest export first; within one export, parents before children."""
    match = FILE_NAME.match(name)
    if not match or match["entity"] not in ENTITY_ORDER:
        return ("", -1, name)
    return (match["stamp"], ENTITY_ORDER.index(match["entity"]), name)


# --- value parsers ---------------------------------------------------------


def _text(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _int(value: str | None) -> int | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise RowError(f"not a whole number: {text[:20]}") from exc


def _bool(value: str | None) -> bool | None:
    text = (_text(value) or "").lower()
    if text in {"true", "t", "yes", "y", "1"}:
        return True
    if text in {"false", "f", "no", "n", "0"}:
        return False
    return None


def _date(value: str | None) -> date | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise RowError("invalid date") from exc


def _timestamp(value: str | None) -> datetime | None:
    """Stride date-times: `YYYY-MM-DD HH:MM:SS` (UTC) or with an explicit offset."""
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RowError("invalid date-time") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _local_time(day: date, value: str | None) -> datetime | None:
    text = _text(value)
    if text is None:
        return None
    for pattern in ("%I:%M %p", "%I:%M:%S %p", "%H:%M", "%H:%M:%S"):
        try:
            # Only the clock is used; the caller applies the clinic timezone.
            clock = datetime.strptime(text.upper(), pattern).time()  # noqa: DTZ007
        except ValueError:
            continue
        return datetime.combine(day, clock)
    raise RowError("invalid time")


def normalize_phone(value: str | None) -> str | None:
    """US numbers to E.164; anything else is left out (the original stays in raw)."""
    text = _text(value)
    if text is None:
        return None
    digits = re.sub(r"\D", "", text)
    if text.startswith("+") and 8 <= len(digits) <= 15 and digits[0] != "0":
        return f"+{digits}"
    if len(digits) == 10 and digits[0] not in "01":
        return f"+1{digits}"
    if len(digits) == 11 and digits[0] == "1" and digits[1] not in "01":
        return f"+{digits}"
    return None


def _preferred(code: str | None, options: dict[str, str | None]) -> str | None:
    return options.get((_text(code) or "").upper())


# --- per-entity column mapping --------------------------------------------


def _patient_columns(row: dict[str, str], _tz: ZoneInfo) -> dict[str, Any]:
    personal = _text(row.get("Personal Email"))
    work = _text(row.get("Work Email"))
    mobile = normalize_phone(row.get("Mobile Phone Number"))
    home = normalize_phone(row.get("Home Phone Number"))
    work_phone = normalize_phone(row.get("Work Phone Number"))
    return {
        "external_id": _text(row.get("External Id")),
        "first_name": _text(row.get("First Name")),
        "last_name": _text(row.get("Last Name")),
        "date_of_birth": _date(row.get("Date Of Birth")),
        "gender": _text(row.get("Gender")),
        "personal_email": personal,
        "work_email": work,
        "preferred_email": _preferred(row.get("Preferred Email"), {"P": personal, "W": work}),
        "mobile_phone": mobile,
        "home_phone": home,
        "work_phone": work_phone,
        "preferred_phone": _preferred(
            row.get("Preferred Phone Number"), {"M": mobile, "H": home, "W": work_phone}
        ),
        "address_1": _text(row.get("Address 1")),
        "address_2": _text(row.get("Address 2")),
        "city": _text(row.get("City")),
        "state": _text(row.get("State")),
        "zip": _text(row.get("Zip")),
        "marketing_opt_in": _bool(row.get("Marketing Opt-in")),
        "referral_source": _text(row.get("Referral Source")),
        "first_appointment_at": _timestamp(row.get("First Appointment")),
        "latest_appointment_at": _timestamp(row.get("Latest Appointment")),
    }


def _case_columns(row: dict[str, str], _tz: ZoneInfo) -> dict[str, Any]:
    return {
        "patient_stride_id": _int(row.get("Patient Id")),
        "title": _text(row.get("Title")),
        "status": _text(row.get("Status")),
        "referring_provider_id": _int(row.get("Referring Provider Id")),
        "referring_provider_name": _text(row.get("Referring Provider Name")),
        "last_appointment_at": _timestamp(row.get("Last Appointment")),
    }


def _appointment_slot(row: dict[str, str], tz: ZoneInfo) -> dict[str, Any]:
    day = _date(row.get("Date"))
    if day is None:
        raise RowError("missing appointment date")
    starts = _local_time(day, row.get("Start Time"))
    if starts is None:
        raise RowError("missing appointment start time")
    ends = _local_time(day, row.get("End Time"))
    return {
        "case_stride_id": _int(row.get("Case Id")),
        "starts_at_local": starts,
        "ends_at_local": ends,
        "start_utc": starts.replace(tzinfo=tz).astimezone(UTC),
        "end_utc": ends.replace(tzinfo=tz).astimezone(UTC) if ends else None,
        "appointment_type_id": _int(row.get("Appointment Type Id")),
        "location_id": _int(row.get("Location Id")),
        "clinician_id": _int(row.get("Primary Attendee Id")),
    }


def _appointment_columns(row: dict[str, str], tz: ZoneInfo) -> dict[str, Any]:
    return {
        "patient_stride_id": _int(row.get("Patient Id")),
        **_appointment_slot(row, tz),
        "appointment_type": _text(row.get("Appointment Type")),
        "category": _text(row.get("Category")),
        "status": _text(row.get("Status")),
        "location_name": _text(row.get("Location Name")),
        "clinician_name": _text(row.get("Primary Attendee Name")),
        "place_of_service": _text(row.get("Place of Service")),
        "payment_type": _text(row.get("Payment Type")),
    }


def _user_columns(row: dict[str, str], _tz: ZoneInfo) -> dict[str, Any]:
    return {
        "first_name": _text(row.get("First Name")),
        "last_name": _text(row.get("Last Name")),
        "email": _text(row.get("Email")),
        "credentials": _text(row.get("Credentials")),
        "npi": _text(row.get("NPI")),
        "is_active": _bool(row.get("Active")),
    }


def _location_columns(row: dict[str, str], _tz: ZoneInfo) -> dict[str, Any]:
    return {
        "name": _text(row.get("Name")),
        "address_1": _text(row.get("Address 1")),
        "address_2": _text(row.get("Address 2")),
        "city": _text(row.get("City")),
        "state": _text(row.get("State")),
        "zip": _text(row.get("Zip Code")),
        "phone": _text(row.get("Phone Number")),
        "is_active": _bool(row.get("Is Active")),
    }


def _other_columns(row: dict[str, str], _tz: ZoneInfo) -> dict[str, Any]:
    return {"patient_stride_id": _int(row.get("Patient Id"))}


@dataclass(frozen=True)
class EntitySpec:
    table: str
    id_column: str
    required: frozenset[str]
    columns: Callable[[dict[str, str], ZoneInfo], dict[str, Any]]
    fields: tuple[str, ...]  # exactly the keys `columns` returns, in table column order
    other_entity: str | None = None  # stride_other_records.entity


_COMMON = {"Action", "Modified Date Time"}
_APPOINTMENT_SLOT = (
    "case_stride_id", "starts_at_local", "ends_at_local", "start_utc", "end_utc",
    "appointment_type_id", "location_id", "clinician_id",
)
ENTITIES: dict[str, EntitySpec] = {
    "patients": EntitySpec(
        "stride_patients", "Id",
        frozenset(_COMMON | {"Id", "First Name", "Last Name", "Date Of Birth"}), _patient_columns,
        (
            "external_id", "first_name", "last_name", "date_of_birth", "gender", "personal_email",
            "work_email", "preferred_email", "mobile_phone", "home_phone", "work_phone",
            "preferred_phone", "address_1", "address_2", "city", "state", "zip", "marketing_opt_in",
            "referral_source", "first_appointment_at", "latest_appointment_at",
        ),
    ),
    "patient_cases": EntitySpec(
        "stride_cases", "Id", frozenset(_COMMON | {"Id", "Patient Id", "Title"}), _case_columns,
        ("patient_stride_id", "title", "status", "referring_provider_id", "referring_provider_name",
         "last_appointment_at"),
    ),
    "appointments": EntitySpec(
        "stride_appointments", "Appointment Id",
        frozenset(_COMMON | {
            "Appointment Id", "Patient Id", "Case Id", "Date", "Start Time", "End Time", "Status",
            "Appointment Type",
        }),
        _appointment_columns,
        ("patient_stride_id", *_APPOINTMENT_SLOT, "appointment_type", "category", "status",
         "location_name", "clinician_name", "place_of_service", "payment_type"),
    ),
    "users": EntitySpec(
        "stride_users", "Id", frozenset(_COMMON | {"Id"}), _user_columns,
        ("first_name", "last_name", "email", "credentials", "npi", "is_active"),
    ),
    "locations": EntitySpec(
        "stride_locations", "Id", frozenset(_COMMON | {"Id"}), _location_columns,
        ("name", "address_1", "address_2", "city", "state", "zip", "phone", "is_active"),
    ),
    "notes": EntitySpec(
        "stride_other_records", "Note Id", frozenset(_COMMON | {"Note Id"}), _other_columns,
        ("patient_stride_id",), "notes",
    ),
    "patient_insurances": EntitySpec(
        "stride_other_records", "Patient Insurance Id",
        frozenset(_COMMON | {"Patient Insurance Id"}), _other_columns, ("patient_stride_id",),
        "patient_insurances",
    ),
    "payers": EntitySpec(
        "stride_other_records", "Insurance Id", frozenset(_COMMON | {"Insurance Id"}), _other_columns,
        ("patient_stride_id",), "payers",
    ),
    "providers": EntitySpec(
        "stride_other_records", "Id", frozenset(_COMMON | {"Id"}), _other_columns,
        ("patient_stride_id",), "providers",
    ),
}


# --- reading ---------------------------------------------------------------


def open_rows(stream: TextIO, entity: str) -> csv.DictReader:
    """A row-by-row reader; rejects the file if a required column is missing.

    Rows are never all held in memory: a multi-gigabyte export reads like a small one.
    """
    reader = csv.DictReader(stream)
    header = [name.strip() for name in (reader.fieldnames or [])]
    if not header:
        raise StrideFileError("file is empty or has no header row")
    missing = ENTITIES[entity].required - set(header)
    if missing:
        raise StrideFileError(f"missing columns: {', '.join(sorted(missing))}")
    reader.fieldnames = header
    return reader


def read_rows(content: bytes, entity: str) -> list[dict[str, str]]:
    """Small in-memory helper (tests, ad-hoc checks); the worker streams with open_rows."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252", errors="replace")
    try:
        return list(open_rows(io.StringIO(text, newline=""), entity))
    except csv.Error as exc:
        raise StrideFileError(f"CSV could not be parsed: {exc}") from exc


def _parse_row(spec: EntitySpec, row: dict[str, str], tz: ZoneInfo) -> tuple[str, dict[str, Any]]:
    """One CSV row to (action, column values). Raises RowError for a row we cannot use."""
    if None in row or any(value is None for value in row.values()):
        raise RowError("row has a different number of columns than the header")
    action = (_text(row.get("Action")) or "").lower()
    if action not in {"upsert", "delete"}:
        raise RowError("Action must be Upsert or Delete")
    modified = _timestamp(row.get("Modified Date Time"))
    if modified is None:
        raise RowError("missing Modified Date Time")
    values: dict[str, Any] = {"stride_id": _int(row.get(spec.id_column)), "stride_modified_at": modified}
    if action == "upsert":
        if values["stride_id"] is None:
            raise RowError(f"missing {spec.id_column}")
        values.update(spec.columns(row, tz))
        values["stride_created_at"] = _timestamp(row.get("Created Date Time"))
        values["raw"] = json.dumps(row, separators=(",", ":"))
    elif values["stride_id"] is None:
        if spec.table != "stride_appointments":
            raise RowError(f"delete is missing {spec.id_column}")
        values.update(_appointment_slot(row, tz))
        if values["case_stride_id"] is None:
            raise RowError("delete has neither Appointment Id nor Case Id")
    return action, values


# --- applying --------------------------------------------------------------
#
# Standard bulk-load pattern: stream every valid row into a temporary staging
# table with COPY, then merge into the real table with a few set-based
# statements. All of it runs in the caller's single transaction, so a file is
# applied completely or not at all, whatever its size.


def _key(spec: EntitySpec) -> str:
    return "practice_id,entity,stride_id" if spec.other_entity else "practice_id,stride_id"


def _key_values(spec: EntitySpec) -> str:
    return "%(practice_id)s,%(entity)s,s.stride_id" if spec.other_entity else "%(practice_id)s,s.stride_id"


def _resolve_slot_deletes(conn, params: dict[str, Any], result: ApplyResult) -> None:
    """Stride's appointment Delete rows carry no Appointment Id.

    Stride allows one appointment per case per start time ("Appointment
    Date+Case combination already exists"), so case + local start finds it, in
    the table or earlier in this file. Type, clinician and location narrow it
    when present. Never guess: no match or several matches is reported.
    """
    def same_slot(t: str) -> str:
        return (
            f"{t}.case_stride_id=d2.case_stride_id and {t}.starts_at_local=d2.starts_at_local "
            f"and (d2.appointment_type_id is null or {t}.appointment_type_id is null "
            f" or {t}.appointment_type_id=d2.appointment_type_id) "
            f"and (d2.clinician_id is null or {t}.clinician_id is null or {t}.clinician_id=d2.clinician_id) "
            f"and (d2.location_id is null or {t}.location_id is null or {t}.location_id=d2.location_id)"
        )

    conn.execute(
        "create temp table _slot_matches on commit drop as "
        "select d.seq,d.row_no,count(distinct c.stride_id) as matches,min(c.stride_id) as stride_id "
        "from _stage d left join ("
        " select d2.seq,t.stride_id from _stage d2 join stride_appointments t "
        f"  on t.practice_id=%(practice_id)s and t.deleted_at is null and {same_slot('t')} "
        "  where d2.action='delete' and d2.stride_id is null "
        " union "
        f" select d2.seq,u.stride_id from _stage d2 join _stage u on u.action='upsert' and u.seq<d2.seq "
        f"  and {same_slot('u')} where d2.action='delete' and d2.stride_id is null"
        ") c on c.seq=d.seq where d.action='delete' and d.stride_id is null group by d.seq,d.row_no",
        params,
    )
    for row in conn.execute("select row_no,matches from _slot_matches where matches<>1 order by row_no"):
        result.issue(row["row_no"], None, "delete_unmatched" if row["matches"] == 0 else "delete_ambiguous")
    conn.execute(
        "update _stage s set stride_id=m.stride_id from _slot_matches m where s.seq=m.seq and m.matches=1"
    )


def apply_file(
    conn,
    entity: str,
    stream: TextIO,
    *,
    practice_id: int,
    import_file_id: int,
    timezone: str,
    max_bad_row_percent: float = 1.0,
    progress: Callable[[int], None] | None = None,
    progress_every: int = 50_000,
) -> ApplyResult:
    """Apply one export file inside the caller's transaction (all or nothing)."""
    spec = ENTITIES[entity]
    tz = ZoneInfo(timezone)
    result = ApplyResult()
    reader = open_rows(stream, entity)
    stage_cols = ("seq", "row_no", "action", "stride_id", "stride_modified_at", "stride_created_at", "raw",
                  *spec.fields)
    select_cols = ",".join(stage_cols[3:])
    # Same column types as the real table, none of its constraints.
    conn.execute(
        f"create temp table _stage on commit drop as select 0::bigint as seq,0 as row_no,''::text as action,"
        f"{select_cols} from {spec.table} with no data"
    )
    valid = 0
    with conn.cursor() as cursor, cursor.copy(f"copy _stage ({','.join(stage_cols)}) from stdin") as copy:
        try:
            for number, row in enumerate(reader, start=2):  # row 1 is the header
                result.row_count += 1
                try:
                    action, values = _parse_row(spec, row, tz)
                except RowError as exc:
                    result.issue(number, _text(row.get(spec.id_column)), str(exc))
                    continue
                valid += 1
                copy.write_row((valid, number, action, *(values.get(name) for name in stage_cols[3:])))
                if progress and result.row_count % progress_every == 0:
                    progress(result.row_count)
        except csv.Error as exc:
            raise StrideFileError(f"CSV could not be parsed near row {result.row_count + 2}: {exc}") from exc

    bad = len(result.issues)
    if result.row_count and bad * 100 > max_bad_row_percent * result.row_count:
        raise StrideFileError(
            f"too many bad rows: {bad} of {result.row_count} (limit {max_bad_row_percent:g}%)"
        )

    params = {"practice_id": practice_id, "entity": spec.other_entity, "import_file_id": import_file_id}
    if spec.table == "stride_appointments":
        _resolve_slot_deletes(conn, params, result)
    # Within one file only each record's latest change matters (Modified Date
    # Time, then file order); earlier versions count as unchanged.
    conn.execute(
        "create temp table _latest on commit drop as select distinct on (stride_id) * from _stage "
        "where stride_id is not null order by stride_id,stride_modified_at desc,seq desc"
    )
    data_cols = [*spec.fields, "stride_created_at", "stride_modified_at", "raw", "import_file_id"]
    source_cols = [f"s.{name}" for name in data_cols[:-1]] + ["%(import_file_id)s"]
    updates = ",".join(f"{name}=excluded.{name}" for name in data_cols)
    # An older row never wins, and an identical re-send is not a change:
    # updated_at is the "changed since" cursor for later sync work.
    upserted = conn.execute(
        f"insert into {spec.table} as t({_key(spec)},{','.join(data_cols)}) "
        f"select {_key_values(spec)},{','.join(source_cols)} from _latest s where s.action='upsert' "
        f"on conflict({_key(spec)}) do update set {updates},deleted_at=null,updated_at=now() "
        "where (t.stride_modified_at is null or excluded.stride_modified_at>=t.stride_modified_at) "
        "and (t.raw is distinct from excluded.raw or t.deleted_at is not null)",
        params,
    ).rowcount
    deleted = conn.execute(
        f"insert into {spec.table} as t({_key(spec)},stride_modified_at,deleted_at,import_file_id) "
        f"select {_key_values(spec)},s.stride_modified_at,now(),%(import_file_id)s from _latest s "
        f"where s.action='delete' on conflict({_key(spec)}) do update set deleted_at=now(),"
        "stride_modified_at=excluded.stride_modified_at,import_file_id=excluded.import_file_id,"
        "updated_at=now() where t.deleted_at is null and (t.stride_modified_at is null "
        "or excluded.stride_modified_at>=t.stride_modified_at)",
        params,
    ).rowcount
    result.applied = max(upserted, 0) + max(deleted, 0)
    result.unchanged = valid - result.applied - (len(result.issues) - bad)
    return result


def is_cancelled(status: str | None) -> bool:
    return (status or "").strip().lower() in CANCELLED_STATUSES
