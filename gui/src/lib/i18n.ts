import { writable, derived } from "svelte/store";

export type Lang = "tr" | "en";

const DICT: Record<string, { tr: string; en: string }> = {
  Dashboard: { tr: "Gösterge Paneli", en: "Dashboard" },
  Analyze: { tr: "Gezgin", en: "Analyze" },
  Categories: { tr: "Kategoriler", en: "Categories" },
  Clean: { tr: "Temizle", en: "Clean" },
  Purge: { tr: "Artıklar", en: "Purge" },
  Installers: { tr: "Kurulumlar", en: "Installers" },
  Uninstall: { tr: "Kaldır", en: "Uninstall" },
  Optimize: { tr: "Optimize", en: "Optimize" },
  Ports: { tr: "Portlar", en: "Ports" },
  Processes: { tr: "İşlemler", en: "Processes" },
  Startup: { tr: "Başlangıç", en: "Startup" },
  Duplicates: { tr: "Yinelenenler", en: "Duplicates" },
  Maintenance: { tr: "Bakım", en: "Maintenance" },
  Settings: { tr: "Ayarlar", en: "Settings" },
  Help: { tr: "Yardım", en: "Help" },
  General: { tr: "Genel", en: "General" },
  Cleaning: { tr: "Temizlik", en: "Cleaning" },
  System: { tr: "Sistem", en: "System" },
  Other: { tr: "Diğer", en: "Other" },
};

function initial(): Lang {
  if (typeof localStorage !== "undefined") {
    const l = localStorage.getItem("wmole-lang");
    if (l === "tr" || l === "en") return l;
  }
  return "tr";
}

export const lang = writable<Lang>(initial());

export function setLang(l: Lang) {
  lang.set(l);
  if (typeof localStorage !== "undefined") localStorage.setItem("wmole-lang", l);
}

export function toggleLang() {
  lang.update((l) => {
    const next: Lang = l === "tr" ? "en" : "tr";
    if (typeof localStorage !== "undefined") localStorage.setItem("wmole-lang", next);
    return next;
  });
}

/** Reaktif çevirici: $t("Dashboard") */
export const t = derived(lang, ($lang) => (key: string) => {
  const entry = DICT[key];
  return entry ? entry[$lang] : key;
});
