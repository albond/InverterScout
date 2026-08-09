(() => {
  const root = document.documentElement;
  const themeToggle = document.querySelector("[data-theme-toggle]");
  const motionAllowed = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const interactiveSelector = "button, .button, .nav-link, .theme-toggle";
  const storedTheme = () => {
    try {
      return window.localStorage.getItem("inverterscout-theme");
    } catch {
      return null;
    }
  };

  const storeTheme = (theme) => {
    try {
      window.localStorage.setItem("inverterscout-theme", theme);
    } catch {
      return;
    }
  };

  const updateThemeControl = () => {
    if (!themeToggle) return;
    const nextTheme = root.dataset.theme === "dark" ? "light" : "dark";
    const label =
      nextTheme === "light"
        ? themeToggle.dataset.labelLight
        : themeToggle.dataset.labelDark;
    themeToggle.setAttribute("aria-label", label);
    themeToggle.setAttribute("title", label);
  };

  updateThemeControl();

  themeToggle?.addEventListener("click", () => {
    const nextTheme = root.dataset.theme === "dark" ? "light" : "dark";
    root.dataset.theme = nextTheme;
    storeTheme(nextTheme);
    updateThemeControl();
  });

  const setPressOrigin = (control, event) => {
    const bounds = control.getBoundingClientRect();
    control.style.setProperty("--press-x", `${event.clientX - bounds.left}px`);
    control.style.setProperty("--press-y", `${event.clientY - bounds.top}px`);
  };

  const releaseControl = (control) => {
    if (!control) return;
    control.classList.remove("is-pressed");
    if (!motionAllowed) return;
    control.classList.remove("is-releasing");
    void control.offsetWidth;
    control.classList.add("is-releasing");
    window.setTimeout(() => control.classList.remove("is-releasing"), 560);
  };

  document.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    const control = event.target.closest(interactiveSelector);
    if (!control || control.matches(":disabled, [aria-disabled='true']")) return;
    setPressOrigin(control, event);
    control.classList.remove("is-releasing");
    control.classList.add("is-pressed");
    control.setPointerCapture?.(event.pointerId);
  });

  document.addEventListener("pointerup", (event) => {
    releaseControl(event.target.closest(interactiveSelector));
  });

  document.addEventListener("pointercancel", (event) => {
    releaseControl(event.target.closest(interactiveSelector));
  });

  const cardPointerAllowed =
    motionAllowed && window.matchMedia("(hover: hover) and (pointer: fine)").matches;

  if (cardPointerAllowed) {
    document.querySelectorAll(".energy-card, .metric-card, .device-card").forEach((card) => {
      let animationFrame = 0;
      let pointerX = 0;
      let pointerY = 0;

      const renderPointerPosition = () => {
        const bounds = card.getBoundingClientRect();
        const relativeX = Math.min(Math.max(pointerX - bounds.left, 0), bounds.width);
        const relativeY = Math.min(Math.max(pointerY - bounds.top, 0), bounds.height);
        const horizontal = relativeX / bounds.width - 0.5;
        const vertical = relativeY / bounds.height - 0.5;

        card.style.setProperty("--card-glint-x", `${relativeX}px`);
        card.style.setProperty("--card-glint-y", `${relativeY}px`);
        card.style.setProperty("--card-tilt-x", `${(-vertical * 2.4).toFixed(2)}deg`);
        card.style.setProperty("--card-tilt-y", `${(horizontal * 2.8).toFixed(2)}deg`);
        card.style.setProperty("--card-icon-x", `${(horizontal * 3).toFixed(2)}px`);
        card.style.setProperty("--card-icon-y", `${(vertical * 3).toFixed(2)}px`);
        animationFrame = 0;
      };

      card.addEventListener("pointerenter", () => {
        card.classList.add("is-pointer-hovered");
      });

      card.addEventListener("pointermove", (event) => {
        pointerX = event.clientX;
        pointerY = event.clientY;
        if (!animationFrame) animationFrame = window.requestAnimationFrame(renderPointerPosition);
      });

      card.addEventListener("pointerleave", () => {
        if (animationFrame) window.cancelAnimationFrame(animationFrame);
        animationFrame = 0;
        card.classList.remove("is-pointer-hovered");
        card.style.setProperty("--card-glint-x", "50%");
        card.style.setProperty("--card-glint-y", "-180px");
        card.style.setProperty("--card-tilt-x", "0deg");
        card.style.setProperty("--card-tilt-y", "0deg");
        card.style.setProperty("--card-icon-x", "0px");
        card.style.setProperty("--card-icon-y", "0px");
      });
    });
  }

  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (event) => {
    if (storedTheme()) return;
    root.dataset.theme = event.matches ? "dark" : "light";
    updateThemeControl();
  });
})();
