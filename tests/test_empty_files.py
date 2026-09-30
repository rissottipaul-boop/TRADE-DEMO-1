"""Тесты регресс-чека пустых файлов (EMPTY-FILE-GUARD).

Проверяет:
1. В текущем репозитории нет пустых .py файлов в src/ и tests/test_*.py.
2. Файлы 0 байт или содержащие только пробелы приводят к падению с выводом имени файла.
3. Валидные файлы и не-тестовые вспомогательные файлы (например tests/__init__.py) обрабатываются корректно.
4. Коды возврата и сообщения CLI.
"""

from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.empty_file_guard import (
    get_project_root,
    is_file_empty,
    main,
    run_check,
    scan_empty_files,
)


class EmptyFileGuardTests(unittest.TestCase):
    def test_repo_has_no_empty_files(self) -> None:
        """Реальный репозиторий: ни один файл в src/*.py и tests/test_*.py не должен быть пуст."""
        root = get_project_root()
        scanned, empty = scan_empty_files(root)

        # Должно быть просканировано существенное количество файлов (src + tests)
        self.assertGreater(
            len(scanned),
            100,
            f"Ожидалось >100 файлов в src и tests, но найдено {len(scanned)}",
        )

        empty_rel_paths = [str(p.relative_to(root)) for p in empty]
        self.assertEqual(
            empty,
            [],
            f"Обнаружены пустые python-файлы (0 байт или только пробелы): {empty_rel_paths}",
        )

    def test_zero_byte_file_in_src_detected(self) -> None:
        """Файл размером 0 байт в src/ должен определяться как пустой."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            src_dir = tmppath / "src"
            src_dir.mkdir(parents=True)
            empty_file = src_dir / "broken_module.py"
            empty_file.write_bytes(b"")

            scanned, empty = scan_empty_files(tmppath)
            self.assertIn(empty_file, scanned)
            self.assertIn(empty_file, empty)
            self.assertEqual(len(empty), 1)

    def test_whitespace_only_file_in_src_detected(self) -> None:
        """Файл, содержащий только пробелы, табы и переводы строк, должен определяться как пустой."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            src_dir = tmppath / "src"
            src_dir.mkdir(parents=True)
            spaces_file = src_dir / "spaces_module.py"
            spaces_file.write_text("   \n\t  \r\n   ", encoding="utf-8")

            scanned, empty = scan_empty_files(tmppath)
            self.assertIn(spaces_file, scanned)
            self.assertIn(spaces_file, empty)
            self.assertEqual(len(empty), 1)

    def test_zero_byte_file_in_tests_detected(self) -> None:
        """Тестовый файл test_*.py размером 0 байт в tests/ должен определяться как пустой."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            tests_dir = tmppath / "tests"
            tests_dir.mkdir(parents=True)
            empty_test = tests_dir / "test_dummy_empty.py"
            empty_test.write_bytes(b"")

            scanned, empty = scan_empty_files(tmppath)
            self.assertIn(empty_test, scanned)
            self.assertIn(empty_test, empty)
            self.assertEqual(len(empty), 1)

    def test_valid_code_file_not_marked_empty(self) -> None:
        """Файл с реальным кодом не должен определяться как пустой."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            src_dir = tmppath / "src"
            src_dir.mkdir(parents=True)
            valid_file = src_dir / "valid_module.py"
            valid_file.write_text("def hello() -> str:\n    return 'ok'\n", encoding="utf-8")

            scanned, empty = scan_empty_files(tmppath)
            self.assertIn(valid_file, scanned)
            self.assertEqual(empty, [])

    def test_non_test_py_in_tests_ignored(self) -> None:
        """Файлы в tests/, не соответствующие маске test_*.py (например __init__.py), не должны блокировать сьют."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            tests_dir = tmppath / "tests"
            tests_dir.mkdir(parents=True)
            init_file = tests_dir / "__init__.py"
            init_file.write_bytes(b"")

            scanned, empty = scan_empty_files(tmppath)
            self.assertNotIn(init_file, scanned)
            self.assertEqual(empty, [])

    def test_run_check_exit_code_and_output(self) -> None:
        """run_check должен возвращать 0 при успехе и 1 с указанием имени файла при ошибке."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            src_dir = tmppath / "src"
            src_dir.mkdir(parents=True)

            # Чистая директория
            valid_file = src_dir / "good.py"
            valid_file.write_text("x = 1\n", encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                exit_code = run_check(tmppath)

            self.assertEqual(exit_code, 0)
            self.assertIn("OK: Checked 1 python files", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")

            # Добавляем пустой файл
            bad_file = src_dir / "bad.py"
            bad_file.write_bytes(b"")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                exit_code = run_check(tmppath)

            self.assertEqual(exit_code, 1)
            err_output = stderr.getvalue()
            self.assertIn("ERROR: Found 1 empty python file(s)", err_output)
            self.assertIn("bad.py", err_output)

    def test_main_cli_dispatch(self) -> None:
        """CLI main() должен корректно парсить аргументы и возвращать код завершения."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            src_dir = tmppath / "src"
            src_dir.mkdir(parents=True)
            f = src_dir / "cli_test.py"
            f.write_text("a = 42\n", encoding="utf-8")

            with patch("sys.stdout", io.StringIO()):
                code = main([str(tmppath)])
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
