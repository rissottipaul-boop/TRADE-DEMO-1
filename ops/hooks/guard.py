"""PreToolUse-guard для всех агентов проекта (Copilot в VS Code и Claude Code).

2026-09-24 (решение человека): периметр сужен. Guard больше НЕ блокирует
live-торговлю, ops/live-policy.json / ops/autopilot.json / pump-pocket.json,
ослабление лимитов src/risk.py, сброс kill-switch/breaker'ов и okx config/auth —
это теперь решают сами агенты (AGENTS.md §2).

Guard продолжает блокировать то, что необратимо и/или эксплуатируемо третьей
стороной (например, через prompt injection в контенте, который агенты читают
при исследованиях — веб, соцсети, скиллы):
- вывод средств (withdraw);
- чтение сырых секретов: .env, переменные окружения с ключами, ~/.okx/config.toml;
- массовое убийство процессов python (задевает движок и других агентов);
- правку своего же периметра: ops/hooks/, .github/hooks/, .claude/settings.json;
- прямые SQL-правки баз состояния и рекурсивное удаление ключевых каталогов/баз.

Вход — JSON на stdin: {"tool_name": ..., "tool_input": {...}}.
Выход — JSON с permissionDecision="deny" (понимают и VS Code, и Claude Code)
или пустой вывод (разрешено). Сбой самого guard — пропуск с записью в лог:
баг guard не должен останавливать всю работу агентов.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[2]
LIVE_POLICY = ROOT / "ops" / "live-policy.json"
GUARD_LOG = Path(os.environ.get("AGENT_GUARD_LOG") or ROOT / "data" / "guard.log")
OKX_CONFIG = Path.home() / ".okx" / "config.toml"

# Файлы-периметр: их меняет только человек
GUARDRAIL_FILES = {"ops/live-policy.json", "ops/autopilot.json", "pump-pocket.json", ".claude/settings.json"}
GUARDRAIL_DIRS = ("ops/hooks/", ".github/hooks/")
RISK_FILE = "src/risk.py"

# Лимиты риск-ядра: направление, в котором изменение ОСЛАБЛЯЕТ защиту
RISK_LIMITS_UP_IS_RISKIER = {
    "DEFAULT_RISK_PCT", "MAX_RISK_PCT", "MAX_PORTFOLIO_HEAT_PCT", "MAX_POSITION_PCT",
    "MAX_OPEN_POSITIONS", "DAILY_LOSS_LIMIT_PCT", "GLOBAL_DD_LIMIT_PCT",
    "MAX_ENTRIES_PER_DAY", "INST_LOSS_STREAK_BLOCK", "SYS_LOSS_STREAK_PAUSE",
}
RISK_LIMITS_DOWN_IS_RISKIER = {"INST_BLOCK_HOURS", "SYS_PAUSE_HOURS"}

WRITE_TOOL_HINTS = ("create", "edit", "replace", "insert", "write", "patch", "delete", "rename", "move")
MUTATING_CMD = re.compile(
    r"(\bset-content\b|\badd-content\b|\bout-file\b|\bsed\s+-i|\brm\b|\bdel\b|\berase\b|"
    r"\bremove-item\b|\bri\b|\bmv\b|\bmove\b|\bmove-item\b|\bcp\b|\bcopy\b|\bcopy-item\b|"
    r"\brename\b|\brename-item\b|\btee\b|\btruncate\b|\bunlink\b|\bnew-item\b|\bni\b|"
    r"\bwrite_text\b|\bopen\([^)]*['\"][wa])"
)
DELETE_CMD = re.compile(r"\b(rm|del|erase|remove-item|ri|rd|rmdir|unlink)\b")
RECURSIVE_FLAG = re.compile(r"(\s-[a-z]*r[a-z]*\b|-recurse\b|/s\b)")

DENY_COMMAND_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bwithdraw(?![-_]?income)"),
     "Вывод средств агентам запрещён всегда (и ключи проекта без права withdraw)."),
    (re.compile(r"src[./\\]ops(\.py)?\s+reset\b|reset_breaker|reset_kill"),
     "Сброс kill-switch/breaker делает только человек: `python -m src.ops reset ...`."),
    (re.compile(r"(?<![\w.])\.env(?![\w.-])"),
     "Файл .env с ключами агентам не читается и не меняется."),
    (re.compile(r"dotenv_values|os\.environ|printenv|\benv:\s*okx|\$okx_|%okx_|"
                r"okx_(demo_)?(api_key|secret|passphrase)"),
     "Вывод переменных окружения с ключами запрещён."),
    (re.compile(r"\.okx[/\\]config\.toml"),
     "~/.okx/config.toml содержит ключи — используйте `okx config show`."),
    (re.compile(r"\bokx(\.cmd|\.exe)?\s+(\S+\s+)*config\s+(init|set|add|remove|delete|use|default|edit)\b|"
                r"\bokx(\.cmd|\.exe)?\s+(\S+\s+)*auth\s+(login|logout)\b"),
     "Профили и авторизацию okx CLI настраивает только человек."),
    (re.compile(r"taskkill\b.*\bpython|stop-process\b.*-name\s+python|\bpkill\b.*python|"
                r"\bkillall\s+python|get-process\s+python\w*\s*\|\s*stop-process"),
     "Нельзя убивать все процессы python (движок, боты, чужие агенты). Останавливайте по PID "
     "или `ops/engine.ps1 stop`."),
]


def _norm_path(path: str) -> str:
    """Путь → относительный от корня проекта, прямые слэши, нижний регистр."""
    p = path.replace("\\", "/").strip().strip('"').lower()
    root = str(ROOT).replace("\\", "/").lower().rstrip("/") + "/"
    if p.startswith(root):
        p = p[len(root):]
    return p[2:] if p.startswith("./") else p


def _is_env_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return name == ".env" or (name.endswith(".env") and not name.endswith(".env.example"))


def _is_guardrail(rel: str) -> bool:
    return rel in GUARDRAIL_FILES or rel.startswith(GUARDRAIL_DIRS)


def load_live_policy(path: Path = LIVE_POLICY) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"live_enabled": False}


def live_allowed(policy: dict, now: Optional[datetime] = None) -> bool:
    if policy.get("live_enabled") is not True:
        return False
    until = policy.get("enabled_until")
    if not until:
        return True
    try:
        deadline = datetime.fromisoformat(str(until))
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) < deadline


def okx_demo_profiles(config: Path = OKX_CONFIG) -> tuple[Optional[str], set[str]]:
    """(default_profile, профили с demo = true) из ~/.okx/config.toml — без чтения ключей."""
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        return None, set()
    default = re.search(r'^\s*default_profile\s*=\s*"([^"]+)"', text, re.M)
    demo, current = set(), None
    for line in text.splitlines():
        header = re.match(r"\s*\[profiles\.([^\]]+)\]", line)
        if header:
            current = header.group(1).strip('"')
        elif current and re.match(r"\s*demo\s*=\s*true", line):
            demo.add(current)
    return (default.group(1) if default else None), demo


def is_live_command(cmd: str, okx_profiles: tuple[Optional[str], set[str]]) -> bool:
    """Команда уходит в live: OKX_MODE=live или okx CLI без demo-режима."""
    if re.search(r"okx_mode\W{0,4}live", cmd):
        return True
    if not re.search(r"(^|[\s;&|(\"'])okx(\.cmd|\.exe)?\s", cmd):
        return False
    if "--live" in cmd:
        return True
    if "--demo" in cmd:
        return False
    default, demo = okx_profiles
    profile = re.search(r"--profile[\s=]+[\"']?([\w.-]+)", cmd)
    name = profile.group(1) if profile else default
    return name is not None and name not in {p.lower() for p in demo}


def check_command(cmd: str, policy: dict, okx_profiles) -> Optional[str]:
    low = cmd.lower().replace("\\", "/")
    for pattern, reason in DENY_COMMAND_RULES:
        if pattern.search(low):
            return reason

    for protected in sorted(GUARDRAIL_FILES) + list(GUARDRAIL_DIRS):
        redirect = re.search(r">>?\s*[\"']?[^\s\"'|&;]*" + re.escape(protected), low)
        if protected in low and (redirect or MUTATING_CMD.search(low)):
            return f"{protected} — периметр безопасности, его меняет только человек."

    mutating = bool(MUTATING_CMD.search(low)) or bool(re.search(r">>?\s*[\"']?[\w./-]", low))
    if mutating:
        if RISK_FILE in low:
            return ("src/risk.py меняется только инструментами редактирования — там guard "
                    "проверяет, что лимиты не ослаблены.")
        if re.search(r"data/kill\b", low) and DELETE_CMD.search(low):
            return "Отменять запрос kill-switch (data/KILL) может только человек."

    if re.search(r"risk_state\.db|bot_state\.db|(?<![\w/])state\.db", low) and re.search(
            r"\b(update|delete|insert|drop|replace|alter|truncate)\b", low):
        return "Изменение баз состояния вручную (SQL) запрещено — состояние меняет только код."

    if DELETE_CMD.search(low):
        if re.search(r"\.db\b|\.db[\"'\s]", low + " "):
            return "Удаление баз состояния (.db) запрещено: потеря состояния = провал критерия Фазы 1."
        if RECURSIVE_FLAG.search(low) and re.search(
                r"(^|[\s\"'/])(data|\.venv|\.github|\.claude|\.agents|ops|insights|src)/?([\s\"']|$)|"
                r"(^|\s)(\.|\*|/|~|c:/?)(\s|$)", low):
            return "Рекурсивное удаление ключевых каталогов проекта запрещено."

    if is_live_command(low, okx_profiles) and not live_allowed(policy):
        return ("Live-торговля выключена политикой ops/live-policy.json (включает только человек). "
                "Используйте --demo / demo-профиль.")
    return None


def _collect_paths(tool_input: Any) -> list[str]:
    paths: list[str] = []

    def walk(node: Any, key: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for item in node:
                walk(item, key)
        elif isinstance(node, str) and key.lower() in (
                "filepath", "file_path", "path", "filepaths", "files", "notebook_path",
                "notebookpath", "uri", "newpath", "new_path", "oldpath", "old_path",
                "includepattern", "include_pattern", "glob"):
            paths.append(node)

    walk(tool_input)
    patch = tool_input.get("input") if isinstance(tool_input, dict) else None
    if isinstance(patch, str):
        paths += re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.M)
        paths += re.findall(r"^\*\*\* Move to: (.+)$", patch, re.M)
    return paths


def _new_text_fragments(tool_input: Any) -> list[str]:
    """Новый текст, который инструмент записывает в файл (для проверки лимитов)."""
    frags: list[str] = []

    def walk(node: Any, key: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for item in node:
                walk(item, key)
        elif isinstance(node, str) and key.lower() in ("newstring", "new_string", "content", "code", "text"):
            frags.append(node)

    walk(tool_input)
    patch = tool_input.get("input") if isinstance(tool_input, dict) else None
    if isinstance(patch, str):
        frags.append("\n".join(line[1:] for line in patch.splitlines() if line.startswith("+")))
    return frags


def _risk_constants(text: str) -> dict[str, float]:
    names = RISK_LIMITS_UP_IS_RISKIER | RISK_LIMITS_DOWN_IS_RISKIER
    found = {}
    for name, value in re.findall(r"^\s*([A-Z_]+)\s*(?::\s*\w+\s*)?=\s*([0-9.]+)", text, re.M):
        if name in names:
            try:
                found[name] = float(value)
            except ValueError:
                pass
    return found


_ALL_LIMITS = "|".join(sorted(RISK_LIMITS_UP_IS_RISKIER | RISK_LIMITS_DOWN_IS_RISKIER))
_LIMIT_ASSIGN = re.compile(rf"^\s*({_ALL_LIMITS})\s*(?::\s*\w+\s*)?=\s*(.+)$", re.M)
_LIMIT_MONKEYPATCH = re.compile(rf"\brisk\s*\.\s*({_ALL_LIMITS})\s*=(?!=)")


def check_limit_monkeypatch(tool_input: Any) -> Optional[str]:
    """Подмена лимита из другого модуля: risk.MAX_POSITION_PCT = 50."""
    for fragment in _new_text_fragments(tool_input):
        match = _LIMIT_MONKEYPATCH.search(fragment)
        if match:
            return f"Подмена лимита риска {match.group(1)} из кода запрещена — лимиты живут в src/risk.py."
    return None


def check_risk_limits(tool_input: Any, risk_path: Path) -> Optional[str]:
    try:
        current = _risk_constants(risk_path.read_text(encoding="utf-8"))
    except OSError:
        return None
    for fragment in _new_text_fragments(tool_input):
        for name, value in _LIMIT_ASSIGN.findall(fragment):
            if not re.match(r"[0-9.]+\s*(#.*)?$", value.strip()):
                return f"Лимит {name} должен оставаться числовой константой (получено: {value.strip()[:40]})."
        for name, new in _risk_constants(fragment).items():
            old = current.get(name)
            if old is None:
                continue
            if (name in RISK_LIMITS_UP_IS_RISKIER and new > old) or (
                    name in RISK_LIMITS_DOWN_IS_RISKIER and new < old):
                return (f"Ослабление лимита риска {name}: {old:g} → {new:g}. Ужесточать можно, "
                        f"ослаблять — только человек.")
    return None


def decide(tool_name: str, tool_input: Any, policy: Optional[dict] = None,
           okx_profiles=None, root: Path = ROOT) -> Optional[str]:
    """Причина запрета или None (разрешено)."""
    policy = load_live_policy() if policy is None else policy
    tool_input = tool_input if isinstance(tool_input, dict) else {}

    command = tool_input.get("command")
    if isinstance(command, str) and command.strip():
        profiles = okx_demo_profiles() if okx_profiles is None else okx_profiles
        reason = check_command(command, policy, profiles)
        if reason:
            return reason

    is_write = any(h in tool_name.lower() for h in WRITE_TOOL_HINTS)
    for raw in _collect_paths(tool_input):
        rel = _norm_path(raw)
        if _is_env_file(rel):
            return "Файл .env с ключами агентам не читается и не меняется."
        if rel.endswith(".okx/config.toml"):
            return "~/.okx/config.toml содержит ключи — используйте `okx config show`."
        if is_write and _is_guardrail(rel):
            return f"{rel} — периметр безопасности, его меняет только человек."
        if is_write and rel.endswith(".py"):
            reason = check_limit_monkeypatch(tool_input)
            if not reason and rel.endswith(RISK_FILE):
                reason = check_risk_limits(tool_input, root / RISK_FILE)
            if reason:
                return reason
    return None


def _log(entry: dict) -> None:
    try:
        GUARD_LOG.parent.mkdir(parents=True, exist_ok=True)
        with GUARD_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _redact(text: str) -> str:
    return re.sub(r"[A-Za-z0-9+/=_-]{24,}", "***", text)[:160]


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
        tool_name = str(payload.get("tool_name", ""))
        tool_input = payload.get("tool_input") or payload.get("toolArgs") or {}
        if isinstance(tool_input, str):
            tool_input = json.loads(tool_input)
        reason = decide(tool_name, tool_input)
    except Exception as exc:  # fail-open: сломанный guard не должен блокировать работу
        _log({"ts": datetime.now(timezone.utc).isoformat(), "decision": "error", "error": repr(exc)})
        return 0

    if reason:
        snippet = tool_input.get("command") if isinstance(tool_input, dict) else None
        _log({"ts": datetime.now(timezone.utc).isoformat(), "decision": "deny", "tool": tool_name,
              "reason": reason, "snippet": _redact(str(snippet or _collect_paths(tool_input)))})
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": f"[guard] {reason}"}}
        # ASCII-JSON: хост читает stdout как UTF-8, а Python на Windows пишет в cp1251
        sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
