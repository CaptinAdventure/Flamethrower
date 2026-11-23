(() => {
  if (window.top !== window.self) return;

  function normalClick(event) {
    return !event.defaultPrevented && event.button === 0 &&
      !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey;
  }

  async function refreshRegion(id) {
    const current = document.getElementById(id);
    if (!current) return;
    const main = document.querySelector("main");
    try {
      const response = await fetch(window.location.href, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      const next = page.getElementById(id);
      if (!next) throw new Error("The refreshed page did not contain the expected section.");
      if (document.querySelector("main") !== main || !current.isConnected) return;
      const focused = current.contains(document.activeElement);
      current.replaceWith(next);
      if (focused) next.querySelector("[data-popover]")?.focus({ preventScroll: true });
      document.dispatchEvent(new Event("flamethrower:content-updated"));
    } catch (error) {
      if (!current.isConnected) return;
      const message = document.createElement("p");
      message.setAttribute("role", "alert");
      message.textContent = `Could not refresh this section: ${error.message} Reload to see the latest changes.`;
      current.append(message);
    }
  }

  document.addEventListener("click", (event) => {
    if (!normalClick(event)) return;
    const link = event.target.closest("a[data-popover][href]");
    if (!link || link.hasAttribute("download") || (link.target && link.target !== "_self")) return;
    const url = new URL(link.href);
    if (url.origin !== window.location.origin || typeof HTMLDialogElement.prototype.showModal !== "function") return;
    event.preventDefault();

    const dialog = document.createElement("dialog");
    dialog.className = "popover";
    dialog.setAttribute("aria-label", link.textContent.trim() || "Page preview");
    const toolbar = document.createElement("div");
    toolbar.className = "popover-toolbar";
    const title = document.createElement("strong");
    title.textContent = link.textContent.trim() || "Page preview";
    const actions = document.createElement("div");
    actions.className = "actions";
    const open = document.createElement("a");
    open.href = url.href;
    open.target = "_blank";
    open.rel = "noopener";
    open.textContent = "Open in new tab";
    const close = document.createElement("button");
    close.type = "button";
    close.className = "secondary";
    close.textContent = "Close";
    actions.append(open, close);
    toolbar.append(title, actions);
    const status = document.createElement("p");
    status.className = "popover-status";
    status.setAttribute("role", "status");
    status.textContent = "Loading...";
    const frame = document.createElement("iframe");
    frame.className = "popover-frame";
    frame.title = title.textContent;
    frame.addEventListener("load", () => {
      const page = frame.contentDocument;
      if (!page?.querySelector("main")) {
        status.hidden = false;
        status.setAttribute("role", "alert");
        status.textContent = "This page could not be displayed here. Use Open in new tab to view it directly.";
        return;
      }
      status.hidden = true;
      page.documentElement.classList.add("popover-page");
      page.documentElement.dataset.theme = document.documentElement.dataset.theme;
      for (const anchor of page.querySelectorAll("a[href]")) {
        if (new URL(anchor.href).pathname !== url.pathname) anchor.target = "_top";
      }
      page.addEventListener("click", (frameEvent) => {
        if (!normalClick(frameEvent) || !frameEvent.target.closest("a[data-popover-close]")) return;
        frameEvent.preventDefault();
        dialog.close();
      });
      page.addEventListener("keydown", (frameEvent) => {
        if (frameEvent.key === "Escape" && !frameEvent.defaultPrevented) dialog.close();
      });
    });
    frame.addEventListener("error", () => {
      status.hidden = false;
      status.setAttribute("role", "alert");
      status.textContent = "The page could not be loaded. Use Open in new tab to try opening it directly.";
    });
    frame.src = url.href;
    dialog.append(toolbar, status, frame);
    close.addEventListener("click", () => dialog.close());
    dialog.addEventListener("click", (backdropEvent) => {
      if (backdropEvent.target !== dialog) return;
      const bounds = dialog.getBoundingClientRect();
      if (backdropEvent.clientX < bounds.left || backdropEvent.clientX > bounds.right ||
          backdropEvent.clientY < bounds.top || backdropEvent.clientY > bounds.bottom) dialog.close();
    });
    dialog.addEventListener("close", () => {
      dialog.remove();
      const opener = link.isConnected ? link :
        [...document.querySelectorAll("a[data-popover][href]")].find((candidate) => candidate.href === url.href);
      opener?.focus({ preventScroll: true });
      if (link.dataset.popoverRefresh) refreshRegion(link.dataset.popoverRefresh);
    }, { once: true });
    document.body.append(dialog);
    dialog.showModal();
  });
})();
