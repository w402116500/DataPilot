import { computed, ref } from "vue";

export type ThemeName = "dark" | "light";

export const THEME_STORAGE_KEY = "datapilot.theme";

const theme = ref<ThemeName>("dark");

function isThemeName(value: string | null | undefined): value is ThemeName {
  return value === "dark" || value === "light";
}

export function readStoredTheme(): ThemeName {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    if (isThemeName(stored)) return stored;
  } catch {
    return "dark";
  }
  return "dark";
}

export function applyTheme(next: ThemeName): void {
  theme.value = next;
  const root = document.documentElement;
  root.dataset.theme = next;
  root.style.colorScheme = next;
  try {
    localStorage.setItem(THEME_STORAGE_KEY, next);
  } catch {
    // Private mode or quota: keep the in-memory theme.
  }
}

export function useTheme() {
  const isDark = computed(() => theme.value === "dark");
  function toggleTheme(): void {
    applyTheme(theme.value === "dark" ? "light" : "dark");
  }
  return { theme, isDark, toggleTheme };
}
