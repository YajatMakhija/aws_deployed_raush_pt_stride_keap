from datetime import date

import pytest

from rpt_agent.parsing import parse_flexible_date


def test_parse_flexible_date_accepts_day_first_and_iso():
    assert parse_flexible_date("15-03-1990") == date(1990, 3, 15)
    assert parse_flexible_date("15/03/1990") == date(1990, 3, 15)
    assert parse_flexible_date("1990-03-15") == date(1990, 3, 15)
    assert parse_flexible_date(date(1990, 3, 15)) == date(1990, 3, 15)
    assert parse_flexible_date(None) is None


def test_parse_flexible_date_rejects_invalid_values():
    with pytest.raises(ValueError):
        parse_flexible_date("31-02-1990")
    with pytest.raises(ValueError):
        parse_flexible_date("1990/03/15")
    with pytest.raises(ValueError):
        parse_flexible_date("not-a-date")
