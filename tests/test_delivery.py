from rpt_agent.services.delivery import apply_twilio_message_status


class _Result:
    rowcount = 1

    def __init__(self, one=None, many=None):
        self.one = one
        self.many = many if many is not None else ([] if one is None else [one])

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class _Connection:
    def __init__(self):
        self.queries = []

    def execute(self, query, params):
        self.queries.append((query, params))
        if query.startswith("update sms_messages"):
            return _Result({
                "lead_id": "lead-1",
                "outreach_event_id": 7,
                "delivery_status": "delivered",
                "failure_reason": None,
            })
        if "returning oe.id,oe.lead_id" in query:
            return _Result({"id": 7, "lead_id": "lead-1"})
        return _Result()


def test_twilio_delivery_updates_are_durable_and_forward_only():
    conn = _Connection()
    matched = apply_twilio_message_status(
        conn,
        {"MessageSid": "SM-test", "MessageStatus": "delivered"},
    )
    assert matched == 3
    assert len(conn.queries) == 5
    assert all("status='delivered'" in query for query, _params in conn.queries[:3])
    assert all(params[-1] == "SM-test" for _query, params in conn.queries[:3])
    assert "update outreach_events" in conn.queries[3][0]
    assert "destination" in conn.queries[4][0]


def test_older_failed_callback_cannot_regress_a_delivered_outreach_event():
    conn = _Connection()
    apply_twilio_message_status(
        conn,
        {"MessageSid": "SM-test", "MessageStatus": "failed", "ErrorCode": "30001"},
    )
    event_params = conn.queries[3][1]
    # The fake SMS update returns its already-forward delivery state. The event
    # must follow that current database truth, not the older callback payload.
    assert event_params[0] == "delivered"
    assert event_params[1] == "delivered"


def test_undelivered_cadence_sms_pauses_for_review():
    class Connection(_Connection):
        def execute(self, query, params):
            self.queries.append((query, params))
            if query.startswith("update sms_messages"):
                return _Result({
                    "lead_id": "lead-1",
                    "outreach_event_id": 7,
                    "delivery_status": "undelivered",
                    "failure_reason": "30003",
                })
            if "returning oe.id,oe.lead_id" in query:
                return _Result({"id": 7, "lead_id": "lead-1"})
            return _Result()

    conn = Connection()
    apply_twilio_message_status(
        conn,
        {"MessageSid": "SM-test", "MessageStatus": "undelivered", "ErrorCode": "30003"},
    )
    review_queries = [query for query, _ in conn.queries if query.startswith("update leads set")]
    assert len(review_queries) == 1
    assert "needs_review=true" in review_queries[0]
    assert "'paused'" in review_queries[0]
    skip = [(query, params) for query, params in conn.queries if "status='skipped'" in query]
    assert len(skip) == 1
    assert skip[0][1][0] == "paused_for_review"
    assert skip[0][1][1] == "lead-1"


def _settle_with(monkeypatch, structured):
    """Run the post-call fallback on one extractor result; return the status
    it applied, or "review" when the call went to staff instead."""
    from contextlib import contextmanager

    from rpt_agent.observability import WorkflowTrace
    from rpt_agent.services import delivery, lead_status

    applied = {}

    class Conn:
        def execute(self, sql, params=None):
            if "needs_review=true" in sql or "outcome='manual'" in sql:
                applied.setdefault("status", "review")
            return self

        def fetchone(self):
            return {"outcome": applied.get("status")}

        def fetchall(self):
            return []

    @contextmanager
    def fake_transaction():
        yield Conn()

    def fake_report(trace, **kwargs):
        applied["status"], applied["notes"] = kwargs["status"], kwargs["notes"]

    monkeypatch.setattr(delivery, "transaction", fake_transaction)
    monkeypatch.setattr(lead_status, "report_lead_status", fake_report)
    message = {"artifact": {"structuredOutputs": {"x": {"result": structured}}}}
    delivery._settle_from_structured_output(
        WorkflowTrace("t", "test"), message, lead_id="lead-1", event_id=1, call_id="c"
    )
    return applied


def test_hang_up_with_no_decision_keeps_outreach_going(monkeypatch):
    """Rohan hung up during the introduction. The extractor is told that is not
    a refusal and leaves status out; that must count as a missed contact."""
    applied = _settle_with(monkeypatch, {"summary": "Call ended during the introduction."})
    assert applied["status"] == "no_answer"


def test_extractor_refusal_and_opt_out_are_applied_with_their_note(monkeypatch):
    applied = _settle_with(monkeypatch, {"status": "declined", "summary": "Said she is all set."})
    assert applied == {"status": "declined", "notes": "Said she is all set."}
    assert _settle_with(monkeypatch, {"status": "do_not_contact"})["status"] == "do_not_contact"


def test_failed_extraction_goes_to_staff(monkeypatch):
    """An empty object is what an extraction that ran out of tokens returns."""
    assert _settle_with(monkeypatch, {})["status"] == "review"
