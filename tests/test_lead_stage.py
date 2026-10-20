"""One rule for the board column and the Sheet's Lead Status (client decision 2026-10-15)."""

import pytest

from rpt_agent.services.lead_stage import sheet_lead_status, stage_of


def _lead(status="in_progress", cadence="active", review=False, total=4, next_event=1):
    return {"status": status, "cadence_state": cadence, "needs_review": review,
            "cadence_total": total, "next_event_id": next_event}


@pytest.mark.parametrize(("lead", "stage", "label"), [
    (_lead(status="new", cadence="pending"), "new", "New"),
    (_lead(), "cadence", "In cadence"),
    (_lead(cadence="paused"), "cadence", "Paused"),
    (_lead(status="callback_scheduled"), "cadence", "In cadence"),
    (_lead(review=True, cadence="paused"), "attention", "Needs Review"),
    (_lead(status="needs_attention", cadence="paused"), "attention", "Needs Review"),
    (_lead(status="booked", cadence="completed"), "booked", "Booked"),
    (_lead(status="booked", review=True), "booked", "Booked"),
    (_lead(status="do_not_contact", cadence="terminated"), "closed", "Do not outreach"),
    (_lead(status="declined", cadence="terminated"), "closed", "Closed"),
    (_lead(status="invalid_phone"), "closed", "Closed"),
    (_lead(status="closed_no_response", cadence="completed"), "closed", "Closed"),  # 14 days done
    (_lead(status="declined", cadence="terminated", next_event=None), "closed", "Closed"),  # not interested
    (_lead(next_event=None), "closed", "Closed"),          # every step done: spent
    (_lead(cadence="paused", next_event=None), "cadence", "Paused"),  # paused is on hold, not spent
])
def test_lead_status_mirrors_the_board_column(lead, stage, label):
    assert stage_of(lead) == stage
    assert sheet_lead_status(lead) == label


def test_the_dashboard_uses_the_same_rule():
    from rpt_agent.routes import dashboard

    assert dashboard._stage is stage_of
