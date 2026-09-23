#!/usr/bin/env python3
"""Unit tests for the low-item ops modules (#25 trade DB, #27 alerts,
#33 position state). All stdlib-only; temp dirs keep the repo clean.
"""
import os
import sys
import tempfile
import unittest

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

import btc5m_alerts as alerts  # noqa: E402
import btc5m_backtest as backtest  # noqa: E402
import btc5m_guards as guards  # noqa: E402
import btc5m_tradedb as tradedb  # noqa: E402


class TradeDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.con = tradedb.connect(self.tmp)

    def test_open_close_roundtrip(self):
        tid = tradedb.record_open(
            self.con, mode="dry", market_slug="m", side="up",
            token_id="123", entry_price=0.72, shares=6.9,
            cost_usdc=5.0, btc_entry=81000.0, open_order_id="oid-1")
        tradedb.record_close(
            self.con, tid, close_reason="time_exit", close_usdc=6.1,
            pnl_usdc=1.1, btc_exit=81100.0, close_tx="tx-1")
        rows = tradedb.recent(self.con, 5)
        self.assertEqual(len(rows), 1)
        t = rows[0]
        self.assertEqual((t["side"], t["close_reason"], t["pnl_usdc"]),
                         ("up", "time_exit", 1.1))
        self.assertEqual((t["btc_entry"], t["btc_exit"]), (81000.0, 81100.0))

    def test_daily_summary(self):
        tid = tradedb.record_open(self.con, mode="live", market_slug="m", side="dn")
        tradedb.record_close(self.con, tid, close_reason="stop_loss", pnl_usdc=-2.0)
        s = tradedb.daily_summary(self.con, 7)
        self.assertEqual(len(s), 1)
        self.assertEqual((s[0]["trades"], s[0]["wins"], s[0]["pnl_usdc"]), (1, 0, -2.0))

    def test_empty_db(self):
        self.assertEqual(tradedb.recent(self.con, 5), [])
        self.assertEqual(tradedb.daily_summary(self.con, 7), [])

    def test_exit_mix(self):
        # book fill, tagged redemption, legacy linger shape (pnl set but
        # no proceeds), stop-loss, and one unscored row.
        b = tradedb.record_open(self.con, mode="dry", side="up")
        tradedb.record_close(self.con, b, close_reason="time_exit_40s",
                             close_usdc=8.5, pnl_usdc=0.5)
        d = tradedb.record_open(self.con, mode="dry", side="dn")
        tradedb.record_close(self.con, d, close_reason="time_exit_40s",
                             close_usdc=None, pnl_usdc=None)
        tradedb.settle_resolution(self.con, d, close_usdc=8.0, pnl_usdc=0.0,
                                  reason_suffix="+resolution_settle_dn")
        lg = tradedb.record_open(self.con, mode="dry", side="up")
        tradedb.record_close(self.con, lg, close_reason="time_exit_40s",
                             close_usdc=None, pnl_usdc=0.16)
        st = tradedb.record_open(self.con, mode="dry", side="dn")
        tradedb.record_close(self.con, st, close_reason="stop_loss_25pct",
                             close_usdc=5.0, pnl_usdc=-3.0)
        u = tradedb.record_open(self.con, mode="dry", side="up")
        tradedb.record_close(self.con, u, close_reason="time_exit_40s",
                             close_usdc=None, pnl_usdc=None)
        m = tradedb.exit_mix(self.con)
        self.assertEqual((m['n'], m['book'], m['redemption'],
                          m['unscored'], m['stops']), (5, 2, 2, 1, 1))
        self.assertAlmostEqual(m['book_fill_rate'], 0.5)

    def test_exit_mix_empty(self):
        m = tradedb.exit_mix(self.con)
        self.assertEqual(m['n'], 0)
        self.assertIsNone(m['book_fill_rate'])


class AlertsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_emit_and_tail(self):
        alerts.emit(self.tmp, "entry", {"side": "up"})
        alerts.emit(self.tmp, "close", {"reason": "time_exit"})
        tailed = alerts.tail(self.tmp, 10)
        self.assertEqual([a["event"] for a in tailed], ["entry", "close"])
        self.assertEqual(tailed[0]["data"], {"side": "up"})
        for a in tailed:
            self.assertTrue(a["ts"].endswith("Z"))

    def test_tail_missing_file(self):
        self.assertEqual(alerts.tail(os.path.join(self.tmp, "nope"), 5), [])

    def test_emit_never_raises(self):
        # bad webhook URL must not raise
        a = alerts.emit(self.tmp, "warn", {"m": 1},
                        webhook_url="http://127.0.0.1:1/unreachable")
        self.assertEqual(a["event"], "warn")


class PositionStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_save_load_clear(self):
        self.assertIsNone(guards.load_open_position(self.tmp))
        pos = {"side": "up", "token_id": "123", "entry_price": 0.7}
        guards.save_open_position(self.tmp, pos)
        self.assertEqual(guards.load_open_position(self.tmp), pos)
        self.assertTrue(guards.clear_open_position(self.tmp))
        self.assertIsNone(guards.load_open_position(self.tmp))
        self.assertFalse(guards.clear_open_position(self.tmp))

    def test_load_corrupt_returns_none(self):
        with open(guards.open_position_path(self.tmp), "w") as fh:
            fh.write("{not json")
        self.assertIsNone(guards.load_open_position(self.tmp))


class PositionFilterTest(unittest.TestCase):
    def test_match_and_dust(self):
        positions = [
            {"asset": "999", "size": "10"},
            {"asset": "123", "size": "0.00000000001"},
        ]
        self.assertIsNone(guards.position_in_tokens(positions, ["123", "456"]))
        positions[1]["size"] = "6.9"
        hit = guards.position_in_tokens(positions, ["123", "456"])
        self.assertEqual(hit, positions[1])

    def test_no_match_and_bad_input(self):
        self.assertIsNone(guards.position_in_tokens([{"asset": "1", "size": "5"}], ["2"]))
        self.assertIsNone(guards.position_in_tokens(None, ["1"]))
        self.assertIsNone(guards.position_in_tokens("junk", ["1"]))


class BacktestTest(unittest.TestCase):
    def _snap(self, ts, side, bid, ask, size=100.0):
        return {"ts_ms": int(ts * 1000), "side": side,
                "best_bid": bid, "best_ask": ask, "spread": ask - bid,
                "bids": f"[{{'price': {bid}, 'size': {size}}}]",
                "asks": f"[{{'price': {ask}, 'size': {size}}}]"}

    def _params(self, **kw):
        p = {"threshold": 0.7, "stake_usd": 5.0, "stop_loss_pct": 0.3,
             "exit_before_sec": 30.0, "min_entry_seconds_left": 60.0,
             "max_spread": 0.05, "min_top_ask_notional_usd": 1.0,
             "max_quote_age_sec": 30.0, "market_end_ts": 1000.0}
        p.update(kw)
        return p

    def _both_sides(self, times, bid=0.70, ask=0.72, **kw):
        snaps = []
        for t in times:
            snaps.append(self._snap(t, "up", bid, ask, **kw))
            snaps.append(self._snap(t, "dn", 1 - ask, 1 - bid, **kw))
        return snaps

    def test_entry_and_time_exit(self):
        snaps = self._both_sides(range(100, 1000, 10))
        res = backtest.replay(snaps, self._params())
        self.assertEqual(res["metrics"]["n_trades"], 1)
        t = res["trades"][0]
        self.assertEqual((t["side"], t["exit_reason"]), ("up", "time_exit"))
        self.assertAlmostEqual(t["entry_price"], 0.72)
        self.assertAlmostEqual(t["pnl_usdc"], round(5 / 0.72 * 0.70 - 5, 4))

    def test_stop_loss_exit(self):
        snaps = self._both_sides(range(100, 300, 10))
        snaps += [self._snap(300, "up", 0.40, 0.45),
                  self._snap(300, "dn", 0.55, 0.60)]
        snaps += self._both_sides(range(310, 1000, 10))
        res = backtest.replay(snaps, self._params())
        # 0.72 * (1 - 0.3) = 0.504 > 0.40 -> stop-loss on the held side
        self.assertEqual(res["trades"][0]["exit_reason"], "stop_loss")

    def test_spread_guard_blocks(self):
        snaps = self._both_sides(range(100, 500, 10), bid=0.50, ask=0.72)
        res = backtest.replay(snaps, self._params())
        self.assertEqual(res["metrics"]["n_trades"], 0)
        self.assertGreater(res["skips"].get("spread_guard", 0), 0)

    def test_below_threshold(self):
        snaps = self._both_sides(range(100, 500, 10), bid=0.58, ask=0.60)
        res = backtest.replay(snaps, self._params())
        self.assertEqual(res["metrics"]["n_trades"], 0)
        self.assertGreater(res["skips"].get("below_threshold", 0), 0)

    def test_end_of_data_flagged(self):
        snaps = self._both_sides(range(100, 500, 10))
        res = backtest.replay(snaps, self._params())
        self.assertEqual(res["metrics"]["n_trades"], 0)
        self.assertEqual(res["metrics"]["end_of_data_positions"], 1)
        self.assertEqual(res["trades"][0]["exit_reason"], "end_of_data")

    def test_parse_ts_unix_and_iso(self):
        self.assertAlmostEqual(backtest.parse_ts("1789759500"), 1789759500.0)
        self.assertAlmostEqual(
            backtest.parse_ts("2026-09-18T19:25:00Z"), 1789759500.0)

    def _mom_params(self, series, **kw):
        p = self._params(**kw)
        p.update({"btc_series": series, "btc_move_usd_min": 70.0,
                  "skew_veto_threshold": 0.10})
        return p

    def test_momentum_entry_follows_btc(self):
        series = [{"t": 700.0, "close": 100000.0},
                  {"t": 750.0, "close": 100100.0},
                  {"t": 990.0, "close": 100120.0}]
        snaps = self._both_sides(range(100, 1000, 10))
        res = backtest.replay(snaps, self._mom_params(series))
        self.assertEqual(res["metrics"]["n_trades"], 1)
        t = res["trades"][0]
        self.assertEqual(t["side"], "up")
        self.assertAlmostEqual(t["btc_move_usd_at_entry"], 100.0)
        self.assertGreater(res["skips"].get("no_btc_momentum", 0), 0)

    def test_momentum_veto_on_opposing_skew(self):
        series = [{"t": 700.0, "close": 100000.0},
                  {"t": 750.0, "close": 100100.0},
                  {"t": 990.0, "close": 100120.0}]
        snaps = []
        for t in range(100, 1000, 10):
            snaps.append(self._snap(t, "up", 0.70, 0.72))
            snaps.append(self._snap(t, "dn", 0.80, 0.85))
        res = backtest.replay(snaps, self._mom_params(series))
        self.assertEqual(res["metrics"]["n_trades"], 0)
        self.assertGreater(res["skips"].get("skew_veto", 0), 0)

    def test_legacy_mode_ignores_btc(self):
        snaps = self._both_sides(range(100, 1000, 10))
        res = backtest.replay(snaps, self._params())
        self.assertEqual(res["metrics"]["n_trades"], 1)
        self.assertIsNone(res["trades"][0]["btc_move_usd_at_entry"])

    def test_token_to_asset_hex(self):
        self.assertEqual(
            backtest.token_to_asset_hex(
                85626091783806950319755530089519759177231513702682138920823697838429460066490),
            "bd4ea68709c90f30fba96735bd0f64ee4b29d928622d33de401ab6b23e1170ba")

    def test_book_top_uses_min_ask_max_bid(self):
        snap = {"bids": "[{'price': 0.05, 'size': 10}, {'price': 0.60, 'size': 5}]",
                "asks": "[{'price': 0.99, 'size': 10}, {'price': 0.72, 'size': 7}]"}
        top = backtest._book_top(snap)
        self.assertEqual((top["best_bid"], top["best_ask"], top["spread"]),
                         (0.60, 0.72, 0.12))
        self.assertAlmostEqual(top["ask_notional"], 0.72 * 7)

    def test_stop_redeem_settles_at_resolution(self):
        # #39: de-facto live arm — no book exit, redeem winner at 1.00.
        snaps = self._both_sides(range(100, 1000, 10))
        res = backtest.replay(
            snaps, self._params(hold_mode="stop_redeem", resolution="up"))
        self.assertEqual(res["metrics"]["n_trades"], 1)
        t = res["trades"][0]
        self.assertEqual((t["side"], t["exit_reason"]), ("up", "resolution"))
        self.assertAlmostEqual(t["exit_price"], 1.0)
        self.assertAlmostEqual(t["pnl_usdc"], round(5 / 0.72 - 5, 4))

    def test_hold_loses_when_side_loses(self):
        snaps = self._both_sides(range(100, 1000, 10))
        res = backtest.replay(
            snaps, self._params(hold_mode="hold", resolution="dn"))
        t = res["trades"][0]
        self.assertEqual(t["exit_reason"], "resolution")
        self.assertAlmostEqual(t["pnl_usdc"], -5.0)

    def test_hold_ignores_stop_loss(self):
        snaps = self._both_sides(range(100, 300, 10))
        snaps += [self._snap(300, "up", 0.40, 0.45),
                  self._snap(300, "dn", 0.55, 0.60)]
        snaps += self._both_sides(range(310, 1000, 10))
        res = backtest.replay(
            snaps, self._params(hold_mode="hold", resolution="up"))
        self.assertEqual(res["trades"][0]["exit_reason"], "resolution")

    def test_stop_redeem_keeps_stop_loss(self):
        # #39: stop_redeem still scratches on the book when it can.
        snaps = self._both_sides(range(100, 300, 10))
        snaps += [self._snap(300, "up", 0.40, 0.45),
                  self._snap(300, "dn", 0.55, 0.60)]
        snaps += self._both_sides(range(310, 1000, 10))
        res = backtest.replay(
            snaps, self._params(hold_mode="stop_redeem", resolution="up"))
        self.assertEqual(res["trades"][0]["exit_reason"], "stop_loss")

    def test_hold_without_resolution_stays_end_of_data(self):
        snaps = self._both_sides(range(100, 1000, 10))
        res = backtest.replay(
            snaps, self._params(hold_mode="hold", resolution=None))
        self.assertEqual(res["metrics"]["n_trades"], 0)
        self.assertEqual(res["trades"][0]["exit_reason"], "end_of_data")

    def test_resolve_event_winner(self):
        up = {"markets": [{"outcomes": ["Up", "Down"],
                           "outcomePrices": ["1", "0"]}]}
        self.assertEqual(backtest.resolve_event_winner(up), "up")
        dn = {"markets": [{"outcomes": ["Up", "Down"],
                           "outcomePrices": ["0", "1"]}]}
        self.assertEqual(backtest.resolve_event_winner(dn), "dn")
        mid = {"markets": [{"outcomes": ["Up", "Down"],
                            "outcomePrices": ["0.6", "0.4"]}]}
        self.assertIsNone(backtest.resolve_event_winner(mid))
        as_str = {"markets": [{"outcomes": '["Up", "Down"]',
                               "outcomePrices": '["0", "1"]'}]}
        self.assertEqual(backtest.resolve_event_winner(as_str), "dn")
        self.assertIsNone(backtest.resolve_event_winner({"markets": []}))


if __name__ == "__main__":
    unittest.main()
