from __future__ import annotations

import csv

import pytest

from pab_pricer.pricer import (
    aggregate_rows,
    merge_unpriced_duplicates,
    price_rows,
    read_brick_rows,
    write_aggregate_csv,
    write_priced_csv,
)

from tests.conftest import FIXTURES_DIR, fake_fetch_part_siblings


def test_read_brick_rows_loads_valid_lines():
    rows = read_brick_rows(FIXTURES_DIR / "simple.csv")
    assert [r["BLItemNo"] for r in rows] == ["3005", "3023", "3867"]


def test_read_brick_rows_skips_blank_and_malformed_rows(tmp_path):
    csv_path = tmp_path / "messy.csv"
    csv_path.write_text(
        "BLItemNo,ElementId,PartName,Qty\n"
        "3005,4211389,Brick 1 x 1,4\n"
        ",,,\n"                        # fully blank row
        "3023,,Plate 1 x 2,6\n"        # missing ElementId
        "3024,notanumber,Plate 1 x 1,8\n"  # non-numeric ElementId
        "TOTAL,,,18\n",                # summary row missing ElementId
        encoding="utf-8",
    )
    rows = read_brick_rows(csv_path)
    assert len(rows) == 1
    assert rows[0]["BLItemNo"] == "3005"


def test_price_rows_prices_known_parts_and_flags_unknown_ones():
    rows = read_brick_rows(FIXTURES_DIR / "simple.csv")
    priced = price_rows(rows, fetcher=fake_fetch_part_siblings, delay=0)

    by_part = {r["BLItemNo"]: r for r in priced}
    assert by_part["3005"]["Availability"] == "AVAILABLE"
    assert by_part["3005"]["UnitPriceGBP"] == "0.06"
    assert by_part["3005"]["LineTotalGBP"] == "0.24"  # 4 * 0.06

    assert by_part["3867"]["Availability"] == "NOT_FOUND_ON_PAB"
    assert by_part["3867"]["UnitPriceGBP"] == ""
    assert by_part["3867"]["LineTotalGBP"] == ""


def test_price_rows_default_fetcher_can_be_monkeypatched(patch_fetcher):
    """Guards the exact mechanism webapp.main relies on: callers that don't
    pass `fetcher` explicitly must still pick up a monkeypatched
    fetch_part_siblings, because price_rows() resolves it at call time."""
    rows = read_brick_rows(FIXTURES_DIR / "simple.csv")
    priced = price_rows(rows, delay=0)
    assert any(r["Availability"] == "AVAILABLE" for r in priced)


def test_merge_unpriced_duplicates_combines_same_missing_piece():
    rows = [
        {"BLItemNo": "3867", "ElementId": "4251286", "Qty": "2", "Availability": "NOT_FOUND_ON_PAB"},
        {"BLItemNo": "3867", "ElementId": "4251286", "Qty": "5", "Availability": "NOT_FOUND_ON_PAB"},
        {"BLItemNo": "3005", "ElementId": "4211389", "Qty": "4", "Availability": "AVAILABLE"},
    ]
    merged = merge_unpriced_duplicates(rows)

    not_found = [r for r in merged if r["Availability"] == "NOT_FOUND_ON_PAB"]
    assert len(not_found) == 1
    assert not_found[0]["Qty"] == "7"
    # Found rows pass through untouched, including duplicates of the same part.
    assert len([r for r in merged if r["Availability"] == "AVAILABLE"]) == 1


def test_merge_unpriced_duplicates_keeps_distinct_pieces_separate():
    rows = [
        {"BLItemNo": "3867", "ElementId": "4251286", "Qty": "1", "Availability": "NOT_FOUND_ON_PAB"},
        {"BLItemNo": "9999", "ElementId": "1234567", "Qty": "3", "Availability": "NOT_FOUND_ON_PAB"},
    ]
    merged = merge_unpriced_duplicates(rows)
    assert len(merged) == 2


def test_aggregate_rows_sums_quantity_and_line_total_per_piece():
    rows = [
        {"BLItemNo": "3005", "ElementId": "4211389", "PartName": "Brick 1 x 1", "ColorName": "Grey",
         "Qty": "4", "UnitPriceGBP": "0.06", "LineTotalGBP": "0.24"},
        {"BLItemNo": "3005", "ElementId": "4211389", "PartName": "Brick 1 x 1", "ColorName": "Grey",
         "Qty": "20", "UnitPriceGBP": "0.06", "LineTotalGBP": "1.20"},
        {"BLItemNo": "3023", "ElementId": "302326", "PartName": "Plate 1 x 2", "ColorName": "Black",
         "Qty": "6", "UnitPriceGBP": "0.07", "LineTotalGBP": "0.42"},
    ]
    aggregated = aggregate_rows(rows)

    assert len(aggregated) == 2
    brick = next(r for r in aggregated if r["BLItemNo"] == "3005")
    assert brick["Qty"] == "24"
    assert brick["LineTotalGBP"] == "1.44"


def test_write_priced_csv_includes_totals_and_not_found_count(tmp_path):
    rows = [
        {"BLItemNo": "3005", "Qty": "4", "LineTotalGBP": "0.24", "Availability": "AVAILABLE"},
        {"BLItemNo": "3867", "Qty": "1", "LineTotalGBP": "", "Availability": "NOT_FOUND_ON_PAB"},
    ]
    out_path = tmp_path / "priced.csv"
    write_priced_csv(rows, out_path)

    with out_path.open(newline="", encoding="utf-8") as f:
        lines = list(csv.reader(f))

    total_row = next(r for r in lines if r and r[0] == "TOTAL")
    not_found_row = next(r for r in lines if r and r[0] == "ITEMS NOT FOUND ON PAB")
    assert total_row[lines[0].index("Qty")] == "5"
    assert total_row[lines[0].index("LineTotalGBP")] == "0.24"
    assert not_found_row[lines[0].index("Qty")] == "1"


def test_write_priced_csv_handles_rows_with_different_columns(tmp_path):
    """Rows merged from different source CSVs may not share every column;
    the writer must union fieldnames rather than assume the first row's
    keys cover every row (regression: a naive DictWriter raises here)."""
    rows = [
        {"BLItemNo": "3005", "Qty": "4", "LineTotalGBP": "0.24", "Availability": "AVAILABLE"},
        {"BLItemNo": "3023", "Qty": "6", "LineTotalGBP": "0.42", "Availability": "AVAILABLE", "Notes": "extra column"},
    ]
    out_path = tmp_path / "priced.csv"
    write_priced_csv(rows, out_path)
    assert "Notes" in out_path.read_text(encoding="utf-8")


def test_write_aggregate_csv_writes_expected_columns_and_totals(tmp_path):
    rows = [
        {"BLItemNo": "3005", "PartName": "Brick 1 x 1", "ColorName": "Grey", "ElementId": "4211389",
         "Qty": "4", "UnitPriceGBP": "0.06", "LineTotalGBP": "0.24"},
    ]
    out_path = tmp_path / "aggregate.csv"
    write_aggregate_csv(rows, out_path)

    content = out_path.read_text(encoding="utf-8")
    assert content.splitlines()[0] == "BLItemNo,ElementId,PartName,ColorName,Qty,UnitPriceGBP,LineTotalGBP"
    assert "TOTAL" in content


def test_aggregate_csv_round_trips_through_read_brick_rows(tmp_path):
    """The simple/aggregate download must be re-uploadable: it needs ElementId
    (read_brick_rows requires it) and its trailing blank/TOTAL rows must be
    skipped rather than misread as part rows."""
    rows = [
        {"BLItemNo": "3005", "ElementId": "4211389", "PartName": "Brick 1 x 1", "ColorName": "Grey",
         "Qty": "4", "UnitPriceGBP": "0.06", "LineTotalGBP": "0.24"},
        {"BLItemNo": "3023", "ElementId": "302326", "PartName": "Plate 1 x 2", "ColorName": "Black",
         "Qty": "6", "UnitPriceGBP": "0.07", "LineTotalGBP": "0.42"},
    ]
    out_path = tmp_path / "aggregate.csv"
    write_aggregate_csv(rows, out_path)

    reread = read_brick_rows(out_path)

    assert len(reread) == 2
    assert {r["BLItemNo"] for r in reread} == {"3005", "3023"}
    assert next(r for r in reread if r["BLItemNo"] == "3005")["ElementId"] == "4211389"


def test_priced_csv_round_trips_through_read_brick_rows(tmp_path):
    """The detailed download's extra columns (UnitPriceGBP/LineTotalGBP/
    Availability) and trailing TOTAL/not-found summary rows must not confuse
    a re-upload of the file."""
    rows = [
        {"BLItemNo": "3005", "ElementId": "4211389", "PartName": "Brick 1 x 1", "Qty": "4",
         "UnitPriceGBP": "0.06", "LineTotalGBP": "0.24", "Availability": "AVAILABLE"},
        {"BLItemNo": "3867", "ElementId": "4251286", "PartName": "Baseplate 16 x 16", "Qty": "1",
         "UnitPriceGBP": "", "LineTotalGBP": "", "Availability": "NOT_FOUND_ON_PAB"},
    ]
    out_path = tmp_path / "priced.csv"
    write_priced_csv(rows, out_path)

    reread = read_brick_rows(out_path)

    assert len(reread) == 2
    assert {r["BLItemNo"] for r in reread} == {"3005", "3867"}
