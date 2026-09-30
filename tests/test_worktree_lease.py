"""Проверки владения изолированными checkout без запуска моделей."""

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.worktree_lease import LeaseError, heartbeat, inspect_leases, prepare_worktree


class WorktreeLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Panel Test")
        self.git("config", "user.email", "panel@example.invalid")
        (self.root / ".gitignore").write_text("/data/\n", encoding="utf-8")
        (self.root / "README.md").write_text("fixture\n", encoding="utf-8")
        self.git("add", ".gitignore", "README.md")
        self.git("commit", "-qm", "fixture")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True,
                       capture_output=True, timeout=10)

    def test_prepare_reserves_task_and_registers_isolated_worktree(self):
        lease = prepare_worktree(self.root, "T1", "run_T1_abc")
        self.assertEqual(lease["state"], "active")
        self.assertEqual(lease["base_commit"], subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.root, check=True,
            capture_output=True, text=True).stdout.strip())
        self.assertTrue(Path(lease["worktree"]).is_dir())
        self.assertNotIn(self.root, Path(lease["worktree"]).parents)
        observed = inspect_leases(self.root)
        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0]["registered"])
        self.assertFalse(observed[0]["owner_verified"])
        self.assertFalse(observed[0]["stale"])
        heartbeat(self.root, "T1", "run_T1_abc")
        with self.assertRaisesRegex(LeaseError, "уже имеет lease"):
            prepare_worktree(self.root, "T1", "run_T1_second")
        with self.assertRaisesRegex(LeaseError, "другому запуску"):
            heartbeat(self.root, "T1", "run_T1_wrong")

    def test_stale_heartbeat_requires_review_without_reclaim(self):
        prepare_worktree(self.root, "T3", "run_T3_old")
        path = self.root / "data" / "worktree-leases" / "T3.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["heartbeat_at"] = "2000-01-01T00:00:00+00:00"
        path.write_text(json.dumps(record), encoding="utf-8")
        observed = inspect_leases(self.root)
        self.assertTrue(observed[0]["stale"])
        self.assertTrue(observed[0]["registered"])
        with self.assertRaisesRegex(LeaseError, "уже имеет lease"):
            prepare_worktree(self.root, "T3", "run_T3_new")

    def test_dirty_checkout_and_nested_destination_are_rejected(self):
        (self.root / "dirty.txt").write_text("uncommitted", encoding="utf-8")
        with self.assertRaisesRegex(LeaseError, "изменена"):
            prepare_worktree(self.root, "T2", "run_T2_dirty")
        (self.root / "dirty.txt").unlink()
        with self.assertRaisesRegex(LeaseError, "вне основного checkout"):
            prepare_worktree(self.root, "T2", "run_T2_nested", parent=self.root / "worktrees")
        self.assertEqual(inspect_leases(self.root), [])


if __name__ == "__main__":
    unittest.main()
