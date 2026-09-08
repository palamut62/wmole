import { writable } from "svelte/store";

/**
 * Two independent axes, mirroring pc_cleaner's model:
 *
 *   - layout theme — changes the *structure* of the Dashboard and the scan
 *     views (dense rows vs. card grid vs. bento tiles vs. HUD gauges) and
 *     brings its own accent palette.
 *   - mode — light or dark, applied on top of whichever layout is active.
 *
 * They are written to `data-theme` and `data-mode` on <html>, so all of the
 * theming lives in CSS and no component has to subscribe to a store to be
 * styled.
 */
export const LAYOUT_THEMES = ["fluent", "linear", "cards", "bento", "hud"] as const;
export type LayoutTheme = (typeof LAYOUT_THEMES)[number];

export const MODES = ["dark", "light"] as const;
export type Mode = (typeof MODES)[number];

export const THEME_LABELS: Record<LayoutTheme, string> = {
  fluent: "Fluent",
  linear: "Linear",
  cards: "Cards",
  bento: "Bento",
  hud: "HUD",
};

/** How each layout presents a list of candidates. */
export const THEME_PRESENTATION: Record<LayoutTheme, "rows" | "grid"> = {
  fluent: "rows",
  linear: "rows",
  hud: "rows",
  cards: "grid",
  bento: "grid",
};

/**
 * HUD is a single fixed dark identity — a cyan-on-black instrument panel has
 * no meaningful light variant, so the mode switch is hidden for it.
 */
export const FIXED_DARK: readonly LayoutTheme[] = ["hud"];

const THEME_KEY = "wmole-theme";
const MODE_KEY = "wmole-mode";
const DENSITY_KEY = "wmole-density";

function isLayout(value: unknown): value is LayoutTheme {
  return typeof value === "string" && (LAYOUT_THEMES as readonly string[]).includes(value);
}

function isMode(value: unknown): value is Mode {
  return typeof value === "string" && (MODES as readonly string[]).includes(value);
}

function stored(key: string): string | null {
  if (typeof localStorage === "undefined") return null;
  return localStorage.getItem(key);
}

function initialTheme(): LayoutTheme {
  const saved = stored(THEME_KEY);
  if (isLayout(saved)) return saved;
  // Pre-0.9 installs stored a colour name here; fall back rather than crash.
  return "fluent";
}

function initialMode(): Mode {
  const saved = stored(MODE_KEY);
  if (isMode(saved)) return saved;
  // The old key doubled as the light/dark choice, so honour it once.
  if (stored(THEME_KEY) === "light") return "light";
  return "dark";
}

function initialDensity(): boolean {
  const saved = stored(DENSITY_KEY);
  // Compact is the default: this is a dense, list-heavy maintenance tool.
  return saved === null ? true : saved === "compact";
}

export const theme = writable<LayoutTheme>(initialTheme());
export const mode = writable<Mode>(initialMode());
export const compact = writable<boolean>(initialDensity());

export function apply(t: LayoutTheme, m: Mode, dense: boolean) {
  if (typeof document !== "undefined") {
    const root = document.documentElement;
    root.setAttribute("data-theme", t);
    // A fixed-identity theme always renders dark regardless of the mode.
    root.setAttribute("data-mode", FIXED_DARK.includes(t) ? "dark" : m);
    root.setAttribute("data-density", dense ? "compact" : "cozy");
  }
  if (typeof localStorage !== "undefined") {
    localStorage.setItem(THEME_KEY, t);
    localStorage.setItem(MODE_KEY, m);
    localStorage.setItem(DENSITY_KEY, dense ? "compact" : "cozy");
  }
}

/** Re-apply from the current store values — used on mount. */
export function applyCurrent() {
  let t: LayoutTheme = "fluent";
  let m: Mode = "dark";
  let d = true;
  theme.subscribe((v) => (t = v))();
  mode.subscribe((v) => (m = v))();
  compact.subscribe((v) => (d = v))();
  apply(t, m, d);
}

export function setTheme(next: LayoutTheme) {
  if (!isLayout(next)) return;
  theme.set(next);
  applyCurrent();
}

export function toggleMode() {
  mode.update((m) => (m === "dark" ? "light" : "dark"));
  applyCurrent();
}

export function toggleDensity() {
  compact.update((c) => !c);
  applyCurrent();
}
