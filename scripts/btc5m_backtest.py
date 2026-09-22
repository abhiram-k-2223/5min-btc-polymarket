#!/usr/bin/env python3
"""Backtest harness for the BTC 5m strategy (#23).

Two subcommands:

  fetch    Export CLOB ``book`` snapshots for one market from the Pendulum
           Flow archive (no auth, DuckDB httpfs partial reads) into a local
           JSONL snapshot file. Requires the ``duckdb`` package.
  fetch-btc
           Export 1-minute BTC closes (Binance klines, stdlib-only) for a
           time range into a JSON series file, for momentum validation (#1).
  replay   Replay the entry/exit rules against a snapshot file. Stdlib-only.

The replay mirrors the live runner (scripts/test_btc_5m_session_exit_sl.py):
entry = highest best-ask >= threshold subject to spread / liquidity /
staleness gates; fill at the best ask; stop-loss monitored on the best bid;
time exit ``exit_before_sec`` before market end, filled at the best bid.

With ``--btc-series`` + ``--btc-move-usd-min`` replay instead applies the
documented momentum strategy (#1, #2, #3): the entry side follows the BTC
move since market open (the move must clear the minimum), the threshold
stays as a minimum-price floor on the picked side, and entries are vetoed
when market skew strongly opposes the momentum direction.

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

from btc5m_guards import momentum_direction, skew_veto

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
# markets: bulk closed-market listing (Gamma, stdlib-only)
# ---------------------------------------------------------------------------

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"


def fetch_closed_markets(start_ts: float, end_ts: float,
                         series_id: str = "10684") -> list[dict]:
    """List closed 5m markets in [start_ts, end_ts] via Gamma date windows.

    The endpoint caps at ~100 rows/call, so walk 8h windows (<=96 markets).
    Returns [{slug, end_ts, up_token, dn_token}].
    """
    import urllib.parse as _up
    import urllib.request as _ur

    out = []
    win = 8 * 3600.0
    cur = float(start_ts)
    import time as _time
    import urllib.error as _uer
    while cur < end_ts:
        wend = min(cur + win, float(end_ts))
        qs = _up.urlencode({
            "series_id": series_id, "closed": "true", "limit": 100,
            "end_date_min": _dt.datetime.fromtimestamp(
                cur, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date_max": _dt.datetime.fromtimestamp(
                wend, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        req = _ur.Request(f"{GAMMA_EVENTS_URL}?{qs}",
                          headers={"User-Agent": "btc5m-backtest/1.0"})
        last_err = None
        events = None
        for attempt in range(4):
            try:
                with _ur.urlopen(req, timeout=30) as resp:
                    events = _json.loads(resp.read().decode("utf-8"))
                last_err = None
                break
            except ( _uer.URLError, ConnectionError, TimeoutError) as e:
                last_err = e
                _time.sleep(2.0 * (attempt + 1))
        if last_err is not None:
            print(f"window {cur}: Gamma failed after retries ({last_err}); skipped",
                  file=_sys.stderr)
            cur = wend
            continue
        for ev in events or []:
            for m in ev.get("markets") or []:
                try:
                    toks = m.get("clobTokenIds") or []
                    if isinstance(toks, str):
                        toks = _json.loads(toks)
                    end = _dt.datetime.fromisoformat(
                        str(m.get("endDate")).replace("Z", "+00:00")).timestamp()
                except (TypeError, ValueError):
                    continue
                if len(toks) >= 2 and start_ts <= end <= end_ts:
                    out.append({"slug": ev.get("slug"), "end_ts": end,
                                "up_token": str(toks[0]),
                                "dn_token": str(toks[1])})
        cur = wend
    # de-dupe by slug (window edges can overlap), sort by end time
    seen = {}
    for m in out:
        seen[m["slug"]] = m
    return sorted(seen.values(), key=lambda m: m["end_ts"])


def fetch_hour_markets(hour: str, markets: list[dict], out_dir: str) -> dict:
    """One DuckDB query per hour for many markets; split rows per market.

    ``markets`` entries need up_token/dn_token/slug. Writes
    ``<out_dir>/<slug>.jsonl``. Returns {slug: row_count}.
    """
    try:
        import duckdb  # lazy: replay stays stdlib-only
    except ImportError:
        print("bulk-fetch needs the 'duckdb' package (pip install duckdb)",
              file=_sys.stderr)
        raise SystemExit(2)
    day, hh = hour.split("T")
    url = PENDULUM_URL.format(day=day, hh=hh)
    hexes = []
    hex_to_slug = {}
    for m in markets:
        for tok, side in ((m["up_token"], "up"), (m["dn_token"], "dn")):
            hx = token_to_asset_hex(tok).lower()
            hexes.append(hx)
            hex_to_slug[hx] = (m["slug"], side)
    ors = " OR ".join(f"asset_id = unhex('{hx}')" for hx in hexes)
    con = duckdb.connect()
    rows = con.execute(
        "SELECT epoch_ms(timestamp) AS ts_ms, hex(asset_id) AS asset,"
        " (SELECT max((u).price) FROM unnest(bids) AS t(u)) AS bb,"
        " (SELECT min((u).price) FROM unnest(asks) AS t(u)) AS ba,"
        " (SELECT (u).size FROM unnest(asks) AS t(u)"
        "   ORDER BY (u).price LIMIT 1) AS ask_sz"
        f" FROM '{url}' WHERE event_type = 'book' AND ({ors})"
        " ORDER BY timestamp"
    ).fetchall()
    _os.makedirs(out_dir, exist_ok=True)
    per_market: dict[str, list] = {}
    for ts_ms, asset, bb, ba, ask_sz in rows:
        hit = hex_to_slug.get(str(asset).lower())
        if hit is None:
            continue
        slug, side = hit
        per_market.setdefault(slug, []).append(_json.dumps({
            "ts_ms": int(ts_ms), "side": side,
            "best_bid": float(bb) if bb is not None else None,
            "best_ask": float(ba) if ba is not None else None,
            "ask_size": float(ask_sz) if ask_sz is not None else None,
        }))
    counts = {}
    for slug, lines in per_market.items():
        with open(_os.path.join(out_dir, f"{slug}.jsonl"), "w",
                  encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        counts[slug] = len(lines)
    return counts

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
        " (SELECT max((u).price) FROM unnest(bids) AS t(u)) AS bb,"
        " (SELECT min((u).price) FROM unnest(asks) AS t(u)) AS ba,"
        " (SELECT (u).size FROM unnest(asks) AS t(u)"
        "   ORDER BY (u).price LIMIT 1) AS ask_sz"
        f" FROM '{url}' WHERE event_type = 'book'"
        f" AND (asset_id = unhex('{up_hex}') OR asset_id = unhex('{dn_hex}'))"
        " ORDER BY timestamp"
    ).fetchall()
    _os.makedirs(_os.path.dirname(_os.path.abspath(out_path)), exist_ok=True)
    n = 0
    with open(out_path, "w", encoding="utf-8") as fh:
        for ts_ms, asset, bb, ba, ask_sz in rows:
            side = "up" if asset.lower() == up_hex else "dn"
            fh.write(_json.dumps({
                "ts_ms": int(ts_ms), "side": side,
                "best_bid": float(bb) if bb is not None else None,
                "best_ask": float(ba) if ba is not None else None,
                "ask_size": float(ask_sz) if ask_sz is not None else None,
            }) + "\n")
            n += 1
    return n


# ---------------------------------------------------------------------------
# BTC series (momentum validation, #1)
# ---------------------------------------------------------------------------

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"


def fetch_btc_series(start_ts: float, end_ts: float, out_path: str,
                      symbol: str = "BTCUSDT") -> int:
    """Fetch 1-minute BTC closes for [start_ts, end_ts] via Binance klines.

    Stdlib-only (urllib). Writes {"t": open_epoch_sec, "close": px} rows as
    JSON lines. Returns the row count.
    """
    import urllib.parse as _up
    import urllib.request as _ur

    out = []
    start_ms = int(start_ts * 1000)
    end_ms = int(end_ts * 1000)
    while True:
        qs = _up.urlencode({"symbol": symbol, "interval": "1m",
                            "startTime": start_ms, "endTime": end_ms,
                            "limit": 1000})
        req = _ur.Request(f"{BINANCE_KLINES_URL}?{qs}",
                          headers={"User-Agent": "btc5m-backtest/1.0"})
        with _ur.urlopen(req, timeout=20) as resp:
            klines = _json.loads(resp.read().decode("utf-8"))
        if not klines:
            break
        for k in klines:
            out.append({"t": k[0] / 1000.0, "close": float(k[4])})
        if len(klines) < 1000:
            break
        start_ms = int(klines[-1][0]) + 60_000
        if start_ms > end_ms:
            break
    _os.makedirs(_os.path.dirname(_os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        for row in out:
            fh.write(_json.dumps(row) + "\n")
    return len(out)


def load_btc_series(path: str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                row = _json.loads(line)
                out.append({"t": float(row["t"]), "close": float(row["close"])})
    return sorted(out, key=lambda r: r["t"])


def btc_at(series: list[dict], ts: float, _times=None):
    """Last series close at or before ``ts`` (None when series is empty or
    starts after ``ts``). Bisect; pass ``_times`` (precomputed [r['t']])
    when calling in a hot loop so the key list isn't rebuilt per call."""
    import bisect as _bi

    if not series:
        return None
    times = _times if _times is not None else [r["t"] for r in series]
    i = _bi.bisect_right(times, ts) - 1
    return series[i]["close"] if i >= 0 else None


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
    """Executable top of book for a snapshot row.

    New compact rows (from fetch/bulk-fetch) carry precomputed tops, so no
    ladder parsing is needed. Legacy rows with ``bids``/``asks`` ladder
    strings are still supported: the archive's top-level best_bid/best_ask
    columns are null for whole hours, so tops derive from the ladders
    (best bid = max bid, best ask = min ask — mirrors _top_of_book).
    """
    if "bids" not in snap and "asks" not in snap:
        bid = snap.get("best_bid")
        ask = snap.get("best_ask")
        ask_size = snap.get("ask_size")
    else:
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
    # Simulated fill cost (#23, large-sample runs): price points added to
    # the entry ask and subtracted from the exit bid. Covers spread
    # crossing + taker slippage (Polymarket charges no trading fees:
    # feesEnabled=False on these markets). 0.0 = quoted-price fills.
    slip = float(params.get("fill_slippage_usd") or 0.0)
    # Momentum-strategy mode (#1, #2, #3): side follows the BTC move since
    # market open instead of the highest share price. Off (None) = legacy
    # share-price trigger, kept for comparison runs.
    btc_series = params.get("btc_series") or []
    btc_move_min = params.get("btc_move_usd_min")
    skew_veto_th = params.get("skew_veto_threshold", 0.10)
    momentum_mode = bool(btc_series) and btc_move_min is not None
    market_open_ts = end_ts - 300.0
    btc_times = [r["t"] for r in btc_series] if momentum_mode else []
    btc_open = btc_at(btc_series, market_open_ts, btc_times) if momentum_mode else None
    if momentum_mode and btc_open is None and btc_series:
        btc_open = btc_series[0]["close"]

    by_side = {"up": [], "dn": []}
    for s in snapshots:
        if s.get("side") in by_side:
            by_side[s["side"]].append(s)

    # Merge both sides in time order; each step carries the latest known
    # quote per side (like the runner's per-poll snapshot). The index
    # tiebreak keeps order deterministic when archive rows share a timestamp.
    events = sorted(
        [(s["ts_ms"], s["side"], i, s) for side in by_side
         for i, s in enumerate(by_side[side])])
    latest: dict[str, dict] = {}
    prev_ts = {}
    skips: dict[str, int] = {}
    trades = []
    in_pos = None

    def skip(reason):
        skips[reason] = skips.get(reason, 0) + 1

    for ts_ms, side, _i, snap in events:
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
            btc_move = None
            if momentum_mode:
                btc_now = btc_at(btc_series, now, btc_times)
                mom_side, btc_move = momentum_direction(
                    btc_open, btc_now, float(btc_move_min or 0.0))
                if mom_side is None:
                    skip("no_btc_momentum")
                    continue
                pick_side = "up" if mom_side == "UP" else "dn"
                ask = latest[pick_side]["best_ask"]
                if ask is None or ask < threshold:
                    skip("below_threshold")
                    continue
                if skew_veto(mom_side, latest["up"]["best_ask"],
                             latest["dn"]["best_ask"], skew_veto_th):
                    skip("skew_veto")
                    continue
            else:
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
            entry_px = ask + slip
            in_pos = {"side": pick_side, "entry_ts": now, "entry_price": entry_px,
                      "shares": stake / entry_px if entry_px > 0 else 0,
                      "cost": stake,
                      "sl_price": ask * (1.0 - sl_pct),
                      "btc_move_usd_at_entry": btc_move}
        else:
            top = latest[in_pos["side"]]
            bid = top["best_bid"]
            if now >= exit_ts:
                reason = "time_exit"
            elif bid is not None and bid <= in_pos["sl_price"]:
                reason = "stop_loss"
            else:
                continue
            exit_px = max(0.0, (bid if bid is not None else 0.0) - slip)
            proceeds = in_pos["shares"] * exit_px
            trades.append({
                "side": in_pos["side"],
                "entry_ts": in_pos["entry_ts"], "exit_ts": now,
                "entry_price": round(in_pos["entry_price"], 4),
                "exit_price": round(exit_px, 4),
                "exit_reason": reason,
                "pnl_usdc": round(proceeds - in_pos["cost"], 4),
                "btc_move_usd_at_entry": in_pos.get("btc_move_usd_at_entry"),
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


def bulk_replay(markets: list[dict], snaps_dir: str, btc_series: list[dict],
                params: dict) -> dict:
    """Replay every market with snapshots; aggregate expectancy stats.

    Markets with no snapshot file are counted as ``missing_data``, not as
    no-trade markets. Returns per-market rows + aggregate metrics.
    """
    rows = []
    all_pnls = []
    for m in markets:
        path = _os.path.join(snaps_dir, f"{m['slug']}.jsonl")
        if not _os.path.exists(path):
            rows.append({"slug": m["slug"], "end_ts": m["end_ts"],
                         "status": "missing_data"})
            continue
        snaps = load_snapshots(path)
        if not snaps:
            rows.append({"slug": m["slug"], "end_ts": m["end_ts"],
                         "status": "empty_data"})
            continue
        p = dict(params)
        p["market_end_ts"] = m["end_ts"]
        p["btc_series"] = btc_series
        res = replay(snaps, p)
        for t in res["trades"]:
            if t["exit_reason"] != "end_of_data":
                all_pnls.append(t["pnl_usdc"])
        rows.append({"slug": m["slug"], "end_ts": m["end_ts"],
                     "status": "ok", "n_trades": res["metrics"]["n_trades"],
                     "pnl_usdc": res["metrics"]["total_pnl_usdc"],
                     "win_rate": res["metrics"]["win_rate"],
                     "end_of_data": res["metrics"]["end_of_data_positions"],
                     "top_skips": sorted(res["skips"].items(),
                                         key=lambda kv: kv[1], reverse=True)[:3]})
    wins = sum(1 for x in all_pnls if x > 0)
    cum = peak = max_dd = 0.0
    for x in all_pnls:
        cum += x
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    n_mkt = sum(1 for r in rows if r["status"] == "ok")
    return {
        "markets": rows,
        "aggregate": {
            "markets_ok": n_mkt,
            "markets_missing_data": sum(
                1 for r in rows if r["status"] != "ok"),
            "n_trades": len(all_pnls),
            "wins": wins,
            "win_rate": round(wins / len(all_pnls), 4) if all_pnls else None,
            "total_pnl_usdc": round(sum(all_pnls), 4),
            "avg_pnl_usdc": round(sum(all_pnls) / len(all_pnls), 4)
            if all_pnls else None,
            "max_drawdown_usdc": round(max_dd, 4),
            "avg_trades_per_market": round(len(all_pnls) / n_mkt, 3)
            if n_mkt else None,
        },
    }


def _replay_params_from_args(args) -> dict:
    return {
        "threshold": args.threshold,
        "stake_usd": args.stake_usd,
        "stop_loss_pct": args.stop_loss_pct,
        "exit_before_sec": args.exit_before_sec,
        "min_entry_seconds_left": args.min_entry_seconds_left,
        "max_spread": args.max_spread,
        "min_top_ask_notional_usd": args.min_top_ask_notional_usd,
        "max_quote_age_sec": args.max_quote_age_sec,
        "fill_slippage_usd": args.fill_slippage_usd,
        "btc_move_usd_min": args.btc_move_usd_min,
        "skew_veto_threshold": args.skew_veto_threshold,
    }


def _add_strategy_args(p) -> None:
    p.add_argument("--threshold", type=float, default=0.70)
    p.add_argument("--stake-usd", type=float, default=5.0)
    p.add_argument("--stop-loss-pct", type=float, default=0.30)
    p.add_argument("--exit-before-sec", type=float, default=40.0)
    p.add_argument("--min-entry-seconds-left", type=float, default=60.0)
    p.add_argument("--max-spread", type=float, default=0.03)
    p.add_argument("--min-top-ask-notional-usd", type=float, default=30.0)
    p.add_argument("--max-quote-age-sec", type=float, default=8.0)
    p.add_argument("--fill-slippage-usd", type=float, default=0.0,
                   help="Price points added to entry / cut from exit (#23)")
    p.add_argument("--btc-series", default=None,
                   help="JSON 1m BTC series (fetch-btc output): enables momentum mode")
    p.add_argument("--btc-move-usd-min", type=float, default=None,
                   help="Min |BTC move| since market open to allow entry (#1)")
    p.add_argument("--skew-veto-threshold", type=float, default=0.10,
                   help="Veto entry when skew opposes momentum beyond this (#2)")


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
    _add_strategy_args(p_r)
    p_r.add_argument("--window-start", default=None,
                     help="ISO UTC: ignore snapshots before this (default: all)")
    p_r.add_argument("--window-end", default=None,
                     help="ISO UTC: ignore snapshots after this (default: all)")

    p_m = sub.add_parser("markets", help="List closed 5m markets (Gamma)")
    p_m.add_argument("--start", required=True, help="ISO UTC range start")
    p_m.add_argument("--end", required=True, help="ISO UTC range end")
    p_m.add_argument("--out", required=True)

    p_bf = sub.add_parser("bulk-fetch", help="Fetch snapshots for many markets")
    p_bf.add_argument("--markets", required=True, help="markets JSON from 'markets'")
    p_bf.add_argument("--out-dir", required=True)

    p_br = sub.add_parser("bulk-replay", help="Replay many markets, aggregate")
    p_br.add_argument("--markets", required=True)
    p_br.add_argument("--snaps-dir", required=True)
    p_br.add_argument("--out", required=True, help="Aggregate JSON output path")
    _add_strategy_args(p_br)

    p_b = sub.add_parser("fetch-btc", help="Export 1m BTC closes (Binance)")
    p_b.add_argument("--start", required=True, help="ISO UTC range start")
    p_b.add_argument("--end", required=True, help="ISO UTC range end")
    p_b.add_argument("--out", required=True)
    args = ap.parse_args()

    if args.cmd == "fetch":
        n = fetch_snapshots(args.hour, args.up_token, args.dn_token, args.out)
        print(f"exported {n} book snapshots -> {args.out}")
        return

    if args.cmd == "fetch-btc":
        n = fetch_btc_series(parse_ts(args.start), parse_ts(args.end), args.out)
        print(f"exported {n} BTC 1m closes -> {args.out}")
        return

    if args.cmd == "markets":
        ms = fetch_closed_markets(parse_ts(args.start), parse_ts(args.end))
        _os.makedirs(_os.path.dirname(_os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            _json.dump(ms, fh)
        print(f"listed {len(ms)} closed markets -> {args.out}")
        return

    if args.cmd == "bulk-fetch":
        with open(args.markets, encoding="utf-8") as fh:
            ms = _json.load(fh)
        by_hour: dict[str, list] = {}
        for m in ms:
            end = _dt.datetime.fromtimestamp(m["end_ts"], tz=UTC)
            # a market's window can straddle two UTC hours: fetch both
            for hh in {end.strftime("%Y-%m-%dT%H"),
                       (end - _dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H")}:
                by_hour.setdefault(hh, []).append(m)
        total = 0
        for hh in sorted(by_hour):
            try:
                counts = fetch_hour_markets(hh, by_hour[hh], args.out_dir)
            except Exception as e:
                print(f"hour {hh}: fetch failed ({e}); skipped")
                continue
            total += sum(counts.values())
            print(f"hour {hh}: {len(counts)} markets, {sum(counts.values())} rows")
        print(f"bulk-fetch done: {total} rows -> {args.out_dir}")
        return

    if args.cmd == "bulk-replay":
        with open(args.markets, encoding="utf-8") as fh:
            ms = _json.load(fh)
        btc = load_btc_series(args.btc_series) if args.btc_series else []
        params = _replay_params_from_args(args)
        agg = bulk_replay(ms, args.snaps_dir, btc, params)
        _os.makedirs(_os.path.dirname(_os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            _json.dump(agg, fh, indent=2)
        print(_json.dumps(agg["aggregate"], indent=2))
        return

    snaps = load_snapshots(args.snapshots)
    if args.window_start:
        ws = parse_ts(args.window_start)
        snaps = [s for s in snaps if s["ts_ms"] / 1000.0 >= ws]
    if args.window_end:
        we = parse_ts(args.window_end)
        snaps = [s for s in snaps if s["ts_ms"] / 1000.0 <= we]
    res = replay(snaps, {
        **_replay_params_from_args(args),
        "market_end_ts": parse_ts(args.market_end),
        "btc_series": load_btc_series(args.btc_series) if args.btc_series else [],
    })
    print(_json.dumps({"metrics": res["metrics"], "skips": res["skips"],
                         "trades": res["trades"]}, indent=2))


if __name__ == "__main__":
    main()
