"""Follow-up 1: surgical ASCII-only edits to tests/test_mr_trader.py.

The file is UTF-8 (with BOM); edits are pure-ASCII replacements with exact-count
assertions, BOM and line endings preserved. Run once from repo root.
"""
import io
import sys

PATH = "tests/test_mr_trader.py"

raw = open(PATH, "rb").read()
has_bom = raw.startswith(b"\xef\xbb\xbf")
body = raw[3:] if has_bom else raw
text = body.decode("utf-8")
print(f"BOM: {has_bom}, CRLF: {body.count(bytes([13, 10]))}, LF-only: {body.count(bytes([10])) - body.count(bytes([13, 10]))}")

pairs = [
    ("self.assertEqual(DEFAULT_OWNER, order_owner.ROUTER)",
     "self.assertEqual(DEFAULT_OWNER, order_owner.MR)"),
    ('self.assertEqual(MRTrader().owner, "botr")',
     'self.assertEqual(MRTrader().owner, "botmr")'),
    ('self.assertEqual(sig["owner"], "botr")',
     'self.assertEqual(sig["owner"], "botmr")'),
    ('self.assertEqual(call["owner"], "botr")',
     'self.assertEqual(call["owner"], "botmr")'),
    ('self.assertEqual(router.exits[0]["owner"], "botr")',
     'self.assertEqual(router.exits[0]["owner"], "botmr")'),
]
for old, new in pairs:
    n = text.count(old)
    assert n == 1, f"{old!r}: found {n}, want 1"
    text = text.replace(old, new)
    print(f"OK: {old[:60]!r}")

# test_owner_validation: botmr now registered -> positive assertions, not ValueError.
lines = text.split("\n")
hits = [i for i, ln in enumerate(lines)
        if ln.strip() == "with self.assertRaises(ValueError):"
        and i + 1 < len(lines) and 'MRTrader(owner="botmr")' in lines[i + 1]]
assert len(hits) == 1, f"botmr raises-block hits: {hits}"
i = hits[0]
removed = lines[i + 1]
assert removed.strip().startswith('MRTrader(owner="botmr")'), removed
lines[i] = '        self.assertEqual(MRTrader(owner="botmr").owner, "botmr")'
lines[i + 1] = '        self.assertEqual(order_owner.get("botmr").kind, "strategy")'
text = "\n".join(lines)
print(f"OK: validation block, removed comment line: {removed.strip()[:50]!r}")

out = (b"\xef\xbb\xbf" if has_bom else b"") + text.encode("utf-8")
open(PATH, "wb").write(out)
print("written", PATH)
