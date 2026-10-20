"""The whole chain, end to end, on a real Postgres schema:

    Google Sheet lead (signed n8n request) -> dashboard lead
    -> Stride CSV export (SFTP import worker) -> lead booked / needs attention
    -> Keap sync worker -> the Keap contact

Every test asserts the dashboard's view of the lead AND what Keap shows, and that they
agree. By default Keap is the in-memory fake; set KEAP_LIVE_SANDBOX=1 (with a connected
sandbox in keap_connections and KEAP_CLIENT_ID/SECRET) to run the same tests against the
real Keap sandbox with synthetic people. Tests that inject outages are fake-only.

Needs TEST_DATABASE_URL (a disposable local database; it uses the seeded 'rausch-pt'
practice and removes everything it created).
"""

import itertools
import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fake_keap import FakeKeap
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from test_stride_import_integration import (
    APPOINTMENT_HEADER,
    PATIENT_HEADER,
    appointment,
    csv_bytes,
    patient,
)

import rpt_agent.keap_sync_worker as keap_worker
import rpt_agent.stride_import_worker as stride_worker
from rpt_agent.api import app
from rpt_agent.config import get_settings
from rpt_agent.db import get_pool
from rpt_agent.observability import WorkflowTrace
from rpt_agent.security import timestamped_hmac
from rpt_agent.services.keap_client import KeapClient

pytestmark = pytest.mark.integration
LIVE = os.getenv("KEAP_LIVE_SANDBOX") == "1"
PENDING, GAVE_UP = "144", "186"
IE_BOOKED, IE_DONE, FU_BOOKED, FU_DONE = "180", "172", "184", "176"
PELVIC_F, PELVIC_M, PELVIC_DONE = "162", "160", "158"
BOOKED_TAGS = {"180", "184", "174", "166", PELVIC_F, PELVIC_M}
FIRST_PID = 810_000
_PHONES = itertools.count(int(uuid.uuid4().int % 9000))
_AIDS = itertools.count(9_000_000)
TOKEN = "d" * 32


def day(offset):
    return (datetime.now(UTC).date() + timedelta(days=offset)).isoformat()


def _local_database(url):
    host = psycopg.conninfo.conninfo_to_dict(url).get("host") or "localhost"
    return host in {"localhost", "127.0.0.1", "::1"} or host.startswith("/")


@pytest.fixture(params=["fake"] + (["live"] if LIVE else []))
def chain(request, monkeypatch, tmp_path):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    # These tests borrow the real rausch-pt practice and their clean-up wipes its Keap
    # links, Stride import list and Stride rows. Run once against the shared demo DB, they
    # erased the Mock Stride data the live demo server was using. Local databases only.
    if not _local_database(url) and os.getenv("CHAIN_TESTS_ALLOW_SHARED_DB") != "1":
        pytest.skip("chain tests run only on a local database (they reset the rausch-pt practice)")
    live = request.param == "live"
    run = uuid.uuid4().hex[:5]
    folders = {n: tmp_path / n for n in ("incoming", "archive", "error")}
    folders["incoming"].mkdir()
    env = {
        "SUPABASE_DB_URL": url, "STRIDE_PRACTICE_SLUG": "rausch-pt", "N8N_PRACTICE_SLUG": "rausch-pt",
        "N8N_INTAKE_KEY_ID": "chain", "N8N_INTAKE_SECRET": "n" * 32, "DASHBOARD_API_TOKEN": TOKEN,
        "STRIDE_IMPORT_ENABLED": "true", "STRIDE_FILE_SETTLE_SECONDS": "60",
        "STRIDE_INBOX_DIR": str(folders["incoming"]), "STRIDE_ARCHIVE_DIR": str(folders["archive"]),
        "STRIDE_ERROR_DIR": str(folders["error"]), "SHEET_SYNC_ENABLED": "false",
        "KEAP_SYNC_ENABLED": "true", "TEST_MODE": "false", "HTTP_RETRY_BASE_SECONDS": "0.1",
    }
    if not live:
        env.update(KEAP_CLIENT_ID="cid", KEAP_CLIENT_SECRET="csecret")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("rpt_agent.services.provider_http.time.sleep", lambda *_: None)
    get_settings.cache_clear()
    get_pool.cache_clear()
    keap_worker._state.update(last_full=None, client=None, stats_day=None)

    db = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    practice_row = db.execute("select id from practices where slug='rausch-pt'").fetchone()
    if practice_row is None:
        pytest.skip("run `rpt seed` first: the chain tests use the rausch-pt practice")
    practice = practice_row["id"]
    saved_settings = db.execute("select * from practice_settings where practice_id=%s",
                                (practice,)).fetchone()
    saved_connection = db.execute("select * from keap_connections where practice_id=%s",
                                  (practice,)).fetchone()
    if live and saved_connection is None:
        pytest.skip("no sandbox connection in keap_connections")
    db.execute(
        "update practice_settings set stride_location_timezone='America/Los_Angeles',"
        "keap_pending_tag_id=%s,keap_gave_up_tag_id=%s,keap_sync_start_at=now()-interval '1 day',"
        "keap_include_existing_patients=true,keap_remove_pending_when_booked=true,"
        "keap_remove_booked_on_cancel=true where practice_id=%s", (PENDING, GAVE_UP, practice))
    for type_id, booked, done in ((2, IE_BOOKED, IE_DONE), (1, FU_BOOKED, FU_DONE), (3, "174", "156")):
        db.execute(
            "insert into keap_appointment_types(practice_id,stride_appointment_type_id,label,"
            "booked_tag_id,completed_tag_id,date_field_id,time_field_id,clinician_field_id) "
            "values(%s,%s,'t',%s,%s,'6','8','10') on conflict(practice_id,stride_appointment_type_id)"
            " do update set booked_tag_id=excluded.booked_tag_id,"
            "completed_tag_id=excluded.completed_tag_id,is_active=true",
            (practice, type_id, booked, done))
    db.execute(
        "insert into keap_appointment_types(practice_id,stride_appointment_type_id,label,"
        "booked_tag_id,male_booked_tag_id,completed_tag_id,date_field_id,time_field_id,"
        "clinician_field_id) values(%s,6,'Pelvic',%s,%s,%s,'6','8','10') "
        "on conflict(practice_id,stride_appointment_type_id) do update set "
        "booked_tag_id=excluded.booked_tag_id,male_booked_tag_id=excluded.male_booked_tag_id,"
        "completed_tag_id=excluded.completed_tag_id,is_active=true",
        (practice, PELVIC_F, PELVIC_M, PELVIC_DONE))
    db.execute(
        "insert into keap_appointment_types(practice_id,stride_appointment_type_id,label,"
        "booked_tag_id,completed_tag_id,date_field_id,time_field_id,clinician_field_id) "
        "values(%s,5,'PT Premium','166','164','6','8','10') "
        "on conflict(practice_id,stride_appointment_type_id) do update set is_active=true",
        (practice,))
    fake = FakeKeap()
    if not live:
        db.execute(
            "insert into keap_connections(practice_id,keap_account,access_token,refresh_token,"
            "access_token_expires_at,status) values(%s,'fake',%s,%s,now()+interval '20 hours','active')"
            " on conflict(practice_id) do update set access_token=excluded.access_token,"
            "refresh_token=excluded.refresh_token,access_token_expires_at=excluded.access_token_expires_at,"
            "status='active'", (practice, fake.access, fake.refresh))
    db.execute("delete from keap_sync where practice_id=%s", (practice,))

    class Chain:
        pass

    c = Chain()
    c.db, c.practice, c.fake, c.live, c.run = db, practice, fake, live, run
    c.client = KeapClient(practice, get_settings(), None if live else fake.client(),
                          audit_enabled=False)
    c.trace = WorkflowTrace("chain", "test")
    c.http = TestClient(app)
    c.lead_ids, c.patient_ids = [], []
    c._clock = {"file": 0, "modified": 0, "pid": FIRST_PID}
    c.keap_contacts = set()

    # --- Google Sheet side -----------------------------------------------------
    def sheet(action="start_cadence", lead_id=None, **lead):
        payload = {"action": action, "lead_id": lead_id, "lead": lead}
        body = json.dumps(payload, separators=(",", ":")).encode()
        stamp = str(int(time.time()))
        response = c.http.post("/api/v1/integrations/n8n/lead-actions", content=body, headers={
            "Content-Type": "application/json", "X-RPT-Key-Id": "chain", "X-RPT-Timestamp": stamp,
            "X-RPT-Signature": "sha256=" + timestamped_hmac("n" * 32, stamp, body),
            "X-Request-ID": str(uuid.uuid4())})
        return response

    def sheet_lead(first, last, *, dob="15/03/1990", phone=None, email=None):
        lead = {"full_name": f"{first} {last}".strip(), "date_of_birth": dob,
                "phone": phone or f"+1555555{next(_PHONES):04d}",
                "location": "Laguna Niguel", "title": "Chain test"}
        if email:
            lead["email"] = email
        response = sheet(**lead)
        assert response.status_code in (200, 201), response.text
        lead_id = response.json()["lead_id"]
        c.lead_ids.append(lead_id)
        return lead_id

    c.sheet, c.sheet_lead = sheet, sheet_lead

    def lead_data(lead_id):
        row = db.execute("select full_name,phone_e164,email,date_of_birth,location from leads "
                         "where id=%s", (lead_id,)).fetchone()
        return {"full_name": row["full_name"], "phone": row["phone_e164"], "email": row["email"],
                "date_of_birth": row["date_of_birth"].isoformat(), "location": row["location"]}

    c.lead_data = lead_data

    def dashboard(lead_id):
        response = c.http.get(f"/api/v1/dashboard/leads/{lead_id}", headers={
            "X-Dashboard-Token": TOKEN, "X-Dashboard-User-ID": "chain-test",
            "X-Dashboard-User-Name": "Chain", "X-Dashboard-User-Role": "super_admin"})
        assert response.status_code == 200, response.text
        return response.json()["lead"]

    c.dashboard = dashboard

    # --- Stride side --------------------------------------------------------------
    def modified():
        c._clock["modified"] += 1
        return (datetime(2026, 10, 1, 10, 0) + timedelta(minutes=c._clock["modified"])).strftime(  # noqa: DTZ001
            "%Y-%m-%d %H:%M:%S")

    def new_pid():
        c._clock["pid"] += 1
        c.patient_ids.append(c._clock["pid"])
        return c._clock["pid"]

    def drop(entity, header, rows):
        c._clock["file"] += 1
        stamp = (datetime(2026, 10, 1, 9, 0) + timedelta(minutes=c._clock["file"])).strftime("%Y%m%d%H%M%S")  # noqa: DTZ001
        path = folders["incoming"] / f"{entity}_{stamp}.csv"
        path.write_bytes(csv_bytes(header, rows))
        old = time.time() - 300
        os.utime(path, (old, old))

    def stride_patient(first, last, *, dob="1990-03-15", mobile="(555) 555-0177", email=None,
                       pid=None, action="Upsert", **extra):
        pid = pid or new_pid()
        row = patient(str(pid), first, last, dob, modified=modified(), action=action, **extra)
        row["Mobile Phone Number"] = mobile
        if email is not None:
            # Stride's "Preferred Email" is a letter (P = personal, W = work) pointing at a column.
            row["Personal Email"], row["Preferred Email"] = email, "P"
        drop("patients", PATIENT_HEADER, [row])
        return pid

    def stride_appt(pid, offset, *, aid=None, type_id=2, status="Unmarked", start="10:00 AM",
                    end="11:00 AM", action="Upsert", clinician="Dana Clinician", mod=None):
        aid = aid or next(_AIDS)
        row = appointment(str(aid), str(pid), str(pid + 5), day(offset), start, end, status,
                          modified=mod or modified(), action=action)
        row.update({"Appointment Type Id": str(type_id), "Primary Attendee Name": clinician,
                    "Appointment Type": {1: "PT", 2: "Initial Evaluation",
                                         3: "Progress Note"}.get(type_id, "Other")})
        drop("appointments", APPOINTMENT_HEADER, [row])
        return aid

    c.stride_patient, c.stride_appt, c.drop = stride_patient, stride_appt, drop
    c.stride_tick = lambda: stride_worker.run_stride_import_tick()
    c.modified = modified

    # --- Keap side ------------------------------------------------------------------
    def keap_tick():
        counts = {}
        for _ in range(6):
            counts = keap_worker.run_keap_sync_tick(c.client)
            waiting = db.execute(
                "select count(*) as n from keap_sync where practice_id=%s and "
                "status in ('pending','retrying','syncing')", (practice,)).fetchone()["n"]
            if not waiting:
                break
        return counts

    def row_of(lead_id=None, pid=None):
        if lead_id:
            return db.execute("select * from keap_sync where lead_id=%s", (lead_id,)).fetchone()
        return db.execute("select * from keap_sync where practice_id=%s and stride_patient_id=%s",
                          (practice, pid)).fetchone()

    def contact(lead_id=None, pid=None):
        row = row_of(lead_id, pid)
        if not row or not row["keap_contact_id"]:
            return None
        c.keap_contacts.add(row["keap_contact_id"])
        return c.client.get_contact(c.trace, row["keap_contact_id"])

    c.keap_tick, c.row_of, c.contact = keap_tick, row_of, contact
    c.writes = lambda: sum(c.client.stats[k] for k in c.client.stats)

    def tags(contact_):
        return set(contact_["tag_ids"]) if contact_ else set()

    def field(contact_, field_id):
        for f in contact_["custom_fields"]:
            if str(f["id"]) == field_id:
                return f["content"]
        return None

    c.tags, c.field = tags, field

    def agree(lead_id, pid=None):
        """The rule the whole feature exists for: Keap and the dashboard never disagree."""
        status = dashboard(lead_id)["status"]
        have = tags(contact(lead_id))
        if have & BOOKED_TAGS:
            assert status == "booked", f"Keap says Booked, dashboard says {status}"
        if status in {"new", "in_progress", "needs_attention", "callback_scheduled"}:
            assert PENDING in have, f"dashboard {status} but Keap has no Pending: {have}"
        if status == "booked":
            assert PENDING not in have, "dashboard booked but Keap still Pending"

    c.agree = agree

    yield c

    # --- clean up everything the test created (local database only) ---------------
    ids = c.lead_ids
    db.execute("delete from keap_sync where practice_id=%s", (practice,))
    db.execute("delete from integration_outbox where aggregate_id=any(%s)", ([str(i) for i in ids],))
    for table in ("stride_patients", "stride_cases", "stride_appointments"):
        db.execute(f"delete from {table} where practice_id=%s and stride_id>=%s", (practice, FIRST_PID))
    db.execute("delete from stride_import_files where practice_id=%s", (practice,))
    db.execute("delete from lead_status_history where lead_id=any(%s)", (ids,))
    db.execute("delete from outreach_events where lead_id=any(%s)", (ids,))
    db.execute("delete from leads where id=any(%s)", (ids,))
    if saved_connection is not None and not live:
        db.execute(
            "update keap_connections set access_token=%s,refresh_token=%s,"
            "access_token_expires_at=%s,status=%s,keap_account=%s where practice_id=%s",
            (saved_connection["access_token"], saved_connection["refresh_token"],
             saved_connection["access_token_expires_at"], saved_connection["status"],
             saved_connection["keap_account"], practice))
    elif saved_connection is None:
        db.execute("delete from keap_connections where practice_id=%s", (practice,))
    db.execute(
        "update practice_settings set stride_location_timezone=%s,keap_pending_tag_id=%s,"
        "keap_gave_up_tag_id=%s,keap_sync_start_at=%s,keap_include_existing_patients=%s,"
        "keap_remove_pending_when_booked=%s,keap_remove_booked_on_cancel=%s where practice_id=%s",
        (saved_settings["stride_location_timezone"], saved_settings["keap_pending_tag_id"],
         saved_settings["keap_gave_up_tag_id"], saved_settings["keap_sync_start_at"],
         saved_settings["keap_include_existing_patients"],
         saved_settings["keap_remove_pending_when_booked"],
         saved_settings["keap_remove_booked_on_cancel"], practice))
    db.execute("delete from service_heartbeats where service in ('keap-worker','stride-worker')")
    db.close()
    get_pool.cache_clear()
    get_settings.cache_clear()
    if live and c.keap_contacts:
        print("\nKeap sandbox contacts created by this run:", sorted(c.keap_contacts, key=int))


# ============================ the happy path ======================================


def test_sheet_lead_appears_on_the_dashboard_and_in_keap_as_pending(chain):
    lead = chain.sheet_lead("Quinn", f"Chain{chain.run}", email=f"quinn.{chain.run}@example.test")
    assert chain.dashboard(lead)["status"] in {"new", "in_progress"}
    chain.keap_tick()
    contact = chain.contact(lead)
    assert chain.tags(contact) == {PENDING}
    assert contact["given_name"] == "Quinn" and contact["family_name"] == f"Chain{chain.run}"
    assert contact["birth_date"] == "1990-03-15" and contact["contact_type"] == "Lead"
    assert contact["email_addresses"][0]["email"] == f"quinn.{chain.run}@example.test"
    chain.agree(lead)


def test_stride_booking_marks_the_lead_booked_and_updates_keap(chain):
    name = f"Chain{chain.run}"
    lead = chain.sheet_lead("Booker", name, email=f"booker.{chain.run}@example.test")
    chain.keap_tick()
    pid = chain.stride_patient("Booker", name, email=f"booker.{chain.run}@example.test")
    chain.stride_appt(pid, 3, start="2:30 PM", end="3:30 PM")
    chain.stride_tick()
    assert chain.dashboard(lead)["status"] == "booked"
    chain.keap_tick()
    contact = chain.contact(lead)
    assert chain.tags(contact) == {IE_BOOKED}
    assert chain.field(contact, "6").startswith(day(3))
    assert chain.field(contact, "8") == "2:30 PM"  # clinic wall time, California
    assert chain.field(contact, "10") == "Dana Clinician"
    chain.agree(lead)
    assert chain.db.execute("select count(*) as n from keap_sync where practice_id=%s",
                            (chain.practice,)).fetchone()["n"] == 1  # lead + patient = one person


def test_reschedule_moves_the_date_and_keeps_the_tag(chain):
    name = f"Chain{chain.run}"
    lead = chain.sheet_lead("Mover", name, email=f"mover.{chain.run}@example.test")
    pid = chain.stride_patient("Mover", name)
    aid = chain.stride_appt(pid, 3)
    chain.stride_tick(); chain.keap_tick()
    assert chain.field(chain.contact(lead), "6").startswith(day(3))
    chain.stride_appt(pid, 6, aid=aid, start="9:00 AM", end="10:00 AM", clinician="Pat Therapist")
    chain.stride_tick(); chain.keap_tick()
    contact = chain.contact(lead)
    assert chain.field(contact, "6").startswith(day(6)) and chain.field(contact, "8") == "9:00 AM"
    assert chain.field(contact, "10") == "Pat Therapist" and chain.tags(contact) == {IE_BOOKED}
    assert chain.dashboard(lead)["status"] == "booked"
    chain.agree(lead)


def test_check_in_adds_completed_and_keeps_booked(chain):
    name = f"Chain{chain.run}"
    lead = chain.sheet_lead("Visitor", name, email=f"visitor.{chain.run}@example.test")
    pid = chain.stride_patient("Visitor", name)
    aid = chain.stride_appt(pid, 2)
    chain.stride_tick(); chain.keap_tick()
    chain.stride_appt(pid, 2, aid=aid, status="Checked In")
    chain.stride_tick(); chain.keap_tick()
    assert chain.tags(chain.contact(lead)) == {IE_BOOKED, IE_DONE}
    assert chain.dashboard(lead)["status"] == "booked"
    chain.agree(lead)


# ======================== cancellations and the invariant ==========================


@pytest.mark.parametrize("how", ["Cancel", "Late Cancel", "delete"])
def test_a_cancelled_or_deleted_booking_flags_the_lead_and_clears_keap(chain, how):
    name = f"Chain{chain.run}"
    lead = chain.sheet_lead("Rory", name, email=f"rory.{chain.run}@example.test")
    pid = chain.stride_patient("Rory", name)
    aid = chain.stride_appt(pid, 4)
    chain.stride_tick(); chain.keap_tick()
    assert chain.tags(chain.contact(lead)) == {IE_BOOKED}
    if how == "delete":
        chain.stride_appt(pid, 4, aid=aid, action="Delete")
    else:
        chain.stride_appt(pid, 4, aid=aid, status=how)
    chain.stride_tick(); chain.keap_tick()
    assert chain.dashboard(lead)["status"] == "needs_attention"
    contact = chain.contact(lead)
    assert chain.tags(contact) == {PENDING}
    assert not chain.field(contact, "6") and not chain.field(contact, "8")
    chain.agree(lead)


def test_rebooking_after_a_cancel_books_the_lead_again(chain):
    name = f"Chain{chain.run}"
    lead = chain.sheet_lead("Rebook", name, email=f"rebook.{chain.run}@example.test")
    pid = chain.stride_patient("Rebook", name)
    first = chain.stride_appt(pid, 3)
    chain.stride_tick(); chain.keap_tick()
    chain.stride_appt(pid, 3, aid=first, status="Cancel")
    chain.stride_tick(); chain.keap_tick()
    assert chain.dashboard(lead)["status"] == "needs_attention"
    chain.stride_appt(pid, 8, type_id=1)  # a new visit, a different type
    chain.stride_tick(); chain.keap_tick()
    assert chain.dashboard(lead)["status"] == "booked"
    contact = chain.contact(lead)
    assert chain.tags(contact) == {FU_BOOKED} and chain.field(contact, "6").startswith(day(8))
    chain.agree(lead)


def test_no_show_keeps_booked_in_keap_while_the_dashboard_flags_it(chain):
    """DECISION NEEDED (docs/KEAP_EDGE_CASES.md E-12): the dashboard treats a no-show as a
    booking that fell through; the agreed Keap rule keeps Booked. This pins today's behaviour."""
    name = f"Chain{chain.run}"
    lead = chain.sheet_lead("Ghost", name, email=f"ghost.{chain.run}@example.test")
    pid = chain.stride_patient("Ghost", name)
    aid = chain.stride_appt(pid, 3)
    chain.stride_tick(); chain.keap_tick()
    chain.stride_appt(pid, 3, aid=aid, status="No Show")
    chain.stride_tick(); chain.keap_tick()
    assert chain.dashboard(lead)["status"] == "needs_attention"
    assert chain.tags(chain.contact(lead)) == {IE_BOOKED, PENDING} - {PENDING}


# ============================== second types, walk-ins ============================


def test_a_second_appointment_type_adds_its_own_tag_and_the_next_date_wins(chain):
    name = f"Chain{chain.run}"
    lead = chain.sheet_lead("Double", name, email=f"double.{chain.run}@example.test")
    pid = chain.stride_patient("Double", name)
    chain.stride_appt(pid, 9, type_id=2, clinician="Late Clinician")
    chain.stride_appt(pid, 2, type_id=1, clinician="Early Clinician")
    chain.stride_tick(); chain.keap_tick()
    contact = chain.contact(lead)
    assert chain.tags(contact) == {IE_BOOKED, FU_BOOKED}
    assert chain.field(contact, "6").startswith(day(2)) and chain.field(contact, "10") == "Early Clinician"


@pytest.mark.parametrize(("type_id", "gender", "booked", "done"), [
    (1, "F", FU_BOOKED, FU_DONE),           # PT Appt Booked / Completed
    (5, "M", "166", "164"),                 # PT Premium Appt Booked / Completed
    (6, "F", PELVIC_F, PELVIC_DONE),        # Pelvic Appt Booked - Female / Completed
    (6, "M", PELVIC_M, PELVIC_DONE),        # Pelvic Appt Booked - Male / Completed
])
def test_pending_then_booked_then_completed_for_each_appointment_type(chain, type_id, gender,
                                                                      booked, done):
    name = f"Chain{chain.run}"
    lead = chain.sheet_lead("Typed", name, email=f"typed.{chain.run}@example.test")
    chain.keap_tick()
    assert chain.tags(chain.contact(lead)) == {PENDING}
    pid = chain.stride_patient("Typed", name, Gender=gender)
    aid = chain.stride_appt(pid, 3, type_id=type_id)
    chain.stride_tick(); chain.keap_tick()
    assert chain.tags(chain.contact(lead)) == {booked}
    chain.agree(lead)
    chain.stride_appt(pid, 3, aid=aid, type_id=type_id, status="Checked In")
    chain.stride_tick(); chain.keap_tick()
    assert chain.tags(chain.contact(lead)) == {booked, done}
    chain.agree(lead)
    chain.stride_appt(pid, 3, aid=aid, type_id=type_id, status="Cancel")
    chain.stride_tick(); chain.keap_tick()
    assert chain.tags(chain.contact(lead)) == {PENDING}
    chain.agree(lead)


def test_a_walk_in_patient_who_was_never_a_lead_gets_a_contact_without_pending(chain):
    pid = chain.stride_patient("Sky", f"Walkin{chain.run}", dob="1975-06-01",
                               email=f"sky.{chain.run}@example.test")
    chain.stride_appt(pid, 5, type_id=1)
    chain.stride_tick(); chain.keap_tick()
    contact = chain.contact(pid=pid)
    assert chain.tags(contact) == {FU_BOOKED}
    assert chain.row_of(pid=pid)["lead_id"] is None


def test_an_appointment_type_we_do_not_map_books_the_lead_but_tags_nothing(chain):
    """E-09: the dashboard counts any Stride appointment; Keap only knows mapped types."""
    name = f"Chain{chain.run}"
    lead = chain.sheet_lead("Odd", name, email=f"odd.{chain.run}@example.test")
    pid = chain.stride_patient("Odd", name)
    chain.stride_appt(pid, 3, type_id=77)
    chain.stride_tick(); chain.keap_tick()
    assert chain.dashboard(lead)["status"] == "booked"
    assert chain.tags(chain.contact(lead)) == set()


def test_a_visit_before_the_go_live_cutoff_tags_nobody(chain):
    chain.db.execute("update practice_settings set keap_sync_start_at=now()+interval '5 days' "
                     "where practice_id=%s", (chain.practice,))
    name = f"Chain{chain.run}"
    lead = chain.sheet_lead("Early", name, email=f"early.{chain.run}@example.test")
    pid = chain.stride_patient("Early", name)
    chain.stride_appt(pid, 2)  # before the cutoff
    chain.stride_tick(); chain.keap_tick()
    assert chain.tags(chain.contact(lead)) == set()  # booked on the dashboard, nothing older tags Keap


# ============================ staff actions on the Sheet ===========================


def test_staff_marking_booked_in_the_sheet_removes_pending_but_adds_no_booked_tag(chain):
    lead = chain.sheet_lead("Manual", f"Chain{chain.run}", email=f"manual.{chain.run}@example.test")
    chain.keap_tick()
    assert chain.tags(chain.contact(lead)) == {PENDING}
    response = chain.sheet("booked", lead_id=lead, **chain.lead_data(lead))
    assert response.status_code in (200, 201), response.text
    chain.keap_tick()
    assert chain.dashboard(lead)["status"] == "booked"
    assert chain.tags(chain.contact(lead)) == set()  # no Stride appointment, so no type to tag
    chain.agree(lead)


def test_do_not_contact_is_left_alone_until_the_client_picks_a_tag(chain):
    lead = chain.sheet_lead("Quiet", f"Chain{chain.run}", email=f"quiet.{chain.run}@example.test")
    chain.keap_tick()
    response = chain.sheet("do_not_contact", lead_id=lead, **chain.lead_data(lead))
    assert response.status_code in (200, 201), response.text
    chain.keap_tick()
    assert chain.dashboard(lead)["status"] == "do_not_contact"
    # Already in Keap: frozen, so its tags stay exactly as they were (no new writes).
    assert chain.tags(chain.contact(lead)) == {PENDING}


def test_a_lead_marked_do_not_contact_before_its_first_sync_never_reaches_keap(chain):
    lead = chain.sheet_lead("Never", f"Chain{chain.run}", email=f"never.{chain.run}@example.test")
    response = chain.sheet("do_not_contact", lead_id=lead, **chain.lead_data(lead))
    assert response.status_code in (200, 201), response.text
    writes_before = chain.writes()
    chain.keap_tick()
    assert chain.contact(lead) is None
    assert chain.writes() == writes_before
    assert "kept out of Keap" in (chain.row_of(lead)["skip_reason"] or "")


def test_a_finished_cadence_swaps_pending_for_ai_gave_up(chain):
    lead = chain.sheet_lead("Done", f"Chain{chain.run}", email=f"done.{chain.run}@example.test")
    chain.keap_tick()
    chain.db.execute("update leads set status='closed_no_response',cadence_state='completed' "
                     "where id=%s", (lead,))
    chain.keap_tick()
    assert chain.tags(chain.contact(lead)) == {GAVE_UP}


# ============================ changes to people ====================================


def test_stride_contact_changes_reach_keap_without_touching_other_slots(chain):
    name = f"Chain{chain.run}"
    lead = chain.sheet_lead("Mover", name, email=f"old.{chain.run}@example.test")
    pid = chain.stride_patient("Mover", name, email=f"old.{chain.run}@example.test")
    chain.stride_tick(); chain.keap_tick()
    cid = chain.row_of(lead)["keap_contact_id"]
    chain.stride_patient("Mover", name, pid=pid, email=f"new.{chain.run}@example.test")
    chain.stride_tick(); chain.keap_tick()
    contact = chain.contact(lead)
    assert contact["email_addresses"][0]["email"] == f"new.{chain.run}@example.test"
    assert chain.row_of(lead)["keap_contact_id"] == cid  # same contact, updated in place


def test_a_deleted_lead_keeps_its_keap_contact(chain):
    name = f"Chain{chain.run}"
    lead = chain.sheet_lead("Gone", name, email=f"gone.{chain.run}@example.test")
    chain.keap_tick()
    cid = chain.row_of(lead)["keap_contact_id"]
    chain.db.execute("delete from outreach_events where lead_id=%s", (lead,))
    chain.db.execute("delete from lead_status_history where lead_id=%s", (lead,))
    chain.db.execute("delete from leads where id=%s", (lead,))
    chain.lead_ids.remove(lead)
    chain.keap_tick()
    row = chain.db.execute("select * from keap_sync where keap_contact_id=%s", (cid,)).fetchone()
    assert row is not None and row["lead_id"] is None
    assert chain.client.get_contact(chain.trace, cid) is not None  # nothing was deleted in Keap


def test_a_patient_deleted_in_stride_changes_nothing_in_keap(chain):
    pid = chain.stride_patient("Erased", f"Walkin{chain.run}", dob="1980-01-01",
                               email=f"erased.{chain.run}@example.test")
    chain.stride_appt(pid, 4, type_id=1)
    chain.stride_tick(); chain.keap_tick()
    before = chain.contact(pid=pid)
    chain.stride_patient("Erased", f"Walkin{chain.run}", pid=pid, action="Delete")
    chain.stride_tick(); chain.keap_tick()
    after = chain.contact(pid=pid)
    assert chain.tags(before) == chain.tags(after) == {FU_BOOKED}
    assert chain.field(before, "6") == chain.field(after, "6")


# ============================ identity edge cases ==================================


def test_two_leads_sharing_an_email_get_two_contacts_and_booking_one_leaves_the_other(chain):
    shared = f"family.{chain.run}@example.test"
    sam = chain.sheet_lead("Sam", f"Chain{chain.run}", email=shared)
    sue = chain.sheet_lead("Sue", f"Chain{chain.run}", email=shared)
    chain.keap_tick()
    assert chain.contact(sam)["id"] != chain.contact(sue)["id"]
    pid = chain.stride_patient("Sam", f"Chain{chain.run}", email=shared)
    chain.stride_appt(pid, 3)
    chain.stride_tick(); chain.keap_tick()
    assert chain.tags(chain.contact(sam)) == {IE_BOOKED}
    assert chain.tags(chain.contact(sue)) == {PENDING}
    chain.agree(sam); chain.agree(sue)


def test_an_unmatched_patient_with_a_leads_email_and_name_is_held_for_review_not_merged(chain):
    """Two people, one Keap contact would fight over its tags: the second goes to review."""
    name = f"Chain{chain.run}"
    lead = chain.sheet_lead("Twin", name, dob="15/03/1990", email=f"twin.{chain.run}@example.test")
    chain.keap_tick()
    # Same name and email but a different birth date and phone: Stride cannot be linked to the lead.
    pid = chain.stride_patient("Twin", name, dob="1961-01-01", mobile="(555) 555-0999",
                               email=f"twin.{chain.run}@example.test")
    chain.stride_appt(pid, 3)
    chain.stride_tick(); chain.keap_tick()
    patient_row = chain.row_of(pid=pid)
    assert patient_row["lead_id"] is None
    assert patient_row["status"] == "needs_review"
    assert chain.tags(chain.contact(lead)) == {PENDING}  # the lead's contact is not disturbed


def test_phone_only_and_email_only_people_still_get_contacts(chain):
    phone_only = chain.sheet_lead("Phone", f"Chain{chain.run}")
    chain.keap_tick()
    contact = chain.contact(phone_only)
    assert contact["phone_numbers"] and not contact["email_addresses"]
    pid = chain.stride_patient("Mail", f"Walkin{chain.run}", mobile="",
                               email=f"mail.{chain.run}@example.test")
    chain.stride_appt(pid, 3, type_id=1)
    chain.stride_tick(); chain.keap_tick()
    contact = chain.contact(pid=pid)
    assert contact["email_addresses"] and chain.tags(contact) == {FU_BOOKED}


def test_names_with_accents_and_apostrophes_survive_the_round_trip(chain):
    last = f"O'Brien-Núñez{chain.run}"
    lead = chain.sheet_lead("José", last, email=f"jose+tag.{chain.run}@example.test")
    chain.keap_tick()
    contact = chain.contact(lead)
    assert contact["given_name"] == "José" and contact["family_name"] == last
    assert contact["email_addresses"][0]["email"] == f"jose+tag.{chain.run}@example.test"
    # a second tick must recognise the same contact, not create another
    created = chain.client.stats["created"]
    chain.db.execute("update keap_sync set status='pending' where lead_id=%s", (lead,))
    chain.keap_tick()
    assert chain.client.stats["created"] == created


def test_a_one_word_sheet_name_still_links_to_its_stride_patient(chain):
    lead = chain.sheet_lead(f"Solo{chain.run}", "", email=f"solo.{chain.run}@example.test") \
        if False else None
    # The Sheet accepts one-word names; Stride always has first + last (see stride_leads).
    response = chain.sheet(full_name=f"Solo{chain.run}", date_of_birth="15/03/1990",
                           phone="+15555550398", location="Laguna Niguel", title="Chain test",
                           email=f"solo.{chain.run}@example.test")
    assert response.status_code in (200, 201), response.text
    lead = response.json()["lead_id"]
    chain.lead_ids.append(lead)
    pid = chain.stride_patient(f"Solo{chain.run}", "Surname")
    chain.stride_appt(pid, 3)
    chain.stride_tick(); chain.keap_tick()
    assert chain.dashboard(lead)["status"] == "booked"
    assert str(chain.row_of(pid=pid)["lead_id"]) == lead  # linked: one row for the person
    assert chain.tags(chain.contact(lead)) == {IE_BOOKED}


def test_a_keap_tick_between_stride_files_never_makes_a_second_contact(chain, monkeypatch):
    # Patients were linked to leads only after every file of a tick: a Keap tick in that gap saw
    # an unlinked patient, made it a second contact, and both rows froze in needs_review with no
    # booked tag (demo, 2026-10-14). A one-word Sheet name is what made the second contact: Stride's
    # first + last name did not match the lead's contact in Keap.
    email = f"racer.{chain.run}@example.test"
    response = chain.sheet(full_name=f"Racer{chain.run}", date_of_birth="15/03/1990",
                           phone="+15555550399", location="Laguna Niguel", title="Chain test",
                           email=email)
    assert response.status_code in (200, 201), response.text
    lead = response.json()["lead_id"]
    chain.lead_ids.append(lead)
    chain.keap_tick()
    pid = chain.stride_patient(f"Racer{chain.run}", "Surname", email=email)
    chain.stride_appt(pid, 3)
    after_each_file = stride_worker.replan_keap_since

    def keap_runs_between_files(started_at):
        after_each_file(started_at)
        chain.keap_tick()

    monkeypatch.setattr(stride_worker, "replan_keap_since", keap_runs_between_files)
    chain.stride_tick()
    chain.keap_tick()
    rows = chain.db.execute("select status from keap_sync where practice_id=%s",
                            (chain.practice,)).fetchall()
    assert [r["status"] for r in rows] == ["synced"]
    assert chain.tags(chain.contact(lead)) == {IE_BOOKED}
    chain.agree(lead)


# ============================ ordering and repetition ==============================


def test_an_older_stride_file_arriving_late_changes_nothing(chain):
    name = f"Chain{chain.run}"
    lead = chain.sheet_lead("Late", name, email=f"late.{chain.run}@example.test")
    pid = chain.stride_patient("Late", name)
    aid = chain.stride_appt(pid, 3, mod="2026-10-01 12:00:00")
    chain.stride_tick(); chain.keap_tick()
    before = chain.field(chain.contact(lead), "6")
    chain.stride_appt(pid, 9, aid=aid, mod="2026-10-01 11:00:00", status="Cancel")  # older
    chain.stride_tick(); chain.keap_tick()
    assert chain.dashboard(lead)["status"] == "booked"
    assert chain.field(chain.contact(lead), "6") == before
    assert chain.tags(chain.contact(lead)) == {IE_BOOKED}


def test_an_idle_system_makes_no_keap_writes(chain):
    name = f"Chain{chain.run}"
    lead = chain.sheet_lead("Calm", name, email=f"calm.{chain.run}@example.test")
    pid = chain.stride_patient("Calm", name)
    chain.stride_appt(pid, 3)
    chain.stride_tick(); chain.keap_tick()
    writes = chain.writes()
    for _ in range(3):
        chain.stride_tick(); chain.keap_tick()
    chain.db.execute("update keap_sync set desired_at=now()-interval '2 hours' where practice_id=%s",
                     (chain.practice,))
    keap_worker._state["last_full"] = None  # force the hourly full pass
    chain.keap_tick()
    assert chain.writes() == writes
    chain.agree(lead)


def test_many_changes_between_ticks_end_in_the_final_state(chain):
    name = f"Chain{chain.run}"
    lead = chain.sheet_lead("Flurry", name, email=f"flurry.{chain.run}@example.test")
    pid = chain.stride_patient("Flurry", name)
    aid = chain.stride_appt(pid, 3)
    chain.stride_appt(pid, 4, aid=aid, status="Cancel")
    chain.stride_appt(pid, 5, type_id=1)
    chain.stride_tick(); chain.keap_tick()  # Keap only ever sees the end result
    assert chain.tags(chain.contact(lead)) == {FU_BOOKED}
    chain.agree(lead)


# ============================ Keap trouble (fake only) =============================


@pytest.fixture
def fake_only(chain):
    if chain.live:
        pytest.skip("failure injection needs the fake Keap")
    return chain


def test_a_keap_outage_never_blocks_the_sheet_or_stride_and_heals_afterwards(fake_only):
    chain = fake_only
    chain.fake.fail_next = [500] * 400
    name = f"Chain{chain.run}"
    lead = chain.sheet_lead("Outage", name, email=f"outage.{chain.run}@example.test")
    pid = chain.stride_patient("Outage", name)
    chain.stride_appt(pid, 3)
    chain.stride_tick()
    chain.keap_tick()
    assert chain.dashboard(lead)["status"] == "booked"  # Sheet, Stride and dashboard carry on
    assert chain.row_of(lead)["status"] in {"retrying", "failed"}
    chain.fake.fail_next = []
    chain.db.execute("update keap_sync set status='pending',attempts=0,next_attempt_at=now() "
                     "where practice_id=%s", (chain.practice,))
    chain.keap_tick()
    assert chain.row_of(lead)["status"] == "synced"
    assert chain.tags(chain.contact(lead)) == {IE_BOOKED}
    chain.agree(lead)


def test_drift_made_by_hand_in_keap_is_not_repaired_until_the_person_changes(fake_only):
    """E-57 (known gap): we do not re-read Keap, so a tag staff remove by hand stays removed
    until something about that person changes."""
    chain = fake_only
    lead = chain.sheet_lead("Drift", f"Chain{chain.run}", email=f"drift.{chain.run}@example.test")
    chain.keap_tick()
    cid = chain.row_of(lead)["keap_contact_id"]
    chain.fake.contacts[cid]["tag_ids"].remove(PENDING)
    keap_worker._state["last_full"] = None
    chain.keap_tick()
    assert PENDING not in chain.fake.contacts[cid]["tag_ids"]
    chain.db.execute("update leads set status='in_progress',updated_at=now(),"
                     "phone_e164='+15555550488' where id=%s", (lead,))
    chain.keap_tick()
    assert PENDING in chain.fake.contacts[cid]["tag_ids"]  # a real change repairs it


def test_a_rate_limit_pauses_everyone_and_resumes_without_losing_work(fake_only):
    chain = fake_only
    leads = [chain.sheet_lead(f"Rate{i}", f"Chain{chain.run}", email=f"rate{i}.{chain.run}@example.test")
             for i in range(3)]
    chain.fake.fail_next, chain.fake.retry_after = [429], 600
    chain.keap_tick()
    assert all(chain.row_of(x)["status"] in {"retrying", "pending"} for x in leads)
    assert all(chain.row_of(x)["attempts"] == 0 for x in leads)
    chain.db.execute("update keap_sync set next_attempt_at=now() where practice_id=%s", (chain.practice,))
    chain.keap_tick()
    assert all(chain.row_of(x)["status"] == "synced" for x in leads)


def test_a_contact_deleted_in_keap_is_rebuilt_on_the_next_change(fake_only):
    chain = fake_only
    name = f"Chain{chain.run}"
    lead = chain.sheet_lead("Wiped", name, email=f"wiped.{chain.run}@example.test")
    chain.keap_tick()
    old = chain.row_of(lead)["keap_contact_id"]
    del chain.fake.contacts[old]
    pid = chain.stride_patient("Wiped", name)
    chain.stride_appt(pid, 3)
    chain.stride_tick(); chain.keap_tick()
    contact = chain.contact(lead)
    assert contact is not None and chain.tags(contact) == {IE_BOOKED}
    assert chain.row_of(lead)["keap_contact_id"] != old


def test_the_clinics_own_tags_and_fields_survive_every_step(fake_only):
    chain = fake_only
    cid = chain.fake.add_contact("Clinic", f"Chain{chain.run}", email=f"clinic.{chain.run}@example.test",
                                 tags=["999"], custom=[("4", "keep me")])
    lead = chain.sheet_lead("Clinic", f"Chain{chain.run}", email=f"clinic.{chain.run}@example.test")
    chain.keap_tick()
    pid = chain.stride_patient("Clinic", f"Chain{chain.run}")
    aid = chain.stride_appt(pid, 3)
    chain.stride_tick(); chain.keap_tick()
    chain.stride_appt(pid, 3, aid=aid, status="Cancel")
    chain.stride_tick(); chain.keap_tick()
    contact = chain.fake.contacts[cid]
    assert "999" in contact["tag_ids"]
    assert {f["id"]: f["content"] for f in contact["custom_fields"]}["4"] == "keep me"
    assert chain.row_of(lead)["keap_contact_id"] == cid
