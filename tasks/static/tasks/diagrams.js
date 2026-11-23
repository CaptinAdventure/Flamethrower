(() => {
  const rendererUrl = new URL(document.currentScript.dataset.rendererUrl, window.location.href);
  const frames = new Map();

  function fail(frame, state, message) {
    clearTimeout(state.timeout);
    state.notice.textContent = message;
    state.notice.setAttribute("role", "alert");
    state.details.open = true;
    frame.hidden = true;
  }

  function render(frame, source) {
    let background = "transparent";
    for (let element = frame.parentElement; element; element = element.parentElement) {
      const candidate = getComputedStyle(element).backgroundColor;
      if (candidate !== "transparent" && candidate !== "rgba(0, 0, 0, 0)") {
        background = candidate;
        break;
      }
    }
    frame.contentWindow.postMessage({
      type: "flamethrower:render-mermaid",
      source,
      dark: document.documentElement.dataset.theme === "dark",
      background,
      color: getComputedStyle(frame.parentElement).color,
    }, "*");
  }

  function scan() {
    for (const [frame, state] of frames) {
      if (!frame.isConnected) {
        clearTimeout(state.timeout);
        frames.delete(frame);
      }
    }
    for (const code of document.querySelectorAll(".markdown pre > code.language-mermaid")) {
      const pre = code.parentElement;
      if (pre.closest("[data-mermaid-container], details:not([open])") || !pre.getClientRects().length) continue;
      const container = document.createElement("div");
      container.dataset.mermaidContainer = "";
      const frame = document.createElement("iframe");
      frame.className = "mermaid-diagram";
      frame.title = "Mermaid diagram";
      frame.setAttribute("sandbox", "allow-scripts");
      // Opaque-origin frames keep agent diagrams isolated from the application.
      frame.srcdoc = `<!doctype html><meta charset="utf-8">
        <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src ${rendererUrl.href}; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
        <style>body{margin:8px;background:transparent}#diagram svg{display:block;max-width:100%;height:auto}</style>
        <div id="diagram"></div><script src="${rendererUrl.href}"></script>`;
      const notice = document.createElement("p");
      notice.className = "muted";
      notice.setAttribute("role", "status");
      notice.textContent = "Rendering diagram…";
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "Diagram source";
      pre.replaceWith(container);
      details.append(summary, pre);
      container.append(notice, frame, details);
      const state = { source: code.textContent, notice, details, ready: false };
      state.timeout = setTimeout(() => fail(frame, state, "Diagram renderer did not load. Refresh the page to retry."), 15000);
      frames.set(frame, state);
    }
  }

  window.addEventListener("message", (event) => {
    if (event.data?.type !== "flamethrower:mermaid") return;
    for (const [frame, state] of frames) {
      if (event.source !== frame.contentWindow) continue;
      if (event.data.ready) {
        clearTimeout(state.timeout);
        state.ready = true;
        render(frame, state.source);
      } else if (typeof event.data.error === "string") {
        fail(frame, state, event.data.error);
      } else if (!frame.hidden && typeof event.data.height === "number" && Number.isFinite(event.data.height)) {
        frame.style.height = `${Math.max(32, Math.min(10000, event.data.height))}px`;
        state.notice.textContent = "";
      }
      break;
    }
  });

  document.addEventListener("flamethrower:content-updated", scan);
  document.addEventListener("toggle", scan, true);
  new MutationObserver(() => {
    for (const [frame, state] of frames) {
      if (state.ready && !frame.hidden) render(frame, state.source);
    }
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  scan();
})();
