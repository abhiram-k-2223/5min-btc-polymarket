#!/usr/bin/env python3
"""Unit tests for scripts/test_btc_5m_session_exit_sl.py (#24).

The runner needs ``py_clob_client`` (not installed everywhere), so this
module injects lightweight stubs into ``sys.modules`` before import. Only
pure/offline helpers are tested here — nothing touches the network.
"""
import argparse
import os
import re
import sys
import time
import types
import unittest
from types import SimpleNamespace

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _install_clob_stubs():
    pkg = types.ModuleType("py_clob_client")
    client_mod = types.ModuleType("py_clob_client.client")
    const_mod = types.ModuleType("py_clob_client.constants")
    types_mod = types.ModuleType("py_clob_client.clob_types")

    class ClobClient:  # pragma: no cover - stub, never instantiated here
        def __init__(self, *a, **k):
            raise AssertionError("network client must not be used in unit tests")

    class ApiCreds:
        def __init__(self, *a, **k):
            pass

    setattr(client_mod, "ClobClient", ClobClient)
    setattr(const_mod, "POLYGON", 137)
    setattr(types_mod, "ApiCreds", ApiCreds)
    setattr(pkg, "client", client_mod)
    setattr(pkg, "constants", const_mod)
    setattr(pkg, "clob_types", types_mod)
    sys.modules["py_clob_client"] = pkg
    sys.modules["py_clob_client.client"] = client_mod
    sys.modules["py_clob_client.constants"] = const_mod
    sys.modules["py_clob_client.clob_types"] = types_mod


_install_clob_stubs()
sys.path.insert(0, SCRIPTS_DIR)

import test_btc_5m_session_exit_sl as r  # noqa: E402


def _book(bids, asks, timestamp=None):
    def lvl(price, size):
        return SimpleNamespace(price=price, size=size)

    return SimpleNamespace(
        bids=[lvl(p, s) for p, s in bids],
        asks=[lvl(p, s) for p, s in asks],
        timestamp=timestamp,
    )


class PureHelpersTest(unittest.TestCase):
    def test_fnum(self):
        self.assertEqual(r._fnum("0.72"), 0.72)
        self.assertEqual(r._fnum(None, 1.0), 1.0)
        self.assertEqual(r._fnum("junk", 2.5), 2.5)
        self.assertEqual(r._fnum(float("nan"), 3.0), 3.0)

    def test_top_of_book(self):
        book = _book([("0.40", "100"), ("0.41", "50")], [("0.60", "30"), ("0.59", "20")])
        bid, bid_sz, ask, ask_sz = r._top_of_book(book)
        self.assertEqual((bid, bid_sz, ask, ask_sz), (0.41, 50.0, 0.59, 20.0))

    def test_top_of_book_empty_and_malformed(self):
        self.assertEqual(r._top_of_book(_book([], [])), (None, 0.0, None, 0.0))
        book = _book([("bad", "10")], [(None, None)])
        bid, _, ask, _ = r._top_of_book(book)
        self.assertIsNone(bid)
        self.assertIsNone(ask)

    def test_book_timestamp_ms_epoch(self):
        ms = str(int(time.time() * 1000))
        age = r._book_timestamp_age_sec(_book([], [], timestamp=ms))
        self.assertIsNotNone(age)
        assert age is not None
        self.assertLess(age, 5.0)

    def test_book_timestamp_garbage_and_missing(self):
        self.assertIsNone(r._book_timestamp_age_sec(_book([], [], timestamp="not-a-time")))
        self.assertIsNone(r._book_timestamp_age_sec(_book([], [])))

    def test_bucket_5m_boundaries(self):
        self.assertEqual(r.bucket_5m(0), 0)
        self.assertEqual(r.bucket_5m(299), 0)
        self.assertEqual(r.bucket_5m(300), 300)
        self.assertEqual(r.bucket_5m(301), 300)

    def test_parse_json_objects(self):
        objs = r.parse_json_objects('noise\n{"a": 1}\n{"b": {"c": [1,2]}}\ntail')
        self.assertEqual(objs, [{"a": 1}, {"b": {"c": [1, 2]}}])


class ProfileTest(unittest.TestCase):
    def _ns(self, **kw):
        base = dict(
            profile="conservative", threshold=None, stake_usd=None,
            stop_loss_pct=None, exit_before_sec=None,
            min_entry_seconds_left=None, entry_timeout_min=None, poll_sec=None,
            max_spread=None, min_top_ask_notional_usd=None,
            max_quote_age_sec=None, max_consecutive_errors=None,
            max_trades_per_day=None, daily_max_loss_pct=None, equity_usd=None,
        )
        base.update(kw)
        return argparse.Namespace(**base)

    def test_conservative_defaults(self):
        a = r.apply_profile(self._ns())
        self.assertEqual((a.threshold, a.stake_usd, a.stop_loss_pct), (0.70, 5.0, 0.25))
        self.assertEqual((a.max_spread, a.min_top_ask_notional_usd, a.max_quote_age_sec), (0.03, 30.0, 8.0))
        self.assertEqual((a.max_consecutive_errors, a.max_trades_per_day), (3, 12))
        self.assertEqual((a.daily_max_loss_pct, a.equity_usd), (10.0, 100.0))

    def test_aggressive_differs_where_intended(self):
        a = r.apply_profile(self._ns(profile="aggressive"))
        self.assertEqual(a.stop_loss_pct, 0.30)
        self.assertEqual(a.max_trades_per_day, 20)
        self.assertEqual(a.daily_max_loss_pct, 15.0)
        # shared execution guards stay identical
        self.assertEqual((a.max_spread, a.min_top_ask_notional_usd), (0.03, 30.0))

    def test_explicit_cli_values_preserved(self):
        a = r.apply_profile(self._ns(threshold=0.65, max_trades_per_day=5))
        self.assertEqual(a.threshold, 0.65)
        self.assertEqual(a.max_trades_per_day, 5)


class LogAttemptCapTest(unittest.TestCase):
    def test_heartbeat_dropped_first_and_counted(self):
        old_cap = r.ATTEMPT_CAP
        r.ATTEMPT_CAP = 5
        try:
            rep = {"attempts": [], "attempts_dropped": 0}
            for i in range(4):
                r.log_attempt(rep, {"status": "heartbeat", "i": i})
            r.log_attempt(rep, {"status": "skip_price_below_threshold"})
            r.log_attempt(rep, {"status": "heartbeat", "i": 4})
            self.assertEqual(len(rep["attempts"]), 5)
            self.assertEqual(rep["attempts_dropped"], 1)
            # the surviving signal entry must be kept
            self.assertTrue(any(a.get("status") == "skip_price_below_threshold" for a in rep["attempts"]))
        finally:
            r.ATTEMPT_CAP = old_cap


class WatcherContractTest(unittest.TestCase):
    """The watcher stops when BOTH greps match the runner's final report."""

    def _matches_watcher(self, out: str) -> bool:
        first = re.search(r'"decision": "enter"', out) is not None
        second = re.search(r'"success": true|"status": "matched"|"order_post_result"', out) is not None
        return first and second

    def test_enter_report_stops_watcher(self):
        out = (
            '{"decision": "enter", "result": "done", '
            '"open_raw": "... \\"order_post_result\\": '
            '{"success": true, "status": "matched"} ..."}'
        )
        self.assertTrue(self._matches_watcher(out))

    def test_no_entry_report_does_not_stop_watcher(self):
        self.assertFalse(self._matches_watcher('{"decision": "no_entry", "result": "no_entry_timeout"}'))
        self.assertFalse(self._matches_watcher('{"result": "no_entry_timeout"}'))


class OpsHelpersTest(unittest.TestCase):
    """Fail-open network helpers (#26, #33): errors must yield None/[],
    never raise."""

    def test_btc_spot_usd_none_on_error(self):
        orig = r.requests.get
        r.requests.get = lambda *a, **k: (_ for _ in ()).throw(IOError("down"))
        try:
            self.assertIsNone(r.btc_spot_usd())
        finally:
            r.requests.get = orig

    def test_btc_spot_usd_parses_binance(self):
        class Resp:
            status_code = 200

            def json(self):
                return {"price": "97500.50"}

        orig = r.requests.get
        r.requests.get = lambda *a, **k: Resp()
        try:
            px = r.btc_spot_usd()
            assert px is not None
            self.assertAlmostEqual(px, 97500.50)
        finally:
            r.requests.get = orig

    def test_wallet_positions_empty_on_error(self):
        orig = r.requests.get
        r.requests.get = lambda *a, **k: (_ for _ in ()).throw(IOError("down"))
        try:
            self.assertEqual(r.wallet_positions("0xabc"), [])
        finally:
            r.requests.get = orig

    def test_wallet_positions_parses_list(self):
        class Resp:
            status_code = 200

            def json(self):
                return [{"asset": "1", "size": "5"}]

        orig = r.requests.get
        r.requests.get = lambda *a, **k: Resp()
        try:
            self.assertEqual(r.wallet_positions("0xabc"), [{"asset": "1", "size": "5"}])
        finally:
            r.requests.get = orig


if __name__ == "__main__":
    unittest.main()
