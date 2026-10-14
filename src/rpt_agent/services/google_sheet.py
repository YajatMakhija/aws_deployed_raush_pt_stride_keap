"""Read and write the outreach Google Sheet directly (replaces the n8n Sheet workflows).

The Sheets API has no row ids or change events, so every poll reads the whole tab (one
request, ~200 KB for 1,000 rows) and finds columns by their header text, never by letter:
staff move columns. Rows are addressed by row number, which staff can shift at any time by
inserting, deleting or sorting rows. So no write trusts the read it came from: just before
writing, the identity cells of every target row (Lead ID, Action Request ID, Name, Phone) are
read again and any row that moved is left for the next poll. A write can never land on the
wrong patient's row.
"""

from __future__ import annotations

import string
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import Settings

# Header text -> our key. Matching ignores case and surrounding spaces.
COLUMNS = {
    "name": "Name",
    "phone": "Phone Number",
    "email": "Email",
    "date_of_birth": "Date Of Birth",
    "location": "Location",
    "case": "Case",
    "action": "Action",
    "lead_status": "Lead Status",
    "cadence_day": "Cadence Day",
    "outcome_status": "Outcome Status",
    "call_outcome": "Call Outcome",
    "message_outcome": "Message Outcome",
    "email_outcome": "Email Outcome",
    "transcript_link": "Transcript Link",
    "action_request_id": "Action Request ID",
    "action_started_at": "Action Started At",
    "lead_id": "Lead ID",
}
# Without these the sync cannot work safely; a missing optional column is just not written.
REQUIRED = ("name", "phone", "action", "outcome_status", "action_request_id",
            "action_started_at", "lead_id")
# Cells that tell whose row this is. Checked again right before every write.
IDENTITY = ("lead_id", "action_request_id", "name", "phone")


class SheetLayoutError(RuntimeError):
    """The header row is missing a column the sync needs (renamed or deleted by staff)."""


class SheetBackend(Protocol):
    def read_values(self) -> list[list[str]]: ...

    def read_columns(self, letters: list[str]) -> dict[str, list[str]]: ...

    def write_cells(self, cells: list[tuple[str, str]]) -> None: ...


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = string.ascii_uppercase[rem] + letters
    return letters


@dataclass
class SheetRow:
    number: int  # 1-based Sheet row number
    cells: dict[str, str]
    # Identity cells as the Sheet last held them (read, or confirmed by our own write). The
    # pre-write check compares against this, never against values still queued in `cells`.
    seen: tuple[str, ...] = ()

    def get(self, key: str) -> str:
        return (self.cells.get(key) or "").strip()

    def identity(self) -> tuple[str, ...]:
        return tuple(self.get(key) for key in IDENTITY)


@dataclass
class SheetGrid:
    columns: dict[str, int]  # our key -> 0-based column index
    rows: list[SheetRow]
    header_row: int
    pending: list[tuple[int, str, str, tuple[str, ...]]] = field(default_factory=list)

    def letter(self, key: str) -> str | None:
        index = self.columns.get(key)
        return column_letter(index) if index is not None else None

    def set(self, row: SheetRow, key: str, value: str | None) -> None:
        """Queue one cell write (skipped when the column does not exist or nothing changes)."""
        if key not in self.columns:
            return
        text = "" if value is None else str(value)
        if row.cells.get(key, "") == text:
            return
        self.pending.append((row.number, key, text, row.seen))
        row.cells[key] = text


def parse_grid(values: list[list[str]], header_row: int) -> SheetGrid:
    if len(values) < header_row:
        raise SheetLayoutError(f"the Sheet has no header row {header_row}")
    wanted = {name.strip().casefold(): key for key, name in COLUMNS.items()}
    columns: dict[str, int] = {}
    for index, title in enumerate(values[header_row - 1]):
        key = wanted.get(str(title).strip().casefold())
        if key and key not in columns:
            columns[key] = index
    missing = [COLUMNS[key] for key in REQUIRED if key not in columns]
    if missing:
        raise SheetLayoutError("the Sheet is missing column(s): " + ", ".join(missing))
    rows = []
    for offset, raw in enumerate(values[header_row:], start=header_row + 1):
        cells = {key: (as_text(raw[i]) if i < len(raw) else "") for key, i in columns.items()}
        if any(value.strip() for value in cells.values()):
            row = SheetRow(offset, cells)
            row.seen = row.identity()
            rows.append(row)
    return SheetGrid(columns, rows, header_row)


def as_text(value: Any) -> str:
    """Cells are read unformatted (dates as serial numbers): 15555550101.0 -> '15555550101'."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


@dataclass
class FlushResult:
    written: int = 0
    deferred_rows: set[int] = field(default_factory=set)


def flush(backend: SheetBackend, grid: SheetGrid) -> FlushResult:
    """Write queued cells whose row still holds the same patient; leave moved rows for later."""
    if not grid.pending:
        return FlushResult()
    letters = {key: grid.letter(key) for key in IDENTITY}
    used = {key: grid.letter(key) for key in {*IDENTITY, *(key for _, key, _, _ in grid.pending)}
            if grid.letter(key)}
    fresh = backend.read_columns(sorted(set(used.values())))
    cells, deferred = [], set()
    # A column inserted, deleted or moved since the read shifts every letter: write nothing.
    moved = any(
        as_text(_cell(fresh.get(letter, []), grid.header_row)).strip().casefold()
        != COLUMNS[key].casefold()
        for key, letter in used.items()
    )
    for number, key, text, expected in grid.pending:
        if moved:
            deferred.add(number)
            continue
        now = tuple(
            as_text(_cell(fresh.get(letters[k] or "", []), number)).strip() if letters[k] else ""
            for k in IDENTITY
        )
        if not _same_row(expected, now):
            deferred.add(number)
            continue
        cells.append((f"{grid.letter(key)}{number}", text))
    if cells:
        backend.write_cells(cells)
    written = {number for number, *_ in grid.pending} - deferred
    for row in grid.rows:
        if row.number in written:
            row.seen = row.identity()  # the Sheet now holds what we queued
    # A deferred row's cells hold values the Sheet never got (a claim, a new request id):
    # nothing else this poll may act on them. The next poll reads it afresh.
    grid.rows = [row for row in grid.rows if row.number not in deferred]
    grid.pending.clear()
    return FlushResult(len(cells), deferred)


def _cell(column: list[Any], number: int) -> Any:
    return column[number - 1] if 0 < number <= len(column) else ""


def _same_row(expected: tuple[str, ...], now: tuple[str, ...]) -> bool:
    """Is the row at this number still the one we read? (IDENTITY order: lead, request, name, phone)

    An Action Request ID is unique, so it decides alone; staff may fix a Name or Phone typo while
    a command runs. Next a Lead ID. A row with neither must still show the same Name and Phone and
    no ids, and a row with nothing to recognise it by is never written.
    """
    lead, request, name, phone = expected
    now_lead, now_request, now_name, now_phone = now
    if request:
        return now_request == request
    if lead:
        return now_lead == lead
    if not (name or phone):
        return False
    return (now_lead, now_request, now_name, now_phone) == ("", "", name, phone)


# Unformatted: a date cell comes back as its serial number, never as text in the Sheet's
# locale (where 3/4/1980 could mean either month); text cells come back as typed.
_READ = {"value_render_option": "UNFORMATTED_VALUE", "date_time_render_option": "SERIAL_NUMBER"}


class GoogleSheetBackend:
    """gspread over the service account; one worksheet."""

    def __init__(self, settings: Settings):
        import gspread  # local: only the sheet-worker needs it

        client = gspread.service_account(filename=settings.google_sheet_credentials_file)
        self.worksheet = client.open_by_key(settings.google_sheet_id).worksheet(
            settings.google_sheet_tab
        )

    def read_values(self) -> list[list[Any]]:
        return self.worksheet.get_all_values(**_READ)

    def read_columns(self, letters: list[str]) -> dict[str, list[Any]]:
        ranges = [f"{letter}:{letter}" for letter in letters]
        result: list[Any] = self.worksheet.batch_get(ranges, major_dimension="COLUMNS", **_READ)
        out = {}
        for letter, value_range in zip(letters, result, strict=True):
            out[letter] = list(value_range[0]) if value_range else []
        return out

    def write_cells(self, cells: list[tuple[str, str]]) -> None:
        # RAW keeps text exactly as written (no phone-number or date re-interpretation).
        self.worksheet.batch_update(
            [{"range": cell, "values": [[value]]} for cell, value in cells],
            value_input_option="RAW",
        )
