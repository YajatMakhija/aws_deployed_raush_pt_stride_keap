"""The worker's claim honours calling hours and holidays, on a real Postgres schema.

Needs TEST_DATABASE_URL (a disposable database with the migrations applied, never production).
2026-10-19: the clinic set 8 AM-7 PM weekdays, 8 AM-1:30 PM Saturday and eight holidays. Planning
skipped holidays, but the claim did not, so a held-back step could still go out on Christmas.
"""

import json
import os
from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.rows import dict_row

from rpt_agent.worker import CLAIM_SQL

pytestmark = pytest.mark.integration
CLINIC = ZoneInfo("America/Los_Angeles")
HOURS = {**{str(d): {"open": "08:00", "close": "19:00"} for d in range(1, 6)},
         "6": {"open": "08:00", "close": "13:30"}, "7": None}
SIM = CLAIM_SQL.replace("now()", "%(sim)s::timestamptz")


@pytest.fixture
def db():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    conn = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    practice = conn.execute(
        "insert into practices(name,slug,timezone) values('Synthetic',%s,'America/Los_Angeles') returning id",
        (f"test-hours-{uuid4().hex[:10]}",)).fetchone()["id"]
    conn.execute("insert into practice_settings(practice_id,business_hours) values(%s,%s)",
                 (practice, json.dumps(HOURS)))

    class Db:
        def due_lead(self):
            lead = conn.execute(
                "insert into leads(practice_id,source_system,full_name,first_name,last_name,phone_e164,"
                "status,cadence_state,is_test) values(%s,'google_sheets','T P','T','P',%s,'in_progress',"
                "'active',false) returning id",
                (practice, f"+1555{uuid4().int % 10_000_000:07d}")).fetchone()["id"]
            conn.execute("insert into outreach_events(lead_id,attempt_no,channel,status,scheduled_for) "
                         "values(%s,1,'sms','planned','2026-01-01')", (lead,))
            return lead

        def holidays(self, value):
            conn.execute("update practice_settings set holidays=%s where practice_id=%s",
                         (json.dumps(value), practice))

        def claims(self, lead, local):
            with conn.transaction():
                conn.execute("select set_config('rpt.worker_claim_protocol','v1',true)")
                rows = conn.execute(SIM, {"sim": local.astimezone(UTC), "limit": 50,
                                          "test_mode": False}).fetchall()
                claimed = any(r["lead_id"] == lead for r in rows)
                conn.execute("update outreach_events set status='planned' where lead_id in "
                             "(select id from leads where practice_id=%s)", (practice,))
            return claimed

    yield Db()
    leads = [r["id"] for r in conn.execute("select id from leads where practice_id=%s", (practice,))]
    conn.execute("delete from outreach_events where lead_id=any(%s)", (leads,))
    conn.execute("delete from integration_outbox where aggregate_id=any(%s)", ([str(i) for i in leads],))
    conn.execute("delete from leads where practice_id=%s", (practice,))
    conn.execute("delete from practice_settings where practice_id=%s", (practice,))
    conn.execute("delete from practices where id=%s", (practice,))
    conn.close()


def _pt(month, day, hour, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=CLINIC)


@pytest.mark.parametrize(("local", "allowed"), [
    (_pt(10, 19, 7, 59), False),   # Monday before 8
    (_pt(10, 19, 8, 0), True),     # Monday 8:00
    (_pt(10, 19, 18, 59), True),   # Monday just before 7 PM
    (_pt(10, 19, 19, 0), False),   # Monday 7 PM
    (_pt(10, 24, 8, 0), True),     # Saturday 8:00
    (_pt(10, 24, 13, 29), True),   # Saturday before 1:30
    (_pt(10, 24, 13, 30), False),  # Saturday 1:30
    (_pt(10, 25, 11, 0), False),   # Sunday
])
def test_claim_follows_the_clinic_hours(db, local, allowed):
    assert db.claims(db.due_lead(), local) is allowed


def test_claim_skips_a_holiday_and_the_rest_of_a_short_day(db):
    lead = db.due_lead()
    db.holidays(["2026-11-26", {"date": "2026-12-24", "close": "12:00"}])
    assert db.claims(lead, _pt(11, 26, 10)) is False   # Thanksgiving
    assert db.claims(lead, _pt(11, 27, 10)) is True    # the day after
    assert db.claims(lead, _pt(12, 24, 11)) is True    # short day, before its close
    assert db.claims(lead, _pt(12, 24, 13)) is False   # short day, after its close
    db.holidays([])
    assert db.claims(lead, _pt(11, 26, 10)) is True
