from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from rpt_agent.api import app
from rpt_agent.config import get_settings
from rpt_agent.routes import dashboard_calendar as calendar
from rpt_agent.security import DashboardActor, require_dashboard_auth


def test_calendar_window_includes_the_whole_dst_transition_day():
    start, end = calendar.calendar_window(date(2026, 11, 1), date(2026, 11, 1),
                                          "America/Los_Angeles")
    assert start == datetime(2026, 11, 1, 7, tzinfo=UTC)
    assert end - start == timedelta(hours=25)
    with pytest.raises(HTTPException):
        calendar.calendar_window(date(2026, 10, 13), date(2027, 1, 1), "UTC")


@pytest.mark.parametrize("raw,expected", [("O", "scheduled"), ("A", "cancelled"),
                                         ("L", "late_cancel"), ("N", "no_show")])
def test_stride_status_codes_and_missing_duration_are_not_invented(raw, expected):
    record = calendar.calendar_record({"id": 1, "start_utc": datetime.now(UTC), "state": raw},
                                      imported=True)
    assert record["state"] == expected
    assert record["duration_minutes"] is None
    assert record["lead_id"] is None
    native = calendar.calendar_record({"id": 99, "stride_appointment_id": 200,
                                      "start_utc": datetime.now(UTC)})
    imported = calendar.calendar_record({"id": 200, "start_utc": datetime.now(UTC)}, imported=True)
    assert native["id"] == imported["id"]


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class CalendarConnection:
    def __init__(self):
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        if "from practice_settings" in sql:
            return Result([{"stride_location_timezone": "America/Los_Angeles",
                            "stride_booking_enabled": True, "business_hours": {}}])
        if "to_regclass" in sql:
            return Result([{"imports": True, "booking": True}])
        start = datetime(2026, 10, 13, 16, tzinfo=UTC)
        row = {"id": 99, "lead_id": uuid4(), "full_name": "Synthetic Patient",
               "start_utc": start, "end_utc": start + timedelta(minutes=30),
               "clinician_id": 12, "stride_appointment_id": 200,
               "location_id": 42, "location": "Stride export clinic name", "lead_source": "google_sheets"}
        if "from appointments a" in sql:
            assert params[0] == 7
            assert "not exists(select 1 from stride_appointments" in sql
            return Result([row])
        if "from stride_appointments sa" in sql:
            assert params[0] == 7
            return Result([{**row, "id": 200, "state": "L"}])
        if "from clinicians c" in sql:
            return Result([{"id": 12, "name": "Synthetic Clinician", "location": "Synthetic Clinic",
                            "stride_location_id": 42}])
        return Result([{"at": start}])


def test_calendar_deduplicates_bookings_and_keeps_sheet_link_and_stride_cancellation(monkeypatch):
    conn = CalendarConnection()

    @contextmanager
    def transaction():
        yield conn

    monkeypatch.setattr(calendar, "transaction", transaction)
    result = calendar.appointment_calendar(DashboardActor("review", "review@example.test", practice_id=7),
                                           date(2026, 10, 13), date(2026, 10, 13))
    assert len(result["appointments"]) == 1
    record = result["appointments"][0]
    assert record["id"] == "stride:200"
    assert record["state"] == "late_cancel"
    assert record["lead_source"] == "google_sheets"
    assert record["duration_minutes"] == 30
    assert record["location"] == "Synthetic Clinic"
    assert result["booking_enabled"]
    assert not any(sql.lstrip().startswith(("insert", "update", "delete")) for sql, _ in conn.queries)


def test_calendar_routes_require_staff_authentication(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            assert client.get("/api/v1/dashboard/appointment-calendar?start=2026-10-13&end=2026-10-13").status_code == 401
            assert client.post("/api/v1/dashboard/appointments", json={"lead_id": str(uuid4()), "slot_id": 1}).status_code == 401
    finally:
        get_settings.cache_clear()


def test_invalid_booking_shape_is_rejected_before_provider_work():
    app.dependency_overrides[require_dashboard_auth] = lambda: DashboardActor("test", "test@example.test")
    try:
        with TestClient(app) as client:
            result = client.post("/api/v1/dashboard/appointments", json={"lead_id": str(uuid4()),
                                  "slot_id": -1, "practitioner_id": "spoofed"})
            assert result.status_code == 422
    finally:
        app.dependency_overrides.pop(require_dashboard_auth, None)


def test_booking_refuses_a_lead_from_another_practice():
    class Connection:
        def execute(self, sql, params):
            assert "practice_id=%s" in sql
            assert params[1] == 7
            return Result([])

    with pytest.raises(HTTPException) as exc:
        calendar._staff_lead(Connection(), DashboardActor("test", "test@example.test", practice_id=7), uuid4())
    assert exc.value.status_code == 404


@pytest.mark.parametrize("result,status", [("BOOKED: confirmed", 201),
                                           ("UNCONFIRMED: check first", 409)])
def test_staff_booking_reuses_the_booking_engine_and_handles_uncertain_results(monkeypatch, result, status):
    lead_id = uuid4()
    captured = {}
    day = date(2026, 10, 13)

    class Connection:
        def execute(self, sql, params):
            if "from leads where" in sql:
                return Result([{"id": lead_id, "first_name": "Synthetic", "last_name": "Patient",
                                "date_of_birth": date(1990, 1, 1), "email": None,
                                "status": "in_progress", "call_opt_out": False, "sms_opt_out": False,
                                "needs_review": False, "phone_e164": "+15550000001"}])
            if "from suppressed_numbers" in sql:
                return Result([])
            if "from appointments where" in sql:
                return Result([{"id": 100, "state": "scheduled",
                                "start_utc": datetime(2026, 10, 13, 16, tzinfo=UTC)}])
            assert params == (42, 7)
            return Result([{"local_date": day, "local_time": datetime.min.time(),
                            "display_name": "Synthetic Clinician", "timezone": "America/Los_Angeles"}])

    @contextmanager
    def transaction():
        yield Connection()

    def book_slot(trace, **kwargs):
        captured.update(kwargs)
        return result

    monkeypatch.setattr(calendar, "transaction", transaction)
    monkeypatch.setattr(calendar, "_audit", lambda *args: None)
    monkeypatch.setattr(calendar.slot_booking, "book_slot", book_slot)
    actor = DashboardActor("test", "test@example.test", practice_id=7)
    payload = calendar.CalendarBooking(lead_id=lead_id, slot_id=42)
    if status == 201:
        saved = calendar.create_calendar_appointment(payload, actor)
        assert saved["id"] == "booking:100"
        assert saved["start_day"] == day
    else:
        with pytest.raises(HTTPException) as exc:
            calendar.create_calendar_appointment(payload, actor)
        assert exc.value.status_code == status
        assert "check Stride before booking again" in exc.value.detail
    assert captured["arguments"]["slot_id"] == 42
    assert captured["source"] == "staff"  # recorded as a staff booking, not the voice agent
    assert captured["lead_id"] == str(lead_id)


def test_slot_search_filters_the_practitioner_before_deduplicating_times(monkeypatch):
    captured = {}

    @contextmanager
    def transaction():
        yield object()

    context = SimpleNamespace(
        booking_enabled=True, clinicians=[{"id": 1, "stride_user_id": 10},
                                          {"id": 2, "stride_user_id": 20}],
        location={"name": "Synthetic Clinic"}, case_type={"name": "Knee", "duration_minutes": 60},
        timezone="America/Los_Angeles",
    )

    def search(conn, context, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(calendar, "transaction", transaction)
    monkeypatch.setattr(calendar, "_staff_lead", lambda *args: {})
    monkeypatch.setattr(calendar.slot_booking, "load_context", lambda *args: context)
    monkeypatch.setattr(calendar.slot_booking, "search_slots", search)
    monkeypatch.setattr(calendar.slot_booking, "cache_is_fresh", lambda *args: True)
    calendar.appointment_slots(DashboardActor("test", "test@example.test"), uuid4(),
                               date(2026, 10, 13), days=7, practitioner_id=20)
    assert captured["clinician_ids"] == [2]


@pytest.mark.parametrize("restriction", ["review", "blocked", "do_not_contact", "opted_out", "booked"])
def test_staff_booking_restrictions_stop_before_any_provider_call(monkeypatch, restriction):
    class Connection:
        def execute(self, sql, params):
            if "from suppressed_numbers" in sql:
                return Result([{"blocked": True}] if restriction == "blocked" else [])
            return Result([{"phone_e164": "+15550000001", "status": restriction,
                            "call_opt_out": restriction == "opted_out",
                            "sms_opt_out": restriction == "opted_out",
                            "needs_review": restriction == "review"}])

    @contextmanager
    def transaction():
        yield Connection()

    def forbidden(*args, **kwargs):
        pytest.fail("The booking engine must not run for a restricted lead")

    monkeypatch.setattr(calendar, "transaction", transaction)
    monkeypatch.setattr(calendar.slot_booking, "book_slot", forbidden)
    with pytest.raises(HTTPException) as exc:
        calendar.create_calendar_appointment(calendar.CalendarBooking(lead_id=uuid4(), slot_id=1),
                                             DashboardActor("test", "test@example.test"))
    assert exc.value.status_code == 409


def _booking_harness(monkeypatch, engine_reply=None, engine_error=None):
    """One bookable lead and slot; the booking engine answers `engine_reply` or raises."""
    lead_id = uuid4()
    audits = []

    class Connection:
        def execute(self, sql, params):
            if "from leads where" in sql:
                return Result([{"id": lead_id, "first_name": "Synthetic", "last_name": "Patient",
                                "date_of_birth": date(1990, 1, 1), "email": None,
                                "status": "in_progress", "call_opt_out": False, "sms_opt_out": False,
                                "needs_review": False, "phone_e164": "+15550000001"}])
            if "from suppressed_numbers" in sql:
                return Result([])
            if "from appointments where" in sql:
                return Result([{"id": 9, "state": "scheduled", "start_utc": datetime(2026, 10, 13, 16, tzinfo=UTC)}])
            return Result([{"local_date": date(2026, 10, 13), "local_time": datetime.min.time(),
                            "display_name": "Synthetic Clinician", "timezone": "America/Los_Angeles"}])

    @contextmanager
    def transaction():
        yield Connection()

    def book_slot(trace, **kwargs):
        if engine_error:
            raise engine_error
        return engine_reply

    monkeypatch.setattr(calendar, "transaction", transaction)
    monkeypatch.setattr(calendar, "_audit", lambda conn, actor, practice, action, kind, ref, meta:
                        audits.append((action, meta)))
    monkeypatch.setattr(calendar.slot_booking, "book_slot", book_slot)
    payload = calendar.CalendarBooking(lead_id=lead_id, slot_id=5)
    return payload, DashboardActor("test", "test@example.test", practice_id=7), audits


@pytest.mark.parametrize("reply,words", [
    ("ALREADY_BOOKED: this patient already has an upcoming appointment in Stride.", "already booked"),
    ("PATIENT_EXISTS: this patient is already in our system. Tell the caller ...", "book them in Stride directly"),
    ("TAKEN: that time was just taken. Other openings ...", "just taken"),
    ("NOT_AVAILABLE: 9 AM is not an open time.", "no longer open"),
    ("FOLLOW_UP: our team will finish it.", "could not be finished in time"),
    ("IN_PROGRESS: a booking is already being made.", "already being made"),
])
def test_staff_sees_the_real_reason_a_booking_did_not_go_through(monkeypatch, reply, words):
    # Before: every one of these said "That time could not be booked", and ALREADY_BOOKED even
    # returned 201 "Appointment confirmed" while showing the patient's older appointment.
    payload, actor, audits = _booking_harness(monkeypatch, engine_reply=reply)
    with pytest.raises(HTTPException) as exc:
        calendar.create_calendar_appointment(payload, actor)
    assert exc.value.status_code == 409
    assert words in exc.value.detail
    assert "caller" not in exc.value.detail  # never the voice agent's script
    assert audits[-1] == ("appointment.booking_refused", {"slot_id": 5, "result": reply.split(":")[0]})


@pytest.mark.parametrize("error,status,words", [
    (ValueError("BAD_DATE_OF_BIRTH: that date of birth is not possible."), 400, "date of birth"),
    (ValueError("BAD_EMAIL: that email address is not valid."), 400, "email address"),
    (calendar.slot_booking.BookingToolError("Booking is switched off for this practice."), 409, "switched off"),
])
def test_bad_lead_details_and_switched_off_booking_get_clear_messages(monkeypatch, error, status, words):
    payload, actor, audits = _booking_harness(monkeypatch, engine_error=error)
    with pytest.raises(HTTPException) as exc:
        calendar.create_calendar_appointment(payload, actor)
    assert (exc.value.status_code, words in exc.value.detail) == (status, True)
    assert audits[-1][0] == "appointment.booking_rejected"


def test_successful_staff_booking_is_audited_as_booked(monkeypatch):
    payload, actor, audits = _booking_harness(monkeypatch, engine_reply="BOOKED: Tuesday at nine")
    calendar.create_calendar_appointment(payload, actor)
    assert [a[0] for a in audits] == ["appointment.booking_requested", "appointment.booking_booked"]


def test_a_non_numeric_slot_id_is_refused_before_any_database_work():
    from rpt_agent.observability import WorkflowTrace

    with pytest.raises(ValueError, match="BAD_SLOT"):
        calendar.slot_booking.book_slot(WorkflowTrace("t", "test"), lead_id=str(uuid4()), call_id=None,
                                        arguments={"date": "2026-10-13", "time": "9:00 AM", "slot_id": "12a"},
                                        providers=object())
