from datetime import date, datetime

import pytest

from rpt_agent.config import Settings, get_settings
from rpt_agent.observability import WorkflowTrace
from rpt_agent.providers import ProviderError
from rpt_agent.worker import (
    CADENCE_WORKER_LOCK_ID,
    CLAIM_SQL,
    Job,
    cadence_worker_lock,
    compute_send_time,
    dispatch_job,
    format_phone,
    materialize_cadence,
    render_sms_template,
)


def test_phone_normalization():
    assert format_phone("(949) 555-1212") == "+19495551212"
    assert format_phone("+19495551212") == "+19495551212"
    assert format_phone("19495551212") == "+19495551212"
    assert format_phone("bad") is None
    assert format_phone("123") is None


def test_existing_scheduler_stays_inside_business_day():
    settings = {
        "timezone": "America/Los_Angeles", "holidays": [],
        "business_hours": {str(day): {"open": "09:00", "close": "17:00"} for day in range(1, 6)},
    }
    result = compute_send_time(settings, "lead-1", date(2026, 8, 24), 0)
    local = result.astimezone(__import__("zoneinfo").ZoneInfo("America/Los_Angeles"))
    assert 9 <= local.hour <= 16


def test_sms_template_renders_name_and_booking_link():
    job = Job(
        event_id=1,
        lead_id="lead-1",
        channel="sms",
        phone="+19495551212",
        name="Synthetic Patient",
        body="Hi {name}, book here: {link}",
        booking_link_url="https://example.test/book",
        day_offset=1,
        vapi_assistant_id=None,
        vapi_phone_number_id=None,
    )
    assert render_sms_template(job) == "Hi Synthetic, book here: https://example.test/book"


class _Result:
    def __init__(self, *, one=None, many=None):
        self.one = one
        self.many = many or []

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class _LockConnection:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.queries.append((query, params))
        return _Result(one=(self.acquired,))


def test_cadence_worker_lock_is_held_and_released(monkeypatch):
    conn = _LockConnection()
    monkeypatch.setattr("rpt_agent.worker.psycopg.connect", lambda *_args, **_kwargs: conn)

    with cadence_worker_lock("postgresql://example", 5) as held:
        assert held is conn

    assert conn.queries == [
        ("select pg_try_advisory_lock(%s)", (CADENCE_WORKER_LOCK_ID,)),
        ("select pg_advisory_unlock(%s)", (CADENCE_WORKER_LOCK_ID,)),
    ]


def test_cadence_worker_lock_rejects_second_worker(monkeypatch):
    conn = _LockConnection(acquired=False)
    monkeypatch.setattr("rpt_agent.worker.psycopg.connect", lambda *_args, **_kwargs: conn)

    with pytest.raises(RuntimeError, match="another cadence worker"):
        with cadence_worker_lock("postgresql://example", 5):
            pass


class _CadenceConnection:
    def __init__(self, is_test: bool, steps=None):
        self.is_test = is_test
        self.inserted = []
        self.steps = steps or [
            {"id": 1, "step_order": 1, "day_offset": 0, "channel": "call"},
            {"id": 2, "step_order": 2, "day_offset": 1, "channel": "sms"},
        ]

    def execute(self, query, params):
        if "select ps.business_hours" in query:
            return _Result(one={
                "business_hours": {
                    str(day): {"open": "09:00", "close": "17:00"} for day in range(1, 6)
                },
                "holidays": [],
                "timezone": "America/Los_Angeles",
            })
        if "select is_test" in query:
            return _Result(one={"is_test": self.is_test, "cadence_state": "active"})
        if "select id from cadence_versions" in query:
            return _Result(one={"id": 3})
        if "select id,step_order" in query:
            return _Result(many=self.steps)
        if "insert into outreach_events" in query:
            self.inserted.append(params[5])
        return _Result()


def test_test_mode_compresses_only_synthetic_leads(monkeypatch):
    monkeypatch.setattr(
        "rpt_agent.worker.get_settings",
        lambda: Settings(test_mode=True, test_cadence_day_minutes=5),
    )
    synthetic = _CadenceConnection(is_test=True)
    materialize_cadence(synthetic, "test-lead", 1, date(2026, 8, 24))
    assert isinstance(synthetic.inserted[0], datetime)
    delta = synthetic.inserted[1] - synthetic.inserted[0]
    assert 300 <= delta.total_seconds() <= 302

    production = _CadenceConnection(is_test=False)
    materialize_cadence(production, "prod-lead", 1, date(2026, 8, 24))
    assert (production.inserted[1] - production.inserted[0]).total_seconds() > 5 * 60


def test_compressed_cadence_uses_exact_day_offsets(monkeypatch):
    monkeypatch.setattr(
        "rpt_agent.worker.get_settings",
        lambda: Settings(test_mode=True, test_cadence_day_minutes=1),
    )
    connection = _CadenceConnection(is_test=True, steps=[
        {"id": 1, "step_order": 0, "day_offset": 0, "channel": "call"},
        {"id": 2, "step_order": 1, "day_offset": 0, "channel": "sms"},
        {"id": 3, "step_order": 2, "day_offset": 1, "channel": "sms"},
    ])
    materialize_cadence(connection, "test-lead", 1, date(2026, 8, 24))
    assert connection.inserted == sorted(connection.inserted)
    assert connection.inserted[1] == connection.inserted[0]
    assert (connection.inserted[2] - connection.inserted[0]).total_seconds() == 60


def test_worker_claims_only_one_due_event_per_lead():
    assert "row_number() over(partition by oe.lead_id" in CLAIM_SQL
    assert "e.lead_order=1" in CLAIM_SQL
    assert "active_event.status in ('in_flight','attempted')" in CLAIM_SQL


def test_dispatch_classifies_safe_retry_and_ambiguous_exception():
    job = Job(1, "lead", "sms", "+15555550123", "Test", "hello", None, 0, None, None)

    class RetryableProvider:
        def send_sms(self, trace, phone, body):
            raise ProviderError("twilio", "429", "rate limited", retryable=True)

    class UnexpectedProvider:
        def send_sms(self, trace, phone, body):
            raise ValueError("malformed successful response")

    trace = WorkflowTrace("worker", "test")
    assert dispatch_job(trace, job, RetryableProvider()).state == "retry"
    assert dispatch_job(trace, job, UnexpectedProvider()).state == "unknown"


def test_outbound_kill_switch_blocks_every_send_path(monkeypatch):
    """The switch must bite inside the provider, not at the call sites.

    The dashboard builds its own TwilioService for manual SMS, so a guard
    placed on the worker alone would leave that path able to send while
    outbound was supposed to be suspended.
    """
    from rpt_agent.services.provider_http import ProviderError
    from rpt_agent.services.twilio_service import TwilioService
    from rpt_agent.services.vapi_service import VapiService

    monkeypatch.setenv("OUTBOUND_ENABLED", "false")
    get_settings.cache_clear()
    trace = WorkflowTrace("test", "test")

    for service, call in (
        (TwilioService(), lambda s: s.send_sms(trace, "+15550000001", "hi")),
        (VapiService(), lambda s: s.create_call(trace, {})),
    ):
        with pytest.raises(ProviderError) as caught:
            call(service)
        assert caught.value.code == "outbound_disabled"

    monkeypatch.setenv("OUTBOUND_ENABLED", "true")
    get_settings.cache_clear()


def test_version_lookup_types_its_optional_parameter():
    """Guard the cast that keeps lead creation working.

    The optional cadence_version_id is None for the two commonest callers --
    creating a lead and restarting one from the board. Without ::bigint,
    Postgres cannot type the bare NULL and raises IndeterminateDatatype, so
    both paths return 500.

    Asserted against the SQL text rather than a live query because every other
    test here mocks the connection: a fake cursor accepts an untyped NULL
    happily, which is exactly how this reached production.
    """
    import inspect

    from rpt_agent import worker

    source = inspect.getsource(worker.materialize_cadence)
    version_query = source[source.index("select id from cadence_versions") :]
    version_query = version_query[: version_query.index("order by")]
    assert "%s::bigint is null" in version_query
    assert "id=%s::bigint" in version_query


def test_cadence_completion_does_not_depend_on_the_calendar():
    """The same rule must close a 13-minute run and a 13-day one.

    The previous condition required cadence_started_on + 14 days, which
    described the standard cadence rather than the one the lead is on. A
    compressed test run stayed 'active' with an empty schedule, and a real
    lead finishing on day 13 waited an extra day.

    That mattered beyond tidiness: activating a cadence version replans every
    active or paused lead, so a finished one would be dialled again from day 0.

    Asserted on the statement because the sweep is a single UPDATE against the
    database, and the point is precisely that it carries no time term.
    """
    import inspect

    from rpt_agent import worker

    source = inspect.getsource(worker.run_safety_checks)
    start = source.index("update leads l set status='closed_no_response'")
    statement = source[start : source.index("returning id", start)]

    # No calendar arithmetic of any kind: no day count, no now(), no current_date.
    assert "cadence_started_on" not in statement
    assert "current_date" not in statement
    assert "interval" not in statement

    # Complete exactly when the lead has events and none are outstanding.
    assert "exists(select 1 from outreach_events oe where oe.lead_id=l.id)" in statement
    assert "and oe.status in ('planned','in_flight','attempted')" in statement
    assert "l.cadence_state='active'" in statement
