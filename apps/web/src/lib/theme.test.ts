import { afterEach, describe, expect, it } from "vitest";

import { applyTheme, readStoredTheme, THEME_STORAGE_KEY, useTheme } from "./theme";

function resetTheme(): void {
  localStorage.removeItem(THEME_STORAGE_KEY);
  applyTheme("dark");
  localStorage.removeItem(THEME_STORAGE_KEY);
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.style.colorScheme = "";
}

describe("theme preference", () => {
  afterEach(resetTheme);

  it("defaults to dark when storage is empty", () => {
    expect(readStoredTheme()).toBe("dark");
  });

  it("rejects invalid stored values", () => {
    localStorage.setItem(THEME_STORAGE_KEY, "system");
    expect(readStoredTheme()).toBe("dark");
  });

  it("reads a stored light preference", () => {
    localStorage.setItem(THEME_STORAGE_KEY, "light");
    expect(readStoredTheme()).toBe("light");
  });

  it("writes dataset, color-scheme, and storage", () => {
    applyTheme("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(document.documentElement.style.colorScheme).toBe("light");
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
  });

  it("toggles between dark and light", () => {
    applyTheme("dark");
    const { toggleTheme, theme, isDark } = useTheme();
    toggleTheme();
    expect(theme.value).toBe("light");
    expect(isDark.value).toBe(false);
    toggleTheme();
    expect(theme.value).toBe("dark");
    expect(isDark.value).toBe(true);
  });
});
