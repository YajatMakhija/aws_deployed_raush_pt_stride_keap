from __future__ import annotations


def flag_lead_for_review(conn, lead_id: str, reason: str) -> None:
    """Pause non-terminal outreach whenever staff attention is required."""
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
