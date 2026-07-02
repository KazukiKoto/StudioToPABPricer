const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("file-input");
const dropzoneText = document.getElementById("dropzone-text");
const form = document.getElementById("upload-form");
const overlay = document.getElementById("loading-overlay");

if (dropzone && fileInput) {
  ["dragenter", "dragover"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.add("dragover");
    })
  );

  ["dragleave", "drop"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.remove("dragover");
    })
  );

  dropzone.addEventListener("drop", (e) => {
    const files = e.dataTransfer.files;
    if (files.length) {
      fileInput.files = files;
      dropzoneText.textContent = files[0].name;
    }
  });

  fileInput.addEventListener("change", () => {
    if (fileInput.files.length) {
      dropzoneText.textContent = fileInput.files[0].name;
    }
  });
}

if (form && overlay) {
  form.addEventListener("submit", () => {
    overlay.classList.remove("hidden");
  });
}
