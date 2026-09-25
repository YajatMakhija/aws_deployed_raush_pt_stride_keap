import hashlib
import hmac
import inspect
import json
import time
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

import rpt_agent.api as api_module
import rpt_agent.routes.n8n as n8n_routes
from rpt_agent.config import Settings, get_settings
from rpt_agent.security import n8n_sheet_headers, timestamped_hmac
from rpt_agent.services import lead_actions
from rpt_agent.services.lead_actions import ActionExecution
from rpt_agent.services.sheet_sync import (
    _action_label,
    build_sheet_snapshot,
    dashboard_call_link,
    enqueue_sheet_update,
    format_cadence_columns,
    format_cadence_result,
    format_callback_time,
)
from rpt_agent.sheet_sync_worker import SheetDeliveryError, _post_snapshot


def _signed_headers(body: bytes, request_id: str, timestamp: str | None = None) -> dict[str, str]:
    timestamp = timestamp or str(int(time.time()))
    signature = timestamped_hmac("intake-secret-that-is-long-enough", timestamp, body)
    return {
        "Content-Type": "application/json",
        "X-RPT-Key-Id": "rausch-sheet",
        "X-RPT-Timestamp": timestamp,
        "X-RPT-Signature": f"sha256={signature}",
        "X-Request-ID": request_id,
    }


def test_signed_n8n_route_returns_action_result(monkeypatch):
    monkeypatch.setenv("N8N_INTAKE_KEY_ID", "rausch-sheet")
    monkeypatch.setenv("N8N_INTAKE_SECRET", "intake-secret-that-is-long-enough")
    get_settings.cache_clear()
    seen = {}

    def fake_execute(**kwargs):
        seen.update(kwargs)
        return ActionExecution(
            201,
            {
                "request_id": str(kwargs["request_id"]),
                "lead_id": "00000000-0000-0000-0000-000000000002",
                "action": "start_cadence",
                "result": "cadence_started",
                "created": True,
                "cadence_event_count": 8,
            },
        )

    monkeypatch.setattr(n8n_routes, "execute_lead_action", fake_execute)
    monkeypatch.setattr(n8n_routes, "record_integration_event", lambda *args, **kwargs: None)
    request_id = "00000000-0000-0000-0000-000000000001"
    body = json.dumps(
        {
            "action": "start_cadence",
            "lead_id": None,
            "lead": {
                "full_name": "Synthetic",
                "phone": "+15555550100",
                "email": "synthetic@example.test",
                "date_of_birth": "15/03/1990",
                "location": "Dana Point",
                "title": "Any referral title",
            },
        },
        separators=(",", ":"),
    ).encode()
    try:
        response = TestClient(api_module.app).post(
            "/api/v1/integrations/n8n/lead-actions",
            content=body,
            headers=_signed_headers(body, request_id),
        )
        assert response.status_code == 201
        assert response.json()["cadence_event_count"] == 8
        assert seen["request_id"] == UUID(request_id)
        assert seen["lead"]["phone"] == "+15555550100"
        assert str(seen["lead"]["date_of_birth"]) == "1990-03-15"
        assert seen["lead"]["title"] == "Any referral title"
        assert seen["lead"]["lead_type"] == "Any referral title"
    finally:
        get_settings.cache_clear()


def test_n8n_route_accepts_free_text_lead_type_alias(monkeypatch):
    monkeypatch.setenv("N8N_INTAKE_KEY_ID", "rausch-sheet")
    monkeypatch.setenv("N8N_INTAKE_SECRET", "intake-secret-that-is-long-enough")
    get_settings.cache_clear()
    seen = {}

    def fake_execute(**kwargs):
        seen.update(kwargs)
        return ActionExecution(201, {"result": "cadence_started"})

    monkeypatch.setattr(n8n_routes, "execute_lead_action", fake_execute)
    monkeypatch.setattr(n8n_routes, "record_integration_event", lambda *args, **kwargs: None)
    body = json.dumps(
        {
            "action": "start_cadence",
            "lead_id": None,
            "lead": {
                "full_name": "Synthetic Patient",
                "phone": "+15555550100",
                "date_of_birth": "15-03-1990",
                "location": "Dana Point",
                "lead_type": "Sports Rehab",
            },
        },
        separators=(",", ":"),
    ).encode()
    try:
        response = TestClient(api_module.app).post(
            "/api/v1/integrations/n8n/lead-actions",
            content=body,
            headers=_signed_headers(body, "00000000-0000-0000-0000-0000000000bb"),
        )
        assert response.status_code == 201
        assert seen["lead"]["lead_type"] == "Sports Rehab"
        assert seen["lead"]["title"] == "Sports Rehab"
    finally:
        get_settings.cache_clear()


def test_signed_n8n_lead_sync_route(monkeypatch):
    monkeypatch.setenv("N8N_INTAKE_KEY_ID", "rausch-sheet")
    monkeypatch.setenv("N8N_INTAKE_SECRET", "intake-secret-that-is-long-enough")
    get_settings.cache_clear()
    seen = {}

    def fake_sync(**kwargs):
        seen.update(kwargs)
        return ActionExecution(
            409,
            {
                "lead_id": str(kwargs["lead_id"]),
                "previous_lead_id": str(kwargs["lead_id"]),
                "result": "phone_changed_needs_review",
            },
        )

    monkeypatch.setattr(n8n_routes, "sync_sheet_lead", fake_sync)
    monkeypatch.setattr(n8n_routes, "record_integration_event", lambda *args, **kwargs: None)
    request_id = "00000000-0000-0000-0000-000000000001"
    body = json.dumps(
        {
            "lead_id": "00000000-0000-0000-0000-000000000002",
            "lead": {
                "full_name": "Updated Name",
                "phone": "+15555550101",
                "date_of_birth": "15/03/1990",
                "location": "Dana Point",
                "title": "Sports Rehab",
            },
        },
        separators=(",", ":"),
    ).encode()
    try:
        response = TestClient(api_module.app).post(
            "/api/v1/integrations/n8n/lead-sync",
            content=body,
            headers=_signed_headers(body, request_id),
        )
        assert response.status_code == 409
        assert response.json()["result"] == "phone_changed_needs_review"
        assert seen["lead_id"] == UUID("00000000-0000-0000-0000-000000000002")
        assert seen["lead"]["full_name"] == "Updated Name"
    finally:
        get_settings.cache_clear()


def test_n8n_route_rejects_conflicting_title_aliases(monkeypatch):
    monkeypatch.setenv("N8N_INTAKE_KEY_ID", "rausch-sheet")
    monkeypatch.setenv("N8N_INTAKE_SECRET", "intake-secret-that-is-long-enough")
    get_settings.cache_clear()
    monkeypatch.setattr(n8n_routes, "record_integration_event", lambda *args, **kwargs: None)
    body = json.dumps(
        {
            "action": "start_cadence",
            "lead_id": None,
            "lead": {
                "full_name": "Synthetic",
                "phone": "+15555550100",
                "date_of_birth": "15/03/1990",
                "location": "Dana Point",
                "title": "Sports Rehab",
                "lead_type": "Wellness",
            },
        },
        separators=(",", ":"),
    ).encode()
    try:
        response = TestClient(api_module.app).post(
            "/api/v1/integrations/n8n/lead-actions",
            content=body,
            headers=_signed_headers(body, "00000000-0000-0000-0000-0000000000cc"),
        )
        assert response.status_code == 422
    finally:
        get_settings.cache_clear()


def test_sheet_name_parser_accepts_one_word_name():
    assert lead_actions._name_parts("Synthetic") == ("Synthetic", None)


def test_existing_sheet_lead_profile_updates_when_name_changes():
    calls = []

    class Connection:
        def execute(self, query, params=None):
            calls.append((" ".join(query.split()), params))

    lead_actions._update_lead_profile(
        Connection(),
        "00000000-0000-0000-0000-000000000002",
        {
            "full_name": "Updated Name",
            "email": "UPDATED@EXAMPLE.TEST",
            "location": "New Location",
            "title": "New Lead Type",
        },
    )

    query, params = calls[0]
    assert query.startswith("update leads set full_name=")
    assert params[0:4] == ("Updated Name", "Updated", "Updated Name", "Name")
    assert params[4] == "updated@example.test"
    assert params[6:8] == ("New Location", "New Lead Type")


def test_sheet_sync_same_phone_updates_existing_lead(monkeypatch):
    updates = []

    class Connection:
        def execute(self, query, params=None):
            normalized = " ".join(query.split())
            if normalized.startswith("select id from practices"):
                return _Rows({"id": 7})
            if normalized.startswith("select id,phone_e164 from leads"):
                return _Rows({"id": params[0], "phone_e164": "+15555550100"})
            updates.append((normalized, params))
            return _Rows(None)

    class _Rows:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    @contextmanager
    def fake_transaction():
        yield Connection()

    monkeypatch.setattr(lead_actions, "transaction", fake_transaction)
    result = lead_actions.sync_sheet_lead(
        request_id=UUID("00000000-0000-0000-0000-000000000001"),
        lead_id=UUID("00000000-0000-0000-0000-000000000002"),
        lead={"full_name": "Changed Name", "phone": "+15555550100"},
    )
    assert result.status_code == 200
    assert result.body["result"] == "profile_updated"
    assert updates[0][0].startswith("update leads set full_name=")


def test_sheet_sync_changed_phone_requires_review(monkeypatch):
    class _Rows:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def execute(self, query, params=None):
            normalized = " ".join(query.split())
            if normalized.startswith("select id from practices"):
                return _Rows({"id": 7})
            return _Rows({"id": params[0], "phone_e164": "+15555550100"})

    @contextmanager
    def fake_transaction():
        yield Connection()

    monkeypatch.setattr(lead_actions, "transaction", fake_transaction)
    old_id = UUID("00000000-0000-0000-0000-000000000002")
    result = lead_actions.sync_sheet_lead(
        request_id=UUID("00000000-0000-0000-0000-000000000001"),
        lead_id=old_id,
        lead={
            "full_name": "Changed Name",
            "phone": "+15555550101",
            "date_of_birth": date(1990, 3, 15),
            "location": "Dana Point",
            "title": "Sports Rehab",
        },
    )
    assert result.status_code == 409
    assert result.body["result"] == "phone_changed_needs_review"
    assert result.body["lead_id"] == str(old_id)


def test_n8n_route_rejects_bad_or_stale_signature(monkeypatch):
    monkeypatch.setenv("N8N_INTAKE_KEY_ID", "rausch-sheet")
    monkeypatch.setenv("N8N_INTAKE_SECRET", "intake-secret-that-is-long-enough")
    monkeypatch.setenv("N8N_INTAKE_AUTH_DISABLED", "false")
    get_settings.cache_clear()
    body = b'{"action":"do_not_contact","lead_id":"00000000-0000-0000-0000-000000000002",' \
        b'"lead":{"phone":"+15555550100"}}'
    headers = _signed_headers(
        body,
        "00000000-0000-0000-0000-000000000001",
        timestamp=str(int(time.time()) - 600),
    )
    monkeypatch.setattr(n8n_routes, "record_integration_event", lambda *args, **kwargs: None)
    try:
        response = TestClient(api_module.app).post(
            "/api/v1/integrations/n8n/lead-actions", content=body, headers=headers
        )
        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_n8n_auth_bypass_is_rejected_in_staging(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("N8N_INTAKE_AUTH_DISABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(n8n_routes, "record_integration_event", lambda *args, **kwargs: None)
    try:
        response = TestClient(api_module.app).post(
            "/api/v1/integrations/n8n/lead-actions",
            content=b"{}",
            headers={"X-Request-ID": "00000000-0000-0000-0000-000000000001"},
        )
        assert response.status_code == 503
        assert "cannot be used" in response.json()["detail"]
    finally:
        get_settings.cache_clear()


def test_sheet_result_combines_call_and_sms():
    lead = {"status": "in_progress", "cadence_state": "active"}
    event_label, outcome = format_cadence_result(
        [
            {"channel": "call", "status": "delivered", "outcome": "no_answer"},
            {"channel": "sms", "status": "delivered", "delivery_status": "delivered"},
        ],
        lead,
    )
    assert event_label == "Call + SMS"
    assert outcome == "Call: No answer, SMS: Delivered"


def test_sheet_result_has_separate_outcome_columns():
    cadence, call, message, email = format_cadence_columns(
        [
            {"channel": "call", "status": "delivered", "outcome": "transferred"},
            {"channel": "sms", "status": "failed", "delivery_status": "undelivered"},
        ],
        {"status": "transferred_human", "cadence_state": "paused"},
    )
    assert (cadence, call, message, email) == (
        "Call + SMS",
        "Call transferred",
        "Not delivered",
        None,
    )


def test_sheet_result_marks_missing_sms_callback_for_review():
    event_label, outcome = format_cadence_result(
        [{"channel": "sms", "status": "unknown", "delivery_status": "queued"}],
        {"status": "in_progress", "cadence_state": "active"},
    )
    assert event_label == "SMS"
    assert outcome == "Needs staff review"


def test_sheet_action_status_only_for_system_outcomes():
    assert _action_label({"status": "in_progress", "cadence_state": "active"}) is None
    assert _action_label({"status": "do_not_contact", "cadence_state": "terminated"}) == (
        "Do not contact applied"
    )
    assert _action_label({"status": "closed_no_response", "cadence_state": "completed"}) == (
        "Cadence completed"
    )
    assert _action_label({"status": "booked", "cadence_state": "completed"}) == "Booked"


class _Rows:
    def __init__(self, *, one=None, many=None):
        self.one = one
        self.many = many or []

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class _SnapshotConnection:
    def execute(self, query, params=None):
        del params
        if query.startswith("select l.id,l.status"):
            return _Rows(one={
                "id": UUID("00000000-0000-0000-0000-000000000002"),
                "status": "callback_scheduled",
                "cadence_state": "active",
                "needs_review": True,
                "callback_requested_at": datetime(2026, 9, 20, 18, 0, tzinfo=UTC),
                "timezone": "America/Los_Angeles",
            })
        if query.startswith("select request_id from lead_action_requests"):
            return _Rows(one={
                "request_id": UUID("00000000-0000-0000-0000-000000000003"),
            })
        if "from lead_action_requests where lead_id" in query:
            return _Rows(one={
                "request_id": UUID("00000000-0000-0000-0000-000000000001"),
                "created_at": datetime(2026, 9, 18, tzinfo=UTC),
                "result": "cadence_started",
            })
        if "select oe.day_offset" in query:
            return _Rows(one={"day_offset": 0, "finished_at": datetime.now(UTC)})
        if "select oe.id,oe.channel" in query:
            return _Rows(many=[
                {
                    "id": 1,
                    "channel": "call",
                    "status": "delivered",
                    "outcome": "callback",
                    "delivery_status": None,
                }
            ])
        if "select ct.transcript_link" in query:
            return _Rows(one={"transcript_link": "https://dashboard.example.test/transcript"})
        raise AssertionError(query)


class _CallbackSnapshotConnection(_SnapshotConnection):
    """The call a patient asked for carries no day number."""

    def execute(self, query, params=None):
        if "select oe.day_offset" in query:
            return _Rows(one={"day_offset": None, "finished_at": datetime.now(UTC)})
        if "select oe.id,oe.channel" in query:
            assert "is not distinct from" in query, query
            return _Rows(many=[{
                "id": 9,
                "channel": "call",
                "status": "delivered",
                "outcome": "not_interested",
                "delivery_status": None,
            }])
        return super().execute(query, params)


def test_callback_call_reaches_the_sheet():
    """A standalone callback has no day_offset, which the Sheet query used to
    filter out - so the row stopped updating at the last numbered day even
    though a later call had happened."""
    snapshot = build_sheet_snapshot(
        _CallbackSnapshotConnection(),
        "00000000-0000-0000-0000-000000000002",
    )
    assert snapshot["sheet"]["cadence_day"] == "Callback"
    assert snapshot["sheet"]["call_outcome"] == "Answered - declined"


class _BookedSnapshotConnection(_SnapshotConnection):
    def execute(self, query, params=None):
        rows = super().execute(query, params)
        if query.startswith("select l.id,l.status"):
            rows.one = {**rows.one, "status": "booked", "cadence_state": "completed"}
        return rows


def test_booked_lead_sets_the_action_cell_too():
    """The Action cell kept the last command ("Restart cadence") next to a
    Booked status, which reads as an instruction still waiting to run."""
    booked = build_sheet_snapshot(_BookedSnapshotConnection(), "00000000-0000-0000-0000-000000000002")
    assert booked["sheet"]["action"] == "Booked"
    assert booked["sheet"]["action_status"] == "Booked"
    active = build_sheet_snapshot(_SnapshotConnection(), "00000000-0000-0000-0000-000000000002")
    assert "action" not in active["sheet"]


def test_sheet_snapshot_uses_lead_id_and_current_database_truth():
    snapshot = build_sheet_snapshot(
        _SnapshotConnection(),
        "00000000-0000-0000-0000-000000000002",
        practice_slug="rausch-pt",
    )
    assert snapshot["lead_id"] == "00000000-0000-0000-0000-000000000002"
    # Row matching follows the newest completed Sheet command, while cadence
    # history still uses the cadence-start request as its time anchor.
    assert snapshot["action_request_id"] == "00000000-0000-0000-0000-000000000003"
    # Intake owns Action Status / Lead ID; outreach snapshots omit action_status.
    assert "action_status" not in snapshot["sheet"]
    assert snapshot["sheet"]["cadence_day"] == "Day 0"
    assert snapshot["sheet"]["cadence"] == "Call"
    assert snapshot["sheet"]["cadence_status"] == "Callback requested"
    assert snapshot["sheet"]["call_outcome"] == "Callback requested"
    assert snapshot["sheet"]["message_outcome"] is None
    assert snapshot["sheet"]["email_outcome"] is None
    assert snapshot["sheet"]["needs_review"] == "Needs Review"
    assert snapshot["sheet"]["callback_at"] == "Sep 20, 2026 at 11:00 AM PT"


def test_sheet_snapshot_has_dashboard_link_before_a_transcript_exists(monkeypatch):
    class Connection(_SnapshotConnection):
        def execute(self, query, params=None):
            if "select ct.transcript_link" in query:
                return _Rows(one=None)
            return super().execute(query, params)

    monkeypatch.setattr(
        "rpt_agent.services.sheet_sync.dashboard_call_link",
        lambda lead_id: f"https://rpt-frontend-pi.vercel.app/leads/{lead_id}/conversations/calls",
    )
    snapshot = build_sheet_snapshot(
        Connection(),
        "00000000-0000-0000-0000-000000000002",
        practice_slug="rausch-pt",
    )
    assert snapshot["sheet"]["transcript_link"].endswith(
        "/leads/00000000-0000-0000-0000-000000000002/conversations/calls"
    )


def test_dashboard_call_link_uses_configured_frontend_base():
    settings = Settings(dashboard_public_url="https://rpt-frontend-pi.vercel.app/")
    assert dashboard_call_link("lead-1", settings) == (
        "https://rpt-frontend-pi.vercel.app/leads/lead-1/conversations/calls"
    )


def test_dashboard_call_link_removes_login_suffix():
    settings = Settings(dashboard_public_url="https://rpt-frontend-pi.vercel.app/login/")
    assert dashboard_call_link("lead-1", settings) == (
        "https://rpt-frontend-pi.vercel.app/leads/lead-1/conversations/calls"
    )


def test_callback_time_is_readable_and_uses_pacific_time():
    value = datetime(2026, 9, 21, 16, 0, tzinfo=UTC)
    assert format_callback_time(value, "America/Los_Angeles") == (
        "Sep 21, 2026 at 9:00 AM PT"
    )


class _DuplicateRequestConnection:
    def __init__(self, request_hash, response):
        self.request_hash = request_hash
        self.response = response

    def execute(self, query, params=None):
        if "select id,timezone from practices" in query:
            return _Rows(one={"id": 1, "timezone": "America/Los_Angeles"})
        if "insert into lead_action_requests" in query:
            return _Rows(one=None)
        if "from lead_action_requests where request_id" in query:
            return _Rows(one={
                "practice_id": "1",
                "action": "start_cadence",
                "request_hash": self.request_hash,
                "status": "completed",
                "response_body": {"http_status": 201, "body": self.response},
            })
        raise AssertionError((query, params))


def test_action_retry_returns_the_saved_response(monkeypatch):
    request_id = UUID("00000000-0000-0000-0000-000000000001")
    lead = {
        "full_name": "Synthetic Patient",
        "phone": "+15555550100",
        "email": None,
        "date_of_birth": "1990-01-01",
        "location": "Dana Point",
        "lead_type": "Physical Therapy",
    }
    request_hash = lead_actions._request_hash("start_cadence", None, lead)
    saved = {
        "request_id": str(request_id),
        "lead_id": "00000000-0000-0000-0000-000000000002",
        "action": "start_cadence",
        "result": "cadence_started",
        "created": True,
        "cadence_event_count": 8,
    }
    connection = _DuplicateRequestConnection(request_hash, saved)

    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(lead_actions, "transaction", fake_transaction)
    result = lead_actions.execute_lead_action(
        request_id=request_id,
        action="start_cadence",
        lead_id=None,
        lead=lead,
    )
    assert result.status_code == 201
    assert result.body == saved


class _Result:
    def fetchone(self):
        return None


class _OutboxConnection:
    def __init__(self):
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((" ".join(query.split()), params))
        return _Result()


def test_sheet_outbox_event_is_minimal_and_idempotent():
    conn = _OutboxConnection()
    enqueue_sheet_update(
        conn,
        lead_id="00000000-0000-0000-0000-000000000002",
        event_type="call_settled",
        source_key="call-1:no_answer",
        outreach_event_id=12,
    )
    query, params = conn.calls[0]
    assert "destination" in query and "'n8n'" in query
    assert "source_system='google_sheets'" in query
    assert "on conflict(event_id) do nothing" in query
    assert params[2] == "00000000-0000-0000-0000-000000000002"
    assert set(params[3].obj) == {"lead_id", "reason", "outreach_event_id"}


def test_repeat_patient_on_same_number_takes_over_and_warns(monkeypatch):
    """Repeat patients are referred again for another body region with the same
    number. The new lead starts; the older lead still in outreach stops, so the
    patient is never worked twice; staff get a warning naming the older lead."""
    older = {"id": UUID("00000000-0000-0000-0000-000000000009"), "full_name": "Emma Katko",
             "lead_type": "Knee", "status": "in_progress", "cadence_state": "active",
             "call_opt_out": False, "sms_opt_out": True}

    class Connection:
        def __init__(self):
            self.queries = []

        def execute(self, query, params=None):
            normalized = " ".join(query.split())
            self.queries.append(normalized)
            if normalized.startswith("insert into leads"):
                return _Rows(one={"id": UUID("00000000-0000-0000-0000-000000000002")})
            if "phone_e164=%s and id<>%s" in normalized:
                return _Rows(many=[older])
            return _Rows()

    conn = Connection()
    monkeypatch.setattr(lead_actions, "materialize_cadence", lambda *args: 8)
    result = lead_actions._start_cadence(
        conn,
        request_id=UUID("00000000-0000-0000-0000-000000000001"),
        practice={"id": 1, "timezone": "America/Los_Angeles"},
        lead_data={"phone": "+15555550100", "full_name": "Emma Katko", "email": None,
                   "date_of_birth": None, "location": "Dana Point", "lead_type": "Lower back"},
    )

    assert result.status_code == 201
    assert result.body["warning"] == "This number is also on file as Emma Katko (Knee)."
    assert any("cadence_state='terminated'" in q for q in conn.queries), conn.queries
    assert any(q.startswith("update outreach_events set status='skipped'") for q in conn.queries)
    # She opted out of texts on the knee referral; that follows her number.
    assert any("sms_opt_out=sms_opt_out or" in q for q in conn.queries)
    # A replaced lead has nothing left to act on, so it must leave Needs Attention.
    assert any("needs_review=false" in q and "cadence_state='terminated'" in q for q in conn.queries)


def test_same_number_warning_mentions_an_earlier_wrong_number():
    from datetime import datetime as dt

    from rpt_agent.services.review import hand_over_number

    wrong = {"id": UUID("00000000-0000-0000-0000-000000000009"), "full_name": "Cris Test",
             "lead_type": None, "status": "invalid_phone", "cadence_state": "paused",
             "call_opt_out": False, "sms_opt_out": False,
             "status_changed_at": dt(2026, 9, 25, 15, 0, tzinfo=UTC)}

    class Connection:
        def execute(self, query, params=None):
            return _Rows(many=[wrong]) if "id<>%s" in query else _Rows()

    warning = hand_over_number(Connection(), practice_id=1, phone="+15555550100", lead_id="new")
    assert warning == (
        "This number is also on file as Cris Test. "
        "It was marked as a wrong number on Cris Test on Sep 25."
    )


def test_sheet_team_can_mark_booked_without_an_appointment():
    lead_id = UUID("00000000-0000-0000-0000-000000000002")

    class Connection:
        def __init__(self):
            self.queries = []

        def execute(self, query, params=None):
            normalized = " ".join(query.split())
            self.queries.append((normalized, params))
            if normalized.startswith("select id,practice_id,status"):
                return _Rows(one={
                    "id": lead_id,
                    "practice_id": 1,
                    "status": "in_progress",
                    "cadence_state": "active",
                    "call_opt_out": False,
                    "sms_opt_out": False,
                    "phone_e164": "+15555550100",
                })
            return _Rows()

    conn = Connection()
    result = lead_actions._mark_booked(
        conn,
        request_id=UUID("00000000-0000-0000-0000-000000000001"),
        practice={"id": 1},
        lead_id=lead_id,
        phone="+15555550100",
    )

    assert result.body["result"] == "booked_applied"
    statements = [query for query, _ in conn.queries]
    assert any("status='booked',cadence_state='completed'" in query for query in statements)
    assert any("status='skipped'" in query for query in statements)
    assert not any("from appointments" in query for query in statements)


class _RestartConnection:
    def __init__(self, cadence_state: str):
        self.cadence_state = cadence_state
        self.queries: list[tuple[str, object]] = []

    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.queries.append((normalized, params))
        if normalized.startswith("select id,practice_id,status,cadence_state"):
            return _Rows(one={
                "id": UUID("00000000-0000-0000-0000-000000000002"),
                "practice_id": 1,
                "status": "needs_attention" if self.cadence_state == "paused" else "closed_no_response",
                "cadence_state": self.cadence_state,
                "call_opt_out": False,
                "sms_opt_out": False,
                "phone_e164": "+15555550100",
            })
        if normalized.startswith("select exists"):
            return _Rows(one={"unresolved": False})
        return _Rows()


@pytest.mark.parametrize("previous_state", ["paused", "completed"])
def test_restart_always_starts_again_from_day_zero(monkeypatch, previous_state):
    conn = _RestartConnection(previous_state)
    materialized = []
    monkeypatch.setattr(
        lead_actions,
        "materialize_cadence",
        lambda *args, **kwargs: materialized.append((args, kwargs)) or 8,
    )

    result = lead_actions._restart_cadence(
        conn,
        request_id=UUID("00000000-0000-0000-0000-000000000001"),
        practice={"id": 1},
        lead_id=UUID("00000000-0000-0000-0000-000000000002"),
        phone="+15555550100",
    )

    statements = [query for query, _ in conn.queries]
    assert result.body["result"] == "cadence_restarted"
    assert result.body["cadence_event_count"] == 8
    assert materialized
    assert any(query.startswith("delete from outreach_events") for query in statements)


def test_booked_lead_can_be_restarted_and_says_so(monkeypatch):
    """A mis-clicked Booked needs an undo; Booked again undoes a mis-clicked
    restart. The response flags it so the Sheet can show "(was Booked)"."""
    conn = _RestartConnection("completed")
    original_execute = conn.execute

    def execute(query, params=None):
        result = original_execute(query, params)
        if "select id,practice_id,status,cadence_state" in " ".join(query.split()):
            result.one["status"] = "booked"
        return result

    conn.execute = execute
    monkeypatch.setattr(lead_actions, "materialize_cadence", lambda *args, **kwargs: 8)
    result = lead_actions._restart_cadence(
        conn,
        request_id=UUID("00000000-0000-0000-0000-000000000001"),
        practice={"id": 1},
        lead_id=UUID("00000000-0000-0000-0000-000000000002"),
        phone="+15555550100",
    )
    assert result.body["result"] == "cadence_restarted"
    assert result.body["was_booked"] is True


@pytest.mark.parametrize("status", ["do_not_contact", "invalid_phone"])
def test_restart_refuses_terminal_or_unsafe_leads(status):
    conn = _RestartConnection("paused")
    original_execute = conn.execute

    def execute(query, params=None):
        result = original_execute(query, params)
        if "select id,practice_id,status,cadence_state" in " ".join(query.split()):
            result.one["status"] = status
        return result

    conn.execute = execute
    with pytest.raises(lead_actions.LeadActionError) as exc_info:
        lead_actions._restart_cadence(
            conn,
            request_id=UUID("00000000-0000-0000-0000-000000000001"),
            practice={"id": 1},
            lead_id=UUID("00000000-0000-0000-0000-000000000002"),
            phone="+15555550100",
        )

    assert exc_info.value.code == "restart_not_allowed"


def test_sheet_webhook_uses_separate_hmac_secret(monkeypatch):
    monkeypatch.setenv("N8N_SHEET_KEY_ID", "aws-sheet-worker")
    monkeypatch.setenv("N8N_SHEET_WEBHOOK_SECRET", "outbound-secret-that-is-long-enough")
    get_settings.cache_clear()
    body = b'{"lead_id":"lead-1"}'
    headers = n8n_sheet_headers(body, timestamp="1000")
    expected = hmac.new(
        b"outbound-secret-that-is-long-enough",
        b"1000." + body,
        hashlib.sha256,
    ).hexdigest()
    try:
        assert headers["X-RPT-Key-Id"] == "aws-sheet-worker"
        assert headers["X-RPT-Signature"] == f"sha256={expected}"
    finally:
        get_settings.cache_clear()


def test_sheet_post_classifies_google_side_failures(monkeypatch):
    monkeypatch.setenv("N8N_SHEET_KEY_ID", "aws-sheet-worker")
    monkeypatch.setenv("N8N_SHEET_WEBHOOK_SECRET", "outbound-secret-that-is-long-enough")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-event-id"] == "delivery-1"
        return httpx.Response(429, headers={"Retry-After": "30"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = Settings(
        n8n_sheet_webhook_url="https://n8n.example.test/webhook/rpt-sheet-updates",
        n8n_sheet_key_id="aws-sheet-worker",
        n8n_sheet_webhook_secret="outbound-secret-that-is-long-enough",
    )
    try:
        try:
            _post_snapshot(client, settings, "delivery-1", {"lead_id": "lead-1", "sheet": {}})
        except SheetDeliveryError as exc:
            assert exc.retryable is True
            assert exc.http_status == 429
            assert exc.retry_after_seconds == 30
        else:
            raise AssertionError("429 must be classified as a Sheet delivery error")
    finally:
        client.close()
        get_settings.cache_clear()


def test_migration_and_worker_routing_contracts():
    sql = Path("supabase/migrations/023_google_sheets_n8n_integration.sql").read_text(
        encoding="utf-8"
    )
    assert "lead_action_requests" in sql
    assert "call_transcripts" in sql and "transcript_link" in sql
    assert "destination in ('keap', 'n8n')" in sql
    assert "idx_leads_practice_phone" in sql
    free_text_sql = Path("supabase/migrations/024_free_text_lead_type.sql").read_text(
        encoding="utf-8"
    )
    assert "drop constraint if exists leads_lead_type_check" in free_text_sql
    assert "create index" not in free_text_sql.lower()
    review_sql = Path("supabase/migrations/029_sheet_booked_and_review_pause.sql").read_text(
        encoding="utf-8"
    )
    assert "'do_not_contact', 'booked'" in review_sql
    assert "new.status in ('failed', 'unknown')" in review_sql
    assert "cadence_state = 'paused'" in review_sql
    assert "status = 'skipped'" in review_sql
    assert "paused_for_review" in review_sql

    from rpt_agent.services import delivery

    delivery_source = inspect.getsource(delivery.process_pending_integrations)
    assert "destination='keap'" in delivery_source

    from rpt_agent import sheet_sync_worker

    sheet_source = inspect.getsource(sheet_sync_worker._claim_sheet_work)
    assert "destination='n8n'" in sheet_source

    from rpt_agent.services import delivery

    assert "dashboard_call_link" in inspect.getsource(delivery.process_vapi_end_report)


def test_sheet_worker_is_disabled_by_default():
    assert Settings.model_fields["sheet_sync_enabled"].default is False


def test_importable_n8n_workflows_are_valid_and_secret_free():
    workflow_dir = Path("config/n8n/workflows")
    workflows = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(workflow_dir.glob("*.json"))]
    assert len(workflows) == 4
    assert all(workflow["active"] is False and workflow["nodes"] for workflow in workflows)
    combined = json.dumps(workflows)
    assert "6c9192c3-ccbf-4530-aa31-88c4ba296d5b" in combined
    assert "RPT_N8N_INTAKE_SECRET" in combined
    assert "RPT_N8N_SHEET_WEBHOOK_SECRET" in combined
    assert "FIRST_SECRET" not in combined and "SECOND_SECRET" not in combined


def test_profile_sync_workflow_matches_the_database_lead_id():
    workflow = json.loads(
        Path("config/n8n/workflows/04-lead-profile-sync.workflow.json").read_text(
            encoding="utf-8"
        )
    )
    nodes = {node["name"]: node for node in workflow["nodes"]}
    trigger = nodes["Profile Fields Changed"]["parameters"]
    assert "Lead ID" not in trigger["options"]["columnsToWatch"]
    build = nodes["Build and Sign Profile Sync"]["parameters"]["jsCode"]
    assert "lead_id:oldLeadId" in build
    assert "row['Lead ID']" in build
    request = nodes["POST Lead Sync to AWS"]["parameters"]
    assert "/api/v1/integrations/n8n/lead-sync" in request["url"]
    assert "RPT_BACKEND_BASE_URL" in request["url"]
    assert "$env.RPT_N8N_INTAKE_SECRET" in build
    assert any(
        header["name"] == "X-RPT-Key-Id"
        and "RPT_N8N_INTAKE_KEY_ID" in header["value"]
        for header in request["headerParameters"]["parameters"]
    )
    assert "Phone Created New Lead?" not in nodes
    assert "Write Replacement Lead ID" not in nodes
    assert workflow["connections"]["Prepare Profile Sync Result"]["main"][0][0][
        "node"
    ] == "Sync Failed?"
    error_update = nodes["Write Sync Error by Lead ID"]["parameters"]
    assert error_update["columns"]["matchingColumns"] == ["Lead ID"]


def test_n8n_intake_retries_are_finite_and_recover_legacy_processing_rows():
    workflow_dir = Path("config/n8n/workflows")
    intake = json.loads(
        (workflow_dir / "01-lead-action-intake.workflow.json").read_text(encoding="utf-8")
    )
    recovery = json.loads(
        (workflow_dir / "02-lead-action-recovery.workflow.json").read_text(encoding="utf-8")
    )

    intake_nodes = {node["name"]: node for node in intake["nodes"]}
    recovery_nodes = {node["name"]: node for node in recovery["nodes"]}
    intake_result = intake_nodes["Prepare Intake Sheet Result"]["parameters"]["jsCode"]
    recovery_build = recovery_nodes["Build Recovery Requests"]["parameters"]["jsCode"]
    recovery_result = recovery_nodes["Prepare Recovery Result"]["parameters"]["jsCode"]

    assert "Retrying 1/3:" in intake_result
    assert "actionStatus.startsWith('Processing: ')" in recovery_build
    assert "Retrying ([12])" in recovery_build
    assert "attemptsUsed < 3" in recovery_result
    assert "Error: backend unavailable after 3 attempts" in recovery_result

    expected_link = (
        "={{ $json.leadId ? 'https://rpt-frontend-pi.vercel.app/leads/' + "
        "$json.leadId + '/conversations/calls' : '' }}"
    )
    assert intake_nodes["Write Intake Result"]["parameters"]["columns"]["value"][
        "Transcript Link"
    ] == expected_link
    assert recovery_nodes["Write Recovery Result"]["parameters"]["columns"]["value"][
        "Transcript Link"
    ] == expected_link


def test_n8n_intake_preserves_every_changed_sheet_row():
    workflow = json.loads(
        Path("config/n8n/workflows/01-lead-action-intake.workflow.json").read_text(
            encoding="utf-8"
        )
    )
    nodes = {node["name"]: node for node in workflow["nodes"]}
    validate = nodes["Validate and Build Intake"]["parameters"]["jsCode"]
    sign = nodes["Sign Intake Request"]["parameters"]["jsCode"]
    prepare = nodes["Prepare Intake Sheet Result"]["parameters"]["jsCode"]

    assert "$input.all().entries()" in validate
    assert "$input.all().entries()" in prepare
    assert "pairedItem: {item: index}" in validate
    assert "pairedItem: {item: index}" in prepare
    assert "$('Validate and Build Intake').all()" in sign
    assert "Could not uniquely match the processing row by Action Request ID" in sign
    assert "$('Validate and Build Intake').item" not in sign
    assert "signedByRequest" in prepare
    assert "sheetRowNumber" in validate
    for node_name in ("Write Validation Error", "Mark Row Processing"):
        assert nodes[node_name]["parameters"]["columns"]["matchingColumns"] == [
            "row_number"
        ]
    assert nodes["Write Intake Result"]["parameters"]["columns"][
        "matchingColumns"
    ] == ["Action Request ID"]


def test_n8n_sheet_results_target_the_latest_action_request_row():
    workflow_dir = Path("config/n8n/workflows")
    recovery = json.loads(
        (workflow_dir / "02-lead-action-recovery.workflow.json").read_text(
            encoding="utf-8"
        )
    )
    webhook = json.loads(
        (workflow_dir / "03-sheet-update-webhook.workflow.json").read_text(
            encoding="utf-8"
        )
    )
    recovery_nodes = {node["name"]: node for node in recovery["nodes"]}
    webhook_nodes = {node["name"]: node for node in webhook["nodes"]}

    assert recovery_nodes["Write Recovery Result"]["parameters"]["columns"][
        "matchingColumns"
    ] == ["Action Request ID"]
    assert webhook_nodes["Find Row by Lead ID"]["parameters"]["filtersUI"][
        "values"
    ][0]["lookupColumn"] == "Action Request ID"
    assert webhook_nodes["Update System Columns by Phone"]["parameters"]["columns"][
        "matchingColumns"
    ] == ["Action Request ID"]
    verify = webhook_nodes["Verify AWS Request"]["parameters"]["jsCode"]
    prepare_update = webhook_nodes["Prepare Phone-Matched Update"]["parameters"][
        "jsCode"
    ]
    assert "action_request_id" in verify
    assert "Action Request ID" in prepare_update
    assert all(
        field in verify
        for field in (
            "call_outcome",
            "message_outcome",
            "email_outcome",
            "needs_review",
        )
    )
    update_columns = webhook_nodes["Update System Columns by Phone"]["parameters"][
        "columns"
    ]["value"]
    assert "Outcome" not in update_columns
    assert {
        "Needs Review",
        "Call Outcome",
        "Message Outcome",
        "Email Outcome",
    }.issubset(update_columns)


def test_n8n_recovery_supports_booked_action():
    workflow = json.loads(
        Path("config/n8n/workflows/02-lead-action-recovery.workflow.json").read_text(
            encoding="utf-8"
        )
    )
    nodes = {node["name"]: node for node in workflow["nodes"]}
    build = nodes["Build Recovery Requests"]["parameters"]["jsCode"]
    prepare = nodes["Prepare Recovery Result"]["parameters"]["jsCode"]
    assert "'Booked':'booked'" in build
    assert "booked_applied:'Booked'" in prepare


def test_n8n_intake_processes_each_supported_action_once_without_update_loops():
    workflow = json.loads(
        Path("config/n8n/workflows/01-lead-action-intake.workflow.json").read_text(
            encoding="utf-8"
        )
    )
    nodes = {node["name"]: node for node in workflow["nodes"]}
    guard_name = "Action Needs Processing?"
    condition = nodes[guard_name]["parameters"]["conditions"]["conditions"][0]
    guard = condition["leftValue"]
    validate = nodes["Validate and Build Intake"]["parameters"]["jsCode"]
    prepare = nodes["Prepare Intake Sheet Result"]["parameters"]["jsCode"]
    sign = nodes["Sign Intake Request"]["parameters"]["jsCode"]

    assert all(action in guard for action in (
        "Start cadence", "Restart cadence", "Do not contact", "Booked"
    ))
    assert all(state in guard for state in ("processing:", "retrying "))
    assert "status.startsWith('error:')" not in guard
    assert "if (action === 'Restart cadence') return true" in guard
    assert "status === 'booked'" in guard
    assert "'Booked':'booked'" in validate
    assert "booked_applied" in prepare
    assert "$env.RPT_N8N_INTAKE_SECRET" in sign
    assert "const secret = \"" not in sign
    assert workflow["connections"]["Google Sheets Trigger"]["main"][0][0]["node"] == (
        guard_name
    )
    assert workflow["connections"][guard_name]["main"][0][0]["node"] == (
        "Validate and Build Intake"
    )
    assert workflow["connections"][guard_name]["main"][1] == []
