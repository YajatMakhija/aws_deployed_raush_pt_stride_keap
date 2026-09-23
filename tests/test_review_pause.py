"""The pause-on-failure rule lives in two places; keep them from drifting.

review.flag_lead_for_review owns what staff see. Migration 030's trigger is the
fail-safe for paths that never call it. They must agree on the mechanism (flag,
pause, stop the remaining steps) and the trigger must not own the lead's status.
"""
from pathlib import Path

from rpt_agent.services.review import PAUSE_SKIP_REASON, flag_lead_for_review

TRIGGER_SQL = Path("supabase/migrations/030_review_pause_trigger_is_a_fallback.sql").read_text(
    encoding="utf-8"
)


class RecordingConnection:
    def __init__(self):
        self.statements: list[str] = []
        self.params: list[object] = []

    def execute(self, sql, params=None):
        self.statements.append(" ".join(sql.split()))
        self.params.extend(params or ())
        return self

    def fetchall(self):
        return []


def test_both_paths_flag_pause_and_stop_the_schedule():
    conn = RecordingConnection()
    flag_lead_for_review(conn, "lead-1", "cadence SMS was not delivered")
    python_sql = " ".join(conn.statements)

    for fragment in ("needs_review=true", "cadence_state=", "status='skipped'"):
        assert fragment in python_sql, python_sql
    for fragment in ("needs_review = true", "cadence_state = 'paused'", "status = 'skipped'"):
        assert fragment in TRIGGER_SQL, fragment
    assert PAUSE_SKIP_REASON in conn.params, conn.params
    assert PAUSE_SKIP_REASON in TRIGGER_SQL


def test_only_the_application_sets_the_lead_status():
    """The trigger ran last on every failure and overrode the status set in code."""
    assert "status = case" not in TRIGGER_SQL.lower()
    assert "set status = 'needs_attention'" not in TRIGGER_SQL.lower()
    assert "status = 'needs_attention'" not in TRIGGER_SQL.lower()

    conn = RecordingConnection()
    flag_lead_for_review(conn, "lead-1", "reason")
    assert "needs_attention" in " ".join(conn.statements)
