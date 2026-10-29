// Apply the saved theme before first paint; dark is the default.
(() => {
  let theme = "dark";
  try { theme = localStorage.getItem("theme") || "dark"; } catch (_error) {}
  document.documentElement.dataset.theme = theme;
})();

// Switch between dark and light and remember the choice.
function toggleTheme() {
  const theme = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("theme", theme); } catch (_error) {}
  document.dispatchEvent(new Event("themechange"));
}

document.addEventListener("DOMContentLoaded", () => {
  const button = document.getElementById("themeToggle");
  if (!button) return;
  const label = () => button.setAttribute(
    "aria-label",
    document.documentElement.dataset.theme === "light" ? "Switch to dark mode" : "Switch to light mode"
  );
  label();
  button.addEventListener("click", () => { toggleTheme(); label(); });
});
