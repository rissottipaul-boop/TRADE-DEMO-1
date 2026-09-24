"""Стакан и WS-клиент: checksum по исходным строкам, цепочка seqId, action верхнего уровня."""
import asyncio
import base64
import hashlib
import hmac
import json
import unittest
import zlib

from src.ws_client import (
    OKXWebSocket,
    OrderBook,
    WSConfig,
    WSCredentials,
    login_args,
    okx_checksum,
    ws_urls,
)


def signed_crc(s: str) -> int:
    v = zlib.crc32(s.encode())
    return v - 2**32 if v > 2**31 - 1 else v


class ChecksumTest(unittest.TestCase):
    def test_format_from_okx_docs(self):
        # Пример из документации OKX: bid/ask чередуются, каждый уровень "px:sz"
        bids = [("3366.1", "7"), ("3366", "6")]
        asks = [("3366.8", "9"), ("3368", "8")]
        self.assertEqual(okx_checksum(bids, asks), signed_crc("3366.1:7:3366.8:9:3366:6:3368:8"))

    def test_uneven_sides(self):
        bids = [("10", "1"), ("9", "2"), ("8", "3")]
        asks = [("11", "1")]
        self.assertEqual(okx_checksum(bids, asks), signed_crc("10:1:11:1:9:2:8:3"))

    def test_original_strings_matter(self):
        # раньше строки пересобирались из float: "0.10" → "0.1", 1e-05 → "1e-05"
        self.assertNotEqual(okx_checksum([("1.10", "2")], []), okx_checksum([("1.1", "2")], []))

    def test_book_verifies_checksum_with_original_strings(self):
        bids = [["100.10", "0.500", "0", "1"]]
        asks = [["100.20", "0.00001", "0", "1"]]
        good = okx_checksum([("100.10", "0.500")], [("100.20", "0.00001")])
        self.assertTrue(OrderBook().apply_snapshot(bids, asks, 1, good))
        self.assertFalse(OrderBook().apply_snapshot(bids, asks, 1, good + 1))


class SequenceTest(unittest.TestCase):
    def setUp(self):
        self.book = OrderBook()
        self.book.apply_snapshot([["100", "1", "0", "1"]], [["101", "1", "0", "1"]], seq_id=10)

    def test_update_in_order(self):
        self.assertTrue(self.book.apply_update([["100", "2", "0", "1"]], [["101", "0", "0", "0"]], 11, 10))
        self.assertEqual(self.book.bids[100.0], ("100", "2"))
        self.assertNotIn(101.0, self.book.asks)  # sz "0" удаляет уровень

    def test_gap_invalidates_book(self):
        self.assertFalse(self.book.apply_update([], [], 13, 12))
        self.assertFalse(self.book.valid)
        # после разрыва дельты не применяются до нового snapshot
        self.assertFalse(self.book.apply_update([["99", "1", "0", "1"]], [], 14, 13))
        self.assertNotIn(99.0, self.book.bids)

    def test_heartbeat_without_changes(self):
        self.assertTrue(self.book.apply_update([], [], 10, 10))

    def test_sequence_reset_after_maintenance(self):
        # seqId может сброситься на меньший — цепочка по prevSeqId при этом цела
        self.assertTrue(self.book.apply_update([["100", "3", "0", "1"]], [], 5, 10))
        self.assertEqual(self.book.seq_id, 5)

    def test_update_before_snapshot_rejected(self):
        self.assertFalse(OrderBook().apply_update([["1", "1", "0", "1"]], [], 2, 1))


class LoginTest(unittest.TestCase):
    def test_sign_prehash(self):
        creds = WSCredentials("key", "secret", "pass")
        args = login_args(creds, ts="1538054050")
        expected = base64.b64encode(
            hmac.new(b"secret", b"1538054050GET/users/self/verify", hashlib.sha256).digest()
        ).decode()
        self.assertEqual(args, {"apiKey": "key", "passphrase": "pass", "timestamp": "1538054050", "sign": expected})

    def test_credentials_not_in_repr(self):
        self.assertNotIn("secret", repr(WSCredentials("key", "secret", "pass")))


class UrlsTest(unittest.TestCase):
    def test_global(self):
        self.assertEqual(ws_urls("www.okx.com", demo=True).public, "wss://wspap.okx.com:8443/ws/v5/public")
        self.assertEqual(ws_urls("www.okx.com", demo=False).private, "wss://ws.okx.com:8443/ws/v5/private")


class BookMessageTest(unittest.IsolatedAsyncioTestCase):
    """Разбор сообщений books: action на верхнем уровне (проверено на живом API)."""

    async def asyncSetUp(self):
        self.ws = OKXWebSocket(WSConfig(url="wss://test"), on_message=lambda d: None)
        self.sent = []

        async def fake_send(op, args):
            self.sent.append((op, args))

        self.ws._send_op = fake_send

    def msg(self, action, bids, asks, seq, prev):
        return json.dumps({
            "arg": {"channel": "books", "instId": "BTC-USDT"},
            "action": action,
            "data": [{"bids": bids, "asks": asks, "seqId": seq, "prevSeqId": prev, "checksum": 0, "ts": "1"}],
        })

    async def test_small_snapshot_recognised_by_action(self):
        # старый код искал action внутри data[] и считал snapshot «по 100+ уровням»
        await self.ws._handle_message(self.msg("snapshot", [["100", "1", "0", "1"]], [["101", "1", "0", "1"]], 5, -1))
        book = self.ws.get_book("BTC-USDT")
        self.assertIsNotNone(book)
        self.assertEqual(book.best_bid(), 100.0)

    async def test_gap_triggers_resync(self):
        await self.ws._handle_message(self.msg("snapshot", [["100", "1", "0", "1"]], [["101", "1", "0", "1"]], 5, -1))
        await self.ws._handle_message(self.msg("update", [["100", "2", "0", "1"]], [], 9, 7))
        self.assertIsNone(self.ws.get_book("BTC-USDT"))  # невалидный стакан наружу не отдаётся
        await asyncio.sleep(0.8)  # ресинк: unsubscribe → пауза 0.5с → subscribe
        ops = [op for op, _ in self.sent]
        self.assertEqual(ops, ["unsubscribe", "subscribe"])

    async def test_callback_errors_do_not_break_receive(self):
        def boom(_):
            raise ValueError("strategy bug")

        self.ws.on_message = boom
        with self.assertLogs("okx.ws", level="ERROR"):
            await self.ws._handle_message(json.dumps({"arg": {"channel": "tickers"}, "data": [{}]}))


if __name__ == "__main__":
    unittest.main()
