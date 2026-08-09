(() => {
  let storedTheme = null;
  try {
    storedTheme = window.localStorage.getItem("inverterscout-theme");
  } catch {
    storedTheme = null;
  }
  const systemTheme = window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
  document.documentElement.dataset.theme =
    storedTheme === "light" || storedTheme === "dark" ? storedTheme : systemTheme;
})();
