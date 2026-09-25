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


REPLACED_REASON = "replaced by a newer referral on this number"


def hand_over_number(conn, *, practice_id: int, phone: str, lead_id: str) -> str | None:
    """Make `lead_id` the one lead working this phone number.

    Repeat patients come back for another body region and reuse their number, so
    several leads may share it - but only one cadence may run, or the patient is
    called twice a day. Every other lead still in outreach on the number stops
    here (its history stays). Opt-outs belong to the person, not the referral,
    so the new lead inherits any the number already has.

    Returns a warning naming the other leads on the number, or None.
    """
    others = conn.execute(
        "select id,full_name,lead_type,status,cadence_state,call_opt_out,sms_opt_out,"
        "status_changed_at "
        "from leads where practice_id=%s and phone_e164=%s and id<>%s "
        "order by created_at for update",
        (practice_id, phone, lead_id),
    ).fetchall()
    if not others:
        return None
    for other in others:
        if other["cadence_state"] in {"pending", "active", "paused"}:
            # needs_review goes too: the board shows any flagged lead in Needs
            # Attention first, and a replaced lead has nothing left to act on.
            conn.execute(
                "update leads set cadence_state='terminated',status_reason=%s,"
                "needs_review=false,status_changed_at=now() where id=%s",
                (REPLACED_REASON, other["id"]),
            )
            skip_remaining_planned(conn, str(other["id"]), REPLACED_REASON)
            conn.execute(
                "insert into lead_status_history(lead_id,from_status,to_status,source,reason) "
                "values(%s,%s,%s,'system',%s)",
                (other["id"], other["status"], other["status"], REPLACED_REASON),
            )
    conn.execute(
        "update leads set call_opt_out=call_opt_out or %s,sms_opt_out=sms_opt_out or %s "
        "where id=%s",
        (
            any(o["call_opt_out"] for o in others),
            any(o["sms_opt_out"] for o in others),
            lead_id,
        ),
    )
    names = ", ".join(
        f"{o['full_name']} ({o['lead_type']})" if o["lead_type"] else str(o["full_name"])
        for o in others
    )
    warning = f"This number is also on file as {names}."
    # A wrong number is a warning, not a block: staff may have corrected the
    # record (the name, not the number, is often what was wrong).
    wrong = [o for o in others if o["status"] == "invalid_phone"]
    if wrong:
        marked = ", ".join(
            f"{o['full_name']} on {o['status_changed_at']:%b} {o['status_changed_at'].day}"
            if o["status_changed_at"] else str(o["full_name"])
            for o in wrong
        )
        warning += f" It was marked as a wrong number on {marked}."
    return warning
