"""Тесты карты ошибок OKX (src/errors.py).

Покрывает задачу ERR-50011: код 50011 должен давать осмысленное
действие (backoff), а не fallback.

Покрывает ERR-ACCT-SWITCH (коды смены режима аккаунта 51070, 59001,
59132-59139 — insights/okx-api.md §10 п.24) и ERR-BOT-CODES (коды
ботов 51055, 51057, 51313, 51340, 51343, 51344, 51348, 51349, 51370,
51380, 51381, 51398, 51399 — insights/futures-bots.md §1.7).
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

    def test_51070_first_switch_is_human_action(self):
        text = explain_error("51070")
        self.assertNotIn("код не в карте ошибок", text)
        self.assertIn("Web/App", text)
        self.assertIn("needs-user", text)

    def test_acct_switch_rejection_codes_not_fallback(self):
        codes = [
            "59001", "59132", "59133", "59134", "59135",
            "59136", "59137", "59138", "59139",
        ]
        for code in codes:
            with self.subTest(code=code):
                text = explain_error(code)
                self.assertIn(code, text)
                self.assertNotIn("код не в карте ошибок", text)

    def test_59132_mentions_precheck_for_incompatible_bots(self):
        text = explain_error("59132").lower()
        self.assertIn("precheck", text)
        self.assertIn("бот", text)

    def test_59136_59137_mention_leverage_preset(self):
        for code in ("59136", "59137"):
            with self.subTest(code=code):
                text = explain_error(code).lower()
                self.assertIn("плечо", text)
                self.assertIn("preset", text)

    def test_bot_codes_not_fallback(self):
        codes = [
            "51055", "51057", "51313", "51340", "51343", "51344",
            "51348", "51349", "51370", "51380", "51381", "51398", "51399",
        ]
        for code in codes:
            with self.subTest(code=code):
                text = explain_error(code)
                self.assertIn(code, text)
                self.assertNotIn("код не в карте ошибок", text)

    def test_51057_bot_requires_acct_lv_2_or_3(self):
        text = explain_error("51057")
        self.assertIn("acctLv 2 или 3", text)

    def test_51055_futures_grid_blocked_in_portfolio_margin(self):
        text = explain_error("51055").lower()
        self.assertIn("portfolio margin", text)
        self.assertIn("acctlv=4", text.replace(" ", ""))

    def test_sl_templates_51348_51344(self):
        self.assertIn("0.97", explain_error("51348"))
        self.assertIn("1.03", explain_error("51344"))

    def test_51381_profit_per_grid_level_threshold(self):
        text = explain_error("51381")
        self.assertIn("0.1%", text)

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
