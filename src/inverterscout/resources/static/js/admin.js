(() => {
  document.querySelectorAll("[data-user-row]").forEach((row) => {
    const safeId = String(row.dataset.userId || "").replace(/[^a-zA-Z0-9_-]/g, "");
    if (safeId) row.style.viewTransitionName = `telegram-user-${safeId}`;

    row.querySelectorAll("form").forEach((form) => {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const button = event.submitter;
        button?.setAttribute("aria-busy", "true");
        button?.setAttribute("disabled", "");
        row.classList.add("is-transitioning");
        window.setTimeout(() => HTMLFormElement.prototype.submit.call(form), 150);
      });
    });
  });
})();
