from __future__ import annotations

# Skipped by pause/review so Resume can restore only these rows, not DNC/closed skips.
PAUSE_SKIP_REASON = "paused_for_review"


def skip_remaining_planned(conn, lead_id: str, reason: str = PAUSE_SKIP_REASON) -> int:
    """Stop later cadence steps from being claimed while the lead is on hold."""
    rows = conn.execute(
        "update outreach_events set status='skipped',failure_reason=%s,updated_at=now() "
        "where lead_id=%s and status='planned' returning id",
        (reason[:500], lead_id),
    ).fetchall()
    return len(rows)


def restore_pause_skipped(conn, lead_id: str) -> int:
    """Put pause/review-skipped steps back on the schedule when outreach resumes."""
    rows = conn.execute(
        "update outreach_events set status='planned',failure_reason=null,updated_at=now() "
        "where lead_id=%s and status='skipped' and failure_reason=%s returning id",
        (lead_id, PAUSE_SKIP_REASON),
    ).fetchall()
    return len(rows)


def flag_lead_for_review(conn, lead_id: str, reason: str) -> None:
    """Pause non-terminal outreach whenever staff attention is required.

    This owns the outcome staff see: the lead's status and the reason. Migration
    030's trigger is the fail-safe for paths that never get here (a stale worker,
    a manual SQL fix) and deliberately does less - it flags, pauses and stops the
    schedule, but never sets leads.status. Change one, check the other.
    """
    conn.execute(
        "update leads set needs_review=true,review_reason=%s,review_flagged_at=now(),"
        "status=case when status in ('booked','declined','do_not_contact','closed_no_response',"
        "'invalid_phone') then status else 'needs_attention' end,"
        "cadence_state=case when status in ('booked','declined','do_not_contact',"
        "'closed_no_response') then cadence_state else 'paused' end,"
        "status_changed_at=case when status in ('booked','declined','do_not_contact',"
        "'closed_no_response') then status_changed_at else now() end where id=%s "
        "and status not in ('booked','declined','do_not_contact','closed_no_response')",
        (reason[:500], lead_id),
    )
    skip_remaining_planned(conn, lead_id)
