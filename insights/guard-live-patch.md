---
status: готов к применению
task: GUARD-LIVE-DRAFT
applies: GUARD-LIVE-PATCH (применяет человек)
date: 2026-09-30
---
# Патч guard под малый live

Проект патча для `ops/hooks/guard.py` и `tests/test_guard.py`. Агентам эти файлы править нельзя (AGENTS.md §2), применяет человек — задача GUARD-LIVE-PATCH, решение «да» от 25.09 03:23. Закрывает блокеры №4–5 [business-plan.md](business-plan.md) §3.

## Что меняется

| № | Критерий | Как сделано |
| --- | --- | --- |
| 1 | `ops/live-pocket.json` в периметре | Добавлен в `GUARDRAIL_FILES`: правка и удаление — только человек, чтение разрешено. `ops/live-pocket.example.json` не затронут |
| 2 | Аварийные live-команды — и вне окна | `check_live_command`: разрешены `src.ops kill/status` с `OKX_MODE=live`, `src.live_preflight`, `ops\live.ps1 status/stop`, а в `okx` CLI — отмена, остановка ботов, `close-position` и чтение (balance, orders, positions, bills…). Условие — одна команда без цепочек (`; & \| > <`, `$(`) и без слов входа (place, amend, create, transfer, leverage, `--side`, `--sz` …). Исключение `(?!_mode\b)` в правиле секретов: раньше оно запрещало любой `$env:OKX_MODE=…`, а значит и аварийный kill |
| 3 | `OKX_MODE=live` — только `src.live_*` | Префикс `OKX_MODE=live` принимается только в начале одной команды. Разрешены аварийные `src.ops kill/status` и `src.live_preflight` в любое время, `src.live_runner` — только в открытом окне. Движок, стратегии, `python -c` и прочее под `OKX_MODE=live` запрещены всегда. Запуск `src.live_runner` и `ops\live.ps1 start` вне окна запрещён и без `OKX_MODE` |
| 4 | `_norm_path` для всех форм пути | Добавлены Git Bash (`/c/...`), URI VS Code (`file:///c%3A/...`, `%20`), префикс `\\?\`, одинарные кавычки, `.\`, свёртка `./` и `../`. `_is_guardrail` ловит и сам каталог `ops/hooks` без слэша |

**Ужесточение сверх критерия.** Live-вход из `okx` CLI (`okx --live spot place …`) теперь запрещён и в открытом окне: входы в live идут только через `src.live_runner` и риск-ядро. Раньше guard их пропускал, и держалось это только на инструкциях агентов (блокер №6, AGENTS-LIVE-RULES). Поэтому в `test_live_window` проверка разрешённого live-входа заменена на `src.live_runner`. Если нужен прежний режим, верните в `check_live_command` окно для CLI: `return None if live_allowed(policy) else …` вместо последнего `return`.

**Ослабление, осознанное.** Чтение live через CLI (`okx --profile main-live account balance`) теперь разрешено и вне окна: Ops Sentinel нужен баланс live-кармана без открытого окна. Строка DeniedTest заменена на live-`place` того же профиля.

## Находка: обход периметра через форму пути

Текущий `_norm_path` не приводит формы `/c/TG/…` (Git Bash), `file:///c%3A/…` и `src/../ops/hooks/…` к относительному пути. Запись по такому пути в `pump-pocket.json`, `ops/live-pocket.json` или `ops/hooks/` guard сейчас не видит (`NormPathTest.test_guardrail_via_any_form` на текущем guard падает). `.env` защищён и так — проверка идёт по имени файла. Патч закрывает обход.

## Проверка

Проверено на копии в scratchpad: оригиналы только читались, `ops/hooks/` не менялся.

- Пропатченный guard, пропатченные тесты: **24/24 OK** — прежние 13 и 11 новых (`LiveEmergencyTest` 4, `NormPathTest` 4, изменённый `test_live_window`).
- Новые тесты против текущего guard: **29 падений** в подтестах `LiveEmergencyTest`, `NormPathTest`, `test_live_window`. Тесты проверяют именно изменения.
- Контракт хука (`HookContractTest`: deny-вывод, тишина при allow, fail-open, BOM) не изменился и зелёный.

## Как применить (человек)

1. Из корня проекта: `git apply insights/guard-live-patch.diff` — файл рядом, тот же diff, что ниже. Или вставить фрагменты вручную.
2. `.venv\Scripts\python.exe -m unittest tests.test_guard` — ожидается 24 OK.
3. `.venv\Scripts\python.exe -m unittest discover -s tests -t .` — всё зелёное.
4. Ответить в GUARD-LIVE-PATCH «применено». Оркестратор проверит критерий и закроет задачу.

Заодно по PERIMETER-CONFIRM: шапка `guard.py` (строки 3–6, «периметр сужен») противоречит коду. Патч её не трогает: это ваш текст, поправьте при применении.

## Diff

```diff
--- a/ops/hooks/guard.py
+++ b/ops/hooks/guard.py
@@ -21,11 +21,13 @@
 """
 import json
 import os
+import posixpath
 import re
 import sys
 from datetime import datetime, timezone
 from pathlib import Path
 from typing import Any, Optional
+from urllib.parse import unquote, urlparse
 
 ROOT = Path(__file__).resolve().parents[2]
 LIVE_POLICY = ROOT / "ops" / "live-policy.json"
@@ -33,7 +35,8 @@
 OKX_CONFIG = Path.home() / ".okx" / "config.toml"
 
 # Файлы-периметр: их меняет только человек
-GUARDRAIL_FILES = {"ops/live-policy.json", "ops/autopilot.json", "pump-pocket.json", ".claude/settings.json"}
+GUARDRAIL_FILES = {"ops/live-policy.json", "ops/live-pocket.json", "ops/autopilot.json", "pump-pocket.json",
+                   ".claude/settings.json"}
 GUARDRAIL_DIRS = ("ops/hooks/", ".github/hooks/")
 RISK_FILE = "src/risk.py"
 
@@ -64,7 +67,8 @@
      "Сброс kill-switch/breaker делает только человек: `python -m src.ops reset ...`."),
     (re.compile(r"(?<![\w.])\.env(?![\w.-])"),
      "Файл .env с ключами агентам не читается и не меняется."),
-    (re.compile(r"dotenv_values|os\.environ|printenv|\benv:\s*okx|\$okx_|%okx_|"
+    # $env:OKX_MODE — не секрет: без исключения аварийный kill с этим префиксом невозможен
+    (re.compile(r"dotenv_values|os\.environ|printenv|\benv:\s*okx(?!_mode\b)|\$okx_|%okx_|"
                 r"okx_(demo_)?(api_key|secret|passphrase)"),
      "Вывод переменных окружения с ключами запрещён."),
     (re.compile(r"\.okx[/\\]config\.toml"),
@@ -80,11 +84,30 @@
 
 
 def _norm_path(path: str) -> str:
-    """Путь → относительный от корня проекта, прямые слэши, нижний регистр."""
-    p = path.replace("\\", "/").strip().strip('"').lower()
+    """Путь → относительный от корня проекта, прямые слэши, нижний регистр.
+
+    Формы, которые передают инструменты: Windows (C:\\..., c:/...), Git Bash (/c/...),
+    URI VS Code (file:///c%3A/...), префикс \\\\?\\, кавычки, ./ и .\\, сегменты ./ и ../
+    """
+    p = path.strip().strip("\"'")
+    if p.lower().startswith("file:"):
+        p = unquote(urlparse(p).path)  # file:///c%3A/x → /c:/x
+        if re.match(r"^/[a-zA-Z]:", p):
+            p = p[1:]
+    p = p.replace("\\", "/")
+    if p.startswith("//?/"):
+        p = p[4:]
+    drive = re.match(r"^/([a-zA-Z])(/.*)?$", p)  # Git Bash: /c/TG → c:/TG
+    if drive:
+        p = f"{drive.group(1)}:{drive.group(2) or '/'}"
+    if p:
+        p = posixpath.normpath(p)  # ops/./hooks, src/../ops/hooks → ops/hooks
+    p = p.lower()
     root = str(ROOT).replace("\\", "/").lower().rstrip("/") + "/"
     if p.startswith(root):
         p = p[len(root):]
+    elif p == root.rstrip("/"):
+        p = "."
     return p[2:] if p.startswith("./") else p
 
 
@@ -94,7 +117,8 @@
 
 
 def _is_guardrail(rel: str) -> bool:
-    return rel in GUARDRAIL_FILES or rel.startswith(GUARDRAIL_DIRS)
+    # normpath срезает хвостовой слэш: сам каталог ops/hooks тоже периметр
+    return rel in GUARDRAIL_FILES or rel.startswith(GUARDRAIL_DIRS) or rel + "/" in GUARDRAIL_DIRS
 
 
 def load_live_policy(path: Path = LIVE_POLICY) -> dict:
@@ -152,6 +176,44 @@
     return name is not None and name not in {p.lower() for p in demo}
 
 
+# GUARD-LIVE-PATCH. Live-вызов проверяется целиком, без цепочек: иначе «безопасное» начало
+# команды протащит опасное продолжение. OKX_MODE=live — только префиксом в начале команды.
+_LIVE_PREFIX = re.compile(r"^\s*(\$env:okx_mode\s*=\s*[\"']?live[\"']?\s*;|(env\s+)?okx_mode=[\"']?live[\"']?)\s*")
+_CHAIN = re.compile(r"[;&|`\n\r<>]|\$\(")
+_PY_M = r"(\S*/)?(python(3)?(\.exe)?|py)\s+-m\s+"
+_OPS_EMERGENCY = re.compile(rf"^{_PY_M}src\.ops\s+(kill|status)\b")
+_LIVE_READONLY = re.compile(rf"^({_PY_M}src\.live_preflight\b|(\S*/)?ops/live\.ps1\s+(status|stop)\b)")
+_LIVE_START = re.compile(rf"{_PY_M}src\.live_runner\b|ops/live\.ps1\s+start\b")
+# okx CLI в live: разрешены отмена, остановка, закрытие и чтение — без единого слова входа
+_OKX_FIRST = re.compile(r"^(\S*/)?okx(\.cmd|\.exe)?\s")
+_OKX_SAFE = re.compile(r"\b(cancel[\w-]*|stop|close-position|balance|positions?|orders?|get|list|details|"
+                       r"history|fills|bills|ticker|status)\b")
+_OKX_RISKY = re.compile(r"\b(place|amend|create|transfer|withdraw\S*|borrow|repay|leverage|set-\S+|margin|"
+                        r"adjust\S*|subscribe|purchase|redeem|switch|config|auth)\b|--side\b|--sz\b")
+
+
+def check_live_command(low: str, policy: dict) -> Optional[str]:
+    """Причина запрета live-команды или None. Аварийные и читающие команды — и вне окна."""
+    prefixed = _LIVE_PREFIX.match(low)
+    rest = low[prefixed.end():] if prefixed else low
+    chained = bool(_CHAIN.search(rest))
+    if re.search(r"okx_mode\W{0,4}live", low):
+        if not prefixed or chained or re.search(r"okx_mode\W{0,4}live", rest):
+            return ("OKX_MODE=live — только префиксом одной команды без цепочек: "
+                    "`$env:OKX_MODE='live'; python -m src.ops kill \"причина\"`.")
+        if _OPS_EMERGENCY.match(rest) or _LIVE_READONLY.match(rest):
+            return None
+        if _LIVE_START.match(rest):
+            return None if live_allowed(policy) else (
+                "Live-окно ops/live-policy.json закрыто — live-runner не запускается.")
+        return ("OKX_MODE=live — только для точек входа src.live_* и аварийных src.ops kill/status. "
+                "Live-входы идут через src.live_runner (риск-ядро), не из CLI и не из других модулей.")
+    if _OKX_FIRST.match(rest) and not chained and _OKX_SAFE.search(rest) and not _OKX_RISKY.search(rest):
+        return None  # аварийное действие или чтение в live — разрешено всегда
+    return ("Live через okx CLI — только отмена, остановка ботов, закрытие позиций и чтение, по одной "
+            "команде. Входы в live — только src.live_runner через риск-ядро (business-plan.md §3).")
+
+
 def check_command(cmd: str, policy: dict, okx_profiles) -> Optional[str]:
     low = cmd.lower().replace("\\", "/")
     for pattern, reason in DENY_COMMAND_RULES:
@@ -183,9 +245,10 @@
                 r"(^|\s)(\.|\*|/|~|c:/?)(\s|$)", low):
             return "Рекурсивное удаление ключевых каталогов проекта запрещено."
 
-    if is_live_command(low, okx_profiles) and not live_allowed(policy):
-        return ("Live-торговля выключена политикой ops/live-policy.json (включает только человек). "
-                "Используйте --demo / demo-профиль.")
+    if _LIVE_START.search(low) and not live_allowed(policy):
+        return "Live-окно ops/live-policy.json закрыто — live-runner не запускается."
+    if is_live_command(low, okx_profiles):
+        return check_live_command(low, policy)
     return None
 
 
--- a/tests/test_guard.py
+++ b/tests/test_guard.py
@@ -71,7 +71,7 @@
             "echo $OKX_DEMO_SECRET",
             "type %USERPROFILE%\\.okx\\config.toml",
             "okx --live spot place --instId BTC-USDT --side buy --sz 1",
-            "okx --profile main-live account balance",
+            "okx --profile main-live spot place --instId BTC-USDT --side buy --sz 1",
             "$env:OKX_MODE='live'; python -m src.engine",
             "okx asset withdraw --ccy USDT --amt 100",
             "okx config init",
@@ -152,10 +152,14 @@
     def test_live_window(self):
         future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
         past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
+        runner = "python -m src.live_runner"
+        self.assertIsNone(term(runner, {"live_enabled": True, "enabled_until": future}))
+        self.assertIsNotNone(term(runner, {"live_enabled": True, "enabled_until": past}))
+        self.assertIsNotNone(term(runner, {"live_enabled": "yes"}))  # только буквальное true
+        self.assertIsNotNone(term(r"ops\live.ps1 start", {"live_enabled": True, "enabled_until": past}))
+        # вход из CLI в live запрещён и в открытом окне: входы — только через src.live_runner
         cmd = "okx --live spot place --instId BTC-USDT --side buy --sz 0.0001 --px 40000"
-        self.assertIsNone(term(cmd, {"live_enabled": True, "enabled_until": future}))
-        self.assertIsNotNone(term(cmd, {"live_enabled": True, "enabled_until": past}))
-        self.assertIsNotNone(term(cmd, {"live_enabled": "yes"}))  # только буквальное true
+        self.assertIsNotNone(term(cmd, {"live_enabled": True, "enabled_until": future}))
 
     def test_withdraw_denied_even_with_live(self):
         self.assertIsNotNone(term("okx --live asset withdraw --amt 1", {"live_enabled": True}))
@@ -163,6 +167,121 @@
     def test_default_live_profile_counts_as_live(self):
         self.assertIsNotNone(guard.decide("run_in_terminal", {"command": "okx spot place --sz 1"},
                                           policy=OFF, okx_profiles=("main", {"okx-demo"})))
+
+
+class LiveEmergencyTest(unittest.TestCase):
+    """GUARD-LIVE-PATCH: аварийные live-команды — всегда, OKX_MODE=live — только src.live_*."""
+
+    def test_emergency_allowed_outside_window(self):
+        for cmd in [
+            "$env:OKX_MODE='live'; python -m src.ops kill \"flash crash\"",
+            "$env:OKX_MODE = \"live\"; .venv\\Scripts\\python.exe -m src.ops status",
+            "OKX_MODE=live python -m src.ops kill manual",
+            "python -m src.ops kill --mode live \"x\"",
+            "$env:OKX_MODE='live'; python -m src.live_preflight",
+            r"$env:OKX_MODE='live'; ops\live.ps1 stop",
+            "okx --live spot cancel --instId BTC-USDT --ordId 1",
+            "okx --live spot algo cancel --instId BTC-USDT --algoId 1",
+            "okx --live bot grid stop --algoOrdType grid --algoId 1 --stopType 1",
+            "okx --live bot dca stop --algoId 1",
+            "okx --live swap close-position --instId BTC-USDT-SWAP --mgnMode cross",
+            "okx --live account balance",
+            "okx --profile main-live spot orders",
+        ]:
+            with self.subTest(cmd=cmd):
+                self.assertIsNone(term(cmd))
+
+    def test_okx_mode_live_only_for_live_entry_points(self):
+        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
+        window = {"live_enabled": True, "enabled_until": future}
+        for cmd in [
+            "$env:OKX_MODE='live'; python -m src.engine",
+            "$env:OKX_MODE='live'; python -m src.dca_bot",
+            "OKX_MODE=live python -m src.smoke_test",
+            "$env:OKX_MODE='live'; python -c \"import src.connector\"",
+            "cd src; $env:OKX_MODE='live'; python -m src.ops kill x",  # префикс не в начале
+        ]:
+            with self.subTest(cmd=cmd):
+                self.assertIsNotNone(term(cmd, window))
+        self.assertIsNone(term("$env:OKX_MODE='live'; python -m src.live_runner", window))
+        self.assertIsNotNone(term("$env:OKX_MODE='live'; python -m src.live_runner"))  # окно закрыто
+
+    def test_emergency_prefix_cannot_carry_payload(self):
+        for cmd in [
+            "$env:OKX_MODE='live'; python -m src.ops kill x; python -m src.engine",
+            "$env:OKX_MODE='live'; python -m src.ops status && python -m src.dca_bot",
+            "okx --live spot cancel --ordId 1; okx --live spot place --side buy --sz 1",
+            "okx --live spot orders | okx --live spot place --sz 1",
+            "okx --live spot amend --ordId 1 --newSz 5",
+            "okx --live bot grid create --algoOrdType grid",
+            "okx --live account set-leverage --lever 10",
+            "okx --live asset transfer --amt 1",
+            "okx --live spot place --instId BTC-USDT --side buy --sz 1  # cancel",
+            "echo $env:OKX_API_KEY",  # исключение для OKX_MODE не открывает секреты
+            "Get-ChildItem env:OKX_DEMO_SECRET",
+            "$env:OKX_MODE='live'; echo $env:OKX_SECRET",
+        ]:
+            with self.subTest(cmd=cmd):
+                self.assertIsNotNone(term(cmd))
+
+    def test_live_pocket_is_guardrail(self):
+        self.assertIsNotNone(edit("create_file", filePath="ops/live-pocket.json", content="{}"))
+        self.assertIsNotNone(term("Set-Content ops/live-pocket.json '{}'"))
+        self.assertIsNone(edit("read_file", filePath="ops/live-pocket.json"))
+        self.assertIsNone(edit("create_file", filePath="ops/live-pocket.example.json", content="{}"))
+
+
+class NormPathTest(unittest.TestCase):
+    """_norm_path: все формы пути от инструментов VS Code и Claude Code."""
+
+    def test_forms(self):
+        root_bs = str(ROOT)
+        root_fs = root_bs.replace("\\", "/")
+        drive, rest = root_fs[0].lower(), root_fs[2:]
+        rest_url = rest.replace(" ", "%20")
+        target = "ops/hooks/guard.py"
+        for raw in [
+            target,
+            "./" + target,
+            ".\\ops\\hooks\\guard.py",
+            "OPS/Hooks/Guard.py",
+            '"ops/hooks/guard.py"',
+            "'ops/hooks/guard.py'",
+            "  ops/hooks/guard.py  ",
+            root_bs + "\\ops\\hooks\\guard.py",
+            root_fs + "/ops/hooks/guard.py",
+            root_fs.upper() + "/OPS/HOOKS/GUARD.PY",
+            drive + ":" + rest + "/ops/hooks/guard.py",
+            "/" + drive + rest + "/ops/hooks/guard.py",  # Git Bash
+            "file:///" + drive + "%3A" + rest_url + "/ops/hooks/guard.py",  # VS Code URI
+            "file:///" + drive.upper() + ":" + rest_url + "/ops/hooks/guard.py",
+            "\\\\?\\" + root_bs + "\\ops\\hooks\\guard.py",
+            "ops/./hooks/guard.py",
+            "src/../ops/hooks/guard.py",
+            root_fs + "/src/../ops/hooks/guard.py",
+        ]:
+            with self.subTest(raw=raw):
+                self.assertEqual(guard._norm_path(raw), target)
+
+    def test_guardrail_via_any_form(self):
+        root_fs = str(ROOT).replace("\\", "/")
+        for raw in ["/" + root_fs[0].lower() + root_fs[2:] + "/pump-pocket.json",
+                    "file:///" + root_fs[0].lower() + "%3A" + root_fs[2:].replace(" ", "%20") + "/ops/live-pocket.json",
+                    "src/../ops/hooks", "ops/hooks/", ".github/hooks"]:
+            with self.subTest(raw=raw):
+                self.assertIsNotNone(edit("create_file", filePath=raw, content="x"))
+
+    def test_env_via_any_form(self):
+        root_fs = str(ROOT).replace("\\", "/")
+        for raw in ["/" + root_fs[0].lower() + root_fs[2:] + "/.env", "src/../.env",
+                    "file:///" + root_fs[0].lower() + "%3A" + root_fs[2:].replace(" ", "%20") + "/.env"]:
+            with self.subTest(raw=raw):
+                self.assertIsNotNone(edit("Read", file_path=raw))
+
+    def test_outside_paths_stay_outside(self):
+        self.assertEqual(guard._norm_path("C:/Other/ops/hooks/guard.py"), "c:/other/ops/hooks/guard.py")
+        self.assertIsNone(edit("create_file", filePath="C:/Other/ops/hooks/guard.py", content="x"))
+        self.assertEqual(guard._norm_path(str(ROOT)), ".")
 
 
 class HookContractTest(unittest.TestCase):
```
