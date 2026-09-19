#!/usr/bin/env python3
"""Structured trade ledger for the BTC 5m runner (#25).

SQLite store (stdlib only) replacing log-file JSON scraping as the source
of truth for trade history. Lives at <runtime>/btc5m_trades.sqlite.

Usage:
    python scripts/btc5m_tradedb.py recent --runtime-dir runtime --limit 10
    python scripts/btc5m_tradedb.py summary --runtime-dir runtime --days 7
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os as _os
import sqlite3 as _sqlite3

DB_FILENAME = "btc5m_trades.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    mode TEXT NOT NULL DEFAULT 'dry',
    market_slug TEXT,
    side TEXT,
    token_id TEXT,
    entry_price REAL,
    shares REAL,
    cost_usdc REAL,
    close_reason TEXT,
    close_usdc REAL,
    pnl_usdc REAL,
    btc_entry REAL,
    btc_exit REAL,
    open_order_id TEXT,
    close_tx TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_opened ON trades(opened_at);
"""


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db_path(runtime_dir: str) -> str:
    return _os.path.join(str(runtime_dir), DB_FILENAME)


def connect(runtime_dir: str) -> _sqlite3.Connection:
    _os.makedirs(str(runtime_dir), exist_ok=True)
    con = _sqlite3.connect(db_path(runtime_dir))
    con.execute("PRAGMA journal_mode=WAL;")
    con.executescript(_SCHEMA)
    return con


def record_open(
    con: _sqlite3.Connection,
    *,
    mode: str,
    market_slug=None,
    side=None,
    token_id=None,
    entry_price=None,
    shares=None,
    cost_usdc=None,
    btc_entry=None,
    open_order_id=None,
) -> int:
    cur = con.execute(
        "INSERT INTO trades (opened_at, mode, market_slug, side, token_id,"
        " entry_price, shares, cost_usdc, btc_entry, open_order_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (utc_now_iso(), mode, market_slug, side, token_id, entry_price,
         shares, cost_usdc, btc_entry, open_order_id),
    )
    con.commit()
    if cur.lastrowid is None:  # pragma: no cover - defensive
        raise RuntimeError("trade insert returned no row id")
    return int(cur.lastrowid)


def record_close(
    con: _sqlite3.Connection,
    trade_id: int,
    *,
    close_reason=None,
    close_usdc=None,
    pnl_usdc=None,
    btc_exit=None,
    close_tx=None,
) -> None:
    con.execute(
        "UPDATE trades SET closed_at=?, close_reason=?, close_usdc=?,"
        " pnl_usdc=?, btc_exit=?, close_tx=? WHERE id=?",
        (utc_now_iso(), close_reason, close_usdc, pnl_usdc, btc_exit, close_tx, trade_id),
    )
    con.commit()


def recent(con: _sqlite3.Connection, limit: int = 20) -> list[dict]:
    con.row_factory = _sqlite3.Row
    rows = con.execute(
        "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (int(limit),)
    ).fetchall()
    return [dict(r) for r in rows]


def daily_summary(con: _sqlite3.Connection, days: int = 7) -> list[dict]:
    con.row_factory = _sqlite3.Row
    rows = con.execute(
        "SELECT substr(opened_at, 1, 10) AS day, mode,"
        " COUNT(*) AS trades, SUM(pnl_usdc) AS pnl_usdc,"
        " SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END) AS wins"
        " FROM trades WHERE opened_at >= date('now', ?)"
        " GROUP BY day, mode ORDER BY day DESC",
        (f"-{int(days)} days",),
    ).fetchall()
    return [dict(r) for r in rows]


def main() -> None:
    ap = argparse.ArgumentParser(description="Query the BTC 5m trade ledger")
    ap.add_argument("--runtime-dir", default=None,
                    help="Runtime dir (default: <repo>/runtime)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_recent = sub.add_parser("recent", help="List recent trades")
    p_recent.add_argument("--limit", type=int, default=20)
    p_recent.add_argument("--runtime-dir", default=None)
    p_sum = sub.add_parser("summary", help="Per-day PnL summary")
    p_sum.add_argument("--days", type=int, default=7)
    p_sum.add_argument("--runtime-dir", default=None)
    args = ap.parse_args()

    runtime_dir = args.runtime_dir or _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "runtime")
    con = connect(runtime_dir)
    if args.cmd == "recent":
        rows = recent(con, args.limit)
        for t in rows:
            print(f"#{t['id']} {t['opened_at']} [{t['mode']}] {t['side']} "
                  f"{t['market_slug']} entry={t['entry_price']} "
                  f"exit={t['close_reason']} pnl={t['pnl_usdc']}")
        if not rows:
            print("no trades recorded")
    else:
        rows = daily_summary(con, args.days)
        for s in rows:
            print(f"{s['day']} [{s['mode']}] trades={s['trades']} "
                  f"wins={s['wins']} pnl={s['pnl_usdc']}")
        if not rows:
            print("no trades recorded")


if __name__ == "__main__":
    main()
