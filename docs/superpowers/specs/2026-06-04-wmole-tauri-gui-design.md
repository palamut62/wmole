# wmole Tauri GUI — Tasarım Dokümanı

Tarih: 2026-06-04
Durum: Onaylandı (brainstorming)

## Amaç

Mevcut `rich` tabanlı interaktif TUI'yi (`mole.py`) bir Tauri masaüstü
uygulamasıyla değiştirmek. TUI dashboard mantığı ve görsel kimliği korunur,
ancak:

- Silme işlemlerindeki donmalar giderilir (eşzamanlı, akışlı, iptal edilebilir).
- Menüler arası geçiş takılmaları giderilir (uzun işlem sürerken UI bloke olmaz).
- Onay/uyarı/bilgi için uygulama-içi (in-app) özel modal/toast dialogları kullanılır.
  Tarayıcı native dialogları (`alert`/`confirm`/`prompt`) KESİNLİKLE kullanılmaz.

Hedef: TUI'deki tüm modlarla **tam parite** (analyze, clean, purge, installers,
uninstall, optimize, status/dashboard, ports, update, remove).

## Mimari

Üç katman; her birinin tek görevi var.

```
Tauri Uygulaması (wmole-gui)
  Web Frontend (Svelte+Vite)  <--IPC-->  Rust Çekirdek (ince köprü)
                                              | stdin/stdout (NDJSON)
                                         Python Sidecar (wmole.exe serve)
```

- **Frontend (Svelte + Vite):** Tüm görsel. TUI estetiği (koyu tema, monospace,
  panel/grid düzeni). İş mantığı yok — olay gönderir, NDJSON olaylarını gösterir,
  modal dialoglarla onay alır.
- **Rust çekirdeği (ince köprü):** Sidecar sürecini başlatır/yönetir, frontend
  isteklerini sidecar stdin'ine yazar, stdout NDJSON satırlarını parse edip
  frontend'e Tauri event olarak yayınlar. İş mantığı yok — taşıma + süreç yaşam
  döngüsü + iptal sinyali.
- **Python sidecar (`mole.py serve`):** Mevcut tüm mantık burada kalır. Yeni
  `serve` modu istekleri okur, `started`/`progress`/`item`/`done`/`error` olayları
  akıtır. Silme yine `delete_path` güvenlik kurallarından geçer. İş mantığı
  yeniden yazılmaz.

**Sınır gerekçesi:** Her katman bağımsız test edilebilir — Python `serve`
protokolü GUI olmadan pipe ile; Rust köprüsü sahte sidecar ile; frontend sahte
event akışıyla. Mevcut `mole.py` mantığı dokunulmadan korunur, risk minimumdadır.

## `serve` Protokolü

`wmole serve` ile başlar. stdin'den satır-başına-bir-JSON istek okur, stdout'a
NDJSON olayları yazar. Her isteğin `id`'si vardır; tüm olaylar o `id`'yi taşır.

### İstekler (frontend → sidecar)

```json
{"id": "req-42", "op": "scan", "mode": "clean", "paths": ["C:\\src"], "dry_run": false}
{"id": "req-43", "op": "delete", "targets": ["C:\\src\\node_modules"], "permanent": false}
{"id": "req-44", "op": "status"}
{"id": "req-42", "op": "cancel"}
```

`op` değerleri: `scan` (mode: clean/purge/installers/analyze), `delete`, `status`,
`uninstall_list`, `uninstall_run`, `leftovers`, `optimize_list`, `optimize_run`,
`ports_list`, `ports_kill`, `update`, `remove`, `cancel`, `ping`.

### Olaylar (sidecar → frontend) — hepsi `id` taşır

```json
{"id":"req-42","ev":"started","total_hint":null}
{"id":"req-42","ev":"progress","done":120,"total":4000,"label":"C:\\src\\.next"}
{"id":"req-42","ev":"item","path":"...","size":81203,"kind":"node_modules","selected":true}
{"id":"req-42","ev":"done","ok":true,"summary":{"count":12,"bytes":910283}}
{"id":"req-42","ev":"error","code":"protected_path","message":"...","path":"..."}
{"id":"req-43","ev":"item_result","path":"...","ok":true,"trashed":true}
```

### Kurallar

- **İptal:** `{"op":"cancel","id":"req-42"}` aynı `id` ile gönderilir; sidecar ilk
  güvenli noktada durur, `{"ev":"done","ok":false,"cancelled":true}` yayınlar.
- **Eşzamanlılık:** her istek ayrı worker thread'de; `status`/`ping` uzun bir
  `scan` sürerken anında cevaplanır → menü geçişleri akıcı kalır.
- **Güvenlik:** `delete` op'u her hedef için
  `delete_path(use_trash=not permanent, dry_run=...)` çağırır. Korumalı yol reddi
  `ev:"error"` ile döner, işlem devam eder. Hiçbir güvenlik kuralı atlanmaz.
- **Hata izolasyonu:** bir hedefteki hata diğerlerini durdurmaz; her hedef kendi
  `item_result` olayını üretir.
- **Loglama:** mevcut `~/.wmole/logs/operations.log` aynen yazılmaya devam eder.

**`mole.py`'ye eklenecek tek yeni yüzey:** `def cmd_serve()` + `op` yönlendiricisi.
Mevcut `Scanner`, `delete_path`, `analyze_path`, `cli_uninstall` vb. mantığını
çağırır — yeniden yazmaz. Tahmini ~250-350 satır ince yapıştırıcı kod.

## Frontend

### Genel düzen (TUI dashboard'a sadık)

```
üst bar:  wmole   [● sidecar bağlı]   v0.4.0   _ □ ✕
sol sidebar: Dashboard / Analyze / Clean / Purge / Installers /
             Uninstall / Optimize / Ports / Update / Remove
içerik paneli: başlık + araç çubuğu, liste/grid/tablo (sanal-kaydırma)
alt durum şeridi: ilerleme çubuğu · 1240/4000 · iptal [✕]
```

- **Dashboard (varsayılan açılış):** TUI status dashboard'unun kart düzenli hâli —
  CPU/RAM/disk, uptime, güç/batarya, ağ sayaçları, sürücü doluluk çubukları.
  `status` op'una ~2 sn'de bir poll ile canlı güncellenir (uzun işlem sürerken
  bile; sidecar eşzamanlı olduğu için).
- **Mod ekranları:** her TUI modu bir sidebar girişi; içerik paneli moda göre
  liste/tablo. Uzun işlemler alt durum şeridinde canlı ilerleme + İptal ile akar.

### Dialoglar (KESİN KURAL)

- Tarayıcı native dialogu YOK (`alert`/`confirm`/`prompt` yasak). Tümü uygulama-içi
  özel modal/toast bileşenleri.
- **Silme onay modalı:** öğe sayısı, toplam boyut, Geri Dönüşüm Kutusu / Kalıcı
  seçimi (kalıcı → kırmızı ikinci onay), iptal/onayla. Korumalı yollar listede gri
  ve "atlanacak" etiketli.
- **Yüksek riskli optimize aksiyonları:** kırmızı uyarı modalı (ikinci onay).
- **Toast bildirimleri:** işlem bitti / X öğe silindi / Y hata (sağ alt).
- **Hata gösterimi:** `ev:"error"` → satır içi rozet + tıklanabilir toast.

### Teknik

Svelte + Vite (hafif, yoğun arayüz için hızlı), sanal-kaydırma (binlerce satır),
Tauri event köprüsü üzerinden NDJSON akışını dinleyen tek bir `sidecar store`.

## Paketleme

- Mevcut `wmole.spec` (PyInstaller) → `wmole.exe`; Tauri `externalBin` (sidecar)
  olarak `src-tauri/binaries/wmole-x86_64-pc-windows-msvc.exe` adıyla gömülür.
  Son kullanıcıda Python gerekmez.
- Tauri `bundle` → tek `.msi`/`.exe`. Mevcut `installer/` ve ikon setleri
  (`assets/icons/desktop/wmole.ico`) yeniden kullanılır.
- Repo yapısı: yeni `gui/` (Svelte) + `src-tauri/` (Rust). `mole.py` kökte kalır,
  yalnızca `serve` modu eklenir. Eski TUI/CLI silinmez — yan yana çalışır.

## Test

- **Python `serve`:** `tests/test_serve.py` — istek JSON pipe et, NDJSON olaylarını
  doğrula (delete'in korumalı yolu reddetmesi, dry_run, iptal). `delete_path` zaten
  test kapsamında.
- **Rust köprü:** sahte sidecar (echo script) ile süreç yaşam döngüsü + NDJSON
  parse + iptal sinyali testleri.
- **Frontend:** sahte event akışıyla bileşen testleri (dialog onay akışı, sanal
  liste, ilerleme şeridi).
- **E2E manuel:** dry_run ile gerçek tarama→silme akışı (Geri Dönüşüm Kutusu
  doğrulaması).

## Fazlama (tam parite, güvenli sıra)

1. Python `serve` protokolü + testler (GUI yok, pipe ile doğrulanır).
2. Tauri iskelet + Rust köprü + sidecar başlatma.
3. Dashboard + status (canlı poll).
4. Scan ekranları (analyze/clean/purge/installers) + silme modalı + iptal
   — donma/onay sorununun çözüldüğü an.
5. Uninstall + leftovers, optimize (yüksek-risk modal), ports, update, remove.
6. Paketleme (.msi) + ikon/installer entegrasyonu.

## Risk Notları

- PyInstaller sidecar ilk açılışta yavaş olabilir → `serve` kalıcı olduğu için tek
  seferlik maliyet.
- Antivirüs PyInstaller exe'yi işaretleyebilir → mevcut imzalama akışı (varsa)
  korunur.
