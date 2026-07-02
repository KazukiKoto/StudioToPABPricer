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

from pab_pricer.pricer import price_rows, read_brick_rows, write_priced_csv

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
OUTPUTS_DIR = REPO_ROOT / "outputs"
LOCALE = "en-gb"  # pins pricing to GBP

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
async def upload(request: Request, file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".csv"):
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"error": "Please upload a .csv file."},
        )

    contents = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp.write(contents)
        tmp_path = Path(tmp.name)

    try:
        rows = read_brick_rows(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    if not rows:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"error": "No priceable rows found in that CSV."},
        )

    priced_rows = price_rows(rows, locale=LOCALE)
    token = uuid.uuid4().hex
    SESSIONS[token] = {"rows": priced_rows, "source_name": file.filename}

    return _render_results(request, token)


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
        row["UnitPriceGBP"] = f"{price:.4f}"
        row["LineTotalGBP"] = f"{price * qty:.4f}"
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
