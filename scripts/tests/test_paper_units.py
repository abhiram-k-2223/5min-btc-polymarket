#!/usr/bin/env python3
"""Unit tests for the paper fill simulator (#paper-mode).

HTTP is fully mocked: no test touches the network. Covers the open/close
math, the guard rejections, the resting-limit path, and the safety
refusals (--execute, bad args).
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

PAPER_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PAPER_DIR, "paper"))

import pm_paper_trade_runner as p  # noqa: E402


def _event(up_token="UPTOK", dn_token="DNTOK"):
    return {"slug": "btc-updown-5m-1",
            "markets": [{"question": "Bitcoin Up or Down",
                         "outcomes": ["Up", "Down"],
                         "clobTokenIds": [up_token, dn_token]}]}


def _book(bid="0.30", bid_size="100", ask="0.32", ask_size="200"):
    return {"bids": [{"price": bid, "size": bid_size}],
            "asks": [{"price": ask, "size": ask_size}]}


def _run(argv, event=None, book=None, env=None):
    """Run main() with mocked HTTP; return (exit_code, stdout_json)."""
    def fake_get(url, params=None, timeout=None, **k):
        if "gamma-api" in url:
            return SimpleNamespace(status_code=200, json=lambda: [event or _event()],
                                   raise_for_status=lambda: None)
        return SimpleNamespace(status_code=200, json=lambda: book or _book(),
                               raise_for_status=lambda: None)

    buf = io.StringIO()
    with mock.patch.object(p.requests, "get", side_effect=fake_get), \
         mock.patch.dict(os.environ, env or {}, clear=False), \
         redirect_stdout(buf):
        code = p.main(argv)
    return code, json.loads(buf.getvalue())


class OpenTest(unittest.TestCase):
    def test_matched_fill_math(self):
        # notional = 100 * 0.05 = 5; ask 0.32 -> shares 15.625, cost 5.0
        code, out = _run(["--market-slug", "s", "--force-side", "UP",
                          "--start-equity", "100", "--risk-frac", "0.05"])
        self.assertEqual(code, 0)
        post = out["order_post_result"]
        self.assertTrue(post["success"] is True)
        self.assertEqual(post["status"], "matched")
        self.assertEqual(out["token_id"], "UPTOK")
        self.assertAlmostEqual(out["entry_price"], 0.32)
        self.assertAlmostEqual(post["takingAmount"], 5 / 0.32)
        self.assertAlmostEqual(post["makingAmount"], 5.0)
        self.assertTrue(post["orderID"].startswith("paper-open"))

    def test_max_notional_cap(self):
        code, out = _run(["--market-slug", "s", "--force-side", "DOWN",
                          "--start-equity", "1000", "--risk-frac", "0.5",
                          "--max-notional-usd", "8"])
        self.assertEqual(code, 0)
        self.assertAlmostEqual(out["order_post_result"]["makingAmount"], 8.0)
        self.assertEqual(out["token_id"], "DNTOK")

    def test_spread_guard_rejects(self):
        book = _book(bid="0.20", ask="0.32")  # spread 0.12
        code, out = _run(["--market-slug", "s", "--force-side", "UP"],
                         book=book, env={"PM_MAX_SPREAD": "0.03"})
        self.assertEqual(code, 1)
        self.assertFalse(out["order_post_result"]["success"])
        self.assertEqual(out["reason"], "spread_guard")

    def test_liquidity_guard_rejects(self):
        book = _book(ask="0.32", ask_size="10")  # notional 3.2
        code, out = _run(["--market-slug", "s", "--force-side", "UP"],
                         book=book, env={"PM_MIN_TOP_ASK_NOTIONAL_USD": "30"})
        self.assertEqual(code, 1)
        self.assertEqual(out["reason"], "liquidity_guard")

    def test_empty_book_fails_open(self):
        code, out = _run(["--market-slug", "s", "--force-side", "UP"],
                         book={"bids": [], "asks": [{"price": "0.99", "size": "5"}]})
        self.assertEqual(code, 1)
        self.assertEqual(out["reason"], "quote_unavailable")

    def test_string_token_ids_parsed(self):
        ev = {"slug": "s", "markets": [{"question": "q",
                                        "outcomes": '["Up", "Down"]',
                                        "clobTokenIds": '["U1", "D1"]'}]}
        code, out = _run(["--market-slug", "s", "--force-side", "DOWN"], event=ev)
        self.assertEqual(code, 0)
        self.assertEqual(out["token_id"], "D1")


class CloseTest(unittest.TestCase):
    def test_matched_close_math(self):
        code, out = _run(["--close-token-id", "T", "--close-shares", "10"])
        self.assertEqual(code, 0)
        post = out["order_post_result"]
        self.assertTrue(post["success"] is True)
        self.assertAlmostEqual(post["takingAmount"], 10 * 0.30)  # bid fill
        self.assertAlmostEqual(post["makingAmount"], 10.0)

    def test_unmarketable_limit_rests(self):
        code, out = _run(["--close-token-id", "T", "--close-shares", "10",
                          "--close-limit-price", "0.50"])
        self.assertEqual(code, 0)
        self.assertEqual(out["order_post_result"]["status"], "resting")
        self.assertEqual(out["close_skipped"], "limit_not_reached")

    def test_marketable_limit_fills_at_bid(self):
        code, out = _run(["--close-token-id", "T", "--close-shares", "10",
                          "--close-limit-price", "0.10"])
        self.assertEqual(code, 0)
        self.assertEqual(out["order_post_result"]["status"], "matched")
        self.assertAlmostEqual(out["fill_price"], 0.30)


def _resolved_event(closed=True, prices=("1", "0")):
    return {"slug": "btc-updown-5m-1",
            "markets": [{"question": "Bitcoin Up or Down",
                         "outcomes": ["Up", "Down"],
                         "clobTokenIds": ["UPTOK", "DNTOK"],
                         "closed": closed,
                         "outcomePrices": list(prices)}]}


def _run_close_nobook(argv, event):
    """Close with an empty (pulled) book; Gamma returns `event`."""
    def fake_get(url, params=None, timeout=None, **k):
        if "gamma-api" in url:
            return SimpleNamespace(status_code=200, json=lambda: [event],
                                   raise_for_status=lambda: None)
        return SimpleNamespace(status_code=200,
                               json=lambda: {"bids": [], "asks": []},
                               raise_for_status=lambda: None)

    buf = io.StringIO()
    with mock.patch.object(p.requests, "get", side_effect=fake_get), \
         redirect_stdout(buf):
        code = p.main(argv)
    return code, json.loads(buf.getvalue())


class CloseResolutionTest(unittest.TestCase):
    """Resolution settlement when the book is gone near/after expiry (#36)."""

    def test_resolved_itm_credits_full(self):
        code, out = _run_close_nobook(
            ["--market-slug", "s", "--close-token-id", "UPTOK",
             "--close-shares", "9.411765", "--close-side", "UP"],
            _resolved_event())
        self.assertEqual(code, 0)
        post = out["order_post_result"]
        self.assertTrue(post["success"] is True)
        self.assertEqual(post["status"], "matched")
        self.assertEqual(out["fill_price_source"], "paper_resolution")
        self.assertTrue(out["resolved"] is True)
        self.assertAlmostEqual(post["takingAmount"], 9.411765)
        self.assertAlmostEqual(post["makingAmount"], 9.411765)

    def test_resolved_otm_credits_zero_but_matched(self):
        code, out = _run_close_nobook(
            ["--market-slug", "s", "--close-token-id", "DNTOK",
             "--close-shares", "4.0", "--close-side", "DOWN"],
            _resolved_event())
        self.assertEqual(code, 0)
        post = out["order_post_result"]
        self.assertTrue(post["success"] is True)
        self.assertEqual(post["status"], "matched")
        self.assertAlmostEqual(post["takingAmount"], 0.0)

    def test_open_market_never_resolves(self):
        code, out = _run_close_nobook(
            ["--market-slug", "s", "--close-token-id", "T",
             "--close-shares", "4.0", "--close-side", "UP"],
            _resolved_event(closed=False, prices=("0.60", "0.40")))
        self.assertEqual(code, 1)
        self.assertEqual(out["reason"], "quote_unavailable")

    def test_closed_mid_prices_not_resolution(self):
        code, out = _run_close_nobook(
            ["--market-slug", "s", "--close-token-id", "T",
             "--close-shares", "4.0", "--close-side", "UP"],
            _resolved_event(closed=True, prices=("0.55", "0.45")))
        self.assertEqual(code, 1)
        self.assertEqual(out["reason"], "quote_unavailable")

    def test_no_slug_falls_back_to_unavailable(self):
        code, out = _run_close_nobook(
            ["--close-token-id", "T", "--close-shares", "4.0"],
            _resolved_event())
        self.assertEqual(code, 1)
        self.assertEqual(out["reason"], "quote_unavailable")


class SafetyTest(unittest.TestCase):
    def test_execute_refused(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = p.main(["--market-slug", "s", "--force-side", "UP", "--execute"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(buf.getvalue())["reason"], "refuse_execute")

    def test_execute_refused_on_close(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = p.main(["--close-token-id", "T", "--close-shares", "1", "--execute"])
        self.assertEqual(code, 2)

    def test_bad_args(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = p.main([])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(buf.getvalue())["reason"], "bad_args")

    def test_top_of_book_unsorted_ladders(self):
        book = {"bids": [{"price": "0.05", "size": "9"}, {"price": "0.30", "size": "1"}],
                "asks": [{"price": "0.90", "size": "9"}, {"price": "0.32", "size": "2"}]}
        bid, ask, bid_size, ask_size = p.top_of_book(book)
        self.assertEqual((bid, ask, bid_size, ask_size), (0.30, 0.32, 1.0, 2.0))


if __name__ == "__main__":
    unittest.main()
