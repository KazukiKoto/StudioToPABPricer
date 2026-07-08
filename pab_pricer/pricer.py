"""Price LEGO parts from a CSV against the official LEGO Pick a Brick website."""

from __future__ import annotations

import csv
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PAB_URL = "https://www.lego.com/{locale}/pick-and-build/pick-a-brick"
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REQUIRED_COLUMNS = ("BLItemNo", "ElementId", "PartName", "Qty")

# LEGO's site sits behind Cloudflare bot management, which fingerprints and
# blocks the TLS/HTTP client used by Python's `requests`/urllib3 while
# allowing plain `curl`. Shelling out to curl (present on Windows/macOS/Linux)
# avoids that block without needing browser-impersonation libraries.


def _curl_get(url: str, params: dict[str, str], timeout: float) -> tuple[int, str]:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    full_url = f"{url}?{query}" if query else url
    result = subprocess.run(
        [
            "curl",
            "-s",
            "-A",
            USER_AGENT,
            "-w",
            "\n%{http_code}",
            "--max-time",
            str(timeout),
            full_url,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise PabPriceFetchError(
            f"curl failed for {full_url!r} (exit {result.returncode}): {result.stderr.strip()}"
        )
    body, _, status_code = result.stdout.rpartition("\n")
    return int(status_code), body


@dataclass
class ElementPrice:
    element_id: str
    unit_price_gbp: Optional[float]
    availability: str


class PabPriceFetchError(RuntimeError):
    pass


def _resolve_ref(apollo_state: dict, node: dict) -> dict:
    """Resolve an Apollo Client normalized-cache reference to its target object."""
    if isinstance(node, dict) and node.get("type") == "id":
        return apollo_state.get(node["id"], {})
    return node


def _extract_siblings(apollo_state: dict) -> dict[str, ElementPrice]:
    siblings: dict[str, ElementPrice] = {}
    for key, value in apollo_state.items():
        if not key.startswith("Sibling:"):
            continue
        element_id = value.get("id") or key.split(":", 1)[1]
        price_node = _resolve_ref(apollo_state, value.get("price", {}))
        unit_price = price_node.get("formattedValue")
        availability = value.get("availability", "UNKNOWN")
        siblings[str(element_id)] = ElementPrice(
            element_id=str(element_id),
            unit_price_gbp=unit_price,
            availability=availability,
        )
    return siblings


def fetch_part_siblings(
    part_number: str,
    locale: str = "en-gb",
    timeout: float = 15.0,
    retries: int = 3,
) -> dict[str, ElementPrice]:
    """Query the LEGO Pick a Brick search page for a part number and return a map
    of elementId -> ElementPrice for every colour/element variant it lists."""
    url = PAB_URL.format(locale=locale)
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            status_code, body = _curl_get(url, {"query": part_number}, timeout)
            if status_code != 200:
                raise PabPriceFetchError(
                    f"HTTP {status_code} fetching part {part_number!r}"
                )
            match = NEXT_DATA_RE.search(body)
            if not match:
                raise PabPriceFetchError(
                    f"Could not find page data for part {part_number!r}"
                )
            data = json.loads(match.group(1))
            apollo_state = data["props"]["pageProps"]["__APOLLO_STATE__"]
            return _extract_siblings(apollo_state)
        except (PabPriceFetchError, KeyError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1.0 * attempt)
    raise PabPriceFetchError(
        f"Failed to fetch pricing for part {part_number!r} after {retries} attempts"
    ) from last_error


def read_brick_rows(csv_path: Path) -> list[dict[str, str]]:
    """Read the input CSV, skipping blank/summary rows that aren't real part lines."""
    rows: list[dict[str, str]] = []
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not all(row.get(col) for col in REQUIRED_COLUMNS):
                continue
            if not row["ElementId"].strip().isdigit():
                continue
            rows.append(row)
    return rows


def price_rows(
    rows: list[dict[str, str]],
    locale: str = "en-gb",
    delay: float = 0.5,
    progress_callback=None,
    fetcher=None,
) -> list[dict[str, str]]:
    """Fetch prices for each row's ElementId, grouping requests by part number so
    each distinct LEGO part is only looked up once.

    `fetcher` defaults to `fetch_part_siblings` (looked up at call time, not bound
    at import time) so tests can monkeypatch the module-level function without
    needing every caller to pass one explicitly.
    """
    fetch = fetcher or fetch_part_siblings
    cache: dict[str, dict[str, ElementPrice]] = {}
    priced_rows: list[dict[str, str]] = []

    part_numbers = sorted({row["BLItemNo"] for row in rows})
    for i, part_number in enumerate(part_numbers):
        try:
            cache[part_number] = fetch(part_number, locale=locale)
        except PabPriceFetchError:
            cache[part_number] = {}
        if progress_callback:
            progress_callback(i + 1, len(part_numbers), part_number)
        if delay and i < len(part_numbers) - 1:
            time.sleep(delay)

    for row in rows:
        siblings = cache.get(row["BLItemNo"], {})
        element = siblings.get(row["ElementId"])
        qty = int(row["Qty"])
        out_row = dict(row)
        if element and element.unit_price_gbp is not None:
            out_row["UnitPriceGBP"] = f"{element.unit_price_gbp:.2f}"
            out_row["LineTotalGBP"] = f"{element.unit_price_gbp * qty:.2f}"
            out_row["Availability"] = element.availability
        else:
            out_row["UnitPriceGBP"] = ""
            out_row["LineTotalGBP"] = ""
            out_row["Availability"] = "NOT_FOUND_ON_PAB"
        priced_rows.append(out_row)

    return priced_rows


def merge_duplicate_parts(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Combine rows for the same piece (BLItemNo + ElementId) into a single row
    with the combined quantity, so the same piece isn't listed once per source
    CSV it appeared in -- regardless of whether it was found on PAB.

    Availability/UnitPriceGBP don't need reconciling across a group: price_rows
    fetches each part number once and applies the identical result to every row
    referencing it, so every row sharing a key already agrees on those fields
    before this runs. Only Qty (and the LineTotalGBP derived from it) actually
    need combining.
    """
    merged: list[dict[str, str]] = []
    groups: dict[tuple[str, str], dict[str, str]] = {}

    for row in rows:
        key = (row["BLItemNo"], row["ElementId"])
        if key in groups:
            existing = groups[key]
            qty = int(existing["Qty"]) + int(row["Qty"])
            existing["Qty"] = str(qty)
            if existing.get("UnitPriceGBP"):
                existing["LineTotalGBP"] = f"{float(existing['UnitPriceGBP']) * qty:.2f}"
        else:
            new_row = dict(row)
            groups[key] = new_row
            merged.append(new_row)

    return merged


def aggregate_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Roll every row up by unique piece (BLItemNo + ElementId), summing quantity
    and line total, for a compact per-part summary CSV.

    ElementId is included in the output (not just used as a grouping key) so
    this CSV can be dropped back into the upload form later and re-priced --
    read_brick_rows() requires it, so without it a re-uploaded aggregate CSV
    would silently match zero rows.
    """
    order: list[tuple[str, str]] = []
    groups: dict[tuple[str, str], dict[str, str]] = {}

    for row in rows:
        key = (row["BLItemNo"], row["ElementId"])
        if key not in groups:
            order.append(key)
            groups[key] = {
                "BLItemNo": row["BLItemNo"],
                "ElementId": row["ElementId"],
                "PartName": row["PartName"],
                "ColorName": row.get("ColorName", ""),
                "Qty": 0,
                "UnitPriceGBP": row["UnitPriceGBP"],
                "LineTotalGBP": 0.0,
            }
        group = groups[key]
        group["Qty"] += int(row["Qty"])
        group["LineTotalGBP"] += float(row["LineTotalGBP"]) if row["LineTotalGBP"] else 0.0
        if not group["UnitPriceGBP"] and row["UnitPriceGBP"]:
            group["UnitPriceGBP"] = row["UnitPriceGBP"]

    aggregated = []
    for key in order:
        group = groups[key]
        aggregated.append(
            {
                "BLItemNo": group["BLItemNo"],
                "ElementId": group["ElementId"],
                "PartName": group["PartName"],
                "ColorName": group["ColorName"],
                "Qty": str(group["Qty"]),
                "UnitPriceGBP": group["UnitPriceGBP"],
                "LineTotalGBP": f"{group['LineTotalGBP']:.2f}" if group["LineTotalGBP"] else "",
            }
        )
    return aggregated


def write_aggregate_csv(rows: list[dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    aggregated = aggregate_rows(rows)
    fieldnames = ["BLItemNo", "ElementId", "PartName", "ColorName", "Qty", "UnitPriceGBP", "LineTotalGBP"]

    total_qty = sum(int(r["Qty"]) for r in aggregated)
    total_cost = sum(float(r["LineTotalGBP"]) for r in aggregated if r["LineTotalGBP"])

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(aggregated)
        writer.writerow({})
        writer.writerow({"BLItemNo": "TOTAL", "Qty": total_qty, "LineTotalGBP": f"{total_cost:.2f}"})


def write_priced_csv(priced_rows: list[dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in priced_rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)

    total_qty = sum(int(r["Qty"]) for r in priced_rows)
    total_cost = sum(float(r["LineTotalGBP"]) for r in priced_rows if r["LineTotalGBP"])
    not_found = sum(1 for r in priced_rows if r["Availability"] == "NOT_FOUND_ON_PAB")

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(priced_rows)
        writer.writerow({})
        writer.writerow(
            {
                fieldnames[0]: "TOTAL",
                "Qty": total_qty,
                "LineTotalGBP": f"{total_cost:.2f}",
            }
        )
        writer.writerow(
            {
                fieldnames[0]: "ITEMS NOT FOUND ON PAB",
                "Qty": not_found,
            }
        )
