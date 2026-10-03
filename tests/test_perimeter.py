"""PERIMETER-DRIFT-CHECK и PERIMETER-COMMIT-WATCH: сверка периметра guard с HEAD
и коммиты в периметр после базы (--since) на временном git-репозитории.

Боевой репозиторий не трогается: фикстура — отдельный `git init` в каталоге tmp.
Состав периметра берётся из настоящего guard, как в рабочей команде.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ops.hooks import guard
from src import ops, perimeter

FIXTURE = {
    "src/risk.py": b"# fixture risk core\n",
    "ops/hooks/guard.py": b"# fixture guard\n",
    ".claude/settings.json": b'{"a": 1}\n',
    ".codex/hooks.json": b"{}\n",
    "README.md": b"not perimeter\n",
    ".gitignore": b"*.local\n__pycache__/\n",
}


class RepoFixture(unittest.TestCase):
    """Временный репозиторий с коммитом FIXTURE; своих тестов нет."""

    def setUp(self):
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Perimeter Test")
        self.git("config", "user.email", "perimeter@example.invalid")
        for rel, data in FIXTURE.items():
            self.write(rel, data)
        self.git("add", *FIXTURE)
        self.git("commit", "-qm", "fixture")

    def git(self, *args) -> str:
        return subprocess.run(["git", *args], cwd=self.root, check=True,
                              capture_output=True, text=True, timeout=20).stdout

    def write(self, rel: str, data: bytes) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def statuses(self, report) -> dict:
        return {e.path: e.status for e in report.entries}

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").strip()

    def commit(self, message: str, files: dict) -> str:
        for rel, data in files.items():
            self.write(rel, data)
        self.git("add", "--", *files)
        self.git("commit", "-qm", message)
        return self.head()

    def ops_run(self, *args) -> tuple[int, str, str]:
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = ops.main(["perimeter", *args, "--root", str(self.root)])
        return code, out.getvalue(), err.getvalue()


class PerimeterRepoTests(RepoFixture):
    def test_unchanged_files_are_clean(self):
        report = perimeter.check(self.root)
        st = self.statuses(report)
        for rel in ("src/risk.py", "ops/hooks/guard.py", ".claude/settings.json", ".codex/hooks.json"):
            self.assertEqual(st[rel], perimeter.CLEAN, rel)
        self.assertNotIn("README.md", st)
        self.assertNotIn(".gitignore", st)
        # Файлы и каталоги периметра, которых нет ни в HEAD, ни на диске, — не расхождение
        self.assertEqual(st["ops/live-policy.json"], perimeter.ABSENT)
        self.assertEqual(st[".github/hooks/"], perimeter.ABSENT)
        self.assertTrue(all(e.mtime for e in report.entries if e.status == perimeter.CLEAN))
        self.assertEqual(report.drift, [])
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.head, self.git("rev-parse", "HEAD").strip())

    def test_modified_file_is_drift(self):
        self.write(".claude/settings.json", b'{"a": 2, "allow": "*"}\n')
        report = perimeter.check(self.root)
        st = self.statuses(report)
        self.assertEqual(st[".claude/settings.json"], perimeter.MODIFIED)
        self.assertEqual(st["src/risk.py"], perimeter.CLEAN)
        self.assertEqual([e.path for e in report.drift], [".claude/settings.json"])
        self.assertEqual(report.exit_code, 1)
        entry = next(e for e in report.entries if e.path == ".claude/settings.json")
        self.assertIsNotNone(entry.mtime)

    def test_same_size_edit_with_restored_mtime_is_detected(self):
        # Сверка по хешам, а не по кэшу stat индекса: подмена того же размера с прежним mtime видна
        path = self.root / "src/risk.py"
        before = path.stat()
        path.write_bytes(b"# fixture risk XXXX\n")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(path.stat().st_size, before.st_size)
        report = perimeter.check(self.root)
        self.assertEqual(self.statuses(report)["src/risk.py"], perimeter.MODIFIED)
        self.assertEqual(report.exit_code, 1)

    def test_new_files_in_perimeter_dirs_are_untracked(self):
        self.write(".codex/config.toml", b"approval = 'never'\n")
        self.write(".muse/extra.local", b"x\n")  # под .gitignore — всё равно расхождение
        report = perimeter.check(self.root)
        by_path = {e.path: e for e in report.entries}
        self.assertEqual(by_path[".codex/config.toml"].status, perimeter.UNTRACKED)
        self.assertFalse(by_path[".codex/config.toml"].ignored)
        self.assertEqual(by_path[".muse/extra.local"].status, perimeter.UNTRACKED)
        self.assertTrue(by_path[".muse/extra.local"].ignored)
        self.assertEqual(report.exit_code, 1)

    def test_deleted_file_is_drift(self):
        (self.root / "ops/hooks/guard.py").unlink()
        report = perimeter.check(self.root)
        entry = next(e for e in report.entries if e.path == "ops/hooks/guard.py")
        self.assertEqual(entry.status, perimeter.DELETED)
        self.assertIsNone(entry.mtime)
        self.assertEqual(report.exit_code, 1)

    def test_bytecode_cache_is_counted_not_compared(self):
        self.write("ops/hooks/__pycache__/guard.cpython-314.pyc", b"\x00bytecode")
        report = perimeter.check(self.root)
        self.assertFalse(any("__pycache__" in e.path for e in report.entries))
        self.assertEqual(report.skipped_bytecode, 1)
        self.assertEqual(report.exit_code, 0)

    def test_subdirectory_root_uses_repository_top(self):
        report = perimeter.check(self.root / "src")
        self.assertEqual(Path(report.root).resolve(), self.root.resolve())
        self.assertEqual(self.statuses(report)["src/risk.py"], perimeter.CLEAN)

    def test_check_does_not_write_git_state(self):
        self.write(".claude/settings.json", b'{"a": 3}\n')
        index = (self.root / ".git" / "index").read_bytes()
        objects = self.git("count-objects", "-v")
        perimeter.check(self.root)
        self.assertEqual((self.root / ".git" / "index").read_bytes(), index)
        self.assertEqual(self.git("count-objects", "-v"), objects)

    def test_ops_subcommand_exit_codes_and_json(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ops.main(["perimeter", "--root", str(self.root)]), 0)
        self.assertIn("Расхождений с HEAD нет", out.getvalue())
        self.write("src/risk.py", b"# changed\n")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ops.main(["perimeter", "--json", "--root", str(self.root)]), 1)
        data = json.loads(out.getvalue())
        self.assertEqual(data["drift"], ["src/risk.py"])
        self.assertEqual(data["exit_code"], 1)
        statuses = {f["path"]: f["status"] for f in data["files"]}
        self.assertEqual(statuses["src/risk.py"], "modified")
        self.assertEqual(statuses[".codex/hooks.json"], "clean")


class PerimeterSinceTests(RepoFixture):
    """--since: коммиты <sha>..HEAD, затронувшие периметр (PERIMETER-COMMIT-WATCH)."""

    def branch(self) -> str:
        return self.git("rev-parse", "--abbrev-ref", "HEAD").strip()

    @staticmethod
    def changes(files) -> list:
        return [(f.path, f.status) for f in files]

    def test_commit_outside_perimeter_exit_0(self):
        base = self.head()
        self.commit("docs", {"README.md": b"docs only\n", "src/other.py": b"x = 1\n"})
        report = perimeter.check(self.root, since=base)
        self.assertEqual(report.since.base, base)
        self.assertTrue(report.since.ancestor)
        self.assertEqual(report.since.commits, [])
        self.assertEqual(report.exit_code, 0)
        code, out, _ = self.ops_run("--since", base[:7])
        self.assertEqual(code, 0)
        self.assertIn(f"Коммитов в периметр после {base[:7]}", out)

    def test_commit_in_perimeter_exit_1_although_files_are_clean(self):
        # Правка уже в коммите: сверка с HEAD чиста, а --since её видит (случай guard.py → b30ca77)
        base = self.head()
        self.commit("docs", {"README.md": b"docs\n"})
        sha = self.commit("tune guard", {"ops/hooks/guard.py": b"# relaxed guard\n", "README.md": b"docs 2\n"})
        report = perimeter.check(self.root, since=base)
        self.assertEqual(report.drift, [])
        self.assertEqual(len(report.since.commits), 1)
        c = report.since.commits[0]
        self.assertEqual((c.sha, c.author, c.email, c.subject),
                         (sha, "Perimeter Test", "perimeter@example.invalid", "tune guard"))
        self.assertRegex(c.time, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
        self.assertRegex(c.committed, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
        self.assertEqual(self.changes(c.files), [("ops/hooks/guard.py", "M")])
        self.assertEqual(report.exit_code, 1)
        # Без --since поведение прежнее: файлы совпадают с HEAD — код 0
        self.assertEqual(perimeter.check(self.root).exit_code, 0)
        self.assertEqual(self.ops_run()[0], 0)

        code, out, _ = self.ops_run("--since", base)
        self.assertEqual(code, 1)
        self.assertIn(f"{sha[:12]}  {c.time}  Perimeter Test  tune guard", out)
        self.assertIn("ops/hooks/guard.py", out)
        code, out, _ = self.ops_run("--since", base, "--json")
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertEqual(data["head"], sha)
        self.assertEqual(data["since"]["base"], base)
        self.assertEqual([x["sha"] for x in data["since"]["commits"]], [sha])
        self.assertEqual(data["since"]["commits"][0]["files"], [{"path": "ops/hooks/guard.py", "status": "M"}])
        self.assertEqual(data["exit_code"], 1)

    def test_unknown_sha_exit_2(self):
        code, _, err = self.ops_run("--since", "0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(code, 2)
        self.assertIn("не найден", err)
        code, out, _ = self.ops_run("--since", "nosuchref", "--json")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["exit_code"], 2)
        with self.assertRaisesRegex(perimeter.PerimeterError, "нужен sha"):
            perimeter.check(self.root, since="--all")  # значение не становится опцией git

    def test_since_head_exit_0_and_head_in_output(self):
        head = self.head()
        code, out, _ = self.ops_run("--since", "HEAD")
        self.assertEqual(code, 0)
        self.assertIn(head, out.splitlines()[0])
        _, out, _ = self.ops_run()  # полный HEAD в первой строке и без --since
        self.assertIn(head, out.splitlines()[0])
        _, out, _ = self.ops_run("--json")
        data = json.loads(out)
        self.assertEqual(data["head"], head)
        self.assertNotIn("since", data)  # без --since JSON прежний

    def test_dir_files_case_and_rename(self):
        # Каталог периметра — по префиксу, регистр не важен, переименование наружу — D
        base = self.head()
        self.commit("codex config", {".codex/config.toml": b"approval = 'never'\n"})
        self.commit("github hooks", {".GitHub/Hooks/pre.json": b"{}\n"})
        self.git("mv", "src/risk.py", "src/risk_old.py")
        self.git("commit", "-qm", "move risk")
        report = perimeter.check(self.root, since=base)
        self.assertEqual([c.subject for c in report.since.commits], ["codex config", "github hooks", "move risk"])
        self.assertEqual([self.changes(c.files) for c in report.since.commits],
                         [[(".codex/config.toml", "A")], [(".GitHub/Hooks/pre.json", "A")], [("src/risk.py", "D")]])
        self.assertEqual(report.exit_code, 1)

    def test_merge_commit_with_own_perimeter_change(self):
        # Правка, внесённая самим слиянием, видна (-c); обычное слияние без своих правок не выводится
        base, main = self.head(), self.branch()
        self.git("checkout", "-qb", "side")
        self.commit("side docs", {"README.md": b"side\n"})
        self.git("checkout", "-q", main)
        self.commit("main docs", {"other.txt": b"main\n"})
        self.git("merge", "-q", "--no-ff", "--no-commit", "side")
        self.write("src/risk.py", b"# changed in merge\n")
        self.git("add", "src/risk.py")
        self.git("commit", "-qm", "merge side")
        merge = self.head()
        report = perimeter.check(self.root, since=base)
        self.assertEqual([c.sha for c in report.since.commits], [merge])
        self.assertEqual(self.changes(report.since.commits[0].files), [("src/risk.py", "MM")])
        self.assertEqual(report.exit_code, 1)

    def test_plain_merge_lists_branch_commit_only(self):
        base, main = self.head(), self.branch()
        self.git("checkout", "-qb", "side")
        side = self.commit("side risk", {"src/risk.py": b"# side\n"})
        self.git("checkout", "-q", main)
        self.commit("main docs", {"other.txt": b"main\n"})
        self.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
        report = perimeter.check(self.root, since=base)
        self.assertEqual([c.sha for c in report.since.commits], [side])
        self.assertEqual(report.exit_code, 1)

    def test_base_not_ancestor_compares_trees(self):
        # База с правкой периметра, которой нет в HEAD (откат истории): коммитов нет, дерево отличается
        main = self.branch()
        self.git("checkout", "-qb", "side")
        base = self.commit("tighten risk", {"src/risk.py": b"# tightened\n"})
        self.git("checkout", "-q", main)
        self.commit("docs", {"README.md": b"docs\n"})
        report = perimeter.check(self.root, since=base)
        self.assertFalse(report.since.ancestor)
        self.assertEqual(report.since.commits, [])
        self.assertEqual(self.changes(report.since.tree_diff), [("src/risk.py", "M")])
        self.assertEqual(report.drift, [])
        self.assertEqual(report.exit_code, 1)
        code, out, _ = self.ops_run("--since", base)
        self.assertEqual(code, 1)
        self.assertIn("не предок HEAD", out)

    def test_base_not_ancestor_without_perimeter_diff_is_0(self):
        main = self.branch()
        self.git("checkout", "-qb", "side")
        base = self.commit("side docs", {"README.md": b"side\n"})
        self.git("checkout", "-q", main)
        self.commit("main docs", {"other.txt": b"main\n"})
        report = perimeter.check(self.root, since=base)
        self.assertFalse(report.since.ancestor)
        self.assertEqual((report.since.commits, report.since.tree_diff), ([], []))
        self.assertEqual(report.exit_code, 0)

    def test_since_does_not_write_git_state(self):
        base = self.head()
        self.commit("tune guard", {"ops/hooks/guard.py": b"# x\n"})
        state = ((self.root / ".git" / "index").read_bytes(), self.git("count-objects", "-v"),
                 self.git("for-each-ref"))
        perimeter.check(self.root, since=base)
        perimeter.check(self.root, since="HEAD")
        self.assertEqual(((self.root / ".git" / "index").read_bytes(), self.git("count-objects", "-v"),
                          self.git("for-each-ref")), state)


class PerimeterErrorTests(unittest.TestCase):
    def test_repository_without_head_is_error(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True, capture_output=True, timeout=20)
            with self.assertRaisesRegex(perimeter.PerimeterError, "нет коммита HEAD"):
                perimeter.check(Path(tmp))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(perimeter.run(Path(tmp)), 2)

    def test_not_a_repository_is_error(self):
        if shutil.which("git") is None:
            self.skipTest("git не установлен")
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(perimeter.run(Path(tmp) / "nowhere", as_json=True), 2)
            self.assertEqual(json.loads(out.getvalue())["exit_code"], 2)


class PerimeterSpecTests(unittest.TestCase):
    """Состав периметра — из guard, без ручного дубля списков."""

    def test_spec_comes_from_guard(self):
        files, dirs = perimeter.perimeter_spec()
        self.assertEqual(set(files), set(guard.GUARDRAIL_FILES) | {guard.RISK_FILE})
        self.assertEqual(dirs, tuple(guard.GUARDRAIL_DIRS))
        for client in (".claude/settings.json", ".gemini/settings.json", ".muse/settings.json"):
            self.assertIn(client, files)

    def test_membership_matches_guard(self):
        samples = (".claude/settings.json", ".CLAUDE/Settings.json", ".gemini/settings.json",
                   ".codex/hooks.json", ".codex/config.toml", ".muse/hooks.json",
                   "ops/hooks/guard.py", "ops/hooksx/a.py", ".github/hooks/x.json",
                   ".github/agents/ops-sentinel.agent.md", "ops/live-policy.json", "ops/autopilot.json",
                   "pump-pocket.json", "src/risk.py", "src/risk_selftest.py", "README.md")
        for rel in samples:
            low = rel.lower()
            expected = guard._is_guardrail(low) or low == guard.RISK_FILE
            self.assertEqual(perimeter.is_perimeter(rel), expected, rel)
        self.assertTrue(perimeter.is_perimeter(".codex/hooks.json"))


if __name__ == "__main__":
    unittest.main()
