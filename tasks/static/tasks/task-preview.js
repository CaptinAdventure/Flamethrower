(() => {
  let active = null;
  let timer = null;
  let closeTimer = null;
  let dismissed = null;
  let nextId = 0;

  function close() {
    clearTimeout(timer);
    clearTimeout(closeTimer);
    if (!active) return;
    active.preview.hidden = true;
    if (active.title !== null) active.trigger.setAttribute("title", active.title);
    if (active.description) active.trigger.setAttribute("aria-describedby", active.description);
    else active.trigger.removeAttribute("aria-describedby");
    active = null;
  }

  function position() {
    if (!active) return;
    if (!active.trigger.isConnected) {
      close();
      return;
    }
    const bounds = active.trigger.getBoundingClientRect();
    const preview = active.preview;
    const gap = 8;
    preview.style.left = `${Math.max(gap, Math.min(bounds.left, window.innerWidth - preview.offsetWidth - gap))}px`;
    const below = bounds.bottom + gap;
    const top = below + preview.offsetHeight <= window.innerHeight - gap
      ? below : Math.max(gap, bounds.top - preview.offsetHeight - gap);
    preview.style.top = `${top}px`;
  }

  function show(trigger) {
    if (!trigger.isConnected) return;
    const row = trigger.closest("[data-task-row]");
    if (!row || row === dismissed) return;
    if (active?.trigger === trigger) return;
    close();
    const preview = row.querySelector("[data-task-preview]");
    if (!preview) return;
    if (!preview.id) preview.id = `task-preview-${++nextId}`;
    const description = trigger.getAttribute("aria-describedby");
    const title = trigger.getAttribute("title");
    trigger.removeAttribute("title");
    active = { row, trigger, preview, description, title };
    trigger.setAttribute("aria-describedby", [description, preview.id].filter(Boolean).join(" "));
    preview.hidden = false;
    position();
  }

  function inActive(target) {
    return target instanceof Node && active && (active.trigger.contains(target) || active.preview.contains(target));
  }

  document.addEventListener("mouseover", (event) => {
    if (inActive(event.target)) clearTimeout(closeTimer);
    const trigger = event.target.closest("[data-task-preview-trigger]");
    if (!trigger || trigger.contains(event.relatedTarget)) return;
    clearTimeout(timer);
    timer = setTimeout(() => show(trigger), 250);
  });
  document.addEventListener("mouseout", (event) => {
    const trigger = event.target.closest("[data-task-preview-trigger]");
    if (trigger && !trigger.contains(event.relatedTarget)) clearTimeout(timer);
    if (inActive(event.target) && !inActive(event.relatedTarget) && document.activeElement !== active.trigger) {
      closeTimer = setTimeout(close, 200);
    }
    if (dismissed && !dismissed.contains(event.relatedTarget)) dismissed = null;
  });
  document.addEventListener("focusin", (event) => {
    const trigger = event.target.closest("[data-task-preview-trigger]");
    if (trigger) {
      dismissed = null;
      show(trigger);
    } else if (active && !inActive(event.target)) close();
  });
  document.addEventListener("focusout", (event) => {
    if (active && event.target === active.trigger && !inActive(event.relatedTarget) && !active.row.matches(":hover")) close();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && active) {
      event.preventDefault();
      dismissed = active.row;
      close();
    }
  });
  window.addEventListener("resize", position);
  window.addEventListener("scroll", position, true);
  document.addEventListener("flamethrower:content-updated", () => {
    if (active && !active.trigger.isConnected) close();
  });
})();
