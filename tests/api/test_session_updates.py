from __future__ import annotations

from tests.api.test_routes import _extract_token, _upload_files
from tests.conftest import FIXTURES_DIR


def test_copies_update_recalculates_totals(app_client, csrf_token):
    files, data = _upload_files(csrf_token, ("simple.csv", 1))
    upload_resp = app_client.post("/upload", files=files, data=data)
    token = _extract_token(upload_resp.text)

    # simple.csv at 1 copy: qty 4+6+1=11, cost 4*0.06 + 6*0.07 = 0.66 (3867 not found)
    assert ">11<" in upload_resp.text
    assert "&pound;0.66" in upload_resp.text

    resp = app_client.post(
        f"/session/{token}/copies", data={"copies_0": "3", "csrf_token": csrf_token}
    )
    assert resp.status_code == 200
    # at 3 copies: qty (4+6+1)*3=33, cost (4*0.06 + 6*0.07)*3 = 1.98
    assert ">33<" in resp.text
    assert "&pound;1.98" in resp.text


def test_copies_update_rejects_non_integer_atomically(app_client, csrf_token):
    files, data = _upload_files(csrf_token, ("simple.csv", 3), ("other.csv", 5))
    upload_resp = app_client.post("/upload", files=files, data=data)
    token = _extract_token(upload_resp.text)

    resp = app_client.post(
        f"/session/{token}/copies",
        data={"copies_0": "5", "copies_1": "not-a-number", "csrf_token": csrf_token},
    )
    assert resp.status_code == 200
    assert "Invalid copies value" in resp.text
    # Neither file's multiplier changed: totals must match the original upload response.
    # 3867 appears in both files: (1 * 3) + (2 * 5) = 13, at the ORIGINAL 3x/5x copies.
    assert ">13<" in resp.text


def test_copies_update_rejects_out_of_range_atomically(app_client, csrf_token):
    import webapp.main as webapp_main

    files, data = _upload_files(csrf_token, ("simple.csv", 1))
    upload_resp = app_client.post("/upload", files=files, data=data)
    token = _extract_token(upload_resp.text)

    resp = app_client.post(
        f"/session/{token}/copies",
        data={"copies_0": str(webapp_main.MAX_MULTIPLIER + 1), "csrf_token": csrf_token},
    )
    assert resp.status_code == 200
    assert "Copies must be between 0" in resp.text
    assert ">11<" in resp.text  # unchanged from original 1x


def test_copies_update_zero_removes_that_file_only(app_client, csrf_token):
    files, data = _upload_files(csrf_token, ("simple.csv", 1), ("other.csv", 1))
    upload_resp = app_client.post("/upload", files=files, data=data)
    token = _extract_token(upload_resp.text)

    resp = app_client.post(
        f"/session/{token}/copies", data={"copies_1": "0", "csrf_token": csrf_token}
    )
    assert resp.status_code == 200
    assert "Removed other.csv" in resp.text
    # other.csv's 3024 (qty 8) is gone; simple.csv's rows remain.
    assert "3024" not in resp.text
    assert "3005" in resp.text


def test_copies_update_removing_last_file_redirects_home(app_client, csrf_token):
    import webapp.main as webapp_main

    files, data = _upload_files(csrf_token, ("simple.csv", 1))
    upload_resp = app_client.post("/upload", files=files, data=data)
    token = _extract_token(upload_resp.text)

    resp = app_client.post(
        f"/session/{token}/copies", data={"copies_0": "0", "csrf_token": csrf_token}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert token not in webapp_main.SESSIONS


def test_copies_update_unknown_token_redirects_home(app_client, csrf_token):
    resp = app_client.post(
        "/session/does-not-exist/copies", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_copies_update_rejects_without_csrf_token(app_client, csrf_token):
    files, data = _upload_files(csrf_token, ("simple.csv", 1))
    upload_resp = app_client.post("/upload", files=files, data=data)
    token = _extract_token(upload_resp.text)

    resp = app_client.post(f"/session/{token}/copies", data={"copies_0": "2"})
    assert resp.status_code == 403


def test_manual_price_survives_copies_update_and_rescales(app_client, csrf_token):
    files, data = _upload_files(csrf_token, ("simple.csv", 1))
    upload_resp = app_client.post("/upload", files=files, data=data)
    token = _extract_token(upload_resp.text)

    app_client.post(f"/finalize/{token}", data={"manual_price_2": "0.15", "csrf_token": csrf_token})

    resp = app_client.post(
        f"/session/{token}/copies", data={"copies_0": "4", "csrf_token": csrf_token}
    )
    assert resp.status_code == 200
    # 3867's original qty is 1; at 4 copies, qty=4, manual price 0.15 -> line total 0.60.
    assert "&pound;0.60" in resp.text
    assert "badge-manual" in resp.text
    assert 'class="card-value">0<' in resp.text  # "Needs attention" back to 0: still MANUAL, not NOT_FOUND


def test_add_files_happy_path(app_client, csrf_token):
    files, data = _upload_files(csrf_token, ("simple.csv", 1))
    upload_resp = app_client.post("/upload", files=files, data=data)
    token = _extract_token(upload_resp.text)

    add_files = [("files", ("other.csv", (FIXTURES_DIR / "other.csv").read_bytes(), "text/csv"))]
    add_data = {"multipliers": ["2"], "csrf_token": csrf_token}
    resp = app_client.post(f"/session/{token}/add-files", files=add_files, data=add_data)

    assert resp.status_code == 200
    assert "Added 1 file(s)." in resp.text
    assert "3024" in resp.text  # other.csv's part now present
    assert "2 combined CSVs" in resp.text  # source name recomputed to reflect both files


def test_add_files_reuses_upload_validation(app_client, csrf_token):
    files, data = _upload_files(csrf_token, ("simple.csv", 1))
    upload_resp = app_client.post("/upload", files=files, data=data)
    token = _extract_token(upload_resp.text)

    bad_files = [("files", ("not_a_csv.txt", (FIXTURES_DIR / "not_a_csv.txt").read_bytes(), "text/plain"))]
    bad_data = {"multipliers": ["1"], "csrf_token": csrf_token}
    resp = app_client.post(f"/session/{token}/add-files", files=bad_files, data=bad_data)

    assert resp.status_code == 200
    assert "not a .csv file" in resp.text
    # Original session untouched.
    assert "simple.csv" in resp.text


def test_add_files_enforces_max_files_per_batch(app_client, csrf_token, monkeypatch):
    import webapp.main as webapp_main

    monkeypatch.setattr(webapp_main, "MAX_FILES_PER_UPLOAD", 1)

    files, data = _upload_files(csrf_token, ("simple.csv", 1))
    upload_resp = app_client.post("/upload", files=files, data=data)
    token = _extract_token(upload_resp.text)

    add_files = [("files", ("other.csv", (FIXTURES_DIR / "other.csv").read_bytes(), "text/csv"))]
    add_data = {"multipliers": ["1"], "csrf_token": csrf_token}
    resp = app_client.post(f"/session/{token}/add-files", files=add_files, data=add_data)

    assert resp.status_code == 200
    assert "Too many files in one batch" in resp.text


def test_add_files_unknown_token_redirects_home(app_client, csrf_token):
    add_files = [("files", ("other.csv", (FIXTURES_DIR / "other.csv").read_bytes(), "text/csv"))]
    resp = app_client.post(
        "/session/does-not-exist/add-files",
        files=add_files,
        data={"multipliers": ["1"], "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_add_files_rejects_without_csrf_token(app_client, csrf_token):
    files, data = _upload_files(csrf_token, ("simple.csv", 1))
    upload_resp = app_client.post("/upload", files=files, data=data)
    token = _extract_token(upload_resp.text)

    add_files = [("files", ("other.csv", (FIXTURES_DIR / "other.csv").read_bytes(), "text/csv"))]
    resp = app_client.post(
        f"/session/{token}/add-files",
        files=add_files,
        data={"multipliers": ["1"], "csrf_token": "not-the-real-token"},
    )
    assert resp.status_code == 403


def test_downloads_reflect_copies_update(app_client, csrf_token):
    files, data = _upload_files(csrf_token, ("simple.csv", 1))
    upload_resp = app_client.post("/upload", files=files, data=data)
    token = _extract_token(upload_resp.text)

    app_client.post(f"/session/{token}/copies", data={"copies_0": "5", "csrf_token": csrf_token})

    resp = app_client.get(f"/download/{token}/simple")
    assert resp.status_code == 200
    lines = resp.text.splitlines()
    total_line = next(line for line in lines if line.startswith("TOTAL"))
    assert total_line.split(",")[3] == "55"  # (4+6+1)*5
