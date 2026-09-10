from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from rpt_agent.services.lead_status import (
    _schedule_callback,
    clamp_to_business_hours,
    resolve_callback_time,
)

PT = ZoneInfo("America/Los_Angeles")
HOURS = {
    "1": {"open": "09:00", "close": "17:00"},
    "2": {"open": "09:00", "close": "17:00"},
    "3": {"open": "09:00", "close": "17:00"},
    "4": {"open": "09:00", "close": "17:00"},
    "5": {"open": "09:00", "close": "17:00"},
    "6": None,
    "7": None,
}


def _resolve(**kwargs):
    base = {
        "callback_requested_at": None,
        "callback_type": None,
        "delay_minutes": None,
        "callback_datetime_iso": None,
    }
    return resolve_callback_time(PT, **{**base, **kwargs})


def test_relative_delay_is_added_to_now():
    now = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)  # Wednesday 11:00 PT
    assert _resolve(callback_type="relative", delay_minutes="20", now=now) == datetime(
        2026, 8, 26, 18, 20, tzinfo=UTC
    )


def test_relative_delay_is_clamped_to_the_documented_range():
    now = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)
    assert _resolve(callback_type="relative", delay_minutes="1", now=now) == now.replace(minute=5)
    assert _resolve(callback_type="relative", delay_minutes="900", now=now) == datetime(
        2026, 8, 26, 22, 0, tzinfo=UTC
    )


def test_absolute_iso_with_offset_is_converted_to_utc():
    assert _resolve(
        callback_type="absolute", callback_datetime_iso="2026-08-26T15:00:00-07:00"
    ) == datetime(2026, 8, 26, 22, 0, tzinfo=UTC)


def test_absolute_iso_without_offset_is_read_as_practice_local_time():
    """A bare wall-clock string is what the patient said, not UTC."""
    assert _resolve(
        callback_type="absolute", callback_datetime_iso="2026-08-26T15:00:00"
    ) == datetime(2026, 8, 26, 22, 0, tzinfo=UTC)


def test_missing_timing_is_rejected():
    with pytest.raises(ValueError):
        _resolve(callback_type="relative")


def test_non_numeric_delay_is_rejected():
    with pytest.raises(ValueError):
        _resolve(callback_type="relative", delay_minutes="soon")


def _local(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=PT).astimezone(UTC)


def test_time_inside_business_hours_is_left_alone():
    inside = _local(2026, 8, 26, 12)  # Wednesday noon
    assert clamp_to_business_hours(inside, PT, HOURS, []) == inside


def test_before_opening_moves_to_opening_time():
    assert clamp_to_business_hours(_local(2026, 8, 26, 7), PT, HOURS, []) == _local(
        2026, 8, 26, 9
    )


def test_after_closing_moves_to_next_morning():
    assert clamp_to_business_hours(_local(2026, 8, 26, 18), PT, HOURS, []) == _local(
        2026, 8, 27, 9
    )


def test_weekend_moves_to_monday_morning():
    """Saturday and Sunday are null in business_hours, so both are closed."""
    saturday = _local(2026, 8, 29, 10)
    monday = _local(2026, 8, 31, 9)
    assert clamp_to_business_hours(saturday, PT, HOURS, []) == monday
    assert clamp_to_business_hours(_local(2026, 8, 30, 10), PT, HOURS, []) == monday


def test_holiday_is_skipped():
    assert clamp_to_business_hours(
        _local(2026, 8, 26, 12), PT, HOURS, ["2026-08-26"]
    ) == _local(2026, 8, 27, 9)


def test_no_configured_hours_leaves_the_time_untouched():
    when = _local(2026, 8, 30, 3)
    assert clamp_to_business_hours(when, PT, {}, []) == when


def test_callback_moves_an_overdue_remainder_behind_the_callback():
    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def __init__(self):
            self.params = []

        def execute(self, sql, params):
            self.params.append((" ".join(sql.split()), params))
            if sql.startswith("select min"):
                return Result({"earliest": datetime(2026, 9, 10, 17, 20, tzinfo=UTC)})
            return Result()

    conn = Connection()
    callback = datetime(2026, 9, 10, 17, 25, tzinfo=UTC)
    _schedule_callback(conn, "lead", callback)

    shift = next(params for sql, params in conn.params if sql.startswith("update outreach_events"))
    assert shift[0].total_seconds() == 301
    insert = next(params for sql, params in conn.params if sql.startswith("insert into outreach_events"))
    assert insert == ("lead", callback)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
