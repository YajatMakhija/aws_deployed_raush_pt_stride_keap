from datetime import UTC, date, datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

import pytest

from rpt_agent.config import Settings, get_settings
from rpt_agent.observability import WorkflowTrace
from rpt_agent.providers import ProviderError
from rpt_agent.worker import (
    CADENCE_WORKER_LOCK_ID,
    CLAIM_SQL,
    Job,
    cadence_day_time,
    cadence_worker_lock,
    dispatch_job,
    first_contact_time,
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


CLINIC = ZoneInfo("America/Los_Angeles")
HOURS = {
    "timezone": "America/Los_Angeles", "holidays": [],
    "business_hours": {str(day): {"open": "09:00", "close": "17:00"} for day in range(1, 6)},
}


def _pt(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=CLINIC)  # Oct 19 2026 is a Monday


@pytest.mark.parametrize(("added", "first"), [
    (_pt(19, 10, 0), _pt(19, 10, 0)),      # in hours: right away (owner, 2026-10-19)
    (_pt(19, 16, 44), _pt(19, 16, 44)),    # still in time before closing
    (_pt(19, 7, 30), _pt(19, 9, 0)),       # before opening: at opening
    (_pt(19, 16, 50), _pt(20, 9, 0)),      # too close to closing: next morning
    (_pt(19, 20, 0), _pt(20, 9, 0)),       # evening: next morning
    (_pt(23, 18, 0), _pt(26, 9, 0)),       # Friday evening: Monday
    (_pt(24, 11, 0), _pt(26, 9, 0)),       # Saturday: Monday
    (_pt(25, 23, 30), _pt(26, 9, 0)),      # Sunday night: Monday
])
def test_first_contact_is_immediate_in_hours_else_next_opening(added, first):
    assert first_contact_time(HOURS, added.astimezone(UTC)) == first.astimezone(UTC)


def test_first_contact_skips_a_holiday_and_respects_a_short_day():
    holiday = {**HOURS, "holidays": ["2026-10-20", {"date": "2026-10-21", "close": "12:00"}]}
    assert first_contact_time(holiday, _pt(20, 10).astimezone(UTC)) == _pt(21, 9).astimezone(UTC)
    assert first_contact_time(holiday, _pt(21, 11, 50).astimezone(UTC)) == _pt(22, 9).astimezone(UTC)


def test_later_days_keep_the_first_contact_time_inside_hours():
    assert cadence_day_time(HOURS, date(2026, 10, 24), _pt(19, 10, 7).timetz()) == _pt(26, 10, 7).astimezone(UTC)
    late = {**HOURS, "holidays": [{"date": "2026-10-22", "close": "12:00"}]}
    assert cadence_day_time(late, date(2026, 10, 22), _pt(19, 16, 40).timetz()) == _pt(22, 11, 45).astimezone(UTC)


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

    with pytest.raises(RuntimeError, match="another cadence worker"), cadence_worker_lock("postgresql://example", 5):
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
            assert "lead_id is null" in query
            assert "lead_id=%s" not in query
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


PROD_STEPS = [
    {"id": i, "step_order": i, "day_offset": day, "channel": channel}
    for i, (day, channel) in enumerate([(0, "call"), (0, "sms"), (1, "sms"), (3, "call"), (5, "call"),
                                        (5, "sms"), (9, "sms"), (13, "sms"), (14, "call")])
]


START_TIMES = [_pt(day, hour, minute) for day in range(19, 26)
               for hour, minute in [(6, 0), (9, 0), (10, 0), (13, 37), (16, 44), (16, 50), (20, 0)]]


@pytest.mark.parametrize("added", START_TIMES, ids=lambda t: t.strftime("%a-%H%M"))
def test_real_cadence_order_hours_and_first_contact(monkeypatch, added):
    """Every weekday and time a lead can be added: steps in cadence order, each cadence day on its
    own business day inside calling hours, and Day 0 right away when added in calling hours.
    2026-10-19: a Sunday lead got the Day 1 reminder before the Day 0 call, and a 10 AM lead
    waited until the afternoon for its first call."""

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return added.astimezone(tz or UTC)

    monkeypatch.setattr("rpt_agent.worker.datetime", Clock)
    monkeypatch.setattr("rpt_agent.worker.get_settings", lambda: Settings(test_mode=False))
    connection = _CadenceConnection(is_test=False, steps=PROD_STEPS)
    materialize_cadence(connection, "lead", 1, added.date())
    times = connection.inserted
    assert times == sorted(times)
    local = [t.astimezone(CLINIC) for t in times]
    assert all(t.weekday() < 5 and dtime(9) <= t.time() < dtime(17) for t in local)
    in_hours = added.weekday() < 5 and dtime(9) <= added.time() < dtime(16, 45)
    assert (times[0] == added.astimezone(UTC)) is in_hours
    day_dates = {}
    for step, t in zip(PROD_STEPS, local, strict=True):
        day_dates.setdefault(step["day_offset"], set()).add(t.date())
    assert all(len(dates) == 1 for dates in day_dates.values())
    ordered = [next(iter(day_dates[d])) for d in sorted(day_dates)]
    assert ordered == sorted(set(ordered))
    # Weekends only push days later: never sooner than the cadence says.
    assert all((day_dates[d].pop() - local[0].date()).days >= d for d in sorted(day_dates))


def test_worker_claims_only_one_due_event_per_lead():
    assert "row_number() over(partition by oe.lead_id" in CLAIM_SQL
    assert "e.lead_order=1" in CLAIM_SQL
    assert "active_event.status in ('in_flight','attempted')" in CLAIM_SQL
    assert "not l.needs_review" in CLAIM_SQL


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


def test_call_sends_clinic_and_case_for_the_booking_agent():
    job = Job(7, "lead-7", "call", "+15555550123", "Test Patient", None, None, 0,
              "assistant", "phone", clinic="Laguna Niguel", case_name="knee",
              case_type="Physical Therapy", case_types_offered="Physical Therapy, Pelvic Health")
    sent = {}

    class Provider:
        def create_vapi_call(self, trace, payload):
            sent.update(payload)
            return "vapi-call-7"

    assert dispatch_job(WorkflowTrace("worker", "test"), job, Provider()).state == "accepted"
    values = sent["assistantOverrides"]["variableValues"]
    assert values["clinic_location"] == "Laguna Niguel" and values["case_name"] == "knee"
    assert values["lead_id"] == "lead-7" and values["outreach_event_id"] == "7"
    assert values["case_type"] == "Physical Therapy"
    assert values["case_types_offered"] == "Physical Therapy, Pelvic Health"
