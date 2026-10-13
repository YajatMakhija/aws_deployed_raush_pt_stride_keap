"""Stride CSV import end to end against a real Postgres schema.

Needs TEST_DATABASE_URL (a disposable database with the migrations applied,
never production). Each test builds its own practice, synthetic patients and
555 leads in temporary inbox/archive/error folders, and removes them afterwards.
"""

import csv
import io
import os
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

import rpt_agent.stride_import_worker as worker
from rpt_agent.config import get_settings
from rpt_agent.db import get_pool

pytestmark = pytest.mark.integration

# Column names exactly as Stride exports them (sample export 2026-04-09).
PATIENT_HEADER = [
    "Id", "External Id", "First Name", "Last Name", "Date Of Birth", "Address 1", "Address 2", "City",
    "State", "Zip", "Marital Status", "Gender", "Occupation", "First Appointment", "Latest Appointment",
    "Personal Email", "Work Email", "Preferred Email", "Mobile Phone Number", "Home Phone Number",
    "Work Phone Number", "Preferred Phone Number", "Emergency Contact Name",
    "Emergency Contact Relationship", "Emergency Contact Phone Number", "Referral Source",
    "Referral Details", "Primary Care Provider Id", "Primary Care Provider Name",
    "Primary Care Provider NPI", "Created Date Time", "Modified Date Time", "Action",
    "Marketing Opt-in",
]
APPOINTMENT_HEADER = [
    "Patient Id", "Patient Name", "Patient Date Of Birth", "Case Id", "Case Title", "Appointment Id",
    "Category", "Appointment Type", "Appointment Type Id", "Subject", "Payment Type", "Date",
    "Start Time", "End Time", "Status", "Place of Service", "Location Id", "Location Name",
    "Primary Attendee Id", "Primary Attendee Name", "Details", "Created Date Time",
    "Modified Date Time", "Action",
]


def csv_bytes(header, rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=header, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in header})
    return out.getvalue().encode()


def patient(pid, first="Test", last="Patient", dob="1990-01-01", modified="2026-10-01 10:00:00",
            action="Upsert", mobile="(555) 201-0000", **extra):
    return {"Id": pid, "First Name": first, "Last Name": last, "Date Of Birth": dob,
            "Mobile Phone Number": mobile, "Preferred Phone Number": "M", "Marketing Opt-in": "True",
            "Created Date Time": "2026-10-01 09:00:00", "Modified Date Time": modified,
            "Action": action, **extra}


def appointment(aid, pid, case_id, day, start="10:00 AM", end="11:00 AM", status="Unmarked",
                modified="2026-10-01 10:00:00", action="Upsert", **extra):
    return {"Patient Id": pid, "Case Id": case_id, "Appointment Id": aid, "Category": "Patient Appointment",
            "Appointment Type": "Initial Evaluation", "Appointment Type Id": "2", "Date": day,
            "Start Time": start, "End Time": end, "Status": status, "Location Id": "2",
            "Location Name": "Clinic", "Primary Attendee Id": "3", "Primary Attendee Name": "Clinician A",
            "Created Date Time": "2026-10-01 09:00:00", "Modified Date Time": modified,
            "Action": action, **extra}


def future_day(days=7):
    return (datetime.now(UTC).date() + timedelta(days=days)).isoformat()


@pytest.fixture
def env(tmp_path, monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    slug = f"test-stride-{uuid4().hex[:10]}"
    conn = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    practice = conn.execute(
        "insert into practices(name,slug,timezone) values('Synthetic Practice',%s,'America/New_York') "
        "returning id",
        (slug,),
    ).fetchone()["id"]
    conn.execute(
        "insert into practice_settings(practice_id,stride_location_timezone) values(%s,'America/New_York')",
        (practice,),
    )
    folders = {name: tmp_path / name for name in ("incoming", "archive", "error")}
    folders["incoming"].mkdir()
    for key, value in {
        "SUPABASE_DB_URL": url,
        "STRIDE_IMPORT_ENABLED": "true",
        "STRIDE_PRACTICE_SLUG": slug,
        "STRIDE_INBOX_DIR": str(folders["incoming"]),
        "STRIDE_ARCHIVE_DIR": str(folders["archive"]),
        "STRIDE_ERROR_DIR": str(folders["error"]),
        "STRIDE_FILE_SETTLE_SECONDS": "60",
        "SHEET_SYNC_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    get_pool.cache_clear()

    class Env:
        db = conn
        practice_id = practice

        def drop(self, name, content, age_seconds=120):
            path = folders["incoming"] / name
            path.write_bytes(content)
            old = time.time() - age_seconds
            os.utime(path, (old, old))
            return path

        def tick(self):
            return worker.run_stride_import_tick()

        def files(self, folder):
            return sorted(p.name for p in folders[folder].rglob("*") if p.is_file())

        def one(self, sql, *args):
            return conn.execute(sql, args).fetchone()

        def lead(self, name, dob="1990-01-01", phone="+15552010000", status="in_progress"):
            lead_id = conn.execute(
                "insert into leads(practice_id,source_system,full_name,first_name,last_name,phone_e164,"
                "date_of_birth,status,cadence_state,is_test) values(%s,'google_sheets',%s,%s,%s,%s,%s,%s,"
                "'active',true) returning id",
                (practice, name, name.split()[0], name.split()[-1], phone, dob, status),
            ).fetchone()["id"]
            for attempt in (1, 2):
                conn.execute(
                    "insert into outreach_events(lead_id,attempt_no,channel,status,scheduled_for) "
                    "values(%s,%s,'call','planned',now()+interval '1 day')",
                    (lead_id, attempt),
                )
            return lead_id

    yield Env()

    lead_ids = [r["id"] for r in conn.execute("select id from leads where practice_id=%s", (practice,))]
    conn.execute("delete from integration_outbox where aggregate_id=any(%s)", ([str(i) for i in lead_ids],))
    for table in ("stride_patients", "stride_cases", "stride_appointments", "stride_users",
                  "stride_locations", "stride_other_records", "stride_import_files"):
        conn.execute(f"delete from {table} where practice_id=%s", (practice,))
    conn.execute("delete from lead_status_history where lead_id=any(%s)", (lead_ids,))
    conn.execute("delete from outreach_events where lead_id=any(%s)", (lead_ids,))
    conn.execute("delete from leads where practice_id=%s", (practice,))
    conn.execute("delete from practice_settings where practice_id=%s", (practice,))
    conn.execute("delete from practices where id=%s", (practice,))
    conn.execute("delete from service_heartbeats where service='stride-worker'")
    conn.close()
    get_pool.cache_clear()
    get_settings.cache_clear()


# --- file handling ---------------------------------------------------------


def test_full_load_is_saved_and_archived(env):
    env.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [patient("101"), patient("102")]))
    env.drop("appointments_20261001100000.csv",
             csv_bytes(APPOINTMENT_HEADER, [appointment("9001", "101", "7001", "2026-07-15")]))
    assert env.tick()["processed"] == 2
    assert env.files("archive") and not env.files("incoming")
    p = env.one("select mobile_phone,preferred_phone,marketing_opt_in,raw from stride_patients "
                "where practice_id=%s and stride_id=101", env.practice_id)
    assert p["mobile_phone"] == p["preferred_phone"] == "+15552010000"
    assert p["marketing_opt_in"] is True
    assert p["raw"]["Mobile Phone Number"] == "(555) 201-0000"
    a = env.one("select starts_at_local,start_utc from stride_appointments where practice_id=%s "
                "and stride_id=9001", env.practice_id)
    assert a["starts_at_local"] == datetime.fromisoformat("2026-07-15T10:00")  # clinic wall time
    assert a["start_utc"] == datetime(2026, 7, 15, 14, 0, tzinfo=UTC)  # EDT is UTC-4


def test_daylight_saving_switch_uses_the_right_offset(env):
    env.drop("appointments_20261001100000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("1", "101", "7001", "2026-03-07"),  # EST, UTC-5
        appointment("2", "101", "7001", "2026-03-09"),  # EDT, UTC-4
    ]))
    env.tick()
    rows = env.db.execute("select stride_id,start_utc from stride_appointments where practice_id=%s "
                          "order by stride_id", (env.practice_id,)).fetchall()
    assert [r["start_utc"].astimezone(UTC).hour for r in rows] == [15, 14]


def test_file_still_uploading_is_left_alone(env):
    env.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [patient("101")]), age_seconds=5)
    assert env.tick().get("processed") is None
    assert env.files("incoming") == ["patients_20261001100000.csv"]


def test_same_file_twice_is_skipped(env):
    content = csv_bytes(PATIENT_HEADER, [patient("101")])
    env.drop("patients_20261001100000.csv", content)
    env.tick()
    before = env.one("select updated_at from stride_patients where practice_id=%s and stride_id=101",
                     env.practice_id)["updated_at"]
    env.drop("patients_20261001100000.csv", content)
    assert env.tick()["duplicate"] == 1
    after = env.one("select updated_at from stride_patients where practice_id=%s and stride_id=101",
                    env.practice_id)["updated_at"]
    assert after == before
    assert len(env.files("archive")) == 2


def test_identical_row_in_a_new_file_is_not_a_change(env):
    env.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [patient("101")]))
    env.tick()
    env.drop("patients_20261001110000.csv", csv_bytes(PATIENT_HEADER, [patient("101")]))
    env.tick()
    f = env.one("select applied_count,unchanged_count from stride_import_files where practice_id=%s "
                "and file_name='patients_20261001110000.csv'", env.practice_id)
    assert (f["applied_count"], f["unchanged_count"]) == (0, 1)


def test_older_file_arriving_late_cannot_overwrite_newer_data(env):
    env.drop("patients_20261001120000.csv", csv_bytes(PATIENT_HEADER, [
        patient("101", mobile="(555) 201-9999", modified="2026-10-01 12:00:00")]))
    env.tick()
    env.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [
        patient("101", mobile="(555) 201-0000", modified="2026-10-01 10:00:00")]))
    env.tick()
    assert env.one("select mobile_phone from stride_patients where practice_id=%s and stride_id=101",
                   env.practice_id)["mobile_phone"] == "+15552019999"


def test_same_record_twice_in_one_file_keeps_the_latest(env):
    env.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [
        patient("101", mobile="(555) 201-2222", modified="2026-10-01 11:00:00"),
        patient("101", mobile="(555) 201-1111", modified="2026-10-01 10:00:00"),
    ]))
    env.tick()
    assert env.one("select mobile_phone from stride_patients where practice_id=%s and stride_id=101",
                   env.practice_id)["mobile_phone"] == "+15552012222"


def _good_patients(count, start=1000):
    return [patient(str(start + i), first=f"P{i}") for i in range(count)]


def test_a_few_bad_rows_are_skipped_and_reported_without_patient_data(env):
    bad = [patient("102", dob="31/31/1990"), patient("103", action="True")]
    # 2 bad of 202 rows is under the 1% limit: skip them, load the rest.
    env.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [*_good_patients(200), *bad]))
    env.tick()
    f = env.one("select status,applied_count,issue_count,issues from stride_import_files "
                "where practice_id=%s", env.practice_id)
    assert (f["status"], f["applied_count"], f["issue_count"]) == ("processed", 200, 2)
    assert {i["reason"] for i in f["issues"]} == {"invalid date", "Action must be Upsert or Delete"}
    assert "P1" not in str(f["issues"])


def test_more_than_one_percent_bad_rows_rejects_the_whole_file(env):
    bad = [patient("102", dob="31/31/1990"), patient("103", dob="1990-13-01"), patient("")]
    env.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [*_good_patients(100), *bad]))
    assert env.tick()["rejected"] == 1
    assert env.one("select count(*) n from stride_patients where practice_id=%s", env.practice_id)["n"] == 0
    f = env.one("select status,error from stride_import_files where practice_id=%s", env.practice_id)
    assert f["status"] == "rejected" and f["error"].startswith("too many bad rows: 3 of 103")
    assert env.files("error") == ["patients_20261001100000.csv"]


def test_row_with_extra_columns_is_reported(env):
    content = csv_bytes(PATIENT_HEADER, _good_patients(150)) + b"102,x,A,B,1990-01-01" + b",x" * 40 + b"\n"
    env.drop("patients_20261001100000.csv", content)
    env.tick()
    f = env.one("select applied_count,issues from stride_import_files where practice_id=%s", env.practice_id)
    assert f["applied_count"] == 150
    assert f["issues"][0]["reason"].startswith("row has a different number of columns")


def test_missing_column_sends_the_file_to_error(env):
    header = [c for c in PATIENT_HEADER if c != "Date Of Birth"]
    env.drop("patients_20261001100000.csv", csv_bytes(header, [patient("101")]))
    assert env.tick()["rejected"] == 1
    assert env.files("error") == ["patients_20261001100000.csv"]
    f = env.one("select status,error from stride_import_files where practice_id=%s", env.practice_id)
    assert f["status"] == "rejected" and "Date Of Birth" in f["error"]


def test_unknown_file_name_goes_to_error_and_is_recorded(env):
    env.drop("perform_demo_patients (1).csv", csv_bytes(PATIENT_HEADER, [patient("101")]))
    assert env.tick()["rejected"] == 1
    assert env.files("error") == ["perform_demo_patients (1).csv"]
    assert env.one("select status,entity from stride_import_files where practice_id=%s",
                   env.practice_id) == {"status": "rejected", "entity": "unknown"}


def test_file_over_the_size_limit_is_not_read(env, monkeypatch):
    monkeypatch.setenv("STRIDE_MAX_FILE_MB", "1")
    get_settings.cache_clear()
    env.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, _good_patients(10)) + b" " * 1_100_000)
    assert env.tick()["rejected"] == 1
    assert env.one("select error from stride_import_files where practice_id=%s",
                   env.practice_id)["error"] == "file is larger than 1 MB"


def test_temporary_upload_names_are_ignored(env):
    for name in ("patients_20261001100000.csv.part", ".patients_20261001100000.csv",
                 "patients_20261001100000.csv.filepart"):
        env.drop(name, csv_bytes(PATIENT_HEADER, [patient("101")]))
    assert env.tick().get("processed") is None
    assert len(env.files("incoming")) == 3


def test_new_stride_column_is_kept_in_raw(env):
    header = [*PATIENT_HEADER, "Preferred Language"]
    env.drop("patients_20261001100000.csv", csv_bytes(header, [patient("101", **{"Preferred Language": "es"})]))
    env.tick()
    assert env.one("select raw->>'Preferred Language' v from stride_patients where practice_id=%s",
                   env.practice_id)["v"] == "es"


def _crash_after_writing(monkeypatch):
    real_apply = worker.apply_file

    def crash(conn, *args, **kwargs):
        real_apply(conn, *args, **kwargs)
        raise ConnectionError("database went away")

    monkeypatch.setattr(worker, "apply_file", crash)
    return real_apply


def test_crash_mid_file_saves_nothing_and_retries_then_parks(env, monkeypatch):
    env.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [patient("101"), patient("102")]))
    _crash_after_writing(monkeypatch)
    assert env.tick()["retry"] == 1
    assert env.one("select count(*) n from stride_patients where practice_id=%s", env.practice_id)["n"] == 0
    assert env.files("incoming") == ["patients_20261001100000.csv"]
    assert env.one("select status from stride_import_files where practice_id=%s",
                   env.practice_id)["status"] == "retrying"
    env.tick()
    assert env.tick()["failed"] == 1  # third attempt
    assert env.files("error") == ["patients_20261001100000.csv"]
    f = env.one("select status,attempts,error from stride_import_files where practice_id=%s", env.practice_id)
    assert (f["status"], f["attempts"], f["error"]) == ("rejected", 3, "import failed: ConnectionError")


def test_a_failing_file_holds_back_later_files(env, monkeypatch):
    env.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [patient("101")]))
    env.drop("patients_20261001110000.csv", csv_bytes(PATIENT_HEADER, [
        patient("101", mobile="(555) 201-9999", modified="2026-10-01 11:00:00")]))
    real_apply = _crash_after_writing(monkeypatch)
    assert env.tick() == {"retry": 1, "leads_linked": 0, "leads_ambiguous": 0, "leads_booked": 0,
                          "leads_flagged": 0}
    assert sorted(env.files("incoming")) == ["patients_20261001100000.csv", "patients_20261001110000.csv"]
    monkeypatch.setattr(worker, "apply_file", real_apply)
    assert env.tick()["processed"] == 2
    assert env.one("select mobile_phone from stride_patients where practice_id=%s",
                   env.practice_id)["mobile_phone"] == "+15552019999"


def test_import_killed_before_it_could_record_failure_still_counts(env):
    """Out of memory or a killed container: the attempt was saved before the work began."""
    path = env.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [patient("101")]))
    settings = get_settings()
    sha, _ = worker._scan(path)
    for _ in range(settings.stride_import_max_attempts):
        worker._start_attempt(settings, path.name, "patients", None, sha, path.stat().st_size)
    assert env.tick()["failed"] == 1
    assert env.files("error") == ["patients_20261001100000.csv"]


def test_large_file_streams_with_flat_memory(env):
    import tracemalloc

    rows = _good_patients(30_000)
    env.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, rows))
    del rows
    tracemalloc.start()
    counts = env.tick()
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert counts["processed"] == 1
    assert env.one("select count(*) n from stride_patients where practice_id=%s", env.practice_id)["n"] == 30_000
    # The old in-memory importer peaked around 24x the file size (~150 MB here).
    assert peak < 20 * 1024 * 1024


def test_heartbeat_reports_what_the_inbox_holds(env):
    env.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [patient("101")]), age_seconds=5)
    env.tick()
    beat = env.one("select details from service_heartbeats where service='stride-worker'")["details"]
    assert beat["inbox_waiting"] == 1 and beat["enabled"] is True and beat["state"] == "idle"
    assert 0 < beat["disk_free_percent"] <= 100


def test_appointment_before_its_patient_is_kept(env):
    env.drop("appointments_20261001100000.csv",
             csv_bytes(APPOINTMENT_HEADER, [appointment("9001", "555", "7001", future_day())]))
    env.tick()
    assert env.one("select patient_stride_id from stride_appointments where practice_id=%s",
                   env.practice_id)["patient_stride_id"] == 555


# --- deletes ---------------------------------------------------------------


def test_delete_without_appointment_id_finds_the_slot(env):
    env.drop("appointments_20261001100000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("9001", "101", "7001", "2026-11-02", "10:00 AM"),
        appointment("9002", "101", "7001", "2026-11-04", "10:00 AM"),
    ]))
    env.tick()
    env.drop("appointments_20261001110000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("", "101", "7001", "2026-11-02", "10:00 AM", action="Delete",
                    modified="2026-10-01 11:00:00", **{"Created Date Time": ""}),
    ]))
    env.tick()
    rows = env.db.execute("select stride_id,deleted_at is not null deleted from stride_appointments "
                          "where practice_id=%s order by stride_id", (env.practice_id,)).fetchall()
    assert [(r["stride_id"], r["deleted"]) for r in rows] == [(9001, True), (9002, False)]


def test_delete_that_matches_nothing_or_two_rows_is_reported_not_guessed(env):
    env.drop("appointments_20261001100000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("9001", "101", "7001", "2026-11-02", **{"Primary Attendee Id": ""}),
        appointment("9002", "101", "7001", "2026-11-02", **{"Primary Attendee Id": ""}),
    ]))
    env.tick()
    env.drop("appointments_20261001110000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("", "101", "7001", "2026-11-02", action="Delete", modified="2026-10-01 11:00:00"),
        appointment("", "101", "7001", "2026-12-25", action="Delete", modified="2026-10-01 11:00:00"),
    ]))
    env.tick()
    f = env.one("select issues from stride_import_files where file_name='appointments_20261001110000.csv' "
                "and practice_id=%s", env.practice_id)
    assert sorted(i["reason"] for i in f["issues"]) == ["delete_ambiguous", "delete_unmatched"]
    assert env.one("select count(*) n from stride_appointments where practice_id=%s and deleted_at is null",
                   env.practice_id)["n"] == 2


def test_deleted_patient_is_kept_and_a_stale_upsert_cannot_revive_it(env):
    env.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [patient("101")]))
    env.tick()
    env.drop("patients_20261001120000.csv", csv_bytes(PATIENT_HEADER, [
        patient("101", action="Delete", modified="2026-10-01 12:00:00")]))
    env.tick()
    env.drop("patients_20261001110000.csv", csv_bytes(PATIENT_HEADER, [
        patient("101", modified="2026-10-01 11:00:00")]))
    env.tick()
    assert env.one("select deleted_at from stride_patients where practice_id=%s", env.practice_id)["deleted_at"]
    env.drop("patients_20261001130000.csv", csv_bytes(PATIENT_HEADER, [
        patient("101", modified="2026-10-01 13:00:00")]))
    env.tick()
    assert env.one("select deleted_at from stride_patients where practice_id=%s",
                   env.practice_id)["deleted_at"] is None


def test_rescheduled_appointment_keeps_its_id_and_moves(env):
    env.drop("appointments_20261001100000.csv",
             csv_bytes(APPOINTMENT_HEADER, [appointment("9001", "101", "7001", "2026-11-02")]))
    env.tick()
    env.drop("appointments_20261001110000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("9001", "101", "7001", "2026-11-05", "2:30 PM", "3:30 PM", modified="2026-10-01 11:00:00")]))
    env.tick()
    assert env.one("select starts_at_local from stride_appointments where practice_id=%s",
                   env.practice_id)["starts_at_local"] == datetime.fromisoformat("2026-11-05T14:30")


# --- leads -----------------------------------------------------------------


def _import_patient_with_appointment(env, day=None, status="Unmarked", **patient_fields):
    env.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [patient("101", **patient_fields)]))
    env.drop("appointments_20261001100000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("9001", "101", "7001", day or future_day(), status=status)]))
    return env.tick()


def test_sheet_lead_who_books_alone_is_marked_booked_and_cadence_stops(env):
    lead = env.lead("Divyansh Test")
    counts = _import_patient_with_appointment(env, first="Divyansh", last="Test")
    assert counts["leads_linked"] == 1 and counts["leads_booked"] == 1
    row = env.one("select status,cadence_state,stride_patient_id,stride_case_id from leads where id=%s", lead)
    assert (row["status"], row["cadence_state"], row["stride_patient_id"], row["stride_case_id"]) == (
        "booked", "completed", 101, 7001)
    assert env.one("select count(*) n from outreach_events where lead_id=%s and status='planned'", lead)["n"] == 0
    assert env.one("select source from lead_status_history where lead_id=%s", lead)["source"] == "stride"
    assert env.one("select event_type from integration_outbox where aggregate_id=%s",
                   str(lead))["event_type"] == "sheet.lead_booked"
    assert env.tick().get("leads_booked") == 0  # recomputed, never repeated


def test_name_match_is_case_and_space_insensitive_with_phone_fallback(env):
    lead = env.lead("Asha  Rao", dob=None)
    _import_patient_with_appointment(env, first="ASHA", last="rao")
    assert env.one("select status from leads where id=%s", lead)["status"] == "booked"


def test_family_member_with_same_name_but_different_birthday_is_not_linked(env):
    lead = env.lead("Sam Test", dob="1960-05-05")
    counts = _import_patient_with_appointment(env, first="Sam", last="Test", dob="1995-05-05")
    assert counts["leads_linked"] == 0
    assert env.one("select status from leads where id=%s", lead)["status"] == "in_progress"


def test_two_leads_matching_one_patient_are_left_for_staff(env):
    first = env.lead("Riya Test")
    second = env.lead("Riya Test", phone="+15552019999")
    counts = _import_patient_with_appointment(env, first="Riya", last="Test")
    assert counts["leads_ambiguous"] == 1 and counts["leads_booked"] == 0
    assert env.one("select lead_match_status from stride_patients where practice_id=%s",
                   env.practice_id)["lead_match_status"] == "ambiguous"
    assert {env.one("select status from leads where id=%s", i)["status"] for i in (first, second)} == {
        "in_progress"}


def test_only_future_appointments_count_as_booked(env):
    lead = env.lead("Past Test")
    counts = _import_patient_with_appointment(env, day="2025-01-10", first="Past", last="Test")
    assert counts["leads_linked"] == 1 and counts["leads_booked"] == 0
    assert env.one("select status from leads where id=%s", lead)["status"] == "in_progress"


def test_cancelled_future_appointment_does_not_book(env):
    lead = env.lead("Cancel Test")
    _import_patient_with_appointment(env, status="Cancel", first="Cancel", last="Test")
    assert env.one("select status from leads where id=%s", lead)["status"] == "in_progress"


def test_lead_added_after_the_patient_still_links(env):
    _import_patient_with_appointment(env, first="Later", last="Lead")
    lead = env.lead("Later Lead")
    env.tick()
    assert env.one("select status from leads where id=%s", lead)["status"] == "booked"


def test_cancelled_booking_flags_the_lead_and_never_restarts_outreach(env):
    lead = env.lead("Flag Test")
    _import_patient_with_appointment(env, first="Flag", last="Test")
    env.drop("appointments_20261001110000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("9001", "101", "7001", future_day(), status="Cancel", modified="2026-10-01 11:00:00")]))
    counts = env.tick()
    assert counts["leads_flagged"] == 1
    row = env.one("select status,needs_review,review_reason,cadence_state from leads where id=%s", lead)
    assert (row["status"], row["needs_review"], row["cadence_state"]) == (
        "needs_attention", True, "completed")
    assert row["review_reason"] == "Cancelled in Stride"
    assert env.one("select count(*) n from outreach_events where lead_id=%s and status='planned'", lead)["n"] == 0
    assert env.tick().get("leads_flagged") == 0


def test_deleted_booking_also_flags_the_lead(env):
    lead = env.lead("Gone Test")
    day = future_day()
    _import_patient_with_appointment(env, day=day, first="Gone", last="Test")
    env.drop("appointments_20261001110000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("", "101", "7001", day, action="Delete", modified="2026-10-01 11:00:00")]))
    env.tick()
    assert env.one("select status from leads where id=%s", lead)["status"] == "needs_attention"


def test_cancel_and_rebook_in_one_export_keeps_the_lead_booked(env):
    lead = env.lead("Rebook Test")
    _import_patient_with_appointment(env, first="Rebook", last="Test")
    env.drop("appointments_20261001110000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("9001", "101", "7001", future_day(), status="Cancel", modified="2026-10-01 11:00:00"),
        appointment("9002", "101", "7001", future_day(9), modified="2026-10-01 11:00:00"),
    ]))
    assert env.tick().get("leads_flagged") == 0
    assert env.one("select status from leads where id=%s", lead)["status"] == "booked"


def test_flagged_lead_that_rebooks_is_booked_again(env):
    lead = env.lead("Again Test")
    _import_patient_with_appointment(env, first="Again", last="Test")
    env.drop("appointments_20261001110000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("9001", "101", "7001", future_day(), status="Cancel", modified="2026-10-01 11:00:00")]))
    env.tick()
    env.drop("appointments_20261001120000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("9003", "101", "7001", future_day(10), modified="2026-10-01 12:00:00")]))
    env.tick()
    row = env.one("select status,needs_review from leads where id=%s", lead)
    assert (row["status"], row["needs_review"]) == ("booked", False)
    events = env.db.execute("select event_type from integration_outbox where aggregate_id=%s order by id",
                            (str(lead),)).fetchall()
    assert [e["event_type"] for e in events] == [
        "sheet.lead_booked", "sheet.stride_booking_cancelled", "sheet.lead_booked"]


def _book_then_change(env, name, **change):
    """Lead books in Stride, then the next export changes that appointment."""
    first, last = name.split()
    lead = env.lead(name)
    day = future_day()
    _import_patient_with_appointment(env, day=day, first=first, last=last)
    env.drop("appointments_20261001110000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("" if change.get("action") == "Delete" else "9001", "101", "7001", day,
                    modified="2026-10-01 11:00:00", **change)]))
    return lead, env.tick()


@pytest.mark.parametrize(("change", "reason"), [
    ({"status": "Cancel"}, "Cancelled in Stride"),
    ({"status": "Late Cancel"}, "Late cancel in Stride"),
    ({"status": "No Show"}, "No show in Stride"),
    ({"status": "A"}, "Cancelled in Stride"),
    ({"status": "L"}, "Late cancel in Stride"),
    ({"status": "N"}, "No show in Stride"),
    ({"action": "Delete"}, "Appointment removed in Stride"),
])
def test_each_way_a_booking_falls_through_gets_its_own_reason(env, change, reason):
    lead, counts = _book_then_change(env, "Reason Test", **change)
    assert counts["leads_flagged"] == 1
    row = env.one("select status,needs_review,review_reason from leads where id=%s", lead)
    assert (row["status"], row["needs_review"], row["review_reason"]) == ("needs_attention", True, reason)
    history = env.one("select reason from lead_status_history where lead_id=%s and to_status='needs_attention'",
                      lead)
    assert history["reason"] == reason
    # The next ticks must leave it alone: a future no-show re-booked the lead (seen on demo).
    assert env.tick().get("leads_booked") == 0
    assert env.one("select status from leads where id=%s", lead)["status"] == "needs_attention"


def test_no_show_patient_who_books_a_new_visit_is_booked_again(env):
    lead, _ = _book_then_change(env, "Return Test", status="No Show")
    env.drop("appointments_20261001120000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("9002", "101", "7001", future_day(14), modified="2026-10-01 12:00:00")]))
    assert env.tick()["leads_booked"] == 1
    assert env.one("select status from leads where id=%s", lead)["status"] == "booked"


@pytest.mark.parametrize("status", ["Confirmed", "O", "Checked In", "C", "Unmarked"])
def test_confirmed_or_checked_in_booking_stays_booked(env, status):
    lead, counts = _book_then_change(env, "Kept Test", status=status)
    assert counts["leads_flagged"] == 0
    assert env.one("select status from leads where id=%s", lead)["status"] == "booked"


def _sheet_marks_booked(env, lead):
    """What the n8n intake does when the Sheet row's Action reads Booked."""
    from uuid import uuid4

    from rpt_agent.services.lead_actions import _mark_booked

    _mark_booked(env.db, request_id=uuid4(), practice={"id": env.practice_id}, lead_id=lead,
                 phone="+15552010000")


def test_staff_rebooking_after_a_cancel_is_not_flagged_again(env):
    # The 2026-10-13 loop: Sheet re-sent Booked, we re-read the same old cancel, flagged again...
    lead, _ = _book_then_change(env, "Loop Test", status="Cancel")
    for _ in range(3):
        _sheet_marks_booked(env, lead)
        assert env.tick().get("leads_flagged") == 0
        assert env.one("select status from leads where id=%s", lead)["status"] == "booked"
    flags = env.one("select count(*) n from lead_status_history where lead_id=%s and to_status='needs_attention'",
                    lead)
    assert flags["n"] == 1


def test_a_new_cancel_after_staff_rebooked_is_still_flagged(env):
    lead, _ = _book_then_change(env, "Second Test", status="Cancel")
    _sheet_marks_booked(env, lead)
    env.tick()
    env.drop("appointments_20261001120000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("9002", "101", "7001", future_day(12), modified="2026-10-01 12:00:00")]))
    env.tick()
    env.drop("appointments_20261001130000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("9002", "101", "7001", future_day(12), status="No Show", modified="2026-10-01 13:00:00")]))
    assert env.tick()["leads_flagged"] == 1
    row = env.one("select status,review_reason from leads where id=%s", lead)
    assert (row["status"], row["review_reason"]) == ("needs_attention", "No show in Stride")


def test_no_show_on_an_old_visit_does_not_flag_a_new_booking(env):
    lead = env.lead("Old Visit")
    env.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [patient("101", first="Old", last="Visit")]))
    env.drop("appointments_20261001100000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("8001", "101", "7001", "2025-01-10", status="Unmarked"),
        appointment("9001", "101", "7001", future_day())]))
    env.tick()
    env.drop("appointments_20261001110000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("8001", "101", "7001", "2025-01-10", status="No Show", modified="2026-10-01 11:00:00")]))
    assert env.tick()["leads_flagged"] == 0
    assert env.one("select status from leads where id=%s", lead)["status"] == "booked"


def test_sheet_gets_a_final_label_and_the_stale_booked_action_is_cleared(env):
    from rpt_agent.services.sheet_sync import build_sheet_snapshot

    lead, _ = _book_then_change(env, "Sheet Test", status="Late Cancel")
    sheet = build_sheet_snapshot(env.db, str(lead))["sheet"]
    # n8n's intake skips any row whose Outcome Status starts with "Needs review".
    assert sheet["action_status"] == "Needs review: Late cancel in Stride"
    assert sheet["action"] == ""
    assert sheet["needs_review"] == "Needs Review"
    _sheet_marks_booked(env, lead)
    sheet = build_sheet_snapshot(env.db, str(lead))["sheet"]
    assert (sheet["action_status"], sheet["action"]) == ("Booked", "Booked")


def test_other_review_reasons_keep_the_staff_action_cell(env):
    from rpt_agent.services.sheet_sync import build_sheet_snapshot

    lead = env.lead("Other Review")
    env.db.execute("update leads set status='needs_attention',needs_review=true,review_reason=%s where id=%s",
                   ("Call result could not be confirmed", lead))
    sheet = build_sheet_snapshot(env.db, str(lead))["sheet"]
    assert "action" not in sheet
    assert not str(sheet.get("action_status") or "").startswith("Needs review")


def test_patients_without_a_lead_never_touch_leads(env):
    _import_patient_with_appointment(env, first="Walk", last="In")
    assert env.one("select count(*) n from leads where practice_id=%s", env.practice_id)["n"] == 0
    assert env.one("select lead_match_status from stride_patients where practice_id=%s",
                   env.practice_id)["lead_match_status"] == "unmatched"


# --- watchdog / dashboard status -------------------------------------------


def _status(env, now=None):
    from rpt_agent.services.stride_sync_status import stride_sync_status

    return stride_sync_status(env.db, env.practice_id, now)


def _titles(status):
    return [(a["level"], a["title"]) for a in status["alerts"]]


def test_status_before_anything_arrives_says_not_started(env):
    env.db.execute("delete from service_heartbeats where service='stride-worker'")
    status = _status(env)
    assert status["overall"] == "not_started"
    assert status["recent_files"] == []


def test_status_is_ok_with_todays_numbers_after_a_normal_load(env):
    lead = env.lead("Nina Test")
    _import_patient_with_appointment(env, first="Nina", last="Test")
    status = _status(env)
    assert status["overall"] == "ok", status["alerts"]
    assert status["today"]["files_received"] == 2
    assert status["today"]["new_patients"] == 1 and status["today"]["new_appointments"] == 1
    assert status["today"]["leads_marked_booked"] == 1
    assert {f["kind"] for f in status["recent_files"]} == {"Patients", "Appointments"}
    assert all(f["status"] == "Loaded" for f in status["recent_files"])
    assert "Nina" not in str(status) and str(lead) not in str(status)


def test_rejected_file_is_a_problem_in_plain_words(env):
    header = [c for c in PATIENT_HEADER if c != "Date Of Birth"]
    env.drop("patients_20261001100000.csv", csv_bytes(header, [patient("101")]))
    env.tick()
    status = _status(env)
    assert status["overall"] == "problem"
    alert = next(a for a in status["alerts"] if a["level"] == "problem")
    assert alert["title"] == "A patients file from Stride was not loaded"
    assert alert["detail"] == "The file was missing information we need (Date Of Birth)."
    assert status["recent_files"][0]["status"] == "Not loaded"


def test_stopped_worker_is_a_problem(env):
    env.tick()
    env.db.execute("update service_heartbeats set last_seen_at=now()-interval '10 minutes' "
                   "where service='stride-worker'")
    status = _status(env)
    assert ("problem", "The Stride import has stopped") in _titles(status)
    assert status["service"]["running"] is False


def test_files_waiting_and_low_disk_are_warnings(env):
    env.tick()
    env.db.execute(
        "update service_heartbeats set details=details||'{\"inbox_waiting\":2,\"oldest_waiting_minutes\":40,"
        "\"disk_free_percent\":15}'::jsonb where service='stride-worker'"
    )
    status = _status(env)
    assert status["overall"] == "attention"
    assert ("warning", "2 file(s) waiting to be loaded") in _titles(status)
    assert ("warning", "Storage for Stride files is getting full") in _titles(status)


def test_long_import_shows_progress_not_an_error(env):
    env.tick()
    env.db.execute(
        "update service_heartbeats set details=details||'{\"state\":\"importing\",\"rows_read\":1200000}'::jsonb "
        "where service='stride-worker'"
    )
    status = _status(env)
    assert status["overall"] == "ok"
    assert any(a["detail"].startswith("1,200,000 rows read") for a in status["alerts"])


def test_quiet_feed_during_clinic_hours_is_a_warning_but_not_at_night(env):
    every_day = {str(day): {"open": "08:00", "close": "18:00"} for day in range(1, 8)}
    env.db.execute("update practice_settings set business_hours=%s where practice_id=%s",
                   (psycopg.types.json.Jsonb(every_day), env.practice_id))
    env.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [patient("101")]))
    env.tick()
    env.db.execute("update stride_import_files set received_at=received_at-interval '2 hours' "
                   "where practice_id=%s", (env.practice_id,))
    noon = datetime.fromisoformat("2026-10-07T12:00:00-04:00")
    night = datetime.fromisoformat("2026-10-07T23:00:00-04:00")
    env.db.execute("update stride_import_files set received_at=%s where practice_id=%s",
                   (noon - timedelta(hours=2), env.practice_id))
    env.db.execute("update service_heartbeats set last_seen_at=%s where service='stride-worker'", (noon,))
    assert ("warning", "No updates from Stride recently") in _titles(_status(env, noon))
    env.db.execute("update service_heartbeats set last_seen_at=%s where service='stride-worker'", (night,))
    assert ("warning", "No updates from Stride recently") not in _titles(_status(env, night))


def test_ambiguous_matches_are_listed_for_staff(env):
    env.lead("Riya Test")
    env.lead("Riya Test", phone="+15552019999")
    _import_patient_with_appointment(env, first="Riya", last="Test")
    assert ("info", "1 patient(s) match more than one lead") in _titles(_status(env))


def test_dashboard_endpoint_returns_the_status(env, monkeypatch):
    from fastapi.testclient import TestClient

    from rpt_agent.api import app

    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    response = TestClient(app).get(
        "/api/v1/dashboard/stride-sync",
        headers={"X-Dashboard-Token": "x" * 32, "X-Dashboard-User-ID": "staff-1",
                 "X-Dashboard-User-Name": "Test%20Staff", "X-Dashboard-User-Role": "employee"},
    )
    assert response.status_code == 200
    assert set(response.json()) >= {"overall", "headline", "alerts", "today", "recent_files", "last_7_days"}


def _one_word_lead(env, name, dob="1990-01-01", phone="+15552010000"):
    # The Sheet accepts one-word names: first_name set, last_name empty.
    return env.db.execute(
        "insert into leads(practice_id,source_system,full_name,first_name,last_name,phone_e164,date_of_birth,"
        "status,cadence_state,is_test) values(%s,'google_sheets',%s,%s,null,%s,%s,'in_progress','active',true) "
        "returning id",
        (env.practice_id, name, name, phone, dob),
    ).fetchone()["id"]


def test_one_word_sheet_name_matches_stride_first_name_and_birthday(env):
    """Seen in testing: lead "devyansh" never matched Stride's "devyansh <last name>"."""
    lead = _one_word_lead(env, "devyansh")
    _import_patient_with_appointment(env, first="Devyansh", last="Chaudhary")
    row = env.one("select status,stride_patient_id from leads where id=%s", lead)
    assert (row["status"], row["stride_patient_id"]) == ("booked", 101)


def test_one_word_name_with_a_different_birthday_does_not_match(env):
    lead = _one_word_lead(env, "devyansh", dob="1960-01-01")
    _import_patient_with_appointment(env, first="Devyansh", last="Chaudhary", dob="2000-06-13")
    assert env.one("select stride_patient_id from leads where id=%s", lead)["stride_patient_id"] is None


def test_two_word_lead_still_needs_the_same_last_name(env):
    lead = env.lead("Sam Test")
    _import_patient_with_appointment(env, first="Sam", last="Other")
    assert env.one("select stride_patient_id from leads where id=%s", lead)["stride_patient_id"] is None


def test_one_word_name_matches_by_phone_when_birthday_is_missing(env):
    lead = _one_word_lead(env, "devyansh", dob=None)
    _import_patient_with_appointment(env, first="Devyansh", last="Chaudhary")
    assert env.one("select status from leads where id=%s", lead)["status"] == "booked"
