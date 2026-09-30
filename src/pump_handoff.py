"""Передача свежего кандидата автоматического скана в задачу PUMP-SCAN (PUMP-SCHED-HANDOFF).

    python -m src.pump_handoff              # последняя строка scan → PUMP-SCAN ready на доске
    python -m src.pump_handoff --dry-run    # только решение: доску не менять
    python -m src.pump_handoff --json       # решение JSON

Зачем. Планировщик Windows (PUMP-SCHED) в hh:02 запускает ops/pump_scan.ps1 → src.pump_scanner,
и строка scan ложится в data/pump_journal.jsonl. Исполняет кандидата только Pump Risk Taker
(LLM, вход со стопом по pump-pocket.json), а его цикл PUMP-SCAN идёт по своему расписанию:
30.09 кандидат ZRO-USDT (сканы 04:17 и 06:02) к приходу агента уже был stale по PUMP-STALE.
Модуль сразу после скана (его вызывает ops/pump_scan.ps1 run) переводит PUMP-SCAN в ready
с пометкой пары — автопилот оркестратора (Stop-хук ops/hooks/autopilot.py) видит готовую
задачу и отдаёт её Pump Risk Taker.

Ордеров модуль не ставит, биржу не читает, карман и лимиты не трогает: только чтение журнала
и одна ячейка статуса + заметка одной строки доски. Вход, размер и стоп по-прежнему решает
Pump Risk Taker и заново проверяет свежесть (PUMP-STALE) на момент входа.

Правило передачи (последняя строка event="scan", source="src.pump_scanner"):
  * feed == "live" (demo-лента — не сигнал, pump-feed.md §4 п. 4);
  * counts.candidates > 0;
  * у кандидата freshness.ok не false, и на момент передачи от закрытия сигнальной свечи
    (candle_ts + 1 ч) прошло не больше freshness.max_age_min строки (по умолчанию 15 мин —
    окно PUMP-STALE). Позже — передавать нечего: вход всё равно запрещён.
Доска (ops/board.md, строка PUMP-SCAN):
  * scheduled → ready; ready остаётся ready — в обоих случаях в начало заметок пишется
    «**Передача ЧЧ:ММ (src.pump_handoff, скан <ts>): …**», прежняя пометка передачи заменяется;
  * in-progress, blocked, needs-user, done — не трогаются (агент уже работает или решает
    человек): только вывод в лог;
  * пометка со временем того же скана уже есть — повтор ничего не меняет (идемпотентно).
Правка — точечная: меняются только ячейки «Статус» и «Заметки» одной строки, остальное
байт-в-байт. Перед записью файл перечитывается; если его изменили параллельно — решение
пересчитывается заново (до 3 попыток). Запись — временный файл + os.replace.

Коды выхода: 0 — решение принято (передано, передавать нечего или статус не позволяет);
2 — журнал или доска не читаются, строка PUMP-SCAN не найдена или не разбирается, запись не
удалась.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

SOURCE = "src.pump_handoff"
SCANNER = "src.pump_scanner"
PROJECT_TZ = timezone(timedelta(hours=5))    # время проекта (ops/board.md)
ROOT = Path(__file__).resolve().parents[1]
JOURNAL_PATH = ROOT / "data" / "pump_journal.jsonl"
BOARD_PATH = ROOT / "ops" / "board.md"
TASK_ID = "PUMP-SCAN"
BAR_MS = 3_600_000
DEFAULT_MAX_AGE_MIN = 15.0                   # окно PUMP-STALE (src.pump_scanner)
TAKE_FROM = ("scheduled", "ready")           # статусы, из которых задача становится ready
MARK_RE = re.compile(r"\*\*Передача [^*]*\(src\.pump_handoff[^*]*\*\*\s*")
WRITE_ATTEMPTS = 3


class HandoffError(Exception):
    """Журнал или доска не читаются, строка задачи не разбирается."""


@dataclass
class Decision:
    handoff: bool
    reason: str
    scan_ts: Optional[str] = None
    pairs: list = field(default_factory=list)      # [{inst_id, age_min, deadline, …}]
    deadline: Optional[str] = None                 # ЧЧ:ММ — конец окна свежести первого кандидата


# --- Журнал ---

def last_scan(path: Path) -> Optional[dict]:
    """Последняя строка event="scan" детерминированного сканера. Битые строки пропускаются."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HandoffError(f"журнал {path}: {exc}") from exc
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("event") == "scan" and row.get("source") == SCANNER:
            return row
    return None


def _parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def _hhmm(dt: datetime) -> str:
    return dt.astimezone(PROJECT_TZ).strftime("%H:%M")


def decide(scan: Optional[dict], now: datetime) -> Decision:
    """Есть ли в строке scan свежий кандидат на момент now."""
    if scan is None:
        return Decision(False, "в журнале нет строки scan")
    ts = scan.get("ts")
    if scan.get("feed") != "live":
        return Decision(False, f"скан {ts}: лента {scan.get('feed')!r}, не live", ts)
    counts = scan.get("counts") or {}
    cands = scan.get("candidates") or []
    if not counts.get("candidates") or not cands:
        return Decision(False, f"скан {ts}: кандидатов 0", ts)
    fr = scan.get("freshness") or {}
    window = float(fr.get("max_age_min") or DEFAULT_MAX_AGE_MIN)
    try:
        close_at = _parse_utc(scan["candle_ts"]) + timedelta(milliseconds=BAR_MS)
    except (KeyError, TypeError, ValueError) as exc:
        raise HandoffError(f"скан {ts}: нет или неверен candle_ts: {exc}") from exc
    age_min = (now - close_at).total_seconds() / 60
    deadline = close_at + timedelta(minutes=window)
    fresh, rejected = [], []
    for c in cands:
        cf = c.get("freshness")
        if isinstance(cf, dict) and cf.get("ok") is False:
            rejected.append(f"{c.get('inst_id')} — freshness.ok=false")
            continue
        if age_min > window:
            rejected.append(f"{c.get('inst_id')} — {age_min:.0f} мин после закрытия свечи "
                            f"> {window:g} мин")
            continue
        fresh.append({"inst_id": c.get("inst_id"), "age_min": round(age_min, 1),
                      "deadline": _hhmm(deadline), "impulse_pct": c.get("impulse_pct"),
                      "vol_ratio_median": c.get("vol_ratio_median"), "rsi": c.get("rsi"),
                      "close": c.get("close")})
    if not fresh:
        return Decision(False, f"скан {ts}: кандидаты устарели: " + "; ".join(rejected), ts)
    return Decision(True, f"скан {ts}: свежих кандидатов {len(fresh)}", ts, fresh,
                    _hhmm(deadline))


# --- Доска ---

def _cells(line: str) -> list[str]:
    """Сырые сегменты строки таблицы: parts[0] и parts[-1] — снаружи крайних «|»."""
    return line.split("|")


def find_row(lines: list[str], task_id: str) -> tuple[int, dict[str, int]]:
    """(индекс строки задачи, колонки заголовка её таблицы {имя: индекс ячейки})."""
    columns: Optional[dict[str, int]] = None
    found: list[tuple[int, dict[str, int]]] = []
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line.startswith("|"):
            columns = None if not line else columns
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        lowered = [c.lower() for c in cells]
        if "id" in lowered and "статус" in lowered:
            columns = {name: lowered.index(name) for name in lowered}
            continue
        if columns is None or cells[columns["id"]].strip("`* ") != task_id:
            continue
        if "заметки" not in columns or len(cells) != len(columns):
            raise HandoffError(f"строка {i + 1} {task_id}: ячеек {len(cells)}, в заголовке "
                               f"{len(columns)} — не разбирается, доску не трогаю")
        found.append((i, columns))
    if not found:
        raise HandoffError(f"на доске нет строки {task_id}")
    if len(found) > 1:
        raise HandoffError(f"строк {task_id} на доске {len(found)} (строки "
                           f"{', '.join(str(i + 1) for i, _ in found)}) — не угадываю, доску "
                           f"не трогаю")
    return found[0]


def _clean(text: str) -> str:
    """В ячейке таблицы нельзя «|» и перевод строки: разбор доски режет по «|»."""
    return re.sub(r"\s+", " ", text.replace("|", "/")).strip()


def _fmt_pair(p: dict) -> str:
    parts = []
    if p.get("impulse_pct") is not None:
        parts.append(f"{p['impulse_pct']:+.2f}%")
    if p.get("vol_ratio_median") is not None:
        parts.append(f"объём {p['vol_ratio_median']:.2f}×")
    if p.get("rsi") is not None:
        parts.append(f"RSI {p['rsi']:.1f}")
    return f"{p['inst_id']} ({', '.join(parts)})" if parts else str(p["inst_id"])


def handoff_note(dec: Decision, now: datetime, old_status: str) -> str:
    pairs = ", ".join(_fmt_pair(p) for p in dec.pairs)
    was = f"; было «{old_status}»" if not old_status.lower().startswith("ready") else ""
    return _clean(
        f"**Передача {_hhmm(now)} (src.pump_handoff, скан {dec.scan_ts}): свежий кандидат "
        f"{pairs}; окно свежести PUMP-STALE до {dec.deadline}, после — вход запрещён. "
        f"Проверить свежесть заново и решить вход по pump-pocket.json; после цикла вернуть "
        f"scheduled{was}.**")


def apply_board(text: str, dec: Decision, now: datetime,
                task_id: str = TASK_ID) -> tuple[Optional[str], str]:
    """(новый текст доски или None — без изменений, итог). Меняются только ячейки
    «Статус» и «Заметки» строки task_id."""
    lines = text.split("\n")
    idx, columns = find_row(lines, task_id)
    parts = _cells(lines[idx])
    s_i, n_i = columns["статус"] + 1, columns["заметки"] + 1
    status = parts[s_i].strip()
    word = status.strip("` ").split(maxsplit=1)[0].lower() if status.strip("` ") else ""
    notes = parts[n_i].strip()
    if f"скан {dec.scan_ts})" in notes:
        return None, f"{task_id}: передача скана {dec.scan_ts} уже записана — без изменений"
    if word not in TAKE_FROM:
        return None, (f"{task_id}: статус «{status}» — не трогаю (агент уже работает или "
                      f"решает человек); кандидаты: {', '.join(p['inst_id'] for p in dec.pairs)}")
    note = handoff_note(dec, now, status)
    rest = MARK_RE.sub("", notes).strip()
    parts[s_i] = " ready "
    parts[n_i] = f" {note} {rest} " if rest else f" {note} "
    lines[idx] = "|".join(parts)
    action = "статус ready" if word != "ready" else "уже ready, добавлена пометка"
    return "\n".join(lines), f"{task_id}: {action} — {', '.join(p['inst_id'] for p in dec.pairs)}"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _replace(tmp: str, path: Path) -> None:
    """os.replace с повтором: на Windows файл бывает коротко занят редактором."""
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.2)


def write_board(path: Path, dec: Decision, now: datetime, task_id: str = TASK_ID,
                dry_run: bool = False) -> tuple[bool, str]:
    """(изменена ли доска, итог). Сравнение-и-замена: доска, изменённая между чтением
    и записью, перечитывается, решение по строке пересчитывается."""
    path = Path(path)
    for _ in range(WRITE_ATTEMPTS):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise HandoffError(f"доска {path}: {exc}") from exc
        new_text, summary = apply_board(raw.decode("utf-8"), dec, now, task_id)
        if new_text is None:
            return False, summary
        if dry_run:
            return False, "(сухой прогон, доска не изменена) " + summary
        fd, tmp = tempfile.mkstemp(prefix=".board-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(new_text.encode("utf-8"))
            if _sha(path.read_bytes()) != _sha(raw):
                continue                          # доску правили параллельно — заново
            _replace(tmp, path)
            return True, summary
        except OSError as exc:
            raise HandoffError(f"доска {path}: запись не удалась: {exc}") from exc
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    raise HandoffError(f"доска {path}: меняется параллельно, {WRITE_ATTEMPTS} попытки — не записал")


# --- CLI ---

def run(journal: Path, board: Path, now: datetime, task_id: str = TASK_ID,
        dry_run: bool = False) -> dict:
    dec = decide(last_scan(journal), now)
    result = {"source": SOURCE, "ts": now.astimezone(PROJECT_TZ).isoformat(timespec="seconds"),
              "task": task_id, "handoff": dec.handoff, "reason": dec.reason,
              "scan_ts": dec.scan_ts, "pairs": dec.pairs, "board_changed": False,
              "board": None, "dry_run": dry_run}
    if dec.handoff:
        result["board_changed"], result["board"] = write_board(board, dec, now, task_id, dry_run)
    return result


def render(result: dict) -> str:
    head = "передача: " + ("да" if result["handoff"] else "нет") + f" — {result['reason']}"
    return head + (f"\nдоска: {result['board']}" if result["board"] else "")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.pump_handoff",
                                description="Свежий кандидат скана → PUMP-SCAN ready на доске")
    p.add_argument("--journal", type=Path, default=JOURNAL_PATH)
    p.add_argument("--board", type=Path, default=BOARD_PATH)
    p.add_argument("--task", default=TASK_ID)
    p.add_argument("--now", type=datetime.fromisoformat, default=None,
                   help="момент решения, ISO с поясом (тесты и разбор)")
    p.add_argument("--dry-run", action="store_true", help="решение без правки доски")
    p.add_argument("--json", action="store_true", help="итог JSON")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    now = args.now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=PROJECT_TZ)
    try:
        result = run(args.journal, args.board, now, args.task, args.dry_run)
    except HandoffError as exc:
        print(f"Передача не выполнена: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False) if args.json else render(result))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
