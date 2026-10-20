"""Keap phase 2A against a real Postgres schema (TEST_DATABASE_URL, never production).

Each test builds its own practice, mapping, synthetic leads (555 phones, example.test
emails) and Stride rows, calls plan_changes and asserts on keap_sync.desired.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from rpt_agent import cli
from rpt_agent.config import get_settings
from rpt_agent.db import get_pool, transaction
from rpt_agent.services import keap_desired

pytestmark = pytest.mark.integration

PENDING = "144"


@pytest.fixture
def env(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    slug = f"test-keap-{uuid4().hex[:10]}"
    monkeypatch.setenv("SUPABASE_DB_URL", url)
    monkeypatch.setenv("KEAP_INCLUDE_TEST_LEADS", "true")
    monkeypatch.setenv("STRIDE_PRACTICE_SLUG", slug)
    get_settings.cache_clear()
    get_pool.cache_clear()
    conn = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    practice = conn.execute(
        "insert into practices(name,slug,timezone) values('Synthetic Practice',%s,'America/New_York') "
        "returning id", (slug,),
    ).fetchone()["id"]
    conn.execute(
        "insert into practice_settings(practice_id,stride_location_timezone,keap_pending_tag_id,"
        "keap_gave_up_tag_id,keap_sync_start_at,keap_include_existing_patients) "
        "values(%s,'America/New_York',%s,'186',now()-interval '1 day',true)",
        (practice, PENDING),
    )
    for type_id, label, booked, done in ((2, "Initial Evaluation", "180", "172"),
                                         (1, "PT", "184", "176")):
        conn.execute(
            "insert into keap_appointment_types(practice_id,stride_appointment_type_id,label,"
            "booked_tag_id,completed_tag_id,date_field_id,time_field_id,clinician_field_id) "
            "values(%s,%s,%s,%s,%s,'6','8','10')", (practice, type_id, label, booked, done),
        )

    class Env:
        db = conn
        practice_id = practice
        _next = 1000

        def one(self, sql, *args):
            return conn.execute(sql, args).fetchone()

        def setting(self, **values):
            for key, value in values.items():
                conn.execute(f"update practice_settings set {key}=%s where practice_id=%s",
                             (value, practice))

        def lead(self, name="Asha Rao", status="in_progress", email=None, phone="+15555550101",
                 is_test=True, dob="1990-02-03"):
            first, _, last = name.partition(" ")
            return conn.execute(
                "insert into leads(practice_id,source_system,full_name,first_name,last_name,email,"
                "phone_e164,date_of_birth,status,cadence_state,is_test) "
                "values(%s,'google_sheets',%s,%s,%s,%s,%s,%s,%s,'active',%s) returning id",
                (practice, name, first, last or None,
                 email or f"{first.lower()}@example.test", phone, dob, status, is_test),
            ).fetchone()["id"]

        def patient(self, first="Pat", last="Walker", lead_id=None, email=None, dob="1980-01-01",
                    created=None, **extra):
            Env._next += 1
            pid = Env._next
            conn.execute(
                "insert into stride_patients(practice_id,stride_id,first_name,last_name,"
                "date_of_birth,preferred_email,lead_id,stride_created_at,mobile_phone) "
                "values(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (practice, pid, first, last, dob, email or f"{first.lower()}.s@example.test",
                 lead_id, created, "+15555550177"),
            )
            return pid

        def appt(self, pid, type_id=2, days=2, status="Unmarked", clinician=None,
                 local=None, deleted=False):
            Env._next += 1
            start = datetime.now(UTC) + timedelta(days=days)
            conn.execute(
                "insert into stride_appointments(practice_id,stride_id,patient_stride_id,"
                "appointment_type_id,appointment_type,status,start_utc,starts_at_local,"
                "clinician_id,clinician_name,deleted_at) values(%s,%s,%s,%s,'x',%s,%s,%s,7,%s,%s)",
                (practice, Env._next, pid, type_id, status, start,
                 local or (start - timedelta(hours=4)).replace(tzinfo=None, microsecond=0),
                 clinician, datetime.now(UTC) if deleted else None),
            )
            return Env._next

        def set_appt(self, aid, **cols):
            for key, value in cols.items():
                conn.execute(f"update stride_appointments set {key}=%s where practice_id=%s "
                             "and stride_id=%s", (value, practice, aid))

        def plan(self, **kw):
            with transaction() as c:
                return keap_desired.plan_changes(c, practice, **kw)

        def row(self, lead_id=None, pid=None):
            if lead_id:
                return conn.execute("select * from keap_sync where lead_id=%s", (lead_id,)).fetchone()
            return conn.execute("select * from keap_sync where practice_id=%s and "
                                "stride_patient_id=%s", (practice, pid)).fetchone()

        def tags(self, **ref):
            return self.row(**ref)["desired"]["tags"]

        def rows(self):
            return conn.execute("select * from keap_sync where practice_id=%s", (practice,)).fetchall()

    yield Env()

    conn.execute("delete from keap_sync where practice_id=%s", (practice,))
    conn.execute("delete from appointments where practice_id=%s", (practice,))
    conn.execute("delete from clinicians where practice_id=%s", (practice,))
    conn.execute("delete from case_types where practice_id=%s", (practice,))
    conn.execute("delete from keap_appointment_types where practice_id=%s", (practice,))
    for table in ("stride_appointments", "stride_patients", "stride_users"):
        conn.execute(f"delete from {table} where practice_id=%s", (practice,))
    conn.execute("delete from leads where practice_id=%s", (practice,))
    conn.execute("delete from practice_settings where practice_id=%s", (practice,))
    conn.execute("delete from practices where id=%s", (practice,))
    conn.close()
    get_pool.cache_clear()
    get_settings.cache_clear()


def test_new_sheet_lead_gets_one_row_pending_and_queued(env):
    lead = env.lead()
    counts = env.plan()
    row = env.row(lead_id=lead)
    assert counts["leads_added"] == 1 and counts["queued"] == 1
    assert row["status"] == "pending" and row["desired"]["tags"] == [PENDING]
    assert row["desired_hash"] and row["desired"]["sources"]["lead_id"] == str(lead)


def test_recompute_without_change_keeps_hash_and_status(env):
    lead = env.lead()
    env.plan()
    env.db.execute("update keap_sync set status='synced',synced_hash=desired_hash where lead_id=%s",
                   (lead,))
    before = env.row(lead_id=lead)
    counts = env.plan(full=True)
    after = env.row(lead_id=lead)
    assert counts["queued"] == 0
    assert after["desired_hash"] == before["desired_hash"] and after["status"] == "synced"


def test_lead_that_books_loses_pending_and_gets_the_type_tag_and_fields(env):
    lead = env.lead()
    pid = env.patient(lead_id=lead)
    env.db.execute("update leads set stride_patient_id=%s where id=%s", (pid, lead))
    env.plan()
    assert env.tags(lead_id=lead) == [PENDING]
    env.appt(pid, 2, days=1, clinician="Jane Clinician", local=datetime(2026, 10, 15, 14, 30))  # noqa: DTZ001
    env.plan()
    d = env.row(lead_id=lead)["desired"]
    assert d["tags"] == ["180"] and d["fields"]["8"]["value"] == "2:30 PM"
    assert d["fields"]["10"]["value"] == "Jane Clinician"
    assert env.row(lead_id=lead)["status"] == "pending"


def test_voice_booked_lead_without_stride_appointment(env):
    lead = env.lead(status="booked")
    env.plan()
    assert env.tags(lead_id=lead) == []


def test_walk_in_patient_gets_a_patient_only_row_without_pending(env):
    pid = env.patient(first="Sky", last="Walker")
    env.appt(pid, 1, days=3)
    counts = env.plan()
    row = env.row(pid=pid)
    assert counts["patients_added"] == 1 and row["lead_id"] is None
    assert row["desired"]["tags"] == ["184"]


def test_check_in_adds_completed_and_cancel_removes_booked(env):
    lead = env.lead()
    pid = env.patient(lead_id=lead)
    aid = env.appt(pid, 2, days=2)
    env.plan()
    assert env.tags(lead_id=lead) == ["180"]
    env.set_appt(aid, status="Checked In", updated_at=datetime.now(UTC))
    env.plan()
    assert env.tags(lead_id=lead) == ["172", "180"]
    env.set_appt(aid, status="Cancel", updated_at=datetime.now(UTC))
    env.plan()
    row = env.row(lead_id=lead)
    assert row["desired"]["tags"] == [PENDING]
    assert all(f["value"] is None for f in row["desired"]["fields"].values())


def test_cancel_keeps_booked_when_setting_off(env):
    env.setting(keap_remove_booked_on_cancel=False)
    pid = env.patient()
    env.appt(pid, 2, status="Cancel")
    env.plan()
    assert env.tags(pid=pid) == ["180"]


def test_reschedule_moves_the_date(env):
    pid = env.patient()
    aid = env.appt(pid, 2, days=2)
    env.plan()
    first = env.row(pid=pid)["desired"]["fields"]["6"]["value"]
    env.set_appt(aid, starts_at_local=datetime(2027, 1, 5, 9, 0),  # noqa: DTZ001
                 start_utc=datetime.now(UTC) + timedelta(days=60), updated_at=datetime.now(UTC))
    env.plan()
    assert env.row(pid=pid)["desired"]["fields"]["6"]["value"] == "2027-01-05" != first


def test_appointment_before_go_live_and_unmapped_type_are_ignored(env):
    pid = env.patient()
    env.appt(pid, 2, days=-10, status="Checked In")
    env.appt(pid, 99, days=2)
    env.plan()
    assert env.tags(pid=pid) == []


def test_old_patients_follow_the_include_setting(env):
    env.setting(keap_include_existing_patients=False)
    old = env.patient(first="Old", created=datetime.now(UTC) - timedelta(days=400))
    new = env.patient(first="New", created=datetime.now(UTC))
    env.plan()
    assert env.row(pid=old) is None and env.row(pid=new) is not None


def test_stride_email_beats_lead_email_and_lead_phone_beats_stride_phone(env):
    lead = env.lead(email="lead@example.test")
    pid = env.patient(lead_id=lead, email="stride@example.test")
    env.db.execute("update leads set stride_patient_id=%s where id=%s", (pid, lead))
    env.plan()
    contact = env.row(lead_id=lead)["desired"]["contact"]
    assert contact["email"] == "stride@example.test" and contact["phone"] == "+15555550101"


def test_row_without_any_contact_detail_is_skipped(env):
    lead = env.lead(phone=None)
    env.db.execute("update leads set email=null where id=%s", (lead,))
    counts = env.plan()
    row = env.row(lead_id=lead)
    assert counts["skipped"] == 1
    assert row["status"] == "skipped" and row["skip_reason"] == "no contact details"


def test_sync_switched_off_skips_everyone(env):
    env.setting(keap_sync_start_at=None)
    lead = env.lead()
    env.plan()
    assert env.row(lead_id=lead)["skip_reason"] == "keap sync not started"


def test_test_leads_are_ignored_unless_enabled(env, monkeypatch):
    monkeypatch.setenv("KEAP_INCLUDE_TEST_LEADS", "false")
    get_settings.cache_clear()
    env.lead(is_test=True)
    real = env.lead(name="Real Person", is_test=False)
    env.plan()
    rows = env.rows()
    assert len(rows) == 1 and rows[0]["lead_id"] == real


def test_linking_a_patient_to_a_synced_lead_row_merges_without_a_new_row(env):
    lead = env.lead()
    env.plan()
    env.db.execute("update keap_sync set keap_contact_id='8842',status='synced' where lead_id=%s",
                   (lead,))
    pid = env.patient(first="Asha", last="Rao")
    env.plan()  # the patient arrives first as its own row
    assert env.row(pid=pid)["lead_id"] is None
    env.db.execute("update stride_patients set lead_id=%s where practice_id=%s and stride_id=%s",
                   (lead, env.practice_id, pid))
    counts = env.plan()
    rows = env.rows()
    assert counts["merged"] == 1 and len(rows) == 1
    assert rows[0]["lead_id"] == lead and rows[0]["stride_patient_id"] == pid
    assert rows[0]["keap_contact_id"] == "8842"


def test_two_different_keap_contacts_for_one_person_need_review(env):
    lead = env.lead()
    pid = env.patient(first="Asha", last="Rao")
    env.plan()
    env.db.execute("update keap_sync set keap_contact_id='8842' where lead_id=%s", (lead,))
    env.db.execute("update keap_sync set keap_contact_id='9001' where stride_patient_id=%s", (pid,))
    env.db.execute("update stride_patients set lead_id=%s where practice_id=%s and stride_id=%s",
                   (lead, env.practice_id, pid))
    first = env.plan()
    second = env.plan()
    assert first["duplicates"] == 1 and second["duplicates"] == 0
    assert {r["status"] for r in env.rows()} == {"needs_review"}
    assert len(env.rows()) == 2


def test_deleted_patient_row_is_left_alone(env):
    pid = env.patient()
    env.plan()
    row = env.row(pid=pid)
    env.db.execute("update keap_sync set status='synced',synced_hash=desired_hash where id=%s",
                   (row["id"],))
    env.db.execute("update stride_patients set deleted_at=now(),updated_at=now() "
                   "where practice_id=%s and stride_id=%s", (env.practice_id, pid))
    env.plan()
    after = env.row(pid=pid)
    assert after["desired_hash"] == row["desired_hash"] and after["status"] == "synced"


def test_change_while_syncing_updates_the_hash_but_keeps_the_status(env):
    lead = env.lead()
    env.plan()
    env.db.execute("update keap_sync set status='syncing' where lead_id=%s", (lead,))
    old_hash = env.row(lead_id=lead)["desired_hash"]
    env.db.execute("update leads set status='closed_no_response' where id=%s", (lead,))
    env.plan()
    row = env.row(lead_id=lead)
    assert row["status"] == "syncing" and row["desired_hash"] != old_hash
    assert row["desired"]["tags"] == ["186"]


def test_change_back_to_the_synced_state_is_not_resent(env):
    lead = env.lead()
    env.plan()
    env.db.execute("update keap_sync set status='synced',synced_hash=desired_hash where lead_id=%s",
                   (lead,))
    env.db.execute("update leads set status='closed_no_response' where id=%s", (lead,))
    env.plan()
    assert env.row(lead_id=lead)["status"] == "pending"
    env.db.execute("update leads set status='in_progress' where id=%s", (lead,))
    env.plan()
    assert env.row(lead_id=lead)["status"] == "synced"


def test_skipped_row_leaves_skipped_once_it_has_contact_details(env):
    lead = env.lead(phone=None)
    env.db.execute("update leads set email=null where id=%s", (lead,))
    env.plan()
    env.db.execute("update leads set email='fix@example.test' where id=%s", (lead,))
    env.plan()
    row = env.row(lead_id=lead)
    assert row["status"] == "pending" and row["skip_reason"] is None


def test_clinician_name_is_looked_up_in_stride_users(env):
    env.db.execute("insert into stride_users(practice_id,stride_id,first_name,last_name) "
                   "values(%s,7,'Pat','Therapist')", (env.practice_id,))
    pid = env.patient()
    env.appt(pid, 2, clinician=None)
    env.plan()
    assert env.row(pid=pid)["desired"]["fields"]["10"]["value"] == "Pat Therapist"


def test_bulk_first_load_spreads_patient_rows_but_not_leads(env):
    for i in range(205):
        env.patient(first=f"Bulk{i}", last="Walker")
    lead = env.lead()
    env.plan(limit=1000)
    assert env.row(lead_id=lead)["next_attempt_at"] <= datetime.now(UTC) + timedelta(seconds=5)
    delayed = env.one("select count(*) as n from keap_sync where practice_id=%s and "
                      "next_attempt_at>now()+interval '30 seconds'", env.practice_id)["n"]
    assert 0 < delayed <= 5


def test_a_time_passing_moves_the_date_without_any_data_change(env):
    pid = env.patient()
    first = env.appt(pid, 2, days=1)
    second = env.appt(pid, 2, days=5)
    env.plan()
    assert env.row(pid=pid)["desired"]["fields"]["6"]["value"] == env.one(
        "select starts_at_local::date::text as d from stride_appointments where practice_id=%s "
        "and stride_id=%s", env.practice_id, first)["d"]
    # the first visit is now in the past, but no row was touched
    env.set_appt(first, start_utc=datetime.now(UTC) - timedelta(hours=1))
    env.db.execute("update keap_sync set desired_at=now()-interval '2 hours' where stride_patient_id=%s",
                   (pid,))
    env.plan()
    assert env.row(pid=pid)["desired"]["fields"]["6"]["value"] == env.one(
        "select starts_at_local::date::text as d from stride_appointments where practice_id=%s "
        "and stride_id=%s", env.practice_id, second)["d"]


def test_dry_run_writes_nothing_and_prints_masked_output(env, capsys):
    env.lead()
    args = type("A", (), {"full": False, "dry_run": True, "limit": None})()
    cli.keap_plan(args)
    out = json.loads(capsys.readouterr().out)
    assert out["counts"]["dry_run"] is True
    assert env.rows() == []
    text = json.dumps(out)
    assert "asha@" not in text and "Asha" not in text


def test_planning_logs_contain_no_personal_data(env, caplog):
    env.lead(email="secret.person@example.test")
    with caplog.at_level("DEBUG"):
        env.plan()
    assert "secret.person" not in caplog.text and "5555550101" not in caplog.text


def test_a_change_stamped_before_the_plan_but_committed_after_is_still_planned(env):
    # Demo 2026-10-14: a Stride import stamped its rows at its transaction START (10:31:36) and
    # committed after the Keap plan (10:31:41). The visit looked older than the plan, so the
    # lead's contact never got its booked tag.
    lead = env.lead("Late Commit", status="booked")
    pid = env.patient("Late", "Commit", lead_id=lead)
    env.plan()
    assert env.tags(lead_id=lead) == []
    planned_at = env.row(lead_id=lead)["desired_at"]
    aid = env.appt(pid)
    env.set_appt(aid, updated_at=planned_at - timedelta(seconds=5))
    env.plan()
    assert env.tags(lead_id=lead) == ["180"]


def test_import_replans_keap_rows_planned_while_it_ran(env):
    from rpt_agent.stride_import_worker import replan_keap_since

    lead = env.lead("Replan Me")
    env.plan()
    before = env.row(lead_id=lead)["desired_at"]
    replan_keap_since(before + timedelta(seconds=1))  # an import that started after the plan
    assert env.row(lead_id=lead)["desired_at"] == before
    replan_keap_since(before - timedelta(seconds=1))  # one that started before it
    assert env.row(lead_id=lead)["desired_at"] is None
    env.plan()
    assert env.row(lead_id=lead)["desired_at"] is not None


def _our_booking(env, lead, *, state="scheduled", days=3, stride_appointment_id=None, clinician=5980):
    case = env.one("select id from case_types where practice_id=%s and stride_appointment_type_id=2",
                   env.practice_id)
    if case is None:
        case = env.one("insert into case_types(practice_id,name,stride_appointment_type_id,duration_minutes,"
                       "is_default) values(%s,'Physical Therapy',2,60,true) returning id", env.practice_id)
        env.db.execute("insert into clinicians(practice_id,stride_user_id,display_name) "
                       "values(%s,5980,'Joe Smith')", (env.practice_id,))
    start = datetime.now(UTC) + timedelta(days=days)
    return env.one(
        "insert into appointments(lead_id,practice_id,booking_source,state,start_utc,end_utc,"
        "clinician_id,case_type_id,stride_appointment_id) values(%s,%s,'voice_agent',%s,%s,%s,%s,%s,%s) "
        "returning id", lead, env.practice_id, state, start, start + timedelta(hours=1), clinician,
        case["id"], stride_appointment_id)["id"]


def test_elliot_booking_tags_keap_before_stride_exports_it(env):
    # Demo 2026-10-14: Elliot booked a lead; Keap lost Pending but got no booked tag because the
    # tag came only from Stride's export (which the demo Stride never sends).
    lead = env.lead(status="booked")
    _our_booking(env, lead, stride_appointment_id=77001)
    env.plan()
    d = env.row(lead_id=lead)["desired"]
    assert d["tags"] == ["180"] and d["fields"]["10"]["value"] == "Joe Smith"
    assert d["fields"]["6"]["value"] is not None


def test_stride_copy_replaces_our_booking_once_imported(env):
    lead = env.lead(status="booked")
    _our_booking(env, lead, stride_appointment_id=77002)
    pid = env.patient(lead_id=lead)
    env.db.execute("update leads set stride_patient_id=%s where id=%s", (pid, lead))
    env.db.execute("update keap_sync set stride_patient_id=%s where lead_id=%s", (pid, lead))
    env.plan()
    assert env.tags(lead_id=lead) == ["180"]
    # Stride's copy of that same visit arrives cancelled: Stride is the record, the tag goes.
    env.db.execute(
        "insert into stride_appointments(practice_id,stride_id,patient_stride_id,appointment_type_id,"
        "appointment_type,status,start_utc,clinician_id) values(%s,77002,%s,2,'x','Cancel',now()+interval '3 days',7)",
        (env.practice_id, pid))
    env.plan()
    assert env.tags(lead_id=lead) == []


def test_our_cancelled_booking_does_not_tag(env):
    lead = env.lead(status="needs_attention")
    _our_booking(env, lead, state="cancelled")
    env.plan()
    assert env.tags(lead_id=lead) == [PENDING]


def test_an_upcoming_elliot_booking_on_the_old_lead_counts_for_the_new_one(env):
    # Decision 2026-10-14: a repeat referral before the visit stays booked in Keap.
    old = env.lead(status="booked")
    _our_booking(env, old, stride_appointment_id=77010)
    new = env.lead()
    env.plan()
    assert env.tags(lead_id=new) == ["180"]
    assert env.row(lead_id=old)["status"] == "skipped"


def test_a_carried_booking_that_stride_cancelled_no_longer_counts(env):
    old = env.lead(status="booked")
    _our_booking(env, old, stride_appointment_id=77011)
    pid = env.patient(first="Asha", last="Rao", lead_id=old)
    env.db.execute(
        "insert into stride_appointments(practice_id,stride_id,patient_stride_id,appointment_type_id,"
        "appointment_type,status,start_utc,clinician_id) values(%s,77011,%s,2,'x','Cancel',"
        "now()+interval '3 days',7)", (env.practice_id, pid))
    new = env.lead()
    env.plan()
    assert env.tags(lead_id=new) == [PENDING]


def test_a_family_member_on_the_same_phone_does_not_carry_the_booking(env):
    parent = env.lead(status="booked")
    _our_booking(env, parent, stride_appointment_id=77012)
    child = env.lead(name="Ravi Rao")
    env.plan()
    assert env.tags(lead_id=child) == [PENDING]
    assert env.row(lead_id=parent)["status"] != "skipped"
