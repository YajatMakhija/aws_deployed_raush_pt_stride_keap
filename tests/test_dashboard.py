from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

import rpt_agent.routes.dashboard as dashboard_routes
from rpt_agent.api import app
from rpt_agent.config import get_settings
from rpt_agent.services.delivery import _call_text_artifacts


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class SnapshotConnection:
    def execute(self, sql, params=None):
        del params
        if "from leads l left join lateral" in sql:
            return Result([{
                "id": uuid4(), "full_name": "Synthetic Lead", "phone_e164": "+15550000001",
                "email": None, "source_system": "synthetic_test", "status": "in_progress",
                "cadence_state": "active", "needs_review": False, "review_reason": None,
                "created_at": datetime.now(UTC), "last_contacted_at": None,
                "next_event_id": 1, "next_step": "Day 3 scheduling call", "next_channel": "call",
                "next_scheduled_for": datetime.now(UTC),
            }])
        if "from appointments a" in sql or "from cadence_steps cs" in sql or "from message_templates mt" in sql:
            return Result([])
        return Result([{"provider_queue": 0, "handoff_queue": 0, "unknown_events": 0, "review_queue": 0}])


class CreateLeadConnection:
    def __init__(self):
        self.lead_id = uuid4()
        self.audit_written = False
        self.created_is_test = False

    def execute(self, sql, params=None):
        if "from practices where slug='rausch-pt'" in sql:
            return Result([{"id": 1, "timezone": "America/Los_Angeles"}])
        if "source_system='dashboard'" in sql:
            return Result([])
        if "insert into leads" in sql:
            self.created_is_test = bool(params[-2])
            return Result([{"id": self.lead_id}])
        if "from leads l left join lateral" in sql:
            return Result([{
                "id": self.lead_id,
                "full_name": "Synthetic Lead",
                "phone_e164": "+15550000001",
                "email": "synthetic@example.test",
                "source_system": "dashboard",
                "status": "in_progress",
                "cadence_state": "active",
                "needs_review": False,
                "review_reason": None,
                "created_at": datetime.now(UTC),
                "last_contacted_at": None,
                "date_of_birth": date(1990, 1, 1),
                "referred_by": "Community partner",
                "lead_type": "Wellness",
                "location": "Dana Point",
                "owner": "Sarah Johnson",
                "is_test": self.created_is_test,
                "next_event_id": 1,
                "next_step": "Initial call",
                "next_channel": "call",
                "next_scheduled_for": datetime.now(UTC),
            }])
        if "insert into dashboard_audit_log" in sql:
            self.audit_written = True
            return Result([])
        raise AssertionError(sql)


class DetailConnection:
    lead_id = uuid4()

    def execute(self, sql, params=None):
        del params
        if "from leads where id=" in sql:
            return Result([{
                "id": self.lead_id,
                "practice_id": 1,
                "full_name": "Synthetic Lead",
                "first_name": "Synthetic",
                "last_name": "Lead",
                "phone_e164": "+15550000001",
                "email": None,
                "date_of_birth": date(1990, 1, 1),
                "source_system": "dashboard",
                "status": "in_progress",
                "status_reason": None,
                "cadence_state": "active",
                "call_opt_out": False,
                "sms_opt_out": False,
                "needs_review": False,
                "review_reason": None,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "last_contacted_at": datetime.now(UTC),
                "callback_requested_at": None,
                "referred_by": None,
                "lead_type": "Physical Therapy",
                "location": "Dana Point",
                "owner": "Test Owner",
                "is_test": True,
            }])
        # The pinned-version lookup selects from cadence_versions but reaches
        # into outreach_events for the id, so it has to be matched first.
        if sql.startswith("select id,name,version_number,status,lead_id from cadence_versions"):
            return Result([{
                "id": 3, "name": "Standard v3", "version_number": 3, "status": "active",
                "lead_id": None, "practice_id": 1, "source_version_id": None,
                "activated_at": datetime.now(UTC), "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }])
        if "from outreach_events oe" in sql:
            return Result([
                {"id": 1, "cadence_step_id": 1, "cadence_version_id": 3,
                 "channel": "sms", "day_offset": 0,
                 "status": "delivered", "scheduled_for": datetime.now(UTC),
                 "created_at": datetime.now(UTC), "executed_at": datetime.now(UTC),
                 "outcome": None, "description": "Initial SMS", "cadence_version_name": "Standard v3",
                 "delivery_status": "delivered", "failure_reason": None},
                {"id": 2, "cadence_step_id": 2, "cadence_version_id": 3,
                 "channel": "call", "day_offset": 3,
                 "status": "attempted", "scheduled_for": datetime.now(UTC),
                 "created_at": datetime.now(UTC), "executed_at": datetime.now(UTC),
                 "outcome": None, "description": "Follow-up Call", "cadence_version_name": "Standard v3",
                 "delivery_status": None, "failure_reason": None},
            ])
        if "from cadence_versions" in sql:
            return Result([{
                "id": 3, "name": "Standard v3", "version_number": 3, "status": "active",
                "lead_id": None, "practice_id": 1, "source_version_id": None,
                "activated_at": datetime.now(UTC), "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }])
        if "from cadence_steps cs" in sql:
            return Result([
                {"id": 1, "step_order": 0, "day_offset": 0, "channel": "sms", "key": "day0_sms",
                 "description": "Initial SMS", "is_active": True, "sms_body": "Hello"},
                {"id": 2, "step_order": 1, "day_offset": 3, "channel": "call", "key": "day3_call",
                 "description": "Follow-up Call", "is_active": True, "sms_body": None},
            ])
        if any(table in sql for table in (
            "from sms_messages", "from call_logs", "from appointments",
            "from lead_status_history", "from lead_message_overrides",
            "from dashboard_audit_log",
        )):
            return Result([])
        raise AssertionError(sql)


def test_dashboard_requires_server_token(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    try:
        response = TestClient(app).get("/api/v1/dashboard/snapshot")
        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_dashboard_snapshot_uses_authenticated_actor(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()

    @contextmanager
    def fake_transaction():
        yield SnapshotConnection()

    monkeypatch.setattr(dashboard_routes, "transaction", fake_transaction)
    try:
        response = TestClient(app).get(
            "/api/v1/dashboard/snapshot",
            headers={
                "X-Dashboard-Token": "x" * 32,
                "X-Dashboard-User-ID": "staff-1",
                "X-Dashboard-User-Email": "staff@example.test",
            },
        )
        assert response.status_code == 200
        assert response.json()["counts"]["cadence"] == 1
    finally:
        get_settings.cache_clear()


def test_dashboard_create_lead_persists_and_materializes(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("TEST_MODE", "true")
    get_settings.cache_clear()
    connection = CreateLeadConnection()

    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(dashboard_routes, "transaction", fake_transaction)
    monkeypatch.setattr(dashboard_routes, "materialize_cadence", lambda *args: 8)
    payload = {
        "idempotency_key": "lead-create-1",
        "first_name": "Synthetic",
        "last_name": "Lead",
        "phone": "+15550000001",
        "email": "synthetic@example.test",
        "date_of_birth": "1990-01-01",
        "referred_by": "Community partner",
        "lead_type": "Wellness",
        "location": "Dana Point",
        "owner": "Sarah Johnson",
        "contact_consent": True,
    }
    headers = {
        "X-Dashboard-Token": "x" * 32,
        "X-Dashboard-User-ID": "staff-1",
        "X-Dashboard-User-Email": "staff@example.test",
    }
    try:
        denied = TestClient(app).post(
            "/api/v1/dashboard/leads",
            headers=headers,
            json={**payload, "contact_consent": False},
        )
        assert denied.status_code == 422
        response = TestClient(app).post(
            "/api/v1/dashboard/leads",
            headers=headers,
            json=payload,
        )
        assert response.status_code == 201
        assert response.json()["lead_type"] == "Wellness"
        assert response.json()["cadence_state"] == "active"
        assert response.json()["is_test"] is True
        assert connection.audit_written
    finally:
        get_settings.cache_clear()


def test_dashboard_lead_detail_uses_database_phone_and_event_progress(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    connection = DetailConnection()

    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(dashboard_routes, "transaction", fake_transaction)
    try:
        response = TestClient(app).get(
            f"/api/v1/dashboard/leads/{connection.lead_id}",
            headers={
                "X-Dashboard-Token": "x" * 32,
                "X-Dashboard-User-ID": "staff-1",
                "X-Dashboard-User-Email": "staff@example.test",
            },
        )
        assert response.status_code == 200
        lead = response.json()["lead"]
        assert lead["phone"] == "+15550000001"
        assert lead["cadence_progress"] == 2
        assert lead["cadence_total"] == 2
        assert lead["next_event_status"] == "attempted"
        assert lead["next_step"] == "Awaiting result: Follow-up Call"
    finally:
        get_settings.cache_clear()


def test_dashboard_migration_and_text_only_call_artifacts():
    sql = Path("supabase/migrations/016_dashboard_security_and_transcripts.sql").read_text(
        encoding="utf-8"
    )
    assert "transcript_text" in sql and "dashboard_audit_log" in sql
    intake_sql = Path("supabase/migrations/017_dashboard_lead_intake.sql").read_text(
        encoding="utf-8"
    )
    assert "lead_type" in intake_sql and "referred_by" in intake_sql
    consent_sql = Path("supabase/migrations/018_dashboard_staff_attestation.sql").read_text(
        encoding="utf-8"
    )
    assert "dashboard_staff_attestation" in consent_sql
    cadence_sql = Path("supabase/migrations/020_cadence_versions.sql").read_text(
        encoding="utf-8"
    )
    assert "create table public.cadence_versions" in cadence_sql
    assert "day_offset between 0 and 365" in cadence_sql
    assert "cadence_versions_one_active_global" in cadence_sql
    assert "cadence_version_id" in cadence_sql
    deletion_sql = Path("supabase/migrations/022_deleted_cadence_versions.sql").read_text(
        encoding="utf-8"
    )
    assert "'deleted'" in deletion_sql and "deleted_at" in deletion_sql
    assert "Personalized plan" in deletion_sql
    transcript, summary = _call_text_artifacts({
        "artifact": {"transcript": "Assistant: Hello\nPatient: Hi"},
        "analysis": {"summary": "Requested a callback."},
    })
    assert transcript == "Assistant: Hello\nPatient: Hi"
    assert summary == "Requested a callback."

class RestartConnection:
    """Records the SQL a stage move issues so the cleanup can be asserted."""

    lead_id = uuid4()

    def __init__(self):
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        del params
        self.statements.append(" ".join(sql.split()))
        if "from leads where id=" in sql:
            return Result([{
                "id": self.lead_id,
                "practice_id": 1,
                "status": "declined",
                "cadence_state": "terminated",
                "needs_review": False,
                "call_opt_out": False,
                "sms_opt_out": False,
            }])
        return Result([])


def test_restart_clears_skipped_steps_not_only_planned(monkeypatch):
    """A lead closed before a restart has leftovers marked 'skipped'.

    Deleting only 'planned' left them behind, so the rebuilt cadence rendered on
    top of the abandoned one and the patient's timeline showed every step twice.
    """
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    connection = RestartConnection()

    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(dashboard_routes, "transaction", fake_transaction)
    monkeypatch.setattr(dashboard_routes, "materialize_cadence", lambda *args: 8)

    response = TestClient(app).post(
        f"/api/v1/dashboard/leads/{connection.lead_id}/stage",
        json={"stage": "new"},
        headers={
            "X-Dashboard-Token": "x" * 32,
            "X-Dashboard-User-ID": "staff-1",
            "X-Dashboard-User-Email": "staff@example.test",
        },
    )
    assert response.status_code == 200

    deletes = [sql for sql in connection.statements if sql.startswith("delete from outreach_events")]
    assert len(deletes) == 1, connection.statements
    assert "'planned'" in deletes[0] and "'skipped'" in deletes[0], deletes[0]


def test_lead_detail_events_expose_creation_batch(monkeypatch):
    """The timeline groups runs by when events were created.

    Without created_at it fell back to watching the day offset step backwards,
    which never happens once two runs overlap in time.
    """
    source = Path(dashboard_routes.__file__).read_text(encoding="utf-8")
    events_query = source[source.index("events = conn.execute("):]
    events_query = events_query[: events_query.index(").fetchall()")]
    assert "oe.created_at" in events_query, events_query


class ActivateVersionConnection:
    def __init__(self, status="draft"):
        now = datetime.now(UTC)
        self.version = {
            "id": 4, "practice_id": 1, "lead_id": None, "version_number": 4,
            "name": "Standard v4", "status": status, "source_version_id": 3,
            "activated_at": None, "created_at": now, "updated_at": now,
        }
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        del params
        normal = " ".join(sql.split())
        self.statements.append(normal)
        if "from cadence_versions where id=" in normal and "for update" in normal:
            return Result([dict(self.version)])
        if normal.startswith("select l.id,l.status,l.cadence_state from leads l"):
            return Result([{
                "id": uuid4(), "status": "in_progress", "cadence_state": "paused",
            }])
        if normal.startswith("update cadence_versions set status='active'"):
            self.version["status"] = "active"
            self.version["activated_at"] = datetime.now(UTC)
            return Result([])
        if "from cadence_versions where id=" in normal:
            return Result([dict(self.version)])
        if "from cadence_steps cs" in normal:
            return Result([{
                "id": 9, "step_order": 0, "day_offset": 30, "channel": "sms",
                "key": "day30_sms", "description": "Day 30 SMS", "is_active": True,
                "sms_body": "Hello",
            }])
        return Result([])


def test_global_activation_replans_only_planned_work_and_preserves_pause(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    connection = ActivateVersionConnection()
    materialized = []

    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(dashboard_routes, "transaction", fake_transaction)
    monkeypatch.setattr(
        dashboard_routes,
        "materialize_cadence",
        lambda *args, **kwargs: materialized.append((args, kwargs)) or 1,
    )
    response = TestClient(app).post(
        "/api/v1/dashboard/cadence-versions/4/activate",
        headers={
            "X-Dashboard-Token": "x" * 32,
            "X-Dashboard-User-ID": "staff-1",
            "X-Dashboard-User-Email": "staff@example.test",
        },
    )
    assert response.status_code == 200
    assert response.json()["replanned_leads"] == 1
    replan = next(sql for sql in connection.statements if sql.startswith("update outreach_events"))
    assert "status='planned'" in replan
    assert "in_flight" not in replan and "delivered" not in replan
    lead_query = next(sql for sql in connection.statements if sql.startswith("select l.id"))
    assert "not exists" in lead_query and "cv.status='active'" in lead_query
    assert materialized[0][1]["cadence_version_id"] == 4
    assert materialized[0][1]["update_lead"] is False
    get_settings.cache_clear()


def test_archived_global_version_can_be_reactivated(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    connection = ActivateVersionConnection(status="archived")

    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(dashboard_routes, "transaction", fake_transaction)
    monkeypatch.setattr(dashboard_routes, "materialize_cadence", lambda *args, **kwargs: 1)
    response = TestClient(app).post(
        "/api/v1/dashboard/cadence-versions/4/activate",
        headers={
            "X-Dashboard-Token": "x" * 32,
            "X-Dashboard-User-ID": "staff-1",
            "X-Dashboard-User-Email": "staff@example.test",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "active"
    get_settings.cache_clear()
def test_cadence_version_rejects_empty_sms_copy(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    response = TestClient(app).put(
        "/api/v1/dashboard/cadence-versions/4",
        headers={
            "X-Dashboard-Token": "x" * 32,
            "X-Dashboard-User-ID": "staff-1",
            "X-Dashboard-User-Email": "staff@example.test",
        },
        json={
            "name": "Month cadence",
            "steps": [{
                "day_offset": 30, "channel": "sms", "description": "Day 30 SMS",
                "is_active": True, "sms_body": "   ",
            }],
        },
    )
    assert response.status_code == 422
    assert "message copy" in response.json()["detail"]
    get_settings.cache_clear()


class PublishedStepConnection:
    def __init__(self):
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        normal = " ".join(sql.split())
        self.statements.append(normal)
        if normal.startswith("select cs.id,cs.practice_id,cs.cadence_version_id"):
            return Result([{
                "id": 12, "practice_id": 1, "cadence_version_id": 9,
                "lead_id": None, "status": "archived",
            }])
        if normal.startswith("select 1 from cadence_steps"):
            return Result([{"exists": 1}])
        if normal.startswith("update cadence_steps"):
            return Result([{"id": 12, "description": "Day 1 reminder", "is_active": params[1]}])
        return Result([])


def test_published_global_step_status_can_be_changed_inline(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    connection = PublishedStepConnection()

    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(dashboard_routes, "transaction", fake_transaction)
    response = TestClient(app).patch(
        "/api/v1/dashboard/cadence-steps/12",
        json={"is_active": False},
        headers={
            "X-Dashboard-Token": "x" * 32,
            "X-Dashboard-User-ID": "staff-1",
            "X-Dashboard-User-Email": "staff@example.test",
        },
    )
    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert any(sql.startswith("update cadence_steps") for sql in connection.statements)
    get_settings.cache_clear()


class DeleteVersionConnection:
    def __init__(self, status="draft"):
        now = datetime.now(UTC)
        self.version = {
            "id": 7, "practice_id": 1, "lead_id": None, "version_number": 7,
            "name": "Standard v7", "status": status, "source_version_id": 3,
            "activated_at": None, "deleted_at": None, "created_at": now, "updated_at": now,
        }
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        del params
        normal = " ".join(sql.split())
        self.statements.append(normal)
        if "from cadence_versions where id=" in normal:
            return Result([dict(self.version)])
        if normal.startswith("update cadence_versions set status='deleted'"):
            self.version["status"] = "deleted"
            self.version["deleted_at"] = datetime.now(UTC)
        return Result([])


def test_cadence_version_delete_is_soft_and_active_is_protected(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    connection = DeleteVersionConnection()

    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(dashboard_routes, "transaction", fake_transaction)
    response = TestClient(app).delete(
        "/api/v1/dashboard/cadence-versions/7",
        headers={
            "X-Dashboard-Token": "x" * 32,
            "X-Dashboard-User-ID": "staff-1",
            "X-Dashboard-User-Email": "staff@example.test",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    assert any("status='deleted'" in sql for sql in connection.statements)

    active = DeleteVersionConnection(status="active")

    @contextmanager
    def active_transaction():
        yield active

    monkeypatch.setattr(dashboard_routes, "transaction", active_transaction)
    response = TestClient(app).delete(
        "/api/v1/dashboard/cadence-versions/7",
        headers={
            "X-Dashboard-Token": "x" * 32,
            "X-Dashboard-User-ID": "staff-1",
            "X-Dashboard-User-Email": "staff@example.test",
        },
    )
    assert response.status_code == 409
    assert not any("status='deleted'" in sql for sql in active.statements)
    get_settings.cache_clear()


class PermanentDeleteVersionConnection:
    def __init__(self, status="deleted"):
        self.version = {
            "id": 7, "practice_id": 1, "lead_id": None, "name": "Standard v7", "status": status,
        }
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        del params
        normal = " ".join(sql.split())
        self.statements.append(normal)
        if "from cadence_versions cv join practices" in normal:
            return Result([dict(self.version)])
        return Result([])


def test_only_deleted_cadence_versions_can_be_permanently_deleted(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    connection = PermanentDeleteVersionConnection()

    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(dashboard_routes, "transaction", fake_transaction)
    response = TestClient(app).delete(
        "/api/v1/dashboard/cadence-versions/7/permanent",
        headers={
            "X-Dashboard-Token": "x" * 32,
            "X-Dashboard-User-ID": "staff-1",
            "X-Dashboard-User-Email": "staff@example.test",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "permanently_deleted"
    assert any("update outreach_events set cadence_step_id=null" in sql for sql in connection.statements)
    assert any("delete from cadence_versions" in sql for sql in connection.statements)

    protected = PermanentDeleteVersionConnection(status="archived")

    @contextmanager
    def protected_transaction():
        yield protected

    monkeypatch.setattr(dashboard_routes, "transaction", protected_transaction)
    response = TestClient(app).delete(
        "/api/v1/dashboard/cadence-versions/7/permanent",
        headers={
            "X-Dashboard-Token": "x" * 32,
            "X-Dashboard-User-ID": "staff-1",
            "X-Dashboard-User-Email": "staff@example.test",
        },
    )
    assert response.status_code == 409
    assert not any(sql.startswith("delete from cadence_versions") for sql in protected.statements)
    get_settings.cache_clear()


class RenameVersionConnection:
    def __init__(self):
        now = datetime.now(UTC)
        self.version = {
            "id": 7, "practice_id": 1, "lead_id": None, "version_number": 7,
            "name": "Standard v7", "status": "deleted", "source_version_id": 3,
            "activated_at": None, "deleted_at": now, "created_at": now, "updated_at": now,
        }

    def execute(self, sql, params=None):
        normal = " ".join(sql.split())
        if normal.startswith("update cadence_versions set name="):
            self.version["name"] = params[0]
            return Result([])
        if "from cadence_versions where id=" in normal:
            return Result([dict(self.version)])
        return Result([])


def test_deleted_cadence_can_be_renamed_and_reused(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()


class TemplateCrudConnection:
    def __init__(self, cadence_step_id=None):
        self.template = {
            "id": 21, "practice_id": 1, "cadence_step_id": cadence_step_id,
            "cadence_version_id": None, "key": "Appointment reminder", "name": "Appointment reminder",
            "body": "Hi {{first_name}}", "is_active": True, "day_offset": None, "description": None,
        }
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        normal = " ".join(sql.split())
        self.statements.append(normal)
        if "from practices where slug='rausch-pt'" in normal:
            return Result([{"id": 1}])
        if normal.startswith("select id from message_templates where practice_id="):
            return Result([])
        if normal.startswith("insert into message_templates"):
            self.template["key"] = params[1]
            self.template["name"] = params[1]
            self.template["body"] = params[2]
            return Result([dict(self.template)])
        if "from message_templates mt join practices" in normal:
            return Result([dict(self.template)])
        if normal.startswith("update message_templates set key="):
            if params[0] is not None:
                self.template["key"] = params[0]
                self.template["name"] = params[0]
            if params[1] is not None:
                self.template["body"] = params[1]
            return Result([])
        if "from message_templates mt left join cadence_steps" in normal:
            return Result([dict(self.template)])
        return Result([])


def test_saved_sms_templates_support_create_rename_and_permanent_delete(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    connection = TemplateCrudConnection()

    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(dashboard_routes, "transaction", fake_transaction)
    headers = {
        "X-Dashboard-Token": "x" * 32,
        "X-Dashboard-User-ID": "staff-1",
        "X-Dashboard-User-Email": "staff@example.test",
    }
    client = TestClient(app)
    response = client.post(
        "/api/v1/dashboard/message-templates",
        headers=headers,
        json={"name": "Appointment reminder", "body": "Hi {{first_name}}"},
    )
    assert response.status_code == 201
    assert response.json()["deletable"] is True

    response = client.patch(
        "/api/v1/dashboard/message-templates/21",
        headers=headers,
        json={"name": "Evaluation reminder", "body": "Please choose a time"},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Evaluation reminder"

    response = client.delete("/api/v1/dashboard/message-templates/21", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "permanently_deleted"
    assert any(sql.startswith("delete from message_templates where id=") for sql in connection.statements)
    get_settings.cache_clear()
    connection = RenameVersionConnection()

    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(dashboard_routes, "transaction", fake_transaction)
    response = TestClient(app).patch(
        "/api/v1/dashboard/cadence-versions/7/name",
        json={"name": "Thirty-day follow-up"},
        headers={
            "X-Dashboard-Token": "x" * 32,
            "X-Dashboard-User-ID": "staff-1",
            "X-Dashboard-User-Email": "staff@example.test",
        },
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Thirty-day follow-up"

    source = Path(dashboard_routes.__file__).read_text(encoding="utf-8")
    clone_query = source[source.index("if payload.source_version_id:") :]
    clone_query = clone_query[: clone_query.index("elif payload.lead_id:")]
    assert "status!='deleted'" not in clone_query
    get_settings.cache_clear()


class StandardCadenceConnection:
    lead_id = uuid4()

    def __init__(self):
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        del params
        normal = " ".join(sql.split())
        self.statements.append(normal)
        if "from leads where id=" in normal:
            return Result([{
                "id": self.lead_id, "practice_id": 1, "status": "in_progress",
                "cadence_state": "paused",
            }])
        if "lead_id is null" in normal and "status='active'" in normal:
            return Result([{"id": 3}])
        return Result([])


def test_switching_to_standard_archives_local_and_replans_only_future(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    connection = StandardCadenceConnection()
    materialized = []

    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(dashboard_routes, "transaction", fake_transaction)
    monkeypatch.setattr(
        dashboard_routes,
        "materialize_cadence",
        lambda *args, **kwargs: materialized.append((args, kwargs)) or 8,
    )
    response = TestClient(app).post(
        f"/api/v1/dashboard/leads/{connection.lead_id}/cadence-mode",
        json={"mode": "standard"},
        headers={
            "X-Dashboard-Token": "x" * 32,
            "X-Dashboard-User-ID": "staff-1",
            "X-Dashboard-User-Email": "staff@example.test",
        },
    )
    assert response.status_code == 200
    assert response.json()["mode"] == "standard"
    assert any("set status='archived'" in sql for sql in connection.statements)
    replan = next(sql for sql in connection.statements if sql.startswith("update outreach_events"))
    assert "status='planned'" in replan
    assert materialized[0][1] == {"cadence_version_id": 3, "update_lead": False}
    get_settings.cache_clear()


class DeleteLeadConnection:
    """Minimal stand-in for the delete path.

    Result has no rowcount, and the endpoint reads it to report what it
    removed, so this returns its own row objects instead.
    """

    class Rows:
        def __init__(self, rows, rowcount=0):
            self.rows, self.rowcount = rows, rowcount

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

    def __init__(self, in_flight=0):
        self.in_flight = in_flight
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        del params
        normal = " ".join(sql.split())
        self.statements.append(normal)
        if normal.startswith("select id,practice_id,full_name,phone_e164 from leads"):
            return self.Rows([{
                "id": uuid4(), "practice_id": 1,
                "full_name": "Delete Me", "phone_e164": "+15550000001",
            }])
        if "status in ('in_flight','attempted')" in normal:
            return self.Rows([{"total": self.in_flight}])
        if normal.startswith("select count(*) as total from outreach_events"):
            return self.Rows([{"total": 8}])
        return self.Rows([], rowcount=2)


def _delete_lead(connection, monkeypatch):
    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(dashboard_routes, "transaction", fake_transaction)
    return TestClient(app).delete(
        f"/api/v1/dashboard/leads/{uuid4()}",
        headers={
            "X-Dashboard-Token": "x" * 32,
            "X-Dashboard-User-ID": "staff-1",
            "X-Dashboard-User-Email": "staff@example.test",
        },
    )


def test_delete_lead_removes_the_tables_that_do_not_cascade(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    connection = DeleteLeadConnection()
    response = _delete_lead(connection, monkeypatch)
    assert response.status_code == 200

    # appointments and dashboard_sms_requests are RESTRICT: leaving them would
    # make the final delete fail. sms_messages and notification_log are SET
    # NULL: leaving them would strand patient message bodies with no owner.
    for table in ("appointments", "dashboard_sms_requests", "sms_messages", "notification_log"):
        assert any(sql.startswith(f"delete from {table} where lead_id=") for sql in connection.statements)
    # The usage ledger records money already spent and is deliberately kept.
    assert not any("delete from test_usage_ledger" in sql for sql in connection.statements)
    assert any("insert into dashboard_audit_log" in sql for sql in connection.statements)
    assert any(sql.startswith("delete from leads where id=") for sql in connection.statements)
    get_settings.cache_clear()


def test_delete_lead_refuses_while_a_call_is_in_flight(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    connection = DeleteLeadConnection(in_flight=1)
    response = _delete_lead(connection, monkeypatch)
    assert response.status_code == 409
    # Nothing may be removed while a provider result is still coming back.
    assert not any(sql.startswith("delete from") for sql in connection.statements)
    get_settings.cache_clear()


class ContactRulesConnection:
    def __init__(self, status="in_progress"):
        self.lead = {
            "id": uuid4(), "practice_id": 1, "status": status, "cadence_state": "active",
            "call_opt_out": False, "sms_opt_out": False,
        }
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        del params
        normal = " ".join(sql.split())
        self.statements.append(normal)
        if normal.startswith("select id,practice_id,status,cadence_state,call_opt_out"):
            return Result([dict(self.lead)])
        if normal.startswith("select status,cadence_state,call_opt_out"):
            return Result([{
                "status": self.lead["status"], "cadence_state": self.lead["cadence_state"],
                "call_opt_out": self.lead["call_opt_out"], "sms_opt_out": self.lead["sms_opt_out"],
            }])
        if normal.startswith("update leads set status='do_not_contact'"):
            self.lead["status"] = "do_not_contact"
            self.lead["cadence_state"] = "terminated"
        return Result([])


def _set_rules(connection, monkeypatch, body):
    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(dashboard_routes, "transaction", fake_transaction)
    return TestClient(app).post(
        f"/api/v1/dashboard/leads/{uuid4()}/contact-rules",
        json=body,
        headers={
            "X-Dashboard-Token": "x" * 32,
            "X-Dashboard-User-ID": "staff-1",
            "X-Dashboard-User-Email": "staff@example.test",
        },
    )


def test_do_not_contact_stops_outreach_as_well_as_blocking_it(monkeypatch):
    """The switch was display-only, so the dashboard said contact was blocked
    while the worker carried on calling. It must reach the lead row, and it must
    also cancel the remaining schedule: planned steps sitting under a
    do_not_contact status would be the same lie in a different place."""
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    connection = ContactRulesConnection()
    response = _set_rules(connection, monkeypatch, {"do_not_contact": True})

    assert response.status_code == 200
    assert response.json()["do_not_contact"] is True
    assert any("status='do_not_contact'" in sql for sql in connection.statements)
    assert any("cadence_state='terminated'" in sql for sql in connection.statements)
    skip = next(sql for sql in connection.statements if sql.startswith("update outreach_events"))
    assert "status='skipped'" in skip and "status='planned'" in skip
    assert any("insert into dashboard_audit_log" in sql for sql in connection.statements)
    get_settings.cache_clear()


def test_clearing_do_not_contact_does_not_silently_restart_outreach(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    connection = ContactRulesConnection(status="do_not_contact")
    response = _set_rules(connection, monkeypatch, {"do_not_contact": False})

    assert response.status_code == 200
    # The block lifts, but nothing re-plans: restarting a lead is a deliberate act.
    assert any("status='in_progress'" in sql for sql in connection.statements)
    assert not any("insert into outreach_events" in sql for sql in connection.statements)
    get_settings.cache_clear()


def test_activating_a_version_leaves_leads_already_in_outreach_alone(monkeypatch):
    """A lead mid-cadence finishes the version it started on.

    Activation used to replan every active or paused lead: remaining steps
    skipped, a fresh schedule built from day 0. A patient on day 13 with one
    message left would be called again from the beginning, and one click did it
    to the whole caseload.
    """
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    connection = ActivateVersionConnection()

    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(dashboard_routes, "transaction", fake_transaction)
    monkeypatch.setattr(dashboard_routes, "materialize_cadence", lambda *a, **k: 1)
    response = TestClient(app).post(
        "/api/v1/dashboard/cadence-versions/4/activate",
        headers={
            "X-Dashboard-Token": "x" * 32,
            "X-Dashboard-User-ID": "staff-1",
            "X-Dashboard-User-Email": "staff@example.test",
        },
    )
    assert response.status_code == 200

    lead_query = next(sql for sql in connection.statements if sql.startswith("select l.id"))
    # Only leads that have not started outreach are picked up.
    assert "l.cadence_state='pending'" in lead_query
    assert "cadence_state in ('active','paused')" not in lead_query
    get_settings.cache_clear()


class TemplateConnection:
    def __init__(self, cadence_step_id=None):
        self.template = {"id": 7, "practice_id": 1, "cadence_step_id": cadence_step_id,
                         "cadence_version_id": 10 if cadence_step_id else None}
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        del params
        normal = " ".join(sql.split())
        self.statements.append(normal)
        if normal.startswith("select mt.id,mt.practice_id,mt.cadence_step_id,mt.cadence_version_id from"):
            return Result([dict(self.template)])
        if normal.startswith("select mt.id,mt.practice_id,mt.cadence_step_id,mt.cadence_version_id,mt.key"):
            return Result([{**self.template, "key": "Welcome", "name": "Welcome", "body": "Hi",
                            "is_active": True, "day_offset": None, "description": None}])
        if normal.startswith("select id from practices"):
            return Result([{"id": 1}])
        if normal.startswith("insert into message_templates"):
            return Result([{"id": 8, "practice_id": 1, "cadence_step_id": None,
                            "cadence_version_id": None, "key": "Welcome", "name": "Welcome",
                            "body": "Hi", "is_active": True}])
        return Result([])


def _template_request(connection, monkeypatch, method, path, body):
    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(dashboard_routes, "transaction", fake_transaction)
    return TestClient(app).request(
        method, f"/api/v1/dashboard/{path}", json=body,
        headers={"X-Dashboard-Token": "x" * 32, "X-Dashboard-User-ID": "staff-1",
                 "X-Dashboard-User-Email": "staff@example.test"},
    )


def test_template_studio_cannot_edit_a_published_cadence_message(monkeypatch):
    """A cadence message belongs to a published, immutable version. Editing it
    from Template Studio changed what live leads receive with no new version.
    Delete already refused this; update did not."""
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    connection = TemplateConnection(cadence_step_id=58)
    response = _template_request(connection, monkeypatch, "PATCH", "message-templates/7", {"body": "changed"})
    assert response.status_code == 409
    assert not any(sql.startswith("update message_templates") for sql in connection.statements)
    get_settings.cache_clear()


def test_reusable_template_stays_editable(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    connection = TemplateConnection(cadence_step_id=None)
    response = _template_request(connection, monkeypatch, "PATCH", "message-templates/7", {"body": "changed"})
    assert response.status_code == 200
    assert any(sql.startswith("update message_templates") for sql in connection.statements)
    get_settings.cache_clear()


def test_new_template_is_not_linked_to_any_cadence(monkeypatch):
    """A new template is reusable copy until someone imports it into a draft."""
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    connection = TemplateConnection()
    response = _template_request(connection, monkeypatch, "POST", "message-templates",
                                 {"name": "Welcome", "body": "Hi"})
    assert response.status_code == 201
    insert = next(sql for sql in connection.statements if sql.startswith("insert into message_templates"))
    assert "values(%s,null,null," in insert
    assert response.json()["cadence_step_id"] is None
    get_settings.cache_clear()


def test_restart_from_new_starts_on_the_active_version():
    """Your rule for version changes: leads already running finish their old
    version; a lead moved back to New starts on whichever version is active.
    The restart passes no version id, so materialize_cadence selects the active
    one -- this pins that down."""
    import inspect

    source = inspect.getsource(dashboard_routes.move_lead_stage)
    restart = source[source.index("restarted = materialize_cadence("):]
    restart = restart[: restart.index(")") + 1]
    assert "cadence_version_id" not in restart
