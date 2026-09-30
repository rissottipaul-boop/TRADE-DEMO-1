"""Адресное чтение доски: без LLM, сети, торговли и записи файлов.

Статусы/зависимости/расписание разбирает существующий autopilot.
Здесь добавлены исходные строки и обнаружение неоднозначностей, а не планировщик.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

from ops.hooks.autopilot import parse_board, ready_tasks

ROOT = Path(__file__).resolve().parents[1]
STATUSES = {"ready", "in-progress", "blocked", "needs-user", "scheduled", "done"}


def read_rows(text: str) -> dict[str, list[dict]]:
    """Сохранить каждую строку, включая дубликаты; не пересказывать заметки."""
    rows: dict[str, list[dict]] = {}
    header = None
    width = 0
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip().startswith("|"):
            if not line.strip():
                header = None
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        lowered = [c.lower() for c in cells]
        if "id" in lowered and "статус" in lowered:
            if not {"задача", "агент", "зависит от", "критерий готовности", "заметки"} <= set(lowered):
                raise ValueError(f"Неполный заголовок доски, строка {number}; читайте оригинал")
            header, width = line, len(cells)
            continue
        if header is None or set(line.replace("|", "").strip()) <= set("-: "):
            continue
        if len(cells) != width:
            raise ValueError(f"Неоднозначные ячейки, строка {number}; читайте оригинал")
        parsed = parse_board(header + "\n" + line)
        for task_id, task in parsed.items():
            if task["status"] not in STATUSES:
                raise ValueError(f"Неизвестный статус {task_id}, строка {number}; читайте оригинал")
            rows.setdefault(task_id, []).append({"line": number, "header": header, "raw": line, "task": task})
    if not rows:
        raise ValueError("Таблица задач не найдена; читайте исходную доску")
    return rows


def dependencies(task_id: str, rows: dict[str, list[dict]]) -> list[str]:
    """Замыкание зависимостей с сохранением всех ветвей дублирующихся ID."""
    seen = {task_id}
    result = []

    def visit(current: str) -> None:
        for row in rows.get(current, []):
            for dep in row["task"]["deps"]:
                if dep not in seen:
                    seen.add(dep)
                    result.append(dep)
                    visit(dep)

    visit(task_id)
    return result


def build_context(text: str, task_id: str | None = None, *, now: datetime | None = None,
                  decisions: list[str] | None = None) -> str:
    rows = read_rows(text)
    tasks = parse_board(text)
    if task_id is not None and task_id not in rows:
        raise ValueError(f"Задача {task_id!r} не найдена; получите обзор без --task")
    now = now or datetime.now(timezone.utc)
    duplicate_ids = {key for key, value in rows.items() if len(value) > 1}
    missing = {dep for variants in rows.values() for row in variants
               for dep in row["task"]["deps"] if dep not in rows}
    candidates = ready_tasks(tasks, now)
    ready = [key for key in candidates
             if not ({key, *dependencies(key, rows)} & (duplicate_ids | missing))]
    parts = ["# Контекст доски (навигация, не разрешение действий)",
             "Источник: ops/board.md; ссылки ниже — номера строк в этом файле.",
             f"SHA256: {hashlib.sha256(text.encode('utf-8')).hexdigest()}",
             f"Расписание проверено на: {now.isoformat()}",
             "Перед CLAIM/правкой перечитать актуальные строки; AGENTS.md и каноническая роль обязательны.",
             "Для торговли/ops заново выполнить штатные проверки состояния; снимок Obsidian их не заменяет."]
    if duplicate_ids:
        parts += ["\n## Неоднозначности — не брать эти ID до сверки оригинала"]
        for key in sorted(duplicate_ids):
            parts.append(f"- Дубликат {key}: строки " + ", ".join(str(r["line"]) for r in rows[key]))
    if missing:
        parts.append("Отсутствующие зависимости: " + ", ".join(sorted(missing)))
    if decisions:
        parts += ["\n## Новые ответы человека — прочитать перед выбором задачи"]
        parts.extend(f"- {path}" for path in decisions)
    if task_id is None:
        counts = Counter(row["task"]["status"] for variants in rows.values() for row in variants)
        parts += ["\n## Обзор", "Строк по статусам: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())),
                  "Кандидаты по правилам автопилота, кроме неоднозначных: " + (", ".join(ready) or "нет"),
                  "Критерии и заметки здесь не показаны. До работы запросить --task ID; вывод не является claim.",
                  "| Строка | ID | Статус | Агент | Зависимости | Задача |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for key, variants in rows.items():
            for row in variants:
                task = row["task"]
                if task["status"] == "done":
                    continue
                parts.append(f"| {row['line']} | {key} | {task['status']} {task['arg']} | {task['agent']} | "
                             f"{', '.join(task['deps']) or '—'} | {task['title']} |")
    else:
        parts += [f"\n## Полная карточка {task_id} — без сокращения критериев и заметок"]
        for row in rows[task_id]:
            parts += [f"Строка {row['line']}:", row["header"], row["raw"]]
        parts += ["\n## Зависимости (включая транзитивные)",
                  "Здесь только статусы; для контракта/изменений зависимости читать её --task ID и артефакты."]
        for dep in dependencies(task_id, rows):
            if dep not in rows:
                parts.append(f"- {dep}: ОТСУТСТВУЕТ")
            for row in rows.get(dep, []):
                task = row["task"]
                parts.append(f"- {dep}: {task['status']} {task['arg']}; {task['title']}; строка {row['line']}")
        parts += ["\n## Все активные claims — полные строки для проверки файлов"]
        for key, variants in rows.items():
            if key == task_id:
                continue
            for row in variants:
                if row["task"]["status"] == "in-progress":
                    parts += [f"Строка {row['line']}:", row["raw"]]
        parts.append("\nПроверить также git diff и свежесть файлов; отсутствие claim не доказывает отсутствие чужой работы.")
    return "\n".join(parts) + "\n"


def new_decisions(root: Path) -> list[str]:
    result = []
    for path in sorted((root / "notes" / "decisions").glob("*.md")):
        text = path.read_text(encoding="utf-8-sig")
        frontmatter = text.split("---", 2)
        if len(frontmatter) >= 3 and not frontmatter[0].strip() and re.search(
                r"^status:\s*новое\s*$", frontmatter[1], re.M):
            result.append(path.relative_to(root).as_posix())
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", help="ID задачи: полная карточка, зависимости, все активные claims")
    parser.add_argument("--stats", action="store_true", help="Только размеры текста; не токены и не деньги")
    args = parser.parse_args(argv)
    # UTF-8 при прямом запуске и перенаправлении в PowerShell; без чтения окружения.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        raw = (ROOT / "ops" / "board.md").read_bytes()
        text = raw.decode("utf-8-sig")
        output = build_context(text, args.task, decisions=new_decisions(ROOT))
        if args.stats:
            output_bytes = len(output.encode("utf-8"))
            print(json.dumps({"board_bytes": len(raw), "context_bytes": output_bytes,
                              "reduction_pct_bytes": round(100 * (1 - output_bytes / len(raw)), 2),
                              "task": args.task, "metric": "UTF-8 bytes, not billed tokens"}, ensure_ascii=False))
        else:
            print(output, end="")
        return 0
    except (OSError, UnicodeError, ValueError, IndexError) as exc:
        print(f"agent_context: {exc}. Прочитайте ops/board.md напрямую.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
