import json
import io
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mole
from rich.console import Console


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

    def test_palette_exposes_operations_and_existing_context_actions(self):
        with mock.patch.object(mole, "LANG", "en"):
            names = [cmd["name"] for cmd in mole.palette_commands()]

        self.assertEqual(
            names,
            [
                "analyze",
                "categories",
                "clean",
                "purge",
                "installers",
                "uninstall",
                "optimize",
                "status",
                "ports",
                "update",
                "help",
                "large",
                "drives",
                "select",
                "delete",
                "permanent",
                "open",
                "refresh",
                "leftovers",
                "lang",
                "back",
                "quit",
            ],
        )
        self.assertEqual(mole.filter_palette("/permanent")[0]["action"], "key:k")
        self.assertEqual(mole.filter_palette("/quit")[0]["action"], "exec:quit")

    def test_palette_can_render_selection_beyond_first_page(self):
        output = io.StringIO()
        test_console = Console(record=True, width=120, color_system=None, file=output)
        with mock.patch.object(mole, "LANG", "en"):
            last_index = len(mole.palette_commands()) - 1
            test_console.print(mole.render_palette("", last_index))

        rendered = test_console.export_text()
        self.assertIn("/quit", rendered)
        self.assertIn("> /quit", rendered)
        self.assertNotIn("/analyze", rendered)

    def test_enter_runs_highlighted_palette_command_after_scrolling(self):
        keys = iter(
            ["/"] + ["DOWN"] * (len(mole.palette_commands()) - 1) + ["ENTER"]
        )
        fake_scanner = mock.Mock(
            status="",
            done=True,
            current_cat_key=None,
            current_item_id=None,
        )
        fake_live = mock.MagicMock()
        fake_live.__enter__.return_value = fake_live
        fake_live.__exit__.return_value = False
        empty_category = mole.Category(
            key="fs:test", title=r"C:\Users\example", description="", items=[]
        )
        with (
            mock.patch.object(mole, "Scanner", return_value=fake_scanner),
            mock.patch.object(mole.threading, "Thread") as thread,
            mock.patch.object(mole, "Live", return_value=fake_live),
            mock.patch.object(mole, "build_fs_category", return_value=empty_category),
            mock.patch.object(mole, "load_whitelist", return_value=[]),
            mock.patch.object(mole, "load_config", return_value={}),
            mock.patch.object(mole, "free_space_gb", return_value=18.1),
            mock.patch.object(mole.os, "system"),
            mock.patch.object(mole.msvcrt, "kbhit", return_value=True),
            mock.patch.object(mole, "read_key", side_effect=lambda: next(keys)),
        ):
            mole.run_tui()

        thread.return_value.start.assert_called_once()

    def test_command_input_is_visible_when_palette_is_idle(self):
        output = io.StringIO()
        console = Console(record=True, width=100, color_system=None, file=output)
        with mock.patch.object(mole, "LANG", "en"):
            console.print(mole.render_command_input())

        rendered = console.export_text()
        self.assertIn("Type / to open operations", rendered)

    def test_help_and_footer_do_not_advertise_top_level_mode_hotkeys(self):
        with mock.patch.object(mole, "LANG", "en"):
            help_text = "\n".join(
                line for _, lines in mole._help_sections_en() for line in lines
            )
            footer_text = "\n".join(mole.build_footer_lines(160))

        self.assertIn("/analyze", help_text)
        self.assertNotIn("Shift+A", help_text)
        self.assertNotIn("A Analyze(FS)", footer_text)

    def test_open_palette_places_command_rows_within_visible_terminal_height(self):
        output = io.StringIO()
        test_console = Console(
            record=True, width=140, height=40, color_system=None, file=output
        )
        items = [
            mole.Item(path=Path(fr"C:\Users\example\folder-{index}"), size=index + 1)
            for index in range(30)
        ]
        category = mole.Category(
            key="fs:test",
            title=r"C:\Users\example",
            description="",
            items=items,
            scanning=False,
        )
        scanner = mock.Mock(
            status="",
            done=True,
            current_cat_key=None,
            current_item_id=None,
        )
        view = mole.View(title=category.title, kind="items", category=category)

        with (
            mock.patch.object(mole, "console", test_console),
            mock.patch.object(mole, "LANG", "en"),
            mock.patch.object(mole, "free_space_gb", return_value=18.1),
        ):
            test_console.print(
                mole.render(
                    scanner, view, 0, "", True, False, [], [], palette=("", 0)
                )
            )

        lines = test_console.export_text().splitlines()
        first_command_row = next(
            (index for index, line in enumerate(lines) if "/analyze" in line),
            None,
        )
        last_command_row = next(
            (index for index, line in enumerate(lines) if "/help" in line),
            None,
        )
        self.assertIsNotNone(first_command_row)
        self.assertIsNotNone(last_command_row)
        self.assertLess(first_command_row, test_console.height)
        self.assertLess(last_command_row, test_console.height)
        self.assertLessEqual(len(lines), test_console.height)

    def test_open_palette_scrolls_inside_border_in_short_terminal(self):
        output = io.StringIO()
        test_console = Console(
            record=True, width=140, height=30, color_system=None, file=output
        )
        items = [
            mole.Item(path=Path(fr"C:\Users\example\folder-{index}"), size=index + 1)
            for index in range(30)
        ]
        category = mole.Category(
            key="fs:test",
            title=r"C:\Users\example",
            description="",
            items=items,
            scanning=False,
        )
        scanner = mock.Mock(
            status="",
            done=True,
            current_cat_key=None,
            current_item_id=None,
        )
        view = mole.View(title=category.title, kind="items", category=category)
        with (
            mock.patch.object(mole, "console", test_console),
            mock.patch.object(mole, "LANG", "en"),
            mock.patch.object(mole, "free_space_gb", return_value=18.1),
        ):
            test_console.print(
                mole.render(
                    scanner, view, 0, "", True, False, [], [],
                    palette=("", len(mole.palette_commands()) - 1),
                )
            )

        lines = test_console.export_text().splitlines()
        self.assertIn("/quit", "\n".join(lines))
        self.assertLessEqual(len(lines), test_console.height)
        self.assertTrue(lines[-1].startswith("\u2514"))

    def test_open_palette_prioritizes_closed_frame_in_minimal_terminal(self):
        output = io.StringIO()
        test_console = Console(
            record=True, width=140, height=22, color_system=None, file=output
        )
        items = [
            mole.Item(path=Path(fr"C:\Users\example\folder-{index}"), size=index + 1)
            for index in range(30)
        ]
        category = mole.Category(
            key="fs:test",
            title=r"C:\Users\example",
            description="",
            items=items,
            scanning=False,
        )
        scanner = mock.Mock(
            status="",
            done=True,
            current_cat_key=None,
            current_item_id=None,
        )
        view = mole.View(title=category.title, kind="items", category=category)
        with (
            mock.patch.object(mole, "console", test_console),
            mock.patch.object(mole, "LANG", "en"),
            mock.patch.object(mole, "free_space_gb", return_value=18.1),
        ):
            test_console.print(
                mole.render(
                    scanner, view, 0, "", True, False, [], [],
                    palette=("", len(mole.palette_commands()) - 1),
                )
            )

        lines = test_console.export_text().splitlines()
        self.assertIn("> /quit", "\n".join(lines))
        self.assertLessEqual(len(lines), test_console.height)
        self.assertTrue(lines[-1].startswith("\u2514"))

    def test_quiet_update_check_returns_visible_up_to_date_status(self):
        release = {"tag_name": f"v{mole.__version__}", "assets": []}
        stdout = io.StringIO()
        with (
            mock.patch.object(mole.sys, "frozen", True, create=True),
            mock.patch.object(mole, "fetch_latest_release", return_value=release),
            contextlib.redirect_stdout(stdout),
        ):
            status = mole.cli_update(json_out=False, quiet=True)

        self.assertEqual(status, "already up to date")
        self.assertEqual(stdout.getvalue(), "")

    def test_turkish_tui_update_status_explicitly_reports_current_version(self):
        with mock.patch.object(mole, "LANG", "tr"):
            self.assertEqual(
                mole.format_tui_update_status("already up to date"),
                "Sürüm güncel.",
            )

    def test_disabling_auto_update_does_not_start_background_check(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td) / ".wmole"
            config_file = state_dir / "config.json"
            stdout = io.StringIO()
            with (
                mock.patch("sys.argv", ["wmole", "update", "--disable-auto", "--json"]),
                mock.patch.object(mole, "WMOLE_DIR", state_dir),
                mock.patch.object(mole, "CONFIG_FILE", config_file),
                mock.patch.object(mole, "load_config", return_value={}),
                mock.patch.object(mole, "load_whitelist", return_value=[]),
                mock.patch.object(mole, "start_auto_update_check") as start_check,
                contextlib.redirect_stdout(stdout),
            ):
                mole.main_cli()

        start_check.assert_not_called()
        self.assertFalse(json.loads(stdout.getvalue())["auto_update"])

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
