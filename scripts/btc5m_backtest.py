#!/usr/bin/env python3
"""Backtest harness for the BTC 5m strategy (#23).

Two subcommands:

  fetch    Export CLOB ``book`` snapshots for one market from the Pendulum
           Flow archive (no auth, DuckDB httpfs partial reads) into a local
           JSONL snapshot file. Requires the ``duckdb`` package.
  replay   Replay the entry/exit rules against a snapshot file. Stdlib-only.

The replay mirrors the live runner (scripts/test_btc_5m_session_exit_sl.py):
entry = highest best-ask >= threshold subject to spread / liquidity /
staleness gates; fill at the best ask; stop-loss monitored on the best bid;
time exit ``exit_before_sec`` before market end, filled at the best bid.

Quote-age proxy: the archive carries no per-book exchange timestamp, so age
is approximated as the gap since the previous snapshot of the same side.
A feed gap therefore reads as a stale quote, which is the safe direction.

Example:
  python scripts/btc5m_backtest.py fetch --hour 2026-09-18T19 \\
      --up-token 8562...90 --dn-token 8733...39 \\
      --out data/backtest/btc-5m-20260918-1920.jsonl
  python scripts/btc5m_backtest.py replay \\
      --snapshots data/backtest/btc-5m-20260918-1920.jsonl \\
      --market-end 2026-09-18T19:25:00Z --threshold 0.70
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json as _json
import os as _os
import sys as _sys

UTC = _dt.timezone.utc


def parse_ts(s: str) -> float:
    """ISO-8601 string or plain unix timestamp -> epoch seconds."""
    text = str(s).strip()
    try:
        return float(text)
    except ValueError:
        pass
    return _dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def token_to_asset_hex(token) -> str:
    """Decimal CTF token ID -> 32-byte big-endian hex (Pendulum asset_id)."""
    return format(int(str(token)), "064x")


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

PENDULUM_URL = ("https://archive.pendulumflow.com/v3/{day}/{hh}/{day}T{hh}.parquet")


def fetch_snapshots(hour: str, up_token, dn_token, out_path: str) -> int:
    try:
        import duckdb  # lazy: replay stays stdlib-only
    except ImportError:
        print("fetch needs the 'duckdb' package (pip install duckdb)", file=_sys.stderr)
        raise SystemExit(2)
    day, hh = hour.split("T")
    url = PENDULUM_URL.format(day=day, hh=hh)
    up_hex, dn_hex = token_to_asset_hex(up_token), token_to_asset_hex(dn_token)
    con = duckdb.connect()
    rows = con.execute(
        "SELECT epoch_ms(timestamp) AS ts_ms, hex(asset_id) AS asset,"
        " CAST(best_bid AS DOUBLE) AS best_bid,"
        " CAST(best_ask AS DOUBLE) AS best_ask,"
        " CAST(spread AS DOUBLE) AS spread,"
        " CAST(bids AS VARCHAR) AS bids, CAST(asks AS VARCHAR) AS asks"
        f" FROM '{url}' WHERE event_type = 'book'"
        f" AND (asset_id = unhex('{up_hex}') OR asset_id = unhex('{dn_hex}'))"
        " ORDER BY timestamp"
    ).fetchall()
    _os.makedirs(_os.path.dirname(_os.path.abspath(out_path)), exist_ok=True)
    n = 0
    with open(out_path, "w", encoding="utf-8") as fh:
        for ts_ms, asset, bb, ba, spread, bids, asks in rows:
            side = "up" if asset.lower() == up_hex else "dn"
            fh.write(_json.dumps({
                "ts_ms": int(ts_ms), "side": side,
                "best_bid": bb, "best_ask": ba, "spread": spread,
                "bids": bids, "asks": asks,
            }) + "\n")
            n += 1
    return n


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

def _parse_ladder(levels_varchar):
    """Parse a DuckDB STRUCT-array string into [(price, size), ...].

    Returns [] when unparseable. Ladders in the archive are NOT sorted
    best-first, so callers must take min/max themselves.
    """
    if not levels_varchar:
        return []
    try:
        import ast as _ast

        levels = _ast.literal_eval(levels_varchar)
        out = []
        for lv in levels if isinstance(levels, list) else []:
            if not isinstance(lv, dict):
                continue
            rp, rs = lv.get("price"), lv.get("size")
            if rp is None or rs is None:
                continue
            try:
                out.append((float(rp), float(rs)))
            except (TypeError, ValueError):
                continue
        return [(p, s) for p, s in out if p > 0 and s > 0]
    except Exception:
        return []


def _book_top(snap: dict) -> dict:
    """Executable top of book derived from the ladders.

    The archive's top-level best_bid/best_ask/spread columns are null for
    whole hours, so the replay derives them: best bid = max bid price,
    best ask = min ask price (mirrors the runner's _top_of_book).
    """
    bids = _parse_ladder(snap.get("bids"))
    asks = _parse_ladder(snap.get("asks"))
    bid, _bid_size = max(bids, key=lambda x: x[0]) if bids else (None, None)
    ask, ask_size = min(asks, key=lambda x: x[0]) if asks else (None, None)
    spread = (ask - bid) if (ask is not None and bid is not None) else None
    notion = (ask * ask_size) if (ask is not None and ask_size) else None
    return {"best_bid": bid, "best_ask": ask, "spread": spread,
            "ask_size": ask_size, "ask_notional": notion}


def replay(snapshots, params: dict) -> dict:
    """Replay strategy over snapshot rows. Returns trades + metrics + skips."""
    threshold = params["threshold"]
    stake = params["stake_usd"]
    sl_pct = params["stop_loss_pct"]
    exit_before = params["exit_before_sec"]
    min_entry_left = params["min_entry_seconds_left"]
    max_spread = params.get("max_spread")
    min_notional = params.get("min_top_ask_notional_usd")
    max_age = params.get("max_quote_age_sec")
    end_ts = params["market_end_ts"]
    exit_ts = end_ts - exit_before

    by_side = {"up": [], "dn": []}
    for s in snapshots:
        if s.get("side") in by_side:
            by_side[s["side"]].append(s)

    # Merge both sides in time order; each step carries the latest known
    # quote per side (like the runner's per-poll snapshot).
    events = sorted(
        [(s["ts_ms"], s["side"], s) for side in by_side for s in by_side[side]])
    latest: dict[str, dict] = {}
    prev_ts = {}
    skips: dict[str, int] = {}
    trades = []
    in_pos = None

    def skip(reason):
        skips[reason] = skips.get(reason, 0) + 1

    for ts_ms, side, snap in events:
        now = ts_ms / 1000.0
        prev = prev_ts.get(snap["side"])
        prev_ts[snap["side"]] = now
        latest[snap["side"]] = _book_top(snap)
        if "up" not in latest or "dn" not in latest:
            continue

        if in_pos is None:
            if now >= exit_ts:
                skip("too_late_to_enter")
                continue
            if (end_ts - now) < min_entry_left:
                skip("too_late_to_enter")
                continue
            cands = []
            for sd, top in latest.items():
                ba = top["best_ask"]
                if ba is not None and ba >= threshold:
                    cands.append((sd, ba))
            if not cands:
                skip("below_threshold")
                continue
            pick_side, ask = sorted(cands, key=lambda x: x[1], reverse=True)[0]
            top = latest[pick_side]
            spread = top["spread"]
            if max_spread is not None and spread is not None and spread > max_spread:
                skip("spread_guard")
                continue
            notion = top["ask_notional"]
            if (min_notional is not None and notion is not None
                    and notion < min_notional):
                skip("liquidity_guard")
                continue
            age = (now - prev) if prev is not None else 0.0
            if max_age is not None and age > max_age:
                skip("stale_quote")
                continue
            shares = stake / ask if ask > 0 else 0
            in_pos = {"side": pick_side, "entry_ts": now, "entry_price": ask,
                      "shares": shares, "cost": stake,
                      "sl_price": ask * (1.0 - sl_pct)}
        else:
            top = latest[in_pos["side"]]
            bid = top["best_bid"]
            if now >= exit_ts:
                reason = "time_exit"
            elif bid is not None and bid <= in_pos["sl_price"]:
                reason = "stop_loss"
            else:
                continue
            exit_px = bid if bid is not None else 0.0
            proceeds = in_pos["shares"] * exit_px
            trades.append({
                "side": in_pos["side"],
                "entry_ts": in_pos["entry_ts"], "exit_ts": now,
                "entry_price": round(in_pos["entry_price"], 4),
                "exit_price": round(exit_px, 4),
                "exit_reason": reason,
                "pnl_usdc": round(proceeds - in_pos["cost"], 4),
            })
            in_pos = None

    # Dangling position at end of data: mark-to-last-bid, flagged.
    if in_pos is not None:
        top = latest[in_pos["side"]]
        bid = top["best_bid"] or 0.0
        trades.append({
            "side": in_pos["side"], "entry_ts": in_pos["entry_ts"],
            "exit_ts": None, "entry_price": round(in_pos["entry_price"], 4),
            "exit_price": round(bid, 4), "exit_reason": "end_of_data",
            "pnl_usdc": round(in_pos["shares"] * bid - in_pos["cost"], 4),
        })

    pnls = [t["pnl_usdc"] for t in trades if t["exit_reason"] != "end_of_data"]
    wins = sum(1 for p in pnls if p > 0)
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return {
        "trades": trades,
        "metrics": {
            "n_trades": len(pnls),
            "wins": wins,
            "win_rate": round(wins / len(pnls), 4) if pnls else None,
            "total_pnl_usdc": round(sum(pnls), 4),
            "avg_pnl_usdc": round(sum(pnls) / len(pnls), 4) if pnls else None,
            "max_drawdown_usdc": round(max_dd, 4),
            "end_of_data_positions": sum(
                1 for t in trades if t["exit_reason"] == "end_of_data"),
        },
        "skips": skips,
    }


def load_snapshots(path: str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(_json.loads(line))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="BTC 5m backtest harness")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_f = sub.add_parser("fetch", help="Export snapshots from Pendulum archive")
    p_f.add_argument("--hour", required=True, help="e.g. 2026-09-18T19 (UTC)")
    p_f.add_argument("--up-token", required=True)
    p_f.add_argument("--dn-token", required=True)
    p_f.add_argument("--out", required=True)

    p_r = sub.add_parser("replay", help="Replay strategy over snapshots")
    p_r.add_argument("--snapshots", required=True)
    p_r.add_argument("--market-end", required=True, help="ISO UTC, e.g. 2026-09-18T19:25:00Z")
    p_r.add_argument("--threshold", type=float, default=0.70)
    p_r.add_argument("--stake-usd", type=float, default=5.0)
    p_r.add_argument("--stop-loss-pct", type=float, default=0.30)
    p_r.add_argument("--exit-before-sec", type=float, default=30.0)
    p_r.add_argument("--min-entry-seconds-left", type=float, default=60.0)
    p_r.add_argument("--max-spread", type=float, default=0.03)
    p_r.add_argument("--min-top-ask-notional-usd", type=float, default=30.0)
    p_r.add_argument("--max-quote-age-sec", type=float, default=8.0)
    p_r.add_argument("--window-start", default=None,
                     help="ISO UTC: ignore snapshots before this (default: all)")
    p_r.add_argument("--window-end", default=None,
                     help="ISO UTC: ignore snapshots after this (default: all)")
    args = ap.parse_args()

    if args.cmd == "fetch":
        n = fetch_snapshots(args.hour, args.up_token, args.dn_token, args.out)
        print(f"exported {n} book snapshots -> {args.out}")
        return

    snaps = load_snapshots(args.snapshots)
    if args.window_start:
        ws = parse_ts(args.window_start)
        snaps = [s for s in snaps if s["ts_ms"] / 1000.0 >= ws]
    if args.window_end:
        we = parse_ts(args.window_end)
        snaps = [s for s in snaps if s["ts_ms"] / 1000.0 <= we]
    res = replay(snaps, {
        "threshold": args.threshold,
        "stake_usd": args.stake_usd,
        "stop_loss_pct": args.stop_loss_pct,
        "exit_before_sec": args.exit_before_sec,
        "min_entry_seconds_left": args.min_entry_seconds_left,
        "max_spread": args.max_spread,
        "min_top_ask_notional_usd": args.min_top_ask_notional_usd,
        "max_quote_age_sec": args.max_quote_age_sec,
        "market_end_ts": parse_ts(args.market_end),
    })
    print(_json.dumps({"metrics": res["metrics"], "skips": res["skips"],
                         "trades": res["trades"]}, indent=2))


if __name__ == "__main__":
    main()
