"""Dashboard block/unblock against a real Postgres schema.

Needs TEST_DATABASE_URL (a disposable database with the migrations applied,
never production). Each test builds its own practice, cadence and synthetic
555 leads, and removes them afterwards.
"""

import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

import rpt_agent.routes.dashboard as dashboard_routes
from rpt_agent.api import app
from rpt_agent.config import get_settings
from rpt_agent.services.sheet_sync import build_sheet_snapshot

pytestmark = pytest.mark.integration

# Two steps on day 0, then days 3 and 5: enough to see gaps survive a resume.
STEPS = [(0, "call"), (0, "sms"), (3, "call"), (5, "sms")]


@pytest.fixture
def db(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()

    @contextmanager
    def real_transaction():
        with psycopg.connect(url, row_factory=dict_row) as conn, conn.transaction():
            yield conn

    monkeypatch.setattr(dashboard_routes, "transaction", real_transaction)
    conn = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    practice = conn.execute(
        "insert into practices(name,slug) values('Synthetic Practice',%s) returning id",
        (f"test-{uuid4().hex}",),
    ).fetchone()["id"]
    conn.execute("insert into practice_settings(practice_id) values(%s)", (practice,))
    version = conn.execute(
        "insert into cadence_versions(practice_id,version_number,name,status,activated_at) "
        "values(%s,1,'Synthetic v1','active',now()) returning id",
        (practice,),
    ).fetchone()["id"]
    for order, (day, channel) in enumerate(STEPS):
        conn.execute(
            "insert into cadence_steps(practice_id,cadence_version_id,step_order,day_offset,channel,"
            "key,description,is_active) values(%s,%s,%s,%s,%s,%s,%s,true)",
            (practice, version, order, day, channel, f"s{order}-{uuid4().hex[:6]}", f"Day {day} {channel}"),
        )
    yield {"conn": conn, "practice": practice, "version": version}
    leads = conn.execute(
        "select id::text as id,phone_e164 from leads where practice_id=%s", (practice,)).fetchall()
    conn.execute("delete from suppressed_numbers where phone_e164=any(%s)",
                 ([r["phone_e164"] for r in leads],))
    conn.execute("delete from integration_outbox where aggregate_id=any(%s)", ([r["id"] for r in leads],))
    conn.execute("delete from dashboard_audit_log where practice_id=%s", (practice,))
    conn.execute("delete from leads where practice_id=%s", (practice,))
    conn.execute("delete from practices where id=%s", (practice,))
    conn.close()
    get_settings.cache_clear()


def _phone():
    return f"+1555{uuid4().int % 10_000_000:07d}"


def _lead(db, phone, *, status="in_progress", cadence="active", source="google_sheets"):
    return str(db["conn"].execute(
        "insert into leads(practice_id,full_name,first_name,last_name,phone_e164,status,"
        "cadence_state,source_system,status_changed_at) "
        "values(%s,'Synthetic Patient','Synthetic','Patient',%s,%s,%s,%s,now()) returning id",
        (db["practice"], phone, status, cadence, source),
    ).fetchone()["id"])


def _schedule(db, lead_id, start, statuses):
    """One event per step, `start` plus the step's day, with the given statuses."""
    steps = db["conn"].execute(
        "select id,day_offset,channel from cadence_steps where cadence_version_id=%s "
        "order by step_order", (db["version"],)).fetchall()
    for step, status in zip(steps, statuses, strict=True):
        db["conn"].execute(
            "insert into outreach_events(lead_id,cadence_step_id,cadence_version_id,channel,"
            "day_offset,scheduled_for,status) values(%s,%s,%s,%s,%s,%s,%s)",
            (lead_id, step["id"], db["version"], step["channel"], step["day_offset"],
             start + timedelta(days=step["day_offset"]), status),
        )


def _sheet_block(db, lead_id, phone, at):
    """What the Sheet's Do Not Contact leaves behind, back-dated to `at`."""
    c = db["conn"]
    c.execute(
        "update leads set status='do_not_contact',cadence_state='terminated',call_opt_out=true,"
        "sms_opt_out=true,status_changed_at=%s where id=%s", (at, lead_id))
    c.execute(
        "update outreach_events set status='skipped',failure_reason='Do Not Contact selected in "
        "Google Sheets',updated_at=%s where lead_id=%s and status='planned'", (at, lead_id))
    c.execute(
        "insert into suppressed_numbers(phone_e164,reason,source,list_type,created_at) "
        "values(%s,'staff selected Do Not Contact','n8n_sheet','internal',%s)", (phone, at))


def _post(lead_id, body, role="super_admin"):
    return TestClient(app).post(
        f"/api/v1/dashboard/leads/{lead_id}/contact-rules", json=body,
        headers={"X-Dashboard-Token": "x" * 32, "X-Dashboard-User-ID": "staff-1",
                 "X-Dashboard-User-Name": "Test%20Admin", "X-Dashboard-User-Role": role})


def _events(db, lead_id):
    return db["conn"].execute(
        "select status,day_offset,channel,scheduled_for from outreach_events where lead_id=%s "
        "order by scheduled_for,id", (lead_id,)).fetchall()


def _snapshot(db, lead_id):
    reasons = [r["payload"]["reason"] for r in db["conn"].execute(
        "select payload from integration_outbox where aggregate_id=%s "
        "and event_type<>'sheet.stage_changed' order by id", (lead_id,))]  # trigger rows: migration 045
    return reasons, build_sheet_snapshot(db["conn"], lead_id, reasons=reasons)["sheet"]


def test_dashboard_block_matches_the_sheet_command(db):
    phone = _phone()
    lead = _lead(db, phone)
    _schedule(db, lead, datetime.now(UTC) + timedelta(hours=1), ["planned"] * 4)

    response = _post(lead, {"do_not_contact": True}, role="employee")

    assert response.status_code == 200, response.text
    block = db["conn"].execute(
        "select source,reason from suppressed_numbers where phone_e164=%s", (phone,)).fetchone()
    assert block["source"] == "dashboard" and "Test Admin" in block["reason"]
    assert {e["status"] for e in _events(db, lead)} == {"skipped"}
    reasons, sheet = _snapshot(db, lead)
    assert reasons == ["dashboard_do_not_contact"]
    assert sheet["action_status"] == "Do Not Contact applied"


def test_continue_resumes_only_the_stopped_steps_with_their_spacing(db):
    phone = _phone()
    lead = _lead(db, phone)
    started = datetime.now(UTC) - timedelta(days=4)
    # Day 0 call and text went out; day 3 and day 5 were still to come.
    _schedule(db, lead, started, ["delivered", "delivered", "planned", "planned"])
    _sheet_block(db, lead, phone, at=started + timedelta(days=1))

    response = _post(lead, {"do_not_contact": False, "after_unblock": "continue"})

    assert response.status_code == 200, response.text
    assert db["conn"].execute(
        "select 1 from suppressed_numbers where phone_e164=%s", (phone,)).fetchone() is None
    row = db["conn"].execute(
        "select status,cadence_state,call_opt_out,sms_opt_out from leads where id=%s", (lead,)).fetchone()
    assert (row["status"], row["cadence_state"], row["call_opt_out"], row["sms_opt_out"]) == (
        "in_progress", "active", False, False)
    events = _events(db, lead)
    assert [e["status"] for e in events] == ["delivered", "delivered", "planned", "planned"]
    day3, day5 = events[2]["scheduled_for"], events[3]["scheduled_for"]
    # Day 3 comes next (no burst of overdue steps), day 5 still two days after it.
    assert abs((day3 - datetime.now(UTC)).total_seconds()) < 120
    assert day5 - day3 == timedelta(days=2)
    _, sheet = _snapshot(db, lead)
    assert sheet["action_status"] == "Cadence resumed in dashboard"
    assert sheet["lead_status"] == "In cadence"
    # The stale "Do Not Contact" in the Action cell is replaced. Next to a Lead
    # ID, "Start cadence" is never re-sent by the intake workflow.
    assert sheet["action"] == "Start cadence"


def test_start_over_builds_a_fresh_day_zero_and_keeps_history(db):
    phone = _phone()
    lead = _lead(db, phone)
    started = datetime.now(UTC) - timedelta(days=20)
    _schedule(db, lead, started, ["delivered", "delivered", "planned", "planned"])
    _sheet_block(db, lead, phone, at=started + timedelta(days=1))

    response = _post(lead, {"do_not_contact": False, "after_unblock": "restart"})

    assert response.status_code == 200, response.text
    events = _events(db, lead)
    assert [e["status"] for e in events] == ["delivered", "delivered"] + ["planned"] * 4
    fresh = [e for e in events if e["status"] == "planned"]
    # Weekend roll-over can land day 3 and day 5 on the same Monday, so only
    # the set of steps is fixed here, not their order.
    assert sorted(e["day_offset"] for e in fresh) == [0, 0, 3, 5]
    _, sheet = _snapshot(db, lead)
    # The Sheet's own restart wording: with Action "Restart cadence" the intake
    # workflow skips it, so the patient is not restarted a second time.
    assert sheet["action_status"] == "Cadence restarted"
    assert sheet["action"] == "Restart cadence"


def test_unblock_must_choose_continue_or_start_over(db):
    """There is no "unblock and wait": a lead left on hold showed a Resume
    button with nothing behind it, because the block had cancelled its steps."""
    phone = _phone()
    lead = _lead(db, phone)
    _schedule(db, lead, datetime.now(UTC) - timedelta(days=4), ["delivered", "delivered", "planned", "planned"])
    _sheet_block(db, lead, phone, at=datetime.now(UTC) - timedelta(days=3))

    for body in ({"do_not_contact": False}, {"do_not_contact": False, "after_unblock": "none"}):
        assert _post(lead, body).status_code == 422
    # Refused before anything changed: still blocked, still Do Not Contact.
    assert db["conn"].execute(
        "select 1 from suppressed_numbers where phone_e164=%s", (phone,)).fetchone()
    assert db["conn"].execute(
        "select status from leads where id=%s", (lead,)).fetchone()["status"] == "do_not_contact"


def test_unblocking_never_releases_a_burst_from_another_lead_on_the_number(db):
    """Emma's case: blocked on one row, then started again on new rows. The new
    lead's steps went overdue behind the block; unblocking must not fire them."""
    phone = _phone()
    old = _lead(db, phone)
    _schedule(db, old, datetime.now(UTC) - timedelta(days=2), ["delivered", "delivered", "planned", "planned"])
    _sheet_block(db, old, phone, at=datetime.now(UTC) - timedelta(days=1))
    newer = _lead(db, phone)
    _schedule(db, newer, datetime.now(UTC) - timedelta(hours=20), ["planned"] * 4)

    response = _post(old, {"do_not_contact": False, "after_unblock": "continue"})

    assert response.status_code == 200, response.text
    assert not any(e["status"] == "planned" for e in _events(db, newer))
    assert db["conn"].execute(
        "select cadence_state from leads where id=%s", (newer,)).fetchone()["cadence_state"] == "terminated"
    due = db["conn"].execute(
        "select count(*) n from outreach_events oe join leads l on l.id=oe.lead_id "
        "where l.phone_e164=%s and oe.status='planned' and oe.scheduled_for<=now()+interval '2 minutes'",
        (phone,)).fetchone()["n"]
    assert due == 1  # only the resumed lead's next step


def test_older_do_not_contact_rows_are_marked_unblocked_too(db):
    phone = _phone()
    old = _lead(db, phone)
    _sheet_block(db, old, phone, at=datetime.now(UTC) - timedelta(days=1))
    current = _lead(db, phone, status="new", cadence="pending")

    response = _post(current, {"do_not_contact": False, "after_unblock": "restart"})

    assert response.status_code == 200, response.text
    reasons, sheet = _snapshot(db, old)
    assert "number_unblocked" in reasons
    # The old lead keeps its own Do Not Contact status, so its Sheet row keeps
    # saying so; the intake workflow skips it until staff change the Action.
    assert sheet["action_status"] == "Do Not Contact applied"
    assert "action" not in sheet


def test_continue_with_nothing_left_changes_nothing(db):
    phone = _phone()
    lead = _lead(db, phone)
    _schedule(db, lead, datetime.now(UTC) - timedelta(days=6), ["delivered"] * 4)
    _sheet_block(db, lead, phone, at=datetime.now(UTC) - timedelta(days=1))

    response = _post(lead, {"do_not_contact": False, "after_unblock": "continue"})

    assert response.status_code == 409
    assert db["conn"].execute(
        "select 1 from suppressed_numbers where phone_e164=%s", (phone,)).fetchone()


def _admin(method, path, body=None, role="super_admin"):
    client = TestClient(app)
    headers = {"X-Dashboard-Token": "x" * 32, "X-Dashboard-User-ID": "staff-1",
               "X-Dashboard-User-Name": "Test%20Admin", "X-Dashboard-User-Role": role}
    return client.request(method, f"/api/v1/dashboard/{path}", json=body, headers=headers)


def test_a_number_whose_lead_was_deleted_can_be_unblocked_from_administration(db):
    # 2026-10-16: a patient said "don't call me", staff deleted the lead, and the number stayed
    # blocked with no lead page to unblock it from.
    phone = _phone()
    db["conn"].execute("insert into suppressed_numbers(phone_e164,reason,source,list_type) "
                       "values(%s,'explicit do-not-contact request','vapi','internal')", (phone,))
    try:
        listed = _admin("GET", "blocked-numbers")
        assert listed.status_code == 200, listed.text
        entry = next(n for n in listed.json()["numbers"] if n["phone"] == phone)
        assert entry["leads"] == [] and entry["open_lead_id"] is None
        assert _admin("POST", "blocked-numbers/unblock", {"phone": phone}, role="employee").status_code == 403
        done = _admin("POST", "blocked-numbers/unblock", {"phone": phone})
        assert done.status_code == 200, done.text
        assert not db["conn"].execute("select 1 from suppressed_numbers where phone_e164=%s",
                                      (phone,)).fetchone()
        assert _admin("POST", "blocked-numbers/unblock", {"phone": phone}).status_code == 404
    finally:
        db["conn"].execute("delete from suppressed_numbers where phone_e164=%s", (phone,))
        db["conn"].execute("delete from dashboard_audit_log where action='number.unblocked'")


def test_an_open_lead_must_be_unblocked_from_its_own_page(db):
    phone = _phone()
    lead = _lead(db, phone, status="needs_attention", cadence="paused")
    db["conn"].execute("insert into suppressed_numbers(phone_e164,reason,source,list_type) "
                       "values(%s,'explicit do-not-contact request','vapi','internal')", (phone,))
    entry = next(n for n in _admin("GET", "blocked-numbers").json()["numbers"] if n["phone"] == phone)
    assert entry["open_lead_id"] == lead
    refused = _admin("POST", "blocked-numbers/unblock", {"phone": phone})
    assert refused.status_code == 409 and "lead's page" in refused.json()["detail"]


def test_unblocking_a_closed_do_not_contact_number_tells_the_sheet(db):
    phone = _phone()
    lead = _lead(db, phone)
    _sheet_block(db, lead, phone, datetime.now(UTC) - timedelta(days=1))
    assert _admin("POST", "blocked-numbers/unblock", {"phone": phone}).status_code == 200
    row = db["conn"].execute("select status,sms_opt_out,call_opt_out from leads where id=%s", (lead,)).fetchone()
    assert (row["status"], row["sms_opt_out"], row["call_opt_out"]) == ("do_not_contact", False, False)
    reasons, _ = _snapshot(db, lead)
    assert "number_unblocked" in reasons
    db["conn"].execute("delete from dashboard_audit_log where action='number.unblocked'")
