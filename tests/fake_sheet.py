"""In-memory stand-in for the outreach Google Sheet (GoogleSheetBackend's three calls)."""

from __future__ import annotations

import re

from rpt_agent.services.google_sheet import COLUMNS

HEADERS = [
    "Name", "Phone Number", "Email", "Date Of Birth", "Location", "Case", "Action", "Lead Status",
    "Cadence Day", "Outcome Status", "Call Outcome", "Message Outcome", "Email Outcome",
    "Transcript Link", "Action Request ID", "Action Started At", "Lead ID",
]
_KEY_BY_HEADER = {name: key for key, name in COLUMNS.items()}


def _index(letter: str) -> int:
    value = 0
    for char in letter:
        value = value * 26 + (ord(char) - 64)
    return value - 1


class FakeSheet:
    def __init__(self, headers: list[str] | None = None):
        self.values: list[list[str]] = [
            ["FOR FRONT DESK USE", "", "", "", "", "", "FOR KEAP", "", "", "", "", "", "",
             "ADDITIONAL INFORMATION"],
            list(headers or HEADERS),
        ]
        self.writes: list[list[tuple[str, str]]] = []
        self.reads = 0  # full-tab reads
        self.row_reads = 0
        self.archive: list[list[str]] | None = None  # the "Archive" tab once created
        self.before_write = None  # hook: lets a test move rows between the read and the write

    # --- test helpers ---
    def add(self, **cells: str) -> int:
        header = self.values[1]
        row = [""] * len(header)
        for key, value in cells.items():
            row[header.index(COLUMNS[key])] = value
        self.values.append(row)
        return len(self.values)

    def cell(self, number: int, key: str) -> str:
        header = self.values[1]
        row = self.values[number - 1]
        index = header.index(COLUMNS[key])
        return row[index] if index < len(row) else ""

    def set(self, number: int, key: str, value: str) -> None:
        header = self.values[1]
        row = self.values[number - 1]
        index = header.index(COLUMNS[key])
        row.extend([""] * (index + 1 - len(row)))
        row[index] = value

    def insert_column_at(self, index: int) -> None:
        for row in self.values:
            row.insert(index, "")

    def insert_row_above(self, number: int) -> None:
        self.values.insert(number - 1, [""] * len(self.values[1]))

    # --- backend protocol ---
    def read_values(self) -> list[list[str]]:
        self.reads += 1
        return [list(row) for row in self.values]

    def read_rows(self, numbers: list[int]) -> dict[int, list[str]]:
        # The pre-write check reads rows: a test's "staff moved something" hook fires here.
        if self.before_write:
            hook, self.before_write = self.before_write, None
            hook()
        self.row_reads += 1
        return {n: list(self.values[n - 1]) if n <= len(self.values) else [] for n in numbers}

    def read_columns(self, letters: list[str]) -> dict[str, list[str]]:
        out = {}
        for letter in letters:
            i = _index(letter)
            out[letter] = [row[i] if i < len(row) else "" for row in self.values]
        return out

    def archive_rows(self, numbers: list[int], header_row: int, archive_tab: str) -> None:
        if self.archive is None:
            self.archive = [list(row) for row in self.values[:header_row]]
        moved = [list(self.values[n - 1]) for n in sorted(numbers)]
        self.archive[header_row:header_row] = moved
        for n in sorted(numbers, reverse=True):
            del self.values[n - 1]

    def write_cells(self, cells: list[tuple[str, str]]) -> None:
        self.writes.append(list(cells))
        for ref, value in cells:
            match = re.fullmatch(r"([A-Z]+)(\d+)", ref)
            assert match, ref
            col, number = _index(match.group(1)), int(match.group(2))
            while len(self.values) < number:
                self.values.append([""] * len(self.values[1]))
            row = self.values[number - 1]
            row.extend([""] * (col + 1 - len(row)))
            row[col] = value
