"""Tests for the per-item explanation/risk layer, real on-disk accounting,
quarantine integrity, the crash-safe journal and handle-addressed deletion."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mole


class ExplainTests(unittest.TestCase):
    def test_known_category_answers_all_six_questions(self):
        card = mole.explain_item(Path(r"C:\x\npm-cache"), kind="npm-cache")
        self.assertIn("npm", card.what)
        self.assertTrue(card.lost and card.kept)
        self.assertEqual(card.rebuild, "npm cache verify")
        self.assertTrue(card.regenerable)
        self.assertTrue(card.when_freed)
        self.assertTrue(card.reversible)

    def test_unknown_path_is_treated_as_irreplaceable_user_data(self):
        card = mole.explain_item(Path(r"C:\Users\me\thesis"), kind="")
        self.assertFalse(card.regenerable)
        self.assertEqual(card.rebuild, "")

    def test_folder_name_alone_identifies_dev_artifacts(self):
        self.assertEqual(mole._explain_key(Path(r"C:\p\node_modules")), "node_modules")
        self.assertEqual(mole._explain_key(Path(r"C:\p\__pycache__")), "__pycache__")

    def test_known_location_recovers_its_category_from_the_path_alone(self):
        self.assertEqual(mole.kind_for_path(mole.APPDATA / "npm-cache"), "npm-cache")
        self.assertEqual(mole.kind_for_path(mole.APPDATA / "npm-cache" / "_cacache"),
                         "npm-cache")
        self.assertEqual(mole.kind_for_path(Path(r"C:\nowhere\at\all")), "")

    def test_reversibility_text_follows_the_configured_delete_mode(self):
        with mock.patch.object(mole, "load_config",
                               return_value={"quarantine_enabled": False}), \
             mock.patch.object(mole, "send2trash", None):
            card = mole.explain_item(Path(r"C:\x\y"), kind="temp")
        self.assertEqual(card.when_freed, mole._FREED_IMMEDIATE)
        self.assertTrue(card.reversible.startswith("No"))


class RiskScoreTests(unittest.TestCase):
    def test_score_is_the_sum_of_its_named_modifiers(self):
        risk = mole.risk_score(Path(r"C:\nonexistent\npm-cache"),
                               kind="npm-cache", check_git=False)
        self.assertEqual(risk.score,
                         max(0, min(100, sum(m.points for m in risk.modifiers))))
        self.assertIn("base", [m.code for m in risk.modifiers])

    def test_cache_scores_lower_than_unrecognised_user_data(self):
        cache = mole.risk_score(Path(r"C:\x\npm-cache"), kind="npm-cache", check_git=False)
        data = mole.risk_score(Path(r"C:\x\photos"), kind="", check_git=False)
        self.assertLess(cache.score, data.score)

    def test_score_is_clamped_and_levelled(self):
        self.assertEqual(mole._risk_level(0), "low")
        self.assertEqual(mole._risk_level(30), "moderate")
        self.assertEqual(mole._risk_level(60), "high")
        self.assertEqual(mole._risk_level(100), "critical")

    def test_quarantine_lowers_the_score_and_says_so(self):
        with mock.patch.object(mole, "load_config",
                               return_value={"quarantine_enabled": True,
                                             "quarantine_retention_days": 30}):
            risk = mole.risk_score(Path(r"C:\x\dist"), kind="dist", check_git=False)
        codes = {m.code: m.points for m in risk.modifiers}
        self.assertLess(codes["quarantined"], 0)


class OnDiskSizeTests(unittest.TestCase):
    def test_tiny_files_cost_at_least_one_allocation_unit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.txt").write_bytes(b"x")
            (root / "b.txt").write_bytes(b"y")
            facts = mole.disk_facts(root)
        self.assertEqual(facts.logical, 2)
        self.assertEqual(facts.files, 2)
        cluster = mole.cluster_size(Path(tempfile.gettempdir()))
        if cluster:
            # Two 1-byte files still occupy two whole clusters.
            self.assertEqual(facts.on_disk, 2 * cluster)
            self.assertGreater(facts.on_disk, facts.logical)

    def test_cloud_placeholders_count_as_zero_reclaimable_bytes(self):
        self.assertTrue(mole.is_cloud_placeholder(mole.FILE_ATTRIBUTE_OFFLINE))
        self.assertTrue(mole.is_cloud_placeholder(
            mole.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS))
        self.assertFalse(mole.is_cloud_placeholder(0))
        self.assertEqual(
            mole.file_on_disk_size(Path(r"C:\x\cloud.psd"), 5 * 1024 ** 3,
                                   mole.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS),
            0)

    def test_placeholders_are_reported_separately_from_reclaimable_bytes(self):
        # A dehydrated OneDrive file reports a big logical length but occupies
        # nothing locally. Size is the only handle the walk gives us onto which
        # entry is which, so the fake keys the attribute off it.
        cloud_len, real_len = 9001, 4096
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "real.bin").write_bytes(b"z" * real_len)
            (root / "cloud.bin").write_bytes(b"z" * cloud_len)
            with mock.patch.object(
                    mole, "_entry_attrs",
                    lambda st: (mole.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
                                if st.st_size == cloud_len else 0)):
                facts = mole.disk_facts(root)
        self.assertEqual(facts.files, 2)
        self.assertEqual(facts.placeholder_files, 1)
        self.assertEqual(facts.placeholder_bytes, cloud_len)
        # The placeholder counts towards `logical` but frees nothing on delete.
        self.assertEqual(facts.logical, cloud_len + real_len)
        self.assertLess(facts.on_disk, facts.logical)

    def test_budget_marks_the_result_as_a_lower_bound(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for i in range(20):
                (root / f"f{i}.bin").write_bytes(b"x" * 16)
            facts = mole.disk_facts(root, max_files=5)
        self.assertTrue(facts.truncated)
        self.assertEqual(facts.files, 5)


class ContentDigestTests(unittest.TestCase):
    def test_identical_trees_hash_the_same_and_edits_change_it(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td) / "a", Path(td) / "b"
            for root in (a, b):
                (root / "sub").mkdir(parents=True)
                (root / "sub" / "x.txt").write_text("hello", encoding="utf-8")
                (root / "y.txt").write_text("world", encoding="utf-8")
            first, partial = mole.content_digest(a)
            second, _ = mole.content_digest(b)
            self.assertFalse(partial)
            self.assertEqual(first, second)
            (b / "y.txt").write_text("WORLD", encoding="utf-8")
            self.assertNotEqual(mole.content_digest(b)[0], first)

    def test_a_missing_file_changes_the_tree_digest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "t"
            root.mkdir()
            (root / "a.txt").write_text("a", encoding="utf-8")
            (root / "b.txt").write_text("b", encoding="utf-8")
            before, _ = mole.content_digest(root)
            (root / "b.txt").unlink()
            self.assertNotEqual(mole.content_digest(root)[0], before)

    def test_verify_reports_the_mismatch_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "f.txt"
            f.write_text("one", encoding="utf-8")
            digest, _ = mole.content_digest(f)
            self.assertIsNone(mole.verify_digest(f, digest, mole.DIGEST_ALGO))
            f.write_text("two", encoding="utf-8")
            self.assertIn("no longer matches",
                          mole.verify_digest(f, digest, mole.DIGEST_ALGO))

    def test_large_content_is_hashed_partially_and_labelled_as_such(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "big.bin"
            f.write_bytes(b"q" * 4096)
            _digest, partial = mole.content_digest(f, budget=16)
            self.assertTrue(partial)


class QuarantineIntegrityTests(unittest.TestCase):
    def _isolated(self, td):
        """Point every wmole state path at a scratch directory."""
        root = Path(td) / "state"
        (root / "quarantine").mkdir(parents=True)
        return mock.patch.multiple(
            mole,
            WMOLE_DIR=root,
            QUARANTINE_DIR=root / "quarantine",
            QUARANTINE_INDEX=root / "quarantine" / "index.json",
            LOG_DIR=root / "logs",
            OP_LOG_FILE=root / "logs" / "operations.log",
            SECURITY_LOG_FILE=root / "logs" / "security.jsonl",
            JOURNAL=mole.OperationJournal(root / "journal.db"),
        )

    def test_restore_is_refused_when_quarantined_content_was_altered(self):
        with tempfile.TemporaryDirectory() as td, self._isolated(td), \
             mock.patch.object(mole, "load_config",
                               return_value={"quarantine_enabled": True,
                                             "quarantine_verify": True,
                                             "quarantine_retention_days": 30,
                                             "protected_defaults": []}), \
             mock.patch.object(mole, "git_delete_risk", return_value=""), \
             mock.patch.object(mole, "is_protected_path", return_value=False):
            victim = Path(td) / "work" / "cache"
            victim.mkdir(parents=True)
            (victim / "a.txt").write_text("original", encoding="utf-8")

            self.assertIsNone(mole.quarantine_path(victim, size=8))
            entry = mole._quarantine_entries()[0]
            self.assertTrue(entry["digest"])
            self.assertEqual(entry["digest_algo"], mole.DIGEST_ALGO)

            # Tamper with the quarantined copy, as a crash or a stray tool would.
            Path(entry["stored"], "a.txt").write_text("tampered", encoding="utf-8")
            error = mole.restore_quarantine(entry["id"])
            self.assertIsNotNone(error)
            self.assertIn("restore refused", error)
            self.assertFalse(victim.exists())

    def test_untouched_content_restores_cleanly(self):
        with tempfile.TemporaryDirectory() as td, self._isolated(td), \
             mock.patch.object(mole, "load_config",
                               return_value={"quarantine_enabled": True,
                                             "quarantine_verify": True,
                                             "quarantine_retention_days": 30,
                                             "protected_defaults": []}), \
             mock.patch.object(mole, "git_delete_risk", return_value=""), \
             mock.patch.object(mole, "is_protected_path", return_value=False):
            victim = Path(td) / "work" / "cache"
            victim.mkdir(parents=True)
            (victim / "a.txt").write_text("original", encoding="utf-8")

            self.assertIsNone(mole.quarantine_path(victim, size=8))
            entry_id = mole._quarantine_entries()[0]["id"]
            self.assertIsNone(mole.restore_quarantine(entry_id))
            self.assertEqual((victim / "a.txt").read_text(encoding="utf-8"), "original")
            self.assertEqual(mole._quarantine_entries(), [])


class JournalTests(unittest.TestCase):
    def _journal(self, td):
        return mole.OperationJournal(Path(td) / "journal.db")

    def test_intent_is_durable_before_the_operation_completes(self):
        with tempfile.TemporaryDirectory() as td:
            journal = self._journal(td)
            entry_id = journal.begin("quarantine", Path(r"C:\x\y"), size=10,
                                     stored=r"C:\q\y")
            self.assertIsNotNone(entry_id)
            # A separate connection sees it: it is committed, not buffered.
            self.assertEqual([row["id"] for row in
                              mole.OperationJournal(Path(td) / "journal.db").open_entries()],
                             [entry_id])

    def test_committed_operations_leave_nothing_open(self):
        with tempfile.TemporaryDirectory() as td:
            journal = self._journal(td)
            entry_id = journal.begin("delete-permanent", Path(r"C:\x\y"))
            journal.mark(entry_id, "committed")
            self.assertEqual(journal.open_entries(), [])

    def test_recovery_voids_an_intent_whose_move_never_started(self):
        with tempfile.TemporaryDirectory() as td:
            original = Path(td) / "still-here"
            original.mkdir()
            journal = self._journal(td)
            journal.begin("quarantine", original, stored=str(Path(td) / "q" / "gone"))
            with mock.patch.object(mole, "_quarantine_entries", return_value=[]), \
                 mock.patch.object(mole, "_save_quarantine_entries"), \
                 mock.patch.object(mole, "log_security_event"):
                outcomes = journal.recover()
            self.assertEqual([o["verdict"] for o in outcomes], ["void"])
            self.assertEqual(journal.open_entries(), [])

    def test_recovery_reattaches_content_that_moved_but_was_never_indexed(self):
        with tempfile.TemporaryDirectory() as td:
            stored = Path(td) / "quarantine" / "1234-abcd" / "cache"
            stored.mkdir(parents=True)
            original = Path(td) / "work" / "cache"
            journal = self._journal(td)
            journal.begin("quarantine", original, size=99, stored=str(stored))
            saved = []
            with mock.patch.object(mole, "_quarantine_entries", return_value=[]), \
                 mock.patch.object(mole, "_save_quarantine_entries", saved.append), \
                 mock.patch.object(mole, "log_security_event"):
                outcomes = journal.recover()
            self.assertEqual([o["verdict"] for o in outcomes], ["reattached"])
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0][0]["id"], "1234-abcd")
            self.assertEqual(saved[0][0]["original"], str(original))
            self.assertTrue(saved[0][0]["recovered"])

    def test_content_missing_from_both_places_is_flagged_not_guessed(self):
        with tempfile.TemporaryDirectory() as td:
            journal = self._journal(td)
            journal.begin("quarantine", Path(td) / "gone",
                          stored=str(Path(td) / "q" / "gone"))
            with mock.patch.object(mole, "_quarantine_entries", return_value=[]), \
                 mock.patch.object(mole, "_save_quarantine_entries"), \
                 mock.patch.object(mole, "log_security_event"):
                outcomes = journal.recover()
            self.assertEqual([o["verdict"] for o in outcomes], ["unaccounted"])


@unittest.skipUnless(os.name == "nt", "handle deletion is a Windows API")
class HandleDeleteTests(unittest.TestCase):
    def test_handle_resolves_the_real_path_of_the_target(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "f.txt"
            f.write_text("x", encoding="utf-8")
            handle, err = mole.open_delete_handle(f)
            self.assertIsNone(err)
            with handle:
                self.assertEqual(handle.resolved.name, "f.txt")
                self.assertTrue(handle.resolved.is_absolute())
                self.assertFalse(str(handle.resolved).startswith("\\\\?\\"))

    def test_guard_sees_the_resolved_path_and_can_abort_the_delete(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "keep.txt"
            f.write_text("x", encoding="utf-8")
            seen = []

            def guard(resolved):
                seen.append(resolved)
                return "blocked by test"

            with mock.patch.object(mole, "log_security_event"):
                error = mole.handle_delete(f, guard=guard)
            self.assertEqual(error, "blocked by test")
            self.assertTrue(f.exists())
            self.assertEqual(seen[0].name, "keep.txt")

    def test_file_is_deleted_through_the_handle(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "f.txt"
            f.write_text("x", encoding="utf-8")
            self.assertIsNone(mole.handle_delete(f))
            self.assertFalse(f.exists())

    def test_directory_tree_is_removed_child_by_child(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "tree"
            (root / "a" / "b").mkdir(parents=True)
            (root / "a" / "b" / "deep.txt").write_text("x", encoding="utf-8")
            (root / "top.txt").write_text("y", encoding="utf-8")
            self.assertIsNone(mole.handle_delete(root))
            self.assertFalse(root.exists())

    def test_read_only_files_do_not_stall_the_delete(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "ro"
            root.mkdir()
            locked = root / "locked.txt"
            locked.write_text("x", encoding="utf-8")
            os.chmod(locked, 0o444)
            self.assertIsNone(mole.handle_delete(root))
            self.assertFalse(root.exists())

    def test_permanent_delete_reports_a_guard_refusal_instead_of_falling_back(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "protected.txt"
            f.write_text("x", encoding="utf-8")
            with mock.patch.object(mole, "is_protected_path", return_value=True), \
                 mock.patch.object(mole, "log_security_event"):
                error = mole.permanent_delete(f)
            self.assertEqual(error, "protected path")
            self.assertTrue(f.exists())


class BenchmarkTests(unittest.TestCase):
    def test_benchmark_reports_both_throughput_figures(self):
        report = mole.run_benchmark(dirs=4, files_per_dir=3, file_bytes=64)
        self.assertEqual(report["files"], 12)
        self.assertEqual(report["scan"]["items"], 12)
        self.assertGreater(report["scan"]["items_per_second"], 0)
        self.assertEqual(report["delete"]["items"], 12)
        self.assertEqual(report["delete"]["errors"], [])
        self.assertIn("cpu_count", report["machine"])


if __name__ == "__main__":
    unittest.main()
