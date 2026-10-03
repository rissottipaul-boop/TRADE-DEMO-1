"""Локальный помощник задач: только чтение, без модели, сети и торговых прав.

Черновики и эвристическая рецензия не меняют доску и не принимают работу.
Факты специалистов снабжены источником; содержимое файлов не является командой.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import threading

from src.agent_context import read_rows
from src.ai_observability import redact


MAX_DESCRIPTION = 4000
MAX_FILES = 40
MAX_FILE_BYTES = 24 * 1024
MAX_DIFF_BYTES = 96 * 1024
MAX_NAME_BYTES = 64 * 1024
MAX_RUN_BYTES = 256 * 1024
GIT_TIMEOUT = 8
MODE = "local-deterministic-read-only"
SPECIALIST_TOOLS = {
    "crypto-insight-hunter": ("task.read", "sources.search"),
    "insight-executor": ("task.read", "diff.review"),
    "ops-sentinel": ("task.read",),
}
ROLE_NAMES = {"insight-executor": "Insight Executor",
              "crypto-insight-hunter": "Crypto Insight Hunter",
              "ops-sentinel": "Ops Sentinel"}
TEXT_EXTENSIONS = {".py", ".ps1", ".md", ".json", ".js", ".ts", ".html", ".css",
                   ".txt", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".cmd", ".bat"}


def _sanitize(value):
    """Единая фильтрация; полные критерии доски сохраняются в пределах её лимита."""
    return redact(value, max_text=1024 * 1024)


def _text(value, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\0" in value:
        raise ValueError(f"Неверное поле {field}: нужна непустая строка до {maximum} символов")
    return value.strip()


def _identifier(value, *, run: bool = False) -> str:
    pattern = r"run_[A-Za-z0-9_-]{1,80}" if run else r"[A-Za-z0-9_-]{1,80}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("Неверный run_id" if run else "Неверный task_id")
    return value


def _safe_path(root: Path, name: str) -> Path:
    root = Path(root).resolve()
    name = name.replace("\\", "/")
    parts = PurePosixPath(name).parts
    if not parts or PurePosixPath(name).is_absolute() or any(p in (".", "..") or ":" in p for p in parts):
        raise ValueError("Путь вне рабочей копии")
    path = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("Ссылки и junction не читаются")
    if not path.resolve().is_relative_to(root):
        raise ValueError("Путь вне рабочей копии")
    return path


def _read(root: Path, name: str, limit: int) -> str:
    path = _safe_path(root, name)
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"Превышен предел чтения: {name}")
    return raw.decode("utf-8-sig")


def _task(root: Path, task_id: str) -> dict:
    task_id = _identifier(task_id)
    text = _read(root, "ops/board.md", 1024 * 1024)
    variants = read_rows(text).get(task_id, [])
    if len(variants) != 1:
        raise ValueError("Задача отсутствует или её ID неоднозначен")
    row = variants[0]
    headings = [s.strip().lower() for s in row["header"].strip("|").split("|")]
    cells = [s.strip() for s in row["raw"].strip("|").split("|")]
    return _sanitize({"task_id": task_id, "goal": row["task"]["title"],
                      "acceptance_criteria": cells[headings.index("критерий готовности")],
                      "notes": cells[headings.index("заметки")], "status": row["task"]["status"],
                      "agent": row["task"]["agent"], "dependencies": row["task"]["deps"],
                      "source": f"ops/board.md:{row['line']}",
                      "board_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})


def recommend_role(description: str) -> dict:
    description = _text(description, "description", MAX_DESCRIPTION)
    lowered = description.lower()
    groups = (("ops-sentinel", ("инцидент", "авар", "монитор", "завис", "kill-switch", "breaker", "alert")),
              ("crypto-insight-hunter", ("исслед", "источник", "изуч", "сравн", "research", "проверить факт", "обзор api")))
    for role, words in groups:
        matched = [word for word in words if word in lowered]
        if matched:
            return {"role": role, "name": ROLE_NAMES[role], "reason": f"Признаки задачи: {', '.join(matched)}",
                    "mode": MODE, "confidence": "heuristic", "launch_authorized": False}
    return {"role": "insight-executor", "name": ROLE_NAMES["insight-executor"],
            "reason": "Задача по реализации или исправлению; уточнить назначение перед CLAIM", "mode": MODE,
            "confidence": "heuristic", "launch_authorized": False}


def task_draft(description: str) -> dict:
    description = _text(description, "description", MAX_DESCRIPTION)
    role = recommend_role(description)
    lowered = description.lower()
    protected = [label for label, pattern in (
        ("live-торговля", r"\blive\b|реальн.*торгов"), ("секреты", r"\.env|токен|секрет|api.?key"),
        ("guard/автопилот", r"guard|autopilot|автопилот"),
        ("сброс блокировок", r"(?:сброс|reset).*(?:kill|breaker|блокиров)"),
        ("ослабление риска", r"(?:ослаб|увелич).*(?:лимит|риск)"),
        ("удаление состояния", r"(?:удал|delete).*(?:данн|состояни|\.db)")) if re.search(pattern, lowered)]
    role_criteria = {
        "insight-executor": ["Поведение из описания воспроизведено и исправлено; приложен diff и пример результата.",
                             "Все unit-тесты и затронутые самотесты зелёные; команды и результаты записаны.",
                             "Для изменений торговли проведена сверка на demo; ордера и итоговое состояние записаны."],
        "crypto-insight-hunter": ["Исследование в insights/ содержит статус, выводы и ограничения применимости.",
                                  "Ключевые факты подтверждены двумя источниками с датами и ссылками.",
                                  "Открытые вопросы сформулированы как отдельные задачи доски."],
        "ops-sentinel": ["Состояние заново получено штатными командами; зафиксированы время и источник.",
                         "Причина, безопасные меры и оставшиеся действия записаны в ops/incidents.md.",
                         "После мер повторная проверка подтверждает состояние; блокировки самовольно не сброшены."],
    }
    criteria = [f"Целевой результат: {description}", *role_criteria[role["role"]],
                "Claim, guard, секреты и действующие лимиты проекта сохранены; документация обновлена."]
    result = _sanitize({"mode": MODE, "kind": "draft", "title": description.splitlines()[0][:120],
                      "goal": description, "recommended_role": role, "acceptance_criteria": criteria,
                      "criterion_text": " ".join(criteria), "suggested_status": "needs-user" if protected else "ready",
                      "requires_human_decision": protected,
                      "open_questions": ["Уточнить воспроизводимый пример и ожидаемый результат перед приёмкой."],
                      "board_written": False, "source_of_truth": "ops/board.md"})
    result["markdown"] = "\n".join([f"# Черновик: {result['title']}", "Локальный помощник; задача ещё не записана в ops/board.md.",
                                    "", result["goal"], "", f"Роль: {role['name']}. {role['reason']}",
                                    "", "Критерий готовности:", *[f"- {value}" for value in result["acceptance_criteria"]],
                                    "", f"Предлагаемый статус: {result['suggested_status']}",
                                    "Нужно решение человека: " + (", ".join(protected) or "не выявлено; требуется сверка AGENTS.md")])
    return result


def _load_run(root: Path, task_id: str, run: dict | None, run_id: str | None) -> dict | None:
    if run_id is not None:
        _identifier(run_id, run=True)
    if run is None and run_id:
        run = json.loads(_read(root, f"data/runs/{run_id}.json", MAX_RUN_BYTES))
    if run is not None:
        if not isinstance(run, dict) or run.get("task_id") != task_id:
            raise ValueError("Запуск не принадлежит задаче")
        _identifier(run.get("id"), run=True)
        if run_id and run.get("id") != run_id:
            raise ValueError("run_id не соответствует записи")
    return run


def build_handoff(root: Path, task_id: str, run: dict | None = None, run_id: str | None = None) -> dict:
    task = _task(root, task_id)
    run = _load_run(root, task_id, run, run_id)
    result = {**task, "mode": MODE, "generated_at": datetime.now(timezone.utc).isoformat(),
              "changed_files": [], "checks": [], "external_actions": [], "next_step": None,
              "run": None, "missing_evidence": [], "source_of_truth": "ops/board.md"}
    if run:
        result["run"] = {key: run.get(key) for key in ("id", "role", "runtime", "model", "status", "exit_code")}
        payload = run.get("result") if isinstance(run.get("result"), dict) else {}
        for field in ("changed_files", "checks", "external_actions"):
            values = payload.get(field, run.get(field, []))
            if isinstance(values, list):
                result[field] = values[:100]
                if len(values) > 100:
                    result["missing_evidence"].append(f"{field}: список обрезан до 100")
        result["next_step"] = payload.get("next_step", run.get("next_step"))
        for event in run.get("events", [])[-200:] if isinstance(run.get("events"), list) else []:
            if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
                continue
            data = event["data"]
            if event.get("type") in ("check.completed", "run.check", "test.result"):
                result["checks"].append({**data, "source": f"data/runs/{run['id']}.json:event:{event.get('cursor')}"})
            if event.get("type") in ("external.action", "tool.unknown"):
                result["external_actions"].append({**data, "source": f"data/runs/{run['id']}.json:event:{event.get('cursor')}"})
        result["provenance"] = f"data/runs/{run['id']}.json (заявленные данные запуска, без повторного исполнения)"
    else:
        result["provenance"] = task["source"]
    for field in ("changed_files", "checks", "external_actions"):
        if len(result[field]) > 100:
            result[field] = result[field][:100]
            result["missing_evidence"].append(f"{field}: события превысили предел 100; перед продолжением читать источник")
        if not result[field]:
            result["missing_evidence"].append(f"{field}: сведений нет; это не подтверждение отсутствия")
    if not result["next_step"]:
        result["next_step"] = "SYNC по актуальной доске и diff; сверить неизвестные внешние действия до любого повтора."
        result["missing_evidence"].append("next_step: использована безопасная рекомендация, а не отчёт агента")
    result["resume_authorized"] = False
    result = _sanitize(result)
    markdown = [f"# Передача задачи {task_id}", f"Цель: {result['goal']}",
                f"Критерий: {result['acceptance_criteria']}", f"Источник: {result['source']}",
                f"SHA256 доски: {result['board_sha256']}", ""]
    if result["run"]:
        markdown.append("Запуск: " + json.dumps(result["run"], ensure_ascii=False))
    for label, field in (("Изменённые файлы", "changed_files"), ("Проверки", "checks"), ("Внешние действия", "external_actions")):
        lines = ["- " + (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)) for value in result[field]]
        markdown += ["", label + ":", *(lines or ["- сведений нет"])]
    markdown += ["", "Следующий шаг: " + str(result["next_step"]), "", "Недостающие доказательства:",
                 *["- " + value for value in result["missing_evidence"]], "", "Передача не создаёт claim и не разрешает продолжение сессии."]
    result["markdown"] = "\n".join(markdown)
    return result


def _git(root: Path, args: list[str], limit: int) -> tuple[bytes, bool]:
    """Фиксированные read-only argv; ограничены и время, и буфер вывода."""
    command = ["git", "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false", *args]
    chunks = bytearray()
    truncated = False
    errors = []
    with subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        def collect():
            nonlocal truncated
            try:
                while True:
                    block = process.stdout.read(4096)
                    if not block:
                        break
                    available = limit - len(chunks)
                    chunks.extend(block[:available])
                    if len(block) > available:
                        truncated = True
                        process.kill()
                        break
            except OSError as exc:
                errors.append(exc)
        worker = threading.Thread(target=collect, daemon=True)
        worker.start()
        worker.join(GIT_TIMEOUT)
        if worker.is_alive():
            process.kill()
            worker.join(2)
            raise RuntimeError("Превышено время чтения Git")
        code = process.wait(timeout=2)
        if errors or (code != 0 and not truncated):
            raise RuntimeError("Не удалось прочитать Git; содержимое stderr исключено")
    return bytes(chunks), truncated


def _protected(name: str) -> str | None:
    lower = name.lower().replace("\\", "/")
    parts = lower.split("/")
    if any(p == ".env" or p.startswith(".env.") for p in parts) or lower.endswith((".pem", ".key", ".p12", ".pfx")) or any(
            word in lower for word in ("credentials", "secrets", "token.dpapi", "id_rsa", "id_ed25519")):
        return "secret-file"
    if lower.startswith(("ops/hooks/", ".github/hooks/")) or lower in (
            "ops/autopilot.json", ".codex/hooks.json", ".claude/settings.json", ".gemini/settings.json", ".muse/hooks.json"):
        return "guard-autopilot-change"
    if lower == "ops/live-policy.json":
        return "live-policy-change"
    if lower in ("src/risk.py", "pump-pocket.json", "ops/pump-pocket.json"):
        return "risk-change-needs-review"
    if lower.startswith(("notes/", ".obsidian/")):
        return "human-space-change"
    return None


def _review_checkout(root: Path, run: dict | None) -> tuple[Path, str | None]:
    """Не доверять cwd из JSON: сверить независимый lease и регистрацию Git."""
    if not run or run.get("workspace") != "worktree":
        return root, None
    task_id, run_id = _identifier(run.get("task_id")), _identifier(run.get("id"), run=True)
    saved = json.loads(_read(root, f"data/worktree-leases/{task_id}.json", 16 * 1024))
    reported = run.get("lease")
    if not isinstance(saved, dict) or saved.get("schema_version") != 1 or not isinstance(reported, dict):
        raise ValueError("Worktree lease не подтверждён")
    expected = _safe_path(root.parent, f"{root.name}-agent-worktrees/{run_id}")
    for entry in (saved, reported):
        if (entry.get("task_id"), entry.get("run_id")) != (task_id, run_id) or not isinstance(entry.get("worktree"), str):
            raise ValueError("Worktree lease принадлежит другой задаче или запуску")
        if Path(entry["worktree"]).resolve() != expected.resolve():
            raise ValueError("Worktree вне ожидаемого каталога запуска")
        if entry.get("branch") != f"agent/{run_id}":
            raise ValueError("Ветка worktree не соответствует запуску")
    base = saved.get("base_commit")
    if not isinstance(base, str) or not re.fullmatch(r"[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?", base) or reported.get("base_commit") != base:
        raise ValueError("Базовый коммит worktree не подтверждён")
    raw, truncated = _git(root, ["worktree", "list", "--porcelain", "-z"], MAX_NAME_BYTES)
    if truncated:
        raise ValueError("Список регистрации worktree обрезан")
    registered = False
    for record in raw.split(b"\0\0"):
        fields = record.decode("utf-8", errors="replace").split("\0")
        path = next((value[9:] for value in fields if value.startswith("worktree ")), "")
        branch = next((value[7:] for value in fields if value.startswith("branch ")), "")
        if path and Path(path).resolve() == expected.resolve() and branch == f"refs/heads/agent/{run_id}":
            registered = True
    if not registered:
        raise ValueError("Worktree не зарегистрирован с ожидаемой веткой")
    return expected.resolve(), base


def review_diff(root: Path, task_id: str, run_id: str | None = None) -> dict:
    root = Path(root).resolve()
    task = _task(root, task_id)
    run = _load_run(root, task_id, None, run_id)
    checkout, base_commit = _review_checkout(root, run)
    report = {"mode": MODE, "task_id": task_id, "goal": task["goal"],
              "acceptance_criteria": task["acceptance_criteria"], "task_source": task["source"],
              "run_id": run_id, "scope": "current-checkout", "attribution": "not-proven",
              "verdict": "requires-human-review", "accepted": False,
              "semantic_acceptance": "not-evaluated", "files": [], "findings": [], "complete": True,
              "limits": {"max_files": MAX_FILES, "max_file_bytes": MAX_FILE_BYTES,
                         "max_diff_bytes": MAX_DIFF_BYTES, "git_timeout_s": GIT_TIMEOUT},
              "limitations": ["Локальная эвристика не доказывает семантический критерий и зелёные тесты.",
                              "Общий checkout может содержать чужие изменения; авторство не подтверждено.",
                              "Committed diff и содержимое submodule не входят в эту проверку."],
              "checks": [], "provenance": "git diff (index/worktree), git ls-files (untracked)"}
    if base_commit:
        report["scope"] = "verified-worktree"
        report["attribution"] = "lease-linked-not-exclusive"
        report["base_commit"] = base_commit
        report["limitations"][2] = "Committed diff относительно lease base_commit включён; submodule не проверены."
    consumed = 0
    seen = 0
    commands = [("staged", ["diff", "--cached", "--name-only", "-z", "--no-renames", "--ignore-submodules=all"]),
                          ("unstaged", ["diff", "--name-only", "-z", "--no-renames", "--ignore-submodules=all"]),
                          ("untracked", ["ls-files", "--others", "--exclude-standard", "-z"])]
    if base_commit:
        commands.insert(0, ("committed", ["diff", f"{base_commit}..HEAD", "--name-only", "-z", "--no-renames", "--ignore-submodules=all"]))
    for kind, command in commands:
        raw, truncated = _git(checkout, command, MAX_NAME_BYTES)
        if truncated:
            report["complete"] = False
            raw = raw.rsplit(b"\0", 1)[0] + b"\0" if b"\0" in raw else b""
        for name in (n.decode("utf-8", errors="replace") for n in raw.split(b"\0") if n):
            if seen >= MAX_FILES:
                report["complete"] = False
                break
            seen += 1
            entry = {"path": name, "kind": kind, "patch": None, "truncated": False}
            report["files"].append(entry)
            protected = _protected(name)
            if protected:
                report["findings"].append({"code": protected, "path": name,
                                           "severity": "requires-human-decision" if protected != "risk-change-needs-review" else "review",
                                           "detail": "Изменён защищённый путь; чтение секретного содержимого исключено."})
            if protected == "secret-file":
                entry["omitted"] = "secret-file"
                report["complete"] = False
                continue
            try:
                _safe_path(checkout, name)
                remaining = min(MAX_FILE_BYTES, MAX_DIFF_BYTES - consumed)
                if remaining <= 0:
                    entry["omitted"] = "total-byte-limit"
                    report["complete"] = False
                    continue
                if kind == "untracked":
                    if Path(name).suffix.lower() not in TEXT_EXTENSIONS:
                        raise ValueError("Формат untracked-файла не разрешён для просмотра")
                    with _safe_path(checkout, name).open("rb") as stream:
                        content = stream.read(remaining + 1)
                    entry["truncated"] = len(content) > remaining
                    content = content[:remaining]
                else:
                    flags = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--ignore-submodules=all", "--unified=3"]
                    if kind == "staged":
                        flags.append("--cached")
                    if kind == "committed":
                        flags.append(f"{base_commit}..HEAD")
                    content, entry["truncated"] = _git(checkout, [*flags, "--", name], remaining)
                consumed += len(content)
                if b"\0" in content:
                    raise ValueError("Бинарное содержимое исключено")
                patch = content.decode("utf-8-sig")
                if patch.startswith("Binary files ") or "\nBinary files " in patch:
                    raise ValueError("Бинарное изменение требует отдельной проверки")
                entry["patch"] = _sanitize(patch)
                if entry["truncated"]:
                    report["complete"] = False
                if entry["patch"] != patch:
                    report["findings"].append({"code": "possible-secret", "path": name,
                                               "severity": "review", "detail": "В изменении обнаружена и скрыта возможная секретная строка."})
                additions = "\n".join(line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")) if kind != "untracked" else patch
                for code, pattern in (("risk-bypass-suspected", r"(?i)(?:skip|bypass|disable)[_ -]*(?:risk|guard)|check_entry_allowed\s*=\s*(?:None|False)"),
                                      ("live-or-reset-suspected", r"(?i)(?:--live\b|\bops\s+reset\b|\bwithdraw(?:al)?\b)")):
                    hit = re.search(pattern, additions)
                    if hit:
                        report["findings"].append({"code": code, "path": name, "severity": "review",
                                                   "added_line": additions[:hit.start()].count("\n") + 1,
                                                   "detail": "Признак требует ручной сверки; эвристика не доказывает нарушение."})
            except (OSError, UnicodeError, ValueError) as exc:
                entry["omitted"] = _sanitize(str(exc))
                report["complete"] = False
    report["bytes_examined"] = consumed
    if not report["files"]:
        report["limitations"].append("В текущем checkout нет видимого diff; это не доказательство выполнения задачи.")
    report = _sanitize(report)
    report["markdown"] = "\n".join([f"# Проверка изменений: {task_id}", f"Цель: {report['goal']}",
                                     f"Критерий: {report['acceptance_criteria']}", f"Область: {report['scope']}",
                                     "Вердикт: требуется ручная рецензия; семантический критерий и тесты не проверены.",
                                     "Полнота просмотра: " + ("в пределах указанных ограничений" if report["complete"] else "неполный просмотр"),
                                     "", "Находки:", *["- " + json.dumps(value, ensure_ascii=False) for value in report["findings"]],
                                     "", "Файлы:", *[f"- {value['kind']}: {value['path']}" +
                                                        (f"; исключён: {value['omitted']}" if value.get("omitted") else
                                                         "; diff обрезан" if value["truncated"] else "") for value in report["files"]],
                                     "", "Ограничения:", *["- " + value for value in report["limitations"]]])
    return report


def specialist_tools(role: str) -> list[str]:
    if not isinstance(role, str) or role not in SPECIALIST_TOOLS:
        raise ValueError("Неизвестная роль специалиста")
    return list(SPECIALIST_TOOLS[role])


def _sources(root: Path, query: str) -> dict:
    query = _text(query, "query", 200)
    words = {word.casefold() for word in re.findall(r"[\w-]{2,}", query)}
    if not words:
        raise ValueError("Запрос должен содержать слова длиной от двух символов")
    base = _safe_path(root, "insights")
    facts, sources = [], []
    truncated = False
    for index, path in enumerate(sorted(base.glob("*.md"))):
        if index >= 100:
            truncated = True
            break
        try:
            name = path.relative_to(Path(root).resolve()).as_posix()
            text = _read(root, name, 64 * 1024)
        except (OSError, UnicodeError, ValueError):
            truncated = True
            continue
        hits = [(line, value) for line, value in enumerate(text.splitlines(), 1)
                if any(word in value.casefold() for word in words)]
        if not hits:
            continue
        sources.append({"path": name, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        "links": re.findall(r"https?://[^\s\])<>]+", text)[:10], "verification": "local-document-only"})
        for line, value in hits[:3]:
            facts.append({"text": _sanitize(value)[:600], "source": f"{name}:{line}",
                          "quality": "document-claim-unverified", "freshness": "not-verified"})
        if len(sources) >= 10:
            truncated = True
            break
    return _sanitize({"facts": facts, "sources": sources, "truncated": truncated,
                      "limitations": ["Поиск в локальных insights/*.md; ссылки и актуальность внешне не проверялись."]})


def call_specialist(root: Path, role: str, tool: str, arguments: dict) -> dict:
    allowed = specialist_tools(role)
    if tool not in allowed or not isinstance(arguments, dict):
        raise ValueError("Инструмент специалиста запрещён")
    schema = {"task.read": ({"task_id"}, set()), "sources.search": ({"query"}, set()),
              "diff.review": ({"task_id"}, {"run_id"})}
    required, optional = schema[tool]
    if not required <= arguments.keys() or not arguments.keys() <= required | optional:
        raise ValueError("Аргументы не соответствуют схеме инструмента")
    if tool == "task.read":
        task = _task(root, arguments["task_id"])
        result = {"facts": [{"goal": task["goal"], "acceptance_criteria": task["acceptance_criteria"],
                             "status": task["status"], "source": task["source"]}],
                  "sources": [task["source"]], "limitations": ["Статус доски не подтверждает состояние биржи."]}
    elif tool == "sources.search":
        result = _sources(Path(root).resolve(), arguments["query"])
    else:
        review = review_diff(root, arguments["task_id"], arguments.get("run_id"))
        result = {"facts": review["findings"], "sources": [review["task_source"], review["provenance"]],
                  "review": review, "limitations": review["limitations"]}
    return {"role": role, "tool": tool, "mode": MODE, "tools_allowed": allowed,
            "execution_authorized": False, **result}
