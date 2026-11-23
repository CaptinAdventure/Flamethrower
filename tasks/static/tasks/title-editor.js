(() => {
  const announce = (message, isError = false) => {
    const status = document.querySelector("[data-title-status]");
    if (!status) return;
    status.textContent = message;
    status.setAttribute("role", isError ? "alert" : "status");
    status.setAttribute("aria-live", isError ? "assertive" : "polite");
    status.classList.toggle("title-edit-error", isError);
  };

  const finishEditing = (form, { focusDisplay = false, message = "" } = {}) => {
    const row = form.closest(".task-title-row");
    const display = row?.querySelector("[data-title-display]");
    const input = form.querySelector("[data-title-input]");
    form.dataset.titleEditing = "false";
    form.dataset.titleSaving = "false";
    form.hidden = true;
    if (display) {
      display.hidden = false;
      input.value = display.textContent;
      if (focusDisplay) display.focus();
    }
    announce(message);
  };

  const cancelEditing = (form, focusDisplay = false) => {
    if (form.dataset.titleSaving === "true") return;
    finishEditing(form, { focusDisplay });
  };

  const startEditing = (display) => {
    const row = display.closest(".task-title-row");
    const form = row?.querySelector("[data-title-editor]");
    const input = form?.querySelector("[data-title-input]");
    if (!form || !input || form.dataset.titleSaving === "true") return;
    input.value = display.textContent;
    display.hidden = true;
    form.hidden = false;
    form.dataset.titleEditing = "true";
    announce("");
    input.focus();
    input.select();
  };

  document.addEventListener("click", (event) => {
    const display = event.target.closest("[data-title-display]");
    if (display) startEditing(display);
  });

  document.addEventListener("pointerdown", (event) => {
    const form = document.querySelector("[data-title-editor][data-title-editing='true']");
    if (form && !form.contains(event.target)) cancelEditing(form);
  });

  document.addEventListener("focusout", (event) => {
    const form = event.target.closest("[data-title-editor]");
    if (form && form.dataset.titleSaving !== "true" && !form.contains(event.relatedTarget)) {
      cancelEditing(form);
    }
  });

  document.addEventListener("keydown", (event) => {
    const form = event.target.closest("[data-title-editor]");
    if (form && event.key === "Escape") {
      event.preventDefault();
      cancelEditing(form);
    }
  });

  document.addEventListener("click", (event) => {
    const cancelButton = event.target.closest("[data-title-cancel]");
    if (cancelButton) cancelEditing(cancelButton.closest("[data-title-editor]"), true);
  });

  document.addEventListener("submit", async (event) => {
    const form = event.target.closest("[data-title-editor]");
    if (!form) return;
    event.preventDefault();
    if (form.dataset.titleSaving === "true") return;
    const input = form.querySelector("[data-title-input]");
    const saveButton = form.querySelector("[data-title-save]");
    const cancelButton = form.querySelector("[data-title-cancel]");
    const display = form.closest(".task-title-row")?.querySelector("[data-title-display]");
    const payload = new FormData(form);
    form.dataset.titleSaving = "true";
    input.readOnly = true;
    saveButton.disabled = true;
    cancelButton.disabled = true;
    announce("Saving title…");
    document.dispatchEvent(new Event("flamethrower:title-save-start"));

    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: payload,
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (!response.headers.get("content-type")?.includes("application/json")) {
        throw new Error("The save outcome is uncertain. Your draft is preserved; reload before retrying.");
      }
      let result;
      try {
        result = await response.json();
      } catch {
        throw new Error("The save outcome is uncertain. Your draft is preserved; reload before retrying.");
      }
      if (!response.ok) {
        const validationMessage = Object.values(result.errors || {})
          .flat()
          .map((error) => error.message)
          .join(" ");
        if (response.status === 409) {
          throw new Error("The task changed. Your draft is preserved; reload before retrying.");
        }
        if (response.status >= 500) {
          throw new Error("The save outcome is uncertain. Your draft is preserved; reload before retrying.");
        }
        throw new Error(result.error || validationMessage || "The title could not be saved. Check it and try again.");
      }

      display.textContent = result.title;
      form.querySelector("[data-title-version]").value = result.version;
      form.querySelector("[data-title-stage-version]").value = result.stage_version;
      document.title = `${result.title}${form.dataset.titleSuffix}`;
      const restoreFocus = form.contains(document.activeElement);
      finishEditing(form, { focusDisplay: restoreFocus, message: "Title saved." });
    } catch (error) {
      form.dataset.titleSaving = "false";
      input.readOnly = false;
      saveButton.disabled = false;
      cancelButton.disabled = false;
      if (form.contains(document.activeElement)) input.focus();
      const networkError = error instanceof TypeError;
      announce(
        networkError
          ? "Could not confirm the save. Your draft is preserved; reload before retrying."
          : `Could not save title: ${error.message}`,
        true,
      );
    } finally {
      form.dataset.titleSaving = "false";
      document.dispatchEvent(new Event("flamethrower:title-save-end"));
    }
  });
})();
