# LEGO Pick a Brick Pricer

Prices the parts in a bricks CSV (BLItemNo/ElementId/Qty, e.g. a BrickLink XML export
converted to CSV) against the official LEGO Pick a Brick website, in GBP.

Ships as both a CLI and a small FastAPI web UI, and is Dockerized so it doesn't depend
on the host's Python version.

## Screenshots

Upload one or more CSVs, with a "copies" count per file:

![Upload page](docs/screenshots/input.png)

Priced results, with a "not found" table for manual pricing and a download dropdown:

![Results page](docs/screenshots/results.png)

## Quick start (Docker, recommended)

```
make start     # build image + run the web UI at http://localhost:8000
make stop      # tear it down
make restart   # stop + start
```

Or price a CSV without the web UI:

```
make docker-price CSV=input/your_parts_list.csv
```

Priced CSVs are written to `outputs/`.

## Web UI

Drag in one or more CSVs at `http://localhost:8000`. Each file gets its own row with
a "Copies" count, so you can price several builds at once (e.g. 3&times; one list
and 17&times; another) as a single aggregate total. Files are validated as CSV both
in the browser and on the server before pricing. You'll get:

- A priced table for every part LEGO's site matched, with GBP unit/line totals
  (rounded to 2dp).
- A "not found" table (usually discontinued colours PAB no longer sells) where you
  can enter a manual unit price to accept it into the total. The same missing piece
  is combined into one row even if it appeared in multiple uploaded CSVs.
- A "Download" button for the final, reconciled result, with a dropdown for either
  format:
  - **Simple** (default) — one row per unique part/colour with combined qty and
    line total.
  - **Detailed** — every priced line item as-is.

  Both are saved as `outputs/<timestamp>_simple.csv` / `outputs/<timestamp>_detailed.csv`,
  and either can be dropped straight back into the upload form later to keep
  editing — manual prices you'd already entered are preserved rather than lost.

### Editing a batch after pricing

Nothing on the results page requires re-uploading or leaves the page:

- **Files panel** — add more CSVs to the batch, change a file's copy count, or
  remove a file entirely (drag its copies to 0, or use its &times; button), each
  applied instantly with the total recalculated in place.
- **Per-piece quantity editing** — every row's Qty (both tables) has its own
  +/&minus; stepper; dragging one to 0 removes just that piece. A quantity you set
  by hand stays put even if you later change a file's copy count elsewhere in the
  batch.
- **Sortable columns** — click any column header (either table) to sort by it,
  click again to reverse. Numeric columns sort numerically; the Qty and manual
  price columns sort by their live edited value, not the original CSV order.
- Manual prices, quantity edits, and the batch's file list all persist across a
  server restart (see *Session storage*, below) — not just across page
  navigation.

## Native (no Docker)

Requires Python 3.11+ and `curl` on PATH (curl.exe ships with Windows 10/11, macOS,
and most Linux distros).

```
make install
make price CSV=input/your_parts_list.csv   # CLI
make web                                   # web UI at http://localhost:8000
```

## How pricing works

There's no public LEGO pricing API. The pricer requests LEGO's Pick a Brick search
page (`?query=<part number>`) and reads the embedded Next.js/Apollo state, which
lists every colour/element variant of that part along with its GBP price and
availability. Each row's exact `ElementId` is matched against that list.

LEGO's site sits behind Cloudflare bot detection that blocks Python's `requests`
library by TLS fingerprint but allows plain `curl` — so the fetcher shells out to
`curl` instead of using a Python HTTP client.

Distinct part numbers are looked up concurrently (default 4 at a time — each
lookup spends nearly all its time waiting on network I/O, not CPU, so a small
worker pool speeds up large CSVs several-fold without hammering LEGO's site).
Tune it with `--workers` on the CLI; the web UI uses the same default.

## Session storage

Results-page state (uploaded files, copy counts, manual prices, quantity edits)
is kept in a small SQLite database at `outputs/sessions.db`, not just in memory —
a session survives a server/container restart, not only page navigation. Sessions
older than 6 hours, or beyond 200 total, are swept automatically.

## Security notes

Meant to run behind an authenticating reverse proxy (e.g. an ALB + Cognito) for a
handful of trusted users, not exposed raw on the internet. Regardless, the app
itself applies: upload size/count limits, a CSRF token (double-submit cookie) on
every state-changing request, baseline security headers (CSP, `X-Frame-Options`,
`X-Content-Type-Options`, HSTS), and runs as a non-root user in Docker.

## Testing

```
make install-dev   # installs test deps + Playwright's Chromium
make test           # unit + API + end-to-end browser tests
make test-unit       # pure functions (pricer, session store) only
make test-api         # FastAPI route tests
make test-e2e          # Playwright, driving a real server in a browser
make audit             # pip-audit against both requirement files
make docker-test        # the same `make test` suite, run inside a container
```

None of these hit the real `lego.com` — the pricing fetcher is monkeypatched to a
small fake catalog (`tests/conftest.py`), so the suite is fast, deterministic, and
offline. A GitHub Actions workflow (`.github/workflows/test.yml`) runs `make test`
and `make audit` on every push and pull request.

## Input CSV format

Expects at least these columns: `BLItemNo`, `ElementId`, `PartName`, `Qty`. Rows
missing any of these, or blank/summary rows, are skipped.

Local part-list CSVs can be kept in `input/`, which is gitignored.
