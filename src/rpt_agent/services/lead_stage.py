"""Which board column a lead is in, and the Google Sheet's Lead Status for it.

One rule for both: the dashboard board and the Sheet's Lead Status column must never disagree
(client decision 2026-10-15: Lead Status mirrors the dashboard column, with Paused shown).
"""

from __future__ import annotations

from typing import Any

CLOSED_STATUSES = frozenset(
    {
        "declined",
        "do_not_contact",
        "closed_no_response",
        "invalid_phone",
    }
)


def stage_of(row: dict[str, Any]) -> str:
    """Bucket a lead for the board. 'cadence' means outreach is still running."""
    if row["status"] == "booked":
        return "booked"
    if row["needs_review"] or row["status"] == "needs_attention":
        return "attention"
    # A finished lead is not in cadence. Without this it falls through below and
    # a declined patient keeps showing as actively worked.
    if row["status"] in CLOSED_STATUSES or row["cadence_state"] in {"completed", "terminated"}:
        return "closed"
    if row["status"] == "new" or row["cadence_state"] == "pending":
        return "new"
    # Pausing skips the remaining steps until resume, so a paused lead has no
    # next event either. That is on hold, not spent.
    if row["cadence_state"] == "paused":
        return "cadence"
    # Every step has run and nothing is awaiting a provider result, so the
    # cadence is spent even though the lead was never explicitly closed.
    if row.get("cadence_total") and not row.get("next_event_id"):
        return "closed"
    return "cadence"


def sheet_lead_status(row: dict[str, Any]) -> str:
    """The Sheet's Lead Status: the board column, with Paused and Do not outreach named."""
    stage = stage_of(row)
    if stage == "booked":
        return "Booked"
    if stage == "attention":
        return "Needs Review"
    if row["status"] == "do_not_contact":
        return "Do not outreach"  # the label staff already know from the n8n Sheet
    if stage == "closed":
        return "Closed"
    if stage == "new":
        return "New"
    if row["cadence_state"] == "paused":
        return "Paused"
    return "In cadence"
