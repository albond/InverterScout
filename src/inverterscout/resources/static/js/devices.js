(() => {
  const page = document.querySelector("[data-devices-page]");
  if (!page) return;

  const provider = document.getElementById("provider");
  const tapoFields = document.getElementById("tapo-fields");
  const tuyaFields = document.getElementById("tuya-fields");
  const addForm = document.getElementById("add-device");
  const toast = page.querySelector("[data-device-toast]");
  let toastTimer;

  const showToast = (message, failed = false) => {
    if (!toast) return;
    window.clearTimeout(toastTimer);
    toast.querySelector("span").textContent = message;
    toast.classList.toggle("is-error", failed);
    toast.hidden = false;
    requestAnimationFrame(() => toast.classList.add("is-visible"));
    toastTimer = window.setTimeout(() => {
      toast.classList.remove("is-visible");
      window.setTimeout(() => {
        toast.hidden = true;
      }, 220);
    }, 2800);
  };

  const updateCard = (device) => {
    const card = page.querySelector(`[data-device-card][data-device-id="${CSS.escape(device.id)}"]`);
    if (!card) return;
    const statePill = card.querySelector("[data-device-state]");
    const stateText = card.querySelector("[data-device-state-text]");
    const dot = card.querySelector("[data-device-dot]");
    const power = card.querySelector("[data-device-power-value]");
    const onButton = card.querySelector("[data-device-action='turn_on']");
    const offButton = card.querySelector("[data-device-action='turn_off']");
    const stateName = device.online ? (device.on ? "on" : "off") : "offline";
    const previousState = card.dataset.state;

    card.dataset.state = stateName;
    if (previousState && previousState !== "loading" && previousState !== stateName) {
      card.classList.remove("is-state-changing");
      void card.offsetWidth;
      card.classList.add("is-state-changing");
      window.setTimeout(() => card.classList.remove("is-state-changing"), 720);
    }
    statePill?.classList.remove("is-loading", "is-on", "is-off", "is-offline");
    statePill?.classList.add(`is-${stateName}`);
    dot?.classList.remove("ok", "bad");
    dot?.classList.add(device.online ? "ok" : "bad");
    if (stateText) {
      stateText.textContent = device.online
        ? device.on
          ? page.dataset.onLabel
          : page.dataset.offLabel
        : page.dataset.offlineLabel;
    }
    if (power) power.textContent = device.current_power ?? "—";
    if (onButton) onButton.disabled = !device.online || device.on === true;
    if (offButton) offButton.disabled = !device.online || device.on !== true;
  };

  const refreshStates = async () => {
    const response = await fetch("/devices/states", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error("state request failed");
    const payload = await response.json();
    (payload.devices || []).forEach(updateCard);
  };

  provider?.addEventListener("change", () => {
    tapoFields.hidden = provider.value !== "tapo";
    tuyaFields.hidden = provider.value !== "tuya";
    tapoFields.querySelector("input")?.toggleAttribute("required", provider.value === "tapo");
    tuyaFields.querySelector("input")?.toggleAttribute("required", provider.value === "tuya");
  });

  addForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = addForm.querySelector("button[type='submit']");
    submitButton?.setAttribute("aria-busy", "true");
    submitButton?.setAttribute("disabled", "");

    try {
      const response = await fetch(addForm.action, {
        method: "POST",
        body: new FormData(addForm),
        headers: { Accept: "application/json" },
      });
      const payload = await response.json();
      if (payload.ok) {
        window.location.reload();
        return;
      }
      showToast(payload.error || page.dataset.errorLabel, true);
    } catch {
      showToast(page.dataset.errorLabel, true);
    } finally {
      submitButton?.removeAttribute("aria-busy");
      submitButton?.removeAttribute("disabled");
    }
  });

  page.querySelectorAll("[data-device-action-form]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const submitter = event.submitter;
      if (!submitter) return;
      const buttons = form.querySelectorAll("button");
      buttons.forEach((button) => {
        button.disabled = true;
      });
      submitter.setAttribute("aria-busy", "true");
      const data = new FormData(form);
      data.set("action", submitter.value);

      try {
        const response = await fetch(form.getAttribute("action") || "/devices", {
          method: "POST",
          body: data,
          headers: { Accept: "application/json" },
        });
        if (!response.ok) throw new Error("command request failed");
        showToast(page.dataset.commandLabel);
        window.setTimeout(() => refreshStates().catch(() => {}), 260);
      } catch {
        showToast(page.dataset.errorLabel, true);
        window.setTimeout(() => refreshStates().catch(() => {}), 120);
      } finally {
        submitter.removeAttribute("aria-busy");
      }
    });
  });

  refreshStates().catch(() => {});
})();
