(() => {
  const disclosure = document.querySelector(".header-settings");
  if (!disclosure) return;
  const summary = disclosure.querySelector("summary");

  document.addEventListener("click", (event) => {
    if (disclosure.open && !disclosure.contains(event.target)) disclosure.open = false;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !disclosure.open) return;
    event.preventDefault();
    event.stopPropagation();
    disclosure.open = false;
    summary.focus();
  });
})();
