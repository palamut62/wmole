import { writable, derived, get } from "svelte/store";

export type Lang = "tr" | "en";

/**
 * Anahtar = Türkçe kaynak metin. Değer = İngilizce karşılık.
 * lang="tr" → anahtarın kendisi döner; lang="en" → buradaki çeviri döner.
 * Böylece bileşenlerde metinler okunur kalır, yalnızca İngilizcesi burada tutulur.
 */
const EN: Record<string, string> = {
  // Sidebar grupları
  Genel: "General",
  Temizlik: "Cleaning",
  Sistem: "System",
  Diğer: "Other",
  // Sidebar / sayfa başlıkları
  "Gösterge Paneli": "Dashboard",
  Gezgin: "Analyze",
  Kategoriler: "Categories",
  Temizle: "Clean",
  Artıklar: "Purge",
  Kurulumlar: "Installers",
  Yinelenenler: "Duplicates",
  Kaldır: "Uninstall",
  Optimize: "Optimize",
  Başlangıç: "Startup",
  Portlar: "Ports",
  İşlemler: "Processes",
  Bakım: "Maintenance",
  Ayarlar: "Settings",
  Yardım: "Help",
  // Ortak butonlar/etiketler
  Tara: "Scan",
  "Analize Başla": "Start Analysis",
  "Taranıyor…": "Scanning…",
  "Tümünü Seç": "Select All",
  Hiçbiri: "None",
  Sil: "Delete",
  "Sil…": "Delete…",
  "Önizle": "Preview",
  Yenile: "Refresh",
  Listele: "List",
  "Yükleniyor…": "Loading…",
  Kaydet: "Save",
  Ekle: "Add",
  Vazgeç: "Cancel",
  Onayla: "Confirm",
  "İptal ✕": "Cancel ✕",
  Kapat: "Close",
  "filtrele…": "filter…",
  "ara…": "search…",
  "Dry-run": "Dry-run",
  "Dry-run (önizleme)": "Dry-run (preview)",
  Çalıştır: "Run",
  "Kategori seç…": "Select category…",
  Sürücüler: "Drives",
  Harita: "Treemap",
  "⬆ Üst": "⬆ Up",
  "Büyük dosyalar": "Large files",
  "Explorer'da aç": "Open in Explorer",
  öğe: "items",
  seçili: "selected",
  program: "programs",
  girdi: "entries",
  işlem: "processes",
  kopya: "duplicates",
  // Dashboard
  "Hızlı Tara": "Quick Scan",
  "Hızlı Temizle": "Quick Clean",
  "Yeniden Tara": "Rescan",
  "Temizlik Analizi": "Cleanup Analysis",
  "Yapılan Temizlikler": "Cleanup History",
  "Güvenli temizlik adaylarını tara ve tek tıkla temizle.":
    "Scan safe cleanup candidates and clean with one click.",
  "✓ Temizlenecek önemli bir şey yok": "✓ Nothing significant to clean",
  Çekirdek: "Cores",
  "Sağlık": "Health",
  boş: "free",
  "şarjda": "charging",
  "Toplam boşaltılan:": "Total freed:",
  "Geri kazanılabilir:": "Reclaimable:",
  "Henüz temizlik kaydı yok.": "No cleanup records yet.",
  "Kategori seç": "Select category",
  // Onboarding
  Başla: "Start",
  // Confirm modal
  "Silme Onayı": "Delete Confirmation",
  "Kalıcı sil (Geri Dönüşüm Kutusu'nu atla)":
    "Permanent delete (skip Recycle Bin)",
  "Geri Dönüşüm Kutusu'na Taşı": "Move to Recycle Bin",
  "Kalıcı Sil": "Permanent Delete",
  "Kalıcı Sil (onayla)": "Permanent Delete (confirm)",
  "⚠ Bu işlem GERİ ALINAMAZ. Onaylamak için tekrar \"Kalıcı Sil\" butonuna bas.":
    '⚠ This is IRREVERSIBLE. Press "Permanent Delete" again to confirm.',
  // Optimize
  "Yüksek Riskli İşlemler": "High-Risk Actions",
  "Evet, Çalıştır": "Yes, Run",
  "YÜKSEK RİSK": "HIGH RISK",
  ADMIN: "ADMIN",
  // Uninstall
  "Kaldırma Onayı": "Uninstall Confirmation",
  "Kaldırıcıyı Başlat": "Launch Uninstaller",
  "Seçili dosyaları sil": "Delete selected files",
  "Kalıntılar": "Leftovers",
  "GitHub'daki son sürümü kontrol et veya güncellemeyi uygula.":
    "Check the latest GitHub release or apply the update.",
  "~/.wmole altındaki yapılandırma, log ve cache'i kalıcı olarak siler. Bu işlem geri alınamaz.":
    "Permanently deletes config, logs and cache under ~/.wmole. This cannot be undone.",
  // Ports / Processes
  "Tüm bind'ler": "All binds",
  "Süreci Kapat": "Kill Process",
  "Süreç Kapatma": "Kill Process",
  "Dinleyen localhost portu yok (tümü için admin gerekebilir).":
    "No listening localhost ports (admin may be required for all).",
  // Startup
  "Başlangıç Programları": "Startup Programs",
  "Başlangıçtan Kaldır": "Remove from Startup",
  "Başlangıç girdisi yok.": "No startup entries.",
  // Settings
  "Yönetici Durumu": "Administrator Status",
  "✓ Yönetici olarak çalışıyor": "✓ Running as administrator",
  "○ Standart kullanıcı — bazı optimize işlemleri admin gerektirir":
    "○ Standard user — some optimize actions require admin",
  "Yönetici Olarak Yeniden Başlat": "Restart as Administrator",
  Eşikler: "Thresholds",
  "Büyük dosya alt sınırı (MB)": "Large file minimum (MB)",
  "Whitelist (korunan yollar)": "Whitelist (protected paths)",
  "Denylist (asla silme)": "Denylist (never delete)",
  "Purge kök yolları": "Purge root paths",
  "Zamanlanmış Haftalık Temizlik": "Scheduled Weekly Cleanup",
  "✓ Etkin (Windows Task Scheduler)": "✓ Enabled (Windows Task Scheduler)",
  "○ Devre dışı": "○ Disabled",
  Planla: "Schedule",
  "PowerShell Tamamlama": "PowerShell Completion",
  "Completion Kur": "Install Completion",
  Boş: "Empty",
  Pazar: "Sunday",
  Pazartesi: "Monday",
  Salı: "Tuesday",
  Çarşamba: "Wednesday",
  Perşembe: "Thursday",
  Cuma: "Friday",
  Cumartesi: "Saturday",
  // Maintenance
  Güncelleme: "Update",
  "Kontrol Et (dry-run)": "Check (dry-run)",
  Güncelle: "Update",
  "wmole Durumunu Kaldır": "Remove wmole State",
  "wmole Durumunu Sil": "Delete wmole State",
  "~/.wmole'u Sil": "Delete ~/.wmole",
  // Duplicates
  "Yinelenen Dosyalar": "Duplicate Files",
  "≥1MB dosyalar boyut+MD5 ile karşılaştırılır. Aynı renkli grup = aynı içerik. Her gruptan birini saklamanız önerilir.":
    "Files ≥1MB are compared by size+MD5. Same-colored group = identical content. Keeping one per group is recommended.",
  // Toast / bildirim mesajları
  silindi: "deleted",
  önizlendi: "previewed",
  hata: "errors",
  "kopya silindi": "duplicates deleted",
  "süreç kapatıldı": "processes killed",
  "kalıntı silindi": "leftovers deleted",
  "Ayarlar kaydedildi": "Settings saved",
  "PowerShell completion kuruldu": "PowerShell completion installed",
  "Kurulum başarısız": "Installation failed",
  "Yükseltme başarısız:": "Elevation failed:",
  "Haftalık temizlik planlandı": "Weekly cleanup scheduled",
  "Planlama başarısız": "Scheduling failed",
  "Zamanlama kaldırıldı": "Schedule removed",
  "Kaldırma başarısız": "Removal failed",
  "Güncelleme kontrolü bitti": "Update check complete",
  "Güncelleme denendi": "Update attempted",
  "wmole durumu (~/.wmole) kaldırıldı": "wmole state (~/.wmole) removed",
  "başlangıçtan kaldırıldı": "removed from startup",
  "İşlem başarısız": "Action failed",
  "kaldırıcısı başlatıldı": "uninstaller launched",
  "Önce silinecek dosya seç (registry anahtarları elle)":
    "Select files to delete first (registry keys manually)",
  "Hızlı temizlik:": "Quick clean:",
  "öğe silindi": "items deleted",
  "Dry-run:": "Dry-run:",
  "işlem tamam": "actions done",
  "wmole temizlik": "wmole cleanup",
  // Onay (modal) mesajları
  "için Windows kaldırıcısı başlatılacak. Devam edilsin mi?":
    "— its Windows uninstaller will launch. Continue?",
  "Seçili işlemlerden bazıları YÜKSEK RİSKLİ ve sistemi etkileyebilir:":
    "Some selected actions are HIGH RISK and may affect the system:",
  "Devam edilsin mi?": "Continue?",
  "süreç sonlandırılacak (kill). Kaydedilmemiş veriler kaybolabilir. Devam?":
    "processes will be terminated (kill). Unsaved data may be lost. Continue?",
  "sistem başlangıcından kaldırılacak. Devam?":
    "will be removed from system startup. Continue?",
  "~/.wmole klasörü kalıcı olarak silinecek (config, log, cache). Geri alınamaz. Devam?":
    "The ~/.wmole folder will be permanently deleted (config, logs, cache). Irreversible. Continue?",
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

/** Reaktif olmayan çevirici (toast/fonksiyon içi kullanım için). */
export function tr(key: string): string {
  return get(lang) === "tr" ? key : EN[key] ?? key;
}

export function toggleLang() {
  lang.update((l) => {
    const next: Lang = l === "tr" ? "en" : "tr";
    if (typeof localStorage !== "undefined") localStorage.setItem("wmole-lang", next);
    return next;
  });
}

/** Reaktif çevirici: $t("Türkçe metin") */
export const t = derived(lang, ($lang) => (key: string) =>
  $lang === "tr" ? key : EN[key] ?? key,
);
