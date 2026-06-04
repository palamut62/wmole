# wmole — Özellik Geliştirme & Eksik İşlev Raporu

Tarih: 2026-06-04
Kaynaklar: upstream [tw93/Mole](https://github.com/tw93/Mole) (macOS), web incelemeleri, mevcut `mole.py` + Tauri GUI kodu.

---

## 1. Yöntem

İki şeyi kıyasladım:
1. **Upstream Mole (macOS, `mo`)** — wmole'un köken aldığı araç. Web'den özellik seti çıkarıldı.
2. **wmole (Windows)** — mevcut `mole.py` (TUI/CLI) ve yeni Tauri GUI.

Amaç: hangi özellikler **eksik**, hangileri **geliştirilebilir**, hangi yeni işlev **eklenebilir**.

---

## 2. Parite Durumu (Mole macOS ↔ wmole)

| Mole (macOS) | wmole karşılığı | Durum |
|---|---|---|
| `mo clean` (deep clean + leftovers) | clean + uninstall leftovers | ✅ var |
| `mo uninstall` (app + 52 leftover) | uninstall + leftover tarama | ✅ var |
| `mo optimize` (spotlight/dns/font) | optimize (DNS/Winsock/IP/Update…) | ✅ var (Windows'a uyarlı) |
| `mo analyze` (disk gezgini, vim nav) | Analyze gezgini | ✅ var · ⚠ vim tuşları yok |
| `mo status` (CPU/GPU/mem/net/temp) | Dashboard | ✅ var · ⚠ GPU & per-core yok |
| `mo purge` (node_modules/target…) | purge | ✅ var |
| `mo installer` (büyük kurulumlar) | installers | ✅ var |
| `mo history` | Dashboard "Yapılan Temizlikler" | ✅ var |
| `mo touchid` (sudo için Touch ID) | — | ❌ yok (Windows'ta UAC/admin karşılığı) |
| `mo completion` (shell tab-completion) | completion (PowerShell) | ✅ CLI'da var, GUI'de yok |
| `mo update` / `mo remove` | update / remove | ✅ var |
| `--dry-run` | dry-run | ✅ (optimize'da GUI'de, diğerlerinde backend'de) |
| `--whitelist` (korumalı yollar) | whitelist.txt | ✅ dosya var · ⚠ GUI'de yönetim yok |
| `--json` (otomatik pipe algılama) | --json | ✅ |
| Raycast/Alfred hızlı başlatıcılar | — | ❌ yok (Windows: tray/Task Scheduler) |
| Cron ile haftalık otomatik temizlik | — | ❌ yok (Windows: Task Scheduler) |
| External drive scanning | Analyze "Sürücüler" | ✅ var |
| Show in Finder | "Explorer'da aç" (↗) | ✅ var |

**Sonuç:** Çekirdek işlevlerin neredeyse tamamı wmole'da mevcut. Eksikler çoğunlukla **otomasyon, kısayol ve yönetim arayüzü** katmanında.

---

## 3. Eksik / Geliştirilebilir Özellikler (öncelikli)

### Yüksek değer
1. **Zamanlanmış otomatik temizlik (Windows Task Scheduler)**
   - Mole'da cron ile haftalık temizlik öneriliyor. wmole'da yok.
   - Öneri: GUI'de "Haftalık hızlı temizlik planla" → `schtasks` ile görev oluştur.

2. **Whitelist / Denylist yönetim ekranı (GUI)**
   - Şu an sadece `~/.wmole/whitelist.txt` / `denylist.txt` dosyaları var; elle düzenleniyor.
   - Öneri: Settings ekranı — korumalı yol ekle/çıkar, `large_file_min_mb`, tarama kök yolları.

3. **Tüm yıkıcı akışlarda GUI dry-run anahtarı**
   - Optimize'da var; Clean/Purge/Uninstall'da silmeden önce "önizle" modu eklenebilir (zaten backend destekliyor).

4. **Admin (UAC) yükseltme akışı**
   - Optimize'daki yüksek-riskli aksiyonlar admin gerektiriyor; şu an hata dönüyor.
   - Öneri: "Yönetici olarak yeniden başlat" butonu (ShellExecute `runas`).

### Orta değer
5. **Klavye kısayolları / vim navigasyonu** (Analyze'de j/k, Enter, Space, D) — güç kullanıcılar için.
6. **GPU + per-core CPU + sıcaklık** dashboard'da (Mole gösteriyor; wmole `status` sıcaklığı kısmen var).
7. **Arama/filtre** — uzun tarama listelerinde anlık filtre kutusu.
8. **Kategori bazlı toplu seçim** — "sadece tarayıcı cache'lerini seç" gibi.
9. **Dil değiştirme (TR/EN) GUI'de** — backend i18n var, GUI şu an TR sabit.
10. **completion kurulumu GUI'den** (PowerShell tab-completion).

### Düşük değer / parlatma
11. **Sistem tepsisi (tray) ikonu** — hızlı erişim + arka plan durum.
12. **Bildirim** — uzun işlem bitince Windows toast bildirimi.
13. **Tema** (açık/koyu) — şu an koyu sabit.
14. **Onboarding / ilk açılış** — TUI'deki "first launch quick clean" karşılığı GUI'de yok.

---

## 4. Yeni Eklenebilecek İşlevler (Mole'da bile olmayan)

- **Büyük klasör ısı haritası** (treemap görselleştirme) — Analyze'i DaisyDisk benzeri görsele çıkarır.
- **Yinelenen dosya bulucu** (duplicate finder) — hash tabanlı.
- **Başlangıç programları yöneticisi** (Windows startup apps) — Optimize'a doğal ek.
- **Servis/işlem yöneticisi** — Ports ekranını genişlet.
- **Temizlik öncesi/sonrası disk kazanımı grafiği** — geçmişi görselleştir.

---

## 5. Mevcut Tauri GUI'de Teknik İyileştirmeler

- **Tarama-ortası iptal**: `Scanner.run()` bloke; iptal yalnız sonuç akışında. Scanner'a iptal kancası eklenmeli.
- **Paketleme**: `.msi` bundle henüz üretilmedi (kullanıcı isteğiyle ertelendi).
- **Sidecar boyutu**: PyInstaller exe ~76MB. `--onedir` veya UPX ile küçültülebilir.
- **Hata yüzeyi**: `ev:"error"` olayları toast'a daha tutarlı bağlanmalı (bazı ekranlarda sessiz).
- **Test kapsamı**: serve için 14 test var; Rust köprü ve Svelte bileşen testleri henüz yok.

---

## 6. Önerilen Yol Haritası (kısa)

1. Settings ekranı (whitelist/denylist + eşikler) — en çok istenen yönetim eksiği.
2. Zamanlanmış temizlik (Task Scheduler) + tray + bildirim — "kur ve unut" deneyimi.
3. Admin yükseltme akışı + GUI dry-run her yerde — güvenlik/şeffaflık.
4. Dil değiştirme + klavye kısayolları — erişilebilirlik.
5. Görsel iyileştirmeler (treemap, duplicate finder) — farklılaştırıcı özellikler.

---

## Kaynaklar
- https://github.com/tw93/Mole
- https://www.xda-developers.com/mole-mac-combines-paid-tools/
- https://www.hongkiat.com/blog/mole-mac-cleaner-guide/
- https://dev.to/tumf/mole-the-basics-of-mac-optimization-from-the-terminal-3p36
