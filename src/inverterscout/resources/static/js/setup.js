(() => {
  const telegramFields = document.getElementById("telegram-fields");
  const modeInputs = document.querySelectorAll("input[name='telegram_mode']");
  const languageSelect = document.querySelector("[data-language-select]");

  languageSelect?.addEventListener("change", () => {
    window.location.href = `/?lang=${encodeURIComponent(languageSelect.value)}`;
  });

  modeInputs.forEach((radio) => {
    radio.addEventListener("change", () => {
      if (!telegramFields || !radio.checked) return;
      telegramFields.hidden = radio.value !== "enabled";
    });
  });
})();
