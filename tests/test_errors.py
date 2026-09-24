"""Тесты карты ошибок OKX (src/errors.py).

Покрывает задачу ERR-50011: код 50011 должен давать осмысленное
действие (backoff), а не fallback.
"""
import unittest

from src.errors import ERROR_MAP, explain_error, extract_error_code


class TestExplainError(unittest.TestCase):
    def test_50011_returns_backoff_action_not_fallback(self):
        text = explain_error("50011")
        self.assertIn("50011", text)
        # Не fallback-формулировка
        self.assertNotIn("код не в карте ошибок", text)
        # Действие — экспоненциальный backoff с указанными параметрами
        self.assertIn("backoff", text.lower())
        self.assertIn("0.5", text)

    def test_50061_subaccount_order_limit(self):
        text = explain_error("50061")
        self.assertNotIn("код не в карте ошибок", text)
        self.assertIn("суб-аккаунт", text.lower())

    def test_50038_demo_unavailable_is_not_retried(self):
        text = explain_error("50038")
        self.assertNotIn("код не в карте ошибок", text)
        self.assertIn("demo", text.lower())
        self.assertIn("не ретраить", text.lower())

    def test_unknown_code_falls_back(self):
        text = explain_error("59999")
        self.assertIn("код не в карте ошибок", text)

    def test_empty_code(self):
        self.assertIn("не извлечён", explain_error(None))
        self.assertIn("не извлечён", explain_error(""))

    def test_all_entries_have_nonempty_action(self):
        for code, (description, action) in ERROR_MAP.items():
            with self.subTest(code=code):
                self.assertTrue(description.strip())
                self.assertTrue(action.strip())


class TestExtractErrorCode(unittest.TestCase):
    def test_code_from_json_payload(self):
        exc = Exception('okx {"code":"50011","data":[],"msg":"Too Many Requests"}')
        self.assertEqual(extract_error_code(exc), "50011")

    def test_scode_from_data_takes_priority(self):
        exc = Exception('okx {"code":"0","data":[{"sCode":"51020","sMsg":"min amount"}]}')
        self.assertEqual(extract_error_code(exc), "51020")

    def test_bare_code_in_text(self):
        exc = Exception("okx rate limit exceeded 50011")
        self.assertEqual(extract_error_code(exc), "50011")


if __name__ == "__main__":
    unittest.main()
