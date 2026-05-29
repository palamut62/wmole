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
    def test_read_cleanup_history_sums_only_successful_deletions(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "operations.log"
            log.write_text(
                "2026-05-29 10:00:00\tdelete-trash\t100\tC:\\a\\cache\tok\n"
                "2026-05-29 10:01:00\tdelete-permanent\t50\tC:\\b\\tmp\tok\n"
                "2026-05-29 10:02:00\tdelete-dry-run\t999\tC:\\c\\x\tok\n"
                "2026-05-29 10:03:00\tdelete-trash\t7\tC:\\d\\y\ttrash: failed\n"
                "2026-05-29 10:04:00\tdelete-blocked\t30\tC:\\Windows\tprotected path\n",
                encoding="utf-8",
            )
            with mock.patch.object(mole, "OP_LOG_FILE", log):
                hist = mole.read_cleanup_history(limit=5)
            self.assertEqual(hist["count"], 2)
            self.assertEqual(hist["total_freed"], 150)
            self.assertEqual(hist["last_ts"], "2026-05-29 10:01:00")
            # newest-first ordering
            self.assertEqual(hist["recent"][0]["path"], "C:\\b\\tmp")

    def test_render_dashboard_handles_missing_scanner(self):
        with mock.patch.object(mole, "OP_LOG_FILE", Path("does-not-exist.log")):
            group = mole.render_dashboard(scanner=None)
        self.assertIsNotNone(group)

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
                "dashboard",
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
            mock.patch.object(mole, "load_config", return_value={"quick_clean_v2_done": True}),
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

            capped = mole.dir_size(root, max_files=1)
            self.assertLessEqual(capped, 350)
            self.assertGreater(capped, 0)

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
                self.assertIsNone(mole.cache_get(reloaded, target, mtime + 10))

    def test_should_redraw_throttles_idle_and_allows_active(self):
        self.assertTrue(mole.should_redraw(last_draw=0.0, now=1.0,
                                            scanner_active=True, min_interval=0.08))
        self.assertFalse(mole.should_redraw(last_draw=1.0, now=1.02,
                                            scanner_active=True, min_interval=0.08))
        self.assertFalse(mole.should_redraw(last_draw=0.0, now=100.0,
                                            scanner_active=False, min_interval=0.08))

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

                with mock.patch.object(mole, "dir_size",
                                       side_effect=AssertionError("should be cached")):
                    sc2 = mole.Scanner(profile="purge", roots=[Path(td)])
                    sc2.run()
                total = sum(c.total for c in sc2.categories)
                self.assertGreaterEqual(total, 4000)

    def test_scanner_use_cache_false_skips_load(self):
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "cache.json"
            cache_file.write_text('{"x": {"mtime": 1, "size": 9}}', encoding="utf-8")
            with mock.patch.object(mole, "CACHE_FILE", cache_file):
                sc = mole.Scanner(profile="idle", use_cache=False)
                self.assertEqual(sc.cache, {})

    def test_denylist_uses_prefix_not_substring(self):
        with tempfile.TemporaryDirectory() as td:
            deny = Path(td) / "data"
            deny.mkdir()
            with mock.patch.object(mole, "load_denylist", return_value=[deny]):
                # exact and real child are protected
                self.assertTrue(mole.is_protected_path(deny))
                self.assertTrue(mole.is_protected_path(deny / "sub"))
                # sibling that merely shares a name prefix is NOT protected
                self.assertFalse(mole.is_protected_path(Path(td) / "database"))

    def test_delete_path_no_send2trash_does_not_permanently_delete(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "keep.txt"
            f.write_bytes(b"data")
            with mock.patch.object(mole, "send2trash", None):
                err = mole.delete_path(f, use_trash=True, dry_run=False)
            self.assertIsNotNone(err)
            self.assertTrue(f.exists())  # must NOT be permanently deleted

    def test_is_leftover_match_is_strict(self):
        tokens = mole.normalize_app_tokens("GitHub Desktop")
        self.assertTrue(mole.is_leftover_match(Path("GitHubDesktop"), tokens))
        self.assertTrue(mole.is_leftover_match(Path("git"), ["git"]))  # exact short token ok
        # unrelated folder must not match short tokens
        self.assertFalse(mole.is_leftover_match(Path("gitignore"), ["vlc", "obs"]))
        # "git" token must not loosely match a longer unrelated folder
        self.assertFalse(mole.is_leftover_match(Path("gitkraken"), ["git"]))

    def test_cache_get_ttl_expiry(self):
        cache = {}
        p = Path("C:/tmp/x")
        mole.cache_set(cache, p, 123.0, 999)
        # force an old scanned_at
        cache[str(p)]["scanned_at"] = 0
        self.assertIsNone(mole.cache_get(cache, p, 123.0))

    def test_cli_optimize_json_skips_high_risk_without_yes(self):
        buf = io.StringIO()
        # Mock run_optimize so no real system commands execute during the test.
        with mock.patch.object(mole, "run_optimize", return_value="ran"):
            with contextlib.redirect_stdout(buf):
                mole.cli_optimize(dry_run=False, json_out=True, yes=False)
        data = json.loads(buf.getvalue())
        highs = [r for r in data["results"] if r.get("risk") == "high"]
        self.assertTrue(highs)
        self.assertTrue(all("requires --yes" in r["result"] for r in highs))
        # high-risk actions must never have been executed
        self.assertTrue(all(r["result"] != "ran" for r in highs))


if __name__ == "__main__":
    unittest.main()
