import unittest
import subprocess
import sys
import json
import os
import tempfile
import pathlib

import mole


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


class StatusPayloadTest(unittest.TestCase):
    def test_status_payload_keys(self):
        payload = mole.status_payload()
        # psutil yoksa boş sözlük döner; varsa anahtarlar bulunmalı
        if payload:
            for key in ("cpu_percent", "memory_percent", "disk_percent",
                        "uptime_seconds", "device"):
                self.assertIn(key, payload)


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


class ServeParityTest(unittest.TestCase):
    def test_optimize_list(self):
        events, err = _run_serve([{"id": "o1", "op": "optimize_list"}])
        ids = [e for e in events if e["id"] == "o1"]
        items = [e for e in ids if e["ev"] == "item"]
        self.assertTrue(items, msg=err)
        self.assertIn("risk", items[0])
        self.assertTrue(any(e["ev"] == "done" and e["ok"] for e in ids))

    def test_optimize_dry_run(self):
        events, err = _run_serve([
            {"id": "o2", "op": "optimize_run", "keys": ["flush-dns"], "dry_run": True}
        ])
        ids = [e for e in events if e["id"] == "o2"]
        results = [e for e in ids if e["ev"] == "item_result"]
        self.assertTrue(results and results[0]["ok"], msg=err)

    def test_ports_list(self):
        events, err = _run_serve([{"id": "pl", "op": "ports_list"}])
        ids = [e for e in events if e["id"] == "pl"]
        self.assertTrue(any(e["ev"] == "done" and e["ok"] for e in ids), msg=err)

    def test_uninstall_list(self):
        events, err = _run_serve([{"id": "u1", "op": "uninstall_list", "limit": 5}])
        ids = [e for e in events if e["id"] == "u1"]
        self.assertTrue(any(e["ev"] == "done" and e["ok"] for e in ids), msg=err)

    def test_remove_dry_run(self):
        events, err = _run_serve([
            {"id": "rm", "op": "remove", "dry_run": True}
        ])
        ids = [e for e in events if e["id"] == "rm"]
        done = [e for e in ids if e["ev"] == "done"]
        self.assertTrue(done and done[0]["ok"], msg=err)
        self.assertIn("payload", done[0])


class ServeExtraTest(unittest.TestCase):
    def test_cleanup_history(self):
        events, err = _run_serve([{"id": "h", "op": "cleanup_history"}])
        done = [e for e in events if e["id"] == "h" and e["ev"] == "done"]
        self.assertTrue(done, msg=err)
        self.assertIn("payload", done[0])
        self.assertIn("total_freed", done[0]["payload"])

    def test_drives(self):
        events, err = _run_serve([{"id": "d", "op": "drives"}])
        done = [e for e in events if e["id"] == "d" and e["ev"] == "done"]
        self.assertTrue(done, msg=err)
        self.assertIn("drives", done[0]["payload"])

    def test_large_scan(self):
        with tempfile.TemporaryDirectory() as d:
            (pathlib.Path(d) / "x.bin").write_text("x")
            events, err = _run_serve([
                {"id": "lg", "op": "scan", "mode": "large", "paths": [d]}
            ])
            ids = [e for e in events if e["id"] == "lg"]
            self.assertTrue(any(e["ev"] == "done" and e["ok"] for e in ids), msg=err)


class ServeFeatureTest(unittest.TestCase):
    def test_settings_get(self):
        events, err = _run_serve([{"id": "s", "op": "settings_get"}])
        done = [e for e in events if e["id"] == "s" and e["ev"] == "done"]
        self.assertTrue(done, msg=err)
        for k in ("config", "whitelist", "denylist", "purge_paths"):
            self.assertIn(k, done[0]["payload"])

    def test_is_admin(self):
        events, err = _run_serve([{"id": "a", "op": "is_admin"}])
        done = [e for e in events if e["id"] == "a" and e["ev"] == "done"]
        self.assertTrue(done, msg=err)
        self.assertIn("is_admin", done[0]["payload"])

    def test_startup_list(self):
        events, err = _run_serve([{"id": "sl", "op": "startup_list"}])
        ids = [e for e in events if e["id"] == "sl"]
        self.assertTrue(any(e["ev"] == "done" and e["ok"] for e in ids), msg=err)

    def test_schedule_get(self):
        events, err = _run_serve([{"id": "sg", "op": "schedule_get"}])
        done = [e for e in events if e["id"] == "sg" and e["ev"] == "done"]
        self.assertTrue(done, msg=err)
        self.assertIn("enabled", done[0]["payload"])

    def test_processes_list(self):
        events, err = _run_serve([{"id": "pr", "op": "processes_list"}])
        ids = [e for e in events if e["id"] == "pr"]
        self.assertTrue(any(e["ev"] == "done" and e["ok"] for e in ids), msg=err)

    def test_settings_set_roundtrip(self):
        events, err = _run_serve([
            {"id": "w", "op": "settings_set", "whitelist": ["C:\\test-wl-xyz"]},
        ])
        done = [e for e in events if e["id"] == "w" and e["ev"] == "done"]
        self.assertTrue(done, msg=err)
        self.assertIn("C:\\test-wl-xyz", done[0]["payload"]["whitelist"])
        # temizle
        _run_serve([{"id": "c", "op": "settings_set", "whitelist": []}])


if __name__ == "__main__":
    unittest.main()
