"""Граница локального stdin-моста Morphy без аккаунтов и внешних запросов."""
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from src import ai_tools_cli


class ToolsBridgeTests(unittest.TestCase):
    def test_unknown_fields_are_rejected_before_dispatch(self):
        with patch('src.control_panel._assistant_tool') as tool:
            for value in ([], {'name': 'ai.usage'}, {'name': 'ai.usage', 'arguments': {}, 'shell': 'okx'},
                          {'name': 7, 'arguments': {}}):
                with self.assertRaises(ValueError):
                    ai_tools_cli.dispatch(Path('.'), value)
            tool.assert_not_called()

    def test_dispatch_uses_shared_validation_and_not_shell(self):
        with patch('src.control_panel._assistant_tool', return_value={'quality': 'unknown'}) as tool:
            result = ai_tools_cli.dispatch(Path('.'), {'name': 'ai.usage', 'arguments': {}})
            self.assertEqual(result, {'ok': True, 'result': {'quality': 'unknown'}})
            tool.assert_called_once_with(Path('.'), 'ai.usage', {})

    def test_rejected_tool_name_and_arguments_are_not_logged(self):
        from tempfile import TemporaryDirectory
        from src.ai_observability import read_actions
        with TemporaryDirectory() as root:
            secret = 'sk-review-canary-secret123456'
            with self.assertRaises(ValueError):
                ai_tools_cli.dispatch(Path(root), {'name': secret, 'arguments': {'token': secret}})
            events = read_actions(Path(root))
            self.assertEqual(events[0]['tool'], 'tool.rejected')
            self.assertEqual(events[0]['outcome'], 'rejected')
            self.assertNotIn(secret, json.dumps(events))

    def test_stdin_limit_refuses_oversized_input(self):
        input_ = io.TextIOWrapper(io.BytesIO(b' ' * (ai_tools_cli.MAX_REQUEST_BYTES + 1)), encoding='utf8')
        output = io.StringIO()
        with patch('sys.stdin', input_), patch('sys.stdout', output), patch('src.control_panel._assistant_tool') as tool:
            self.assertEqual(ai_tools_cli.main(), 2)
            tool.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())['ok'])

    def test_malformed_stdin_and_unexpected_error_do_not_expose_raw_data(self):
        for raw in (b'{', b'{"name":"ai.usage","arguments":{}}'):
            input_ = io.TextIOWrapper(io.BytesIO(raw), encoding='utf8')
            output = io.StringIO()
            with patch('sys.stdin', input_), patch('sys.stdout', output), \
                 patch('src.control_panel._assistant_tool', side_effect=RuntimeError('private_key=TOPSECRET')):
                self.assertEqual(ai_tools_cli.main(), 2)
            self.assertNotIn('TOPSECRET', output.getvalue())


if __name__ == '__main__':
    unittest.main()
