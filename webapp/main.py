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

# Generous relative to the handful of trusted users this runs for, but bounded
# so a mistaken (or malicious) upload can't exhaust memory/disk.
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB per CSV
MAX_FILES_PER_UPLOAD = 64
MAX_MULTIPLIER = 256

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
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:"
    )
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


# In-memory per-upload state. Fine for a handful of concurrent users behind a
# single process; not multi-worker safe and not persisted across restarts
# (see plan/results-page-overhaul.md Stage 3 for a possible SQLite upgrade).
SESSIONS: dict[str, dict] = {}

NOT_FOUND_STATUSES = {"NOT_FOUND_ON_PAB"}


def _evict_stale_sessions() -> None:
    now = time.time()
    expired = [tok for tok, sess in SESSIONS.items() if now - sess["last_accessed"] > SESSION_TTL_SECONDS]
    for tok in expired:
        del SESSIONS[tok]

    if len(SESSIONS) > MAX_SESSIONS:
        oldest_first = sorted(SESSIONS.items(), key=lambda item: item[1]["last_accessed"])
        for tok, _ in oldest_first[: len(SESSIONS) - MAX_SESSIONS]:
            del SESSIONS[tok]


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


def _materialize(session: dict) -> list[dict[str, str]]:
    """Derive the current, flattened, priced row list from a session's
    per-file base rows + live copy counts + any manual price overrides.

    Recomputed on every render rather than stored, so changing one file's
    copy count (or adding/removing a file) never needs to touch the others'
    already-fetched PAB prices.
    """
    scaled_rows = [
        _scale_row(row, file_entry["multiplier"])
        for file_entry in session["files"]
        for row in file_entry["base_rows"]
    ]
    merged = merge_unpriced_duplicates(scaled_rows)

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

    token = uuid.uuid4().hex
    SESSIONS[token] = {
        "files": _build_file_entries(valid_files),
        "manual_overrides": {},
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

    message = f"Removed {', '.join(removed_names)}." if removed_names else None
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

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = OUTPUTS_DIR / f"{timestamp}_detailed_{token[:8]}.csv"
    write_priced_csv(_materialize(session), output_path)

    return FileResponse(
        output_path,
        media_type="text/csv",
        filename=f"{timestamp}_detailed.csv",
    )
