"""Сверка файлов периметра guard с HEAD (PERIMETER-DRIFT-CHECK, PERIMETER-COMMIT-WATCH).

Журнал guard пишет только отказы: разрешённая запись в периметр следа не оставляет
(случай guard.py 03.10 11:35:53, MON-ENGINE). Эта проверка сравнивает каждый файл
периметра с его версией в HEAD и показывает статус и mtime.

    python -m src.ops perimeter [--since <sha>] [--json] [--root <каталог>]
    python -m src.perimeter     [--since <sha>] [--json] [--root <каталог>]

Сверка с HEAD не видит правку, которая уже вошла в коммит: правка guard.py 11:35:53
попала в b30ca77, и файл снова clean. Поэтому `--since <sha>` дополнительно перечисляет
коммиты диапазона <sha>..HEAD, затронувшие периметр: хеш, автор, время, файлы со статусом
(A, M, D; у слияния по букве на каждого родителя). Базой служит HEAD из прошлой проверки
(Ops Sentinel пишет его в заметку MON-ENGINE); текущий HEAD есть в первой строке вывода
и в поле `head` JSON. Если база не предок HEAD (история переписана или другая ветка),
дополнительно сверяется дерево базы с HEAD: так видна и откатанная правка периметра.
Коммиты читаются без pathspec и фильтруются тем же правилом, что и файлы на диске
(регистр не важен, каталоги — по префиксу); переименования разложены на D + A,
у слияния — файлы, отличные от всех родителей (`-c`).

Состав периметра берётся из guard импортом (ops/hooks/guard.py): GUARDRAIL_FILES,
GUARDRAIL_DIRS и RISK_FILE; здесь списки не дублируются. Настройки клиентов
(.claude/settings.json, .gemini/settings.json, .muse/*, .codex/hooks.json) входят в них.

Статусы:
    clean      совпадает с HEAD;
    modified   содержимое отличается от HEAD;
    untracked  файла нет в HEAD, но он есть на диске (в том числе под .gitignore);
    deleted    в HEAD файл есть, на диске нет;
    absent     файла или каталога периметра нет ни в HEAD, ни на диске; это не расхождение.
Кэш байткода (__pycache__) не сверяется: выводится только число таких файлов.

Коды выхода: 0 — расхождений нет; 1 — есть modified, untracked или deleted, а с --since
ещё коммиты в периметр после базы (или отличие дерева базы, если она не предок HEAD);
2 — сверка невозможна (git недоступен, не репозиторий, нет HEAD, guard не импортируется,
коммит --since не найден).

Только чтение: `git log`, `rev-parse`, `merge-base --is-ancestor`, `diff` двух коммитов.
Содержимое сравнивается по хешам blob: `git ls-tree -r HEAD` против
`git hash-object --stdin-paths` (без -w, с теми же фильтрами autocrlf и .gitattributes,
что при коммите). Кэш stat в индексе не используется, поэтому правку того же размера
с возвращённым mtime видно. `git status` не нужен: он может переписать .git/index,
обновляя этот кэш.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]

CLEAN, MODIFIED, UNTRACKED, DELETED, ABSENT = "clean", "modified", "untracked", "deleted", "absent"
STATUSES = (CLEAN, MODIFIED, UNTRACKED, DELETED, ABSENT)
DRIFT = frozenset({MODIFIED, UNTRACKED, DELETED})
BYTECODE_DIR = "__pycache__"


class PerimeterError(RuntimeError):
    """Сверка невозможна: git, репозиторий, HEAD или сам guard недоступны."""


@dataclass
class Entry:
    path: str
    status: str
    mtime: Optional[str] = None  # локальное время ISO; None — файла на диске нет
    ignored: bool = False        # untracked-файл попадает под .gitignore
    kind: str = "file"           # file | dir (dir — только absent-каталог периметра)


@dataclass
class FileChange:
    path: str
    status: str  # A, M, D, T; у слияния (-c) — буква на каждого родителя, например MM


@dataclass
class Commit:
    sha: str
    author: str
    email: str
    time: str        # время автора, ISO
    committed: str   # время коммита, ISO
    subject: str
    files: list[FileChange] = field(default_factory=list)


@dataclass
class Since:
    ref: str                 # как передано в --since
    base: str                # полный sha базы
    ancestor: bool           # база — предок HEAD
    commits: list[Commit] = field(default_factory=list)          # <base>..HEAD, от старых к новым
    tree_diff: list[FileChange] = field(default_factory=list)    # только если база не предок HEAD

    @property
    def flagged(self) -> bool:
        return bool(self.commits or self.tree_diff)


@dataclass
class Report:
    root: str
    head: str
    entries: list[Entry] = field(default_factory=list)
    skipped_bytecode: int = 0
    since: Optional[Since] = None

    @property
    def drift(self) -> list[Entry]:
        return [e for e in self.entries if e.status in DRIFT]

    @property
    def exit_code(self) -> int:
        return 1 if self.drift or (self.since and self.since.flagged) else 0


def _guard():
    """Модуль guard — источник правды о периметре. Не импортируется — сверять не с чем."""
    try:
        from ops.hooks import guard
    except Exception as exc:  # SyntaxError и прочее тоже: guard изменён или удалён
        raise PerimeterError(f"ops/hooks/guard.py не импортируется ({type(exc).__name__}: {exc}); "
                             "состав периметра неизвестен") from exc
    return guard


def perimeter_spec() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(файлы, каталоги) периметра из guard: GUARDRAIL_FILES + RISK_FILE и GUARDRAIL_DIRS."""
    guard = _guard()
    files = tuple(sorted(set(guard.GUARDRAIL_FILES) | {guard.RISK_FILE}))
    return files, tuple(guard.GUARDRAIL_DIRS)


def _in_perimeter(rel: str, files: tuple[str, ...], dirs: tuple[str, ...]) -> bool:
    # Как guard._is_guardrail: путь в нижнем регистре, файлы — точно, каталоги — по префиксу
    low = rel.replace("\\", "/").lower()
    return low in files or low.startswith(dirs)


def is_perimeter(rel: str) -> bool:
    """Путь относительно корня проекта входит в периметр guard (вместе с RISK_FILE)."""
    return _in_perimeter(rel, *perimeter_spec())


def _key(rel: str) -> str:
    # Сопоставление путей HEAD и диска: на Windows регистр не различается
    return os.path.normcase(rel)


def _git(root: Path, *args: str, stdin: Optional[str] = None,
         ok: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], input=stdin, capture_output=True,
                              text=True, encoding="utf-8", errors="surrogateescape", check=False)
    except OSError as exc:
        raise PerimeterError(f"git не запускается: {exc}") from exc
    if proc.returncode not in ok:
        detail = proc.stderr.strip() or f"код {proc.returncode}"
        raise PerimeterError(f"git {args[0]}: {detail}")
    return proc


def _head_blobs(root: Path, files, dirs) -> dict[str, tuple[str, str]]:
    """normcase(путь) → (путь в HEAD, sha blob) для файлов периметра в HEAD."""
    out = _git(root, "ls-tree", "-r", "-z", "--full-tree", "HEAD").stdout
    blobs: dict[str, tuple[str, str]] = {}
    for record in out.split("\0"):
        meta, sep, path = record.partition("\t")
        if not sep:
            continue
        _mode, otype, sha = meta.split()
        if otype == "blob" and _in_perimeter(path, files, dirs):
            blobs[_key(path)] = (path, sha)
    return blobs


def _disk_files(root: Path, files, dirs) -> tuple[dict[str, str], int]:
    """normcase(путь) → путь для файлов периметра на диске; второе — число файлов кэша байткода."""
    found: dict[str, str] = {}
    skipped = 0
    for rel in files:
        if (root / rel).is_file():
            found[_key(rel)] = rel
    for directory in dirs:
        base = root / directory
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            if BYTECODE_DIR in dirnames:
                dirnames.remove(BYTECODE_DIR)
                skipped += sum(len(names) for _, _, names in os.walk(Path(dirpath) / BYTECODE_DIR))
            for name in filenames:
                rel = (Path(dirpath) / name).relative_to(root).as_posix()
                found[_key(rel)] = rel
    return found, skipped


def _worktree_blobs(root: Path, paths: list[str]) -> list[str]:
    """sha blob для файлов на диске так, как их записал бы коммит (без записи в базу git)."""
    if not paths:
        return []
    out = _git(root, "hash-object", "--stdin-paths", stdin="\n".join(paths) + "\n").stdout.split()
    if len(out) != len(paths):
        raise PerimeterError(f"git hash-object вернул {len(out)} хешей на {len(paths)} файлов")
    return out


def _ignored(root: Path, paths: list[str]) -> set[str]:
    if not paths:
        return set()
    proc = _git(root, "check-ignore", "--stdin", "-z", stdin="\0".join(paths) + "\0", ok=(0, 1))
    return {_key(p) for p in proc.stdout.split("\0") if p}


def _mtime(path: Path) -> Optional[str]:
    try:
        ts = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _resolve_commit(top: Path, ref: str) -> str:
    """Полный sha коммита из --since; не найден — сверка невозможна (код 2)."""
    if not ref or ref.startswith("-"):  # не даём значению стать опцией git
        raise PerimeterError(f"--since {ref!r}: нужен sha коммита")
    proc = _git(top, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", ok=(0, 1))
    sha = proc.stdout.strip()
    if proc.returncode or not sha:
        raise PerimeterError(f"--since {ref}: коммит не найден в {top}")
    return sha


def _perimeter_changes(raw: str, files, dirs) -> list[FileChange]:
    """Файлы периметра из вывода `--name-status -z` (пары «статус, путь», без переименований)."""
    out: list[FileChange] = []
    tokens = iter(raw.split("\0"))
    for status in tokens:
        status = status.strip()  # перед первым статусом коммита git ставит перевод строки
        if not status:
            continue
        path = next(tokens, "")
        if _in_perimeter(path, files, dirs):
            out.append(FileChange(path, status))
    return out


def _commits_since(top: Path, base: str, head: str, files, dirs) -> list[Commit]:
    """Коммиты <base>..<head>, затронувшие периметр, от старых к новым."""
    raw = _git(top, "log", "-z", "--reverse", "--no-color", "--no-renames", "--name-status", "-c",
               "--format=%x1e%H", f"{base}..{head}").stdout
    touched: dict[str, list[FileChange]] = {}
    for chunk in raw.split("\x1e")[1:]:
        sha, _, rest = chunk.partition("\0")
        found = _perimeter_changes(rest, files, dirs)
        if found:
            touched[sha.strip()] = found
    if not touched:
        return []
    meta = _git(top, "log", "--no-walk=unsorted", "--stdin", "-z", "--no-color",
                "--format=%H%x1f%an%x1f%ae%x1f%aI%x1f%cI%x1f%s",
                stdin="\n".join(touched) + "\n").stdout
    commits: dict[str, Commit] = {}
    for record in meta.split("\0"):
        parts = record.strip("\n").split("\x1f", 5)
        if len(parts) == 6:
            sha, author, email, time, committed, subject = parts
            commits[sha] = Commit(sha, author, email, time, committed, subject, touched.get(sha, []))
    missing = [sha for sha in touched if sha not in commits]
    if missing:
        raise PerimeterError(f"git log не вернул данные коммитов: {', '.join(missing)}")
    return [commits[sha] for sha in touched]


def _since(top: Path, ref: str, head: str, files, dirs) -> Since:
    base = _resolve_commit(top, ref)
    ancestor = _git(top, "merge-base", "--is-ancestor", base, head, ok=(0, 1)).returncode == 0
    since = Since(ref=ref, base=base, ancestor=ancestor,
                  commits=_commits_since(top, base, head, files, dirs))
    if not ancestor:
        # Правка периметра, которая есть в базе, но не в HEAD, в <base>..HEAD не попадёт
        raw = _git(top, "diff", "-z", "--no-color", "--no-ext-diff", "--no-renames", "--name-status",
                   base, head).stdout
        since.tree_diff = _perimeter_changes(raw, files, dirs)
    return since


def check(root: Path = ROOT, since: Optional[str] = None) -> Report:
    """Сверка периметра с HEAD репозитория, в котором лежит root; since — коммиты после sha.

    Только чтение."""
    files, dirs = perimeter_spec()
    top = Path(_git(Path(root), "rev-parse", "--show-toplevel").stdout.strip())
    proc = _git(top, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", ok=(0, 1))
    head = proc.stdout.strip()
    if proc.returncode or not head:
        raise PerimeterError(f"{top}: нет коммита HEAD, сверять не с чем")
    since_report = _since(top, since, head, files, dirs) if since is not None else None

    head_blobs = _head_blobs(top, files, dirs)
    disk, skipped = _disk_files(top, files, dirs)
    report = Report(root=str(top), head=head, skipped_bytecode=skipped, since=since_report)

    present = [(key, path, sha) for key, (path, sha) in head_blobs.items() if (top / path).is_file()]
    hashes = dict(zip((key for key, _, _ in present), _worktree_blobs(top, [p for _, p, _ in present])))
    for key, (path, sha) in head_blobs.items():
        if key not in hashes:
            report.entries.append(Entry(path, DELETED))
        else:
            status = CLEAN if hashes[key] == sha else MODIFIED
            report.entries.append(Entry(path, status, _mtime(top / path)))

    new = [path for key, path in disk.items() if key not in head_blobs]
    ignored = _ignored(top, new)
    for path in new:
        report.entries.append(Entry(path, UNTRACKED, _mtime(top / path), ignored=_key(path) in ignored))

    seen = [e.path.lower() for e in report.entries]
    for rel in files:
        if rel not in seen:
            report.entries.append(Entry(rel, ABSENT))
    for directory in dirs:
        if not any(p.startswith(directory) for p in seen):
            report.entries.append(Entry(directory, ABSENT, kind="dir"))
    report.entries.sort(key=lambda e: e.path.lower())
    return report


def _render_since(s: Since, head: str) -> list[str]:
    span = f"{s.base[:12]}..{head[:12]}"
    lines = []
    if not s.ancestor:
        lines.append(f"ВНИМАНИЕ: {s.base[:12]} не предок HEAD (история переписана или другая ветка): "
                     "кроме коммитов сверено дерево базы с HEAD")
    if s.commits:
        lines.append(f"КОММИТЫ в периметр после {s.ref} ({span}): {len(s.commits)}")
        for c in s.commits:
            lines.append(f"  {c.sha[:12]}  {c.time}  {c.author}  {c.subject}")
            lines.extend(f"      {f.status:<2}  {f.path}" for f in c.files)
    else:
        lines.append(f"Коммитов в периметр после {s.ref} ({span}) нет")
    if s.tree_diff:
        lines.append(f"ОТЛИЧИЕ дерева {s.base[:12]} от HEAD в файлах периметра: {len(s.tree_diff)}")
        lines.extend(f"      {f.status:<2}  {f.path}" for f in s.tree_diff)
    return lines


def render_text(report: Report) -> str:
    lines = [f"Периметр guard против HEAD {report.head} ({report.root})",
             f"{'статус':<9}  {'mtime':<25}  файл"]
    for e in report.entries:
        note = "  (под .gitignore)" if e.ignored else "  (каталог пуст)" if e.kind == "dir" else ""
        lines.append(f"{e.status:<9}  {e.mtime or '-':<25}  {e.path}{note}")
    if report.skipped_bytecode:
        lines.append(f"Кэш байткода {BYTECODE_DIR} не сверяется: файлов {report.skipped_bytecode}")
    s = report.since
    if s:
        lines.extend(_render_since(s, report.head))
    counts = Counter(e.status for e in report.entries)
    summary = ", ".join(f"{st} {counts.get(st, 0)}" for st in STATUSES)
    drift = report.drift
    verdict = ("Расхождений с HEAD нет" if not drift else
               f"РАСХОЖДЕНИЕ с HEAD в {len(drift)} файл(ах): " + ", ".join(e.path for e in drift))
    if s:
        verdict += f". Коммитов в периметр после {s.ref}: {len(s.commits)}"
        if s.tree_diff:
            verdict += f", отличие дерева базы: {len(s.tree_diff)} файл(ов)"
    lines.append(f"Итог: {summary}. {verdict}. Код {report.exit_code}.")
    return "\n".join(lines)


def render_json(report: Report) -> str:
    data = {
        "root": report.root,
        "head": report.head,
        "files": [asdict(e) for e in report.entries],
        "skipped_bytecode": report.skipped_bytecode,
        "drift": [e.path for e in report.drift],
    }
    if report.since:  # без --since JSON прежний
        data["since"] = asdict(report.since)
    data["exit_code"] = report.exit_code
    return json.dumps(data, ensure_ascii=False, indent=2)


def run(root: Path = ROOT, as_json: bool = False, since: Optional[str] = None) -> int:
    """Точка входа CLI (и `python -m src.ops perimeter`): печатает отчёт, возвращает код выхода."""
    try:
        report = check(root, since)
    except PerimeterError as exc:
        if as_json:
            print(json.dumps({"error": str(exc), "exit_code": 2}, ensure_ascii=False))
        else:
            print(f"Сверка периметра невозможна: {exc}", file=sys.stderr)
        return 2
    print(render_json(report) if as_json else render_text(report))
    return report.exit_code


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="отчёт в JSON")
    parser.add_argument("--root", type=Path, default=ROOT,
                        help="каталог git-репозитория (по умолчанию — этот проект)")
    parser.add_argument("--since", metavar="SHA", default=None,
                        help="ещё и коммиты <SHA>..HEAD, затронувшие периметр (SHA — HEAD прошлой проверки)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.perimeter", description=__doc__.splitlines()[0])
    add_arguments(parser)
    args = parser.parse_args(argv)
    return run(args.root, args.json, args.since)


if __name__ == "__main__":
    sys.exit(main())
