import { writable } from "svelte/store";

export type Theme = "dark" | "light";

function initial(): Theme {
  if (typeof localStorage !== "undefined") {
    const t = localStorage.getItem("wmole-theme");
    if (t === "light" || t === "dark") return t;
  }
  return "dark";
}

export const theme = writable<Theme>(initial());

export function applyTheme(t: Theme) {
  if (typeof document !== "undefined") {
    document.documentElement.setAttribute("data-theme", t);
  }
  if (typeof localStorage !== "undefined") {
    localStorage.setItem("wmole-theme", t);
  }
}

export function toggleTheme() {
  theme.update((t) => {
    const next: Theme = t === "dark" ? "light" : "dark";
    applyTheme(next);
    return next;
  });
}
