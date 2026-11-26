(() => {
  let busy = false;
  let timer;
  let contentVersion = 0;
  const isBoard = Boolean(document.getElementById("task-board"));
  const isActivity = Boolean(document.getElementById("session-activity-page"));

  function fingerprint(element) {
    const copy = element.cloneNode(true);
    for (const token of copy.querySelectorAll('input[name="csrfmiddlewaretoken"]')) {
      token.setAttribute("value", "");
    }
    for (const diagram of copy.querySelectorAll("[data-mermaid-container]")) {
      diagram.replaceWith(diagram.querySelector("pre"));
    }
    return copy.innerHTML;
  }

  function hasSelection(element) {
    const selection = window.getSelection();
    return selection && !selection.isCollapsed &&
      (element.contains(selection.anchorNode) || element.contains(selection.focusNode));
  }

  function isInUse(element) {
    return element.contains(document.activeElement) ||
      element.querySelector("[data-title-editor][data-title-editing='true'], [data-title-editor][data-title-saving='true'], [data-request-editor][data-request-editing='true'], [data-request-editor][data-request-saving='true']") ||
      element.querySelector("details[open], [data-action-editor]:not([hidden]), [data-link-error]") ||
      hasSelection(element);
  }

  async function update() {
    if (busy || document.body.dataset.actionPending || document.body.classList.contains("task-dragging")) return;
    busy = true;
    const main = document.querySelector("main");
    const version = contentVersion;
    try {
      const response = await fetch(window.location.href, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      if (document.querySelector("main") !== main || version !== contentVersion || document.body.dataset.actionPending || document.body.classList.contains("task-dragging")) return;
      let held = false;
      const regions = isBoard ? ["task-board"] : isActivity ? ["task-heading", "session-activity-page", "run-history"] :
        ["task-heading", "workflow-progress", "workflow-status", "next-action", "task-request", "work-type-suggestion", "task-suggestions", "task-links"];
      const requestRegions = ["next-action", "task-request"].map((id) => ({
        current: document.getElementById(id), next: page.getElementById(id),
      }));
      const holdRequestRegions = !isBoard && !isActivity &&
        requestRegions.every(({ current, next }) => current && next && fingerprint(current) !== fingerprint(next)) &&
        requestRegions.some(({ current }) => isInUse(current));
      for (const id of regions) {
        const current = document.getElementById(id);
        const next = page.getElementById(id);
        if (!current || !next || fingerprint(current) === fingerprint(next)) continue;
        if ((holdRequestRegions && (id === "next-action" || id === "task-request")) || isInUse(current)) {
          held = true;
          continue;
        }
        current.replaceWith(next);
      }
      document.dispatchEvent(new Event("flamethrower:content-updated"));
      document.getElementById("live-status").textContent = held ? "Live updates paused for the section you are using." : "";
      const settled = isActivity && document.getElementById("session-activity-page").dataset.active === "false";
      if (!held && settled) {
        clearInterval(timer);
        timer = null;
      }
    } catch (error) {
      document.getElementById("live-status").textContent = `Live update failed: ${error.message}. Refresh the page to retry.`;
    } finally {
      busy = false;
    }
  }
  function startPolling() {
    if (!timer) timer = setInterval(update, 3000);
  }
  document.addEventListener("flamethrower:content-updated", () => {
    contentVersion++;
    if (document.querySelector('[data-active="true"]')) startPolling();
  });
  for (const eventName of ["flamethrower:title-save-start", "flamethrower:title-save-end", "flamethrower:request-save-start", "flamethrower:request-save-end"]) {
    document.addEventListener(eventName, () => {
      contentVersion++;
    });
  }
  startPolling();
})();
