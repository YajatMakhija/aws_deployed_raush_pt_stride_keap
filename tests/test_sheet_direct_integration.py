"""Direct Google Sheet sync end to end: fake Sheet, real Postgres schema.

Needs TEST_DATABASE_URL (a disposable database with the migrations applied, never production).
Each test builds its own practice and cadence, uses synthetic 555 numbers, and removes them.
"""

import os
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from fake_sheet import FakeSheet
from psycopg.rows import dict_row

from rpt_agent import sheet_sync_worker
from rpt_agent.config import get_settings
from rpt_agent.db import get_pool
from rpt_agent.observability import WorkflowTrace
from rpt_agent.services import sheet_direct
from rpt_agent.services.google_sheet import parse_grid
from rpt_agent.services.lead_actions import execute_lead_action
from rpt_agent.services.sheet_sync import enqueue_sheet_update
from rpt_agent.sheet_sync_worker import run_direct_tick

pytestmark = pytest.mark.integration
STEPS = [(0, "call"), (0, "sms"), (3, "call"), (5, "sms")]


@pytest.fixture
def env(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    slug = f"test-sheet-{uuid4().hex[:10]}"
    for key, value in {
        "SUPABASE_DB_URL": url, "N8N_PRACTICE_SLUG": slug, "SHEET_SYNC_ENABLED": "true",
        "SHEET_SYNC_MODE": "direct", "DASHBOARD_PUBLIC_URL": "https://dash.example.test",
        "GOOGLE_SHEET_ID": "sheet", "SHEET_STUCK_SECONDS": "120", "SHEET_SETTLE_SECONDS": "0",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    get_pool.cache_clear()
    monkeypatch.setattr(sheet_sync_worker, "_direct_state",
                        {"last_full": 0.0, "archived_on": None, "settle": None, "profiles": None})
    conn = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    practice = conn.execute(
        "insert into practices(name,slug,timezone) values('Synthetic',%s,'America/Los_Angeles') "
        "returning id", (slug,)).fetchone()["id"]
    conn.execute("insert into practice_settings(practice_id) values(%s)", (practice,))
    version = conn.execute(
        "insert into cadence_versions(practice_id,version_number,name,status,activated_at) "
        "values(%s,1,'Synthetic v1','active',now()) returning id", (practice,)).fetchone()["id"]
    for order, (day, channel) in enumerate(STEPS):
        conn.execute(
            "insert into cadence_steps(practice_id,cadence_version_id,step_order,day_offset,"
            "channel,key,description,is_active) values(%s,%s,%s,%s,%s,%s,%s,true)",
            (practice, version, order, day, channel, f"s{order}-{uuid4().hex[:6]}", "step"))
    # Outbox rows other tests left behind must not be delivered by this test's ticks.
    conn.execute("update integration_outbox set status='dead' where destination='n8n' "
                 "and status in ('pending','sending')")

    class Env:
        db = conn
        practice_id = practice
        sheet = FakeSheet()

        def tick(self, full=True):
            return run_direct_tick(self.sheet, full=full)

        def start_row(self, phone=None, **cells):
            values = {"name": "Asha Rao", "phone": phone or _phone(), "email": "asha@example.test",
                      "date_of_birth": "16/01/1990", "location": "Dana Point", "case": "Ortho",
                      "action": "Start cadence"}
            values.update(cells)
            return self.sheet.add(**values)

        def cell(self, number, key):
            return self.sheet.cell(number, key)

        def leads(self):
            return conn.execute("select * from leads where practice_id=%s order by created_at",
                                (practice,)).fetchall()

        def lead(self, lead_id):
            return conn.execute("select * from leads where id=%s", (lead_id,)).fetchone()

    yield Env()

    leads = conn.execute("select id::text as id,phone_e164 from leads where practice_id=%s",
                         (practice,)).fetchall()
    ids, phones = [r["id"] for r in leads], [r["phone_e164"] for r in leads]
    conn.execute("delete from suppressed_numbers where phone_e164=any(%s)", (phones,))
    conn.execute("delete from integration_outbox where aggregate_id=any(%s)", (ids,))
    conn.execute("delete from lead_action_requests where practice_id=%s", (str(practice),))
    conn.execute("delete from leads where practice_id=%s", (practice,))
    conn.execute("delete from practices where id=%s", (practice,))
    conn.close()
    get_pool.cache_clear()
    get_settings.cache_clear()


def _phone():
    return f"555{uuid4().int % 10_000_000:07d}"


def test_a_start_row_creates_the_lead_and_the_sheet_shows_it(env):
    number = env.start_row()
    env.tick()
    lead_id = env.cell(number, "lead_id")
    assert UUID(lead_id)
    assert env.cell(number, "outcome_status") == "Cadence started"
    assert env.cell(number, "lead_status") == "In cadence"
    assert env.cell(number, "transcript_link") == (
        f"https://dash.example.test/leads/{lead_id}/conversations/calls")
    assert UUID(env.cell(number, "action_request_id"))
    lead = env.lead(lead_id)
    assert lead["full_name"] == "Asha Rao" and lead["source_system"] == "google_sheets"
    assert str(lead["date_of_birth"]) == "1990-01-16"
    events = env.db.execute("select count(*) as n from outreach_events where lead_id=%s",
                            (lead_id,)).fetchone()["n"]
    assert events == len(STEPS)
    # Our own writes never re-trigger: more polls change nothing.
    writes = len(env.sheet.writes)
    env.tick()
    env.tick()
    assert len(env.leads()) == 1 and len(env.sheet.writes) == writes


def test_an_invalid_row_shows_why_and_runs_once_staff_fix_it(env):
    number = env.start_row(date_of_birth="")
    env.tick()
    assert env.cell(number, "outcome_status") == (
        "Error: Date Of Birth is required")
    assert env.cell(number, "action_request_id") == "" and env.leads() == []
    env.tick()
    assert env.leads() == []  # an Error is not retried by itself
    env.sheet.set(number, "date_of_birth", "16/01/1990")
    env.sheet.set(number, "outcome_status", "")
    env.tick()
    assert env.cell(number, "outcome_status") == "Cadence started" and len(env.leads()) == 1


def test_do_not_contact_and_booked_from_the_sheet(env):
    first, second = env.start_row(), env.start_row(name="Ravi Rao")
    env.tick()
    env.sheet.set(first, "action", "Do Not Contact")
    env.sheet.set(first, "outcome_status", "")
    env.sheet.set(second, "action", "Booked")
    env.sheet.set(second, "outcome_status", "")
    env.tick()
    assert env.cell(first, "outcome_status") == "Do Not Contact applied"
    assert env.cell(first, "lead_status") == "Do not outreach"
    assert env.lead(env.cell(first, "lead_id"))["status"] == "do_not_contact"
    assert env.cell(second, "outcome_status") == "Booked"
    assert env.cell(second, "lead_status") == "Booked"
    assert env.lead(env.cell(second, "lead_id"))["status"] == "booked"
    assert env.cell(second, "action") == "Booked"


def test_a_row_left_processing_is_replayed_not_started_twice(env):
    # The worker stopped after the backend committed but before the Sheet heard back.
    phone = _phone()
    request_id = uuid4()
    execute_lead_action(request_id=request_id, action="start_cadence", lead_id=None, lead={
        "full_name": "Asha Rao", "phone": "+1" + phone, "email": "asha@example.test",
        "date_of_birth": date(1990, 1, 16), "location": "Dana Point", "title": "Ortho",
        "lead_type": "Ortho"})
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    number = env.start_row(phone=phone, outcome_status="Processing: Start cadence",
                           action_request_id=str(request_id), action_started_at=old)
    env.tick()
    assert env.cell(number, "outcome_status") == "Cadence started"
    assert env.cell(number, "lead_id") == str(env.leads()[0]["id"])
    assert len(env.leads()) == 1


def test_a_fresh_processing_row_is_left_alone_until_it_is_stale(env):
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    number = env.start_row(outcome_status="Processing: Start cadence",
                           action_request_id=str(uuid4()), action_started_at=now)
    env.tick()
    assert env.cell(number, "outcome_status") == "Processing: Start cadence" and env.leads() == []


def test_profile_edits_sync_and_a_name_change_goes_to_review(env):
    number = env.start_row()
    env.tick()
    lead_id = env.cell(number, "lead_id")
    env.sheet.set(number, "email", "new@example.test")
    env.sheet.set(number, "case", "Pelvic")
    env.tick()
    assert env.lead(lead_id)["email"] == "asha@example.test"  # seen once: wait for it to hold
    env.tick()
    lead = env.lead(lead_id)
    assert lead["email"] == "new@example.test" and lead["lead_type"] == "Pelvic"
    assert env.cell(number, "outcome_status") == "Cadence started"  # success writes nothing
    env.sheet.set(number, "name", "Asha Kumar")
    env.tick()
    env.tick()
    assert env.cell(number, "outcome_status") == "Name changed; needs review"
    assert env.cell(number, "lead_status") == "Needs Review"
    assert env.lead(lead_id)["needs_review"] is True
    assert env.lead(lead_id)["full_name"] == "Asha Rao"  # identity is never overwritten


def test_call_results_are_written_into_the_leads_row(env):
    number = env.start_row()
    env.tick()
    lead_id = env.cell(number, "lead_id")
    event = env.db.execute(
        "update outreach_events set status='delivered',outcome='no_answer',settled_at=now(),"
        "executed_at=now() where id=(select id from outreach_events where lead_id=%s "
        "and channel='call' order by day_offset limit 1) returning id", (lead_id,)).fetchone()
    with env.db.transaction():
        enqueue_sheet_update(env.db, lead_id=lead_id, event_type="call_settled",
                             source_key=f"call:{event['id']}")
    env.tick()
    assert env.cell(number, "cadence_day") == "Day 0"
    assert env.cell(number, "call_outcome") == "No answer"
    assert env.cell(number, "outcome_status") == "Cadence started"
    status = env.db.execute("select status from integration_outbox where aggregate_id=%s "
                            "and event_type='sheet.call_settled'", (lead_id,)).fetchone()
    assert status["status"] == "delivered"


def test_a_result_for_a_lead_missing_from_the_sheet_is_retried_not_lost(env):
    number = env.start_row()
    env.tick()
    lead_id = env.cell(number, "lead_id")
    env.sheet.set(number, "lead_id", "")  # staff cleared it by mistake
    with env.db.transaction():
        enqueue_sheet_update(env.db, lead_id=lead_id, event_type="cadence_finished",
                             source_key="test")
    env.tick()
    row = env.db.execute("select status,last_error from integration_outbox where aggregate_id=%s "
                         "and event_type='sheet.cadence_finished'", (lead_id,)).fetchone()
    assert row["status"] == "pending" and "not found" in row["last_error"]


def test_a_renamed_column_stops_the_sync_and_says_why(env):
    env.sheet.values[1][env.sheet.values[1].index("Outcome Status")] = "Status"
    env.start_row()
    assert env.tick()["claimed"] == 0
    beat = env.db.execute("select details from service_heartbeats where service='sheet-worker'"
                          ).fetchone()["details"]
    assert beat["ok"] is False and "Outcome Status" in beat["error"]
    assert env.leads() == []


def test_a_row_inserted_while_a_command_is_claimed_never_hits_the_wrong_patient(env):
    first = env.start_row()
    env.sheet.before_write = lambda: env.sheet.insert_row_above(first)  # staff insert a row
    env.tick()
    assert env.leads() == [] and env.sheet.writes == []  # nothing claimed, nothing run
    env.tick()  # next poll reads the new layout and runs the command on the moved row
    moved = first + 1
    assert env.cell(moved, "outcome_status") == "Cadence started"
    assert env.cell(first, "outcome_status") == "" and len(env.leads()) == 1


def test_recovery_replays_the_stored_result_even_after_staff_fixed_the_name(env):
    # Review 2026-10-14: rebuilding from the edited row gave "Request ID was reused" and the
    # Sheet never learnt the Lead ID of a lead that was already being called.
    phone = _phone()
    request_id = uuid4()
    execute_lead_action(request_id=request_id, action="start_cadence", lead_id=None, lead={
        "full_name": "Asha Rao", "phone": "+1" + phone, "email": "asha@example.test",
        "date_of_birth": date(1990, 1, 16), "location": "Dana Point", "title": "Ortho",
        "lead_type": "Ortho"})
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    number = env.start_row(phone=phone, name="Asha Rao-Kumar", action_started_at=old,
                           outcome_status="Processing: Start cadence",
                           action_request_id=str(request_id))
    env.tick()
    assert env.cell(number, "outcome_status") == "Cadence started"
    assert env.cell(number, "lead_id") == str(env.leads()[0]["id"]) and len(env.leads()) == 1


def test_a_row_copied_with_the_old_request_id_is_its_own_person(env):
    # Review 2026-10-14: a copied row for a family member must not inherit the other's lead.
    phone = _phone()
    first = env.start_row(phone=phone)
    env.tick()
    copy = env.start_row(phone=phone, name="Ravi Rao",
                         action_request_id=env.cell(first, "action_request_id"))
    env.tick()
    assert env.cell(copy, "outcome_status") == "Cadence started"
    assert env.cell(copy, "lead_id") not in ("", env.cell(first, "lead_id"))
    assert env.lead(env.cell(copy, "lead_id"))["full_name"] == "Ravi Rao"


def test_a_start_row_rerun_after_a_lost_result_links_the_lead_instead_of_starting_twice(env):
    number = env.start_row()
    env.tick()
    request_id, lead_id = env.cell(number, "action_request_id"), env.cell(number, "lead_id")
    env.sheet.set(number, "lead_id", "")       # the result never reached the Sheet,
    env.sheet.set(number, "outcome_status", "")  # and staff cleared the status to try again
    env.tick()
    assert env.cell(number, "lead_id") == lead_id and len(env.leads()) == 1
    assert env.cell(number, "action_request_id") == request_id


def test_lower_case_actions_are_ignored_as_n8n_ignored_them(env):
    number = env.start_row(action="start cadence")
    env.tick()
    assert env.cell(number, "outcome_status") == "" and env.leads() == []


def test_only_one_sheet_worker_polls_at_a_time(env):
    holder = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
    try:
        holder.execute("select pg_advisory_lock(hashtext('rpt:sheet-direct'))")
        env.start_row()
        assert env.tick().get("locked") == 1 and env.leads() == []
    finally:
        holder.close()
    env.tick()
    assert len(env.leads()) == 1


# --- narrow polls (no full read) ------------------------------------------------------------


def test_a_narrow_poll_runs_a_new_command_without_reading_the_whole_tab(env):
    for _ in range(50):  # a busy Sheet: finished rows the narrow poll must not load
        env.sheet.add(name="Old Patient", phone=_phone(), action="Booked", outcome_status="Booked",
                      lead_id=str(uuid4()))
    number = env.start_row()
    env.tick(full=False)
    assert env.sheet.reads == 0  # never the whole tab
    assert env.cell(number, "outcome_status") == "Cadence started" and len(env.leads()) == 1


def test_a_narrow_poll_writes_results_and_recovers_stuck_rows(env):
    number = env.start_row()
    env.tick(full=False)
    lead_id = env.cell(number, "lead_id")
    with env.db.transaction():
        enqueue_sheet_update(env.db, lead_id=lead_id, event_type="cadence_finished",
                             source_key="narrow")
    env.db.execute("update leads set cadence_state='completed' where id=%s", (lead_id,))
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    stuck = env.start_row(outcome_status="Processing: Start cadence",
                          action_request_id=str(uuid4()), action_started_at=old)
    env.tick(full=False)
    assert env.cell(number, "outcome_status") == "Cadence completed"
    assert env.cell(stuck, "outcome_status") == "Cadence started" and len(env.leads()) == 2
    assert env.sheet.reads == 0


def test_profile_edits_wait_for_the_full_read(env):
    number = env.start_row()
    env.tick()
    lead_id = env.cell(number, "lead_id")
    env.sheet.set(number, "email", "later@example.test")
    env.tick(full=False)
    env.tick(full=False)
    assert env.lead(lead_id)["email"] == "asha@example.test"
    env.tick(full=True)
    env.tick(full=True)
    assert env.lead(lead_id)["email"] == "later@example.test"


def test_a_half_edited_row_is_never_sent_as_a_name_change(env):
    # Staff retype a row over two full reads: the name first, the corrected phone later.
    number = env.start_row()
    env.tick()
    lead_id = env.cell(number, "lead_id")
    env.tick()
    env.sheet.set(number, "name", "Asha R")       # caught mid-edit by one full read
    env.tick()
    env.sheet.set(number, "name", "Asha Rao")     # finished: back to the stored name
    env.tick()
    env.tick()
    assert env.lead(lead_id)["needs_review"] is False
    assert env.cell(number, "outcome_status") == "Cadence started"


def test_a_command_waits_until_the_row_stops_changing(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "sheet_settle_seconds", 60)
    clock = {"t": 1000.0}
    monkeypatch.setattr(sheet_sync_worker, "_clock", lambda: clock["t"])
    number = env.start_row(date_of_birth="")  # Action picked before the row is finished
    env.tick(full=False)
    assert env.cell(number, "outcome_status") == ""  # no error yet: staff are still typing
    clock["t"] += 30
    env.sheet.set(number, "date_of_birth", "16/01/1990")  # finished 30 s later
    env.tick(full=False)
    clock["t"] += 45
    env.tick(full=False)
    assert env.cell(number, "outcome_status") == "" and env.leads() == []  # 45 s still: wait
    clock["t"] += 20
    env.tick(full=False)
    assert env.cell(number, "outcome_status") == "Cadence started" and len(env.leads()) == 1


def test_a_pasted_block_is_worked_through_a_few_rows_per_poll(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "sheet_max_commands_per_poll", 3)
    numbers = [env.start_row(name=f"Patient {i}") for i in range(7)]
    env.tick(full=False)
    assert len(env.leads()) == 3
    env.tick(full=False)
    env.tick(full=False)
    assert len(env.leads()) == 7
    assert all(env.cell(n, "outcome_status") == "Cadence started" for n in numbers)


# --- nightly archive ------------------------------------------------------------------------


def _finished(env, number, *, status="booked", days_ago=40, review=False):
    lead_id = env.cell(number, "lead_id")
    env.db.execute(
        "update leads set status=%s,cadence_state='completed',needs_review=%s,"
        "status_changed_at=now()-make_interval(days=>%s) where id=%s",
        (status, review, days_ago, lead_id))
    return lead_id


def _archive(env, days=30):
    grid = parse_grid(env.sheet.read_values(), 2)
    trace = WorkflowTrace("t", "t")
    return sheet_direct.archive(env.sheet, grid, days, "Archive", trace)


def test_finished_quiet_rows_move_to_the_archive_and_others_stay(env):
    done = env.start_row(name="Done Patient")
    recent = env.start_row(name="Recent Patient")
    review = env.start_row(name="Review Patient")
    active = env.start_row(name="Active Patient")
    dnc = env.start_row(name="Dnc Patient")
    env.tick()
    done_id = _finished(env, done)
    _finished(env, recent, days_ago=3)
    _finished(env, review, review=True)
    _finished(env, dnc, status="do_not_contact")
    active_id = env.cell(active, "lead_id")
    assert _archive(env) == 2
    names = [row[0] for row in env.sheet.values[2:]]
    assert names == ["Recent Patient", "Review Patient", "Active Patient"]
    archived_names = [row[0] for row in env.sheet.archive[2:]]
    assert sorted(archived_names) == ["Dnc Patient", "Done Patient"]
    assert env.sheet.archive[1] == env.sheet.values[1]  # same headers
    state = env.db.execute("select archived_at from sheet_row_state where lead_id=%s",
                           (done_id,)).fetchone()
    assert state["archived_at"] is not None
    # A later update for an archived lead is dropped quietly, not retried as "not found".
    with env.db.transaction():
        enqueue_sheet_update(env.db, lead_id=done_id, event_type="lead_booked", source_key="late")
        enqueue_sheet_update(env.db, lead_id=active_id, event_type="call_settled", source_key="x")
    env.tick()
    row = env.db.execute("select status from integration_outbox where aggregate_id=%s "
                         "and event_type='sheet.lead_booked'", (done_id,)).fetchone()
    assert row["status"] == "delivered"


def test_a_row_that_moved_before_the_archive_is_left_alone(env):
    done = env.start_row(name="Done Patient")
    env.tick()
    _finished(env, done)
    env.sheet.before_write = lambda: env.sheet.insert_row_above(done)
    assert _archive(env) == 0 and env.sheet.archive is None


def test_the_archive_runs_once_in_its_hour_and_only_when_enabled(env, monkeypatch):
    settings = get_settings()
    at = {"now": datetime(2026, 10, 15, 3, 10, tzinfo=sheet_direct.SHEET_TZ)}
    monkeypatch.setattr(sheet_sync_worker, "_pacific_now", lambda: at["now"])
    assert not sheet_sync_worker._archive_due(settings)  # off by default
    monkeypatch.setattr(settings, "sheet_archive_enabled", True)
    assert sheet_sync_worker._archive_due(settings)
    sheet_sync_worker._direct_state["archived_on"] = at["now"].date()
    assert not sheet_sync_worker._archive_due(settings)  # already ran today
    at["now"] = at["now"].replace(hour=4)
    sheet_sync_worker._direct_state["archived_on"] = None
    assert not sheet_sync_worker._archive_due(settings)  # outside the hour
