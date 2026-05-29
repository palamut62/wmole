# Tarama Performansı & Sağlamlık Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** wmole tarama hızını `os.scandir` + paralel boyutlandırma + boyut cache'i ile belirgin artırmak, UI render'ını throttle etmek, hepsini testle güvenceye almak.

**Architecture:** Tek dosya `mole.py` korunur (modüler refactor yok). `dir_size` scandir tabanlı yeniden yazılır (imza aynı). `Scanner` öğeleri `ThreadPoolExecutor` ile paralel boyutlandırır. `~/.wmole/cache.json` mtime-anahtarlı boyut cache'i sağlar. Pure throttle yardımcısı render sıklığını sınırlar. Testler `tests/test_mole.py` (unittest) içine eklenir.

**Tech Stack:** Python 3.9+, stdlib (`os.scandir`, `concurrent.futures`, `json`, `threading`), `rich`, `unittest`.

**Kaçış valfleri:** `WMOLE_SCAN_WORKERS=1` (tek-thread), `--no-cache` (cache bypass).

---

### Task 1: `dir_size` scandir yeniden yazımı

**Files:**
- Modify: `mole.py:125-154` (`dir_size`)
- Test: `tests/test_mole.py`

- [ ] **Step 1: Write the failing test**

`tests/test_mole.py` içine `WmoleBehaviorTests` sınıfına ekle:

```python
    def test_dir_size_scandir_counts_nested_and_respects_budget(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.txt").write_bytes(b"x" * 100)
            sub = root / "sub"
            sub.mkdir()
            (sub / "b.txt").write_bytes(b"y" * 250)
            self.assertEqual(mole.dir_size(root), 350)

            seen = []
            total = mole.dir_size(root, on_progress=lambda n: seen.append(n))
            self.assertEqual(total, 350)
            self.assertTrue(seen)

            # max_files budget stops early
            capped = mole.dir_size(root, max_files=1)
            self.assertLessEqual(capped, 350)
            self.assertGreater(capped, 0)
```

- [ ] **Step 2: Run test to verify it passes against current impl (baseline parity)**

Run: `py -m unittest tests.test_mole.WmoleBehaviorTests.test_dir_size_scandir_counts_nested_and_respects_budget -v`
Expected: PASS (mevcut `os.walk` impl zaten doğru toplamı veriyor — bu test yeni impl'in paritesini koruyacak).

- [ ] **Step 3: Replace `dir_size` body with scandir implementation**

`mole.py:125-154` tamamını şununla değiştir:

```python
def dir_size(path: Path, on_progress=None, max_seconds: Optional[float] = None,
             max_files: Optional[int] = None) -> int:
    total = 0
    last_emit = 0
    started = time.time()
    seen = 0

    def walk(p) -> bool:
        """Returns False when a budget limit is hit (stop signal)."""
        nonlocal total, last_emit, seen
        try:
            with os.scandir(p) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not walk(entry.path):
                                return False
                            continue
                        total += entry.stat(follow_symlinks=False).st_size
                        seen += 1
                    except OSError:
                        continue
                    if max_files is not None and seen >= max_files:
                        return False
                    if max_seconds is not None and time.time() - started >= max_seconds:
                        return False
                    if on_progress and total - last_emit > 25_000_000:
                        on_progress(total)
                        last_emit = total
        except OSError:
            return True
        return True

    try:
        if Path(path).is_file():
            try:
                return os.stat(path).st_size
            except OSError:
                return 0
        walk(path)
    except Exception:
        pass
    if on_progress:
        on_progress(total)
    return total
```

- [ ] **Step 4: Run the test and the full suite**

Run: `py -m unittest discover -s tests -v`
Expected: PASS (yeni test + mevcut `analyze_path` testleri).

- [ ] **Step 5: Compile check**

Run: `py -m py_compile mole.py`
Expected: çıktı yok (başarı).

- [ ] **Step 6: Commit**

```bash
git add mole.py tests/test_mole.py
git commit -m "perf: rewrite dir_size with os.scandir (single stat per entry)"
```

---

### Task 2: Boyut cache yardımcıları (`~/.wmole/cache.json`)

**Files:**
- Modify: `mole.py` (sabitler bölümü `mole.py:101-108` civarı; yeni fonksiyonlar `dir_size`'dan sonra ~`mole.py:155`)
- Test: `tests/test_mole.py`

- [ ] **Step 1: Write the failing test**

```python
    def test_size_cache_roundtrip_and_invalidation(self):
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "cache.json"
            with mock.patch.object(mole, "CACHE_FILE", cache_file):
                target = Path(td) / "data"
                target.mkdir()
                cache = mole.load_size_cache()
                self.assertEqual(cache, {})
                mtime = target.stat().st_mtime
                mole.cache_set(cache, target, mtime, 1234)
                mole.save_size_cache(cache)

                reloaded = mole.load_size_cache()
                self.assertEqual(mole.cache_get(reloaded, target, mtime), 1234)
                # mtime mismatch -> miss
                self.assertIsNone(mole.cache_get(reloaded, target, mtime + 10))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m unittest tests.test_mole.WmoleBehaviorTests.test_size_cache_roundtrip_and_invalidation -v`
Expected: FAIL with `AttributeError: module 'mole' has no attribute 'CACHE_FILE'` (veya `load_size_cache`).

- [ ] **Step 3: Add CACHE_FILE constant**

`mole.py:106` (`PURGE_PATHS_FILE = ...` satırının hemen altına) ekle:

```python
CACHE_FILE = WMOLE_DIR / "cache.json"
```

- [ ] **Step 4: Add cache helper functions**

`dir_size` fonksiyonundan hemen sonra (`mole.py:155` civarı) ekle:

```python
def load_size_cache() -> dict:
    """Returns {path_str: {"mtime": float, "size": int, "scanned_at": float}}."""
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_size_cache(cache: dict) -> None:
    try:
        WMOLE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass


def cache_get(cache: dict, path: Path, mtime: float) -> Optional[int]:
    entry = cache.get(str(path))
    if entry and abs(entry.get("mtime", -1) - mtime) < 1e-6:
        return int(entry.get("size", 0))
    return None


def cache_set(cache: dict, path: Path, mtime: float, size: int) -> None:
    cache[str(path)] = {"mtime": mtime, "size": size, "scanned_at": time.time()}
```

- [ ] **Step 5: Run test and suite**

Run: `py -m unittest discover -s tests -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add mole.py tests/test_mole.py
git commit -m "feat: add mtime-keyed size cache helpers"
```

---

### Task 3: Throttle yardımcısı

**Files:**
- Modify: `mole.py` (yeni fonksiyon, `dir_size`/cache helper'larından sonra)
- Test: `tests/test_mole.py`

- [ ] **Step 1: Write the failing test**

```python
    def test_should_redraw_throttles_idle_and_allows_active(self):
        # Active scan: redraw allowed once min_interval elapsed
        self.assertTrue(mole.should_redraw(last_draw=0.0, now=1.0,
                                            scanner_active=True, min_interval=0.08))
        # Active scan but too soon: skip
        self.assertFalse(mole.should_redraw(last_draw=1.0, now=1.02,
                                            scanner_active=True, min_interval=0.08))
        # Idle scan: never forced redraw
        self.assertFalse(mole.should_redraw(last_draw=0.0, now=100.0,
                                            scanner_active=False, min_interval=0.08))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m unittest tests.test_mole.WmoleBehaviorTests.test_should_redraw_throttles_idle_and_allows_active -v`
Expected: FAIL with `AttributeError: module 'mole' has no attribute 'should_redraw'`.

- [ ] **Step 3: Add the helper**

cache helper'larından sonra ekle:

```python
def should_redraw(last_draw: float, now: float, scanner_active: bool,
                  min_interval: float = 0.08) -> bool:
    """Throttle in-loop re-renders: only redraw while scanning and after min_interval."""
    if not scanner_active:
        return False
    return (now - last_draw) >= min_interval
```

- [ ] **Step 4: Run test and suite**

Run: `py -m unittest discover -s tests -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mole.py tests/test_mole.py
git commit -m "feat: add should_redraw throttle helper"
```

---

### Task 4: Paralel boyutlandırma (`Scanner`)

**Files:**
- Modify: `mole.py:1035-1178` (`Scanner.__init__`, `_size_item`, `run`)
- Test: `tests/test_mole.py`

- [ ] **Step 1: Write the failing test**

```python
    def test_scanner_parallel_sizes_clean_categories(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = root / "proj"
            (proj / "node_modules").mkdir(parents=True)
            (proj / "node_modules" / "big.js").write_bytes(b"z" * 5000)
            sc = mole.Scanner(profile="purge", roots=[root])
            sc.run()
            self.assertTrue(sc.done)
            total = sum(c.total for c in sc.categories)
            self.assertGreaterEqual(total, 5000)
```

- [ ] **Step 2: Run test to verify current behavior (baseline)**

Run: `py -m unittest tests.test_mole.WmoleBehaviorTests.test_scanner_parallel_sizes_clean_categories -v`
Expected: PASS (mevcut sıralı impl ile de geçer; paralelleştirme sonrası da geçmeli — eşitlik koruması).

- [ ] **Step 3: Add executor to `Scanner.__init__`**

`mole.py:1046` (`self._lock = threading.Lock()` satırının altına) ekle:

```python
        self.workers = int(os.environ.get("WMOLE_SCAN_WORKERS",
                                          min(8, (os.cpu_count() or 4) * 2)))
```

Dosyanın tepesindeki importlara (ör. `mole.py:1` civarı `import threading` yanına) ekle (yoksa):

```python
from concurrent.futures import ThreadPoolExecutor
```

- [ ] **Step 4: Wrap status updates in `_size_item` with lock**

`mole.py:1052-1069` `_size_item` içinde `self.current_item_id = id(it)` ve `self.current_item_id = None` atamalarını lock altına al:

```python
    def _size_item(self, it: Item) -> None:
        if is_whitelisted(it.path, self.whitelist):
            it.size = 0
            it.scanning = False
            it.error = "whitelisted"
            it.selected = False
            return
        it.scanning = True
        with self._lock:
            self.current_item_id = id(it)
        def on_progress(n: int) -> None:
            it.partial = n
        if self.profile == "clean":
            it.size = dir_size(it.path, on_progress=on_progress, max_seconds=0.35, max_files=5000)
        else:
            it.size = dir_size(it.path, on_progress=on_progress)
        it.partial = it.size
        it.scanning = False
        with self._lock:
            self.current_item_id = None
```

- [ ] **Step 5: Parallelize the fixed-category sizing loop in `run`**

`mole.py:1143-1150` mevcut sıralı blok:

```python
        for cat in fixed_cats:
            self.status = f"Scanning {cat.title}…"
            self.current_cat_key = cat.key
            for it in cat.items:
                if it.size == 0:  # installers already have size from old_installers
                    self._size_item(it)
            cat.scanning = False
            self.current_cat_key = None
```

şununla değiştir:

```python
        pending = [it for cat in fixed_cats for it in cat.items if it.size == 0]
        self.status = f"Scanning {len(fixed_cats)} categories ({len(pending)} items)…"
        if pending:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                list(pool.map(self._size_item, pending))
        for cat in fixed_cats:
            cat.scanning = False
        self.current_cat_key = None
```

- [ ] **Step 6: Parallelize the dev-folder sizing loop in `run`**

`mole.py:1161-1170` mevcut blok:

```python
        for title, p in iter_dev_folders(roots):
            cat = dev_titles[title]
            it = Item(path=p, selected=True)
            cat.items.append(it)
            self.status = f"Sizing {p}…"
            self.current_cat_key = cat.key
            self._size_item(it)
            cat.items.sort(key=lambda i: i.size, reverse=True)
            cat.description = f"{len(cat.items)} folder(s) under your dev dirs"
            self.current_cat_key = None
```

şununla değiştir:

```python
        dev_items: List[Item] = []
        for title, p in iter_dev_folders(roots):
            cat = dev_titles[title]
            it = Item(path=p, selected=True)
            with self._lock:
                cat.items.append(it)
            dev_items.append(it)
        self.status = f"Sizing {len(dev_items)} dev folder(s)…"
        if dev_items:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                list(pool.map(self._size_item, dev_items))
        for title, cat in dev_titles.items():
            cat.items.sort(key=lambda i: i.size, reverse=True)
            cat.description = f"{len(cat.items)} folder(s) under your dev dirs"
        self.current_cat_key = None
```

- [ ] **Step 7: Run test and full suite**

Run: `py -m unittest discover -s tests -v`
Expected: PASS.

- [ ] **Step 8: Verify single-thread escape valve**

Run (bash): `WMOLE_SCAN_WORKERS=1 py -m unittest tests.test_mole.WmoleBehaviorTests.test_scanner_parallel_sizes_clean_categories -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add mole.py tests/test_mole.py
git commit -m "perf: parallelize Scanner sizing with ThreadPoolExecutor"
```

---

### Task 5: Cache'i `_size_item`'a bağla

**Files:**
- Modify: `mole.py` (`Scanner.__init__`, `_size_item`, `run` sonu)
- Test: `tests/test_mole.py`

- [ ] **Step 1: Write the failing test**

```python
    def test_scanner_uses_size_cache_on_second_run(self):
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "cache.json"
            with mock.patch.object(mole, "CACHE_FILE", cache_file):
                root = Path(td) / "proj"
                (root / "node_modules").mkdir(parents=True)
                (root / "node_modules" / "x.js").write_bytes(b"q" * 4000)

                sc1 = mole.Scanner(profile="purge", roots=[Path(td)])
                sc1.run()
                self.assertTrue(cache_file.exists())

                # Second run: dir_size must NOT be called for cached path
                with mock.patch.object(mole, "dir_size",
                                       side_effect=AssertionError("should be cached")):
                    sc2 = mole.Scanner(profile="purge", roots=[Path(td)])
                    sc2.run()
                total = sum(c.total for c in sc2.categories)
                self.assertGreaterEqual(total, 4000)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m unittest tests.test_mole.WmoleBehaviorTests.test_scanner_uses_size_cache_on_second_run -v`
Expected: FAIL (cache henüz bağlı değil; ikinci run `dir_size` çağırır → AssertionError).

- [ ] **Step 3: Load cache in `__init__`**

`Scanner.__init__` içinde `self.workers = ...` satırının altına ekle:

```python
        self.use_cache = use_cache
        self.cache = load_size_cache() if use_cache else {}
        self._cache_dirty = False
```

Ve imzayı güncelle (`mole.py:1036-1037`):

```python
    def __init__(self, whitelist: Optional[List[Path]] = None,
                 profile: str = "full", roots: Optional[List[Path]] = None,
                 use_cache: bool = True) -> None:
```

- [ ] **Step 4: Consult/update cache inside `_size_item`**

`_size_item` içinde, whitelist kontrolünden sonra ve `it.scanning = True`'dan önce, sonra boyut hesaplama bölümünü cache ile sar. `_size_item`'ı şu hale getir:

```python
    def _size_item(self, it: Item) -> None:
        if is_whitelisted(it.path, self.whitelist):
            it.size = 0
            it.scanning = False
            it.error = "whitelisted"
            it.selected = False
            return
        try:
            mtime = it.path.stat().st_mtime
        except OSError:
            mtime = None
        if self.use_cache and mtime is not None:
            cached = cache_get(self.cache, it.path, mtime)
            if cached is not None:
                it.size = cached
                it.partial = cached
                it.scanning = False
                return
        it.scanning = True
        with self._lock:
            self.current_item_id = id(it)
        def on_progress(n: int) -> None:
            it.partial = n
        if self.profile == "clean":
            it.size = dir_size(it.path, on_progress=on_progress, max_seconds=0.35, max_files=5000)
        else:
            it.size = dir_size(it.path, on_progress=on_progress)
        it.partial = it.size
        it.scanning = False
        with self._lock:
            self.current_item_id = None
            if self.use_cache and mtime is not None:
                cache_set(self.cache, it.path, mtime, it.size)
                self._cache_dirty = True
```

Not: `clean` profili `max_seconds`/`max_files` ile kısmi sonuç verebildiğinden cache'e yazılır ama partial olabilir; bu kabul edilebilir (gösterim amaçlı, mtime değişince yenilenir).

- [ ] **Step 5: Save cache at end of `run`**

`run` metodunun her `self.done = True` ile biten dalından **önce** cache'i kaydetmek yerine, en sade yol: `run`'ın gövdesini try/finally'ye almak yerine, dört `self.done = True` noktasının her birinin hemen üstüne ekle:

```python
            if self._cache_dirty:
                save_size_cache(self.cache)
```

`self.done = True` içeren satırlar: `mole.py` purge dalı, clean(roots) dalı, installer dalı, clean-profil sonu, ve normal sonu. Her birinde `self.done = True`'dan hemen önce yukarıdaki iki satırı koy.

- [ ] **Step 6: Run test and suite**

Run: `py -m unittest discover -s tests -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add mole.py tests/test_mole.py
git commit -m "feat: consult mtime size cache during scan, persist after run"
```

---

### Task 6: `--no-cache` CLI bayrağı

**Files:**
- Modify: `mole.py:3679-3781` (`main_cli` argparse + Scanner çağrıları), `run_tui` (`mole.py:2492`), `cli_clean`/`cli_categories` çağrı zinciri.
- Test: `tests/test_mole.py`

- [ ] **Step 1: Write the failing test**

```python
    def test_scanner_use_cache_false_skips_load(self):
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "cache.json"
            cache_file.write_text('{"x": {"mtime": 1, "size": 9}}', encoding="utf-8")
            with mock.patch.object(mole, "CACHE_FILE", cache_file):
                sc = mole.Scanner(profile="idle", use_cache=False)
                self.assertEqual(sc.cache, {})
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `py -m unittest tests.test_mole.WmoleBehaviorTests.test_scanner_use_cache_false_skips_load -v`
Expected: PASS (Task 5'te `use_cache` eklendiği için bu zaten geçer — bu test bayrağın Scanner ucunu kilitler). Geçiyorsa Step 3'e bayrak entegrasyonu için devam et.

- [ ] **Step 3: Add `--no-cache` argument**

`mole.py:3697` (`--all-binds` argümanından sonra) ekle:

```python
    p.add_argument("--no-cache", action="store_true", help="bypass size cache for this run")
```

- [ ] **Step 4: Thread `use_cache` into run_tui**

`run_tui` imzasını güncelle (`mole.py:2479`):

```python
def run_tui(initial_view: str = "analyze", start_path: Optional[Path] = None,
            use_cache: bool = True) -> None:
```

`mole.py:2492` Scanner oluşturmayı güncelle:

```python
    scanner = Scanner(whitelist=whitelist, profile=profile, use_cache=use_cache)
```

- [ ] **Step 5: Pass flag from main_cli to run_tui calls**

`main_cli` içinde iki `run_tui(...)` çağrısına `use_cache=not args.no_cache` ekle:

`mole.py:3714`:
```python
        run_tui(initial_view="analyze", start_path=target_paths[0], use_cache=not args.no_cache)
```
`mole.py:3781`:
```python
        run_tui(initial_view=args.mode if args.mode in ("status", "optimize", "uninstall", "purge", "installer", "installers") else "analyze", use_cache=not args.no_cache)
```

- [ ] **Step 6: Run suite and compile**

Run: `py -m unittest discover -s tests -v && py -m py_compile mole.py`
Expected: PASS, derleme hatasız.

- [ ] **Step 7: Commit**

```bash
git add mole.py tests/test_mole.py
git commit -m "feat: add --no-cache flag to bypass size cache"
```

---

### Task 7: UI throttle'ı render polling döngüsüne uygula

**Files:**
- Modify: `mole.py:2535-2585` (Live döngüsü, iç polling)
- Test: yok (davranış görsel; `should_redraw` Task 3'te birim test edildi)

- [ ] **Step 1: Track last draw time before the loop**

`mole.py:2535` `with Live(...) as live:` satırından hemen sonra, `while True:`'dan önce ekle:

```python
        last_draw = 0.0
```

- [ ] **Step 2: Throttle the inner polling re-render**

`mole.py:2576-2583` mevcut iç blok:

```python
            key = None
            for _ in range(20):
                if os.name == "nt" and msvcrt.kbhit():
                    key = read_key()
                    break
                time.sleep(0.05)
                live.update(render(scanner, view, cursor, msg, use_trash, dry_run, apps_cache, opt_cache,
                               palette=(palette_query, palette_cursor) if palette_open else None))
```

şununla değiştir:

```python
            key = None
            for _ in range(20):
                if os.name == "nt" and msvcrt.kbhit():
                    key = read_key()
                    break
                time.sleep(0.05)
                now = time.time()
                if should_redraw(last_draw, now, scanner_active=not scanner.done):
                    live.update(render(scanner, view, cursor, msg, use_trash, dry_run, apps_cache, opt_cache,
                                   palette=(palette_query, palette_cursor) if palette_open else None))
                    last_draw = now
```

- [ ] **Step 3: Update last_draw on the main render call**

`mole.py:2573-2574` ana render çağrısından sonra ekle (`last_draw = time.time()`):

```python
            live.update(render(scanner, view, cursor, msg, use_trash, dry_run, apps_cache, opt_cache,
                               palette=(palette_query, palette_cursor) if palette_open else None))
            last_draw = time.time()
```

- [ ] **Step 4: Compile check**

Run: `py -m py_compile mole.py`
Expected: hatasız.

- [ ] **Step 5: Manual smoke test (TUI redraw not stalled)**

Run: `py mole.py analyze` (kısa süre çalıştır, tarama ilerlemesi akıyor mu kontrol et, `Q` ile çık).
Expected: Tarama sırasında ilerleme güncelleniyor; idle iken CPU sakin.

- [ ] **Step 6: Commit**

```bash
git add mole.py
git commit -m "perf: throttle TUI re-render while scanning"
```

---

### Task 8: README & doğrulama

**Files:**
- Modify: `README.md` (Configuration / Command Set bölümleri)

- [ ] **Step 1: Document the new knobs**

`README.md` Configuration bölümündeki "Useful knobs" listesine ekle:

```markdown
- `WMOLE_SCAN_WORKERS`: tarama paralelliği (varsayılan `min(8, cpu*2)`, `1` = tek-thread).
- `--no-cache`: bu çalıştırmada `~/.wmole/cache.json` boyut cache'ini atla.
- `~/.wmole/cache.json`: mtime-anahtarlı boyut cache'i (otomatik, bozulursa güvenle yok sayılır).
```

- [ ] **Step 2: Full suite + compile final check**

Run: `py -m unittest discover -s tests -v && py -m py_compile mole.py`
Expected: tüm testler PASS, derleme hatasız.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document scan workers, --no-cache, and size cache"
```

---

## Notlar
- `Optional`, `List` zaten `mole.py` tepesinde `typing`'den import edilmiş (mevcut imzalarda kullanılıyor).
- `concurrent.futures.ThreadPoolExecutor` import'u Task 4 Step 3'te eklenir; tekrar eklenmemeli.
- Cache yalnız boyut/gösterim içindir; silme güvenliği (`is_protected_path`) her zaman canlı çalışır — cache'ten silme kararı türetilmez.
