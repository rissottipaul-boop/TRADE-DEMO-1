"""Регресс-чек пустых python-файлов в src/ и tests/.

Сканирует src/**/*.py и tests/**/test_*.py:
если файл 0 байт или состоит только из пробельных символов — возвращает ошибку
с точным указанием пути к файлу. Предотвращает ложно-зеленый тестовый сьют,
когда новые или пересозданные модули остаются 0 байт.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence


def get_project_root() -> Path:
    """Возвращает корень проекта (родительский каталог src/)."""
    return Path(__file__).resolve().parent.parent


def is_file_empty(path: Path) -> bool:
    """Проверяет, пуст ли файл (0 байт или только пробельные символы)."""
    try:
        if path.stat().st_size == 0:
            return True
        content = path.read_text(encoding="utf-8-sig", errors="replace")
        return len(content.strip()) == 0
    except OSError:
        return True


def scan_empty_files(
    root: Path | str | None = None,
) -> tuple[list[Path], list[Path]]:
    """Сканирует каталог root на наличие пустых .py файлов.

    Возвращает кортеж (scanned_files, empty_files).
    Проверяемые маски:
      - src/**/*.py (все модули кодовой базы)
      - tests/**/test_*.py (все тестовые файлы)
    """
    root_path = Path(root) if root is not None else get_project_root()

    src_dir = root_path / "src"
    tests_dir = root_path / "tests"

    scanned_files: list[Path] = []
    empty_files: list[Path] = []

    # Сканирование src/
    if src_dir.exists() and src_dir.is_dir():
        for path in sorted(src_dir.rglob("*.py")):
            if path.is_file():
                scanned_files.append(path)
                if is_file_empty(path):
                    empty_files.append(path)

    # Сканирование tests/ (только test_*.py)
    if tests_dir.exists() and tests_dir.is_dir():
        for path in sorted(tests_dir.rglob("test_*.py")):
            if path.is_file():
                scanned_files.append(path)
                if is_file_empty(path):
                    empty_files.append(path)

    return scanned_files, empty_files


def run_check(root: Path | str | None = None) -> int:
    """Выполняет проверку и выводит отчет.

    Возвращает 0, если все файлы содержат код;
    возвращает 1, если найден хотя бы один пустой файл.
    """
    root_path = Path(root) if root is not None else get_project_root()
    scanned, empty = scan_empty_files(root_path)

    if not empty:
        print(f"OK: Checked {len(scanned)} python files in src/ and tests/. No empty files found.")
        return 0

    print(
        f"ERROR: Found {len(empty)} empty python file(s) (0 bytes or only whitespace):",
        file=sys.stderr,
    )
    for p in empty:
        try:
            rel = p.relative_to(root_path)
        except ValueError:
            rel = p
        print(f"  - {rel} (size={p.stat().st_size} bytes)", file=sys.stderr)

    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Проверка на отсутствие пустых .py файлов в src/ и tests/ (EMPTY-FILE-GUARD)"
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=None,
        help="Корень проекта (по умолчанию директория проекта)",
    )
    args = parser.parse_args(argv)
    return run_check(args.root)


if __name__ == "__main__":
    sys.exit(main())
