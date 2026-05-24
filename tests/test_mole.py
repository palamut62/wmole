import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mole


class WmoleBehaviorTests(unittest.TestCase):
    def test_analyze_path_entries_reports_children_and_large_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "small.txt").write_bytes(b"a" * 10)
            nested = root / "nested"
            nested.mkdir()
            (nested / "big.bin").write_bytes(b"b" * 2048)

            result = mole.analyze_path(root, large_file_min=1024)

            self.assertEqual(result["path"], str(root))
            self.assertEqual(result["total_files"], 2)
            self.assertEqual(result["total_size"], 2058)
            self.assertEqual(result["entries"][0]["name"], "nested")
            self.assertEqual(result["entries"][0]["size"], 2048)
            self.assertEqual(result["large_files"][0]["name"], "big.bin")
            json.dumps(result)

    def test_purge_candidates_only_include_known_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            keep = root / "src"
            keep.mkdir()
            artifact = root / "app" / "node_modules"
            artifact.mkdir(parents=True)
            (artifact / "pkg.txt").write_bytes(b"x" * 3)

            categories = mole.build_purge_categories([root], whitelist=[])

            paths = [item.path for cat in categories for item in cat.items]
            self.assertIn(artifact, paths)
            self.assertNotIn(keep, paths)

    def test_protected_path_blocks_drive_root_and_windows_dir(self):
        self.assertTrue(mole.is_protected_path(Path("C:\\")))
        self.assertTrue(mole.is_protected_path(Path(r"C:\Windows\Temp")))
        self.assertFalse(mole.is_protected_path(Path(r"C:\Users\example\AppData\Local\Temp\wmole-safe")))

    def test_installer_categories_are_separate_from_full_scanner(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            installer = root / "setup.exe"
            installer.write_bytes(b"x" * 7)

            categories = mole.build_installer_categories([root], days=0)

            self.assertEqual(len(categories), 1)
            self.assertEqual(categories[0].key, "installers")
            self.assertEqual(categories[0].items[0].path, installer)
            self.assertEqual(categories[0].items[0].size, 7)

    def test_fixed_categories_include_common_windows_app_caches(self):
        keys = {row[0] for row in mole.build_fixed_path_categories()}

        self.assertIn("firefox-cache", keys)
        self.assertIn("brave-cache", keys)
        self.assertIn("discord-cache", keys)
        self.assertIn("teams-cache", keys)
        self.assertIn("thumbnail-cache", keys)
        self.assertIn("directx-shader-cache", keys)
        self.assertIn("store-cache", keys)
        self.assertIn("wsl-logs", keys)

    def test_configured_purge_roots_override_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            config_root = home / "custom-work"
            config_root.mkdir()
            paths_file = home / ".wmole" / "purge_paths.txt"
            paths_file.parent.mkdir()
            paths_file.write_text("# comment\n~/custom-work\n\n", encoding="utf-8")

            roots = mole.load_purge_roots(home=home)

            self.assertEqual(roots, [config_root])

    def test_firefox_fixed_category_targets_cache2_not_profile_root(self):
        with tempfile.TemporaryDirectory() as td:
            profiles = Path(td) / "Profiles"
            cache = profiles / "abc.default" / "cache2"
            cache.mkdir(parents=True)

            items = mole.fixed_category_items("firefox-cache", profiles, selected=True)

            self.assertEqual([item.path for item in items], [cache])

    def test_fixed_category_items_skips_inaccessible_path(self):
        blocked = Path(r"C:\blocked-cache")
        with mock.patch.object(Path, "exists", side_effect=PermissionError("denied")):
            items = mole.fixed_category_items("delivery-optimization", blocked, selected=True)

        self.assertEqual(items, [])

    def test_clean_scanner_does_not_scan_dev_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact = root / "project" / "node_modules"
            artifact.mkdir(parents=True)
            (artifact / "pkg.txt").write_bytes(b"x" * 5)

            scanner = mole.Scanner(whitelist=[], profile="clean", roots=[root])
            scanner.run()

            keys = {cat.key for cat in scanner.categories}
            self.assertNotIn("dev-node_modules", keys)

    def test_purge_scanner_only_builds_purge_categories(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact = root / "project" / "node_modules"
            artifact.mkdir(parents=True)
            (artifact / "pkg.txt").write_bytes(b"x" * 5)

            scanner = mole.Scanner(whitelist=[], profile="purge", roots=[root])
            scanner.run()

            self.assertEqual([cat.key for cat in scanner.categories], ["dev-node_modules"])

    def test_leftover_matching_normalizes_app_names_and_includes_shortcuts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            appdata = root / "Local"
            shortcuts = root / "Start Menu"
            target = appdata / "FooBar"
            shortcut = shortcuts / "Foo Bar.lnk"
            target.mkdir(parents=True)
            shortcuts.mkdir()
            shortcut.write_bytes(b"shortcut")

            app = {"name": "Foo Bar 2024 (x64)", "publisher": "Foo Inc."}
            items = mole.find_leftover_candidates(app, roots=[appdata, shortcuts])

            paths = {item.path for item in items}
            self.assertIn(target, paths)
            self.assertIn(shortcut, paths)

    def test_collect_selected_targets_separates_protected_items(self):
        allowed = mole.Item(path=Path(r"C:\Users\example\AppData\Local\Temp\cache"), size=10, selected=True)
        blocked = mole.Item(path=Path(r"C:\Windows\Temp"), size=20, selected=True)
        cat = mole.Category(key="test", title="Test", description="", items=[allowed, blocked])

        result = mole.collect_selected_targets([cat], estimated=True)

        self.assertEqual([row["path"] for row in result["targets"]], [str(allowed.path)])
        self.assertEqual(result["total"], 10)
        self.assertEqual(result["blocked"][0]["path"], str(blocked.path))
        self.assertTrue(result["estimated"])

    def test_filter_apps_supports_query_and_limit(self):
        apps = [
            {"name": "Zoom Workplace", "publisher": "Zoom"},
            {"name": "Git", "publisher": "The Git Development Community"},
            {"name": "Google Chrome", "publisher": "Google"},
        ]

        filtered = mole.filter_apps(apps, query="git", limit=1)

        self.assertEqual(filtered, [apps[1]])

    def test_parse_paths_arg_supports_semicolon_and_comma(self):
        paths = mole.parse_paths_arg(r"C:\A;C:\B, C:\C")
        self.assertEqual(paths, [Path(r"C:\A"), Path(r"C:\B"), Path(r"C:\C")])

    def test_git_root_is_protected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".git").mkdir()
            self.assertTrue(mole.is_protected_path(root))

    def test_build_fs_category_contains_up_entry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "dir1").mkdir()
            (root / "file1.txt").write_bytes(b"x")

            cat = mole.build_fs_category(root)

            self.assertTrue(cat.key.startswith("fs:"))
            self.assertEqual(cat.items[0].error, "up")

    def test_run_optimize_windows_update_dry_run(self):
        action = next(a for a in mole.OPTIMIZE_ACTIONS if a.key == "windows-update")
        res = mole.run_optimize(action, dry_run=True)
        self.assertIn("dry-run", res)


if __name__ == "__main__":
    unittest.main()
