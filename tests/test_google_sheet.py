import pytest
from fake_sheet import HEADERS, FakeSheet

from rpt_agent.services.google_sheet import (
    SheetLayoutError,
    column_letter,
    flush,
    parse_grid,
)


def test_column_letters():
    assert [column_letter(i) for i in (0, 25, 26, 27, 51, 52)] == ["A", "Z", "AA", "AB", "AZ", "BA"]


def test_columns_are_found_by_header_text_wherever_staff_put_them():
    sheet = FakeSheet(headers=list(reversed(HEADERS)))
    sheet.add(name="Asha Rao", phone="5555550101")
    grid = parse_grid(sheet.read_values(), header_row=2)
    assert grid.rows[0].get("name") == "Asha Rao" and grid.rows[0].number == 3
    assert grid.letter("lead_id") == "A"


def test_a_renamed_required_column_stops_the_sync_with_a_clear_reason():
    headers = [h if h != "Outcome Status" else "Action Status" for h in HEADERS]
    with pytest.raises(SheetLayoutError, match="Outcome Status"):
        parse_grid(FakeSheet(headers=headers).read_values(), header_row=2)


def test_blank_rows_are_ignored():
    sheet = FakeSheet()
    sheet.values.append([""] * len(HEADERS))
    sheet.add(name="Asha Rao", phone="5555550101")
    grid = parse_grid(sheet.read_values(), header_row=2)
    assert [row.number for row in grid.rows] == [4]


def test_queued_cells_are_written_in_one_call_and_unchanged_cells_are_skipped():
    sheet = FakeSheet()
    number = sheet.add(name="Asha Rao", phone="5555550101", outcome_status="Cadence started")
    grid = parse_grid(sheet.read_values(), header_row=2)
    row = grid.rows[0]
    grid.set(row, "outcome_status", "Cadence started")  # unchanged: not written
    grid.set(row, "cadence_day", "Day 1")
    grid.set(row, "call_outcome", "No answer")
    result = flush(sheet, grid)
    assert result.written == 2 and result.deferred_rows == set()
    assert len(sheet.writes) == 1
    assert sheet.cell(number, "cadence_day") == "Day 1"


def test_a_row_moved_by_staff_before_the_write_is_left_alone():
    sheet = FakeSheet()
    sheet.add(name="Asha Rao", phone="5555550101", lead_id="lead-a")
    other = sheet.add(name="Ravi Rao", phone="5555550102", lead_id="lead-b")
    grid = parse_grid(sheet.read_values(), header_row=2)
    grid.set(grid.rows[0], "call_outcome", "No answer")
    sheet.before_write = lambda: sheet.insert_row_above(3)  # staff insert a row at the top
    result = flush(sheet, grid)
    assert result.written == 0 and result.deferred_rows == {3}
    assert sheet.writes == []
    assert sheet.cell(other + 1, "call_outcome") == ""  # nobody else's row was touched


def test_a_cell_filled_in_this_poll_does_not_count_as_a_moved_row():
    sheet = FakeSheet()
    number = sheet.add(name="Asha Rao", phone="5555550101", action="Start cadence")
    grid = parse_grid(sheet.read_values(), header_row=2)
    row = grid.rows[0]
    grid.set(row, "action_request_id", "req-1")
    flush(sheet, grid)
    grid.set(row, "lead_id", "lead-a")
    assert flush(sheet, grid).written == 1
    assert sheet.cell(number, "lead_id") == "lead-a"


def test_a_column_inserted_before_the_write_stops_every_write():
    # Review 2026-10-14: column letters from the read would point at the neighbouring column.
    sheet = FakeSheet()
    number = sheet.add(name="Asha Rao", phone="5555550101", action="Start cadence")
    grid = parse_grid(sheet.read_values(), header_row=2)
    grid.set(grid.rows[0], "outcome_status", "Processing: Start cadence")
    sheet.before_write = lambda: sheet.insert_column_at(0)
    result = flush(sheet, grid)
    assert result.written == 0 and result.deferred_rows == {number} and sheet.writes == []
    assert grid.rows == []  # nothing else this poll may act on the unwritten claim


def test_a_row_with_nothing_to_recognise_it_by_is_never_written():
    sheet = FakeSheet()
    sheet.add(action="Start cadence")
    grid = parse_grid(sheet.read_values(), header_row=2)
    grid.set(grid.rows[0], "outcome_status", "Error: Name is required")
    assert flush(sheet, grid).written == 0 and sheet.writes == []


def test_a_name_typo_fixed_mid_command_does_not_block_the_result():
    sheet = FakeSheet()
    number = sheet.add(name="Asha Rao", phone="5555550101", action_request_id="req-1",
                       outcome_status="Processing: Start cadence")
    grid = parse_grid(sheet.read_values(), header_row=2)
    grid.set(grid.rows[0], "outcome_status", "Cadence started")
    sheet.before_write = lambda: sheet.set(number, "name", "Asha Rao-Kumar")
    assert flush(sheet, grid).written == 1
    assert sheet.cell(number, "outcome_status") == "Cadence started"


def test_another_row_moved_into_place_with_a_different_request_id_is_not_written():
    sheet = FakeSheet()
    number = sheet.add(name="Asha Rao", phone="5555550101", action_request_id="req-1")
    grid = parse_grid(sheet.read_values(), header_row=2)
    grid.set(grid.rows[0], "outcome_status", "Cadence started")
    sheet.before_write = lambda: sheet.set(number, "action_request_id", "req-2")
    assert flush(sheet, grid).written == 0


def test_neighbouring_rows_share_one_read_range():
    from rpt_agent.services.google_sheet import _spans

    assert _spans([3, 4, 5, 9, 11, 12]) == [(3, 5), (9, 9), (11, 12)]


def test_a_scan_reads_only_the_narrow_columns_and_loads_just_the_rows_asked_for():
    from rpt_agent.services.google_sheet import SheetGrid, load_rows, scan

    sheet = FakeSheet()
    first = sheet.add(name="Asha Rao", phone="5555550101", action="Start cadence")
    sheet.add(name="Ravi Rao", phone="5555550102", lead_id="L2", outcome_status="Booked")
    columns, rows = scan(sheet, header_row=2)
    assert [r.number for r in rows] == [3, 4] and "name" not in rows[0].cells
    assert rows[0].get("action") == "Start cadence" and rows[1].get("lead_id") == "L2"
    grid = SheetGrid(columns, [], 2)
    load_rows(sheet, grid, {first})
    assert [r.number for r in grid.rows] == [first] and grid.rows[0].get("name") == "Asha Rao"
    assert sheet.reads == 0
