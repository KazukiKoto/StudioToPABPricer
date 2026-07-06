"""FastAPI web UI for pricing a bricks CSV against LEGO Pick a Brick.

Run with: make web  (or: uvicorn webapp.main:app --reload)
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pab_pricer.pricer import (
    merge_unpriced_duplicates,
    price_rows,
    read_brick_rows,
    write_aggregate_csv,
    write_priced_csv,
)

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
OUTPUTS_DIR = REPO_ROOT / "outputs"
LOCALE = "en-gb"  # pins pricing to GBP

# LEGO/BrickLink CSV exports are served under a handful of MIME types
# depending on OS/browser; anything else is almost certainly not a CSV.
ALLOWED_CSV_CONTENT_TYPES = {
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
    "text/plain",
    "application/octet-stream",
    "",
}


def _csv_validation_error(filename: str, content_type: str | None, contents: bytes) -> str | None:
    """Return a human-readable reason the upload isn't a usable CSV, or None if it's fine."""
    if not filename.lower().endswith(".csv"):
        return "not a .csv file"
    if content_type and content_type.lower() not in ALLOWED_CSV_CONTENT_TYPES:
        return f"unexpected file type ({content_type})"
    if b"\x00" in contents[:4096]:
        return "does not look like a text CSV file"
    return None

app = FastAPI(title="LEGO Pick a Brick Pricer")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# In-memory per-upload state. Fine for a single-user local tool; not
# multi-process safe and not persisted across restarts.
SESSIONS: dict[str, dict] = {}

NOT_FOUND_STATUSES = {"NOT_FOUND_ON_PAB"}


def _summary(rows: list[dict]) -> dict:
    not_found = [r for r in rows if r["Availability"] in NOT_FOUND_STATUSES]
    manual = [r for r in rows if r["Availability"] == "MANUAL"]
    found = [r for r in rows if r["Availability"] not in NOT_FOUND_STATUSES and r["Availability"] != "MANUAL"]
    total_cost = sum(float(r["LineTotalGBP"]) for r in rows if r["LineTotalGBP"])
    total_qty = sum(int(r["Qty"]) for r in rows)
    return {
        "found": found,
        "not_found": not_found,
        "total_cost": total_cost,
        "total_qty": total_qty,
        "found_count": len(found),
        "not_found_count": len(not_found),
        "manual_count": len(manual),
    }


def _render_results(request: Request, token: str, message: str | None = None) -> HTMLResponse:
    session = SESSIONS[token]
    rows = session["rows"]
    indexed_rows = list(enumerate(rows))
    return templates.TemplateResponse(
        request=request,
        name="results.html",
        context={
            "token": token,
            "source_name": session["source_name"],
            "rows": indexed_rows,
            "not_found_statuses": NOT_FOUND_STATUSES,
            "message": message,
            **_summary(rows),
        },
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.post("/upload", response_class=HTMLResponse)
async def upload(
    request: Request,
    files: list[UploadFile] = File(...),
    multipliers: list[str] = Form(...),
):
    errors: list[str] = []
    all_rows: list[dict[str, str]] = []
    source_names: list[str] = []

    for file, raw_multiplier in zip(files, multipliers):
        name = file.filename or "file"

        try:
            multiplier = int(raw_multiplier)
            if multiplier < 1:
                raise ValueError
        except ValueError:
            errors.append(f"{name}: invalid copy count {raw_multiplier!r}, skipped.")
            continue

        contents = await file.read()
        invalid_reason = _csv_validation_error(name, file.content_type, contents)
        if invalid_reason:
            errors.append(f"{name}: {invalid_reason}, skipped.")
            continue

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            tmp.write(contents)
            tmp_path = Path(tmp.name)
        try:
            rows = read_brick_rows(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

        if not rows:
            errors.append(f"{name}: no priceable rows found, skipped.")
            continue

        source_names.append(name)
        for row in rows:
            row = dict(row)
            row["Qty"] = str(int(row["Qty"]) * multiplier)
            all_rows.append(row)

    if not all_rows:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"error": " ".join(errors) or "No priceable rows found in the uploaded CSV(s)."},
        )

    priced_rows = merge_unpriced_duplicates(price_rows(all_rows, locale=LOCALE))
    token = uuid.uuid4().hex
    source_name = source_names[0] if len(source_names) == 1 else f"{len(source_names)} combined CSVs"
    SESSIONS[token] = {"rows": priced_rows, "source_name": source_name}

    return _render_results(request, token, message=" ".join(errors) if errors else None)


@app.post("/finalize/{token}", response_class=HTMLResponse)
async def finalize(request: Request, token: str):
    session = SESSIONS.get(token)
    if not session:
        return RedirectResponse("/", status_code=303)

    form = await request.form()
    rows = session["rows"]
    updated = 0
    for idx, row in enumerate(rows):
        if row["Availability"] not in NOT_FOUND_STATUSES and row["Availability"] != "MANUAL":
            continue
        raw_value = str(form.get(f"manual_price_{idx}", "")).strip()
        if not raw_value:
            continue
        try:
            price = float(raw_value)
        except ValueError:
            continue
        qty = int(row["Qty"])
        row["UnitPriceGBP"] = f"{price:.2f}"
        row["LineTotalGBP"] = f"{price * qty:.2f}"
        row["Availability"] = "MANUAL"
        updated += 1

    message = f"Updated {updated} manual price(s)." if updated else "No manual prices entered."
    return _render_results(request, token, message=message)


@app.get("/download/{token}")
def download(token: str):
    session = SESSIONS.get(token)
    if not session:
        return RedirectResponse("/", status_code=303)

    stem = Path(session["source_name"]).stem
    output_path = OUTPUTS_DIR / f"{stem}_priced_{token[:8]}.csv"
    write_priced_csv(session["rows"], output_path)

    return FileResponse(
        output_path,
        media_type="text/csv",
        filename=f"{stem}_priced.csv",
    )


@app.get("/download/{token}/aggregate")
def download_aggregate(token: str):
    session = SESSIONS.get(token)
    if not session:
        return RedirectResponse("/", status_code=303)

    stem = Path(session["source_name"]).stem
    output_path = OUTPUTS_DIR / f"{stem}_aggregate_{token[:8]}.csv"
    write_aggregate_csv(session["rows"], output_path)

    return FileResponse(
        output_path,
        media_type="text/csv",
        filename=f"{stem}_aggregate.csv",
    )
