"""Booking views on a real Postgres schema: real Stride import, real SQL.

Needs TEST_DATABASE_URL (a disposable local database). Reuses the Stride import fixture,
which builds its own practice and removes it afterwards.
"""

from datetime import UTC, datetime, timedelta

import pytest
from test_stride_import_integration import (  # noqa: F401 (env is a fixture)
    APPOINTMENT_HEADER,
    PATIENT_HEADER,
    appointment,
    csv_bytes,
    env,
    future_day,
    patient,
)

from rpt_agent.services import appointment_views as av

pytestmark = pytest.mark.integration


@pytest.fixture
def booked(env):  # noqa: F811
    yield env
    db, practice = env.db, env.practice_id
    leads = [r["id"] for r in db.execute("select id from leads where practice_id=%s", (practice,))]
    db.execute("delete from appointments where practice_id=%s", (practice,))
    db.execute("delete from call_transcripts where lead_id=any(%s)", (leads,))
    db.execute("delete from call_logs where lead_id=any(%s)", (leads,))
    db.execute("delete from clinician_assignments where clinician_id in "
               "(select id from clinicians where practice_id=%s)", (practice,))
    for table in ("clinicians", "case_types", "booking_locations"):
        db.execute(f"delete from {table} where practice_id=%s", (practice,))


def _booking_setup(ctx):
    db, practice = ctx.db, ctx.practice_id
    location = db.execute(
        "insert into booking_locations(practice_id,name,stride_location_id,timezone) "
        "values(%s,'Laguna Niguel',2,'America/New_York') returning id", (practice,)).fetchone()["id"]
    case = db.execute(
        "insert into case_types(practice_id,name,stride_appointment_type_id,duration_minutes) "
        "values(%s,'Physical Therapy',2,60) returning id", (practice,)).fetchone()["id"]
    clinician = db.execute(
        "insert into clinicians(practice_id,stride_user_id,display_name) values(%s,3,'Joe Smith') "
        "returning id", (practice,)).fetchone()["id"]
    db.execute("insert into clinician_assignments(clinician_id,booking_location_id,case_type_id) "
               "values(%s,%s,%s)", (clinician, location, case))
    return case


def test_every_kind_of_booking_is_tagged_and_named(booked):
    ctx = booked
    case = _booking_setup(ctx)
    db, practice = ctx.db, ctx.practice_id
    now = datetime.now(UTC)
    front_desk = ctx.lead("Sheet Lead", dob="1990-01-01", phone="+15552010001")
    elliot = ctx.lead("Elliot Lead", dob="1991-02-02", phone="+15552010002")
    ctx.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [
        patient("101", "Sheet", "Lead", "1990-01-01", mobile="(555) 201-0001"),
        patient("102", "Walk", "In", "1985-05-05", mobile="(555) 201-0099"),
    ]))
    ctx.drop("appointments_20261001100000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("9001", "101", "7001", future_day(3)),
        appointment("9002", "102", "7002", future_day(4), status="Checked In"),
        appointment("9003", "102", "7002", future_day(5), status="Late Cancel"),
    ]))
    ctx.tick()

    # Elliot booked on a call 84 seconds in; Stride has not exported it yet.
    dialed = now - timedelta(hours=2)
    call = db.execute(
        "insert into call_logs(lead_id,vapi_call_id,dialed_at) values(%s,'call-elliot-1',%s) returning id",
        (elliot, dialed)).fetchone()["id"]
    db.execute("insert into call_transcripts(call_log_id,lead_id,transcript_text) values(%s,%s,%s)",
               (call, elliot, ("AI: Does Tuesday work?\nUser: Mornings are better for me please.\n"
                               "AI: Great, you're all set.")))
    start = now + timedelta(days=6)
    db.execute(
        "insert into appointments(lead_id,practice_id,booking_source,state,start_utc,end_utc,clinician_id,"
        "location_id,case_type_id,call_id,created_at) values(%s,%s,'voice_agent','scheduled',%s,%s,3,2,%s,"
        "'call-elliot-1',%s)",
        (elliot, practice, start, start + timedelta(hours=1), case, dialed + timedelta(seconds=84)))

    records, truncated = av.load_records(db, practice, lower=now - timedelta(days=1),
                                         upper=now + timedelta(days=30))
    assert not truncated
    by_id = {r["id"]: r for r in records}
    assert set(by_id) == {"stride:9001", "stride:9002", "stride:9003", next(
        k for k in by_id if k.startswith("booking:"))}
    sheet = by_id["stride:9001"]
    assert (sheet["tag"], sheet["booked_by"], sheet["state"]) == ("sheet_lead", "stride", "scheduled")
    assert sheet["lead_id"] == front_desk and sheet["location"] == "Laguna Niguel"
    assert by_id["stride:9002"]["tag"] == "stride_only" and by_id["stride:9002"]["state"] == "completed"
    assert by_id["stride:9003"]["state"] == "late_cancel" and by_id["stride:9003"]["lead_id"] is None
    ours = next(r for r in records if r["id"].startswith("booking:"))
    assert (ours["tag"], ours["booked_by"], ours["type"]) == ("elliot", "voice_agent", "Initial Evaluation")
    assert ours["practitioner_name"] == "Joe Smith" and ours["case_title"] == "Physical Therapy"
    assert ours["call_log_id"] == call and ours["call_offset_seconds"] == 84

    card = av.booking_card(db, practice, elliot, now)
    assert card["quote"] == "Mornings are better for me please."
    bookings = av.lead_bookings(db, practice, [front_desk, elliot], now)
    assert bookings[str(front_desk)]["id"] == "stride:9001"
    assert bookings[str(elliot)]["tag"] == "elliot"

    # Upcoming-only window (home page): the past is excluded, open upper end works.
    upcoming, _ = av.load_records(db, practice, lower=now)
    assert {r["id"] for r in upcoming} == set(by_id)


def test_stride_copy_wins_and_a_removed_visit_stays_hidden(booked):
    ctx = booked
    _booking_setup(ctx)
    db, practice = ctx.db, ctx.practice_id
    lead = ctx.lead("Elliot Lead", dob="1991-02-02", phone="+15552010002")
    start = datetime.now(UTC) + timedelta(days=5)
    db.execute(
        "insert into appointments(lead_id,practice_id,booking_source,state,start_utc,stride_appointment_id) "
        "values(%s,%s,'voice_agent','scheduled',%s,9100)", (lead, practice, start))
    ctx.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [
        patient("201", "Elliot", "Lead", "1991-02-02", mobile="(555) 201-0002")]))
    ctx.drop("appointments_20261001100000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("9100", "201", "7100", future_day(5), status="Cancel")]))
    ctx.tick()
    records, _ = av.load_records(db, practice, lead_ids=[lead])
    # One visit, Stride's version (cancelled), still credited to Elliot.
    assert [(r["id"], r["state"], r["tag"]) for r in records] == [("stride:9100", "cancelled", "elliot")]

    db.execute("update stride_appointments set deleted_at=now() where practice_id=%s and stride_id=9100",
               (practice,))
    records, _ = av.load_records(db, practice, lead_ids=[lead])
    assert records == []  # removed in Stride: neither copy shows
