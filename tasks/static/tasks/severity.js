(() => {
  const closers = new WeakMap();
  const closePickers = (target = null) => {
    document.querySelectorAll("[data-severity-picker]").forEach((picker) => {
      if (!target || !picker.contains(target)) closers.get(picker)?.();
    });
  };
  const initialize = () => {
    document.querySelectorAll("[data-severity-picker]").forEach((picker, index) => {
      if (picker.dataset.severityEnhanced) return;
      const select = picker.querySelector("select");
      const toggle = picker.querySelector("[data-severity-toggle]");
      const menu = picker.querySelector("[data-severity-options]");
      const value = picker.querySelector("[data-severity-value]");
      const error = picker.querySelector("[data-severity-error]");
      const options = [...picker.querySelectorAll("[data-severity-choice]")];
      if (!select || !toggle || !menu || !value || !error || !options.length) return;

      const id = select.id || `severity_${index}`;
      select.id = `${id}_native`;
      toggle.id = id;
      menu.id = `${id}_options`;
      error.id = `${id}_picker_error`;
      toggle.setAttribute("aria-controls", menu.id);
      toggle.setAttribute("aria-required", String(select.required));
      toggle.disabled = select.disabled;
      for (const attribute of ["aria-describedby", "aria-invalid"]) {
        if (select.hasAttribute(attribute)) toggle.setAttribute(attribute, select.getAttribute(attribute));
      }
      options.forEach((option, optionIndex) => {
        option.id = `${id}_option_${optionIndex}`;
        option.tabIndex = -1;
      });
      select.value = picker.dataset.severityCurrent;
      let active = 0;
      let search = "";
      let searchedAt = 0;

      const close = () => {
        menu.hidden = true;
        toggle.setAttribute("aria-expanded", "false");
        toggle.removeAttribute("aria-activedescendant");
        search = "";
      };
      const activate = (index) => {
        active = (index + options.length) % options.length;
        options.forEach((option, optionIndex) => option.classList.toggle("is-active", optionIndex === active));
        toggle.setAttribute("aria-activedescendant", options[active].id);
        options[active].scrollIntoView({ block: "nearest" });
      };
      const open = () => {
        closePickers();
        menu.hidden = false;
        toggle.setAttribute("aria-expanded", "true");
        const selected = options.findIndex((option) => option.dataset.severityChoice === select.value);
        activate(selected < 0 ? 0 : selected);
      };
      const sync = () => {
        const selected = options.find((option) => option.dataset.severityChoice === select.value);
        options.forEach((option) => option.setAttribute("aria-selected", String(option === selected)));
        if (selected) value.replaceChildren(selected.querySelector(".severity").cloneNode(true));
        error.hidden = true;
        error.textContent = "";
        const description = select.getAttribute("aria-describedby");
        if (description) toggle.setAttribute("aria-describedby", description);
        else toggle.removeAttribute("aria-describedby");
        if (select.validity.valid) toggle.removeAttribute("aria-invalid");
      };
      const choose = (option) => {
        select.value = option.dataset.severityChoice;
        select.dispatchEvent(new Event("change", { bubbles: true }));
        close();
        toggle.focus();
      };

      toggle.addEventListener("click", () => menu.hidden ? open() : close());
      toggle.addEventListener("keydown", (event) => {
        if (event.ctrlKey || event.metaKey || event.altKey) return;
        if (event.key === "Escape" || event.key === "Tab") {
          if (event.key === "Escape" && !menu.hidden) event.preventDefault();
          close();
          return;
        }
        if (["ArrowDown", "ArrowUp", "Home", "End", "Enter", " "].includes(event.key)) {
          event.preventDefault();
          if (menu.hidden) {
            open();
          } else if (event.key === "Enter" || event.key === " ") {
            choose(options[active]);
            return;
          } else if (event.key === "ArrowDown") activate(active + 1);
          else if (event.key === "ArrowUp") activate(active - 1);
          if (event.key === "Home") activate(0);
          if (event.key === "End") activate(options.length - 1);
        } else if (event.key.length === 1 && /^[a-z]$/i.test(event.key)) {
          event.preventDefault();
          if (menu.hidden) open();
          const now = Date.now();
          search = (now - searchedAt < 500 ? search : "") + event.key.toLowerCase();
          searchedAt = now;
          const match = options.findIndex((option) => option.textContent.trim().toLowerCase().startsWith(search));
          if (match >= 0) activate(match);
        }
      });
      options.forEach((option, optionIndex) => {
        option.addEventListener("click", () => choose(option));
        option.addEventListener("pointermove", () => activate(optionIndex));
      });
      select.addEventListener("change", sync);
      select.addEventListener("invalid", (event) => {
        event.preventDefault();
        close();
        error.textContent = "Choose a severity.";
        error.hidden = false;
        toggle.setAttribute("aria-invalid", "true");
        toggle.setAttribute("aria-describedby", [select.getAttribute("aria-describedby"), error.id].filter(Boolean).join(" "));
        toggle.focus();
      });
      closers.set(picker, close);
      sync();
      picker.querySelector("[data-severity-native]").hidden = true;
      toggle.hidden = false;
      picker.dataset.severityEnhanced = "true";
    });
  };
  initialize();
  document.addEventListener("click", (event) => closePickers(event.target));
  document.addEventListener("flamethrower:content-updated", initialize);
})();
