// Theme: dark-first with an explicit light option. index.html applies the
// stored/system choice inline before first paint, so this module only owns the
// toggle and keeps localStorage in sync. Both themes bind the same token names
// (see css/base.css), so nothing else in the app is theme-aware.

const LS_KEY = "tracecast.console.theme.v1";

export const currentTheme = () =>
  document.documentElement.dataset.theme === "light" ? "light" : "dark";

function apply(theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem(LS_KEY, theme);
  } catch (e) {
    console.warn("主题偏好未能保存", e);
  }
  const button = document.getElementById("theme-toggle");
  if (!button) return;
  const dark = theme === "dark";
  button.textContent = dark ? "亮色" : "暗色";
  button.setAttribute("aria-label", dark ? "切换到亮色主题" : "切换到暗色主题");
}

export function initTheme() {
  const button = document.getElementById("theme-toggle");
  if (!button) return;
  apply(currentTheme());
  button.addEventListener("click", () =>
    apply(currentTheme() === "dark" ? "light" : "dark"));
}
