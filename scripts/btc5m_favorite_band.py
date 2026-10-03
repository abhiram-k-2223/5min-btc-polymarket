#!/usr/bin/env python3
"""Favorite-band entry predicate + sizing (spec 3a). Stdlib-only.

Ports the deleted #2 sillygoose mid-band edge with #1 sizing: buy the
favorite side when the book leans to it at 0.50-0.70 (prefer 0.52-0.57)
and BTC momentum since slot open agrees. Fixed $10 paper stake, capped
at 5% equity once equity > 200, floored at $1. No stop-loss on this leg.
"""
from __future__ import annotations

BAND_LOW = 0.50
BAND_HIGH = 0.70
PREFER_LOW = 0.52
PREFER_HIGH = 0.57
DEFAULT_STAKE_USD = 10.0


def _fnum(v, default=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return f


def prefer_zone(fav_ask, low: float = PREFER_LOW, high: float = PREFER_HIGH) -> bool:
    px = _fnum(fav_ask, None)
    if px is None:
        return False
    return float(low) <= px <= float(high)


def allow_entry(*, side_gap_usd=None, fav_ask=None, min_gap_usd: float = 70.0,
                band_low: float = BAND_LOW, band_high: float = BAND_HIGH) -> bool:
    gap = _fnum(side_gap_usd, None)
    px = _fnum(fav_ask, None)
    mg = _fnum(min_gap_usd, 70.0)
    if gap is None or px is None or mg is None:
        return False
    if abs(gap) < abs(mg):
        return False
    return float(band_low) <= px <= float(band_high)


def size_for_band(*, equity_usd=None, stake_usd: float = DEFAULT_STAKE_USD,
                  equity_cap_pct: float = 5.0, floor_usd: float = 1.0) -> float:
    try:
        cap_fixed = max(0.0, float(stake_usd))
    except (TypeError, ValueError):
        cap_fixed = DEFAULT_STAKE_USD
    eq = _fnum(equity_usd, None)
    if eq is None:
        return max(float(floor_usd), min(cap_fixed, DEFAULT_STAKE_USD))
    if eq <= 0:
        return float(floor_usd)
    try:
        cap_pct = max(0.0, float(eq) * float(equity_cap_pct) / 100.0)
    except (TypeError, ValueError):
        cap_pct = cap_fixed
    if eq <= 200.0:
        sized = min(cap_fixed, DEFAULT_STAKE_USD)
    else:
        sized = min(cap_fixed, cap_pct) if cap_pct > 0 else cap_fixed
    return max(float(floor_usd), float(sized))


def decision_json(*, mode: str, side: str, gap_usd=None, fav_ask=None,
                  prefer: bool = False, reason: str = "ok") -> dict:
    return {"mode": mode, "side": side, "gap_usd": gap_usd,
            "fav_ask": fav_ask, "prefer_zone": bool(prefer), "reason": reason}


def select_mode(*, entry_mode: str, fav_ask=None, side_ask=None,
                band_max_ask: float = 0.70, base_threshold: float = 0.60) -> str:
    m = (entry_mode or "both").lower()
    fav = _fnum(fav_ask, None)
    ask = _fnum(side_ask, None)
    band_ok = fav is not None and fav <= float(band_max_ask) and fav >= BAND_LOW
    base_ok = ask is not None and ask >= float(base_threshold)
    if m == "band":
        return "band" if band_ok else "skip"
    if m == "base":
        return "base" if base_ok else "skip"
    if band_ok:
        return "band"
    if base_ok:
        return "base"
    return "skip"
