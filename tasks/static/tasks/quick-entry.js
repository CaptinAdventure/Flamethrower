const quickTaskForm = document.getElementById("quick-task-form");
if (quickTaskForm) {
  const reviewButton = quickTaskForm.querySelector('button[value="edit"]');
  let submitting = false;
  quickTaskForm.addEventListener("submit", (event) => {
    if (submitting) {
      event.preventDefault();
      return;
    }
    submitting = true;
    setTimeout(() => { reviewButton.disabled = true; }, 0);
    document.getElementById("quick-task-status").textContent =
      "Generating a title locally. The first use may take longer while the model downloads.";
  });
  window.addEventListener("pageshow", () => {
    submitting = false;
    reviewButton.disabled = false;
    document.getElementById("quick-task-status").textContent = "";
  });
}
