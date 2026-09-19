#!/usr/bin/env python3
"""Unit tests for scripts/btc5m_guards.py (stdlib only, no network/creds)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import btc5m_guards as g


class LedgerTest(unittest.TestCase):
    def test_missing_file_returns_fresh(self):
        led = g.load_ledger("/nonexistent/path.json", "2026-09-19")
        self.assertEqual(led, {"date": "2026-09-19", "trades_taken": 0, "realized_pnl_usdc": 0.0})

    def test_corrupt_file_returns_fresh(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{not json")
            path = fh.name
        try:
            led = g.load_ledger(path, "2026-09-19")
            self.assertEqual(led["trades_taken"], 0)
        finally:
            os.unlink(path)

    def test_previous_day_rolls_over(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ledger.json")
            self.assertTrue(g.save_ledger(path, {"date": "2026-09-18", "trades_taken": 12, "realized_pnl_usdc": -9.0}))
            led = g.load_ledger(path, "2026-09-19")
            self.assertEqual(led, {"date": "2026-09-19", "trades_taken": 0, "realized_pnl_usdc": 0.0})

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ledger.json")
            led = g.fresh_ledger("2026-09-19")
            g.record_open(led)
            g.record_close(led, -1.25)
            self.assertTrue(g.save_ledger(path, led))
            back = g.load_ledger(path, "2026-09-19")
            self.assertEqual(back["trades_taken"], 1)
            self.assertAlmostEqual(back["realized_pnl_usdc"], -1.25)

    def test_can_open_ok(self):
        led = {"date": "d", "trades_taken": 3, "realized_pnl_usdc": -1.0}
        ok, reason = g.can_open(led, max_trades_per_day=12, daily_max_loss_pct=10, equity_usd=100.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_can_open_blocks_max_trades(self):
        led = {"date": "d", "trades_taken": 12, "realized_pnl_usdc": 0.0}
        ok, reason = g.can_open(led, max_trades_per_day=12, daily_max_loss_pct=10, equity_usd=100.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "max_trades_per_day_reached")

    def test_can_open_blocks_daily_loss(self):
        led = {"date": "d", "trades_taken": 2, "realized_pnl_usdc": -10.0}
        ok, reason = g.can_open(led, max_trades_per_day=12, daily_max_loss_pct=10, equity_usd=100.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "daily_max_loss_hit")

    def test_can_open_boundary_not_blocked(self):
        # -9.99 against a $10 cap: still allowed
        led = {"date": "d", "trades_taken": 2, "realized_pnl_usdc": -9.99}
        ok, _ = g.can_open(led, max_trades_per_day=12, daily_max_loss_pct=10, equity_usd=100.0)
        self.assertTrue(ok)

    def test_record_close_none_pnl(self):
        led = g.fresh_ledger("d")
        g.record_close(led, None)
        self.assertEqual(led["realized_pnl_usdc"], 0.0)


class GateTest(unittest.TestCase):
    def test_spread_gate(self):
        self.assertEqual(g.spread_gate(0.02, 0.03), (True, "ok"))
        self.assertEqual(g.spread_gate(0.03, 0.03), (True, "ok"))
        self.assertEqual(g.spread_gate(0.031, 0.03), (False, "spread_too_wide"))
        self.assertEqual(g.spread_gate(None, 0.03), (False, "no_book"))

    def test_liquidity_gate(self):
        self.assertEqual(g.liquidity_gate(45.0, 30.0), (True, "ok"))
        self.assertEqual(g.liquidity_gate(30.0, 30.0), (True, "ok"))
        self.assertEqual(g.liquidity_gate(5.0, 30.0), (False, "liquidity_too_thin"))
        self.assertEqual(g.liquidity_gate(None, 30.0), (False, "no_book"))

    def test_staleness_gate(self):
        self.assertEqual(g.staleness_gate(3.0, 8.0), (True, "ok"))
        self.assertEqual(g.staleness_gate(9.0, 8.0), (False, "quote_stale"))
        # unknown age fails open but is flagged
        self.assertEqual(g.staleness_gate(None, 8.0), (True, "age_unknown"))

    def test_error_budget(self):
        self.assertFalse(g.error_budget_exceeded(0, 3))
        self.assertFalse(g.error_budget_exceeded(2, 3))
        self.assertTrue(g.error_budget_exceeded(3, 3))
        self.assertTrue(g.error_budget_exceeded(10, 3))


if __name__ == "__main__":
    unittest.main()
