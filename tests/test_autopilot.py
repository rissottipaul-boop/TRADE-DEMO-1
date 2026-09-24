"""Автопилот: доска → решение «продолжать ли работу»."""
import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("autopilot", ROOT / "ops" / "hooks" / "autopilot.py")
autopilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(autopilot)

BOARD = """
| ID | Задача | Агент | Статус | Зависит от | Критерий готовности | Заметки |
| --- | --- | --- | --- | --- | --- | --- |
| A1 | первая | Insight Executor | done | — | x | |
| A2 | вторая | Insight Executor | ready | A1 | x | |
| A3 | третья | Insight Executor | ready | A4 | x | зависит от незавершённой |
| A4 | четвёртая | Ops Sentinel | in-progress Ops Sentinel 02:10 | — | x | |
| S1 | проверка | Ops Sentinel | scheduled 2026-09-24T10:30+05:00 | — | x | |
| U1 | вопрос | Human | needs-user | — | x | |
"""
ON = {"enabled": True, "max_continuations": 2}


class BoardTest(unittest.TestCase):
    def test_parse(self):
        tasks = autopilot.parse_board(BOARD)
        self.assertEqual(tasks["A2"]["deps"], ["A1"])
        self.assertEqual(tasks["A4"]["status"], "in-progress")
        self.assertEqual(tasks["S1"]["arg"], "2026-09-24T10:30+05:00")

    def test_ready_respects_deps_and_schedule(self):
        tasks = autopilot.parse_board(BOARD)
        before = datetime(2026, 9, 24, 5, 0, tzinfo=timezone.utc)  # 10:00 +05
        after = datetime(2026, 9, 24, 6, 0, tzinfo=timezone.utc)  # 11:00 +05
        self.assertEqual(autopilot.ready_tasks(tasks, before), ["A2"])
        self.assertEqual(autopilot.ready_tasks(tasks, after), ["A2", "S1"])


class DecideTest(unittest.TestCase):
    def test_continues_until_limit(self):
        state = {}
        for expected in (True, True, False):
            reason, state = autopilot.decide({"session_id": "s"}, BOARD, ON, state)
            self.assertEqual(reason is not None, expected)
        # другая сессия — свой счётчик
        self.assertIsNotNone(autopilot.decide({"session_id": "other"}, BOARD, ON, state)[0])

    def test_disabled_or_nothing_ready(self):
        self.assertIsNone(autopilot.decide({}, BOARD, {"enabled": False}, {})[0])
        idle = "| ID | Задача | Агент | Статус |\n| --- | --- | --- | --- |\n| U1 | q | Human | needs-user |"
        self.assertIsNone(autopilot.decide({}, idle, ON, {})[0])


if __name__ == "__main__":
    unittest.main()
