from __future__ import annotations

from playwright.sync_api import expect

from tests.conftest import FIXTURES_DIR


def test_multi_file_upload_with_copies_and_not_found_merge(page, live_server):
    page.goto(live_server)
    page.wait_for_selector("#file-rows .file-row")

    page.click("#add-file-row")
    file_inputs = page.locator('input[type="file"]')
    file_inputs.nth(0).set_input_files(str(FIXTURES_DIR / "simple.csv"))
    file_inputs.nth(1).set_input_files(str(FIXTURES_DIR / "other.csv"))

    multiplier_inputs = page.locator('input[name="multipliers"]')
    multiplier_inputs.nth(0).fill("3")
    multiplier_inputs.nth(1).fill("17")

    page.click('button[type="submit"]', timeout=60000)
    page.wait_for_selector(".summary-cards", timeout=60000)

    # simple.csv: 3005 qty4, 3023 qty6, 3867 qty1 -- all x3
    # other.csv: 3024 qty8, 3867 qty2 -- all x17
    # total qty = (4+6+1)*3 + (8+2)*17 = 33 + 170 = 203
    total_qty = page.locator(".card-value").nth(0).inner_text()
    assert total_qty == "203"

    # 3867 is not in the fake catalog and appears in both files; it must be
    # merged into a single not-found row, not listed twice.
    assert page.locator('input[name^="manual_price_"]').count() == 1


def test_manual_price_entry_updates_totals(page, live_server):
    page.goto(live_server)
    page.wait_for_selector("#file-rows .file-row")
    page.locator('input[type="file"]').first.set_input_files(str(FIXTURES_DIR / "simple.csv"))
    page.click('button[type="submit"]', timeout=60000)
    page.wait_for_selector(".summary-cards", timeout=60000)

    before_cost = page.locator(".card-value").nth(1).inner_text()
    assert before_cost == "£0.66"  # 4*0.06 + 6*0.07 = 0.66, excludes the not-found 3867

    page.fill('input[name^="manual_price_"]', "0.15")
    page.click('button:has-text("Update Prices")')
    page.wait_for_selector(".banner-info")

    after_cost = page.locator(".card-value").nth(1).inner_text()
    # + 1 * 0.15 for the now-manually-priced 3867
    assert after_cost == "£0.81"
    assert page.locator(".card-value").nth(3).inner_text() == "0"  # "Needs attention" drops to 0


def test_split_button_downloads_both_formats(page, live_server):
    page.goto(live_server)
    page.wait_for_selector("#file-rows .file-row")
    page.locator('input[type="file"]').first.set_input_files(str(FIXTURES_DIR / "simple.csv"))
    page.click('button[type="submit"]', timeout=60000)
    page.wait_for_selector(".summary-cards", timeout=60000)

    with page.expect_download() as simple_dl:
        page.click(".split-btn-main")
    assert "_simple" in simple_dl.value.suggested_filename

    page.click(".split-btn-toggle")
    page.wait_for_selector(".split-btn-menu:not(.hidden)")
    with page.expect_download() as detailed_dl:
        page.click("text=Download Detailed")
    assert "_detailed" in detailed_dl.value.suggested_filename


def test_client_side_rejects_non_csv_file(page, live_server, tmp_path):
    bad_file = tmp_path / "not_a_csv.txt"
    bad_file.write_text("nope")

    page.goto(live_server)
    page.wait_for_selector("#file-rows .file-row")
    page.locator('input[type="file"]').first.set_input_files(str(bad_file))

    error = page.locator(".file-row-error").first
    assert "isn't a .csv file" in error.inner_text()

    # The invalid file must not have been accepted onto the input.
    file_count = page.locator('input[type="file"]').first.evaluate("el => el.files.length")
    assert file_count == 0


def test_copies_stepper_auto_recalculates_without_explicit_button(page, live_server):
    page.goto(live_server)
    page.wait_for_selector("#file-rows .file-row")
    page.locator('input[type="file"]').first.set_input_files(str(FIXTURES_DIR / "simple.csv"))
    page.click('button[type="submit"]', timeout=60000)
    page.wait_for_selector(".summary-cards", timeout=60000)

    assert page.locator(".card-value").first.inner_text() == "11"  # 4+6+1 at 1 copy

    # The Files dropdown is closed by default; open it to reach the stepper.
    page.click("#batch-dropdown-toggle")

    # The "Files" stepper's + button submits on its own -- no separate
    # "recalculate" button should be needed. `expect(...)` polls/retries,
    # since this is now an in-place AJAX update rather than a navigation
    # (the element never disappears, so waiting for it to "reappear" isn't
    # a valid signal that the update has actually landed).
    page.click("#copies-form .stepper-plus")
    expect(page.locator(".card-value").first).to_have_text("22")  # (4+6+1) * 2 copies


def test_copies_stepper_zero_confirms_and_removes_file(page, live_server):
    page.goto(live_server)
    page.wait_for_selector("#file-rows .file-row")
    page.click("#add-file-row")
    file_inputs = page.locator('input[type="file"]')
    file_inputs.nth(0).set_input_files(str(FIXTURES_DIR / "simple.csv"))
    file_inputs.nth(1).set_input_files(str(FIXTURES_DIR / "other.csv"))
    page.click('button[type="submit"]', timeout=60000)
    page.wait_for_selector(".summary-cards", timeout=60000)

    page.click("#batch-dropdown-toggle")  # closed by default
    assert page.locator(".batch-file").count() == 2

    page.click(".batch-file:has-text('other.csv') .stepper-minus")
    page.wait_for_selector("#confirm-modal:not(.hidden)")
    page.click("#confirm-modal-confirm")

    expect(page.locator(".batch-file")).to_have_count(1)
    assert "Removed other.csv" in page.locator(".banner-info").inner_text()
    assert "3024" not in page.content()  # other.csv's only part is gone


def test_copies_stepper_zero_via_x_button_removes_file(page, live_server):
    """The X button next to each file is an alternate way to trigger the
    same confirm-then-remove flow as dragging its stepper to 0."""
    page.goto(live_server)
    page.wait_for_selector("#file-rows .file-row")
    page.click("#add-file-row")
    file_inputs = page.locator('input[type="file"]')
    file_inputs.nth(0).set_input_files(str(FIXTURES_DIR / "simple.csv"))
    file_inputs.nth(1).set_input_files(str(FIXTURES_DIR / "other.csv"))
    page.click('button[type="submit"]', timeout=60000)
    page.wait_for_selector(".summary-cards", timeout=60000)

    page.click("#batch-dropdown-toggle")  # closed by default
    page.click(".batch-file:has-text('other.csv') .batch-file-remove")
    page.wait_for_selector("#confirm-modal:not(.hidden)")
    page.click("#confirm-modal-confirm")

    expect(page.locator(".batch-file")).to_have_count(1)
    assert "3024" not in page.content()


def test_copies_stepper_zero_cancelled_reverts_value(page, live_server):
    page.goto(live_server)
    page.wait_for_selector("#file-rows .file-row")
    page.locator('input[type="file"]').first.set_input_files(str(FIXTURES_DIR / "simple.csv"))
    page.click('button[type="submit"]', timeout=60000)
    page.wait_for_selector(".summary-cards", timeout=60000)

    page.click("#batch-dropdown-toggle")  # closed by default
    page.click(".batch-file .stepper-minus")  # 1 -> 0 triggers the in-page confirm modal
    page.wait_for_selector("#confirm-modal:not(.hidden)")
    page.click("#confirm-modal-cancel")
    expect(page.locator("#confirm-modal")).to_be_hidden()

    # No request should have been sent, and the stepper should show its old value.
    assert page.locator(".batch-file").count() == 1
    assert page.locator('.batch-file input[type="number"]').input_value() == "1"


def test_manual_price_survives_copies_change(page, live_server):
    page.goto(live_server)
    page.wait_for_selector("#file-rows .file-row")
    page.locator('input[type="file"]').first.set_input_files(str(FIXTURES_DIR / "simple.csv"))
    page.click('button[type="submit"]', timeout=60000)
    page.wait_for_selector(".summary-cards", timeout=60000)

    page.fill('input[name^="manual_price_"]', "0.15")
    page.click('button:has-text("Update Prices")')
    page.wait_for_selector(".banner-info")
    assert page.locator(".card-value").nth(1).inner_text() == "£0.81"  # 0.66 + 1*0.15

    # "Update Prices" is a full page reload, so the dropdown
    # is back to closed-by-default; open it to reach the stepper.
    page.click("#batch-dropdown-toggle")
    page.click("#copies-form .stepper-plus")  # 1 -> 2 copies

    # Found parts double to £1.32, plus the manual price rescaling to 2*0.15=0.30 -> £1.62
    expect(page.locator(".card-value").nth(1)).to_have_text("£1.62")
    assert page.locator(".badge-manual").count() == 1


def test_add_csv_widget_replaces_button_with_row_and_auto_submits(page, live_server):
    """"+ Add CSV" swaps itself in place for a single dropzone/stepper/x row
    (no "add another"/"add to batch" buttons); choosing a valid file there
    submits immediately."""
    page.goto(live_server)
    page.wait_for_selector("#file-rows .file-row")
    page.locator('input[type="file"]').first.set_input_files(str(FIXTURES_DIR / "simple.csv"))
    page.click('button[type="submit"]', timeout=60000)
    page.wait_for_selector(".summary-cards", timeout=60000)

    page.click("#batch-dropdown-toggle")  # closed by default
    expect(page.locator("#add-csv-toggle")).to_be_visible()
    expect(page.locator("#add-csv-form")).to_be_hidden()

    page.click("#add-csv-toggle")
    expect(page.locator("#add-csv-toggle")).to_be_hidden()
    expect(page.locator("#add-csv-form")).to_be_visible()

    page.locator("#add-csv-row input[type=\"file\"]").set_input_files(str(FIXTURES_DIR / "other.csv"))

    expect(page.locator(".batch-file")).to_have_count(2)
    assert "3024" in page.content()  # other.csv's part is now present

    # After a successful add, the whole batch panel re-renders fresh, so the
    # widget is back to showing the "+ Add CSV" button.
    page.click("#batch-dropdown-toggle")
    expect(page.locator("#add-csv-toggle")).to_be_visible()


def test_add_csv_widget_x_button_cancels_back_to_button(page, live_server):
    page.goto(live_server)
    page.wait_for_selector("#file-rows .file-row")
    page.locator('input[type="file"]').first.set_input_files(str(FIXTURES_DIR / "simple.csv"))
    page.click('button[type="submit"]', timeout=60000)
    page.wait_for_selector(".summary-cards", timeout=60000)

    page.click("#batch-dropdown-toggle")
    page.click("#add-csv-toggle")
    expect(page.locator("#add-csv-form")).to_be_visible()

    page.click("#add-csv-cancel")
    expect(page.locator("#add-csv-form")).to_be_hidden()
    expect(page.locator("#add-csv-toggle")).to_be_visible()
    # Cancelling must not have added anything or sent any request.
    assert page.locator(".batch-file").count() == 1


def test_info_banner_auto_dismisses(page, live_server):
    """The "Added N file(s)."/"Removed x.csv." confirmations are transient,
    not something to act on -- they should fade away on their own rather
    than sit on the page permanently."""
    page.goto(live_server)
    page.wait_for_selector("#file-rows .file-row")
    page.locator('input[type="file"]').first.set_input_files(str(FIXTURES_DIR / "simple.csv"))
    page.click('button[type="submit"]', timeout=60000)
    page.wait_for_selector(".summary-cards", timeout=60000)

    page.click("#batch-dropdown-toggle")
    page.click("#add-csv-toggle")
    page.locator('#add-csv-row input[type="file"]').set_input_files(str(FIXTURES_DIR / "other.csv"))
    page.wait_for_selector(".banner-info")

    expect(page.locator(".banner-info")).to_be_hidden(timeout=6000)


def test_copies_update_does_not_reload_the_page(page, live_server):
    """Regression test for the "no flash/reload" requirement: a JS-side
    marker that only a real navigation would clear must survive a copies
    update, since that update is now an in-place AJAX swap of <main> only."""
    page.goto(live_server)
    page.wait_for_selector("#file-rows .file-row")
    page.locator('input[type="file"]').first.set_input_files(str(FIXTURES_DIR / "simple.csv"))
    page.click('button[type="submit"]', timeout=60000)
    page.wait_for_selector(".summary-cards", timeout=60000)

    page.click("#batch-dropdown-toggle")  # closed by default
    page.evaluate("window.__noReloadMarker = 'still-here'")
    page.click("#copies-form .stepper-plus")
    expect(page.locator(".card-value").first).to_have_text("22")

    assert page.evaluate("window.__noReloadMarker") == "still-here"


def test_files_dropdown_can_collapse_and_expand(page, live_server):
    page.goto(live_server)
    page.wait_for_selector("#file-rows .file-row")
    page.locator('input[type="file"]').first.set_input_files(str(FIXTURES_DIR / "simple.csv"))
    page.click('button[type="submit"]', timeout=60000)
    page.wait_for_selector(".summary-cards", timeout=60000)

    # Closed by default.
    assert page.locator("#batch-dropdown-toggle").get_attribute("aria-expanded") == "false"
    expect(page.locator("#batch-dropdown-panel")).to_be_hidden()

    page.click("#batch-dropdown-toggle")
    assert page.locator("#batch-dropdown-toggle").get_attribute("aria-expanded") == "true"
    expect(page.locator(".batch-file").first).to_be_visible()

    page.click("#batch-dropdown-toggle")
    assert page.locator("#batch-dropdown-toggle").get_attribute("aria-expanded") == "false"
    expect(page.locator("#batch-dropdown-panel")).to_be_hidden()


def test_dark_mode_toggle_persists(page, live_server):
    page.goto(live_server)
    page.wait_for_selector("#theme-toggle")

    initial = page.evaluate("document.documentElement.getAttribute('data-theme')")
    page.click("#theme-toggle")
    toggled = page.evaluate("document.documentElement.getAttribute('data-theme')")
    assert toggled != initial
    assert page.evaluate("localStorage.getItem('theme')") == toggled

    page.reload()
    page.wait_for_selector("#theme-toggle")
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == toggled
