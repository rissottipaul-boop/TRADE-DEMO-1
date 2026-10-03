"""Структурированный отчёт запуска: python -m src.agent_run_report.

Записывает только data/run-results/<run_id>.json текущего checkout. Это
заявленный отчёт агента, не независимая приёмка, не запуск команд и не торговля.
Пример: --run-id run_T1_x --task-id T1 --input report.json; '-' читает stdin.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile

from src.ai_observability import redact


ROOT = Path(__file__).absolute().parents[1]
MAX_BYTES = 64 * 1024
MAX_ITEMS = 50
REPORT_FIELDS = {"summary", "changed_files", "checks", "external_actions", "next_step"}
ENVELOPE_FIELDS = {"schema_version", "run_id", "task_id", "quality", "acceptance_verified",
                   "reported_at", "result", "provenance"}
CHECK_STATUSES = {"passed", "failed", "unknown", "not-run"}
ACTION_STATUSES = {"completed", "failed", "cancelled", "unknown", "pending", "not-executed"}


def _id(value, *, run: bool = False) -> str:
    pattern = r"run_[A-Za-z0-9_-]{1,80}" if run else r"[A-Za-z0-9_-]{1,80}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("Неверная привязка запуска или задачи")
    return value


def _string(value, limit: int, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\0" in value:
        raise ValueError(f"Неверное поле отчёта: {field}")
    return value.strip()


def _no_links(path: Path) -> None:
    for current in (*reversed(path.parents), path):
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("Ссылки и junction запрещены для отчёта")


def _root(root: Path) -> Path:
    path = Path(root).absolute()
    _no_links(path)
    if not path.is_dir():
        raise ValueError("Корень checkout недоступен")
    return path.resolve()


def _relative_ref(root: Path, value, field: str) -> str:
    value = _string(value, 300, field).replace("\\", "/")
    parts = PurePosixPath(value).parts
    if (PurePosixPath(value).is_absolute() or not parts or value != "/".join(parts)
            or any(part in (".", "..") for part in parts)
            or any(character in value for character in ":?#=%*[]|`\r\n\t\0")):
        raise ValueError(f"Нужен относительный путь без query/спецсимволов: {field}")
    lower = value.lower()
    if (any(part == ".git" or part == ".env" or part.startswith(".env.") for part in lower.split("/"))
            or lower.endswith((".pem", ".key", ".pfx", ".p12", ".dpapi"))
            or any(word in lower for word in ("credentials", "secrets", "id_rsa", "id_ed25519"))):
        raise ValueError(f"Секретный путь запрещён: {field}")
    path = root.joinpath(*parts)
    _no_links(path)
    if not path.resolve().is_relative_to(root):
        raise ValueError("Путь отчёта выходит из checkout")
    return value


def _list(value, field: str) -> list:
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise ValueError(f"Неверный список отчёта: {field}")
    return value


def validate_report(root: Path, report: dict) -> dict:
    """Закрытая схема; команды — только строки отчёта, никогда не исполняются."""
    root = _root(root)
    if not isinstance(report, dict) or set(report) != REPORT_FIELDS:
        raise ValueError("Нужна полная схема отчёта без неизвестных полей")
    result = {"summary": _string(report["summary"], 2000, "summary"),
              "changed_files": [_relative_ref(root, value, "changed_files")
                                for value in _list(report["changed_files"], "changed_files")],
              "checks": [], "external_actions": [],
              "next_step": _string(report["next_step"], 2000, "next_step")}
    if len(set(result["changed_files"])) != len(result["changed_files"]):
        raise ValueError("changed_files содержит дубликаты")
    for check in _list(report["checks"], "checks"):
        required = {"command", "exit_code", "status"}
        if not isinstance(check, dict) or not required <= set(check) or set(check) - required - {"artifact_ref"}:
            raise ValueError("Неверная схема checks")
        command = _string(check["command"], 1000, "checks.command")
        status, code = check["status"], check["exit_code"]
        if not isinstance(status, str) or status not in CHECK_STATUSES or (code is not None and (
                type(code) is not int or not -2147483648 <= code <= 2147483647)):
            raise ValueError("Неверный status/exit_code проверки")
        if (status == "passed" and code != 0) or (status == "failed" and (code is None or code == 0)) or (status == "not-run" and code is not None):
            raise ValueError("status и exit_code проверки противоречат друг другу")
        item = {"command": command, "exit_code": code, "status": status}
        if "artifact_ref" in check:
            item["artifact_ref"] = _relative_ref(root, check["artifact_ref"], "checks.artifact_ref")
        result["checks"].append(item)
    for action in _list(report["external_actions"], "external_actions"):
        if not isinstance(action, dict) or set(action) != {"id", "kind", "status"}:
            raise ValueError("Неверная схема external_actions")
        identifier = _string(action["id"], 128, "external_actions.id")
        kind = _string(action["kind"], 40, "external_actions.kind")
        status = action["status"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", identifier) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,39}", kind):
            raise ValueError("ID и kind внешнего действия должны быть идентификаторами без query")
        if not isinstance(status, str) or status not in ACTION_STATUSES:
            raise ValueError("Неверный статус внешнего действия")
        if redact(identifier) != identifier or redact(kind) != kind:
            raise ValueError("Секрет не может служить ID внешнего действия")
        result["external_actions"].append({"id": identifier, "kind": kind, "status": status})
    clean = redact(result, max_text=MAX_BYTES)
    if len(json.dumps(clean, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_BYTES:
        raise ValueError("Отчёт превышает предел байтов")
    return clean


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Дублирующиеся поля JSON запрещены")
        result[key] = value
    return result


def _json(raw: bytes):
    if len(raw) > MAX_BYTES:
        raise ValueError("Превышен предел JSON")
    def reject_constant(_value):
        raise ValueError("Нефинитные числа JSON запрещены")
    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs, parse_constant=reject_constant)
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("Некорректный JSON отчёта") from exc


def _destination(root: Path, run_id: str) -> Path:
    destination = root / "data" / "run-results" / f"{run_id}.json"
    _no_links(destination)
    if not destination.resolve().is_relative_to(root):
        raise ValueError("Хранилище отчётов вне checkout")
    return destination


def load_report(root: Path, run_id: str, task_id: str) -> dict | None:
    """Только фиксированное хранилище; отсутствующий отчёт не считается успехом."""
    root = _root(root)
    run_id, task_id = _id(run_id, run=True), _id(task_id)
    path = _destination(root, run_id)
    if not path.exists():
        return None
    with path.open("rb") as stream:
        envelope = _json(stream.read(MAX_BYTES + 1))
    if not isinstance(envelope, dict) or set(envelope) != ENVELOPE_FIELDS:
        raise ValueError("Неверная схема сохранённого отчёта")
    expected_provenance = f"data/run-results/{run_id}.json"
    if (type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1
            or (envelope["run_id"], envelope["task_id"]) != (run_id, task_id)
            or envelope["quality"] != "reported" or envelope["acceptance_verified"] is not False
            or envelope["provenance"] != expected_provenance):
        raise ValueError("Привязка или качество отчёта не подтверждены")
    try:
        stamp = datetime.fromisoformat(envelope["reported_at"])
        if stamp.tzinfo is None:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Время отчёта некорректно") from None
    return {**envelope, "result": validate_report(root, envelope["result"])}


def write_report(root: Path, run_id: str, task_id: str, report: dict) -> dict:
    """Полная валидация до записи; atomic replace только собственного binding."""
    root = _root(root)
    run_id, task_id = _id(run_id, run=True), _id(task_id)
    result = validate_report(root, report)
    envelope = {"schema_version": 1, "run_id": run_id, "task_id": task_id,
                "quality": "reported", "acceptance_verified": False,
                "reported_at": datetime.now(timezone.utc).isoformat(), "result": result,
                "provenance": f"data/run-results/{run_id}.json"}
    raw = json.dumps(envelope, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError("Конверт отчёта превышает предел байтов")
    destination = _destination(root, run_id)
    if destination.exists():
        load_report(root, run_id, task_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _no_links(destination)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=f".{run_id}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Hard link — атомарная установка без перезаписи победителя параллельного
        # первого вызова. Обновление существующего файла требует того же binding.
        try:
            os.link(temporary, destination)
        except FileExistsError:
            load_report(root, run_id, task_id)
            _no_links(destination)
            os.replace(temporary, destination)
            temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return envelope


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--input", default="-", help="Относительный JSON-файл в checkout либо '-' для stdin")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        root = _root(ROOT)
        if args.input == "-":
            raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        else:
            reference = _relative_ref(root, args.input, "input")
            if not reference.lower().endswith(".json"):
                raise ValueError("Нужен JSON-файл отчёта")
            with (root / reference).open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
        envelope = write_report(root, args.run_id, args.task_id, _json(raw))
    except (OSError, ValueError, TypeError, RecursionError):
        print(json.dumps({"ok": False, "error": "Отчёт отвергнут: проверьте схему, привязку, пределы и пути"}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, "run_id": envelope["run_id"], "task_id": envelope["task_id"],
                      "provenance": envelope["provenance"], "quality": "reported", "acceptance_verified": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
