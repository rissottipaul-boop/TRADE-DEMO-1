"""Локальный мост read-only инструментов для интерфейса Morphy; JSON через stdin."""
from __future__ import annotations

import json
import sys
from pathlib import Path

MAX_REQUEST_BYTES = 20_000


def dispatch(root: Path, payload: object) -> dict:
    from src.control_panel import _assistant_tool
    if not isinstance(payload, dict) or set(payload) != {"name", "arguments"}:
        raise ValueError("Нужны только поля name и arguments")
    if not isinstance(payload["name"], str) or not isinstance(payload["arguments"], dict):
        raise ValueError("Неверная схема инструмента")
    return {"ok": True, "result": _assistant_tool(root, payload["name"], payload["arguments"])}


def main() -> int:
    from src.ai_observability import redact
    try:
        data = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(data) > MAX_REQUEST_BYTES:
            raise ValueError("Запрос слишком большой")
        result = dispatch(Path(__file__).resolve().parent.parent, json.loads(data))
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        result = {"ok": False, "error": redact(str(exc))}
    except Exception:
        result = {"ok": False, "error": "Данные инструмента недоступны; повторите чтение состояния"}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
