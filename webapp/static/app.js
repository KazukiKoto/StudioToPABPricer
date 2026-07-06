const fileRows = document.getElementById("file-rows");
const rowTemplate = document.getElementById("file-row-template");
const addRowBtn = document.getElementById("add-file-row");
const form = document.getElementById("upload-form");
const overlay = document.getElementById("loading-overlay");
const clientError = document.getElementById("client-error");

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

if (form && overlay) {
  form.addEventListener("submit", (e) => {
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

    if (hasInvalid) {
      e.preventDefault();
      if (clientError) {
        clientError.textContent = "Please fix the highlighted CSV(s) before pricing.";
        clientError.classList.remove("hidden");
      }
      return;
    }

    if (clientError) clientError.classList.add("hidden");
    overlay.classList.remove("hidden");
  });
}

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
