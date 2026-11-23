(() => {
  let dragged = null;

  document.addEventListener("dragstart", (event) => {
    const card = event.target.closest(".task-card[draggable]");
    if (!card) return;
    dragged = card;
    document.body.classList.add("task-dragging");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", card.dataset.taskId);
  });

  document.addEventListener("dragover", (event) => {
    const target = event.target.closest(".task-card[draggable]");
    if (!dragged || !target || target === dragged || target.closest(".lane") !== dragged.closest(".lane")) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    document.querySelectorAll(".drag-target").forEach((item) => item.classList.remove("drag-target"));
    target.classList.add("drag-target");
  });

  document.addEventListener("drop", (event) => {
    const target = event.target.closest(".task-card[draggable]");
    if (!dragged || !target || target === dragged || target.closest(".lane") !== dragged.closest(".lane")) return;
    event.preventDefault();
    const form = dragged.querySelector("form").cloneNode(true);
    for (const button of form.querySelectorAll("button")) button.remove();
    const fields = {
      target: target.dataset.taskId,
      direction: event.clientY < target.getBoundingClientRect().top + target.offsetHeight / 2 ? "before" : "after",
    };
    for (const [name, value] of Object.entries(fields)) {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = name;
      input.value = value;
      form.append(input);
    }
    document.body.append(form);
    form.submit();
  });

  document.addEventListener("dragend", () => {
    dragged = null;
    document.body.classList.remove("task-dragging");
    document.querySelectorAll(".drag-target").forEach((item) => item.classList.remove("drag-target"));
  });
})();
