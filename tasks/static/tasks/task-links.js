(() => {
  const pickers = new WeakMap();

  function closeResults(picker) {
    picker.querySelector('[role="listbox"]').hidden = true;
    const input = picker.querySelector("[data-link-search]");
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }

  function highlight(picker, index) {
    const state = pickers.get(picker);
    const options = [...picker.querySelectorAll('[role="option"]')];
    state.active = index;
    options.forEach((option, i) => option.setAttribute("aria-selected", String(i === index)));
    const input = picker.querySelector("[data-link-search]");
    if (options[index]) input.setAttribute("aria-activedescendant", options[index].id);
    else input.removeAttribute("aria-activedescendant");
  }

  function search(picker) {
    const state = pickers.get(picker);
    const input = picker.querySelector("[data-link-search]");
    const query = input.value.trim().toLowerCase();
    const matches = state.options.filter((option) => option.text.toLowerCase().includes(query));
    const results = picker.querySelector('[role="listbox"]');
    results.replaceChildren();
    for (const option of matches.slice(0, 10)) {
      const button = document.createElement("button");
      button.type = "button";
      button.setAttribute("role", "option");
      button.setAttribute("aria-selected", "false");
      button.id = `linked-task-option-${option.value}`;
      button.dataset.linkTargetId = option.value;
      button.textContent = option.text;
      results.append(button);
    }
    results.hidden = matches.length === 0;
    input.setAttribute("aria-expanded", String(matches.length > 0));
    picker.querySelector("[data-link-search-status]").textContent = matches.length === 0 ?
      "No matching tasks." : matches.length > 10 ? "Showing 10 matches. Refine your search to see other tasks." : "";
    highlight(picker, -1);
  }

  function initialize() {
    for (const picker of document.querySelectorAll("[data-task-picker]")) {
      if (pickers.has(picker)) continue;
      const form = picker.closest("form");
      const select = form.querySelector('select[name="target"]');
      if (!select) continue;
      pickers.set(picker, {
        options: [...select.options].filter((option) => option.value).map((option) => ({
          value: option.value, text: option.textContent,
        })),
        active: -1,
      });
      form.querySelector("[data-link-target]").hidden = true;
      form.querySelector("[data-link-submit]").hidden = true;
      form.querySelector("[data-link-instant-note]")?.removeAttribute("hidden");
      picker.hidden = false;
    }
  }

  function selectTarget(picker, option) {
    if (!option) return;
    const form = picker.closest("form");
    form.querySelector('select[name="target"]').value = option.dataset.linkTargetId;
    closeResults(picker);
    form.requestSubmit();
  }

  document.addEventListener("input", (event) => {
    if (event.target.matches("[data-link-search]")) search(event.target.closest("[data-task-picker]"));
  });
  document.addEventListener("focusin", (event) => {
    if (event.target.matches("[data-link-search]")) search(event.target.closest("[data-task-picker]"));
  });
  document.addEventListener("focusout", (event) => {
    const picker = event.target.closest("[data-task-picker]");
    if (picker && !picker.contains(event.relatedTarget)) closeResults(picker);
  });
  document.addEventListener("keydown", (event) => {
    if (!event.target.matches("[data-link-search]")) return;
    const picker = event.target.closest("[data-task-picker]");
    const options = [...picker.querySelectorAll('[role="option"]')];
    const state = pickers.get(picker);
    if (event.key === "Escape") {
      event.preventDefault();
      closeResults(picker);
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (picker.querySelector('[role="listbox"]').hidden) search(picker);
      const count = picker.querySelectorAll('[role="option"]').length;
      if (count) highlight(picker, state.active < 0 ? (event.key === "ArrowDown" ? 0 : count - 1) :
        (state.active + (event.key === "ArrowDown" ? 1 : -1) + count) % count);
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (!picker.querySelector('[role="listbox"]').hidden) selectTarget(picker, options[state.active < 0 ? 0 : state.active]);
    }
  });
  document.addEventListener("click", (event) => {
    const option = event.target.closest("[data-link-target-id]");
    if (option) selectTarget(option.closest("[data-task-picker]"), option);
  });

  initialize();
})();
