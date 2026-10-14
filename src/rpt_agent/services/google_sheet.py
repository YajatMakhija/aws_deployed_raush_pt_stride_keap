"""Read and write the outreach Google Sheet directly (replaces the n8n Sheet workflows).

The Sheets API has no row ids or change events. Columns are found by their header text, never
by letter (staff move columns). Most polls only `scan` five narrow columns (Action, Outcome
Status, Lead ID, Action Request ID, Action Started At: ~2 MB even at 200,000 rows) and then
`load_rows` the few rows that need work; a full read of the tab is for the slower profile
check and the nightly archive.

Rows are addressed by row number, which staff can shift at any time by inserting, deleting or
sorting rows. So no write trusts the read it came from: just before writing, the target rows
and the header row are read again and any row that moved (or a moved column) is left for the
next poll. A write can never land on the wrong patient's row.
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
# Enough to decide which rows need work this poll (commands, stuck rows, results to write).
SCAN = ("action", "outcome_status", "lead_id", "action_request_id", "action_started_at")
# Rows per batchGet request (ranges go in the URL).
READ_CHUNK = 100


class SheetLayoutError(RuntimeError):
    """The header row is missing a column the sync needs (renamed or deleted by staff)."""


class SheetBackend(Protocol):
    def read_values(self) -> list[list[Any]]: ...

    def read_rows(self, numbers: list[int]) -> dict[int, list[Any]]: ...

    def read_columns(self, letters: list[str]) -> dict[str, list[Any]]: ...

    def write_cells(self, cells: list[tuple[str, str]]) -> None: ...

    def archive_rows(self, numbers: list[int], header_row: int, archive_tab: str) -> None: ...


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


def header_columns(header: list[Any]) -> dict[str, int]:
    wanted = {name.strip().casefold(): key for key, name in COLUMNS.items()}
    columns: dict[str, int] = {}
    for index, title in enumerate(header):
        key = wanted.get(as_text(title).strip().casefold())
        if key and key not in columns:
            columns[key] = index
    missing = [COLUMNS[key] for key in REQUIRED if key not in columns]
    if missing:
        raise SheetLayoutError("the Sheet is missing column(s): " + ", ".join(missing))
    return columns


def _row(number: int, raw: list[Any], columns: dict[str, int]) -> SheetRow | None:
    cells = {key: (as_text(raw[i]) if i < len(raw) else "") for key, i in columns.items()}
    if not any(value.strip() for value in cells.values()):
        return None
    row = SheetRow(number, cells)
    row.seen = row.identity()
    return row


def parse_grid(values: list[list[Any]], header_row: int) -> SheetGrid:
    """The whole tab (full read): every non-blank row."""
    if len(values) < header_row:
        raise SheetLayoutError(f"the Sheet has no header row {header_row}")
    columns = header_columns(values[header_row - 1])
    rows = [row for number, raw in enumerate(values[header_row:], start=header_row + 1)
            if (row := _row(number, raw, columns))]
    return SheetGrid(columns, rows, header_row)


def scan(backend: SheetBackend, header_row: int) -> tuple[dict[str, int], list[SheetRow]]:
    """Header + the five SCAN columns of every row. Rows carry only those cells."""
    header = backend.read_rows([header_row]).get(header_row) or []
    columns = header_columns(header)
    letters = {key: column_letter(columns[key]) for key in SCAN}
    fresh = backend.read_columns(sorted(set(letters.values())))
    length = max((len(values) for values in fresh.values()), default=0)
    rows = []
    for number in range(header_row + 1, length + 1):
        cells = {key: as_text(_cell(fresh.get(letter, []), number)) for key, letter in letters.items()}
        if any(value.strip() for value in cells.values()):
            rows.append(SheetRow(number, cells))
    return columns, rows


def load_rows(backend: SheetBackend, grid: SheetGrid, numbers: set[int]) -> None:
    """Add the full content of these rows to the grid (rows already in it are kept as they are)."""
    have = {row.number for row in grid.rows}
    wanted = sorted(set(numbers) - have)
    if not wanted:
        return
    fetched = backend.read_rows(wanted)
    for number in wanted:
        if row := _row(number, fetched.get(number) or [], grid.columns):
            grid.rows.append(row)
    grid.rows.sort(key=lambda row: row.number)


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
    used = {key for _, key, _, _ in grid.pending} | set(IDENTITY)
    fresh = backend.read_rows(sorted({grid.header_row, *(n for n, *_ in grid.pending)}))
    header = fresh.get(grid.header_row) or []
    cells, deferred = [], set()
    # A column inserted, deleted or moved since the read shifts every letter: write nothing.
    moved = any(
        as_text(_cell(header, grid.columns[key] + 1)).strip().casefold() != COLUMNS[key].casefold()
        for key in used if key in grid.columns
    )
    for number, key, text, expected in grid.pending:
        if moved:
            deferred.add(number)
            continue
        raw = fresh.get(number) or []
        now = tuple(
            as_text(_cell(raw, grid.columns[k] + 1)).strip() if k in grid.columns else ""
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

    def read_rows(self, numbers: list[int]) -> dict[int, list[Any]]:
        out: dict[int, list[Any]] = {}
        spans = _spans(sorted(set(numbers)))
        for start in range(0, len(spans), READ_CHUNK):
            chunk = spans[start:start + READ_CHUNK]
            ranges = [f"{first}:{last}" for first, last in chunk]
            result: list[Any] = self.worksheet.batch_get(ranges, major_dimension="ROWS", **_READ)
            for (first, last), value_range in zip(chunk, result, strict=True):
                values = list(value_range or [])
                for offset, number in enumerate(range(first, last + 1)):
                    out[number] = list(values[offset]) if offset < len(values) else []
        return out

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

    def archive_rows(self, numbers: list[int], header_row: int, archive_tab: str) -> None:
        """Move whole rows (values and formatting) to the top of the archive tab, in ONE request.

        A spreadsheet batchUpdate is all-or-nothing, so a row is never deleted without its copy.
        """
        import gspread

        book, main = self.worksheet.spreadsheet, self.worksheet
        width = main.col_count
        try:
            archive = book.worksheet(archive_tab)
        except gspread.WorksheetNotFound:
            archive = book.add_worksheet(archive_tab, rows=header_row + 1, cols=width)
            book.batch_update({"requests": [_copy(main.id, archive.id, 0, 0, header_row, width)]})
        requests: list[dict[str, Any]] = []
        if archive.col_count < width:
            requests.append({"appendDimension": {"sheetId": archive.id, "dimension": "COLUMNS",
                                                 "length": width - archive.col_count}})
        spans = _spans(sorted(set(numbers)))  # neighbouring rows move as one block
        requests.append({"insertDimension": {"range": {
            "sheetId": archive.id, "dimension": "ROWS", "startIndex": header_row,
            "endIndex": header_row + sum(last - first + 1 for first, last in spans)},
            "inheritFromBefore": False}})
        offset = header_row
        for first, last in spans:
            requests.append(_copy(main.id, archive.id, first - 1, offset, last - first + 1, width))
            offset += last - first + 1
        for first, last in reversed(spans):  # bottom up, so earlier row numbers stay valid
            requests.append({"deleteDimension": {"range": {
                "sheetId": main.id, "dimension": "ROWS", "startIndex": first - 1,
                "endIndex": last}}})
        book.batch_update({"requests": requests})


def _copy(source: int, target: int, source_row: int, target_row: int, rows: int,
          width: int) -> dict[str, Any]:
    def grid(sheet: int, first: int) -> dict[str, int]:
        return {"sheetId": sheet, "startRowIndex": first, "endRowIndex": first + rows,
                "startColumnIndex": 0, "endColumnIndex": width}

    return {"copyPaste": {"source": grid(source, source_row), "destination": grid(target, target_row),
                          "pasteType": "PASTE_NORMAL"}}


def _spans(numbers: list[int]) -> list[tuple[int, int]]:
    """[3, 4, 5, 9] -> [(3, 5), (9, 9)]: neighbouring rows share one range."""
    spans: list[tuple[int, int]] = []
    for number in numbers:
        if spans and number == spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], number)
        else:
            spans.append((number, number))
    return spans
