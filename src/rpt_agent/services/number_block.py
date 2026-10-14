"""Block and unblock a phone number from the dashboard.

A block lives on the number (suppressed_numbers), not on one lead: the worker
never dials or texts a suppressed number, whichever lead it belongs to. The
Google Sheet blocks the same way, so the dashboard switch and the Sheet's Do Not
Contact have to mean the same thing.

Unblocking is the dangerous direction. While a number is blocked its schedule
keeps ageing, so releasing it without care either fires every overdue step at
once or leaves the Sheet still saying Do Not Contact, ready to block the
patient again the next time the row is touched. Every unblock therefore picks
what happens next (continue, or start over) and tells the Sheet. There is no
"unblock and wait": a lead left on hold showed a Resume button with nothing to
resume, because the block had already cancelled its steps.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from ..worker import materialize_cadence
from .lead_status import record_status
from .review import REPLACED_REASON, hand_over_number, skip_remaining_planned
from .sheet_sync import enqueue_sheet_update

AfterUnblock = Literal["continue", "restart"]

DASHBOARD_BLOCK_SKIP_REASON = "Do Not Contact selected in the dashboard"

# Outbox reasons the Sheet snapshot turns into an Outcome Status. Each is sent
# once, at the moment it happens; later updates leave the cell alone.
SHEET_REASON_FOR = {
    "continue": "cadence_resumed_after_unblock",
    "restart": "cadence_restarted_after_unblock",
}


class NumberBlockError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _sheet_update(conn, lead_id: str, reason: str) -> None:
    enqueue_sheet_update(
        conn, lead_id=lead_id, event_type=reason, source_key=f"{lead_id}:{uuid4().hex}"
    )


def block_number(conn, lead: dict, staff_name: str) -> None:
    """Dashboard Do Not Contact: the same effect as the Sheet's command."""
    lead_id = str(lead["id"])
    conn.execute(
        "update leads set status='do_not_contact',cadence_state='terminated',call_opt_out=true,"
        "sms_opt_out=true,needs_review=false,review_reason=null,review_resolved_at=case when needs_review then now() else review_resolved_at end,"
        "status_reason='staff selected Do Not Contact in the dashboard',"
        "status_changed_at=now() where id=%s",
        (lead_id,),
    )
    skip_remaining_planned(conn, lead_id, DASHBOARD_BLOCK_SKIP_REASON)
    if lead["phone_e164"]:
        conn.execute(
            "insert into suppressed_numbers(phone_e164,reason,source,list_type) "
            "values(%s,%s,'dashboard','internal') "
            "on conflict(phone_e164) do update set reason=excluded.reason,source=excluded.source,"
            "last_verified_at=now()",
            (lead["phone_e164"], f"staff selected Do Not Contact in the dashboard ({staff_name})"[:500]),
        )
    record_status(
        conn, lead_id, lead["status"], "do_not_contact", "dashboard",
        "Do Not Contact selected in the dashboard",
    )
    _sheet_update(conn, lead_id, "dashboard_do_not_contact")


def _remaining_steps(conn, lead_id: str, blocked_at: datetime) -> list[dict]:
    """Steps the block stopped: still planned, or skipped when the block landed.

    Skips older than the block are not the block's doing (an earlier pause, a
    restart), and a lead replaced by a newer referral stays replaced.
    """
    return conn.execute(
        "select id,scheduled_for from outreach_events where lead_id=%s and ("
        "status='planned' or (status='skipped' and updated_at>=%s "
        "and coalesce(failure_reason,'')<>%s)) order by scheduled_for,id",
        (lead_id, blocked_at, REPLACED_REASON),
    ).fetchall()


def unblock_number(conn, lead: dict, after: AfterUnblock, staff_name: str) -> dict:
    """Release the number on this lead and decide what outreach does next.

    The lead being unblocked becomes the one lead working the number, the same
    rule as a restart from the Sheet: any other live cadence on it stops, so a
    patient is never run on two schedules at once.
    """
    lead_id = str(lead["id"])
    phone = lead["phone_e164"]
    block = conn.execute(
        "select created_at from suppressed_numbers where phone_e164=%s for update", (phone,)
    ).fetchone() if phone else None
    if not block and lead["status"] != "do_not_contact":
        raise NumberBlockError(409, "this number is not blocked")
    blocked_at = block["created_at"] if block else lead["status_changed_at"]

    remaining: list[dict] = []
    if after == "continue":
        remaining = _remaining_steps(conn, lead_id, blocked_at)
        if not remaining:
            raise NumberBlockError(409, "no outreach steps are left to continue; choose start over")
    unresolved = conn.execute(
        "select 1 from outreach_events where lead_id=%s and status in ('in_flight','attempted') "
        "limit 1",
        (lead_id,),
    ).fetchone()
    if unresolved:
        raise NumberBlockError(409, "wait for the current call or text result first")

    if phone:
        conn.execute("delete from suppressed_numbers where phone_e164=%s", (phone,))
        # Opt-outs belong to the person, so every lead on the number is released.
        others = conn.execute(
            "update leads set call_opt_out=false,sms_opt_out=false where practice_id=%s "
            "and phone_e164=%s and id<>%s returning id,status",
            (lead["practice_id"], phone, lead_id),
        ).fetchall()
    else:
        others = []
    conn.execute(
        "update leads set call_opt_out=false,sms_opt_out=false,status='in_progress',"
        "status_reason=null,needs_review=false,review_reason=null,review_flagged_at=null,"
        "status_changed_at=now() where id=%s",
        (lead_id,),
    )

    if after == "continue":
        # Keep the cadence's own spacing, counted from now: the first stopped
        # step runs next (inside calling hours) and the rest follow at the gaps
        # they always had.
        first = remaining[0]["scheduled_for"]
        conn.execute(
            "update outreach_events set status='planned',failure_reason=null,"
            "scheduled_for=now()+(scheduled_for-%s),updated_at=now() where id=any(%s)",
            (first, [row["id"] for row in remaining]),
        )
        conn.execute("update leads set cadence_state='active' where id=%s", (lead_id,))
    else:
        conn.execute(
            "delete from outreach_events where lead_id=%s and status in ('planned','skipped')",
            (lead_id,),
        )
        conn.execute(
            "update leads set status='new',cadence_state='pending',last_call_outcome=null,"
            "callback_requested_at=null,callback_notes=null,call_attempts=0 where id=%s",
            (lead_id,),
        )
        if not materialize_cadence(conn, lead_id, lead["practice_id"], datetime.now(UTC).date()):
            raise NumberBlockError(409, "no active cadence is configured")

    if phone:
        hand_over_number(conn, practice_id=lead["practice_id"], phone=phone, lead_id=lead_id)
    new_status = conn.execute("select status from leads where id=%s", (lead_id,)).fetchone()["status"]
    record_status(
        conn, lead_id, lead["status"], new_status, "dashboard",
        f"number unblocked in the dashboard by {staff_name}; next: {after}"[:500],
    )
    _sheet_update(conn, lead_id, SHEET_REASON_FOR[after])
    # Older Sheet rows on this number still read Do Not Contact. Marking them
    # unblocked keeps the intake workflow from re-sending that stale command.
    for other in others:
        if other["status"] == "do_not_contact":
            _sheet_update(conn, str(other["id"]), "number_unblocked")
    return {"after_unblock": after, "resumed_steps": len(remaining)}
