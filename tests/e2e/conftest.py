"""E2E-only fixtures: a real uvicorn server (in a background thread, in-process)
driven by a real browser via Playwright.

Run in-process (rather than a subprocess) specifically so the session-scoped
fetch_part_siblings monkeypatch below is honoured -- a subprocess would import
its own, unpatched copy of pab_pricer.pricer and try to hit the real lego.com.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn

from tests.conftest import fake_fetch_part_siblings
from webapp.session_store import SQLiteSessionStore


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_server(tmp_path_factory):
    import pab_pricer.pricer as pricer_module
    import webapp.main as webapp_main

    original_fetcher = pricer_module.fetch_part_siblings
    pricer_module.fetch_part_siblings = fake_fetch_part_siblings
    e2e_dir = tmp_path_factory.mktemp("e2e-outputs")
    webapp_main.OUTPUTS_DIR = e2e_dir
    webapp_main.SESSIONS = SQLiteSessionStore(e2e_dir / "sessions.db")

    port = _free_port()
    config = uvicorn.Config(webapp_main.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("live_server did not start within 10s")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)
    pricer_module.fetch_part_siblings = original_fetcher


@pytest.fixture(autouse=True)
def _clear_sessions_between_tests(live_server):
    """Each e2e test gets a clean slate even though the server itself is
    session-scoped and shared across the whole file/module for speed."""
    import webapp.main as webapp_main

    webapp_main.SESSIONS.clear()
    yield
    webapp_main.SESSIONS.clear()
