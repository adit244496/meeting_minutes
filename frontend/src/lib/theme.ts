import { useCallback, useEffect, useState } from "react";

export type Theme = "light" | "dark" | "system";

const KEY = "mm.theme";

/** Stamp (or clear) data-theme on <html>. "system" leaves it unstamped so the
 *  CSS falls through to prefers-color-scheme. */
function apply(theme: Theme) {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

function stored(): Theme {
  try {
    const value = localStorage.getItem(KEY);
    if (value === "light" || value === "dark" || value === "system") return value;
  } catch {
    // Private browsing or blocked site data - fall through to system.
  }
  return "system";
}

/** Applied before React mounts, so there is no flash of the wrong theme. */
export function initTheme() {
  apply(stored());
}

export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(stored);

  useEffect(() => {
    apply(theme);
  }, [theme]);

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next);
    try {
      localStorage.setItem(KEY, next);
    } catch {
      // Not persisting is survivable; the choice still applies this session.
    }
  }, []);

  /** What is actually on screen right now, resolving "system". */
  const resolved: Exclude<Theme, "system"> =
    theme === "system"
      ? window.matchMedia?.("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light"
      : theme;

  const cycle = useCallback(() => {
    setTheme(resolved === "dark" ? "light" : "dark");
  }, [resolved, setTheme]);

  return { theme, resolved, setTheme, cycle };
}
