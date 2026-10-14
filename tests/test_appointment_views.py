"""Booking records shared by the calendar, lead page, home page and board."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from rpt_agent.services import appointment_views as av

NOW = datetime(2026, 10, 20, 16, tzinfo=UTC)


@pytest.mark.parametrize(("raw", "expected"), [
    ("Unmarked", "scheduled"), ("Confirmed", "scheduled"), ("O", "scheduled"), ("U", "scheduled"),
    ("Checked In", "completed"), ("C", "completed"),
    ("Cancel", "cancelled"), ("A", "cancelled"), ("Late Cancel", "late_cancel"), ("L", "late_cancel"),
    ("No Show", "no_show"), ("N", "no_show"), (None, "scheduled"), ("Something new", "something new"),
])
def test_stride_words_become_dashboard_states(raw, expected):
    assert av.normalise_state(raw, imported=True) == expected


def test_our_own_states_pass_through_unchanged():
    # "c" is Stride's letter for Checked In, but our own rows never use Stride letters.
    assert av.normalise_state("scheduled", imported=False) == "scheduled"
    assert av.normalise_state("cancelled", imported=False) == "cancelled"


@pytest.mark.parametrize(("row", "imported", "tag", "by"), [
    ({"booking_source": "voice_agent", "lead_id": uuid4()}, False, "elliot", "voice_agent"),
    ({"booking_source": "voice_agent", "lead_id": uuid4()}, True, "elliot", "voice_agent"),
    ({"booking_source": "manual", "lead_id": uuid4()}, False, "sheet_lead", "staff"),
    ({"booking_source": None, "lead_id": uuid4()}, True, "sheet_lead", "stride"),
    ({"booking_source": None, "lead_id": None}, True, "stride_only", "stride"),
])
def test_tag_and_who_booked(row, imported, tag, by):
    assert av.booking_tag(row) == tag
    assert av.booked_by(row, imported=imported) == by


def test_quote_is_the_patients_words_before_elliot_confirms():
    transcript = (
        "AI: Hi, this is Elliot from Rausch Physical Therapy.\n"
        "User: Yes.\n"
        "AI: Would Tuesday at nine work?\n"
        "User: Mornings are better for me, I start work at eleven.\n"
        "User: Yes.\n"
        "AI: Great, you're all set for Tuesday at 9 AM with Joe.\n"
        "User: Thanks, bye."
    )
    assert av.booking_quote(transcript) == "Mornings are better for me, I start work at eleven."


@pytest.mark.parametrize("transcript", [
    None, "", "AI: Hello\nUser: Not interested.", "AI: You're booked.\nUser: Okay great thanks so much.",
    "Some text without speakers. booked.",
])
def test_no_quote_when_there_is_no_clear_moment(transcript):
    assert av.booking_quote(transcript) is None


def test_long_quote_is_shortened():
    transcript = "User: " + "word " * 100 + "\nAI: You are booked."
    quote = av.booking_quote(transcript)
    assert quote.endswith("…") and len(quote) <= 240


def _row(**extra):
    start = NOW + timedelta(days=7)
    return {"id": 5, "lead_id": uuid4(), "full_name": "Synthetic Patient", "start_utc": start,
            "end_utc": start + timedelta(minutes=60), "clinician_id": 5980, "clinician_name": "Clin A",
            "location": "Laguna Niguel", "case_title": "Physical Therapy", "state": "scheduled",
            "stride_appointment_id": None, "booking_source": "voice_agent", "booked_at": NOW,
            "call_log_id": 9, "call_offset_seconds": 84.6, **extra}


def test_record_for_an_elliot_booking_carries_the_call_and_visit_type():
    r = av.record(_row(), imported=False)
    assert r["id"] == "booking:5"
    assert r["tag"] == "elliot" and r["booked_by"] == "voice_agent"
    assert r["type"] == "Initial Evaluation"  # our engine only books first visits
    assert r["call_log_id"] == 9 and r["call_offset_seconds"] == 84
    assert r["duration_minutes"] == 60


def test_record_ignores_a_negative_call_offset():
    # A hold made before the call row's dial time (clock skew) must not read "-0:03".
    assert av.record(_row(call_offset_seconds=-3), imported=False)["call_offset_seconds"] is None


def test_imported_record_keeps_stride_visit_type_and_id():
    r = av.record(_row(id=777, stride_appointment_id=777, type="Follow-up", state="Checked In",
                       booking_source=None, call_log_id=None, call_offset_seconds=None), imported=True)
    assert r["id"] == "stride:777" and r["type"] == "Follow-up" and r["state"] == "completed"
    assert r["tag"] == "sheet_lead" and r["booked_by"] == "stride"


def _rec(days, state="scheduled"):
    return {"start_utc": NOW + timedelta(days=days), "state": state}


def test_pick_booking_prefers_the_next_visit_still_on_the_books():
    chosen = av.pick_booking([_rec(-10, "completed"), _rec(9), _rec(3), _rec(1, "cancelled")], NOW)
    assert chosen["start_utc"] == NOW + timedelta(days=3)


def test_pick_booking_falls_back_to_the_latest_visit_whatever_happened():
    chosen = av.pick_booking([_rec(-10, "completed"), _rec(2, "late_cancel")], NOW)
    assert chosen["state"] == "late_cancel"
    assert av.pick_booking([], NOW) is None


@pytest.mark.parametrize(("state", "bucket"), [
    ("scheduled", "booked"), ("booking", "booked"), ("unknown", "booked"),
    ("cancelled", "cancelled"), ("late_cancel", "cancelled"), ("no_show", "no_show"),
    ("completed", "completed"),
])
def test_status_buckets(state, bucket):
    assert av.status_bucket(state) == bucket


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _Conn:
    """Answers the booking queries: one of our Elliot rows and Stride's copy of another visit."""

    def __init__(self, lead):
        self.lead, self.queries = lead, []

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        if "to_regclass" in sql:
            return _Result([{"imports": True, "booking": True}])
        if "from appointments a join leads" in sql:
            return _Result([_row(lead_id=self.lead, location_id=3169, location="Lead's own text")])
        if "from stride_appointments sa" in sql:
            return _Result([_row(id=900, stride_appointment_id=900, lead_id=self.lead, state="Cancel",
                                 type="Initial Evaluation", booking_source=None, call_log_id=None,
                                 call_offset_seconds=None, location_id=3169,
                                 start_utc=NOW - timedelta(days=4), end_utc=None)])
        if "from clinicians c" in sql:
            return _Result([{"id": 5980, "name": "Joe Smith", "location": "Laguna Niguel",
                             "stride_location_id": 3169}])
        if "from call_logs cl" in sql:
            return _Result([{"transcript_text": "User: I'd prefer Joe if he is free please.\n"
                                                "AI: Perfect, you're all set."}])
        raise AssertionError(sql)


def test_booking_card_reads_names_tags_and_the_call_moment():
    lead = uuid4()
    conn = _Conn(lead)
    card = av.booking_card(conn, 7, lead, NOW)
    assert card["tag"] == "elliot" and card["state"] == "scheduled"
    assert card["practitioner_name"] == "Joe Smith"  # our rows name clinicians from our table
    assert card["location"] == "Laguna Niguel"  # Stride location id wins over free text
    assert card["quote"] == "I'd prefer Joe if he is free please."
    # Every query is scoped to the practice and the lead, and nothing is written.
    for sql, params in conn.queries:
        assert not sql.lstrip().lower().startswith(("insert", "update", "delete"))
        if "from appointments a join leads" in sql or "from stride_appointments sa" in sql:
            assert params[0] == 7 and params[5] == [str(lead)]


def test_no_leads_means_no_queries():
    class Never:
        def execute(self, *_):
            raise AssertionError("queried for an empty lead list")
    assert av.lead_bookings(Never(), 7, [], NOW) == {}
