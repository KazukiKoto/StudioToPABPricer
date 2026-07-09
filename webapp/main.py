"""FastAPI web UI for pricing a bricks CSV against LEGO Pick a Brick.

Run with: make web  (or: uvicorn webapp.main:app --reload)
"""

from __future__ import annotations

import secrets
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pab_pricer.pricer import (
    PAB_URL,
    merge_duplicate_parts,
    price_rows,
    read_brick_rows,
    write_aggregate_csv,
    write_priced_csv,
)
from webapp.session_store import SQLiteSessionStore

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
OUTPUTS_DIR = REPO_ROOT / "outputs"
SESSIONS_DB_PATH = REPO_ROOT / "outputs" / "sessions.db"
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

# Generous relative to the handful of trusted users this runs for, but bounded
# so a mistaken (or malicious) upload can't exhaust memory/disk.
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB per CSV
MAX_FILES_PER_UPLOAD = 64
MAX_MULTIPLIER = 256
MAX_QTY = 100_000  # per-row quantity edits on the results page

# In-memory sessions never expire on their own, so a long-lived deployment
# needs an eviction policy: sessions older than the TTL, or beyond the count
# cap (oldest first), are dropped on every upload.
SESSION_TTL_SECONDS = 6 * 60 * 60  # 6 hours
MAX_SESSIONS = 200

CSRF_COOKIE_NAME = "csrf_token"


def _csv_validation_error(filename: str, content_type: str | None, contents: bytes) -> str | None:
    """Return a human-readable reason the upload isn't a usable CSV, or None if it's fine."""
    if not filename.lower().endswith(".csv"):
        return "not a .csv file"
    if content_type and content_type.lower() not in ALLOWED_CSV_CONTENT_TYPES:
        return f"unexpected file type ({content_type})"
    if len(contents) > MAX_FILE_SIZE_BYTES:
        return f"too large (max {MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB)"
    if b"\x00" in contents[:4096]:
        return "does not look like a text CSV file"
    return None


def _verify_csrf(request: Request, submitted_token: str | None) -> None:
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie_token or not submitted_token or not secrets.compare_digest(cookie_token, submitted_token):
        raise HTTPException(
            status_code=403,
            detail="Your session token is missing or expired. Please reload the page and try again.",
        )


app = FastAPI(title="LEGO Pick a Brick Pricer")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.middleware("http")
async def security_headers_and_csrf_cookie(request: Request, call_next):
    """Issues a CSRF cookie (double-submit pattern, checked in _verify_csrf for
    POST routes) and adds baseline security headers to every response.

    This app is meant to end up hosted behind an authenticating reverse proxy
    (e.g. an ALB + Cognito) for a handful of trusted users, not exposed raw on
    the internet -- but the headers below are cheap insurance regardless, and
    the CSRF cookie matters more once there's more than one concurrent user.
    """
    existing_token = request.cookies.get(CSRF_COOKIE_NAME)
    token = existing_token or secrets.token_urlsafe(32)
    request.state.csrf_token = token

    response = await call_next(request)

    if not existing_token:
        response.set_cookie(
            CSRF_COOKIE_NAME,
            token,
            httponly=True,  # only ever compared server-side; JS never needs to read it
            samesite="lax",
            secure=request.url.scheme == "https",
            max_age=SESSION_TTL_SECONDS,
        )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    )
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


# Per-upload state, persisted to SQLite (webapp/session_store.py) so sessions
# survive a process restart/redeploy instead of vanishing with an in-memory
# dict. __getitem__ returns a fresh deserialized copy on every access -- any
# route that mutates a fetched session dict in place must write it back
# (SESSIONS[token] = session) for the change to actually persist.
SESSIONS = SQLiteSessionStore(SESSIONS_DB_PATH)

NOT_FOUND_STATUSES = {"NOT_FOUND_ON_PAB"}


def _evict_stale_sessions() -> None:
    SESSIONS.evict(SESSION_TTL_SECONDS, MAX_SESSIONS)


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


def _scale_row(row: dict[str, str], multiplier: int) -> dict[str, str]:
    """Rebuild a priced row for `multiplier` copies of its source CSV, recomputing
    Qty/LineTotalGBP from the row's original (1-copy) quantity and unit price
    rather than compounding an already-scaled value."""
    scaled = dict(row)
    qty = int(row["Qty"]) * multiplier
    scaled["Qty"] = str(qty)
    scaled["LineTotalGBP"] = f"{float(row['UnitPriceGBP']) * qty:.2f}" if row["UnitPriceGBP"] else ""
    return scaled


def _source_name(session: dict) -> str:
    names = [f["name"] for f in session["files"]]
    return names[0] if len(names) == 1 else f"{len(names)} combined CSVs"


def _pab_link(bl_item_no: str) -> str:
    """Link to PAB's search results for a part number -- the same query the
    pricer itself fetches (pab_pricer.pricer.fetch_part_siblings), so it's
    known to actually resolve. Element-specific deep links aren't exposed by
    PAB's search API, so this points at the part's search results rather
    than one exact colour."""
    return f"{PAB_URL.format(locale=LOCALE)}?query={bl_item_no}"


def _materialize(session: dict) -> list[dict[str, str]]:
    """Derive the current, flattened, priced row list from a session's
    per-file base rows + live copy counts + any quantity/manual price
    overrides.

    Recomputed on every render rather than stored, so changing one file's
    copy count (or adding/removing a file) never needs to touch the others'
    already-fetched PAB prices.
    """
    scaled_rows = [
        _scale_row(row, file_entry["multiplier"])
        for file_entry in session["files"]
        for row in file_entry["base_rows"]
    ]
    merged = merge_duplicate_parts(scaled_rows)

    # Quantity overrides are an absolute pin, not a delta -- like manual price
    # overrides, once a user sets one it wins regardless of later copies/file
    # changes elsewhere in the batch, until they change or remove it. Applied
    # before manual-price overrides below so a manually-priced row's
    # recomputed LineTotalGBP uses the already-overridden quantity.
    qty_overrides = session.get("qty_overrides", {})
    if qty_overrides:
        adjusted = []
        for row in merged:
            key = (row["BLItemNo"], row["ElementId"])
            if key in qty_overrides:
                qty = qty_overrides[key]
                if qty <= 0:
                    continue  # 0 removes that piece from the batch entirely
                row["Qty"] = str(qty)
                if row["UnitPriceGBP"]:
                    row["LineTotalGBP"] = f"{float(row['UnitPriceGBP']) * qty:.2f}"
            adjusted.append(row)
        merged = adjusted

    overrides = session.get("manual_overrides", {})
    for row in merged:
        key = (row["BLItemNo"], row["ElementId"])
        if key in overrides:
            price = overrides[key]
            qty = int(row["Qty"])
            row["UnitPriceGBP"] = f"{price:.2f}"
            row["LineTotalGBP"] = f"{price * qty:.2f}"
            row["Availability"] = "MANUAL"
    return merged


def _render_results(request: Request, token: str, message: str | None = None) -> HTMLResponse:
    session = SESSIONS[token]
    session["last_accessed"] = time.time()
    SESSIONS[token] = session
    rows = _materialize(session)
    indexed_rows = list(enumerate(rows))
    return templates.TemplateResponse(
        request=request,
        name="results.html",
        context={
            "token": token,
            "source_name": _source_name(session),
            "rows": indexed_rows,
            "files": list(enumerate(session["files"])),
            "not_found_statuses": NOT_FOUND_STATUSES,
            "message": message,
            "csrf_token": request.state.csrf_token,
            "max_multiplier": MAX_MULTIPLIER,
            "max_qty": MAX_QTY,
            **_summary(rows),
        },
    )


def _index_context(request: Request, error: str | None = None) -> dict:
    return {
        "csrf_token": request.state.csrf_token,
        "max_multiplier": MAX_MULTIPLIER,
        "error": error,
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context=_index_context(request))


async def _read_and_validate_csv(
    file: UploadFile, raw_multiplier: str
) -> tuple[str, int, list[dict[str, str]] | None, str | None]:
    """Validate one uploaded file + its copy count. Returns
    (name, multiplier, rows-or-None, error-or-None); shared by /upload and
    /session/{token}/add-files so both apply identical checks."""
    name = file.filename or "file"

    try:
        multiplier = int(raw_multiplier)
        if not (1 <= multiplier <= MAX_MULTIPLIER):
            raise ValueError
    except ValueError:
        return name, 0, None, f"{name}: invalid copy count {raw_multiplier!r}, skipped."

    contents = await file.read()
    invalid_reason = _csv_validation_error(name, file.content_type, contents)
    if invalid_reason:
        return name, multiplier, None, f"{name}: {invalid_reason}, skipped."

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp.write(contents)
        tmp_path = Path(tmp.name)
    try:
        rows = read_brick_rows(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    if not rows:
        return name, multiplier, None, f"{name}: no priceable rows found, skipped."

    return name, multiplier, rows, None


def _extract_manual_overrides(rows: list[dict[str, str]]) -> dict[tuple[str, str], float]:
    """Pull manual-price overrides out of rows read from a previously-downloaded
    results CSV (simple or detailed) dropped back into the upload form.

    Only detailed CSVs carry an Availability column, so this is a no-op for
    simple/aggregate re-uploads -- those have no way to distinguish a manual
    price from a PAB one, which matches the simple format's existing
    limitations. Read from the raw uploaded rows (before price_rows() fetches
    fresh data and overwrites Availability/UnitPriceGBP on its own copies),
    so a part no longer found on PAB keeps its previous manual price instead
    of reverting to NOT_FOUND.
    """
    overrides: dict[tuple[str, str], float] = {}
    for row in rows:
        if row.get("Availability") != "MANUAL":
            continue
        raw_price = (row.get("UnitPriceGBP") or "").strip()
        if not raw_price:
            continue
        try:
            price = float(raw_price)
        except ValueError:
            continue
        overrides[(row["BLItemNo"], row["ElementId"])] = price
    return overrides


def _build_file_entries(valid_files: list[tuple[str, int, list[dict[str, str]]]]) -> list[dict]:
    """Price every row across all files in one batch (so distinct part numbers
    shared between files are only looked up once), then split the priced rows
    back out per file, at each file's original (1-copy) quantity."""
    all_rows = [row for _, _, rows in valid_files for row in rows]
    priced = price_rows(all_rows, locale=LOCALE)

    entries = []
    cursor = 0
    for name, multiplier, rows in valid_files:
        n = len(rows)
        entries.append({"name": name, "multiplier": multiplier, "base_rows": priced[cursor : cursor + n]})
        cursor += n
    return entries


@app.post("/upload", response_class=HTMLResponse)
async def upload(
    request: Request,
    files: list[UploadFile] = File(...),
    multipliers: list[str] = Form(...),
    csrf_token: str = Form(...),
):
    _verify_csrf(request, csrf_token)

    if len(files) > MAX_FILES_PER_UPLOAD:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context=_index_context(request, error=f"Too many files in one upload (max {MAX_FILES_PER_UPLOAD})."),
        )

    errors: list[str] = []
    valid_files: list[tuple[str, int, list[dict[str, str]]]] = []
    for file, raw_multiplier in zip(files, multipliers):
        name, multiplier, rows, error = await _read_and_validate_csv(file, raw_multiplier)
        if error:
            errors.append(error)
            continue
        valid_files.append((name, multiplier, rows))

    if not valid_files:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context=_index_context(
                request, error=" ".join(errors) or "No priceable rows found in the uploaded CSV(s)."
            ),
        )

    manual_overrides: dict[tuple[str, str], float] = {}
    for _, _, rows in valid_files:
        manual_overrides.update(_extract_manual_overrides(rows))

    token = uuid.uuid4().hex
    SESSIONS[token] = {
        "files": _build_file_entries(valid_files),
        "manual_overrides": manual_overrides,
        "qty_overrides": {},
        "last_accessed": time.time(),
    }
    _evict_stale_sessions()

    return _render_results(request, token, message=" ".join(errors) if errors else None)


@app.post("/session/{token}/add-files", response_class=HTMLResponse)
async def add_files(
    request: Request,
    token: str,
    files: list[UploadFile] = File(...),
    multipliers: list[str] = Form(...),
    csrf_token: str = Form(...),
):
    """Add more CSVs to an existing results session, with the same per-file
    validation as the initial /upload."""
    session = SESSIONS.get(token)
    if not session:
        return RedirectResponse("/", status_code=303)
    _verify_csrf(request, csrf_token)
    session["last_accessed"] = time.time()

    if len(session["files"]) + len(files) > MAX_FILES_PER_UPLOAD:
        return _render_results(
            request, token, message=f"Too many files in one batch (max {MAX_FILES_PER_UPLOAD})."
        )

    errors: list[str] = []
    valid_files: list[tuple[str, int, list[dict[str, str]]]] = []
    for file, raw_multiplier in zip(files, multipliers):
        name, multiplier, rows, error = await _read_and_validate_csv(file, raw_multiplier)
        if error:
            errors.append(error)
            continue
        valid_files.append((name, multiplier, rows))

    if valid_files:
        session["files"].extend(_build_file_entries(valid_files))
        for _, _, rows in valid_files:
            session["manual_overrides"].update(_extract_manual_overrides(rows))
        SESSIONS[token] = session

    if errors:
        message = " ".join(errors)
    elif valid_files:
        message = f"Added {len(valid_files)} file(s)."
    else:
        message = None
    return _render_results(request, token, message=message)


@app.post("/session/{token}/copies", response_class=HTMLResponse)
async def update_copies(request: Request, token: str):
    """Auto-recalculating copies editor: every stepper change on the results
    page re-submits this whole form. Setting a file's copies to 0 removes it
    from the batch entirely (the client confirms with the user first)."""
    session = SESSIONS.get(token)
    if not session:
        return RedirectResponse("/", status_code=303)

    form = await request.form()
    _verify_csrf(request, form.get("csrf_token"))
    session["last_accessed"] = time.time()

    # Validate everything before changing anything, so a single bad field
    # can't leave the session in a half-updated state.
    new_values: dict[int, int] = {}
    for idx in range(len(session["files"])):
        raw = form.get(f"copies_{idx}")
        if raw is None:
            continue
        try:
            value = int(raw)
        except ValueError:
            return _render_results(request, token, message=f"Invalid copies value {raw!r}; no changes made.")
        if value < 0 or value > MAX_MULTIPLIER:
            return _render_results(
                request, token, message=f"Copies must be between 0 and {MAX_MULTIPLIER}; no changes made."
            )
        new_values[idx] = value

    removed_names: list[str] = []
    kept_files: list[dict] = []
    for idx, file_entry in enumerate(session["files"]):
        if idx in new_values:
            value = new_values[idx]
            if value == 0:
                removed_names.append(file_entry["name"])
                continue
            file_entry["multiplier"] = value
        kept_files.append(file_entry)
    session["files"] = kept_files

    if not session["files"]:
        del SESSIONS[token]
        return RedirectResponse("/", status_code=303)

    SESSIONS[token] = session
    message = f"Removed {', '.join(removed_names)}." if removed_names else None
    return _render_results(request, token, message=message)


@app.post("/session/{token}/quantities", response_class=HTMLResponse)
async def update_quantities(request: Request, token: str):
    """Per-piece quantity editor: each row's Qty field on the results page
    (both the priced and not-found tables) submits here. Setting a row's
    quantity to 0 removes just that piece from the batch (the client confirms
    with the user first, mirroring the per-file copies remove-at-zero
    behaviour). Overrides are keyed by (BLItemNo, ElementId) and, like manual
    price overrides, persist as an absolute value until changed again --
    see _materialize()."""
    session = SESSIONS.get(token)
    if not session:
        return RedirectResponse("/", status_code=303)

    form = await request.form()
    _verify_csrf(request, form.get("csrf_token"))
    session["last_accessed"] = time.time()

    rows = _materialize(session)

    # Validate everything before changing anything, so a single bad field
    # can't leave the session in a half-updated state.
    new_values: dict[tuple[str, str], int] = {}
    for idx, row in enumerate(rows):
        raw = form.get(f"qty_{idx}")
        if raw is None:
            continue
        try:
            value = int(raw)
        except ValueError:
            return _render_results(request, token, message=f"Invalid quantity {raw!r}; no changes made.")
        if value < 0 or value > MAX_QTY:
            return _render_results(
                request, token, message=f"Quantity must be between 0 and {MAX_QTY}; no changes made."
            )
        new_values[(row["BLItemNo"], row["ElementId"])] = value

    if not new_values:
        return _render_results(request, token)

    session.setdefault("qty_overrides", {}).update(new_values)
    SESSIONS[token] = session

    removed = sum(1 for v in new_values.values() if v == 0)
    message = f"Removed {removed} piece(s) from the batch." if removed else None
    return _render_results(request, token, message=message)


@app.post("/finalize/{token}", response_class=HTMLResponse)
async def finalize(request: Request, token: str):
    session = SESSIONS.get(token)
    if not session:
        return RedirectResponse("/", status_code=303)

    form = await request.form()
    _verify_csrf(request, form.get("csrf_token"))
    session["last_accessed"] = time.time()

    rows = _materialize(session)
    overrides = session.setdefault("manual_overrides", {})
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
        overrides[(row["BLItemNo"], row["ElementId"])] = price
        updated += 1

    SESSIONS[token] = session
    message = f"Updated {updated} manual price(s)." if updated else "No manual prices entered."
    return _render_results(request, token, message=message)


@app.get("/download/{token}")
def download(token: str):
    """Default download: the simple, per-part aggregate CSV."""
    return RedirectResponse(f"/download/{token}/simple", status_code=307)


@app.get("/download/{token}/simple")
def download_simple(token: str):
    session = SESSIONS.get(token)
    if not session:
        return RedirectResponse("/", status_code=303)
    session["last_accessed"] = time.time()
    SESSIONS[token] = session

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = OUTPUTS_DIR / f"{timestamp}_simple_{token[:8]}.csv"
    write_aggregate_csv(_materialize(session), output_path)

    return FileResponse(
        output_path,
        media_type="text/csv",
        filename=f"{timestamp}_simple.csv",
    )


@app.get("/download/{token}/detailed")
def download_detailed(token: str):
    session = SESSIONS.get(token)
    if not session:
        return RedirectResponse("/", status_code=303)
    session["last_accessed"] = time.time()
    SESSIONS[token] = session

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = OUTPUTS_DIR / f"{timestamp}_detailed_{token[:8]}.csv"

    rows = _materialize(session)
    for row in rows:
        # Manual prices are for pieces PAB doesn't list at all, so a search
        # link for them would just be misleading -- left blank instead.
        if row["Availability"] != "MANUAL":
            row["PABLink"] = _pab_link(row["BLItemNo"])
    write_priced_csv(rows, output_path)

    return FileResponse(
        output_path,
        media_type="text/csv",
        filename=f"{timestamp}_detailed.csv",
    )
