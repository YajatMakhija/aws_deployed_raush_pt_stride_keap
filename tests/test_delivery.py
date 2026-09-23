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


def test_hang_up_during_intro_is_not_a_refusal():
    """Rohan confirmed his name and hung up while Sarah introduced herself. The
    summary read that as "declined" and closed his outreach. Only the patient
    saying no may end it."""
    from rpt_agent.services.delivery import _patient_refused

    rohan = {"artifact": {"messages": [
        {"role": "user", "message": "Hello?"},
        {"role": "bot", "message": "Hi. Am I speaking with Rohan?"},
        {"role": "user", "message": "Yes."},
        {"role": "bot", "message": "Great. This is Sarah calling from Rausch"},
    ]}}
    assert not _patient_refused(rohan)

    refused = {"artifact": {"messages": [
        {"role": "bot", "message": "Would you like to schedule?"},
        {"role": "user", "message": "No, I’m not interested, please don’t call again."},
    ]}}
    assert _patient_refused(refused)

    # Transcript-only reports (no message list) are read the same way.
    assert _patient_refused({"artifact": {"transcript": "AI: Hi\nUser: stop calling me"}})
    assert not _patient_refused({"artifact": {"transcript": "AI: not interested?\nUser: Yes."}})
