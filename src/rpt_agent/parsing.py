"""Shared input parsing helpers for sheet and API payloads."""

from __future__ import annotations

from datetime import date, datetime


def parse_flexible_date(value: date | datetime | str | None) -> date | None:
    """Accept ISO YYYY-MM-DD or day-first DD-MM-YYYY / DD/MM/YYYY dates."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text:
        return None

    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return date.fromisoformat(text)

    for separator in ("-", "/"):
        parts = text.split(separator)
        if len(parts) != 3:
            continue
        if not all(part.isdigit() for part in parts):
            continue
        day_s, month_s, year_s = parts
        if len(day_s) > 2 or len(month_s) > 2 or len(year_s) != 4:
            continue
        try:
            return date(int(year_s), int(month_s), int(day_s))
        except ValueError as exc:
            raise ValueError("date must be a real calendar day in DD-MM-YYYY or YYYY-MM-DD") from exc

    raise ValueError("date must be DD-MM-YYYY, DD/MM/YYYY, or YYYY-MM-DD")
