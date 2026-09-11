"""The patient's last decision on a call is the one acted on.

Three pieces make a correction safe: a second callback replaces the first, a
booking link waits and is dropped if the decision changes, and the duplicate
check keys on the tool call rather than the outcome. Verified end to end against
the production database in a rolled-back transaction; these tests stop any of
the three being undone quietly.
"""
import inspect
from datetime import UTC, datetime

from rpt_agent.routes import leads as leads_route
from rpt_agent.services import delivery, lead_status


class Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class Recorder:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params))
        return Result({"earliest": None}) if sql.lstrip().startswith("select min") else Result()


def test_a_second_callback_replaces_the_first():
    """Ten, then "no, nine": without this the patient is called at both times."""
    conn = Recorder()
    lead_status._schedule_callback(conn, "lead-1", datetime(2026, 9, 14, 16, 30, tzinfo=UTC))

    first_sql, first_params = conn.statements[0]
    assert first_sql.startswith("delete from outreach_events")
    for condition in ("status='planned'", "cadence_step_id is null", "day_offset is null"):
        assert condition in first_sql, "only never-dispatched standalone callbacks may be removed"
    assert first_params == ("lead-1",)
    assert conn.statements[-1][0].startswith("insert into outreach_events")


def test_duplicate_check_is_per_tool_call():
    """A new tool call is a new decision; only the same tool call twice is a duplicate."""
    source = inspect.getsource(lead_status.report_lead_status)
    assert 'f"tool:{tool_call_id}:{lead_id}:{normalized}"' in source
    assert "tool_call_id=tool_call_id" in inspect.getsource(leads_route.lead_status)


def test_booking_link_waits_and_is_one_per_call():
    source = inspect.getsource(lead_status.report_lead_status)
    assert "now()+interval '2 minutes'" in source, "the link must wait for the patient's last word"
    assert "payload->>'call_id'" in source, "asking again must re-arm this call's text, not add one"


def test_a_changed_decision_stops_the_waiting_link():
    sql = " ".join(delivery.CANCEL_CHANGED_BOOKING_LINK_SQL.split())
    assert "n.status='queued'" in sql and "sms_booking_link" in sql
    assert "l.status<>'booking_link_sent'" in sql

    # It has to run before the sender picks texts up, or it cancels nothing.
    source = inspect.getsource(delivery.process_pending_integrations)
    assert source.index("CANCEL_CHANGED_BOOKING_LINK_SQL") < source.index("notifications = conn.execute")
