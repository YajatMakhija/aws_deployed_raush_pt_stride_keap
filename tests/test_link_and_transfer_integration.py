""""Text me the booking link AND connect me", end to end on a real Postgres schema.

Needs TEST_DATABASE_URL (a disposable database with the migrations applied, never production).
2026-10-16: a patient who asked for both was only transferred and never got the link.
"""

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from rpt_agent.config import get_settings
from rpt_agent.db import get_pool
from rpt_agent.observability import WorkflowTrace
from rpt_agent.services import delivery
from rpt_agent.services.lead_status import report_lead_status

pytestmark = pytest.mark.integration
LINK = "https://book.example.test/rausch"


@pytest.fixture
def env(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    monkeypatch.setenv("SUPABASE_DB_URL", url)
    get_settings.cache_clear()
    get_pool.cache_clear()
    conn = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    practice = conn.execute(
        "insert into practices(name,slug,timezone) values('Synthetic',%s,'America/Los_Angeles') returning id",
        (f"test-link-{uuid4().hex[:10]}",)).fetchone()["id"]
    conn.execute("insert into practice_settings(practice_id,booking_link_url) values(%s,%s)", (practice, LINK))

    class Env:
        db = conn

        def call(self):
            """A lead on a live call: returns (lead_id, event_id, call_id)."""
            phone = f"+1555{uuid4().int % 10_000_000:07d}"
            lead = conn.execute(
                "insert into leads(practice_id,source_system,full_name,first_name,last_name,phone_e164,"
                "status,cadence_state,is_test) values(%s,'google_sheets','Test Patient','Test','Patient',%s,"
                "'in_progress','active',true) returning id", (practice, phone)).fetchone()["id"]
            call_id = f"call-{uuid4().hex[:12]}"
            event = conn.execute(
                "insert into outreach_events(lead_id,attempt_no,channel,status,scheduled_for,vapi_call_id) "
                "values(%s,1,'call','in_flight',now(),%s) returning id", (lead, call_id)).fetchone()["id"]
            return str(lead), event, call_id

        def report(self, lead, event, call_id, status):
            return report_lead_status(
                WorkflowTrace("test", "test"), lead_id=lead, status=status, call_id=call_id,
                tool_call_id=f"tool-{uuid4().hex[:8]}", event_id=event, notes="test",
                callback_type="", delay_minutes="", callback_datetime_iso="")

        def link_text(self, lead):
            return conn.execute(
                "select status,payload from notification_log where lead_id=%s "
                "and notification_type='sms_booking_link'", (lead,)).fetchone()

        def lead(self, lead):
            return conn.execute("select status,cadence_state from leads where id=%s", (lead,)).fetchone()

        def run_sender(self):
            """The worker's delivery pass, two minutes later, with a fake Twilio."""
            conn.execute("update notification_log set next_attempt_at=now() where status='queued' "
                         "and lead_id in (select id from leads where practice_id=%s)", (practice,))
            sent = []

            class FakeProviders:
                settings = get_settings()

                def send_sms(self, trace, to, body):
                    sent.append((to, body))
                    return f"SM{uuid4().hex[:30]}"

            delivery.process_pending_integrations(WorkflowTrace("test", "test"), FakeProviders())
            # The sender works through every due text in the database; in the demo schema other
            # suites (booking confirmations) leave some behind, so count only this practice's.
            ours = {r["phone_e164"] for r in conn.execute(
                "select phone_e164 from leads where practice_id=%s", (practice,))}
            return [(to, body) for to, body in sent if to in ours]

    yield Env()

    leads = [r["id"] for r in conn.execute("select id from leads where practice_id=%s", (practice,))]
    for table in ("notification_log", "sms_messages", "lead_status_history", "outreach_events"):
        conn.execute(f"delete from {table} where lead_id=any(%s)", (leads,))
    conn.execute("delete from integration_outbox where aggregate_id=any(%s)", ([str(i) for i in leads],))
    conn.execute("delete from provider_events where payload->>'lead_id'=any(%s)", ([str(i) for i in leads],))
    conn.execute("delete from leads where practice_id=%s", (practice,))
    conn.execute("delete from practice_settings where practice_id=%s", (practice,))
    conn.execute("delete from practices where id=%s", (practice,))
    conn.close()
    get_pool.cache_clear()
    get_settings.cache_clear()


def test_link_and_transfer_sends_the_link_and_records_the_transfer(env):
    lead, event, call = env.call()
    env.report(lead, event, call, "booking_link_and_transfer")
    assert env.lead(lead) == {"status": "transferred_human", "cadence_state": "active"}
    text = env.link_text(lead)
    assert text["status"] == "queued" and text["payload"]["with_transfer"] is True
    sent = env.run_sender()
    assert len(sent) == 1 and LINK in sent[0][1]
    assert env.link_text(lead)["status"] == "sent"


def test_link_then_a_plain_transfer_is_a_change_of_mind(env):
    lead, event, call = env.call()
    env.report(lead, event, call, "booking_link")
    env.report(lead, event, call, "transferred_human")
    assert env.run_sender() == []
    assert env.link_text(lead)["status"] == "skipped"


def test_both_then_just_transfer_drops_the_link(env):
    lead, event, call = env.call()
    env.report(lead, event, call, "booking_link_and_transfer")
    env.report(lead, event, call, "transferred_human")
    assert env.run_sender() == []
    assert env.link_text(lead)["status"] == "skipped"


def test_both_then_declined_drops_the_link_and_closes(env):
    lead, event, call = env.call()
    env.report(lead, event, call, "booking_link_and_transfer")
    env.report(lead, event, call, "declined")
    assert env.run_sender() == []
    assert env.link_text(lead)["status"] == "skipped"
    assert env.lead(lead)["status"] == "declined"


def test_link_only_still_sends(env):
    lead, event, call = env.call()
    env.report(lead, event, call, "booking_link")
    assert env.lead(lead)["status"] == "booking_link_sent"
    assert len(env.run_sender()) == 1


def test_link_then_both_keeps_one_text(env):
    lead, event, call = env.call()
    env.report(lead, event, call, "booking_link")
    env.report(lead, event, call, "booking_link_and_transfer")
    assert env.db.execute("select count(*) as n from notification_log where lead_id=%s",
                          (lead,)).fetchone()["n"] == 1
    assert len(env.run_sender()) == 1
