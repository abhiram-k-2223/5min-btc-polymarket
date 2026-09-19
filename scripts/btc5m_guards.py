#!/usr/bin/env python3
"""Risk-ledger and pre-entry gate helpers for the BTC 5m runner.

Stdlib-only on purpose: the canonical runner needs ``requests`` and
``py_clob_client``, but everything in here must stay importable (and
unit-testable) anywhere, including machines without those packages.

Covers problems.md items:
  #4  daily max-loss enforcement (via JSON risk ledger)
  #5  max trades-per-day enforcement (via JSON risk ledger)
  #6  spread guard
  #7  top-of-book liquidity guard
  #8  quote staleness guard (best-effort)
  #9  consecutive-error abort (streak predicate)
"""

from __future__ import annotations

import datetime as _dt
import json as _json
import os as _os


LEDGER_FILENAME = "btc5m_risk_ledger.json"


# ---------------------------------------------------------------------------
# Risk ledger (items #4, #5)
# ---------------------------------------------------------------------------

def utc_today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def ledger_path(runtime_dir: str) -> str:
    return _os.path.join(str(runtime_dir), LEDGER_FILENAME)


def fresh_ledger(today: str) -> dict:
    return {"date": today, "trades_taken": 0, "realized_pnl_usdc": 0.0}


def load_ledger(path: str, today: str) -> dict:
    """Load the ledger, failing open to a fresh one.

    A corrupt file, a missing file, or a ledger from a previous UTC day
    all yield a fresh ledger for ``today``. Fail-open is deliberate: a
    broken ledger file must never brick the runner at startup; the
    in-loop checks below still gate each entry while the process runs.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = _json.load(fh)
        if not isinstance(data, dict):
            return fresh_ledger(today)
        if data.get("date") != today:
            return fresh_ledger(today)
        return {
            "date": today,
            "trades_taken": int(data.get("trades_taken") or 0),
            "realized_pnl_usdc": float(data.get("realized_pnl_usdc") or 0.0),
        }
    except Exception:
        return fresh_ledger(today)


def save_ledger(path: str, ledger: dict) -> bool:
    """Persist the ledger atomically (tmp file + replace)."""
    try:
        parent = _os.path.dirname(path)
        if parent:
            _os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            _json.dump(ledger, fh)
        _os.replace(tmp, path)
        return True
    except Exception:
        return False


def can_open(
    ledger: dict,
    *,
    max_trades_per_day: int,
    daily_max_loss_pct: float,
    equity_usd: float,
) -> tuple[bool, str]:
    """Return (allowed, reason) for opening a new position today."""
    try:
        trades = int(ledger.get("trades_taken") or 0)
    except (TypeError, ValueError):
        trades = 0
    try:
        pnl = float(ledger.get("realized_pnl_usdc") or 0.0)
    except (TypeError, ValueError):
        pnl = 0.0
    if trades >= int(max_trades_per_day):
        return False, "max_trades_per_day_reached"
    cap_usd = float(equity_usd) * float(daily_max_loss_pct) / 100.0
    if pnl <= -cap_usd:
        return False, "daily_max_loss_hit"
    return True, "ok"


def record_open(ledger: dict) -> dict:
    ledger["trades_taken"] = int(ledger.get("trades_taken") or 0) + 1
    return ledger


def record_close(ledger: dict, pnl_usdc) -> dict:
    try:
        pnl = float(pnl_usdc) if pnl_usdc is not None else 0.0
    except (TypeError, ValueError):
        pnl = 0.0
    try:
        cur = float(ledger.get("realized_pnl_usdc") or 0.0)
    except (TypeError, ValueError):
        cur = 0.0
    ledger["realized_pnl_usdc"] = round(cur + pnl, 6)
    return ledger


# ---------------------------------------------------------------------------
# Pre-entry gates (items #6, #7, #8)
# ---------------------------------------------------------------------------

def spread_gate(spread, max_spread: float) -> tuple[bool, str]:
    """Spread must be known and within ``max_spread``."""
    if spread is None:
        return False, "no_book"
    try:
        if float(spread) <= float(max_spread):
            return True, "ok"
    except (TypeError, ValueError):
        return False, "bad_spread"
    return False, "spread_too_wide"


def liquidity_gate(notional_usd, minimum_usd: float) -> tuple[bool, str]:
    """Top-of-book ask notional must meet the minimum."""
    if notional_usd is None:
        return False, "no_book"
    try:
        if float(notional_usd) >= float(minimum_usd):
            return True, "ok"
    except (TypeError, ValueError):
        return False, "bad_notional"
    return False, "liquidity_too_thin"


def staleness_gate(age_sec, max_age_sec: float) -> tuple[bool, str]:
    """Quote age must be within ``max_age_sec``.

    ``None`` (unknown age) is allowed through: many books carry no usable
    timestamp, and blocking on unknown age would brick trading. The age is
    still logged so staleness stays observable.
    """
    if age_sec is None:
        return True, "age_unknown"
    try:
        if float(age_sec) <= float(max_age_sec):
            return True, "ok"
    except (TypeError, ValueError):
        return True, "age_unknown"
    return False, "quote_stale"


# ---------------------------------------------------------------------------
# Consecutive-error budget (item #9)
# ---------------------------------------------------------------------------

def error_budget_exceeded(streak: int, max_allowed: int) -> bool:
    try:
        return int(streak) >= int(max_allowed)
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Open-position state file (item #33 — crash recovery)
# ---------------------------------------------------------------------------
#
# The runner writes this file when a position opens and deletes it on close.
# A leftover file means the process died mid-position: `--resume` picks the
# recorded position back up instead of opening a second one.

OPEN_POSITION_FILENAME = "open_position.json"


def open_position_path(runtime_dir: str) -> str:
    return _os.path.join(str(runtime_dir), OPEN_POSITION_FILENAME)


def save_open_position(runtime_dir: str, position: dict) -> str:
    import json as _json

    _os.makedirs(str(runtime_dir), exist_ok=True)
    path = open_position_path(runtime_dir)
    with open(path, "w", encoding="utf-8") as fh:
        _json.dump(position, fh)
    return path


def load_open_position(runtime_dir: str):
    import json as _json

    path = open_position_path(runtime_dir)
    if not _os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            obj = _json.load(fh)
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def clear_open_position(runtime_dir: str) -> bool:
    path = open_position_path(runtime_dir)
    try:
        _os.unlink(path)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Data-API position filter (item #33 — pre-entry duplicate check)
# ---------------------------------------------------------------------------
#
# py-clob-client exposes no holdings query, so the pre-entry duplicate check
# uses the public data-api positions endpoint (no auth needed, just the
# wallet address). Pure filter below stays unit-testable; the HTTP call
# lives in the runner and fails open.

def position_in_tokens(positions, token_ids) -> dict | None:
    """Return the first position whose asset/token matches ``token_ids``
    with a non-trivial size, else None. ``positions`` is the decoded
    data-api JSON list."""
    if not isinstance(positions, list):
        return None
    wanted = {str(t) for t in (token_ids or []) if t}
    for p in positions:
        if not isinstance(p, dict):
            continue
        asset = str(p.get("asset") or p.get("tokenID") or p.get("tokenId") or "")
        if asset not in wanted:
            continue
        try:
            size = float(p.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if size > 1e-9:
            return p
    return None
