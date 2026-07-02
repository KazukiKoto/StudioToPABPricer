# LEGO Pick a Brick Pricer

Prices the parts in a bricks CSV (BLItemNo/ElementId/Qty, e.g. a BrickLink XML export
converted to CSV) against the official LEGO Pick a Brick website, in GBP.

Ships as both a CLI and a small FastAPI web UI, and is Dockerized so it doesn't depend
on the host's Python version.

## Quick start (Docker, recommended)

```
make start     # build image + run the web UI at http://localhost:8000
make stop      # tear it down
make restart   # stop + start
```

Or price a CSV without the web UI:

```
make docker-price CSV=runway_sample.csv
```

Priced CSVs are written to `outputs/`.

## Web UI

Drag a CSV in at `http://localhost:8000`. You'll get:

- A priced table for every part LEGO's site matched, with GBP unit/line totals.
- A "not found" table (usually discontinued colours PAB no longer sells) where you
  can enter a manual unit price to accept it into the total.
- A CSV download of the final, reconciled result.

## Native (no Docker)

Requires Python 3.11+ and `curl` on PATH (curl.exe ships with Windows 10/11, macOS,
and most Linux distros).

```
make install
make price CSV=runway_sample.csv   # CLI
make web                           # web UI at http://localhost:8000
```

## How pricing works

There's no public LEGO pricing API. The pricer requests LEGO's Pick a Brick search
page (`?query=<part number>`) and reads the embedded Next.js/Apollo state, which
lists every colour/element variant of that part along with its GBP price and
availability. Each row's exact `ElementId` is matched against that list.

LEGO's site sits behind Cloudflare bot detection that blocks Python's `requests`
library by TLS fingerprint but allows plain `curl` — so the fetcher shells out to
`curl` instead of using a Python HTTP client.

## Input CSV format

Expects at least these columns: `BLItemNo`, `ElementId`, `PartName`, `Qty` (see
`runway_sample.csv`). Rows missing any of these, or blank/summary rows, are skipped.
