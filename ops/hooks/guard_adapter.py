"""Адаптер hook-вызовов Codex, Gemini CLI и Muse Code к общему guard (AGENT-GUARD-COMPAT).

Общий guard (`ops/hooks/guard.py`) понимает формат Claude Code / VS Code:
`{"tool_name": ..., "tool_input": {"command": "..."}}` и отвечает
`hookSpecificOutput.permissionDecision = "deny"`. Другие клиенты шлют иначе:

- Codex: `Bash` и `apply_patch` оба кладут текст в `tool_input.command` — для
  `apply_patch` это ТЕКСТ ПАТЧА, а guard ищет патч в `input`. Хост-инструменты
  (`exec_command` с `cmd`, `functions.exec`, `multi_tool_use.parallel`) могут
  прийти вложенными вызовами или кодом.
- Gemini CLI: событие `BeforeTool`, инструменты `run_shell_command`, `write_file`,
  `replace`, `read_many_files` (`include`), `dir_path`; отказ — только верхнеуровневое
  `{"decision": "deny", "reason": ...}`: `hookSpecificOutput.permissionDecision`
  Gemini не читает, и инструмент выполнится.
- Muse Code в WSL: пути вида `/mnt/c/...`, которые guard не приводит к пути проекта.

Адаптер разворачивает вход в список плоских вызовов в формате guard, прогоняет
каждый через `guard.decide` и отвечает в формате клиента. Сам правил не содержит:
все решения — в guard. Контракт как у guard: allow — пустой вывод и код 0,
сбой адаптера — пропуск с записью в журнал guard (fail-open).

Место: до решения человека — `src/guard_adapter.py` (проверяется тестами, hooks его
не вызывают); патч `insights/guard-compat-patch.diff` переносит файл в периметр
`ops/hooks/` и подключает hook-командами из корня проекта:
    .venv\\Scripts\\python.exe ops\\hooks\\guard_adapter.py --client gemini
    .venv/Scripts/python.exe ops/hooks/guard_adapter.py --client muse    # sh в WSL, Windows-python через interop
Windows-python и в WSL: Linux-python не видит ~/.okx/config.toml Windows, и guard
перестаёт отличать live-профиль okx CLI от demo.

`AGENT_GUARD_TRACE=1` или файл `data/GUARD_TRACE` (клиенты могут чистить env)
дописывает в журнал guard форму каждого вызова (имя инструмента и ключи, без
значений) — для E2E-разведки: сработал ли hook и что шлёт клиент.

Ещё одна находка, общая для всех клиентов: корень проекта содержит пробелы,
и правило guard о перенаправлении `> путь` не видит абсолютный путь
(`echo x > 'C:\\...\\TRADE DEMO 1\\ops\\hooks\\guard.py'`). Адаптер заменяет корень
в тексте команды относительным путём (`relativize_command`).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

CLIENTS = ("auto", "claude", "codex", "gemini", "muse")
MAX_DEPTH = 8


def _find_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "ops" / "hooks" / "guard.py").is_file():
            return parent
    return here.parents[1]


ROOT = _find_root()
GUARD_PATH = ROOT / "ops" / "hooks" / "guard.py"


def load_guard(path: Path = GUARD_PATH) -> ModuleType:
    """Общий guard как модуль — только чтение файла, без копии правил."""
    spec = importlib.util.spec_from_file_location("agent_guard", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"guard не найден: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Call:
    """Плоский вызов в формате, который понимает guard.decide."""
    __slots__ = ("tool_name", "tool_input")

    def __init__(self, tool_name: str, tool_input: Optional[dict] = None):
        self.tool_name = tool_name
        self.tool_input = tool_input if tool_input is not None else {}

    def __repr__(self) -> str:
        return f"Call({self.tool_name!r}, {sorted(self.tool_input)!r})"


# Ключи, под которыми клиенты передают вложенный вызов
_NAME_KEYS = ("tool_name", "toolName", "recipient_name", "name")
_ARGS_KEYS = ("tool_input", "toolArgs", "parameters", "arguments", "args", "input")
# Текст shell-команды
_COMMAND_KEYS = ("command", "cmd", "commandLine", "command_line", "script")
# Код, который исполняет инструмент (code mode): внутри могут быть любые вызовы
_CODE_KEYS = ("code", "source", "js", "javascript")
_CODE_TOOL = re.compile(r"(^|[._:-])(exec|code|eval|js|javascript|repl|script)([._:-]|$)", re.I)
# Пути, которые guard сам не собирает (Gemini: dir_path, include; прочие клиенты)
_EXTRA_PATH_KEYS = ("dir_path", "dirpath", "include", "paths", "file_paths", "filename", "file",
                    "target", "target_file", "destination", "source_path", "cwd", "workdir")
# Любой ключ-путь (включая известные guard) — для приведения WSL-путей
_PATH_KEYS = {"filepath", "file_path", "path", "filepaths", "files", "notebook_path", "notebookpath", "uri",
              "newpath", "new_path", "oldpath", "old_path", "includepattern", "include_pattern", "glob",
              *_EXTRA_PATH_KEYS}
# Признаки записи в аргументах: инструмент с таким входом guard считает пишущим
_WRITE_PAYLOAD_KEYS = {"content", "new_string", "newstring", "new_str", "file_text", "patch", "diff", "edits",
                       "contents", "text"}
_SHELLS = re.compile(r"(^|[/\\])(ba|z|da|k)?sh(\.exe)?$|(^|[/\\])(pwsh|powershell|cmd)(\.exe)?$", re.I)
_SHELL_FLAGS = {"-c", "-lc", "-ic", "-lic", "-command", "/c", "/k", "-encodedcommand"}
_PATCH_BLOCK = re.compile(r"\*\*\* Begin Patch.*?(?:\*\*\* End Patch|\Z)", re.S)
_PATCH_PATH = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.M)
_STRING_LITERAL = re.compile(r"""(["'`])((?:\\.|(?!\1).)*)\1""", re.S)
_WSL_MNT = re.compile(r"^/mnt/([a-zA-Z])(/.*)?$")
_WSL_UNC = re.compile(r"^(?://|\\\\)wsl(?:\$|\.localhost)[/\\][^/\\]+[/\\]mnt[/\\]([a-zA-Z])([/\\].*)?$", re.I)


def _parse_json_object(value: Any) -> Optional[dict]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _root_forms(root: Path) -> list[str]:
    """Корень проекта в формах Windows (c:/...) и WSL (/mnt/c/...), нижний регистр, без слэша в конце."""
    text = str(root).replace("\\", "/").rstrip("/").lower()
    forms = {text}
    win = re.match(r"^([a-z]):(/.*)?$", text)
    if win:
        forms.add(f"/mnt/{win.group(1)}{win.group(2) or ''}")
    wsl = _WSL_MNT.match(text)
    if wsl:
        forms.add(f"{wsl.group(1)}:{wsl.group(2) or ''}")
    return sorted(forms, key=len, reverse=True)


def normalize_path(raw: str, root: Path = ROOT) -> str:
    """WSL-формы пути → путь относительно корня (или c:/... вне проекта).

    Остальные формы (Windows, Git Bash, file://, \\\\?\\) приводит сам guard._norm_path.
    """
    p = raw.strip().strip("\"'")
    unc = _WSL_UNC.match(p)
    if unc:
        p = f"/mnt/{unc.group(1)}{(unc.group(2) or '').replace(chr(92), '/')}"
    low = p.replace("\\", "/").lower()
    for form in _root_forms(root):
        if low == form:
            return "."
        if low.startswith(form + "/"):
            return p.replace("\\", "/")[len(form) + 1:]
    mnt = _WSL_MNT.match(p.replace("\\", "/"))
    if mnt:
        return f"{mnt.group(1)}:{mnt.group(2) or '/'}"
    return raw


def relativize_command(command: str, root: Path = ROOT) -> str:
    """Абсолютный корень проекта в тексте команды → относительный путь.

    В корне есть пробелы («TRADE DEMO 1»): правило guard о перенаправлении
    `> путь` рвёт путь на пробеле, и `echo x > 'C:\\...\\TRADE DEMO 1\\ops\\hooks\\guard.py'`
    проходит. После замены guard видит `> 'ops/hooks/guard.py'`.
    """
    forms = set(_root_forms(root))
    forms |= {re.sub(r"^([a-z]):", r"/\1", f) for f in forms if re.match(r"^[a-z]:", f)}  # Git Bash /c/...
    for form in sorted(forms, key=len, reverse=True):
        pattern = r"[/\\]+".join(re.escape(part) for part in form.split("/"))
        command = re.sub(pattern + r"[/\\]+", "", command, flags=re.I)
    return command


def _normalize_paths(node: Any, root: Path, key: str = "") -> Any:
    if isinstance(node, dict):
        return {k: _normalize_paths(v, root, k) for k, v in node.items()}
    if isinstance(node, list):
        return [_normalize_paths(v, root, key) for v in node]
    if isinstance(node, str) and key.lower() in _PATH_KEYS:
        return normalize_path(node, root)
    return node


def _extra_paths(args: dict) -> list[str]:
    found: list[str] = []
    for key in _EXTRA_PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, list):
            found += [v for v in value if isinstance(v, str)]
    return found


def argv_to_command(argv: list) -> str:
    """argv → текст команды. Обёртка оболочки (bash -lc, powershell -Command, cmd /c) снимается."""
    parts = [str(a) for a in argv]
    if parts and _SHELLS.search(parts[0]):
        for i, part in enumerate(parts[1:], start=1):
            if part.lower() in _SHELL_FLAGS and i + 1 < len(parts):
                return " ".join(parts[i + 1:])
    return " ".join(parts)


def _command_text(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, list) and value and all(isinstance(v, (str, int, float)) for v in value):
        return argv_to_command(value)
    return None


def _patch_calls(patch: str, root: Path) -> Call:
    paths = [normalize_path(a or b, root) for a, b in _PATCH_PATH.findall(patch)]
    return Call("apply_patch", {"input": patch, "filepaths": paths})


def _opaque_call(name: str, code: str, root: Path) -> Call:
    """Код, внутри которого могут быть любые вызовы: проверяется консервативно.

    Весь текст — как команда, строковые литералы — как пути записи. Упоминание
    файла периметра или .env в таком коде — отказ, даже если это чтение.
    """
    literals = [m.group(2) for m in _STRING_LITERAL.finditer(code)]
    paths = [normalize_path(s, root) for s in literals
             if s and len(s) < 400 and "\n" not in s and re.search(r"[/\\]|^\.?[\w.-]+\.\w+$|^\.env", s)]
    return Call(f"{name or 'code'}:opaque_write", {"command": relativize_command(code, root), "filepaths": paths})


def _nested(node: Any, depth: int) -> list[tuple[str, dict]]:
    """Вложенные вызовы на любой глубине: {name/recipient_name, arguments/parameters/...}."""
    found: list[tuple[str, dict]] = []
    if depth > MAX_DEPTH:
        return found
    if isinstance(node, dict):
        name = next((node[k] for k in _NAME_KEYS if isinstance(node.get(k), str)), None)
        args = next((a for a in (_parse_json_object(node.get(k)) for k in _ARGS_KEYS) if a is not None), None)
        if name and args is not None:
            found.append((name, args))
            return found  # содержимое args развернёт expand
        for value in node.values():
            found += _nested(value, depth + 1)
    elif isinstance(node, list):
        for item in node:
            found += _nested(item, depth + 1)
    elif isinstance(node, str) and node.lstrip().startswith("{"):
        parsed = _parse_json_object(node)
        if parsed is not None:
            found += _nested(parsed, depth + 1)
    return found


def expand(tool_name: str, tool_input: Any, root: Path = ROOT, depth: int = 0) -> list[Call]:
    """Один вызов клиента → плоские вызовы для guard (включая вложенные)."""
    name = re.sub(r"^functions\.", "", str(tool_name or ""))
    args = _parse_json_object(tool_input)
    if args is None:
        if isinstance(tool_input, str) and tool_input.strip():
            return expand(name, {"command": tool_input}, root, depth)
        return [Call(name, {})]
    if depth > MAX_DEPTH:
        return [_opaque_call(name, json.dumps(args, ensure_ascii=False), root)]

    calls: list[Call] = []
    rest = dict(args)

    # Вложенные вызовы (multi_tool_use.parallel, tool_calls, обёртки хоста)
    for nested_name, nested_args in _nested({k: v for k, v in args.items()}, 0):
        calls += expand(nested_name, nested_args, root, depth + 1)

    # Код, исполняемый инструментом
    # (только у инструментов-исполнителей; у редакторов `code` — новый текст файла, его проверит guard)
    code = next((args[k] for k in _CODE_KEYS if isinstance(args.get(k), str) and args[k].strip()), None)
    if code is not None and _CODE_TOOL.search(name):
        calls.append(_opaque_call(name, code, root))

    # Текст команды: Bash / exec_command / run_shell_command / apply_patch Codex
    command = None
    for key in _COMMAND_KEYS:
        text = _command_text(args.get(key))
        rest.pop(key, None)
        if text is not None and command is None:
            command = text
    if isinstance(rest.get("argv"), list):
        command = command or _command_text(rest.pop("argv"))

    patch_text = args.get("input") if isinstance(args.get("input"), str) else None
    if patch_text is None and isinstance(args.get("patch"), str):
        patch_text = args["patch"]
    is_patch_tool = name.lower() in ("apply_patch", "applypatch", "patch")
    if command is not None and (is_patch_tool or command.lstrip().startswith("*** Begin Patch")):
        patch_text, command = command, None
    elif command is not None and "*** Begin Patch" in command:
        # apply_patch через heredoc в shell: патч — отдельно, команда — без тела патча
        for block in _PATCH_BLOCK.findall(command):
            calls.append(_patch_calls(block, root))
        command = _PATCH_BLOCK.sub(" ", command)
    if patch_text is not None and "*** " in patch_text:
        calls.append(_patch_calls(patch_text, root))
        rest.pop("input", None)
        rest.pop("patch", None)

    if command is not None:
        rest["command"] = relativize_command(command, root)
    rest = _normalize_paths(rest, root)
    extra = _extra_paths(rest)
    if extra:
        known = rest.get("filepaths")
        known = known if isinstance(known, list) else [known] if isinstance(known, str) else []
        rest["filepaths"] = known + extra
    writes = {k.lower() for k in rest} & _WRITE_PAYLOAD_KEYS
    guard_name = name if not writes or re.search(r"write|edit|patch|replace|create", name, re.I) else f"{name}:write"
    if rest or not calls:
        calls.insert(0, Call(guard_name, rest))
    return calls


def detect_client(payload: dict, requested: str = "auto") -> str:
    if requested != "auto":
        return requested
    if payload.get("hook_event_name") in ("BeforeTool", "AfterTool"):
        return "gemini"
    return "claude"


def evaluate(payload: Any, guard: ModuleType, policy: Optional[dict] = None, okx_profiles=None,
             root: Path = ROOT) -> tuple[Optional[str], list[Call]]:
    """(причина отказа или None, плоские вызовы). Первый отказ побеждает."""
    payload = _parse_json_object(payload) or {}
    tool_name = next((payload[k] for k in ("tool_name", "toolName", "name") if isinstance(payload.get(k), str)), "")
    tool_input = next((payload[k] for k in ("tool_input", "toolArgs", "arguments", "args")
                       if payload.get(k) not in (None, "")), {})
    calls = expand(tool_name, tool_input, root)
    for call in calls:
        reason = guard.decide(call.tool_name, call.tool_input, policy=policy, okx_profiles=okx_profiles)
        if reason:
            return reason, calls
    return None, calls


def render_deny(client: str, reason: str) -> str:
    text = f"[guard] {reason}"
    if client == "gemini":
        # Gemini читает только верхнеуровневые decision/reason (hooks/reference.md, BeforeTool)
        return json.dumps({"decision": "deny", "reason": text})
    # Claude Code, Codex, Muse: формат Claude, причина обязана быть непустой
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": text}})


def _shape(calls: list[Call]) -> list[dict]:
    return [{"tool": c.tool_name, "keys": sorted(c.tool_input)} for c in calls]


def trace_enabled(root: Path = ROOT) -> bool:
    """Разведка формы вызовов: env AGENT_GUARD_TRACE или файл data/GUARD_TRACE (env клиенты могут чистить)."""
    return bool(os.environ.get("AGENT_GUARD_TRACE")) or (root / "data" / "GUARD_TRACE").exists()


# --- Проверка конфигов hooks клиентов (только чтение; для E2E и приёмки патча) ---

GEMINI_EVENTS = {"BeforeTool", "AfterTool", "BeforeAgent", "AfterAgent", "BeforeModel", "AfterModel",
                 "BeforeToolSelection", "SessionStart", "SessionEnd", "PreCompress", "Notification"}
_ALL_TOOLS = {None, "", "*", ".*"}


def lint_hook_config(client: str, config: Any) -> list[str]:
    """Проблемы конфига hooks клиента: пустой список — guard подключён через адаптер как нужно.

    gemini — `.gemini/settings.json`: событие BeforeTool, timeout в миллисекундах (hooks/reference.md).
    codex  — `.codex/hooks.json`: PreToolUse, timeout в секундах (learn.chatgpt.com/docs/hooks).
    muse   — `.muse/hooks.json`: PreToolUse; hook исполняет sh внутри WSL — без обратных слэшей;
             единица timeout не подтверждена, принимаем секунды как в формате Claude.
    """
    problems: list[str] = []
    hooks = config.get("hooks") if isinstance(config, dict) else None
    if not isinstance(hooks, dict):
        return ["нет раздела hooks"]
    event = "BeforeTool" if client == "gemini" else "PreToolUse"
    if client == "gemini":
        for name in hooks:
            if name not in GEMINI_EVENTS:
                problems.append(f"событие {name} Gemini не знает и пропускает (Invalid hook event name)")
    groups = hooks.get(event)
    if not isinstance(groups, list) or not groups:
        return problems + [f"нет события {event}"]
    covered = False
    for group in groups:
        if not isinstance(group, dict):
            problems.append("группа hooks не объект")
            continue
        all_tools = group.get("matcher") in _ALL_TOOLS
        for hook in group.get("hooks") or []:
            command = str(hook.get("command") or "")
            if "guard_adapter.py" not in command or f"--client {client}" not in command:
                problems.append(f"команда не вызывает адаптер с --client {client}: {command!r}")
                continue
            timeout = hook.get("timeout")
            if client == "gemini":
                if not isinstance(timeout, (int, float)) or not 5000 <= timeout <= 60000:
                    problems.append(f"timeout {timeout!r}: у Gemini миллисекунды, нужно 5000–60000")
            elif not isinstance(timeout, (int, float)) or not 5 <= timeout <= 120:
                problems.append(f"timeout {timeout!r}: ожидаются секунды 5–120")
            if client == "muse" and "\\" in command:
                problems.append("команда Muse исполняется sh в WSL: обратный слэш съедается (exit 127)")
            if all_tools:
                covered = True
    if not covered:
        problems.append(f"{event}: нет hook адаптера на все инструменты (matcher '*')")
    return problems


_MUSE_UNTRUSTED = re.compile(r"workspace is untrusted|project skills skipped because workspace is untrusted|"
                             r"Workspace trust: untrusted", re.I)


def muse_trust_status(stderr_text: str) -> str:
    """Доверие workspace по stderr `muse exec`: untrusted — project rules, skills и hooks не загружены."""
    return "untrusted" if _MUSE_UNTRUSTED.search(stderr_text or "") else "unknown"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Адаптер hook-вызовов клиентов к ops/hooks/guard.py")
    parser.add_argument("--client", choices=CLIENTS, default="auto")
    opts = parser.parse_args(argv)
    guard = None
    try:
        guard = load_guard()
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
        payload = payload if isinstance(payload, dict) else {}
        client = detect_client(payload, opts.client)
        reason, calls = evaluate(payload, guard)
    except Exception as exc:  # fail-open, как у guard: сломанный адаптер не останавливает работу
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "decision": "error", "adapter": opts.client,
                 "error": repr(exc)}
        if guard is not None:
            guard._log(entry)
        return 0

    now = datetime.now(timezone.utc).isoformat()
    if trace_enabled():
        guard._log({"ts": now, "decision": "trace", "client": client,
                    "event": payload.get("hook_event_name"), "calls": _shape(calls)})
    if reason:
        snippet = next((c.tool_input.get("command") or c.tool_input.get("filepaths") for c in calls
                        if c.tool_input.get("command") or c.tool_input.get("filepaths")), "")
        guard._log({"ts": now, "decision": "deny", "client": client, "tool": calls[0].tool_name if calls else "",
                    "reason": reason, "snippet": guard._redact(str(snippet))})
        sys.stdout.write(render_deny(client, reason))
    return 0


if __name__ == "__main__":
    sys.exit(main())
