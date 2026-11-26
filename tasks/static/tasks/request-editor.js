(() => {
  const status = (container, message, isError = false) => {
    const element = container.querySelector("[data-request-status]");
    element.textContent = message;
    element.setAttribute("role", isError ? "alert" : "status");
    element.setAttribute("aria-live", isError ? "assertive" : "polite");
    element.classList.toggle("title-edit-error", isError);
  };

  const cancel = (form) => {
    if (form.dataset.requestSaving === "true") return;
    const container = form.closest("[data-request-container]");
    const display = container.querySelector("[data-request-display]");
    form.dataset.requestEditing = "false";
    form.hidden = true;
    display.hidden = false;
    status(container, "");
    display.focus();
  };

  document.addEventListener("click", (event) => {
    const display = event.target.closest("[data-request-display]");
    if (display) {
      const container = display.closest("[data-request-container]");
      if (document.querySelector("[data-title-editor][data-title-saving='true']")) {
        status(container, "Wait for the title to finish saving.", true);
        return;
      }
      const form = container.querySelector("[data-request-editor]");
      const input = form.querySelector("[data-request-input]");
      input.value = display.textContent;
      display.hidden = true;
      form.hidden = false;
      form.dataset.requestEditing = "true";
      status(container, "");
      input.focus();
      return;
    }
    const button = event.target.closest("[data-request-cancel]");
    if (button) cancel(button.closest("[data-request-editor]"));
  });

  document.addEventListener("keydown", (event) => {
    const form = event.target.closest("[data-request-editor]");
    if (form && event.key === "Escape") {
      event.preventDefault();
      cancel(form);
    }
  });

  window.addEventListener("beforeunload", (event) => {
    const form = document.querySelector("[data-request-editor][data-request-editing='true']");
    if (!form) return;
    const display = form.closest("[data-request-container]").querySelector("[data-request-display]");
    if (form.dataset.requestSaving === "true" || form.querySelector("[data-request-input]").value !== display.textContent) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  document.addEventListener("submit", (event) => {
    if (event.target.matches("[data-request-editor]")) return;
    const editor = document.querySelector("[data-request-editor][data-request-editing='true']");
    if (!editor) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    status(editor.closest("[data-request-container]"), "Save or cancel the request edit before continuing.", true);
  }, true);

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!form.matches("[data-request-editor]")) return;
    event.preventDefault();
    if (form.dataset.requestSaving === "true") return;

    const container = form.closest("[data-request-container]");
    const input = form.querySelector("[data-request-input]");
    const save = form.querySelector("[data-request-save]");
    const cancelButton = form.querySelector("[data-request-cancel]");
    const payload = new FormData(form);
    form.dataset.requestSaving = "true";
    input.readOnly = true;
    save.disabled = true;
    cancelButton.disabled = true;
    status(container, "Saving requested change…");
    document.dispatchEvent(new Event("flamethrower:request-save-start"));

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
        if (response.status >= 500) {
          throw new Error("The save outcome is uncertain. Your draft is preserved; reload before retrying.");
        }
        throw new Error(result.error || validationMessage || "The requested change could not be saved.");
      }
      form.dataset.requestEditing = "false";
      form.dataset.requestSaving = "false";
      window.location.reload();
    } catch (error) {
      form.dataset.requestSaving = "false";
      input.readOnly = false;
      save.disabled = false;
      cancelButton.disabled = false;
      status(
        container,
        error instanceof TypeError
          ? "Could not confirm the save. Your draft is preserved; reload before retrying."
          : `Could not save requested change: ${error.message}`,
        true,
      );
    } finally {
      document.dispatchEvent(new Event("flamethrower:request-save-end"));
    }
  });
})();
