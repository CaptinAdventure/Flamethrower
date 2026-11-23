(() => {
  document.addEventListener("click", (event) => {
    const link = event.target.closest("[data-refine-plan], [data-cancel-refinement]");
    if (!link) return;
    event.preventDefault();
    if (document.body.dataset.actionPending) return;
    const actions = link.closest("[data-architect-actions]");
    const refining = link.hasAttribute("data-refine-plan");
    actions.querySelector("[data-architect-choices]").hidden = refining;
    actions.querySelector("[data-action-editor]").hidden = !refining;
    const focus = refining ? actions.querySelector("textarea") : actions.querySelector("[data-refine-plan]");
    focus.focus({ preventScroll: true });
  });

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!form.matches("[data-inplace-action]")) return;
    event.preventDefault();
    if (document.body.dataset.actionPending) return;
    const data = new FormData(form);
    const actions = form.closest("[data-architect-actions]");
    const buttons = [...actions.querySelectorAll("button")];
    const scroll = { left: window.scrollX, top: window.scrollY, behavior: "instant" };
    document.body.dataset.actionPending = "true";
    buttons.forEach((button) => { button.disabled = true; });
    actions.querySelector("[data-workflow-error]")?.remove();
    let retrySafe = false;
    try {
      const response = await fetch(form.action, { method: "POST", body: data, cache: "no-store" });
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      const next = page.querySelector("main");
      if (!next?.querySelector("#next-action")) {
        retrySafe = !response.redirected && response.status >= 400 && response.status < 500;
        throw new Error(`HTTP ${response.status}: ${next?.textContent.trim() || "Unexpected server response"}`);
      }
      document.querySelector("main").replaceWith(next);
      if (response.ok) history.replaceState(null, "", response.url);
      window.scrollTo(scroll);
      const nextAction = next.querySelector("#next-action");
      const bounds = nextAction.getBoundingClientRect();
      if (bounds.top < 0 || bounds.top >= window.innerHeight) {
        nextAction.scrollIntoView({ block: "start", behavior: "instant" });
      }
    } catch (error) {
      const message = document.createElement("p");
      message.dataset.workflowError = "";
      message.setAttribute("role", "alert");
      message.textContent = retrySafe ? `Action failed: ${error.message}` :
        `Action outcome is unknown: ${error.message}. It may already have been recorded. Reload before retrying.`;
      actions.append(message);
      if (retrySafe) buttons.forEach((button) => { button.disabled = false; });
    } finally {
      delete document.body.dataset.actionPending;
      document.dispatchEvent(new Event("flamethrower:content-updated"));
    }
  });
})();
