"""Keap phase 2B against a real Postgres schema and the in-memory fake Keap.

Needs TEST_DATABASE_URL (never production). The real Keap is never called.
"""

import os
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg
import pytest
from fake_keap import FakeKeap
from psycopg.rows import dict_row

import rpt_agent.keap_sync_worker as worker
from rpt_agent.config import get_settings
from rpt_agent.db import get_pool, transaction
from rpt_agent.observability import WorkflowTrace
from rpt_agent.services import keap_desired, keap_oauth
from rpt_agent.services.keap_client import KeapClient, KeapRateLimited, RateLimiter
from rpt_agent.services.keap_oauth import KeapAuthError
from rpt_agent.services.keap_sync import sync_row
from rpt_agent.services.keap_sync_status import keap_sync_status

pytestmark = pytest.mark.integration
PENDING = "144"


@pytest.fixture
def env(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    slug = f"test-keap2b-{uuid4().hex[:10]}"
    for key, value in {
        "SUPABASE_DB_URL": url, "KEAP_INCLUDE_TEST_LEADS": "true", "STRIDE_PRACTICE_SLUG": slug,
        "KEAP_SYNC_ENABLED": "true", "KEAP_CLIENT_ID": "cid", "KEAP_CLIENT_SECRET": "csecret",
        "HTTP_RETRY_BASE_SECONDS": "0.1",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("rpt_agent.services.provider_http.time.sleep", lambda *_: None)
    get_settings.cache_clear()
    get_pool.cache_clear()
    worker._state.update(last_full=None, client=None, stats_day=None)
    conn = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    practice = conn.execute(
        "insert into practices(name,slug,timezone) values('Synthetic',%s,'America/New_York') "
        "returning id", (slug,)).fetchone()["id"]
    conn.execute(
        "insert into practice_settings(practice_id,stride_location_timezone,keap_pending_tag_id,"
        "keap_gave_up_tag_id,keap_sync_start_at,keap_include_existing_patients) "
        "values(%s,'America/New_York',%s,'186',now()-interval '1 day',true)", (practice, PENDING))
    for type_id, booked, done in ((2, "180", "172"), (1, "184", "176")):
        conn.execute(
            "insert into keap_appointment_types(practice_id,stride_appointment_type_id,label,"
            "booked_tag_id,completed_tag_id,date_field_id,time_field_id,clinician_field_id) "
            "values(%s,%s,'t',%s,%s,'6','8','10')", (practice, type_id, booked, done))
    fake = FakeKeap()
    conn.execute(
        "insert into keap_connections(practice_id,keap_account,access_token,refresh_token,"
        "access_token_expires_at) values(%s,'fake',%s,%s,now()+interval '20 hours')",
        (practice, fake.access, fake.refresh))

    class Env:
        db = conn
        practice_id = practice
        keap = fake
        trace = WorkflowTrace("keap_test", "test")
        _pid = 5000

        def client(self, **kw):
            return KeapClient(practice, get_settings(), fake.client(), audit_enabled=False, **kw)

        def lead(self, name="Asha Rao", email="asha@example.test", phone="+15555550101",
                 status="in_progress"):
            first, _, last = name.partition(" ")
            return conn.execute(
                "insert into leads(practice_id,source_system,full_name,first_name,last_name,email,"
                "phone_e164,status,cadence_state,is_test) values(%s,'google_sheets',%s,%s,%s,%s,%s,"
                "%s,'active',true) returning id",
                (practice, name, first, last, email, phone, status)).fetchone()["id"]

        def book(self, lead_id, type_id=2, days=2, status="Unmarked", clinician="Jane Clinician"):
            Env._pid += 1
            pid = Env._pid
            conn.execute(
                "insert into stride_patients(practice_id,stride_id,first_name,last_name,lead_id,"
                "preferred_email) select %s,%s,first_name,last_name,%s,email from leads where id=%s",
                (practice, pid, lead_id, lead_id))
            conn.execute("update leads set stride_patient_id=%s where id=%s", (pid, lead_id))
            self.appt(pid, type_id, days, status, clinician)
            return pid

        def appt(self, pid, type_id=2, days=2, status="Unmarked", clinician="Jane Clinician"):
            Env._pid += 1
            start = datetime.now(UTC) + timedelta(days=days)
            conn.execute(
                "insert into stride_appointments(practice_id,stride_id,patient_stride_id,"
                "appointment_type_id,appointment_type,status,start_utc,starts_at_local,clinician_id,"
                "clinician_name) values(%s,%s,%s,%s,'x',%s,%s,%s,7,%s)",
                (practice, Env._pid, pid, type_id, status, start,
                 (start - timedelta(hours=4)).replace(tzinfo=None, microsecond=0), clinician))
            return Env._pid

        def plan(self):
            with transaction() as c:
                return keap_desired.plan_changes(c, practice)

        def row(self, lead_id):
            return conn.execute("select * from keap_sync where lead_id=%s", (lead_id,)).fetchone()

        def sync(self, lead_id, client=None, plan=True):
            """Plan, claim like the worker does, and sync."""
            if plan:
                self.plan()
            claimed = conn.execute(
                "update keap_sync set status='syncing',attempts=attempts+1 where lead_id=%s "
                "returning *", (lead_id,)).fetchone()
            return sync_row(client or self.client(), claimed, self.trace, get_settings())

        def contact(self, lead_id):
            return fake.contacts[self.row(lead_id)["keap_contact_id"]]

    yield Env()

    conn.execute("delete from keap_sync where practice_id=%s", (practice,))
    conn.execute("delete from keap_connections where practice_id=%s", (practice,))
    conn.execute("delete from keap_appointment_types where practice_id=%s", (practice,))
    for table in ("stride_appointments", "stride_patients"):
        conn.execute(f"delete from {table} where practice_id=%s", (practice,))
    conn.execute("delete from leads where practice_id=%s", (practice,))
    conn.execute("delete from practice_settings where practice_id=%s", (practice,))
    conn.execute("delete from service_heartbeats where service='keap-worker'")
    conn.execute("delete from practices where id=%s", (practice,))
    conn.close()
    get_pool.cache_clear()
    get_settings.cache_clear()


# --- contacts -----------------------------------------------------------------


def test_new_lead_is_created_tagged_pending_and_marked_synced(env):
    lead = env.lead()
    assert env.sync(lead) == "synced"
    row, contact = env.row(lead), env.contact(lead)
    assert env.keap.counts["POST /contacts"] == 1
    assert row["status"] == "synced" and row["synced_hash"] == row["desired_hash"]
    assert contact["tag_ids"] == [PENDING] and contact["contact_type"] == "Lead"
    assert contact["email_addresses"] == [{"email": "asha@example.test", "field": "EMAIL1"}]


def test_syncing_the_same_row_twice_makes_no_writes_the_second_time(env):
    lead = env.lead()
    env.sync(lead)
    writes = len(env.keap.writes())
    assert env.sync(lead) == "synced"
    assert len(env.keap.writes()) == writes


def test_existing_contact_is_reused_and_clinic_data_survives(env):
    cid = env.keap.add_contact("Asha", "Rao", email="asha@example.test", tags=["999"],
                               custom=[("4", "clinic note")])
    lead = env.lead()
    env.sync(lead)
    assert env.keap.counts["POST /contacts"] == 0
    assert env.row(lead)["keap_contact_id"] == cid
    contact = env.keap.contacts[cid]
    assert {"4": "clinic note"}.items() <= {f["id"]: f["content"] for f in contact["custom_fields"]}.items()
    assert set(contact["tag_ids"]) == {"999", PENDING}
    assert contact["contact_type"] == "Customer"  # the clinic's own classification is not ours


def test_same_email_different_first_name_is_a_new_contact(env):
    env.keap.add_contact("Priya", "Rao", email="asha@example.test")
    lead = env.lead()
    env.sync(lead)
    assert env.keap.counts["POST /contacts"] == 1 and len(env.keap.contacts) == 2


def test_a_create_never_asks_keap_to_merge_duplicates(env):
    # The sandbox showed duplicate_option=EmailAndName updating a family member who shares
    # the email and family name; we search by email + full name first, so we never ask.
    env.keap.add_contact("Priya", "Rao", email="asha@example.test")
    env.sync(env.lead())
    assert env.keap.create_queries == [{}]
    assert env.keap.contacts["300"]["given_name"] == "Priya"  # the family member is untouched


def test_phone_and_name_match_when_the_email_search_is_empty(env):
    cid = env.keap.add_contact("Asha", "Rao", phone="+15555550101")
    lead = env.lead(email="other@example.test")
    env.sync(lead)
    assert env.row(lead)["keap_contact_id"] == cid and env.keap.counts["POST /contacts"] == 0


def test_two_matching_contacts_need_review_and_nothing_is_written(env):
    env.keap.add_contact("Asha", "Rao", email="asha@example.test")
    env.keap.add_contact("Asha", "Rao", email="asha@example.test")
    lead = env.lead()
    assert env.sync(lead) == "needs_review"
    assert env.row(lead)["status"] == "needs_review" and env.keap.writes() == []


def test_a_timed_out_create_is_found_by_search_next_time(env):
    lead = env.lead()
    env.keap.timeout_on_create = True
    assert env.sync(lead) == "retrying"
    assert len(env.keap.contacts) == 1  # created, response lost
    env.db.execute("update keap_sync set status='retrying' where lead_id=%s", (lead,))
    assert env.sync(lead) == "synced"
    assert len(env.keap.contacts) == 1 and env.keap.counts["POST /contacts"] == 1


def test_contact_id_is_saved_before_anything_else_can_fail(env, monkeypatch):
    lead = env.lead()
    client = env.client()
    monkeypatch.setattr(client, "get_contact", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("boom")), raising=True)
    env.sync(lead, client)
    assert env.row(lead)["keap_contact_id"] is not None
    assert env.row(lead)["status"] == "retrying"
    assert len(env.keap.contacts) == 1


def test_contact_deleted_in_keap_is_found_or_recreated(env):
    lead = env.lead()
    env.sync(lead)
    old = env.row(lead)["keap_contact_id"]
    del env.keap.contacts[old]  # deleted in Keap
    env.db.execute("update leads set status='closed_no_response' where id=%s", (lead,))
    env.sync(lead)
    assert env.row(lead)["keap_contact_id"] != old and env.row(lead)["status"] == "synced"


# --- tags and fields ----------------------------------------------------------


def test_booking_applies_the_new_tag_before_removing_pending(env):
    lead = env.lead()
    env.sync(lead)
    env.keap.requests.clear()
    env.book(lead)
    env.sync(lead)
    tag_calls = [path for _, path in env.keap.requests if "contacts:" in path]
    assert tag_calls == ["/tags/180/contacts:applyTags", f"/tags/{PENDING}/contacts:removeTags"]
    assert env.contact(lead)["tag_ids"] == ["180"]


def test_dates_times_and_clinician_are_written_and_foreign_fields_survive(env):
    cid = env.keap.add_contact("Asha", "Rao", email="asha@example.test", custom=[("4", "keep me")])
    lead = env.lead()
    env.book(lead)
    env.sync(lead)
    fields = {f["id"]: f["content"] for f in env.keap.contacts[cid]["custom_fields"]}
    assert fields["4"] == "keep me" and fields["10"] == "Jane Clinician"
    assert len(fields["6"]) == 10 and fields["8"].endswith(("AM", "PM"))


def test_cancelling_removes_booked_but_not_the_clinics_own_tags(env):
    lead = env.lead()
    pid = env.book(lead)
    env.sync(lead)
    env.contact(lead)["tag_ids"].append("999")
    env.db.execute("update stride_appointments set status='Cancel',updated_at=now() "
                   "where practice_id=%s and patient_stride_id=%s", (env.practice_id, pid))
    env.db.execute("update leads set status='needs_attention' where id=%s", (lead,))
    env.sync(lead)
    tags = set(env.contact(lead)["tag_ids"])
    assert "180" not in tags and PENDING in tags and "999" in tags


def test_clearing_a_date_clears_only_our_fields(env):
    lead = env.lead()
    pid = env.book(lead)
    env.sync(lead)
    cid = env.row(lead)["keap_contact_id"]
    env.keap.contacts[cid]["custom_fields"].append({"id": "4", "content": "clinic"})
    env.db.execute("update stride_appointments set deleted_at=now(),updated_at=now() "
                   "where practice_id=%s and patient_stride_id=%s", (env.practice_id, pid))
    env.sync(lead)
    fields = {f["id"]: f["content"] for f in env.keap.contacts[cid]["custom_fields"]}
    assert fields["4"] == "clinic"
    assert fields["6"] is None and fields["8"] is None and fields["10"] is None


def test_an_email_already_in_a_second_slot_is_left_alone(env):
    cid = env.keap.add_contact("Asha", "Rao", email="home@example.test")
    env.keap.contacts[cid]["email_addresses"].append({"email": "asha@example.test", "field": "EMAIL2"})
    env.keap.contacts[cid]["phone_numbers"] = [{"number": "+15555550101", "field": "PHONE1"}]
    lead = env.lead()
    env.sync(lead)
    patched = [m for m in env.keap.writes() if m[0] == "PATCH"]
    assert env.row(lead)["keap_contact_id"] == cid
    assert env.keap.contacts[cid]["email_addresses"][0]["email"] == "home@example.test"
    assert patched == []


def test_a_changed_phone_updates_slot_one_and_keeps_the_rest(env):
    cid = env.keap.add_contact("Asha", "Rao", email="asha@example.test", phone="+15555550100")
    env.keap.contacts[cid]["phone_numbers"].append({"number": "+15555550999", "field": "PHONE2"})
    lead = env.lead()
    env.sync(lead)
    phones = {p["field"]: p["number"] for p in env.keap.contacts[cid]["phone_numbers"]}
    assert phones == {"PHONE1": "+15555550101", "PHONE2": "+15555550999"}


def test_an_unsupported_field_type_fails_with_a_reason(env):
    env.keap.custom_fields["6"] = "CURRENCY"
    lead = env.lead()
    env.book(lead)
    assert env.sync(lead) == "failed"
    assert env.row(lead)["last_error"] == "unsupported field type CURRENCY for field 6"


def test_an_unknown_tag_fails_with_a_clear_reason(env):
    del env.keap.tags[PENDING]
    lead = env.lead()
    env.sync(lead)
    assert env.row(lead)["status"] == "failed"
    assert env.row(lead)["last_error"] == f"tag {PENDING} not found in Keap"


# --- failures and retries -----------------------------------------------------


def test_a_server_error_retries_then_succeeds(env):
    lead = env.lead()
    env.keap.fail_next = [500, 500, 500, 500]
    assert env.sync(lead) == "retrying"
    row = env.row(lead)
    assert row["status"] == "retrying" and row["next_attempt_at"] > datetime.now(UTC)
    env.keap.fail_next = []
    env.db.execute("update keap_sync set next_attempt_at=now() where lead_id=%s", (lead,))
    assert env.sync(lead, plan=False) == "synced"


def test_three_failures_end_as_failed(env):
    lead = env.lead()
    for _ in range(3):
        env.keap.fail_next = [500] * 4
        env.sync(lead)
        env.db.execute("update keap_sync set next_attempt_at=now() where lead_id=%s", (lead,))
    row = env.row(lead)
    assert row["status"] == "failed" and row["attempts"] == 3
    assert "Keap error" in row["last_error"]


def test_a_long_rate_limit_stops_the_tick_and_is_respected(env):
    lead = env.lead()
    env.keap.fail_next, env.keap.retry_after = [429], 600
    env.plan()
    claimed = env.db.execute("update keap_sync set status='syncing',attempts=attempts+1 "
                             "where lead_id=%s returning *", (lead,)).fetchone()
    with pytest.raises(KeapRateLimited):
        sync_row(env.client(), claimed, env.trace, get_settings())
    row = env.row(lead)
    assert row["status"] == "retrying" and row["attempts"] == 0
    assert row["next_attempt_at"] > datetime.now(UTC) + timedelta(seconds=500)


def test_a_rejected_token_is_refreshed_once_and_both_tokens_are_saved(env):
    lead = env.lead()
    env.keap.unauthorized_once = True
    assert env.sync(lead) == "synced"
    saved = env.db.execute("select access_token,refresh_token from keap_connections "
                           "where practice_id=%s", (env.practice_id,)).fetchone()
    assert saved == {"access_token": "access-1", "refresh_token": "refresh-1"}
    assert env.keap.token_issued == 1


def test_a_refresh_rejected_by_keap_asks_for_reconnection_and_keeps_the_row(env):
    lead = env.lead()
    env.keap.unauthorized_once = True
    env.keap.refresh_rejected = True
    with pytest.raises(KeapAuthError):
        env.sync(lead)
    row = env.row(lead)
    status = env.db.execute("select status from keap_connections where practice_id=%s",
                            (env.practice_id,)).fetchone()["status"]
    assert status == "reauthorization_required"
    assert row["status"] == "pending" and row["attempts"] == 0 and env.keap.contacts == {}


def test_still_unauthorized_after_a_refresh_needs_reconnection(env):
    lead = env.lead()
    env.keap.unauthorized_always = True
    with pytest.raises(KeapAuthError):
        env.sync(lead)
    assert env.db.execute("select status from keap_connections where practice_id=%s",
                          (env.practice_id,)).fetchone()["status"] == "reauthorization_required"


def test_two_callers_with_an_expiring_token_refresh_only_once(env):
    env.db.execute("update keap_connections set access_token_expires_at=now()+interval '1 hour' "
                   "where practice_id=%s", (env.practice_id,))
    tokens, errors = [], []

    def call():
        try:
            tokens.append(keap_oauth.access_token(env.practice_id, client=env.keap.client()))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and env.keap.token_issued == 1 and set(tokens) == {"access-1"}


def test_a_plan_changed_while_syncing_ends_pending_not_synced(env, monkeypatch):
    lead = env.lead()
    client = env.client()
    original = client.create_contact

    def create_then_change(*a, **k):
        result = original(*a, **k)
        env.db.execute("update keap_sync set desired_hash='changed',status='syncing' "
                       "where lead_id=%s", (lead,))
        return result

    monkeypatch.setattr(client, "create_contact", create_then_change)
    assert env.sync(lead, client) == "requeued"
    assert env.row(lead)["status"] == "pending" and env.row(lead)["synced_hash"] is None


# --- the worker ---------------------------------------------------------------


def test_worker_tick_syncs_resets_stuck_rows_and_writes_a_heartbeat(env):
    lead, stuck = env.lead(), env.lead("Sky Walker", "sky@example.test", "+15555550102")
    env.plan()
    env.db.execute("alter table keap_sync disable trigger trg_keap_sync_updated")
    env.db.execute("update keap_sync set status='syncing',updated_at=now()-interval '20 minutes' "
                   "where lead_id=%s", (stuck,))
    env.db.execute("alter table keap_sync enable trigger trg_keap_sync_updated")
    counts = worker.run_keap_sync_tick(env.client())
    assert counts["synced"] == 2
    assert env.row(lead)["status"] == env.row(stuck)["status"] == "synced"
    beat = env.db.execute("select details from service_heartbeats where service='keap-worker'"
                          ).fetchone()["details"]
    assert beat["state"] == "idle" and beat["queue"]["synced"] == 2
    assert beat["today"]["created"] == 2


def test_worker_idles_when_switched_off_or_not_connected(env, monkeypatch):
    env.lead()
    env.db.execute("update keap_connections set status='reauthorization_required' where practice_id=%s",
                   (env.practice_id,))
    assert worker.run_keap_sync_tick(env.client()) == {}
    beat = env.db.execute("select details from service_heartbeats where service='keap-worker'"
                          ).fetchone()["details"]
    assert beat["state"] == "needs_connection" and env.keap.requests == []
    monkeypatch.setenv("KEAP_SYNC_ENABLED", "false")
    get_settings.cache_clear()
    worker.run_keap_sync_tick(env.client())
    assert env.db.execute("select details from service_heartbeats where service='keap-worker'"
                          ).fetchone()["details"]["state"] == "switched_off"


def test_rate_limit_during_a_tick_releases_the_rest_without_using_attempts(env):
    leads = [env.lead(f"Person{i} Test", f"p{i}@example.test", f"+155555502{i:02d}") for i in range(3)]
    env.keap.fail_next, env.keap.retry_after = [429], 900
    worker.run_keap_sync_tick(env.client())
    rows = [env.row(x) for x in leads]
    assert all(r["attempts"] == 0 for r in rows)
    assert all(r["next_attempt_at"] > datetime.now(UTC) + timedelta(seconds=800) for r in rows)


def test_a_second_worker_cannot_start(env):
    url = os.environ["TEST_DATABASE_URL"]
    with (
        worker.keap_worker_lock(url, 5),
        pytest.raises(RuntimeError, match="already owns"),
        worker.keap_worker_lock(url, 5),
    ):
        pass
    with worker.keap_worker_lock(url, 5):  # released: a restart can take it again
        pass


# --- limiter, oauth, status ---------------------------------------------------


def test_rate_limiter_never_exceeds_the_per_minute_budget():
    now = [0.0]
    sent = []

    def sleep(seconds):
        now[0] += seconds

    limiter = RateLimiter(5, 1000, clock=lambda: now[0], sleep=sleep, today=lambda: "d")
    for _ in range(23):
        limiter.acquire()
        sent.append(now[0])
    for index, moment in enumerate(sent):
        assert len([t for t in sent if moment - 60 < t <= moment]) <= 5, index
    assert now[0] >= 180  # 23 requests at 5 a minute cannot finish sooner


def test_daily_budget_stops_with_a_long_wait():
    limiter = RateLimiter(100, 2, clock=lambda: 0.0, sleep=lambda _: None, today=lambda: "d")
    limiter.acquire(), limiter.acquire()
    with pytest.raises(KeapRateLimited):
        limiter.acquire()


def test_oauth_callback_rejects_bad_state_and_stores_tokens_without_echoing(env, monkeypatch):
    from fastapi.testclient import TestClient

    from rpt_agent.api import app

    monkeypatch.setattr(keap_oauth, "httpx", SimpleNamespace(
        Client=lambda **kw: env.keap.client(), HTTPError=httpx.HTTPError))
    http = TestClient(app)
    assert http.get("/api/v1/keap/oauth/callback?code=good-code&state=garbage").status_code == 400
    env.db.execute("delete from keap_connections where practice_id=%s", (env.practice_id,))
    state = keap_oauth.make_state(env.practice_id)
    response = http.get(f"/api/v1/keap/oauth/callback?code=good-code&state={state}")
    assert response.status_code == 200 and "connected" in response.text
    assert "access-" not in response.text and "good-code" not in response.text
    saved = env.db.execute("select access_token,keap_account,status from keap_connections "
                           "where practice_id=%s", (env.practice_id,)).fetchone()
    assert saved["access_token"].startswith("access-") and saved["status"] == "active"
    assert saved["keap_account"] == "fake.infusionsoft.com"
    assert http.get("/api/v1/keap/oauth/callback?code=bad&state=" + state).status_code == 400


def test_oauth_start_is_for_super_admins_only(env, monkeypatch):
    from fastapi.testclient import TestClient

    from rpt_agent.api import app

    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    http = TestClient(app)

    def headers(role):
        return {"X-Dashboard-Token": "x" * 32, "X-Dashboard-User-ID": "tester1",
                "X-Dashboard-User-Name": "Test", "X-Dashboard-User-Role": role}

    assert http.get("/api/v1/keap/oauth/start", headers=headers("employee")).status_code == 403
    ok = http.get("/api/v1/keap/oauth/start", headers=headers("super_admin"))
    assert ok.status_code == 200 and ok.json()["authorize_url"].startswith(
        "https://accounts.infusionsoft.com/app/oauth/authorize?client_id=cid")


def test_status_is_plain_phi_free_and_raises_the_right_alerts(env):
    lead = env.lead(email="secret.person@example.test")
    env.sync(lead)
    env.db.execute("update keap_sync set status='failed',last_error='contact create rejected: 400' "
                   "where lead_id=%s", (lead,))
    env.db.execute("update keap_connections set status='reauthorization_required' "
                   "where practice_id=%s", (env.practice_id,))
    env.db.execute(
        "insert into service_heartbeats(service,last_seen_at,details) values('keap-worker',"
        "now()-interval '20 minutes','{\"enabled\":true,\"requests_today\":39000,"
        "\"requests_budget\":40000}'::jsonb) on conflict(service) do update set "
        "last_seen_at=excluded.last_seen_at,details=excluded.details")
    with transaction() as conn:
        status = keap_sync_status(conn, env.practice_id)
    titles = " ".join(a["title"] for a in status["alerts"])
    assert status["overall"] == "problem"
    assert "stopped" in titles and "Reconnect" in titles and "could not be sent" in titles
    assert "daily request limit" in titles
    assert status["problems"][0]["reason"] == "contact create rejected: 400"
    assert "secret.person" not in str(status) and "Asha" not in str(status)


def test_status_when_everything_is_fine(env):
    lead = env.lead()
    worker.run_keap_sync_tick(env.client())
    with transaction() as conn:
        status = keap_sync_status(conn, env.practice_id)
    assert status["overall"] == "ok" and status["counts"]["synced"] == 1
    assert status["connection"]["connected"] and status["alerts"] == []
    assert env.row(lead)["status"] == "synced"


def test_logs_never_contain_personal_data_or_tokens(env, caplog):
    lead = env.lead(email="secret.person@example.test")
    with caplog.at_level("DEBUG"):
        env.sync(lead)
        worker.run_keap_sync_tick(env.client())
    text = caplog.text
    for secret in ("secret.person", "5555550101", "access-0", "refresh-0", "Asha"):
        assert secret not in text



def test_retried_requests_count_against_the_keap_budget():
    # The shared HTTP layer retries a failing call up to HTTP_RETRY_ATTEMPTS times; every one of
    # those is a real request Keap counts, so our limiter must count it too (PR #13 review).
    from rpt_agent.config import get_settings
    from rpt_agent.observability import WorkflowTrace

    calls = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(503 if len(calls) < 3 else 200, json={"contacts": []})

    limiter = RateLimiter(100, 1000, clock=lambda: 0.0, sleep=lambda _: None, today=lambda: "d")
    client = KeapClient(1, get_settings(), httpx.Client(transport=httpx.MockTransport(flaky)),
                        limiter=limiter, audit_enabled=False)
    KeapClient(1, get_settings(), client.client, limiter=limiter, audit_enabled=False)  # no double hook
    import rpt_agent.services.keap_client as module
    original = module.keap_oauth.access_token
    module.keap_oauth.access_token = lambda *a, **k: "token"
    try:
        client._call(WorkflowTrace("t", "test"), "GET", "/contacts", operation="list", idempotent=True)
    finally:
        module.keap_oauth.access_token = original
    assert len(calls) == 3
    assert limiter.used_today == 3


def test_one_person_that_cannot_be_linked_does_not_stop_planning_for_everyone(env, monkeypatch):
    # PR #13 review: linking ran outside the per-row savepoint, so one unique-index clash failed
    # the whole planning step on every tick. Now that person is flagged and the rest carry on.
    import psycopg

    first, second = env.lead("Clash One", email="one@example.test"), env.lead(
        "Fine Two", email="two@example.test", phone="+15555550102")
    env.plan()
    env.book(first), env.book(second)
    clash_row = env.row(first)["id"]
    original = keap_desired._link_pair

    def link(conn, pair, counts):
        if pair["lead_row"] == clash_row:
            raise psycopg.errors.UniqueViolation("simulated duplicate link")
        return original(conn, pair, counts)

    monkeypatch.setattr(keap_desired, "_link_pair", link)
    env.plan()  # must not raise
    flagged, fine = env.row(first), env.row(second)
    assert flagged["status"] == "needs_review" and "could not link" in flagged["last_error"]
    assert fine["stride_patient_id"] is not None


def test_an_email_that_would_change_keaps_search_filter_never_matches():
    from rpt_agent.config import get_settings
    from rpt_agent.observability import WorkflowTrace

    def no_http(request):
        raise AssertionError("must not call Keap")

    client = KeapClient(1, get_settings(), httpx.Client(transport=httpx.MockTransport(no_http)),
                        audit_enabled=False)
    for bad in ("a@b.test;given_name==X", "a@b.test,c@d.test", "a b@c.test", ""):
        assert client.find_by_email(WorkflowTrace("t", "test"), bad) == []
