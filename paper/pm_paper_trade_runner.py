#!/usr/bin/env python3
"""Paper fill simulator with the `pm_live_trade_runner.py` CLI contract.

The strategy runner (`scripts/test_btc_5m_session_exit_sl.py`) shells out to
`<exec-repo>/.venv/bin/python src/live/pm_live_trade_runner.py`, which lives
in the upstream author's private execution stack (see problems.md `i1`).
This module implements the same CLI + JSON contract so **paper trading**
works with no private stack: fills are simulated against the live public
CLOB book (buys lift the best ask, sells hit the best bid).

SAFETY: this simulator NEVER places real orders. If `--execute` is passed
it refuses outright (exit 2). Real execution still requires the private
stack and remains an unsolved upstream dependency (`i2`).

Output contract (parsed by the runner's brace-scanning `parse_json_objects`):
exactly one JSON object on stdout containing `order_post_result` plus the
sibling `token_id` / `entry_price` fields. Human diagnostics go to stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid

import requests

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
HTTP_TIMEOUT = 12.0


def log(msg: str) -> None:
    print(f"[paper] {msg}", file=sys.stderr, flush=True)


def paper_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _fnum(v, default=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f


def fetch_event(slug: str) -> dict:
    r = requests.get(GAMMA_EVENTS_URL, params={"slug": slug}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    evs = r.json()
    if not evs:
        raise RuntimeError(f"no Gamma event for slug {slug!r}")
    return evs[0]


def _as_list(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return v if isinstance(v, list) else []


def side_token_id(event: dict, side: str) -> str:
    """Map UP/DOWN to a CLOB token id via outcomes[i] <-> clobTokenIds[i]."""
    want = "up" if side.strip().upper() == "UP" else "down"
    for m in event.get("markets") or []:
        outcomes = [str(o).lower() for o in _as_list(m.get("outcomes"))]
        tokens = [str(t) for t in _as_list(m.get("clobTokenIds"))]
        if want in outcomes and len(tokens) == len(outcomes):
            return tokens[outcomes.index(want)]
    raise RuntimeError(f"no {want!r} token found for event {event.get('slug')!r}")


def fetch_book(token_id: str, retries: int = 2) -> dict:
    """Fetch the CLOB book, retrying transient connection errors once.

    A single blip must not kill a fill; a persistently dead book still
    surfaces as an error the runner's retry/abort logic can handle.
    """
    last: Exception | None = None
    for attempt in range(max(1, retries) + 1):
        try:
            r = requests.get(CLOB_BOOK_URL, params={"token_id": token_id},
                             timeout=HTTP_TIMEOUT)
            if r.status_code == 404:
                raise RuntimeError(f"no orderbook for token {token_id[:16]}...")
            r.raise_for_status()
            book = r.json()
            if isinstance(book, dict) and book.get("error"):
                raise RuntimeError(f"book error: {book['error']}")
            return book
        except Exception as e:
            last = e
            time.sleep(1.0)
    raise RuntimeError(f"book fetch failed after retries: {last}")


def top_of_book(book: dict):
    """(best_bid, best_ask, bid_size_at_best, ask_size_at_best).

    Ladders are not guaranteed best-first sorted, so min/max explicitly.
    """
    bids, asks = [], []
    for lvl in book.get("bids") or []:
        p, s = _fnum(lvl.get("price")), _fnum(lvl.get("size"), 0.0)
        if p is not None and p > 0:
            bids.append((p, s or 0.0))
    for lvl in book.get("asks") or []:
        p, s = _fnum(lvl.get("price")), _fnum(lvl.get("size"), 0.0)
        if p is not None and p > 0:
            asks.append((p, s or 0.0))
    if not bids or not asks:
        raise RuntimeError("empty book side")
    best_bid, bid_size = max(bids, key=lambda x: x[0])
    best_ask, ask_size = min(asks, key=lambda x: x[0])
    return best_bid, best_ask, bid_size, ask_size


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def fail(reason: str, **extra) -> int:
    emit({"success": False, "status": "error", "reason": reason, **extra,
          "order_post_result": {"success": False, "status": "error",
                                "reason": reason}})
    log(f"FAILED: {reason} {extra}")
    return 1


def do_open(args) -> int:
    try:
        event = fetch_event(args.market_slug)
        token_id = side_token_id(event, args.force_side)
        book = fetch_book(token_id)
        best_bid, best_ask, _bid_size, ask_size = top_of_book(book)
    except Exception as e:
        return fail("quote_unavailable", error=str(e)[:200])

    spread = best_ask - best_bid
    max_spread = _fnum(os.environ.get("PM_MAX_SPREAD"), 1.0)
    ask_notional = best_ask * ask_size
    min_notional = _fnum(os.environ.get("PM_MIN_TOP_ASK_NOTIONAL_USD"), 0.0)
    if max_spread is not None and spread > max_spread:
        return fail("spread_guard", spread=round(spread, 4), max_spread=max_spread)
    if min_notional is not None and ask_notional < min_notional:
        return fail("liquidity_guard", ask_notional_usd=round(ask_notional, 2),
                    min_notional_usd=min_notional)

    equity = _fnum(args.start_equity, 100.0) or 100.0
    risk_frac = _fnum(args.risk_frac, 0.05) or 0.05
    notional = equity * risk_frac
    if args.max_notional_usd is not None:
        notional = min(notional, float(args.max_notional_usd))
    if notional <= 0:
        return fail("bad_notional", notional=notional)

    shares = notional / best_ask
    cost = shares * best_ask
    oid = paper_id("paper-open")
    log(f"OPEN {args.force_side} {args.market_slug} fill@{best_ask:.4f} "
        f"shares={shares:.4f} cost=${cost:.2f} (spread {spread:.4f})")
    emit({
        "market_slug": args.market_slug,
        "side": args.force_side,
        "token_id": token_id,
        "entry_price": round(best_ask, 6),
        "notional_usdc": round(cost, 4),
        "fill_price_source": "paper_clob_ask",
        "order_post_result": {
            "success": True,
            "status": "matched",
            "takingAmount": round(shares, 6),
            "makingAmount": round(cost, 6),
            "orderID": oid,
            "transactionsHashes": [oid],
        },
    })
    return 0


def do_close(args) -> int:
    shares = _fnum(args.close_shares, 0.0) or 0.0
    if shares <= 0:
        return fail("bad_shares", shares=args.close_shares)
    try:
        book = fetch_book(args.close_token_id)
        best_bid, _best_ask, _b, _a = top_of_book(book)
    except Exception as e:
        return fail("quote_unavailable", error=str(e)[:200])

    limit = _fnum(args.close_limit_price)
    if limit is not None and limit > 0 and best_bid < limit:
        # Limit sell not marketable: order would rest. Report it so the
        # runner's FAK -> GTC -> FORCE cascade proceeds naturally.
        emit({"close_token_id": args.close_token_id,
              "close_skipped": "limit_not_reached",
              "limit_price": limit, "best_bid": round(best_bid, 6),
              "order_post_result": {"success": False, "status": "resting",
                                    "reason": "limit_not_reached"}})
        log(f"CLOSE resting (bid {best_bid:.4f} < limit {limit:.4f})")
        return 0

    proceeds = shares * best_bid
    oid = paper_id("paper-close")
    log(f"CLOSE {shares:.4f} shares fill@{best_bid:.4f} proceeds=${proceeds:.2f}")
    emit({
        "close_token_id": args.close_token_id,
        "close_shares": round(shares, 6),
        "fill_price": round(best_bid, 6),
        "fill_price_source": "paper_clob_bid",
        "order_post_result": {
            "success": True,
            "status": "matched",
            "takingAmount": round(proceeds, 6),
            "makingAmount": round(shares, 6),
            "orderID": oid,
            "transactionsHashes": [oid],
        },
    })
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Paper fill simulator (never trades real)")
    ap.add_argument("--market-slug", default=None)
    ap.add_argument("--force-side", default=None)
    ap.add_argument("--start-equity", type=float, default=100.0)
    ap.add_argument("--risk-frac", type=float, default=0.05)
    ap.add_argument("--max-notional-usd", type=float, default=None)
    ap.add_argument("--close-token-id", default=None)
    ap.add_argument("--close-shares", type=float, default=None)
    ap.add_argument("--close-limit-price", type=float, default=None)
    ap.add_argument("--execute", action="store_true",
                    help="REFUSED by the paper simulator (safety)")
    args = ap.parse_args(argv)

    if args.execute:
        emit({"success": False, "status": "error", "reason": "refuse_execute",
              "order_post_result": {"success": False, "status": "error",
                                    "reason": "refuse_execute"}})
        log("REFUSED --execute: paper simulator never places real orders")
        return 2

    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    log(f"paper run at {ts}")
    try:
        if args.close_token_id:
            return do_close(args)
        if args.market_slug and args.force_side:
            return do_open(args)
    except Exception as e:  # never traceback into the runner's parser
        return fail("internal_error", error=str(e)[:200])
    ap.print_usage(sys.stderr)
    return fail("bad_args",
                hint="need --market-slug+--force-side or --close-token-id+--close-shares")


if __name__ == "__main__":
    sys.exit(main())
