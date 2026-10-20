"""Direct Google Sheet sync: the n8n rules it reimplements (no database, no network)."""

from datetime import UTC, datetime

import pytest
from fake_sheet import FakeSheet

from rpt_agent.services.google_sheet import parse_grid
from rpt_agent.services.lead_actions import ActionExecution
from rpt_agent.services.sheet_direct import (
    apply_snapshot,
    build_action,
    command_of,
    label_result,
    needs_processing,
    normalize_dob,
    normalize_phone,
    parse_started_at,
    profile_hash,
)


def _row(**cells):
    sheet = FakeSheet()
    sheet.add(**cells)
    return parse_grid(sheet.read_values(), 2).rows[0]


@pytest.mark.parametrize(("raw", "expected"), [
    ("(555) 555-0101", "+15555550101"),
    ("+1 555 555 0101", "+15555550101"),
    ("15555550101", "+15555550101"),
    ("0015555550101", "+15555550101"),
    ("+91 98765 43210", "+919876543210"),
    ("00919876543210", "+919876543210"),
    ("9876543210", "+19876543210"),  # n8n rule: every bare 10-digit number is +1
    ("555-0101", None),
    ("call me", None),
    ("", None),
])
def test_phone_rules_match_the_n8n_intake(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), [
    ("16/01/2007", "16/01/2007"),
    ("1/16/2007", "16/01/2007"),     # second part > 12: month first
    ("03/04/1980", "03/04/1980"),    # ambiguous: day first (n8n rule)
    ("1990-12-31", "31/12/1990"),
    ("1990-12-31T00:00:00", "31/12/1990"),
    ("32874", "01/01/1990"),         # a date cell read unformatted (serial number)
    ("Feb 17, 1974", "17/02/1974"),
    ("31-02", None),
    ("", None),
])
def test_date_of_birth_rules_match_the_n8n_intake(raw, expected):
    assert normalize_dob(raw) == expected


def test_started_at_reads_our_iso_text_and_n8n_pacific_date_serials():
    assert parse_started_at("2026-10-14T17:00:00Z") == datetime(2026, 10, 14, 17, tzinfo=UTC)
    # 46309.5 = 2026-10-14 12:00 wall clock in Los Angeles (PDT, UTC-7)
    assert parse_started_at("46309.5") == datetime(2026, 10, 14, 19, tzinfo=UTC)
    assert parse_started_at("") is None and parse_started_at("soon") is None


@pytest.mark.parametrize(("action", "status", "extra", "expected"), [
    ("Start cadence", "", {}, True),
    ("Start  cadence ", "", {}, True),                      # extra spaces forgiven
    ("booked", "", {"lead_id": "x"}, None),                  # exact text, as n8n: not a command
    ("Start cadence", "", {"lead_id": "x"}, False),          # already linked
    ("Start cadence", "Error: x", {"action_request_id": "r"}, False),
    ("Start cadence", "", {"action_request_id": "r"}, True),  # staff cleared status to re-run
    ("Start cadence", "Cadence started", {}, False),
    ("Restart cadence", "", {"lead_id": "x"}, True),
    ("Restart cadence", "Cadence restarted", {"lead_id": "x"}, False),
    ("Restart cadence", "Cadence started", {"lead_id": "x"}, True),
    ("Restart cadence", "Processing: Restart cadence", {"lead_id": "x"}, False),
    ("Restart cadence", "Retrying 1/3: Restart cadence", {"lead_id": "x"}, False),
    ("Restart cadence", "Phone number changed; needs review", {"lead_id": "x"}, False),
    ("Booked", "Booked", {"lead_id": "x"}, False),
    ("Booked", "Cadence started", {"lead_id": "x"}, True),
    ("Do Not Contact", "Do not contact applied", {"lead_id": "x"}, False),
    ("Do Not Contact", "Unblocked in dashboard", {"lead_id": "x"}, False),
    ("Restart cadence", "Unblocked in dashboard", {"lead_id": "x"}, True),
    ("Call later", "", {}, None),
])
def test_which_rows_need_processing(action, status, extra, expected):
    row = _row(name="Asha Rao", action=action, outcome_status=status, **extra)
    command = command_of(row)
    if expected is None:
        assert command is None
    else:
        assert needs_processing(row, command) is expected


def test_a_start_row_is_validated_like_the_n8n_intake():
    row = _row(name="Asha Rao", phone="555", action="Start cadence", location="Dana Point")
    request, error = build_action(row, "start_cadence")
    assert request is None
    assert error == "invalid phone number; Date Of Birth is required; Case is required"


def test_a_valid_start_row_builds_the_same_request_as_the_api():
    row = _row(name=" Asha Rao ", phone="(555) 555-0101", email="ASHA@Example.Test",
               date_of_birth="16/01/2007", location="Dana Point", case="Ortho",
               action="Start cadence")
    request, error = build_action(row, "start_cadence")
    assert error is None
    lead = request.lead.model_dump(mode="python")
    assert lead["phone"] == "+15555550101" and lead["email"] == "asha@example.test"
    assert lead["date_of_birth"].isoformat() == "2007-01-16" and lead["title"] == "Ortho"


def test_an_impossible_date_gets_a_readable_error_not_request_rejected():
    row = _row(name="Asha Rao", phone="5555550101", date_of_birth="31/02/1990",
               location="Dana Point", case="Ortho", action="Start cadence")
    _, error = build_action(row, "start_cadence")
    assert error.startswith("Date Of Birth") and "calendar day" in error


@pytest.mark.parametrize(("status", "body", "expected"), [
    (201, {"result": "cadence_started", "lead_id": "L"}, ("Cadence started", "In cadence", False)),
    (200, {"result": "already_booked", "lead_id": None}, ("Already booked", "Booked", False)),
    (200, {"result": "do_not_contact_applied"}, ("Do Not Contact applied", "Do not outreach", False)),
    (200, {"result": "something_new"}, ("Completed", None, False)),
    (503, {"detail": "down"}, ("", None, True)),
    (409, {"detail": "Request is still processing"}, ("", None, True)),
    (409, {"code": "restart_too_soon", "detail": "Wait 10 minutes"}, ("Error: Wait 10 minutes", None, False)),
    (422, {"detail": [{"msg": "x"}]}, ("Error: request rejected", None, False)),
])
def test_backend_results_become_the_n8n_labels(status, body, expected):
    outcome = label_result(ActionExecution(status, body))
    assert (outcome.status, outcome.lead_status, outcome.temporary) == expected


def test_profile_fingerprint_ignores_the_columns_we_write():
    row = _row(name="Asha Rao", phone="5555550101", lead_id="L")
    before = profile_hash(row)
    row.cells.update(outcome_status="Cadence started", call_outcome="No answer", lead_status="x")
    assert profile_hash(row) == before
    row.cells["case"] = "Pelvic"
    assert profile_hash(row) != before


def _grid_with(**cells):
    sheet = FakeSheet()
    number = sheet.add(**cells)
    return sheet, number, parse_grid(sheet.read_values(), 2)


def test_a_snapshot_lands_on_the_row_with_its_lead_id_and_writes_the_action_cell():
    _, _, grid = _grid_with(name="Asha Rao", lead_id="L1", action="Restart cadence",
                                     outcome_status="Cadence restarted", lead_status="In cadence")
    snapshot = {"lead_id": "L1", "sheet": {
        "cadence_day": "Day 0", "call_outcome": "Booked", "message_outcome": None,
        "email_outcome": None, "transcript_link": "https://dash/leads/L1/conversations/calls",
        "action_status": "Booked", "action": "Booked", "needs_review": ""}}
    assert apply_snapshot(grid, snapshot)
    row = grid.rows[0]
    assert row.get("outcome_status") == "Booked" and row.get("lead_status") == "Booked"
    assert row.get("action") == "Booked" and row.get("cadence_day") == "Day 0"


def test_a_snapshot_for_a_lead_not_in_the_sheet_is_reported():
    _, _, grid = _grid_with(name="Asha Rao", lead_id="L1")
    assert not apply_snapshot(grid, {"lead_id": "L2", "sheet": {}})


def test_a_snapshot_without_action_status_keeps_the_outcome_cell():
    _, _, grid = _grid_with(name="Asha Rao", lead_id="L1", outcome_status="Cadence started")
    apply_snapshot(grid, {"lead_id": "L1", "sheet": {"call_outcome": "No answer"}})
    assert grid.rows[0].get("outcome_status") == "Cadence started"
    assert grid.rows[0].get("call_outcome") == "No answer"


def test_sheet_archive_days_zero_is_not_the_default(monkeypatch):
    from types import SimpleNamespace

    from rpt_agent import cli

    seen = {}

    class Backend:
        def __init__(self, settings):
            pass

        def read_values(self):
            return FakeSheet().read_values()

    monkeypatch.setattr("rpt_agent.services.google_sheet.GoogleSheetBackend", Backend)
    monkeypatch.setattr("rpt_agent.services.sheet_direct.archive_candidates",
                        lambda grid, days: seen.update(days=days) or [])
    cli.sheet_archive(SimpleNamespace(days=0, apply=False))
    assert seen["days"] == 0


def test_a_sheet_update_is_tried_three_times_then_given_up(monkeypatch):
    from contextlib import contextmanager

    from rpt_agent import sheet_sync_worker
    from rpt_agent.config import Settings

    statuses = []

    class Conn:
        def execute(self, sql, params=None):
            statuses.append("dead" if "status='dead'" in sql else "retry")

    @contextmanager
    def fake_transaction():
        yield Conn()

    monkeypatch.setattr(sheet_sync_worker, "transaction", fake_transaction)
    error = sheet_sync_worker.SheetDeliveryError("Lead ID not found in the Sheet", retryable=True)
    settings = Settings(_env_file=None)
    assert settings.sheet_max_attempts == 3
    # `attempts` is the count before this try: tries 1 and 2 retry, try 3 gives up.
    for before in (0, 1, 2):
        sheet_sync_worker._mark_failed([{"id": 1, "attempts": before}], error, settings)
    assert statuses == ["retry", "retry", "dead"]
