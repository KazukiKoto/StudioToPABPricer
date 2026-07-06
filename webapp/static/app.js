// ---- Confirm modal ---------------------------------------------------
// Lives outside <main> in both templates, so unlike everything below it's
// wired exactly once: AJAX swaps only ever replace <main>'s contents.
const confirmModal = document.getElementById("confirm-modal");
const confirmModalMessage = document.getElementById("confirm-modal-message");
const confirmModalCancelBtn = document.getElementById("confirm-modal-cancel");
const confirmModalConfirmBtn = document.getElementById("confirm-modal-confirm");
let resolveConfirmModal = null;

function showConfirm(message) {
  if (!confirmModal) return Promise.resolve(window.confirm(message));
  return new Promise((resolve) => {
    confirmModalMessage.textContent = message;
    confirmModal.classList.remove("hidden");
    resolveConfirmModal = resolve;
    confirmModalConfirmBtn.focus();
  });
}

function closeConfirmModal(result) {
  if (!confirmModal) return;
  confirmModal.classList.add("hidden");
  if (resolveConfirmModal) {
    resolveConfirmModal(result);
    resolveConfirmModal = null;
  }
}

if (confirmModal) {
  confirmModalCancelBtn.addEventListener("click", () => closeConfirmModal(false));
  confirmModalConfirmBtn.addEventListener("click", () => closeConfirmModal(true));
  confirmModal.addEventListener("click", (e) => {
    if (e.target === confirmModal) closeConfirmModal(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !confirmModal.classList.contains("hidden")) closeConfirmModal(false);
  });
}

// ---- Everything inside <main> -----------------------------------------
// Re-run after every AJAX swap (see submitViaAjax below), since replacing
// <main>'s innerHTML destroys these elements and their listeners along
// with it. Querying fresh each call means there's never a stale-reference
// or duplicate-listener risk -- the old nodes are simply gone.
function initMainContent() {
  const fileRows = document.getElementById("file-rows");
  const rowTemplate = document.getElementById("file-row-template");
  const addRowBtn = document.getElementById("add-file-row");
  const overlay = document.getElementById("loading-overlay");
  const clientError = document.getElementById("client-error");

  // Informational banners (e.g. "Added 1 file(s).", "Removed other.csv.")
  // are transient confirmations, not something the user needs to act on --
  // auto-dismiss them after a few seconds instead of leaving them up
  // forever. Error banners are left alone: those need the user to fix
  // something, so they should stay until the next action clears them.
  const infoBanner = document.querySelector(".banner-info");
  if (infoBanner) {
    setTimeout(() => {
      infoBanner.classList.add("fading");
      infoBanner.addEventListener("transitionend", () => infoBanner.classList.add("hidden"), { once: true });
    }, 4000);
  }

  const DEFAULT_ROW_TEXT = "Drag & drop a CSV here, or click to browse";

  function isCsvFile(file) {
    return !!file && file.name.toLowerCase().endsWith(".csv");
  }

  function showRowError(row, message) {
    const err = row.querySelector(".file-row-error");
    err.textContent = message;
    err.classList.remove("hidden");
  }

  function clearRowError(row) {
    const err = row.querySelector(".file-row-error");
    err.textContent = "";
    err.classList.add("hidden");
  }

  function resetRowFile(row) {
    const input = row.querySelector('input[type="file"]');
    const text = row.querySelector(".file-row-text");
    input.value = "";
    text.textContent = DEFAULT_ROW_TEXT;
  }

  function setRowFile(row, file) {
    if (!isCsvFile(file)) {
      resetRowFile(row);
      showRowError(row, `"${file.name}" isn't a .csv file.`);
      return;
    }
    clearRowError(row);
    const input = row.querySelector('input[type="file"]');
    const text = row.querySelector(".file-row-text");
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    text.textContent = file.name;
  }

  function wireRow(row) {
    const dropLabel = row.querySelector(".file-row-drop");
    const input = row.querySelector('input[type="file"]');
    const removeBtn = row.querySelector(".btn-remove-row");
    const multiplierInput = row.querySelector('input[name="multipliers"]');
    const minusBtn = row.querySelector(".stepper-minus");
    const plusBtn = row.querySelector(".stepper-plus");

    function stepMultiplier(delta) {
      const min = parseInt(multiplierInput.min, 10) || 1;
      const max = parseInt(multiplierInput.max, 10) || Infinity;
      const current = parseInt(multiplierInput.value, 10) || min;
      multiplierInput.value = Math.min(max, Math.max(min, current + delta));
    }

    minusBtn.addEventListener("click", () => stepMultiplier(-1));
    plusBtn.addEventListener("click", () => stepMultiplier(1));

    input.addEventListener("change", () => {
      if (input.files.length) {
        setRowFile(row, input.files[0]);
      }
    });

    ["dragenter", "dragover"].forEach((evt) =>
      dropLabel.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropLabel.classList.add("dragover");
      })
    );

    ["dragleave", "drop"].forEach((evt) =>
      dropLabel.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropLabel.classList.remove("dragover");
      })
    );

    dropLabel.addEventListener("drop", (e) => {
      const files = Array.from(e.dataTransfer.files || []);
      if (!files.length) return;
      setRowFile(row, files[0]);
      // Any extra files dropped on a single row each get their own new row.
      files.slice(1).forEach((file) => addRow(file));
    });

    removeBtn.addEventListener("click", () => {
      if (fileRows.children.length > 1) {
        row.remove();
      } else {
        // Keep at least one row; just clear it.
        resetRowFile(row);
        clearRowError(row);
        row.querySelector('input[type="number"]').value = 1;
      }
    });
  }

  function addRow(file) {
    const fragment = rowTemplate.content.cloneNode(true);
    const row = fragment.querySelector(".file-row");
    wireRow(row);
    fileRows.appendChild(row);
    if (file) {
      setRowFile(row, file);
    }
    return row;
  }

  if (fileRows && rowTemplate) {
    addRow();

    if (addRowBtn) {
      addRowBtn.addEventListener("click", () => addRow());
    }

    // Dropping multiple files anywhere in the file-rows area fans them out
    // across rows: the first empty row gets the first file, and a new row
    // is created for each additional file.
    ["dragenter", "dragover"].forEach((evt) =>
      fileRows.addEventListener(evt, (e) => e.preventDefault())
    );
    fileRows.addEventListener("drop", (e) => {
      const target = e.target.closest(".file-row-drop");
      if (target) return; // handled by the row's own listener
      e.preventDefault();
      const files = Array.from(e.dataTransfer.files || []);
      files.forEach((file) => addRow(file));
    });
  }

  function validateFileRows() {
    if (!fileRows) return true;
    const rows = Array.from(fileRows.querySelectorAll(".file-row"));
    let hasInvalid = false;
    rows.forEach((row) => {
      const input = row.querySelector('input[type="file"]');
      const file = input.files[0];
      if (!file) {
        showRowError(row, "Please choose a CSV file.");
        hasInvalid = true;
      } else if (!isCsvFile(file)) {
        showRowError(row, `"${file.name}" isn't a .csv file.`);
        hasInvalid = true;
      }
    });
    return !hasInvalid;
  }

  function showClientError(message) {
    if (!clientError) return;
    clientError.textContent = message;
    clientError.classList.remove("hidden");
  }

  // ---- AJAX submission for the results-page batch forms ----
  // (copies-form's stepper auto-submit, and add-files-form) so editing a
  // batch never flashes/reloads the page. The initial /upload form on the
  // index page deliberately keeps a normal full-page submit -- landing on
  // the results page *is* a real navigation.
  async function submitViaAjax(form) {
    if (overlay) overlay.classList.remove("hidden");
    if (clientError) clientError.classList.add("hidden");
    try {
      const resp = await fetch(form.action, { method: "POST", body: new FormData(form) });
      if (resp.redirected) {
        window.location.href = resp.url;
        return;
      }
      const html = await resp.text();
      const newDoc = new DOMParser().parseFromString(html, "text/html");
      const newMain = newDoc.querySelector("main");
      const currentMain = document.querySelector("main");
      if (newMain && currentMain) {
        currentMain.innerHTML = newMain.innerHTML;
        document.title = newDoc.title;
        initMainContent();
      }
    } catch (err) {
      showClientError("Something went wrong updating this batch. Please reload the page.");
    } finally {
      const freshOverlay = document.getElementById("loading-overlay");
      if (freshOverlay) freshOverlay.classList.add("hidden");
    }
  }

  const uploadForm = document.getElementById("upload-form"); // index page only
  if (uploadForm && overlay) {
    uploadForm.addEventListener("submit", (e) => {
      if (!validateFileRows()) {
        e.preventDefault();
        showClientError("Please fix the highlighted CSV(s) before pricing.");
        return;
      }
      if (clientError) clientError.classList.add("hidden");
      overlay.classList.remove("hidden");
    });
  }

  const copiesForm = document.getElementById("copies-form");
  if (copiesForm) {
    copiesForm.addEventListener("submit", (e) => {
      e.preventDefault();
      submitViaAjax(copiesForm);
    });
  }

  // ---- "Files" dropdown open/close ----
  const batchToggle = document.getElementById("batch-dropdown-toggle");
  const batchPanel = document.getElementById("batch-dropdown-panel");
  if (batchToggle && batchPanel) {
    batchToggle.addEventListener("click", () => {
      const expanded = batchToggle.getAttribute("aria-expanded") === "true";
      batchToggle.setAttribute("aria-expanded", String(!expanded));
      batchPanel.classList.toggle("collapsed", expanded);
    });
  }

  // ---- "+ Add CSV": replaces itself in place with a single dropzone +
  // copies stepper + cancel ("x") button. Choosing/dropping a valid file
  // submits immediately -- there's no separate "add another"/"submit"
  // button here, unlike the multi-file widget on the upload page. ----
  const addCsvToggle = document.getElementById("add-csv-toggle");
  const addCsvForm = document.getElementById("add-csv-form");
  const addCsvRow = document.getElementById("add-csv-row");
  if (addCsvToggle && addCsvForm && addCsvRow) {
    const addCsvInput = addCsvRow.querySelector('input[type="file"]');
    const addCsvDrop = addCsvRow.querySelector(".file-row-drop");
    const addCsvMultiplier = addCsvRow.querySelector('input[name="multipliers"]');
    const addCsvMinus = addCsvRow.querySelector(".stepper-minus");
    const addCsvPlus = addCsvRow.querySelector(".stepper-plus");

    function resetAddCsvRow() {
      addCsvInput.value = "";
      addCsvRow.querySelector(".file-row-text").textContent = DEFAULT_ROW_TEXT;
      addCsvMultiplier.value = 1;
      clearRowError(addCsvRow);
    }

    function showAddCsvButton() {
      addCsvForm.classList.add("hidden");
      addCsvToggle.classList.remove("hidden");
      resetAddCsvRow();
    }

    function showAddCsvRow() {
      addCsvToggle.classList.add("hidden");
      addCsvForm.classList.remove("hidden");
    }

    addCsvToggle.addEventListener("click", showAddCsvRow);
    addCsvRow.querySelector("#add-csv-cancel").addEventListener("click", showAddCsvButton);

    addCsvForm.addEventListener("submit", (e) => e.preventDefault());

    addCsvMinus.addEventListener("click", () => {
      const min = parseInt(addCsvMultiplier.min, 10) || 1;
      const current = parseInt(addCsvMultiplier.value, 10) || min;
      addCsvMultiplier.value = Math.max(min, current - 1);
    });
    addCsvPlus.addEventListener("click", () => {
      const max = parseInt(addCsvMultiplier.max, 10) || Infinity;
      const current = parseInt(addCsvMultiplier.value, 10) || 1;
      addCsvMultiplier.value = Math.min(max, current + 1);
    });

    function trySubmitAddCsv(file) {
      if (!isCsvFile(file)) {
        showRowError(addCsvRow, `"${file.name}" isn't a .csv file.`);
        return;
      }
      clearRowError(addCsvRow);
      const dt = new DataTransfer();
      dt.items.add(file);
      addCsvInput.files = dt.files;
      submitViaAjax(addCsvForm);
    }

    addCsvInput.addEventListener("change", () => {
      if (addCsvInput.files.length) trySubmitAddCsv(addCsvInput.files[0]);
    });

    ["dragenter", "dragover"].forEach((evt) =>
      addCsvDrop.addEventListener(evt, (e) => {
        e.preventDefault();
        addCsvDrop.classList.add("dragover");
      })
    );
    ["dragleave", "drop"].forEach((evt) =>
      addCsvDrop.addEventListener(evt, (e) => {
        e.preventDefault();
        addCsvDrop.classList.remove("dragover");
      })
    );
    addCsvDrop.addEventListener("drop", (e) => {
      const files = Array.from(e.dataTransfer.files || []);
      if (!files.length) return;
      // Single-slot widget: only the first dropped file is used.
      trySubmitAddCsv(files[0]);
    });
  }

  // ---- Results-page "Files" steppers: auto-submit on every change ----
  // Dropping a file to 0 copies removes it from the batch, confirmed via
  // the in-page modal (not a browser confirm() popup).
  document.querySelectorAll('.stepper[data-auto-submit="true"]').forEach((stepper) => {
    const input = stepper.querySelector('input[type="number"]');
    const minusBtn = stepper.querySelector(".stepper-minus");
    const plusBtn = stepper.querySelector(".stepper-plus");
    const stepperForm = stepper.closest("form");
    const filename = stepper.dataset.filename || "this CSV";

    async function commit(newValue) {
      const previous = input.value;
      if (newValue <= 0) {
        const confirmed = await showConfirm(`Remove "${filename}" from this batch?`);
        if (!confirmed) {
          input.value = previous;
          return;
        }
        input.value = 0;
      } else {
        const max = parseInt(input.max, 10) || Infinity;
        input.value = Math.min(max, newValue);
      }
      submitViaAjax(stepperForm);
    }

    minusBtn.addEventListener("click", () => commit((parseInt(input.value, 10) || 0) - 1));
    plusBtn.addEventListener("click", () => commit((parseInt(input.value, 10) || 0) + 1));
    input.addEventListener("change", () => {
      const value = parseInt(input.value, 10);
      if (!Number.isNaN(value)) commit(value);
    });
  });

  // ---- Per-file "x" remove button: same confirm-then-remove path as
  // dragging that file's stepper to 0. ----
  document.querySelectorAll(".batch-file-remove").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const filename = btn.dataset.filename || "this CSV";
      const confirmed = await showConfirm(`Remove "${filename}" from this batch?`);
      if (!confirmed) return;
      const row = btn.closest(".batch-file");
      const input = row.querySelector('input[type="number"]');
      input.value = 0;
      const stepperForm = row.closest("form");
      submitViaAjax(stepperForm);
    });
  });

  // ---- Split download button dropdown ----
  document.querySelectorAll(".split-btn").forEach((splitBtn) => {
    const toggle = splitBtn.querySelector(".split-btn-toggle");
    const menu = splitBtn.querySelector(".split-btn-menu");
    if (!toggle || !menu) return;

    function closeMenu() {
      menu.classList.add("hidden");
      toggle.setAttribute("aria-expanded", "false");
    }

    toggle.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = !menu.classList.contains("hidden");
      if (isOpen) {
        closeMenu();
      } else {
        menu.classList.remove("hidden");
        toggle.setAttribute("aria-expanded", "true");
      }
    });

    document.addEventListener("click", (e) => {
      if (!splitBtn.contains(e.target)) closeMenu();
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeMenu();
    });
  });

  // ---- Theme toggle (fixed-position on the index page, inline next to
  // "Back to upload" on the results page -- either way, it may live inside
  // <main> and get replaced on an AJAX swap, so this re-wires it too). ----
  const themeToggle = document.getElementById("theme-toggle");
  const themeIcon = document.getElementById("theme-toggle-icon");
  const themeLabel = document.getElementById("theme-toggle-label");

  function applyThemeUI(theme) {
    if (themeIcon) themeIcon.textContent = theme === "dark" ? "☾" : "☀";
    if (themeLabel) themeLabel.textContent = theme === "dark" ? "Light mode" : "Dark mode";
  }

  if (themeToggle) {
    applyThemeUI(document.documentElement.getAttribute("data-theme"));

    themeToggle.addEventListener("click", () => {
      const current = document.documentElement.getAttribute("data-theme");
      const next = current === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      localStorage.setItem("theme", next);
      applyThemeUI(next);
    });
  }
}

initMainContent();
