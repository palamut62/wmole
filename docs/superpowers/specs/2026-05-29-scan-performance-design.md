# wmole — Tarama Performansı & Sağlamlık İyileştirmesi

- **Tarih:** 2026-05-29
- **Yaklaşım:** A (hedefli, düşük-riskli; modüler refactor yok)
- **Hedef:** Tarama hızını belirgin artırmak, UI akıcılığını iyileştirmek, paralel/cache kodunu testle güvenceye almak.

## Bağlam

`mole.py` tek dosyalık bir Windows bakım aracı. Mevcut darboğazlar:

1. `Scanner.run` (`mole.py:1071`) tamamen tek-thread ve sıralı: ~40 sabit kategori + dev klasörleri art arda boyutlandırılıyor.
2. `dir_size` (`mole.py:125`) her dosya için `os.walk` + ayrı `os.stat` = 2 syscall/dosya.
3. Çalıştırmalar arası boyut cache'i yok; her şey sıfırdan taranıyor.
4. Render ana thread'de; uzun işlemlerde kasma riski.
5. `tests/` mevcut ama eşzamanlı kod için kapsam yetersiz.

## Tasarım

### 1. `dir_size` yeniden yazımı (scandir)
- `os.walk`+`os.stat` yerine özyinelemeli `os.scandir`; `DirEntry.stat(follow_symlinks=False)` cache'li → dosya başına syscall yarıya iner.
- Mevcut imza (`path`, `on_progress`, `max_seconds`, `max_files`) **birebir korunur** — çağıranlar değişmez.
- Sembolik bağlantı döngüsü koruması: dizinlere `follow_symlinks=False` ile inilir.
- `on_progress` emit eşiği (25 MB) ve zaman/dosya bütçesi davranışı korunur.

### 2. Paralel boyutlandırma
- `Scanner`'a `ThreadPoolExecutor`. Worker sayısı: `int(os.environ.get("WMOLE_SCAN_WORKERS", min(8, (os.cpu_count() or 4) * 2)))`.
- `WMOLE_SCAN_WORKERS=1` → tek-thread kaçış valfi.
- Kategori öğeleri (`_size_item`) havuza gönderilir; I/O-bound olduğu için stat sırasında GIL serbest kalır.
- `self._lock` (mevcut) ile `categories`, `current_cat_key`, `current_item_id` güncellemeleri korunur.
- `clean` profilindeki öğe-başına `max_seconds=0.35` / `max_files=5000` bütçesi korunur.
- Tüm future'lar bittikten sonra kategori sıralaması (`.total` azalan) yapılır → deterministik sonuç.

### 3. Boyut cache'i
- Konum: `~/.wmole/cache.json`, şema: `{path: {"mtime": float, "size": int, "scanned_at": float}}`.
- Boyutlandırmadan önce kök dizinin `mtime`'ı cache ile eşleşiyorsa tarama atlanır.
- Yazım atomik: temp dosyaya yaz + `os.replace`.
- `--no-cache` CLI bayrağı; bozuk cache okuma hatası yakalanıp yok sayılır (tam tarama).
- **Güvenlik:** cache yalnızca gösterim/boyut içindir. Silme öncesi `is_protected_path` ve gerçek yol kontrolü her zaman canlı yapılır; cache'ten silme kararı türetilmez.

### 4. UI throttle
- Render döngüsünde son çizimden bu yana < eşik (varsayılan 80 ms ≈ 12 fps) ise çizim atlanır.
- Throttle saf yardımcı fonksiyon olarak ayrılır (test edilebilir).
- Paralel modda durum metni: "N/M kategori · X taranıyor…".
- Mevcut TUI tuş akışı ve görünüm değişmez.

## Test
`tests/` altına birim testleri:
- scandir `dir_size`, geçici dizin fixture'ında bilinen toplamı verir (eski davranışla paritesi).
- Paralel tarama, sıralı tarama ile aynı kategori toplamlarını üretir.
- Cache invalidation: `mtime` değişince yeniden tarar; aynıyken atlar.
- Throttle yardımcısı: eşik altında `False`, üstünde `True`.
- `py -m py_compile mole.py` ve `unittest discover -s tests` yeşil kalır.

## Riskler & Geri-Dönüş
- Paralel hata → `WMOLE_SCAN_WORKERS=1`.
- Cache bozulması → okuma hatası yakalanır, tam tarama.
- Her madde bağımsız commit; tek tek geri alınabilir.

## Kapsam Dışı (YAGNI)
- Modül ayrımı / paketleme.
- asyncio tabanlı motor.
- Yeni temizlik kategorileri.
- Görsel yeniden tasarım.
