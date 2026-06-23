import { writable, get } from "svelte/store";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { toast } from "$lib/toast";
import { tr } from "$lib/i18n";

export type UpdatePhase =
  | "idle"
  | "checking"
  | "available"
  | "downloading"
  | "ready"
  | "error";

export interface UpdateState {
  phase: UpdatePhase;
  current: string;
  latest: string;
  notes: string;
  url: string;
  sha256Url: string;
  size: number;
  pct: number;
  setupPath: string;
  error: string;
}

const init: UpdateState = {
  phase: "idle",
  current: "",
  latest: "",
  notes: "",
  url: "",
  sha256Url: "",
  size: 0,
  pct: 0,
  setupPath: "",
  error: "",
};

export const update = writable<UpdateState>(init);

// Modal görünür mü? Sessiz kontrolde sadece status bar ikonu yanar;
// kullanıcı ikona tıklayınca veya manuel kontrolde modal açılır.
export const modalOpen = writable(false);

export function openUpdateModal() {
  modalOpen.set(true);
}

// İndirme ilerlemesini dinle (Rust → "update-progress").
listen<{ pct: number }>("update-progress", (e) =>
  update.update((s) => ({ ...s, pct: e.payload.pct })),
);

export async function checkForUpdate(silent = true) {
  update.update((s) => ({ ...s, phase: "checking", error: "" }));
  try {
    const r = await invoke<any>("check_update");
    if (r.available) {
      update.set({
        ...init,
        phase: "available",
        current: r.current,
        latest: r.latest,
        notes: r.notes,
        url: r.download_url,
        sha256Url: r.sha256_url,
        size: r.size,
      });
      // Manuel kontrolde modalı aç; sessizde sadece status bar ikonu yanar.
      if (!silent) modalOpen.set(true);
    } else {
      update.update((s) => ({
        ...s,
        phase: "idle",
        current: r.current,
        latest: r.latest,
      }));
      if (!silent) toast(tr("Zaten en güncel sürümdesiniz"), "ok");
    }
  } catch (e) {
    update.update((s) => ({ ...s, phase: "error", error: String(e) }));
    if (!silent) toast(tr("Güncelleme kontrolü başarısız") + ": " + e, "err");
  }
}

export async function downloadUpdate() {
  const s = get(update);
  update.update((v) => ({ ...v, phase: "downloading", pct: 0, error: "" }));
  try {
    const path = await invoke<string>("download_update", {
      url: s.url,
      sha256Url: s.sha256Url,
      total: s.size,
    });
    update.update((v) => ({ ...v, phase: "ready", setupPath: path, pct: 100 }));
  } catch (e) {
    const msg = String(e);
    const friendly = msg.startsWith("checksum-mismatch")
      ? tr("İndirilen dosya bozuk (doğrulama hatası)")
      : msg.includes("disk")
        ? tr("Disk alanı yetersiz veya yazma hatası")
        : msg.includes("network") || msg.includes("stream") || msg.includes("http")
          ? tr("Ağ hatası")
          : tr("İndirme başarısız");
    update.update((v) => ({ ...v, phase: "error", error: friendly }));
    toast(friendly, "err");
  }
}

export async function applyUpdate() {
  const s = get(update);
  try {
    await invoke("install_update", { setupPath: s.setupPath });
  } catch (e) {
    toast(tr("Kurulum başlatılamadı") + ": " + e, "err");
  }
}

export function dismissUpdate() {
  // Modalı kapat; güncelleme durumunu koru ki status bar ikonu yanmaya devam etsin.
  modalOpen.set(false);
}
