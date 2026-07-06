"""Shared fixtures for the unit and API test suites.

The e2e suite has its own conftest.py (tests/e2e/conftest.py) because it needs
a session-scoped live server rather than the function-scoped monkeypatching
used here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pab_pricer.pricer import ElementPrice

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# A small, deterministic stand-in for LEGO's Pick a Brick catalog. Every test
# in this repo prices against this fake instead of the real lego.com so the
# suite is fast, offline, and doesn't hammer LEGO's site.
#
# Note: 3867 is deliberately absent, so any row referencing it comes back
# NOT_FOUND_ON_PAB -- fixtures/simple.csv and fixtures/other.csv both include
# 3867 so tests can exercise the not-found / manual-price paths.
FAKE_CATALOG: dict[str, dict[str, ElementPrice]] = {
    "3005": {"4211389": ElementPrice("4211389", 0.06, "AVAILABLE")},
    "3023": {"302326": ElementPrice("302326", 0.07, "AVAILABLE")},
    "3024": {"302426": ElementPrice("302426", 0.05, "AVAILABLE")},
}


def fake_fetch_part_siblings(part_number, locale="en-gb", timeout=15.0, retries=3):
    return FAKE_CATALOG.get(part_number, {})


@pytest.fixture
def patch_fetcher(monkeypatch):
    """Point pab_pricer.pricer.fetch_part_siblings at the fake catalog for the
    duration of one test. price_rows() looks this name up at call time (not at
    import time), so this monkeypatch is honoured even by code paths (like
    webapp.main.upload) that call price_rows() without passing a fetcher."""
    monkeypatch.setattr("pab_pricer.pricer.fetch_part_siblings", fake_fetch_part_siblings)
    return fake_fetch_part_siblings


@pytest.fixture
def app_client(tmp_path, patch_fetcher, monkeypatch):
    """A FastAPI TestClient wired to a clean, per-test SESSIONS dict and a
    scratch OUTPUTS_DIR, so tests never touch the repo's real outputs/ folder
    or leak state between tests."""
    from fastapi.testclient import TestClient

    import webapp.main as webapp_main

    monkeypatch.setattr(webapp_main, "SESSIONS", {})
    monkeypatch.setattr(webapp_main, "OUTPUTS_DIR", tmp_path)

    with TestClient(webapp_main.app) as client:
        yield client


@pytest.fixture
def csrf_token(app_client):
    """The app issues a CSRF cookie (double-submit pattern) on first GET; POST
    routes reject requests unless the form's csrf_token matches that cookie.
    This fixture primes the client's cookie jar and returns the matching
    value to include in a test's form payload."""
    app_client.get("/")
    return app_client.cookies.get("csrf_token")
