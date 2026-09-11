"""A call Sarah never spoke on cannot carry the patient's decision.

A carrier's "call has been forwarded to voice mail" recording ended as
customer-ended-call; with the tool silent, the post-call summary read it as a
decline and closed the lead. Such a call is now a missed call, while a real
conversation still falls back to the summary.
"""
import inspect
from contextlib import contextmanager

from rpt_agent.observability import WorkflowTrace
from rpt_agent.services import delivery, lead_status

LEAD = "11111111-1111-1111-1111-111111111111"
RECORDING = [
    {"role": "system", "message": "prompt"},
    {"role": "user", "message": "call has been forwarded to voice mail. The person is not available"},
]


class Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class Conn:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if sql.startswith("select lead_id,status,outcome from outreach_events"):
            return Result({"lead_id": LEAD, "status": "attempted", "outcome": None})
        return Result()


def end_report(messages):
    variables = {"lead_id": LEAD, "outreach_event_id": "7"}
    return {"message": {
        "type": "end-of-call-report",
        "endedReason": "customer-ended-call",
        "call": {"id": "call-1", "assistantOverrides": {"variableValues": variables}},
        "artifact": {
            "messages": messages,
            "structuredOutputs": {"so": {"result": {"status": "declined", "summary": "voicemail"}}},
        },
    }}


def process(monkeypatch, messages):
    conn, calls = Conn(), []

    @contextmanager
    def held():
        yield conn

    monkeypatch.setattr(delivery, "transaction", held)
    monkeypatch.setattr(delivery, "apply_call_outcome", lambda trace, **kw: calls.append(("outcome", kw)))
    monkeypatch.setattr(
        delivery, "_settle_from_structured_output",
        lambda *args, **kw: calls.append(("summary", kw)) or "not_interested",
    )
    result = delivery.process_vapi_end_report(WorkflowTrace("test", "test"), end_report(messages))
    call_log = next(params for sql, params in conn.statements if sql.startswith("insert into call_logs"))
    return result, calls, call_log


def test_a_call_sarah_never_spoke_on_is_a_missed_call(monkeypatch):
    result, calls, call_log = process(monkeypatch, RECORDING)
    assert result == "no_answer"
    assert calls == [("outcome", {"lead_id": LEAD, "event_id": 7, "outcome": "no_answer", "source": "webhook"})]
    assert call_log[5] == "no_answer"  # answer_state
    assert call_log[7] == "webhook"  # outcome_source


def test_a_real_conversation_still_falls_back_to_the_summary(monkeypatch):
    spoken = RECORDING[:1] + [
        {"role": "bot", "message": "Hi, am I speaking with Maria?"},
        {"role": "user", "message": "Not interested."},
    ]
    result, calls, call_log = process(monkeypatch, spoken)
    assert result == "not_interested"
    assert [kind for kind, _ in calls] == ["summary"]
    assert call_log[7] == "webhook"  # the summary is not the tool


def test_summary_decisions_are_labelled_as_such():
    assert inspect.signature(lead_status.report_lead_status).parameters["source"].default == "tool"
    assert 'source="call summary"' in inspect.getsource(delivery._settle_from_structured_output)
