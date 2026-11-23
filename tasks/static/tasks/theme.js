(() => {
  const storageKey = "flamethrower-theme";
  const validChoices = new Set(["light", "dark", "system"]);
  const mediaQuery = typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-color-scheme: dark)")
    : null;
  const root = document.documentElement;
  const controls = [...document.querySelectorAll('input[name="theme"]')];

  function readChoice() {
    try {
      const choice = localStorage.getItem(storageKey);
      return validChoices.has(choice) ? choice : "system";
    } catch (_) {
      return root.dataset.themeChoice || "system";
    }
  }

  function apply(choice) {
    const dark = choice === "dark" || (choice === "system" && mediaQuery?.matches);
    root.dataset.theme = dark ? "dark" : "light";
    root.dataset.themeChoice = choice;
    for (const control of controls) control.checked = control.value === choice;
  }

  function save(choice) {
    try {
      localStorage.setItem(storageKey, choice);
    } catch (_) {
      // Theme still applies for this page when storage is unavailable.
    }
    apply(choice);
  }

  for (const control of controls) {
    control.addEventListener("change", () => save(control.value));
  }
  const handleSystemChange = () => {
    if (readChoice() === "system") apply("system");
  };
  if (mediaQuery && typeof mediaQuery.addEventListener === "function") {
    mediaQuery.addEventListener("change", handleSystemChange);
  } else if (mediaQuery && typeof mediaQuery.addListener === "function") {
    mediaQuery.addListener(handleSystemChange);
  }
  apply(readChoice());
})();
