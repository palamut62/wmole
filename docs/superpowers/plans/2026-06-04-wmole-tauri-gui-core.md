# wmole Tauri GUI (Çekirdek: Faz 1–4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** wmole TUI'sinin çekirdek akışlarını (dashboard + analyze/clean/purge/installers tarama + güvenli silme) donmasız, iptal edilebilir, in-app modal dialoglu bir Tauri masaüstü uygulamasına taşımak.

**Architecture:** Üç katman — Svelte+Vite frontend (yalnız görsel), ince Rust köprüsü (sidecar yaşam döngüsü + NDJSON taşıma), ve mevcut `mole.py`'ye eklenen kalıcı `serve` modu (tüm iş mantığı; `delete_path`/`Scanner`/`analyze_path` yeniden kullanılır). Frontend ↔ Rust IPC; Rust ↔ Python stdin/stdout üzerinden satır-başına-bir-JSON (NDJSON).

**Tech Stack:** Python 3.9+ (mevcut `mole.py`, stdlib `json`/`threading`), Rust + Tauri v2 (`tauri-plugin-shell` sidecar), Svelte + Vite + TypeScript.

**Kapsam notu:** Bu plan Faz 1–4'ü kapsar ve tek başına çalışan/test edilebilir bir uygulama üretir. Faz 5–6 (uninstall/optimize/ports/update/remove + .msi paketleme) ayrı bir planda ele alınacaktır.

---

## File Structure

- `mole.py` (Modify): yeni `serve` modu + `op` yönlendirici (~250-350 satır ek), `status_payload()` refactor'u.
- `tests/test_serve.py` (Create): serve protokol testleri (pipe tabanlı, GUI'siz).
- `gui/` (Create): Svelte+Vite frontend.
  - `gui/src/lib/sidecar.ts`: NDJSON istek/olay store (Tauri event köprüsü).
  - `gui/src/lib/types.ts`: istek/olay tipleri.
  - `gui/src/lib/components/`: `Sidebar.svelte`, `TopBar.svelte`, `StatusBar.svelte`, `ConfirmModal.svelte`, `Toast.svelte`, `VirtualList.svelte`.
  - `gui/src/routes/`: `Dashboard.svelte`, `ScanView.svelte` (analyze/clean/purge/installers ortak).
  - `gui/src/App.svelte`, `gui/src/main.ts`, `gui/src/app.css`.
- `src-tauri/` (Create): Tauri Rust kabuğu.
  - `src-tauri/src/lib.rs`: sidecar başlatma, istek komutu, NDJSON parse → event emit, cancel.
  - `src-tauri/tauri.conf.json`, `src-tauri/Cargo.toml`, `src-tauri/build.rs`.

Her dosya tek sorumluluk taşır: `sidecar.ts` taşıma, route'lar ekran, `components` yeniden kullanılır UI parçaları, `lib.rs` köprü.

---

## Task 1: `status_payload()` helper'ını ayıkla

Status JSON'ı şu an `main()` dispatcher'ı içinde gömülü (mole.py:4369-4393). `serve` ve CLI'ın paylaşması için saf bir fonksiyona çıkar.

**Files:**
- Modify: `mole.py:4369-4395` (status dispatcher) ve yeni helper ekle.
- Test: `tests/test_serve.py`

- [ ] **Step 1: Failing test yaz**

`tests/test_serve.py` oluştur:

```python
import unittest
import mole


class StatusPayloadTest(unittest.TestCase):
    def test_status_payload_keys(self):
        payload = mole.status_payload()
        # psutil yoksa boş sözlük döner; varsa anahtarlar bulunmalı
        if payload:
            for key in ("cpu_percent", "memory_percent", "disk_percent",
                        "uptime_seconds", "device"):
                self.assertIn(key, payload)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Testi çalıştır, başarısız olduğunu gör**

Run: `py -m unittest tests.test_serve.StatusPayloadTest -v`
Expected: FAIL — `AttributeError: module 'mole' has no attribute 'status_payload'`

- [ ] **Step 3: Helper'ı ekle**

`mole.py` içinde `cli_clean` yakınına (örn. satır 3486 öncesi) ekle:

```python
def status_payload() -> dict:
    """Machine-readable system status. Returns {} if psutil unavailable."""
    if not psutil:
        return {}
    vm = psutil.virtual_memory()
    du = psutil.disk_usage(USER.anchor or 'C:\\')
    net = psutil.net_io_counters()
    dio = psutil.disk_io_counters()
    bat = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    boot = psutil.boot_time()
    sysinfo = platform.uname()
    temps = {}
    if hasattr(psutil, "sensors_temperatures"):
        try:
            temps = psutil.sensors_temperatures() or {}
        except Exception:
            temps = {}
    return {
        "health": health_score(), "cpu_percent": psutil.cpu_percent(interval=0.3),
        "memory_percent": vm.percent, "memory_used": vm.used, "memory_total": vm.total,
        "disk_percent": du.percent, "disk_free": du.free, "disk_total": du.total,
        "disk_read_bytes": (dio.read_bytes if dio else 0), "disk_write_bytes": (dio.write_bytes if dio else 0),
        "net_up_bytes": net.bytes_sent, "net_down_bytes": net.bytes_recv,
        "uptime_seconds": int(time.time() - boot),
        "battery_percent": (bat.percent if bat else None), "power_plugged": (bat.power_plugged if bat else None),
        "device": {"system": sysinfo.system, "release": sysinfo.release, "node": sysinfo.node},
        "temperature_sensors": {k: [{"label": t.label, "current": t.current} for t in v[:3]] for k, v in temps.items()},
    }
```

Sonra dispatcher'daki `elif args.mode == "status" and args.json_out:` bloğunu sadeleştir:

```python
    elif args.mode == "status" and args.json_out:
        print(json.dumps(status_payload(), indent=2))
```

- [ ] **Step 4: Testi çalıştır, geçtiğini gör**

Run: `py -m unittest tests.test_serve.StatusPayloadTest -v`
Expected: PASS

- [ ] **Step 5: Mevcut testlerin hâlâ geçtiğini doğrula + commit**

```bash
py -m py_compile mole.py
py -m unittest discover -s tests
git add mole.py tests/test_serve.py
git commit -m "refactor: extract status_payload() for reuse in serve mode"
```

---

## Task 2: `serve` döngüsü iskeleti — ping & status

Kalıcı stdin/stdout NDJSON döngüsü. Her satır bir istek JSON'ı; her istek için olaylar yazılır. İlk olarak `ping` ve `status` op'ları.

**Files:**
- Modify: `mole.py` — yeni `cmd_serve()` + dispatcher'a `serve` modu.
- Test: `tests/test_serve.py`

- [ ] **Step 1: Failing test yaz**

`tests/test_serve.py` içine ekle:

```python
import subprocess
import sys
import json
import os


def _run_serve(requests):
    """serve modunu pipe ile sürer; her istek bir satır. Olay listesini döner."""
    proc = subprocess.Popen(
        [sys.executable, "mole.py", "serve"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    payload = "".join(json.dumps(r) + "\n" for r in requests)
    out, err = proc.communicate(payload, timeout=60)
    events = [json.loads(line) for line in out.splitlines() if line.strip()]
    return events, err


class ServePingStatusTest(unittest.TestCase):
    def test_ping(self):
        events, err = _run_serve([{"id": "p1", "op": "ping"}])
        self.assertTrue(any(e["id"] == "p1" and e["ev"] == "done" and e["ok"]
                            for e in events), msg=err)

    def test_status(self):
        events, err = _run_serve([{"id": "s1", "op": "status"}])
        done = [e for e in events if e["id"] == "s1" and e["ev"] == "done"]
        self.assertTrue(done, msg=err)
        self.assertIn("payload", done[0])
```

- [ ] **Step 2: Testi çalıştır, başarısız olduğunu gör**

Run: `py -m unittest tests.test_serve.ServePingStatusTest -v`
Expected: FAIL — `serve` modu tanınmaz / olay üretilmez.

- [ ] **Step 3: `cmd_serve()` ekle**

`mole.py` içine (örn. `cli_completion` yakınına) ekle:

```python
import threading as _serve_threading


def _serve_emit(lock, event: dict) -> None:
    """Tek satır NDJSON olayı stdout'a atomik yaz."""
    line = json.dumps(event, ensure_ascii=False)
    with lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _serve_handle(req: dict, emit, cancels: dict) -> None:
    """Tek bir isteği işle. emit(event_dict) çağrılır. Task 3/4'te genişler."""
    rid = req.get("id")
    op = req.get("op")
    try:
        if op == "ping":
            emit({"id": rid, "ev": "done", "ok": True})
        elif op == "status":
            emit({"id": rid, "ev": "done", "ok": True, "payload": status_payload()})
        else:
            emit({"id": rid, "ev": "error", "code": "unknown_op",
                  "message": f"unknown op: {op}"})
            emit({"id": rid, "ev": "done", "ok": False})
    except Exception as exc:  # bir istek hatası döngüyü düşürmesin
        emit({"id": rid, "ev": "error", "code": "exception", "message": str(exc)})
        emit({"id": rid, "ev": "done", "ok": False})


def cmd_serve() -> None:
    """Kalıcı NDJSON istek/olay döngüsü. stdin'den satır-başına-bir-JSON okur."""
    out_lock = _serve_threading.Lock()
    cancels: dict = {}  # rid -> threading.Event (Task 3/4)

    def emit_for(rid):
        return lambda ev: _serve_emit(out_lock, ev)

    _serve_emit(out_lock, {"id": None, "ev": "ready"})
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            _serve_emit(out_lock, {"id": None, "ev": "error",
                                   "code": "bad_json", "message": raw[:200]})
            continue
        rid = req.get("id")
        if req.get("op") == "cancel":
            ev = cancels.get(rid)
            if ev:
                ev.set()
            continue
        worker = _serve_threading.Thread(
            target=_serve_handle,
            args=(req, lambda e: _serve_emit(out_lock, e), cancels),
            daemon=True,
        )
        worker.start()
```

Dispatcher'a ekle (status bloğundan önce uygun bir yere):

```python
    elif args.mode == "serve":
        cmd_serve()
```

- [ ] **Step 4: Testi çalıştır, geçtiğini gör**

Run: `py -m unittest tests.test_serve.ServePingStatusTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
py -m py_compile mole.py
git add mole.py tests/test_serve.py
git commit -m "feat(serve): NDJSON request loop with ping and status ops"
```

---

## Task 3: `scan` op — analyze/clean/purge/installers akışı + iptal

Tarama sonuçlarını `item` olaylarıyla akıt, `progress` ile ilerleme bildir, `cancel` ile durdur.

**Files:**
- Modify: `mole.py` — `_serve_handle` içine `scan` op.
- Test: `tests/test_serve.py`

- [ ] **Step 1: Failing test yaz**

```python
import tempfile
import pathlib


class ServeScanTest(unittest.TestCase):
    def test_analyze_scan_emits_items_and_done(self):
        with tempfile.TemporaryDirectory() as d:
            (pathlib.Path(d) / "a.txt").write_text("hello")
            (pathlib.Path(d) / "sub").mkdir()
            events, err = _run_serve([
                {"id": "a1", "op": "scan", "mode": "analyze", "paths": [d]}
            ])
            ids = [e for e in events if e["id"] == "a1"]
            self.assertTrue(any(e["ev"] == "started" for e in ids), msg=err)
            self.assertTrue(any(e["ev"] == "item" for e in ids), msg=err)
            done = [e for e in ids if e["ev"] == "done"]
            self.assertTrue(done and done[0]["ok"])
```

- [ ] **Step 2: Testi çalıştır, başarısız olduğunu gör**

Run: `py -m unittest tests.test_serve.ServeScanTest -v`
Expected: FAIL — `unknown op: scan`

- [ ] **Step 3: `scan` op'unu ekle**

`_serve_handle` imzasını iptal event'i alacak şekilde güncelle ve `scan` dalını ekle. `_serve_handle` içindeki `op == "ping"` zincirine ekleyerek, fonksiyonun başında bir cancel event'i kaydet:

```python
def _serve_handle(req: dict, emit, cancels: dict) -> None:
    rid = req.get("id")
    op = req.get("op")
    cancel = _serve_threading.Event()
    if rid is not None:
        cancels[rid] = cancel
    try:
        if op == "ping":
            emit({"id": rid, "ev": "done", "ok": True})
        elif op == "status":
            emit({"id": rid, "ev": "done", "ok": True, "payload": status_payload()})
        elif op == "scan":
            _serve_scan(req, emit, cancel)
        else:
            emit({"id": rid, "ev": "error", "code": "unknown_op",
                  "message": f"unknown op: {op}"})
            emit({"id": rid, "ev": "done", "ok": False})
    except Exception as exc:
        emit({"id": rid, "ev": "error", "code": "exception", "message": str(exc)})
        emit({"id": rid, "ev": "done", "ok": False})
    finally:
        cancels.pop(rid, None)
```

Yeni `_serve_scan` fonksiyonu ekle:

```python
def _serve_scan(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    mode = req.get("mode", "clean")
    paths = [Path(p) for p in req.get("paths", []) if p]
    emit({"id": rid, "ev": "started", "total_hint": None})

    if mode == "analyze":
        target = paths[0] if paths else USER
        min_large = int(load_config().get("large_file_min_mb", 512)) * 1024 * 1024
        result = analyze_path(target, large_file_min=min_large)
        for entry in result["entries"]:
            if cancel.is_set():
                emit({"id": rid, "ev": "done", "ok": False, "cancelled": True})
                return
            emit({"id": rid, "ev": "item", "path": str(target / entry["name"]),
                  "name": entry["name"], "size": entry["size"],
                  "kind": "dir" if entry["is_dir"] else "file", "selected": False})
        emit({"id": rid, "ev": "done", "ok": True,
              "summary": {"count": len(result["entries"]),
                          "bytes": result["total_size"]}})
        return

    # clean / purge / installers — Scanner profillerini kullan
    profile = {"clean": "clean", "purge": "purge",
               "installers": "installers"}.get(mode, "clean")
    scanner = Scanner(whitelist=load_whitelist(), profile=profile,
                      roots=paths or None, use_cache=not req.get("no_cache"))
    scanner.run()
    if cancel.is_set():
        emit({"id": rid, "ev": "done", "ok": False, "cancelled": True})
        return
    report = collect_selected_targets(scanner.categories, estimated=not paths)
    count = 0
    total = report.get("total", 0)
    for cat in scanner.categories:
        for it in cat.items:
            if cancel.is_set():
                emit({"id": rid, "ev": "done", "ok": False, "cancelled": True})
                return
            if it.deleted:
                continue
            count += 1
            emit({"id": rid, "ev": "item", "path": str(it.path),
                  "name": it.path.name, "size": int(it.size),
                  "kind": cat.key, "category": cat.title,
                  "selected": bool(it.selected)})
    emit({"id": rid, "ev": "done", "ok": True,
          "summary": {"count": count, "bytes": total}})
```

> Not: `Scanner.run()` mevcut hâliyle bloke çalışır; uzun tarama sürerken `cancel` yalnızca sonuç akışında kontrol edilir. UI yine de bloke olmaz çünkü her istek ayrı thread'dedir ve `status`/`ping` paralel cevaplanır. Daha ince iptal (tarama ortasında) Faz 5 iyileştirmesidir.

- [ ] **Step 4: Testi çalıştır, geçtiğini gör**

Run: `py -m unittest tests.test_serve.ServeScanTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
py -m py_compile mole.py
py -m unittest discover -s tests
git add mole.py tests/test_serve.py
git commit -m "feat(serve): scan op streaming items for analyze/clean/purge/installers"
```

---

## Task 4: `delete` op — güvenli silme akışı + dry_run + item_result

Verilen hedefleri tek tek `delete_path` ile sil; her hedef için `item_result` yayınla. Korumalı yol reddi işlemi durdurmaz.

**Files:**
- Modify: `mole.py` — `_serve_handle` içine `delete` op + `_serve_delete`.
- Test: `tests/test_serve.py`

- [ ] **Step 1: Failing test yaz**

```python
class ServeDeleteTest(unittest.TestCase):
    def test_dry_run_delete_reports_ok(self):
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "junk.txt"
            f.write_text("x")
            events, err = _run_serve([
                {"id": "d1", "op": "delete", "targets": [str(f)],
                 "permanent": False, "dry_run": True}
            ])
            ids = [e for e in events if e["id"] == "d1"]
            results = [e for e in ids if e["ev"] == "item_result"]
            self.assertTrue(results, msg=err)
            self.assertTrue(results[0]["ok"])
            self.assertTrue(f.exists())  # dry-run: dosya durmalı

    def test_protected_path_is_reported_not_fatal(self):
        events, err = _run_serve([
            {"id": "d2", "op": "delete", "targets": ["C:\\Windows"],
             "permanent": False, "dry_run": False}
        ])
        ids = [e for e in events if e["id"] == "d2"]
        results = [e for e in ids if e["ev"] == "item_result"]
        self.assertTrue(results, msg=err)
        self.assertFalse(results[0]["ok"])
        self.assertTrue(any(e["ev"] == "done" for e in ids))
```

- [ ] **Step 2: Testi çalıştır, başarısız olduğunu gör**

Run: `py -m unittest tests.test_serve.ServeDeleteTest -v`
Expected: FAIL — `unknown op: delete`

- [ ] **Step 3: `delete` op'unu ekle**

`_serve_handle` zincirine ekle (`scan` dalından sonra):

```python
        elif op == "delete":
            _serve_delete(req, emit, cancel)
```

`_serve_delete` fonksiyonunu ekle:

```python
def _serve_delete(req: dict, emit, cancel) -> None:
    rid = req.get("id")
    targets = [Path(p) for p in req.get("targets", []) if p]
    use_trash = not bool(req.get("permanent"))
    dry_run = bool(req.get("dry_run"))
    emit({"id": rid, "ev": "started", "total_hint": len(targets)})
    ok = err = 0
    for idx, path in enumerate(targets):
        if cancel.is_set():
            emit({"id": rid, "ev": "done", "ok": False, "cancelled": True,
                  "summary": {"ok": ok, "err": err}})
            return
        error = delete_path(path, use_trash=use_trash, dry_run=dry_run)
        if error:
            err += 1
            emit({"id": rid, "ev": "item_result", "path": str(path),
                  "ok": False, "error": error})
        else:
            ok += 1
            emit({"id": rid, "ev": "item_result", "path": str(path),
                  "ok": True, "trashed": use_trash, "dry_run": dry_run})
        emit({"id": rid, "ev": "progress", "done": idx + 1, "total": len(targets),
              "label": str(path)})
    emit({"id": rid, "ev": "done", "ok": True,
          "summary": {"ok": ok, "err": err}})
```

- [ ] **Step 4: Testi çalıştır, geçtiğini gör**

Run: `py -m unittest tests.test_serve.ServeDeleteTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
py -m py_compile mole.py
py -m unittest discover -s tests
git add mole.py tests/test_serve.py
git commit -m "feat(serve): delete op with safety rules, dry_run and per-item results"
```

---

## Task 5: Tauri + Svelte iskeleti

Tauri v2 + Svelte+Vite frontend kabuğu oluştur. Bu noktada sadece boş pencere açılır.

**Files:**
- Create: `gui/`, `src-tauri/` (scaffold).

- [ ] **Step 1: Scaffold oluştur**

Run (proje kökünde):

```bash
npm create tauri-app@latest -- --template svelte-ts --manager npm --identifier app.wmole.gui --yes
```

Komut etkileşim isterse: app adı `gui`, frontend dili TypeScript, paket yöneticisi npm, template `svelte`. Sonuç: `gui/` (frontend) + `gui/src-tauri/` veya kök `src-tauri/`. Aşağıdaki adımlar `gui/` altında frontend, `gui/src-tauri/` altında Rust varsayar; farklıysa yolları buna göre uyarla.

- [ ] **Step 2: Bağımlılıkları kur ve boş pencerenin açıldığını doğrula**

Run:

```bash
cd gui && npm install && npm run tauri dev
```

Expected: Boş bir Tauri penceresi açılır (varsayılan Svelte şablonu). Pencereyi kapat.

- [ ] **Step 3: shell plugin ekle (sidecar için)**

Run (`gui/src-tauri` içinde):

```bash
cargo add tauri-plugin-shell
```

`gui/src-tauri/capabilities/default.json` içine sidecar izinlerini ekle:

```json
{
  "permissions": [
    "core:default",
    "shell:allow-execute",
    "shell:allow-spawn",
    {
      "identifier": "shell:allow-spawn",
      "allow": [{ "name": "binaries/wmole", "sidecar": true, "args": ["serve"] }]
    }
  ]
}
```

- [ ] **Step 4: Commit**

```bash
git add gui src-tauri 2>/dev/null; git add gui
git commit -m "chore: scaffold Tauri v2 + Svelte-TS GUI shell"
```

---

## Task 6: Rust köprüsü — sidecar başlat, istek gönder, NDJSON → event

Rust tarafı: sidecar süreci başlat, frontend'den gelen JSON isteği stdin'e yaz, stdout NDJSON satırlarını `sidecar-event` Tauri event'i olarak frontend'e yay.

**Files:**
- Modify: `gui/src-tauri/src/lib.rs`
- Sidecar binary: geliştirme sırasında `gui/src-tauri/binaries/wmole-<target-triple>` (Task 11'de gerçek exe; şimdilik `dist/wmole.exe` kopyası).

- [ ] **Step 1: Sidecar binary'sini yerleştir (dev)**

Run (hedef triple'ı öğren):

```bash
rustc -Vv | grep host
```

`host: x86_64-pc-windows-msvc` çıktısını al. Mevcut derlenmiş exe'yi sidecar olarak kopyala:

```bash
mkdir -p gui/src-tauri/binaries
cp dist/wmole.exe "gui/src-tauri/binaries/wmole-x86_64-pc-windows-msvc.exe"
```

`gui/src-tauri/tauri.conf.json` içine `bundle.externalBin` ekle:

```json
"bundle": {
  "externalBin": ["binaries/wmole"]
}
```

- [ ] **Step 2: `lib.rs` köprü kodunu yaz**

`gui/src-tauri/src/lib.rs` dosyasını şu içerikle değiştir:

```rust
use std::io::Write;
use std::process::{ChildStdin};
use std::sync::Mutex;
use tauri::{Emitter, Manager, State};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

struct Sidecar(Mutex<Option<CommandChild>>);

#[tauri::command]
fn send_request(state: State<Sidecar>, line: String) -> Result<(), String> {
    let mut guard = state.0.lock().map_err(|e| e.to_string())?;
    let child = guard.as_mut().ok_or("sidecar not started")?;
    child
        .write((line + "\n").as_bytes())
        .map_err(|e| e.to_string())?;
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(Sidecar(Mutex::new(None)))
        .setup(|app| {
            let sidecar = app.shell().sidecar("wmole").unwrap().args(["serve"]);
            let (mut rx, child) = sidecar.spawn().expect("failed to spawn sidecar");
            app.state::<Sidecar>().0.lock().unwrap().replace(child);

            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx.recv().await {
                    if let CommandEvent::Stdout(bytes) = event {
                        let text = String::from_utf8_lossy(&bytes);
                        for line in text.lines() {
                            if line.trim().is_empty() {
                                continue;
                            }
                            let _ = handle.emit("sidecar-event", line.to_string());
                        }
                    }
                }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![send_request])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
```

`ChildStdin` import'u kullanılmıyorsa kaldır; derleyici uyarısını gidermek için yalnızca gerekli import'ları tut.

- [ ] **Step 3: Derlenip çalıştığını doğrula**

Run:

```bash
cd gui && npm run tauri dev
```

Expected: Pencere açılır, hata olmadan derlenir. (Olaylar henüz frontend'de gösterilmiyor; sonraki task.)

- [ ] **Step 4: Commit**

```bash
git add gui/src-tauri
git commit -m "feat(bridge): spawn python sidecar, forward requests, emit NDJSON events"
```

---

## Task 7: Frontend — sidecar store + uygulama kabuğu

NDJSON event'lerini dinleyen tek bir store + topbar/sidebar/statusbar düzeni.

**Files:**
- Create: `gui/src/lib/types.ts`, `gui/src/lib/sidecar.ts`
- Create: `gui/src/lib/components/TopBar.svelte`, `Sidebar.svelte`, `StatusBar.svelte`
- Modify: `gui/src/App.svelte`, `gui/src/app.css`

- [ ] **Step 1: Tipleri tanımla**

`gui/src/lib/types.ts`:

```ts
export type Op =
  | "ping" | "status" | "scan" | "delete" | "cancel";

export interface Request {
  id: string;
  op: Op;
  mode?: "analyze" | "clean" | "purge" | "installers";
  paths?: string[];
  targets?: string[];
  permanent?: boolean;
  dry_run?: boolean;
}

export interface SidecarEvent {
  id: string | null;
  ev: "ready" | "started" | "progress" | "item" | "item_result" | "done" | "error";
  [key: string]: unknown;
}

export interface ScanItem {
  path: string;
  name: string;
  size: number;
  kind: string;
  category?: string;
  selected: boolean;
}
```

- [ ] **Step 2: sidecar store'u yaz**

`gui/src/lib/sidecar.ts`:

```ts
import { listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";
import { writable } from "svelte/store";
import type { Request, SidecarEvent } from "./types";

type Handler = (e: SidecarEvent) => void;
const handlers = new Map<string, Handler>();
let counter = 0;

export const connected = writable(false);

listen<string>("sidecar-event", (msg) => {
  let e: SidecarEvent;
  try {
    e = JSON.parse(msg.payload);
  } catch {
    return;
  }
  if (e.ev === "ready") {
    connected.set(true);
    return;
  }
  if (e.id && handlers.has(e.id)) {
    handlers.get(e.id)!(e);
    if (e.ev === "done") handlers.delete(e.id);
  }
});

export function nextId(prefix = "req"): string {
  counter += 1;
  return `${prefix}-${counter}`;
}

/** İstek gönder; her olay için onEvent çağrılır. done'da Promise çözülür. */
export function request(req: Omit<Request, "id">, onEvent?: Handler): Promise<SidecarEvent> {
  const id = nextId(req.op);
  const full: Request = { ...req, id };
  return new Promise((resolve) => {
    handlers.set(id, (e) => {
      onEvent?.(e);
      if (e.ev === "done") resolve(e);
    });
    invoke("send_request", { line: JSON.stringify(full) });
  });
}

/** Çalışan bir isteği iptal et. */
export function cancel(id: string) {
  invoke("send_request", { line: JSON.stringify({ id, op: "cancel" }) });
}
```

- [ ] **Step 3: Kabuk bileşenlerini yaz**

`gui/src/lib/components/Sidebar.svelte`:

```svelte
<script lang="ts">
  export let active: string;
  export let onSelect: (view: string) => void;
  const views = ["Dashboard", "Analyze", "Clean", "Purge", "Installers"];
</script>

<nav class="sidebar">
  {#each views as v}
    <button class:active={active === v} on:click={() => onSelect(v)}>{v}</button>
  {/each}
</nav>

<style>
  .sidebar { display: flex; flex-direction: column; gap: 2px; padding: 8px;
    background: #11161c; min-width: 150px; }
  button { background: none; border: none; color: #9aa7b4; text-align: left;
    padding: 8px 12px; font-family: monospace; cursor: pointer; border-radius: 4px; }
  button:hover { background: #1b2530; color: #e6edf3; }
  button.active { background: #243140; color: #58d6a0; }
</style>
```

`gui/src/lib/components/TopBar.svelte`:

```svelte
<script lang="ts">
  import { connected } from "../sidecar";
</script>

<header class="topbar">
  <span class="brand">wmole</span>
  <span class="status" class:on={$connected}>
    {$connected ? "● sidecar bağlı" : "○ bağlanıyor…"}
  </span>
</header>

<style>
  .topbar { display: flex; align-items: center; gap: 16px; padding: 8px 14px;
    background: #0d1117; border-bottom: 1px solid #1b2530; font-family: monospace; }
  .brand { color: #58d6a0; font-weight: bold; }
  .status { color: #6e7681; font-size: 12px; }
  .status.on { color: #58d6a0; }
</style>
```

`gui/src/lib/components/StatusBar.svelte`:

```svelte
<script lang="ts">
  export let label = "";
  export let done = 0;
  export let total = 0;
  export let onCancel: (() => void) | null = null;
  $: pct = total > 0 ? Math.round((done / total) * 100) : 0;
</script>

{#if total > 0}
  <footer class="statusbar">
    <div class="bar"><div class="fill" style="width:{pct}%"></div></div>
    <span class="text">{done}/{total} · {label}</span>
    {#if onCancel}<button on:click={onCancel}>İptal ✕</button>{/if}
  </footer>
{/if}

<style>
  .statusbar { display: flex; align-items: center; gap: 10px; padding: 6px 14px;
    background: #0d1117; border-top: 1px solid #1b2530; font-family: monospace; font-size: 12px; }
  .bar { flex: 1; height: 6px; background: #1b2530; border-radius: 3px; overflow: hidden; }
  .fill { height: 100%; background: #58d6a0; transition: width .15s; }
  .text { color: #9aa7b4; white-space: nowrap; max-width: 40%; overflow: hidden; text-overflow: ellipsis; }
  button { background: #243140; color: #e6edf3; border: none; padding: 3px 8px;
    border-radius: 4px; cursor: pointer; font-family: monospace; }
</style>
```

- [ ] **Step 4: App.svelte'i bağla**

`gui/src/App.svelte`:

```svelte
<script lang="ts">
  import TopBar from "./lib/components/TopBar.svelte";
  import Sidebar from "./lib/components/Sidebar.svelte";
  import Dashboard from "./routes/Dashboard.svelte";
  import ScanView from "./routes/ScanView.svelte";

  let active = "Dashboard";
</script>

<div class="app">
  <TopBar />
  <div class="body">
    <Sidebar {active} onSelect={(v) => (active = v)} />
    <main class="content">
      {#if active === "Dashboard"}
        <Dashboard />
      {:else}
        <ScanView mode={active.toLowerCase()} />
      {/if}
    </main>
  </div>
</div>

<style>
  :global(body) { margin: 0; background: #0d1117; color: #e6edf3; }
  .app { display: flex; flex-direction: column; height: 100vh; }
  .body { display: flex; flex: 1; min-height: 0; }
  .content { flex: 1; overflow: auto; padding: 16px; }
</style>
```

`Dashboard.svelte` ve `ScanView.svelte` Task 8/9'da yazılacak; geçici olarak boş bileşenler oluştur ki derlensin:

```bash
mkdir -p gui/src/routes
printf '<div>Dashboard TODO</div>\n' > gui/src/routes/Dashboard.svelte
printf '<script lang="ts">export let mode: string;</script>\n<div>{mode} TODO</div>\n' > gui/src/routes/ScanView.svelte
```

- [ ] **Step 5: Çalıştır ve "sidecar bağlı" göstergesini doğrula**

Run:

```bash
cd gui && npm run tauri dev
```

Expected: Üstte "● sidecar bağlı" yeşile döner (serve `ready` olayı geldiğinde), sidebar görünür.

- [ ] **Step 6: Commit**

```bash
git add gui/src
git commit -m "feat(ui): sidecar store + app shell (topbar/sidebar/statusbar)"
```

---

## Task 8: Dashboard ekranı + canlı status poll

`status` op'una ~2 sn'de bir poll ile kart düzenli dashboard.

**Files:**
- Modify: `gui/src/routes/Dashboard.svelte`

- [ ] **Step 1: Dashboard'u yaz**

`gui/src/routes/Dashboard.svelte`:

```svelte
<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { request } from "../lib/sidecar";

  let s: Record<string, any> = {};
  let timer: number;

  async function poll() {
    const done = await request({ op: "status" });
    if (done.payload) s = done.payload as Record<string, any>;
  }
  function gb(n: number) { return (n / 1024 ** 3).toFixed(1) + " GB"; }

  onMount(() => { poll(); timer = setInterval(poll, 2000); });
  onDestroy(() => clearInterval(timer));
</script>

<div class="grid">
  <div class="card"><h3>CPU</h3><div class="big">{s.cpu_percent ?? "—"}%</div></div>
  <div class="card"><h3>Bellek</h3><div class="big">{s.memory_percent ?? "—"}%</div>
    <small>{s.memory_used ? gb(s.memory_used) : ""} / {s.memory_total ? gb(s.memory_total) : ""}</small></div>
  <div class="card"><h3>Disk</h3><div class="big">{s.disk_percent ?? "—"}%</div>
    <small>{s.disk_free ? gb(s.disk_free) : ""} boş</small></div>
  <div class="card"><h3>Sağlık</h3><div class="big">{s.health ?? "—"}</div></div>
  <div class="card"><h3>Uptime</h3><div class="big">{s.uptime_seconds ? Math.floor(s.uptime_seconds / 3600) + "s" : "—"}</div></div>
  <div class="card"><h3>Batarya</h3><div class="big">{s.battery_percent ?? "—"}{s.battery_percent != null ? "%" : ""}</div>
    <small>{s.power_plugged ? "şarjda" : ""}</small></div>
</div>

<style>
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; }
  .card { background: #11161c; border: 1px solid #1b2530; border-radius: 8px; padding: 14px; font-family: monospace; }
  h3 { margin: 0 0 8px; color: #6e7681; font-size: 12px; text-transform: uppercase; }
  .big { font-size: 28px; color: #58d6a0; }
  small { color: #9aa7b4; }
</style>
```

- [ ] **Step 2: Çalıştır ve canlı değerleri doğrula**

Run: `cd gui && npm run tauri dev`
Expected: Dashboard kartları gerçek CPU/RAM/disk değerleriyle dolar ve ~2 sn'de bir güncellenir.

- [ ] **Step 3: Commit**

```bash
git add gui/src/routes/Dashboard.svelte
git commit -m "feat(ui): live dashboard with status polling"
```

---

## Task 9: Scan ekranı — sanal liste + ilerleme

analyze/clean/purge/installers için ortak tarama ekranı; `item` olaylarını listele, progress'i StatusBar'a bağla.

**Files:**
- Create: `gui/src/lib/components/VirtualList.svelte`
- Modify: `gui/src/routes/ScanView.svelte`

- [ ] **Step 1: Basit sanal liste bileşeni yaz**

`gui/src/lib/components/VirtualList.svelte`:

```svelte
<script lang="ts">
  export let items: any[] = [];
  export let rowHeight = 26;
  let scrollTop = 0;
  let viewport: HTMLDivElement;
  let height = 400;
  $: start = Math.max(0, Math.floor(scrollTop / rowHeight) - 5);
  $: visibleCount = Math.ceil(height / rowHeight) + 10;
  $: slice = items.slice(start, start + visibleCount);
</script>

<div class="vp" bind:this={viewport} bind:clientHeight={height}
     on:scroll={() => (scrollTop = viewport.scrollTop)}>
  <div style="height:{items.length * rowHeight}px; position:relative;">
    {#each slice as item, i (item.path)}
      <div class="row" style="position:absolute; top:{(start + i) * rowHeight}px; height:{rowHeight}px;">
        <slot {item} />
      </div>
    {/each}
  </div>
</div>

<style>
  .vp { overflow: auto; height: 100%; }
  .row { left: 0; right: 0; display: flex; align-items: center; }
</style>
```

- [ ] **Step 2: ScanView'i yaz**

`gui/src/routes/ScanView.svelte`:

```svelte
<script lang="ts">
  import { request, cancel } from "../lib/sidecar";
  import type { ScanItem, SidecarEvent } from "../lib/types";
  import VirtualList from "../lib/components/VirtualList.svelte";
  import StatusBar from "../lib/components/StatusBar.svelte";

  export let mode: string;

  let items: ScanItem[] = [];
  let scanning = false;
  let activeId: string | null = null;
  let progress = { done: 0, total: 0, label: "" };

  function fmt(n: number) {
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; let v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return v.toFixed(1) + " " + u[i];
  }

  async function scan() {
    items = []; scanning = true; progress = { done: 0, total: 0, label: "" };
    const buf: ScanItem[] = [];
    const done = await request({ op: "scan", mode: mode as any }, (e: SidecarEvent) => {
      if (e.ev === "started") activeId = String(e.id);
      if (e.ev === "item") buf.push(e as unknown as ScanItem);
      if (e.ev === "progress") progress = { done: e.done as number, total: e.total as number, label: String(e.label ?? "") };
    });
    items = buf;
    scanning = false; activeId = null; progress = { done: 0, total: 0, label: "" };
    void done;
  }

  function toggle(it: ScanItem) { it.selected = !it.selected; items = items; }
  $: selected = items.filter((i) => i.selected);
</script>

<div class="scan">
  <div class="toolbar">
    <h2>{mode}</h2>
    <button on:click={scan} disabled={scanning}>{scanning ? "Taranıyor…" : "Tara"}</button>
    <span class="count">{items.length} öğe · {selected.length} seçili</span>
  </div>
  <div class="list">
    <VirtualList {items} let:item>
      <label class="entry">
        <input type="checkbox" checked={item.selected} on:change={() => toggle(item)} />
        <span class="size">{fmt(item.size)}</span>
        <span class="name">{item.path}</span>
      </label>
    </VirtualList>
  </div>
</div>
<StatusBar label={progress.label} done={progress.done} total={progress.total}
  onCancel={activeId ? () => cancel(activeId!) : null} />

<style>
  .scan { display: flex; flex-direction: column; height: 100%; font-family: monospace; }
  .toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
  .toolbar h2 { margin: 0; text-transform: capitalize; color: #e6edf3; }
  button { background: #243140; color: #e6edf3; border: none; padding: 6px 14px; border-radius: 4px; cursor: pointer; }
  button:disabled { opacity: .5; cursor: default; }
  .count { color: #9aa7b4; font-size: 12px; }
  .list { flex: 1; min-height: 0; background: #11161c; border: 1px solid #1b2530; border-radius: 8px; }
  .entry { display: flex; gap: 10px; align-items: center; padding: 0 10px; width: 100%; cursor: pointer; }
  .size { color: #58d6a0; min-width: 80px; text-align: right; }
  .name { color: #9aa7b4; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
```

- [ ] **Step 3: Çalıştır ve taramayı doğrula**

Run: `cd gui && npm run tauri dev`
Expected: "Clean"/"Purge"/"Analyze" sekmesinde "Tara" → liste dolar, ilerleme şeridi görünür, binlerce satırda akıcı kaydırma. (Silme henüz yok — sonraki task.)

- [ ] **Step 4: Commit**

```bash
git add gui/src/routes/ScanView.svelte gui/src/lib/components/VirtualList.svelte
git commit -m "feat(ui): scan view with virtual list and live progress"
```

---

## Task 10: Silme onay modalı + toast + yürütme

In-app modal ile onay (Geri Dönüşüm Kutusu / Kalıcı), `delete` op'unu çağır, sonuçları toast'la bildir. **Native dialog kullanılmaz.**

**Files:**
- Create: `gui/src/lib/components/ConfirmModal.svelte`, `gui/src/lib/components/Toast.svelte`, `gui/src/lib/toast.ts`
- Modify: `gui/src/routes/ScanView.svelte`, `gui/src/App.svelte`

- [ ] **Step 1: Toast store + bileşeni yaz**

`gui/src/lib/toast.ts`:

```ts
import { writable } from "svelte/store";

export interface Toast { id: number; text: string; kind: "ok" | "err" | "info"; }
export const toasts = writable<Toast[]>([]);
let n = 0;

export function toast(text: string, kind: Toast["kind"] = "info") {
  const id = ++n;
  toasts.update((t) => [...t, { id, text, kind }]);
  setTimeout(() => toasts.update((t) => t.filter((x) => x.id !== id)), 4000);
}
```

`gui/src/lib/components/Toast.svelte`:

```svelte
<script lang="ts">
  import { toasts } from "../toast";
</script>

<div class="wrap">
  {#each $toasts as t (t.id)}
    <div class="toast {t.kind}">{t.text}</div>
  {/each}
</div>

<style>
  .wrap { position: fixed; bottom: 16px; right: 16px; display: flex; flex-direction: column; gap: 8px; z-index: 50; }
  .toast { padding: 10px 14px; border-radius: 6px; font-family: monospace; font-size: 13px;
    background: #1b2530; color: #e6edf3; border-left: 3px solid #58d6a0; }
  .toast.err { border-left-color: #e5534b; }
  .toast.info { border-left-color: #539bf5; }
</style>
```

- [ ] **Step 2: Onay modalını yaz**

`gui/src/lib/components/ConfirmModal.svelte`:

```svelte
<script lang="ts">
  export let open = false;
  export let count = 0;
  export let bytes = 0;
  export let onConfirm: (permanent: boolean) => void;
  export let onCancel: () => void;

  let permanent = false;
  let confirmPermanent = false;

  function fmt(n: number) {
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; let v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return v.toFixed(1) + " " + u[i];
  }
  function confirm() {
    if (permanent && !confirmPermanent) { confirmPermanent = true; return; }
    onConfirm(permanent);
  }
</script>

{#if open}
  <div class="backdrop" on:click={onCancel}>
    <div class="modal" on:click|stopPropagation>
      <h3>Silme Onayı</h3>
      <p>{count} öğe · {fmt(bytes)}</p>
      <label class="opt">
        <input type="checkbox" bind:checked={permanent} on:change={() => (confirmPermanent = false)} />
        Kalıcı sil (Geri Dönüşüm Kutusu'nu atla)
      </label>
      {#if permanent && confirmPermanent}
        <p class="warn">⚠ Bu işlem GERİ ALINAMAZ. Onaylamak için tekrar "Kalıcı Sil" butonuna bas.</p>
      {/if}
      <div class="actions">
        <button class="ghost" on:click={onCancel}>Vazgeç</button>
        <button class:danger={permanent} on:click={confirm}>
          {permanent ? (confirmPermanent ? "Kalıcı Sil (onayla)" : "Kalıcı Sil") : "Geri Dönüşüm Kutusu'na Taşı"}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .backdrop { position: fixed; inset: 0; background: rgba(0,0,0,.6); display: flex;
    align-items: center; justify-content: center; z-index: 100; }
  .modal { background: #11161c; border: 1px solid #1b2530; border-radius: 10px;
    padding: 22px; min-width: 360px; font-family: monospace; color: #e6edf3; }
  h3 { margin: 0 0 12px; color: #58d6a0; }
  .opt { display: flex; gap: 8px; align-items: center; margin: 12px 0; color: #9aa7b4; }
  .warn { color: #e5534b; font-size: 13px; }
  .actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 16px; }
  button { border: none; padding: 8px 14px; border-radius: 5px; cursor: pointer; font-family: monospace; }
  .ghost { background: #243140; color: #e6edf3; }
  button.danger { background: #e5534b; color: white; }
  button:not(.ghost):not(.danger) { background: #2ea043; color: white; }
</style>
```

- [ ] **Step 3: ScanView'e silme akışını bağla**

`gui/src/routes/ScanView.svelte` `<script>` sonuna ekle:

```ts
  import ConfirmModal from "../lib/components/ConfirmModal.svelte";
  import { toast } from "../lib/toast";

  let modalOpen = false;
  $: selectedBytes = selected.reduce((s, i) => s + (i.size || 0), 0);

  function askDelete() { if (selected.length) modalOpen = true; }

  async function doDelete(permanent: boolean) {
    modalOpen = false;
    const targets = selected.map((i) => i.path);
    let ok = 0, err = 0;
    await request({ op: "delete", targets, permanent }, (e) => {
      if (e.ev === "item_result") (e.ok ? ok++ : err++);
      if (e.ev === "progress") progress = { done: e.done as number, total: e.total as number, label: String(e.label ?? "") };
    });
    progress = { done: 0, total: 0, label: "" };
    items = items.filter((i) => !(i.selected && !targets.includes(i.path) === false && i.selected) || false);
    items = items.filter((i) => !targets.includes(i.path) || false);
    items = items.filter((i) => !targets.includes(i.path));
    toast(`${ok} silindi${err ? `, ${err} hata` : ""}`, err ? "err" : "ok");
  }
```

> Yukarıdaki gereksiz filtre satırlarını tek satıra indir: `items = items.filter((i) => !targets.includes(i.path));`

Toolbar'a sil butonu ekle (`.count` span'ından sonra):

```svelte
    <button class="danger" on:click={askDelete} disabled={!selected.length}>Sil…</button>
```

ve toolbar `<style>` içine:

```css
  button.danger { background: #e5534b; color: white; }
```

Şablon sonuna modalı ekle (StatusBar'dan önce):

```svelte
<ConfirmModal open={modalOpen} count={selected.length} bytes={selectedBytes}
  onConfirm={doDelete} onCancel={() => (modalOpen = false)} />
```

- [ ] **Step 4: App.svelte'e Toast'u ekle**

`gui/src/App.svelte` şablonunda `</div>` (`.app`) öncesine ekle, `<script>` içine import:

```svelte
  import Toast from "./lib/components/Toast.svelte";
```

```svelte
  <Toast />
```

- [ ] **Step 5: Uçtan uca test (dry-run davranışı kullanıcı tarafından)**

Run: `cd gui && npm run tauri dev`
Expected: Clean/Purge'de tara → öğe seç → "Sil…" → modal açılır → "Geri Dönüşüm Kutusu'na Taşı" → öğeler listeden kalkar, sağ altta yeşil toast. "Kalıcı sil" işaretlenince ikinci onay ister. Pencere hiç donmaz.

- [ ] **Step 6: Commit**

```bash
git add gui/src
git commit -m "feat(ui): in-app delete confirm modal + toasts, wired to delete op"
```

---

## Task 11: Sürüm exe'sini sidecar olarak doğrula + çalıştırma talimatı

Geliştirme `dist/wmole.exe` kopyasıyla çalışıyor. Bu task, kullanıcı testinden önce sidecar'ın `serve` modunu içeren güncel exe ile çalıştığını garanti eder.

**Files:**
- Modify: `gui/src-tauri/binaries/wmole-x86_64-pc-windows-msvc.exe` (güncel build)

- [ ] **Step 1: serve içeren güncel exe'yi derle**

Run:

```bash
py -m PyInstaller wmole.spec --noconfirm
cp dist/wmole.exe "gui/src-tauri/binaries/wmole-x86_64-pc-windows-msvc.exe"
```

Expected: `dist/wmole.exe` yeniden üretilir (artık `serve` modunu içerir).

- [ ] **Step 2: Sidecar serve'ün exe üzerinden çalıştığını doğrula**

Run:

```bash
echo {"id":"t","op":"ping"} | "gui/src-tauri/binaries/wmole-x86_64-pc-windows-msvc.exe" serve
```

Expected: `{"id": null, "ev": "ready"}` ve `{"id": "t", "ev": "done", "ok": true}` satırları.

- [ ] **Step 3: Tam uygulamayı çalıştır (kullanıcı testi)**

Run: `cd gui && npm run tauri dev`
Expected: Dashboard canlı, tarama akıcı, silme modalı çalışır. Kullanıcı testi için hazır.

- [ ] **Step 4: Commit**

```bash
git add gui/src-tauri/binaries
git commit -m "build: bundle serve-enabled sidecar binary for dev run"
```

---

## Self-Review Notları

- **Spec kapsamı (Faz 1–4):** serve protokolü (Task 2-4), Rust köprü (Task 6), dashboard (Task 8), scan ekranları + silme modalı + iptal (Task 9-10) — hepsi kapsandı. In-app dialog kuralı Task 10'da uygulandı (native dialog yok). Faz 5-6 ayrı planda.
- **Tip tutarlılığı:** `request()`/`cancel()`/`SidecarEvent`/`ScanItem` tüm task'larda aynı imzayla kullanıldı. serve olay anahtarları (`ev`, `id`, `item`, `item_result`, `progress`, `done`) Python ve TS tarafında eşleşiyor.
- **Placeholder:** Geçici `Dashboard/ScanView TODO` bileşenleri Task 7'de bilinçli iskelet; Task 8/9'da gerçek içerikle değiştiriliyor. Task 10 Step 3'teki fazlalık filtre satırları aynı adımda tek satıra indiriliyor (not düşüldü).
- **Bilinen sınır:** `scan` sırasında iptal yalnızca sonuç akışı aşamasında etkilidir (Scanner.run bloke); UI eşzamanlılık sayesinde yine donmaz. Tarama-ortası iptal Faz 5 iyileştirmesi olarak işaretlendi.
