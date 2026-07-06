from __future__ import annotations

import re

from tests.conftest import FIXTURES_DIR


def _upload_files(csrf_token: str, *names_and_multipliers: tuple[str, int]):
    """Build the multipart payload for repeated `files=`/`multipliers=` fields.

    Note: httpx's TestClient needs `data`'s repeated values passed as a dict of
    lists (not a list of tuples) when combined with a `files=` list of tuples,
    or it silently drops both fields and the request 422s.
    """
    files = []
    multipliers = []
    for name, multiplier in names_and_multipliers:
        path = FIXTURES_DIR / name
        files.append(("files", (name, path.read_bytes(), "text/csv")))
        multipliers.append(str(multiplier))
    return files, {"multipliers": multipliers, "csrf_token": csrf_token}


def _extract_token(html: str) -> str:
    return re.search(r"/download/([0-9a-f]{32})", html).group(1)


def test_index_page_loads(app_client):
    resp = app_client.get("/")
    assert resp.status_code == 200
    assert "Pick a Brick" in resp.text


def test_upload_single_csv_happy_path(app_client, csrf_token):
    files, data = _upload_files(csrf_token, ("simple.csv", 1))
    resp = app_client.post("/upload", files=files, data=data)

    assert resp.status_code == 200
    assert "Pricing results" in resp.text
    assert "Not found on Pick a Brick" in resp.text  # 3867 isn't in the fake catalog
    assert "3005" in resp.text and "3023" in resp.text


def test_upload_multiple_csvs_aggregates_and_merges_not_found(app_client, csrf_token):
    files, data = _upload_files(csrf_token, ("simple.csv", 3), ("other.csv", 17))
    resp = app_client.post("/upload", files=files, data=data)

    assert resp.status_code == 200
    # simple.csv qty=4 brick * 3 copies + other.csv has none of that part -> 12
    # 3867 appears in both files: (1 * 3) + (2 * 17) = 37, merged into one row/input.
    assert resp.text.count('name="manual_price_') == 1
    assert "37" in resp.text


def test_upload_rejects_without_csrf_token(app_client):
    """A POST with no (or a stale) csrf_token must be rejected outright --
    this is the whole point of the check, so it gets its own direct test
    rather than relying on the fixture always supplying a valid token."""
    files = [("files", ("simple.csv", (FIXTURES_DIR / "simple.csv").read_bytes(), "text/csv"))]
    data = {"multipliers": ["1"], "csrf_token": "not-the-real-token"}
    resp = app_client.post("/upload", files=files, data=data)
    assert resp.status_code == 403


def test_upload_rejects_non_csv_extension(app_client, csrf_token):
    files = [("files", ("not_a_csv.txt", (FIXTURES_DIR / "not_a_csv.txt").read_bytes(), "text/plain"))]
    data = {"multipliers": ["1"], "csrf_token": csrf_token}
    resp = app_client.post("/upload", files=files, data=data)

    assert resp.status_code == 200
    assert "not a .csv file" in resp.text
    assert "Pricing results" not in resp.text


def test_upload_rejects_binary_content_disguised_as_csv(app_client, csrf_token):
    files = [("files", ("sneaky.csv", b"PK\x00\x00binary\x00junk", "text/csv"))]
    data = {"multipliers": ["1"], "csrf_token": csrf_token}
    resp = app_client.post("/upload", files=files, data=data)

    assert resp.status_code == 200
    assert "does not look like a text CSV file" in resp.text


def test_upload_rejects_oversized_file(app_client, csrf_token):
    import webapp.main as webapp_main

    too_big = b"BLItemNo,ElementId,PartName,Qty\n" + b"3005,4211389,Brick 1 x 1,1\n" * 1
    oversized_payload = too_big + b"#" * (webapp_main.MAX_FILE_SIZE_BYTES + 1)
    files = [("files", ("big.csv", oversized_payload, "text/csv"))]
    data = {"multipliers": ["1"], "csrf_token": csrf_token}
    resp = app_client.post("/upload", files=files, data=data)

    assert resp.status_code == 200
    assert "too large" in resp.text


def test_upload_rejects_too_many_files(app_client, csrf_token):
    import webapp.main as webapp_main

    one_file = (FIXTURES_DIR / "simple.csv").read_bytes()
    files = [("files", (f"f{i}.csv", one_file, "text/csv")) for i in range(webapp_main.MAX_FILES_PER_UPLOAD + 1)]
    data = {"multipliers": ["1"] * len(files), "csrf_token": csrf_token}
    resp = app_client.post("/upload", files=files, data=data)

    assert resp.status_code == 200
    assert "Too many files" in resp.text


def test_upload_rejects_multiplier_above_max(app_client, csrf_token):
    import webapp.main as webapp_main

    files, data = _upload_files(csrf_token, ("simple.csv", 1))
    data["multipliers"] = [str(webapp_main.MAX_MULTIPLIER + 1)]
    resp = app_client.post("/upload", files=files, data=data)

    assert resp.status_code == 200
    assert "invalid copy count" in resp.text


def test_upload_rejects_invalid_multiplier(app_client, csrf_token):
    files, _ = _upload_files(csrf_token, ("simple.csv", 1))
    data = {"multipliers": ["0"], "csrf_token": csrf_token}
    resp = app_client.post("/upload", files=files, data=data)

    assert resp.status_code == 200
    assert "invalid copy count" in resp.text


def test_upload_with_no_valid_rows_shows_error(app_client, csrf_token):
    empty_csv = b"BLItemNo,ElementId,PartName,Qty\n"
    files = [("files", ("empty.csv", empty_csv, "text/csv"))]
    data = {"multipliers": ["1"], "csrf_token": csrf_token}
    resp = app_client.post("/upload", files=files, data=data)

    assert resp.status_code == 200
    assert "no priceable rows found" in resp.text


def test_upload_source_name_is_html_escaped(app_client, csrf_token):
    """A CSV filename with HTML/script-like characters must not be able to
    inject markup into the rendered page (Jinja autoescaping should already
    guarantee this; this test pins that guarantee)."""
    payload = (FIXTURES_DIR / "simple.csv").read_bytes()
    evil_name = "<script>alert(1)</script>.csv"
    files = [("files", (evil_name, payload, "text/csv"))]
    data = {"multipliers": ["1"], "csrf_token": csrf_token}
    resp = app_client.post("/upload", files=files, data=data)

    assert resp.status_code == 200
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_finalize_sets_manual_price_and_recalculates(app_client, csrf_token):
    files, data = _upload_files(csrf_token, ("simple.csv", 1))
    upload_resp = app_client.post("/upload", files=files, data=data)
    token = _extract_token(upload_resp.text)

    finalize_resp = app_client.post(
        f"/finalize/{token}", data={"manual_price_2": "0.15", "csrf_token": csrf_token}
    )
    assert finalize_resp.status_code == 200
    assert "Updated 1 manual price" in finalize_resp.text
    assert "&pound;0.15" in finalize_resp.text


def test_finalize_with_no_manual_prices_entered_shows_that_message(app_client, csrf_token):
    files, data = _upload_files(csrf_token, ("simple.csv", 1))
    upload_resp = app_client.post("/upload", files=files, data=data)
    token = _extract_token(upload_resp.text)

    resp = app_client.post(f"/finalize/{token}", data={"csrf_token": csrf_token})
    assert resp.status_code == 200
    assert "No manual prices entered." in resp.text


def test_finalize_rejects_without_csrf_token(app_client, csrf_token):
    files, data = _upload_files(csrf_token, ("simple.csv", 1))
    upload_resp = app_client.post("/upload", files=files, data=data)
    token = _extract_token(upload_resp.text)

    resp = app_client.post(f"/finalize/{token}", data={"manual_price_2": "0.15"})
    assert resp.status_code == 403


def test_finalize_unknown_token_redirects_home(app_client, csrf_token):
    resp = app_client.post(
        "/finalize/does-not-exist", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_download_simple_returns_aggregated_csv(app_client, csrf_token):
    files, data = _upload_files(csrf_token, ("simple.csv", 1))
    upload_resp = app_client.post("/upload", files=files, data=data)
    token = _extract_token(upload_resp.text)

    resp = app_client.get(f"/download/{token}/simple")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.text.splitlines()[0] == "BLItemNo,PartName,ColorName,Qty,UnitPriceGBP,LineTotalGBP"


def test_download_detailed_returns_full_csv(app_client, csrf_token):
    files, data = _upload_files(csrf_token, ("simple.csv", 1))
    upload_resp = app_client.post("/upload", files=files, data=data)
    token = _extract_token(upload_resp.text)

    resp = app_client.get(f"/download/{token}/detailed")
    assert resp.status_code == 200
    assert "Availability" in resp.text.splitlines()[0]


def test_download_default_redirects_to_simple(app_client, csrf_token):
    files, data = _upload_files(csrf_token, ("simple.csv", 1))
    upload_resp = app_client.post("/upload", files=files, data=data)
    token = _extract_token(upload_resp.text)

    resp = app_client.get(f"/download/{token}", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == f"/download/{token}/simple"


def test_download_ignores_path_traversal_in_source_filename(app_client, csrf_token, tmp_path):
    """The uploaded filename must never influence where output CSVs land on
    disk -- output paths are built only from a server-generated timestamp and
    the session token, never from user input. Regression test for that
    invariant, not just a read-through of the code."""
    payload = (FIXTURES_DIR / "simple.csv").read_bytes()
    files = [("files", ("../../../../etc/evil.csv", payload, "text/csv"))]
    data = {"multipliers": ["1"], "csrf_token": csrf_token}
    upload_resp = app_client.post("/upload", files=files, data=data)
    token = _extract_token(upload_resp.text)

    resp = app_client.get(f"/download/{token}/simple")
    assert resp.status_code == 200

    produced_files = list(tmp_path.glob("*.csv"))
    assert len(produced_files) == 1
    assert produced_files[0].parent == tmp_path
    assert ".." not in produced_files[0].name


def test_download_unknown_token_redirects_home(app_client):
    resp = app_client.get("/download/does-not-exist/simple", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_security_headers_present_on_every_response(app_client):
    resp = app_client.get("/")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'self'" in resp.headers["content-security-policy"]
    assert "max-age=" in resp.headers["strict-transport-security"]


def test_session_eviction_caps_total_sessions(app_client, csrf_token, monkeypatch):
    import webapp.main as webapp_main

    monkeypatch.setattr(webapp_main, "MAX_SESSIONS", 2)
    for _ in range(3):
        files, data = _upload_files(csrf_token, ("simple.csv", 1))
        resp = app_client.post("/upload", files=files, data=data)
        assert resp.status_code == 200

    assert len(webapp_main.SESSIONS) == 2


def test_session_eviction_drops_stale_sessions_by_ttl(app_client, csrf_token, monkeypatch):
    import time

    import webapp.main as webapp_main

    monkeypatch.setattr(webapp_main, "SESSION_TTL_SECONDS", 0.01)

    files, data = _upload_files(csrf_token, ("simple.csv", 1))
    first_resp = app_client.post("/upload", files=files, data=data)
    first_token = _extract_token(first_resp.text)
    assert first_token in webapp_main.SESSIONS

    time.sleep(0.05)

    files, data = _upload_files(csrf_token, ("other.csv", 1))
    app_client.post("/upload", files=files, data=data)

    assert first_token not in webapp_main.SESSIONS
