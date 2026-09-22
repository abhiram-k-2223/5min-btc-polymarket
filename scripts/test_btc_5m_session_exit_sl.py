#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import subprocess
import time
from typing import Any, Optional
from pathlib import Path

import requests

from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import ApiCreds

from btc5m_guards import (
    can_open,
    clear_open_position,
    error_budget_exceeded,
    ledger_path,
    liquidity_gate,
    load_ledger,
    load_open_position,
    momentum_direction,
    position_in_tokens,
    record_close,
    record_open,
    save_ledger,
    save_open_position,
    skew_veto,
    spread_gate,
    staleness_gate,
    utc_today,
)

import btc5m_alerts as alerts
import btc5m_tradedb as tradedb

UTC = dt.timezone.utc


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def ts_utc() -> str:
    return now_utc().isoformat().replace('+00:00', 'Z')


# Cap for the in-report attempts array (#34). Heartbeat entries are the
# noisiest, so the oldest heartbeats are dropped first; every drop is
# counted in report['attempts_dropped'] so nothing is silently lost.
ATTEMPT_CAP = 1000


def log_attempt(report: dict[str, Any], entry: dict[str, Any]) -> None:
    report['attempts'].append(entry)  # direct list op: this IS the append path
    if len(report['attempts']) > ATTEMPT_CAP:
        for i, a in enumerate(report['attempts']):
            if isinstance(a, dict) and a.get('status') == 'heartbeat':
                del report['attempts'][i]
                break
        else:
            del report['attempts'][0]
        report['attempts_dropped'] = report.get('attempts_dropped', 0) + 1


def btc_spot_usd(timeout: float = 8.0) -> Optional[float]:
    """Spot BTC/USD for trade context (#26). Binance first, Coinbase
    fallback. Best-effort: returns None on any failure, never raises."""
    sources = [
        ('https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT',
         lambda j: (j or {}).get('price')),
        ('https://api.coinbase.com/v2/prices/BTC-USD/spot',
         lambda j: ((j or {}).get('data') or {}).get('amount')),
    ]
    for url, pick in sources:
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code != 200:
                continue
            px = _fnum(pick(r.json()))
            if px is not None and px > 0:
                return px
        except Exception:
            continue
    return None


DATA_API_POSITIONS_URL = 'https://data-api.polymarket.com/positions'


def wallet_positions(wallet: str, timeout: float = 10.0) -> list:
    """Positions for a wallet via the public data-api (#33, pre-entry
    duplicate check). py-clob-client exposes no holdings query, so this
    unauthenticated endpoint is used. Returns a list (possibly empty);
    never raises — callers treat failure as 'unknown' and fail open."""
    try:
        r = requests.get(DATA_API_POSITIONS_URL, params={'user': wallet},
                         timeout=timeout)
        if r.status_code != 200:
            return []
        obj = r.json()
        return obj if isinstance(obj, list) else []
    except Exception:
        return []


BINANCE_KLINES_URL = 'https://api.binance.com/api/v3/klines'

# Per-market 1m BTC close cache (#1): refreshed at most every 30s so each
# poll does not cost an HTTP round-trip.
_btc_klines_cache: dict[str, tuple[float, list]] = {}


def fetch_btc_klines_1m(start_ts: float, end_ts: float,
                        timeout: float = 10.0) -> list:
    """1-minute (open_epoch, close) BTC klines via Binance (#1, momentum
    reference). Returns [] on any failure — callers fail closed."""
    try:
        r = requests.get(BINANCE_KLINES_URL,
                         params={'symbol': 'BTCUSDT', 'interval': '1m',
                                 'startTime': int(start_ts * 1000),
                                 'endTime': int(end_ts * 1000), 'limit': 1000},
                         timeout=timeout)
        if r.status_code != 200:
            return []
        out = []
        for k in r.json():
            try:
                out.append((float(k[0]) / 1000.0, float(k[4])))
            except (TypeError, ValueError, IndexError):
                continue
        return out
    except Exception:
        return []


def btc_series_cached(slug: str, start_ts: float, end_ts: float,
                      max_age_sec: float = 30.0) -> list:
    now = time.time()
    hit = _btc_klines_cache.get(slug)
    if hit and (now - hit[0]) < max_age_sec and hit[1]:
        return hit[1]
    rows = fetch_btc_klines_1m(start_ts, end_ts)
    if rows:
        _btc_klines_cache[slug] = (now, rows)
        return rows
    return hit[1] if hit else []


def btc_close_at(rows: list, ts: float) -> Optional[float]:
    """Last series close at or before ``ts`` (None when unavailable)."""
    px = None
    for t, c in rows:
        if t <= ts:
            px = c
        else:
            break
    return px


def parse_json_objects(text: str) -> list[dict[str, Any]]:
    out = []
    cur = []
    depth = 0
    for ch in text:
        if ch == '{':
            depth += 1
        if depth > 0:
            cur.append(ch)
        if ch == '}' and depth > 0:
            depth -= 1
            if depth == 0:
                s = ''.join(cur)
                cur = []
                try:
                    out.append(json.loads(s))
                except Exception:
                    pass
    return out


def bucket_5m(ts: int) -> int:
    return ts - (ts % 300)


def fetch_event(slug: str) -> Optional[dict[str, Any]]:
    r = requests.get('https://gamma-api.polymarket.com/events', params={'slug': slug}, timeout=12)
    r.raise_for_status()
    arr = r.json()
    return arr[0] if arr else None


def resolve_slot_market(slot_ts: int, fetch=fetch_event) -> Optional[dict[str, Any]]:
    """Fetch + validate the BTC 5m market for one slot timestamp (#17).

    A slot is usable when its event resolves to a market that is active,
    not closed, and ends more than 5s out. ``fetch`` is injectable for
    unit tests (defaults to the live Gamma lookup).
    """
    slug = f'btc-updown-5m-{slot_ts}'
    try:
        ev = fetch(slug)
    except Exception:
        return None
    if not ev:
        return None

    mkts = ev.get('markets') or []
    if not mkts:
        return None

    m = mkts[0]
    if m.get('closed') is True:
        return None
    if m.get('active') is False:
        return None

    end_iso = str(m.get('endDate') or m.get('endDateIso') or '')
    try:
        end_ts = dt.datetime.fromisoformat(end_iso.replace('Z', '+00:00')).timestamp()
    except Exception:
        return None

    sec_left = end_ts - time.time()
    if sec_left <= 5:
        return None

    mm = dict(m)
    mm['_event_slug'] = slug
    mm['_seconds_left'] = sec_left
    return mm


def resolve_active_current_5m_market() -> Optional[dict[str, Any]]:
    """Return active BTC 5m market for the current slot only.

    Kept for compatibility (watcher/tests); the entry loop uses
    choose_slot_market() across candidate_slots (#17).
    """
    now = int(time.time())
    return resolve_slot_market(bucket_5m(now))


# Slot offsets in seconds relative to the current 5m bucket (#17).
# Mirrors config/btc_5m_profiles.yaml market_validation.candidate_slots.
SLOT_OFFSETS = {"prev": -300, "current": 0, "next": 300, "next2": 600}


def choose_slot_market(now: float, candidate_slots, min_seconds_left: float,
                       fetch=fetch_event) -> Optional[dict[str, Any]]:
    """Pick the closest valid slot market (YAML ``choose: closest_valid``).

    The current slot is tried first (lazy: the common path costs one Gamma
    call). If it is missing or has less than ``min_seconds_left`` left,
    the remaining candidates are tried nearest-first (future preferred on
    ties — a past slot is already over). Returns the market tagged with
    ``_slot``, or None when no slot is tradeable right now.
    """
    slots = candidate_slots
    if isinstance(slots, str):
        slots = slots.split(',')
    slots = [str(s).strip() for s in (slots or []) if str(s).strip()]
    if "current" in slots:
        ordered: list[str] = ["current"]
    else:
        ordered = []

    def _dist(name: str) -> tuple[float, int]:
        off = SLOT_OFFSETS.get(name, 0)
        # future slots sort before past ones on ties
        return (abs(off), 0 if off >= 0 else 1)

    ordered += sorted([s for s in slots if s != "current"], key=_dist)
    cur_bucket = bucket_5m(int(now))
    for name in ordered:
        mm = resolve_slot_market(cur_bucket + SLOT_OFFSETS.get(name, 0), fetch=fetch)
        if mm is None:
            continue
        try:
            left = float(mm.get('_seconds_left') or 0)
        except (TypeError, ValueError):
            continue
        if left < float(min_seconds_left):
            continue
        mm['_slot'] = name
        return mm
    return None


def parse_json_field(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def market_side_prices(market: dict[str, Any]) -> tuple[float, float, str, str, str, str]:
    outcomes = parse_json_field(market.get('outcomes')) or []
    prices = parse_json_field(market.get('outcomePrices')) or []
    token_ids = parse_json_field(market.get('clobTokenIds')) or []
    if len(prices) < 2 or len(token_ids) < 2:
        raise RuntimeError('missing outcomePrices/clobTokenIds')

    up_i, down_i = 0, 1
    labs = [str(x).lower() for x in outcomes[:2]] if isinstance(outcomes, list) else []
    if len(labs) >= 2 and ('up' in labs[1] or 'yes' in labs[1]):
        up_i, down_i = 1, 0

    up_p = float(prices[up_i])
    dn_p = float(prices[down_i])
    up_t = str(token_ids[up_i])
    dn_t = str(token_ids[down_i])
    return up_p, dn_p, up_t, dn_t, str(market.get('slug') or market.get('_event_slug') or ''), str(market.get('endDate') or market.get('endDateIso') or '')


def _fnum(v, default=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN guard
        return default
    return f


def _top_of_book(book) -> tuple[Optional[float], float, Optional[float], float]:
    """Return (best_bid, bid_size, best_ask, ask_size). Sizes default to 0.0."""
    bids = getattr(book, 'bids', []) or []
    asks = getattr(book, 'asks', []) or []
    best_bid, bid_size, best_ask, ask_size = None, 0.0, None, 0.0
    for b in bids:
        p = _fnum(getattr(b, 'price', None))
        if p is None:
            continue
        if best_bid is None or p > best_bid:
            best_bid = p
            bid_size = _fnum(getattr(b, 'size', None), 0.0) or 0.0
    for a in asks:
        p = _fnum(getattr(a, 'price', None))
        if p is None:
            continue
        if best_ask is None or p < best_ask:
            best_ask = p
            ask_size = _fnum(getattr(a, 'size', None), 0.0) or 0.0
    return best_bid, bid_size, best_ask, ask_size


def _book_timestamp_age_sec(book) -> Optional[float]:
    """Best-effort quote age from the CLOB book timestamp.

    The CLOB book carries a millisecond-epoch ``timestamp``. If it is
    missing or unparseable, return None (caller falls back to fetch
    latency and flags the age as unknown rather than blocking).
    """
    ts = getattr(book, 'timestamp', None)
    if ts is None:
        return None
    try:
        v = float(ts)
    except (TypeError, ValueError):
        try:
            dtv = dt.datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
            v = dtv.timestamp()
        except Exception:
            return None
    else:
        if v > 1e12:  # ms epoch
            v = v / 1000.0
        elif v > 1e9:  # s epoch
            pass
        else:
            return None
    return max(0.0, time.time() - v)


def clob_side_prices(up_token: str, down_token: str, clob_base: str = 'https://clob.polymarket.com') -> dict[str, Any]:
    """Return a top-of-book snapshot for both sides.

    Keys: up_ask, up_ask_size, up_bid, dn_ask, dn_ask_size, dn_bid,
    up_spread, dn_spread, quote_age_sec, fetch_sec.
    ``quote_age_sec`` is the older of the two book timestamps when
    available, else the fetch latency (flagged as unknown by callers).
    Sizes are in shares; multiply by price for USD notional.
    """
    pub = ClobClient(host=clob_base, chain_id=POLYGON)
    t0 = time.time()
    up_book = pub.get_order_book(str(up_token))
    dn_book = pub.get_order_book(str(down_token))
    fetch_sec = max(0.0, time.time() - t0)
    up_bid, _up_bid_sz, up_ask, up_ask_sz = _top_of_book(up_book)
    dn_bid, _dn_bid_sz, dn_ask, dn_ask_sz = _top_of_book(dn_book)

    up_spread = (up_ask - up_bid) if (up_ask is not None and up_bid is not None) else None
    dn_spread = (dn_ask - dn_bid) if (dn_ask is not None and dn_bid is not None) else None

    ages = [a for a in (_book_timestamp_age_sec(up_book), _book_timestamp_age_sec(dn_book)) if a is not None]
    quote_age = max(ages) if ages else None

    return {
        'up_ask': up_ask,
        'up_ask_size': up_ask_sz,
        'up_bid': up_bid,
        'dn_ask': dn_ask,
        'dn_ask_size': dn_ask_sz,
        'dn_bid': dn_bid,
        'up_spread': max(0.0, up_spread) if up_spread is not None else None,
        'dn_spread': max(0.0, dn_spread) if dn_spread is not None else None,
        'quote_age_sec': quote_age if quote_age is not None else fetch_sec,
        'quote_age_known': bool(ages),
        'fetch_sec': fetch_sec,
    }


def clob_best_bid(token_id: str, clob_base: str = 'https://clob.polymarket.com') -> Optional[float]:
    pub = ClobClient(host=clob_base, chain_id=POLYGON)
    book = pub.get_order_book(str(token_id))
    best_bid, _, _, _ = _top_of_book(book)
    return best_bid


def auth_clob_client(clob_base: str = 'https://clob.polymarket.com') -> Optional[ClobClient]:
    try:
        key = os.getenv('PM_PRIVATE_KEY') or ''
        funder = os.getenv('PM_FUNDER') or os.getenv('PM_ADDRESS') or None
        sig = int(os.getenv('PM_SIGNATURE_TYPE', '2'))
        v1 = os.getenv('PM_API_KEY') or ''
        v2 = os.getenv('PM_API_SECRET') or ''
        v3 = os.getenv('PM_API_PASSPHRASE') or ''
        if not key or not v1 or not v2 or not v3:
            return None
        c = ClobClient(host=clob_base, chain_id=POLYGON, key=key, signature_type=sig, funder=funder)
        creds = {
            f"api_{'key'}": v1,
            f"api_{'secret'}": v2,
            f"api_{'passphrase'}": v3,
        }
        c.set_api_creds(ApiCreds(**creds))
        return c
    except Exception:
        return None


def poll_order_status(client: Optional[ClobClient], order_id: str, wait_sec: float = 6.0, step_sec: float = 1.0) -> tuple[str, Optional[dict[str, Any]]]:
    if client is None or not order_id:
        return '', None
    deadline = time.time() + max(0.0, float(wait_sec))
    last = None
    while time.time() <= deadline:
        try:
            last = client.get_order(order_id)
            st = str((last or {}).get('status') or '').upper()
            if st and st not in ('LIVE', 'OPEN'):
                return st, last
        except Exception:
            pass
        time.sleep(max(0.2, float(step_sec)))
    try:
        last = client.get_order(order_id)
    except Exception:
        pass
    st = str((last or {}).get('status') or '').upper()
    return st, last


def cancel_token_orders(client: Optional[ClobClient], token_id: str) -> Optional[dict[str, Any]]:
    if client is None:
        return None
    try:
        return client.cancel_market_orders(asset_id=str(token_id))
    except Exception as e:
        return {'error': str(e)}


def run_open(
    repo: str,
    slug: str,
    side: str,
    stake: float,
    execute: bool,
    max_spread: float | None = None,
    min_top_ask_notional_usd: float | None = None,
    equity_usd: float = 100.0,
    max_notional_usd: float | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    try:
        equity = float(equity_usd) if float(equity_usd) > 0 else 100.0
    except (TypeError, ValueError):
        equity = 100.0
    cmd = [
        '.venv/bin/python',
        'src/live/pm_live_trade_runner.py',
        '--market-slug', slug,
        '--force-side', side,
        '--start-equity', str(equity),
        '--risk-frac', str(float(stake) / equity),
        '--max-notional-usd', str(max_notional_usd if max_notional_usd else stake),
    ]
    if execute:
        cmd.append('--execute')
    env = os.environ.copy()
    # Propagate this skill's spread/liquidity guards to the external runner
    # instead of disabling them (previously hard-coded to 1 and 0).
    if max_spread is not None:
        env['PM_MAX_SPREAD'] = str(max_spread)
    else:
        env.setdefault('PM_MAX_SPREAD', '1')
    if min_top_ask_notional_usd is not None:
        env['PM_MIN_TOP_ASK_NOTIONAL_USD'] = str(min_top_ask_notional_usd)
    else:
        env.setdefault('PM_MIN_TOP_ASK_NOTIONAL_USD', '0')
    env.setdefault('PM_ORDER_TYPE', 'FAK')
    p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=env)
    out = (p.stdout or '') + '\n' + (p.stderr or '')
    return out, parse_json_objects(out)


def run_close(
    repo: str,
    slug: str,
    token_id: str,
    shares: float,
    execute: bool,
    close_order_type: str = 'FAK',
    close_limit_price: float | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    cmd = [
        '.venv/bin/python',
        'src/live/pm_live_trade_runner.py',
        '--market-slug', slug,
        '--close-token-id', token_id,
        '--close-shares', f'{shares:.8f}',
    ]
    if close_limit_price is not None and close_limit_price > 0:
        cmd += ['--close-limit-price', f'{close_limit_price:.6f}']
    if execute:
        cmd.append('--execute')
    env = os.environ.copy()
    env['PM_CLOSE_ORDER_TYPE'] = str(close_order_type or 'FAK').upper()
    p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=env)
    out = (p.stdout or '') + '\n' + (p.stderr or '')
    return out, parse_json_objects(out)


def close_position(
    repo: str,
    slug: str,
    side: str,
    token_id: str,
    shares: float,
    execute: bool,
    close_retry_max: int = 18,
    close_retry_delay_sec: float = 2.0,
    force_discount_pct: float = 0.10,
    force_max_discount_abs: float = 0.02,
    last_side_price=None,
    entry_price=None,
    label: str = 'main',
) -> tuple[dict[str, Any], str, list[dict[str, Any]], Any, Any]:
    """Shared exit cascade (#13 extraction): FAK -> GTC-limit near the
    executable price -> poll/cancel/repost aggressive (FORCE_GTC with the
    proportional #12 pricing). Used for both the main exit and the
    micro-hedge exit; debug entries carry ``leg`` so the two are
    distinguishable in the report. Returns
    (close_obj, raw_out, close_debug, fallback_used, force_close_used).
    """
    close_debug: list[dict[str, Any]] = []
    close_obj: dict[str, Any] = {}
    out = ''
    fallback_used = None
    force_close_used = None
    client = auth_clob_client()

    def _dbg(entry: dict[str, Any]) -> None:
        entry['leg'] = label
        close_debug.append(entry)

    for i in range(max(1, int(close_retry_max))):
        out, objs = run_close(
            repo,
            slug,
            token_id,
            shares,
            execute,
            close_order_type='FAK',
        )
        close_obj = objs[-1] if objs else {}
        post = close_obj.get('order_post_result') or {}
        status = str(post.get('status') or '').lower()
        skipped = str(close_obj.get('close_skipped') or '')
        _dbg({
            'ts': ts_utc(),
            'attempt': i + 1,
            'order_type': 'FAK',
            'status': status,
            'close_skipped': skipped,
        })
        if post.get('success') is True and status == 'matched':
            break

        # common transient path right after open: token balance not yet visible
        if skipped == 'zero_effective_shares':
            time.sleep(float(close_retry_delay_sec))
            continue

        # fallback: if FAK has no instant match, try a GTC limit close near current side price
        txt = ((out or '') + '\n' + json.dumps(close_obj, ensure_ascii=False)).lower()
        if 'no orders found to match with fak order' in txt:
            bb = None
            try:
                bb = clob_best_bid(token_id)
            except Exception:
                bb = None
            # Prefer the executable bid; fall back to Gamma mid, then the
            # last monitored price, then the entry price.
            px = bb
            if px is None:
                px = get_side_price_from_slug(slug, side)
            if px is None:
                px = last_side_price
            if px is None:
                px = entry_price
            if px is None:
                # No price reference at all (previously a TypeError crash);
                # rest a coin-flip GTC and note the unknown reference.
                px = 0.5
                fallback_used = {'type': 'GTC_LIMIT', 'price': None,
                                 'price_unknown': True}
            limit_px = max(0.01, min(0.99, float(px - 0.01)))
            if fallback_used is None:
                fallback_used = {'type': 'GTC_LIMIT', 'price': limit_px}
            out2, objs2 = run_close(
                repo,
                slug,
                token_id,
                shares,
                execute,
                close_order_type='GTC',
                close_limit_price=limit_px,
            )
            close_obj2 = objs2[-1] if objs2 else {}
            post2 = close_obj2.get('order_post_result') or {}
            status2 = str(post2.get('status') or '').lower()
            _dbg({
                'ts': ts_utc(),
                'attempt': i + 1,
                'order_type': 'GTC',
                'status': status2,
                'close_skipped': str(close_obj2.get('close_skipped') or ''),
                'limit_price': limit_px,
            })
            close_obj = close_obj2
            out = out2
            if post2.get('success') is True and status2 == 'matched':
                break

            # If GTC is accepted but still live, force-close flow: poll status, cancel, repost aggressive.
            if post2.get('success') is True and status2 == 'live':
                oid2 = str(post2.get('orderID') or '')
                st_upd, ord_upd = poll_order_status(client, oid2, wait_sec=min(8.0, max(2.0, float(close_retry_delay_sec) * 2)), step_sec=1.0)
                _dbg({
                    'ts': ts_utc(),
                    'attempt': i + 1,
                    'order_type': 'GTC_POLL',
                    'status': st_upd.lower() if st_upd else '',
                    'order_id': oid2,
                })
                if st_upd == 'MATCHED':
                    post2['status'] = 'matched'
                    close_obj['order_post_result'] = post2
                    break

                cancel_info = cancel_token_orders(client, token_id)
                bb2 = None
                try:
                    bb2 = clob_best_bid(token_id)
                except Exception:
                    bb2 = None
                # Last-resort price: proportional discount off the executable
                # bid (#12), walking executable bid -> last monitored price
                # -> entry price before giving up at 0.01.
                force_px = (
                    force_close_price(bb2, force_discount_pct,
                                      force_max_discount_abs)
                    or force_close_price(last_side_price,
                                         force_discount_pct,
                                         force_max_discount_abs)
                    or force_close_price(entry_price,
                                         force_discount_pct,
                                         force_max_discount_abs)
                    or 0.01
                )
                force_close_used = {
                    'type': 'FORCE_GTC_LIMIT',
                    'price': force_px,
                    'cancel_info': cancel_info,
                }
                out3, objs3 = run_close(
                    repo,
                    slug,
                    token_id,
                    shares,
                    execute,
                    close_order_type='GTC',
                    close_limit_price=force_px,
                )
                close_obj3 = objs3[-1] if objs3 else {}
                post3 = close_obj3.get('order_post_result') or {}
                status3 = str(post3.get('status') or '').lower()
                _dbg({
                    'ts': ts_utc(),
                    'attempt': i + 1,
                    'order_type': 'FORCE_GTC',
                    'status': status3,
                    'close_skipped': str(close_obj3.get('close_skipped') or ''),
                    'limit_price': force_px,
                })
                close_obj = close_obj3
                out = out3
                if post3.get('success') is True and status3 == 'matched':
                    break

        time.sleep(float(close_retry_delay_sec))

    return close_obj, out, close_debug, fallback_used, force_close_used


def get_side_price_from_slug(slug: str, side: str) -> Optional[float]:
    try:
        ev = fetch_event(slug)
        if not ev:
            return None
        mkts = ev.get('markets') or []
        if not mkts:
            return None
        up, dn, *_ = market_side_prices(mkts[0])
        return up if side == 'UP' else dn
    except Exception:
        return None


PROFILES: dict[str, dict[str, Any]] = {
    # Conservative: strict filters, lower per-trade risk (#10, #11).
    # Stake is equity-derived (8% of equity, capped at $8), NOT fixed.
    'conservative': {
        'threshold': 0.70,
        'stop_loss_pct': 0.25,
        'exit_before_sec': 20,
        'min_entry_seconds_left': 60,
        'entry_timeout_min': 60,
        'poll_sec': 5.0,
        # Pre-entry execution guards (mirror config/btc_5m_profiles.yaml execution_safety).
        'max_spread': 0.03,
        'min_top_ask_notional_usd': 30.0,
        'max_quote_age_sec': 8.0,
        'max_consecutive_errors': 3,
        # Session risk ledger (mirror config profiles sizing caps).
        'max_trades_per_day': 12,
        'daily_max_loss_pct': 10.0,
        'equity_usd': 100.0,
        # Equity-based sizing (mirror config profiles sizing).
        'risk_per_trade_pct': 8.0,
        'max_notional_usd': 8.0,
        # Micro-hedge: small opposite-side position when the held side
        # looks almost certain, guarding late-reversal tail risk (#13;
        # mirrors config profiles hedge).
        'hedge_enabled': True,
        'hedge_trigger_price': 0.95,
        'hedge_trigger_seconds_left': 45,
        'hedge_share_pct': 3.0,
        'hedge_min_notional_usd': 1.0,
        'hedge_max_notional_usd': 2.0,
        # Momentum-strategy entry (mirror config strategy_reference): BTC
        # must move >= $70 in the interval; skew veto blocks entries
        # against strong crowd flow (#1, #2, #3).
        'btc_move_usd_min': 70.0,
        'skew_veto_threshold': 0.10,
    },
    # Aggressive: higher frequency (lower threshold, looser guards) and
    # higher risk (15% of equity, capped at $15) (#10, #11).
    'aggressive': {
        'threshold': 0.65,
        'stop_loss_pct': 0.30,
        'exit_before_sec': 20,
        'min_entry_seconds_left': 60,
        'entry_timeout_min': 60,
        'poll_sec': 5.0,
        'max_spread': 0.05,
        'min_top_ask_notional_usd': 20.0,
        'max_quote_age_sec': 12.0,
        'max_consecutive_errors': 3,
        'max_trades_per_day': 20,
        'daily_max_loss_pct': 15.0,
        'equity_usd': 100.0,
        'risk_per_trade_pct': 15.0,
        'max_notional_usd': 15.0,
        'hedge_enabled': True,
        'hedge_trigger_price': 0.93,
        'hedge_trigger_seconds_left': 50,
        'hedge_share_pct': 5.0,
        'hedge_min_notional_usd': 1.0,
        'hedge_max_notional_usd': 3.0,
        # Momentum entry is looser (mirror config): $50 move suffices, and
        # the skew veto tolerates more disagreement (#1, #2, #3).
        'btc_move_usd_min': 50.0,
        'skew_veto_threshold': 0.15,
    },
}


def _apply_profile_value(args: argparse.Namespace, name: str, cast) -> argparse.Namespace:
    if getattr(args, name, None) is None:
        prof = PROFILES.get(args.profile or 'conservative', PROFILES['conservative'])
        setattr(args, name, cast(prof[name]))
    return args


def apply_profile(args: argparse.Namespace) -> argparse.Namespace:
    for name, cast in (
        ('threshold', float),
        ('stop_loss_pct', float),
        ('exit_before_sec', int),
        ('min_entry_seconds_left', int),
        ('entry_timeout_min', int),
        ('poll_sec', float),
        ('max_spread', float),
        ('min_top_ask_notional_usd', float),
        ('max_quote_age_sec', float),
        ('max_consecutive_errors', int),
        ('max_trades_per_day', int),
        ('daily_max_loss_pct', float),
        ('equity_usd', float),
        ('risk_per_trade_pct', float),
        ('max_notional_usd', float),
        ('hedge_trigger_price', float),
        ('hedge_trigger_seconds_left', int),
        ('hedge_share_pct', float),
        ('hedge_min_notional_usd', float),
        ('hedge_max_notional_usd', float),
        ('btc_move_usd_min', float),
        ('skew_veto_threshold', float),
    ):
        _apply_profile_value(args, name, cast)
    if getattr(args, 'disable_momentum', False):
        # --disable-momentum restores the legacy highest-ask trigger (#3
        # escape hatch for comparison runs).
        args.btc_move_usd_min = None
    # NOTE: stake_usd is intentionally NOT profile-filled. It is an
    # explicit override only; when unset, resolve_stake_usd() derives the
    # stake from equity x risk_per_trade_pct capped at max_notional_usd.
    # hedge_enabled is also NOT profile-filled: profiles both default it
    # True, and only an explicit --disable-hedge turns it off.
    return args


# Minimum order size on Polymarket (~$1). Equity-derived stakes are
# floored here so dust equity still yields a placeable order.
MIN_STAKE_USD = 1.0


def resolve_stake_usd(args: argparse.Namespace) -> tuple[float, str]:
    """Per-trade stake in USD (#11).

    Explicit ``--stake-usd`` wins ('explicit'). Otherwise the stake is
    ``equity_usd * risk_per_trade_pct / 100`` capped at
    ``max_notional_usd`` and floored at ``MIN_STAKE_USD`` ('equity_pct'),
    so account growth/shrinkage moves sizing automatically.
    """
    explicit = getattr(args, 'stake_usd', None)
    if explicit is not None:
        try:
            if float(explicit) > 0:
                return float(explicit), 'explicit'
        except (TypeError, ValueError):
            pass
    equity = getattr(args, 'equity_usd', None)
    equity = 100.0 if equity is None else equity
    pct = getattr(args, 'risk_per_trade_pct', None) or 0.0
    cap = getattr(args, 'max_notional_usd', None)
    try:
        auto = max(0.0, float(equity) * float(pct) / 100.0)
    except (TypeError, ValueError):
        auto = 0.0
    try:
        if cap is not None and float(cap) > 0:
            auto = min(auto, float(cap))
    except (TypeError, ValueError):
        pass
    return max(MIN_STAKE_USD, auto), 'equity_pct'


# Graceful shutdown (#14). First SIGTERM/SIGINT sets the flag so the entry
# loop stops looking, the monitor loop breaks out, and the normal close
# cascade still runs. A second signal forces immediate exit.
_shutdown_requested = False


def request_shutdown(signum, _frame) -> None:
    global _shutdown_requested
    if _shutdown_requested:
        raise SystemExit(128 + int(signum or 15))
    _shutdown_requested = True


def install_signal_handlers() -> None:
    try:
        import signal as _signal
        _signal.signal(_signal.SIGTERM, request_shutdown)
        _signal.signal(_signal.SIGINT, request_shutdown)
    except (ValueError, OSError, RuntimeError):
        # Not the main thread, or signals unavailable (e.g. Windows).
        pass


def force_close_price(
    best_bid,
    discount_pct: float = 0.10,
    max_discount_abs: float = 0.02,
) -> Optional[float]:
    """Last-resort exit price (#12).

    The old code always crossed ``best_bid - 0.02``, which at low prices
    (e.g. a 0.05 bid) is a ~40% haircut. The discount is now proportional
    (``discount_pct`` of the bid) capped at ``max_discount_abs``, so high
    prices behave as before (0.70 -> 0.68) while thin-book bids keep most
    of their value (0.10 -> 0.09, 0.03 -> 0.027). Returns None when the
    bid is missing/non-positive so the caller can walk its fallback chain.
    """
    try:
        bid = float(best_bid) if best_bid is not None else 0.0
    except (TypeError, ValueError):
        return None
    if bid <= 0:
        return None
    try:
        pct = float(discount_pct)
    except (TypeError, ValueError):
        pct = 0.10
    try:
        cap = float(max_discount_abs)
    except (TypeError, ValueError):
        cap = 0.02
    discount = min(max(0.0, cap), max(0.0, bid * pct))
    return max(0.01, min(0.99, bid - discount))


def hedge_sizing(
    main_cost_usdc,
    share_pct: float = 3.0,
    min_notional_usdc: float = 1.0,
    max_notional_usdc: float = 2.0,
) -> float:
    """Micro-hedge notional (#13): ``share_pct`` of the main position cost,
    clamped to [min, max]. Pure function for unit testing."""
    try:
        base = max(0.0, float(main_cost_usdc)) * max(0.0, float(share_pct)) / 100.0
    except (TypeError, ValueError):
        return 0.0
    try:
        lo = max(0.0, float(min_notional_usdc))
    except (TypeError, ValueError):
        lo = 0.0
    try:
        hi = float(max_notional_usdc)
    except (TypeError, ValueError):
        hi = base
    if hi > 0:
        base = min(base, hi)
    return max(lo, base) if base > 0 else 0.0


def hedge_triggered(
    side_bid,
    seconds_left,
    trigger_price: float = 0.95,
    trigger_seconds_left: int = 45,
    enabled: bool = True,
    already_placed_or_attempted: bool = False,
    hedge_token_id=None,
) -> bool:
    """Fire-once predicate for the micro-hedge (#13): enabled, not yet
    attempted, opposite token known, held-side bid at/above trigger with
    at most ``trigger_seconds_left`` remaining. Pure; unit-tested."""
    if not enabled or already_placed_or_attempted or not hedge_token_id:
        return False
    try:
        if float(side_bid or 0) < float(trigger_price):
            return False
    except (TypeError, ValueError):
        return False
    try:
        if float(seconds_left if seconds_left is not None else 1e9) > float(trigger_seconds_left):
            return False
    except (TypeError, ValueError):
        return False
    return True


def default_repo_path() -> str:
    env_repo = os.environ.get('BTC5M_REPO')
    if env_repo:
        return env_repo
    return str(Path(__file__).resolve().parents[3] / 'pm-hl-conservative-plus-repo')


def repo_path_error(path) -> Optional[str]:
    """Fail-fast validation for the execution repo dir (#18).

    Returns a human-readable error when ``path`` is empty or not a
    directory, else None. The ``parents[3]`` fallback in
    ``default_repo_path`` assumes a fixed checkout layout, so a wrong
    layout must surface here — at startup — instead of as a confusing
    failure deep in the entry loop.
    """
    if not path or not str(path).strip():
        return ("execution repo path is empty "
                "(set --repo or BTC5M_REPO to the execution checkout)")
    if not os.path.isdir(str(path)):
        return (f"execution repo not found: {path} "
                "(set --repo or BTC5M_REPO to the execution checkout)")
    return None


def default_runtime_dir() -> str:
    env_dir = os.environ.get('BTC5M_RUNTIME_DIR')
    if env_dir:
        return env_dir
    return str(Path(__file__).resolve().parents[1] / 'runtime')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--repo', default=default_repo_path())
    ap.add_argument('--profile', choices=['conservative', 'aggressive'], default='conservative')
    ap.add_argument('--threshold', type=float, default=None)
    ap.add_argument('--stake-usd', type=float, default=None, help='Explicit per-trade stake override. When unset, the stake is derived from equity x --risk-per-trade-pct capped at --max-notional-usd (#11)')
    ap.add_argument('--risk-per-trade-pct', type=float, default=None, help='Pct of equity risked per trade (default: 8 conservative, 15 aggressive)')
    ap.add_argument('--max-notional-usd', type=float, default=None, help='Cap on the equity-derived stake (default: 8 conservative, 15 aggressive)')
    ap.add_argument('--stop-loss-pct', type=float, default=None, help='0.30 means -30%% from entry price')
    ap.add_argument('--exit-before-sec', type=int, default=None)
    ap.add_argument('--min-entry-seconds-left', type=int, default=None, help='Do not open if less seconds remain in current 5m slot')
    ap.add_argument('--entry-timeout-min', type=int, default=None)
    ap.add_argument('--poll-sec', type=float, default=None)
    ap.add_argument('--close-retry-max', type=int, default=18, help='Max close retries when position is not yet visible / not immediately closable')
    ap.add_argument('--close-retry-delay-sec', type=float, default=2.0, help='Delay between close retries')
    ap.add_argument('--force-close-discount-pct', type=float, default=0.10, help='Last-resort exit discount as a fraction of the best bid, capped by --force-close-max-discount-abs (#12)')
    ap.add_argument('--force-close-max-discount-abs', type=float, default=0.02, help='Cap (in price points) on the last-resort exit discount (#12)')
    ap.add_argument('--disable-hedge', action='store_true', help='Do not place the micro-hedge even when its trigger fires (#13)')
    ap.add_argument('--hedge-trigger-price', type=float, default=None, help='Place the micro-hedge once the held side bid reaches this (default: 0.95 conservative, 0.93 aggressive)')
    ap.add_argument('--btc-move-usd-min', type=float, default=None, help='Min |BTC move| since market open to allow entry; side follows the move (default: 70 conservative, 50 aggressive; #1, #3)')
    ap.add_argument('--skew-veto-threshold', type=float, default=None, help='Veto entry when skew opposes momentum beyond this (default: 0.10 conservative, 0.15 aggressive; #2)')
    ap.add_argument('--disable-momentum', action='store_true', help='Restore the legacy highest-ask entry trigger (comparison runs only; #3)')
    ap.add_argument('--candidate-slots', default='prev,current,next,next2', help='Comma-separated slot scan list; the entry loop picks the closest slot with a tradeable market (mirrors YAML candidate_slots; #17)')
    ap.add_argument('--hedge-trigger-seconds-left', type=int, default=None, help='... and at most this many seconds remain (default: 45 conservative, 50 aggressive)')
    ap.add_argument('--hedge-share-pct', type=float, default=None, help='Hedge notional as pct of main cost (default: 3 conservative, 5 aggressive)')
    ap.add_argument('--hedge-min-notional-usd', type=float, default=None, help='Floor on hedge notional (default 1.0)')
    ap.add_argument('--hedge-max-notional-usd', type=float, default=None, help='Cap on hedge notional (default: 2 conservative, 3 aggressive)')
    ap.add_argument('--max-spread', type=float, default=None, help='Skip entry if picked-side spread exceeds this')
    ap.add_argument('--min-top-ask-notional-usd', type=float, default=None, help='Skip entry if picked-side top ask notional (USD) is below this')
    ap.add_argument('--max-quote-age-sec', type=float, default=None, help='Skip entry if CLOB quote age exceeds this')
    ap.add_argument('--max-consecutive-errors', type=int, default=None, help='Abort run after this many consecutive API/execution errors')
    ap.add_argument('--max-trades-per-day', type=int, default=None, help='Block new entries after this many live trades today (UTC)')
    ap.add_argument('--daily-max-loss-pct', type=float, default=None, help='Block new entries after losing this pct of equity today (UTC)')
    ap.add_argument('--equity-usd', type=float, default=None, help='Account equity reference for the daily-loss cap')
    ap.add_argument('--runtime-dir', default=None, help='Runtime dir holding logs and the risk ledger (default: <skill>/runtime)')
    ap.add_argument('--resume', action='store_true', help='Resume a leftover open position from a crashed run instead of opening a new one (#33)')
    ap.add_argument('--wallet-address', default=os.environ.get('BTC5M_WALLET_ADDRESS') or os.environ.get('POLY_WALLET_ADDRESS'), help='Wallet for the pre-entry duplicate-position check (#33)')
    ap.add_argument('--alert-webhook-url', default=None, help='Optional webhook URL for entry/close/abort alerts; falls back to BTC5M_ALERT_WEBHOOK env (#27)')
    ap.add_argument('--execute', action='store_true')
    args = apply_profile(ap.parse_args())
    if args.runtime_dir is None:
        args.runtime_dir = default_runtime_dir()
    # Fail fast on a bad execution checkout (#18) instead of dying
    # mid-run when the first subprocess call needs it.
    _repo_err = repo_path_error(args.repo)
    if _repo_err is not None:
        ap.error(_repo_err)
    # Per-trade stake: explicit --stake-usd wins, else equity-derived (#11).
    stake_usd, stake_basis = resolve_stake_usd(args)
    # Graceful shutdown (#14): SIGTERM/SIGINT finish the run via the close
    # cascade instead of orphaning the position.
    install_signal_handlers()
    # Micro-hedge master switch (#13): profiles default it on; only an
    # explicit --disable-hedge turns it off.
    hedge_enabled = not args.disable_hedge

    report: dict[str, Any] = {
        'started_at': ts_utc(),
        'params': {
            'profile': args.profile,
            'threshold': args.threshold,
            'stake_usd': stake_usd,
            'stake_basis': stake_basis,
            'risk_per_trade_pct': args.risk_per_trade_pct,
            'max_notional_usd': args.max_notional_usd,
            'stop_loss_pct': args.stop_loss_pct,
            'exit_before_sec': args.exit_before_sec,
            'min_entry_seconds_left': args.min_entry_seconds_left,
            'entry_timeout_min': args.entry_timeout_min,
            'poll_sec': args.poll_sec,
            'close_retry_max': args.close_retry_max,
            'close_retry_delay_sec': args.close_retry_delay_sec,
            'max_spread': args.max_spread,
            'min_top_ask_notional_usd': args.min_top_ask_notional_usd,
            'max_quote_age_sec': args.max_quote_age_sec,
            'max_consecutive_errors': args.max_consecutive_errors,
            'max_trades_per_day': args.max_trades_per_day,
            'daily_max_loss_pct': args.daily_max_loss_pct,
            'equity_usd': args.equity_usd,
            'runtime_dir': args.runtime_dir,
            'execute': args.execute,
            'resume': args.resume,
            'wallet_configured': bool(args.wallet_address),
            'hedge_enabled': hedge_enabled,
            'hedge_trigger_price': args.hedge_trigger_price,
            'hedge_trigger_seconds_left': args.hedge_trigger_seconds_left,
            'hedge_share_pct': args.hedge_share_pct,
            'hedge_min_notional_usd': args.hedge_min_notional_usd,
            'hedge_max_notional_usd': args.hedge_max_notional_usd,
            'force_close_discount_pct': args.force_close_discount_pct,
            'force_close_max_discount_abs': args.force_close_max_discount_abs,
            'btc_move_usd_min': args.btc_move_usd_min,
            'skew_veto_threshold': args.skew_veto_threshold,
            'disable_momentum': args.disable_momentum,
            'candidate_slots': args.candidate_slots,
        },
        'attempts': [],
        'attempts_dropped': 0,
    }

    deadline = time.time() + args.entry_timeout_min * 60
    opened = None
    err_streak = 0
    # Session risk ledger (#4, #5): live trades only; dry-runs neither
    # consume the daily budget nor enforce it.
    ledger_file = ledger_path(args.runtime_dir) if args.execute else None

    def abort_run(result: str, decision: str):
        report['finished_at'] = ts_utc()
        report['result'] = result
        report['decision'] = decision
        report['consecutive_errors'] = err_streak
        try:
            alerts.emit(args.runtime_dir,
                        'blocked' if decision == 'blocked' else 'aborted',
                        {'result': result}, args.alert_webhook_url)
        except Exception:
            pass
        print(json.dumps(report, ensure_ascii=False, indent=2))

    # Crash recovery (#33): a leftover open_position.json means the previous
    # process died mid-position. With --resume, monitor that position instead
    # of opening a new one; without it, refuse to run so we never double in.
    leftover = load_open_position(args.runtime_dir)
    if leftover is not None:
        if args.resume:
            opened = dict(leftover)
            report['resumed'] = True
            try:
                alerts.emit(args.runtime_dir, 'resumed',
                            {'side': opened.get('side'),
                             'market_slug': opened.get('market_slug')},
                            args.alert_webhook_url)
            except Exception:
                pass
            log_attempt(report, {'ts': ts_utc(), 'status': 'resumed_position',
                                 'side': opened.get('side'),
                                 'slug': opened.get('market_slug')})
        else:
            log_attempt(report, {'ts': ts_utc(), 'status': 'blocked_leftover_position',
                                 'position': {k: leftover.get(k) for k in
                                              ('side', 'market_slug', 'token_id')}})
            abort_run('blocked_leftover_position', 'blocked')
            return

    while opened is None and time.time() < deadline and not _shutdown_requested:
        try:
            # Block new entries once the daily loss cap or the
            # max-trades cap is hit (#4, #5). Exits the run: the
            # session is done for the day.
            if ledger_file is not None:
                _ledger = load_ledger(ledger_file, utc_today())
                _allowed, _reason = can_open(
                    _ledger,
                    max_trades_per_day=args.max_trades_per_day,
                    daily_max_loss_pct=args.daily_max_loss_pct,
                    equity_usd=args.equity_usd,
                )
                if not _allowed:
                    log_attempt(report, {'ts': ts_utc(), 'status': 'blocked_daily_limits', 'reason': _reason})
                    abort_run('blocked_' + _reason, 'blocked')
                    return

            m = choose_slot_market(time.time(), args.candidate_slots,
                                   args.min_entry_seconds_left)
            if not m:
                log_attempt(report, {'ts': ts_utc(), 'status': 'heartbeat_no_current_market'})
                err_streak = 0
                time.sleep(args.poll_sec)
                continue

            g_up, g_dn, up_t, dn_t, slug, end_iso = market_side_prices(m)

            end_ts = None
            sec_left = None
            try:
                end_ts = dt.datetime.fromisoformat(end_iso.replace('Z', '+00:00')).timestamp()
                sec_left = max(0.0, end_ts - time.time())
            except Exception:
                pass

            if sec_left is None:
                log_attempt(report, {'ts': ts_utc(), 'slug': slug, 'status': 'heartbeat_bad_market_end'})
                err_streak = 0
                time.sleep(args.poll_sec)
                continue

            # Do not open if less than N seconds remain in current slot.
            if sec_left < args.min_entry_seconds_left:
                log_attempt(report, {
                    'ts': ts_utc(),
                    'slug': slug,
                    'status': 'skip_too_late_to_enter',
                    'seconds_left': sec_left,
                    'min_entry_seconds_left': args.min_entry_seconds_left,
                })
                err_streak = 0
                time.sleep(args.poll_sec)
                continue

            # CLOB-based trigger price (best ask of selected side), not Gamma outcomePrices.
            try:
                snap = clob_side_prices(up_t, dn_t)
            except Exception as e:
                log_attempt(report, {'ts': ts_utc(), 'slug': slug, 'status': 'skip_clob_unavailable', 'error': str(e)})
                err_streak += 1
                if error_budget_exceeded(err_streak, args.max_consecutive_errors):
                    abort_run('aborted_consecutive_errors', 'aborted')
                    return
                time.sleep(args.poll_sec)
                continue

            up_ask = snap['up_ask']
            dn_ask = snap['dn_ask']
            spreads = [s for s in (snap['up_spread'], snap['dn_spread']) if s is not None]
            min_spread = min(spreads) if spreads else None

            log_attempt(report, {
                'ts': ts_utc(),
                'slug': slug,
                'slot': m.get('_slot', 'current'),
                'status': 'heartbeat',
                'gamma_up': g_up,
                'gamma_down': g_dn,
                'clob_up_ask': up_ask,
                'clob_down_ask': dn_ask,
                'up_ask_size': snap['up_ask_size'],
                'dn_ask_size': snap['dn_ask_size'],
                'up_spread': snap['up_spread'],
                'dn_spread': snap['dn_spread'],
                'seconds_left': sec_left,
                'min_spread': min_spread,
                'quote_age_sec': snap['quote_age_sec'],
                'quote_age_known': snap['quote_age_known'],
            })

            # Momentum-strategy entry (#1, #2, #3): the side follows the BTC
            # move since market open (it must clear --btc-move-usd-min);
            # --threshold stays as a minimum-price floor on the picked side;
            # --skew-veto-threshold blocks entries against strong crowd flow.
            # With --disable-momentum the legacy highest-ask trigger applies.
            btc_move: Optional[float] = None
            btc_dir = None
            side = None
            trigger_price = None
            if args.btc_move_usd_min is not None:
                m_open_ts = end_ts - 300.0 if end_ts else None
                btc_rows = (btc_series_cached(slug, m_open_ts, time.time())
                            if m_open_ts else [])
                btc_open_px = btc_close_at(btc_rows, m_open_ts) if m_open_ts else None
                btc_now_px = btc_close_at(btc_rows, time.time())
                if btc_now_px is None:
                    btc_now_px = btc_spot_usd()
                mom_side, btc_move = momentum_direction(
                    btc_open_px, btc_now_px, args.btc_move_usd_min)
                btc_dir = mom_side
                if mom_side is None:
                    log_attempt(report, {
                        'ts': ts_utc(),
                        'slug': slug,
                        'status': 'skip_no_btc_momentum',
                        'btc_move_usd': btc_move,
                        'btc_move_usd_min': args.btc_move_usd_min,
                        'seconds_left': sec_left,
                    })
                    err_streak = 0
                    time.sleep(args.poll_sec)
                    continue
                side = mom_side
                snap_side = 'up' if side == 'UP' else 'dn'
                trigger_price = snap[f'{snap_side}_ask']
                if trigger_price is None or float(trigger_price) < args.threshold:
                    log_attempt(report, {
                        'ts': ts_utc(),
                        'slug': slug,
                        'status': 'skip_price_below_threshold',
                        'threshold': args.threshold,
                        'side': side,
                        'btc_move_usd': btc_move,
                        'seconds_left': sec_left,
                    })
                    err_streak = 0
                    time.sleep(args.poll_sec)
                    continue
                if skew_veto(side, up_ask, dn_ask, args.skew_veto_threshold):
                    log_attempt(report, {
                        'ts': ts_utc(),
                        'slug': slug,
                        'status': 'skip_skew_veto',
                        'side': side,
                        'btc_move_usd': btc_move,
                        'clob_up_ask': up_ask,
                        'clob_down_ask': dn_ask,
                        'seconds_left': sec_left,
                    })
                    err_streak = 0
                    time.sleep(args.poll_sec)
                    continue
            else:
                candidates: list[tuple[str, float]] = []
                if up_ask is not None and float(up_ask) >= args.threshold:
                    candidates.append(('UP', float(up_ask)))
                if dn_ask is not None and float(dn_ask) >= args.threshold:
                    candidates.append(('DOWN', float(dn_ask)))

                if not candidates:
                    log_attempt(report, {
                        'ts': ts_utc(),
                        'slug': slug,
                        'status': 'skip_price_below_threshold',
                        'threshold': args.threshold,
                        'clob_up_ask': up_ask,
                        'clob_down_ask': dn_ask,
                        'seconds_left': sec_left,
                    })
                    err_streak = 0
                    time.sleep(args.poll_sec)
                    continue

                side, trigger_price = sorted(candidates, key=lambda x: x[1], reverse=True)[0]

            # Pre-entry execution guards (#6, #7, #8) on the picked side.
            snap_side = 'up' if side == 'UP' else 'dn'
            side_spread = snap[f'{snap_side}_spread']
            side_ask = snap[f'{snap_side}_ask']
            side_ask_size = snap[f'{snap_side}_ask_size']
            side_notional = (side_ask * side_ask_size) if (side_ask is not None) else None
            side_age = snap['quote_age_sec'] if snap['quote_age_known'] else None
            gate_checks = [
                ('skip_spread_guard', spread_gate(side_spread, args.max_spread)),
                ('skip_liquidity_guard', liquidity_gate(side_notional, args.min_top_ask_notional_usd)),
                ('skip_stale_quote', staleness_gate(side_age, args.max_quote_age_sec)),
            ]
            gate_failed = None
            for gate_status, (gate_ok, gate_reason) in gate_checks:
                if not gate_ok:
                    gate_failed = (gate_status, gate_reason)
                    break
            if gate_failed is not None:
                gate_status, gate_reason = gate_failed
                log_attempt(report, {
                    'ts': ts_utc(),
                    'slug': slug,
                    'status': gate_status,
                    'reason': gate_reason,
                    'side': side,
                    'side_spread': side_spread,
                    'side_ask_notional_usd': side_notional,
                    'btc_move_usd': btc_move,
                    'quote_age_sec': snap['quote_age_sec'],
                    'quote_age_known': snap['quote_age_known'],
                })
                err_streak = 0
                time.sleep(args.poll_sec)
                continue

            # Duplicate-position guard (#33): skip if the wallet already
            # holds either side's token (e.g. a crashed run's position, or
            # a manual trade). Lookup failures fail open — a warn is logged
            # and the entry attempt proceeds.
            if args.wallet_address:
                try:
                    _dup = position_in_tokens(
                        wallet_positions(args.wallet_address), [up_t, dn_t])
                except Exception as e:
                    _dup = None
                    log_attempt(report, {'ts': ts_utc(), 'slug': slug,
                                         'status': 'warn_positions_lookup_failed',
                                         'error': str(e)})
                if _dup is not None:
                    log_attempt(report, {'ts': ts_utc(), 'slug': slug,
                                         'status': 'skip_duplicate_position',
                                         'side': side,
                                         'size': _dup.get('size')})
                    try:
                        alerts.emit(args.runtime_dir, 'blocked',
                                    {'reason': 'duplicate_position',
                                     'side': side}, args.alert_webhook_url)
                    except Exception:
                        pass
                    err_streak = 0
                    time.sleep(args.poll_sec)
                    continue

            out, objs = run_open(args.repo, slug, side, stake_usd, args.execute,
                                 args.max_spread, args.min_top_ask_notional_usd,
                                 args.equity_usd, args.max_notional_usd)
            post = None
            runner = None
            for o in objs:
                if isinstance(o, dict) and 'order_post_result' in o:
                    runner = o
                    post = o.get('order_post_result') or {}
            if post and post.get('success') is True and str(post.get('status', '')).lower() == 'matched':
                token_id = str(runner.get('token_id') or (up_t if side == 'UP' else dn_t))
                shares = float(post.get('takingAmount') or 0)
                cost = float(post.get('makingAmount') or 0)
                entry_price = float(runner.get('entry_price') or trigger_price)
                opened = {
                    'opened_at': ts_utc(),
                    'market_slug': slug,
                    'market_end_iso': end_iso,
                    'side': side,
                    'token_id': token_id,
                    'entry_price': entry_price,
                    'shares': shares,
                    'cost_usdc': cost,
                    'open_order_id': post.get('orderID'),
                    'open_tx': (post.get('transactionsHashes') or [None])[0],
                    # Micro-hedge bookkeeping (#13): the opposite-side token
                    # is known at entry; placement happens in the monitor
                    # loop when hedge_triggered() fires (fire-once).
                    'hedge_token_id': str(dn_t if side == 'UP' else up_t) if (dn_t if side == 'UP' else up_t) else None,
                    'hedge_placed': False,
                    'hedge_attempted': False,
                    # Momentum context (#1, #2, #3): BTC move that
                    # authorized this entry (None in legacy trigger mode).
                    'btc_move_usd_at_entry': btc_move,
                    'btc_direction_at_entry': btc_dir,
                }
                report['open_raw'] = out[-4000:]
                # Machine-readable outcome for watchers (#29): the watcher
                # stops its loop on '"decision": "enter"'.
                report['decision'] = 'enter'
                err_streak = 0
                if ledger_file is not None:
                    _ledger = load_ledger(ledger_file, utc_today())
                    record_open(_ledger)
                    if not save_ledger(ledger_file, _ledger):
                        report['ledger_warn'] = 'save_failed'
                # Ops hooks (#25 trade DB, #26 BTC context, #27 alert,
                # #33 crash-recovery state). All best-effort: a failure
                # here must never undo a filled entry.
                btc_entry = btc_spot_usd()
                if btc_entry is not None:
                    opened['btc_entry_usd'] = btc_entry
                try:
                    _con = tradedb.connect(args.runtime_dir)
                    opened['trade_db_id'] = tradedb.record_open(
                        _con, mode='live' if args.execute else 'dry',
                        market_slug=slug, side=side, token_id=token_id,
                        entry_price=entry_price, shares=shares, cost_usdc=cost,
                        btc_entry=btc_entry, open_order_id=post.get('orderID'))
                    _con.close()
                except Exception as e:
                    report['tradedb_warn'] = str(e)
                try:
                    save_open_position(args.runtime_dir, opened)
                except Exception as e:
                    report['position_state_warn'] = str(e)
                try:
                    alerts.emit(args.runtime_dir, 'entry',
                                {'side': side, 'market_slug': slug,
                                 'entry_price': entry_price, 'shares': shares,
                                 'cost_usdc': cost, 'btc_entry_usd': btc_entry,
                                 'execute': bool(args.execute)},
                                args.alert_webhook_url)
                except Exception:
                    pass
                break
            else:
                report['last_open_try'] = out[-2000:]
                err_streak += 1
                if error_budget_exceeded(err_streak, args.max_consecutive_errors):
                    abort_run('aborted_consecutive_errors', 'aborted')
                    return
        except Exception as e:
            log_attempt(report, {'ts': ts_utc(), 'status': 'error', 'error': str(e)})
            err_streak += 1
            if error_budget_exceeded(err_streak, args.max_consecutive_errors):
                abort_run('aborted_consecutive_errors', 'aborted')
                return
        time.sleep(args.poll_sec)

    if not opened:
        report['finished_at'] = ts_utc()
        if _shutdown_requested:
            report['result'] = 'shutdown_before_entry'
        else:
            report['result'] = 'no_entry_timeout'
        report['decision'] = 'no_entry'
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    report['opened'] = opened

    # monitor after open: stop-loss or time exit
    end_ts = None
    try:
        end_ts = dt.datetime.fromisoformat(opened['market_end_iso'].replace('Z', '+00:00')).timestamp()
    except Exception:
        end_ts = time.time() + 300

    sl_price = opened['entry_price'] * (1.0 - args.stop_loss_pct)
    report['stop_loss_price'] = sl_price

    close_reason = None
    while not _shutdown_requested:
        now = time.time()
        if now >= (end_ts - args.exit_before_sec):
            close_reason = f'time_exit_{args.exit_before_sec}s_before_end'
            break

        # Stop-loss is evaluated against the CLOB best bid of the held
        # token (#31) — the price an exit can actually fill at — instead
        # of the Gamma outcomePrices mid. This also avoids a Gamma HTTP
        # round-trip on every poll (#15, monitor path).
        try:
            side_px = clob_best_bid(opened['token_id'])
        except Exception:
            side_px = None
        report['last_side_price'] = side_px
        report['last_side_price_source'] = 'clob_best_bid'
        report['last_check_at'] = ts_utc()
        if side_px is not None and side_px <= sl_price:
            close_reason = f"stop_loss_{int(args.stop_loss_pct * 100)}pct"
            break
        # Micro-hedge placement (#13): once the held side looks almost
        # certain late in the slot, buy a small opposite-side position as
        # tail-risk insurance. Fire-once per run; placement uses the same
        # run_open path as the main entry, and any failure only logs.
        if hedge_triggered(
            side_px,
            (end_ts - now) if end_ts else None,
            trigger_price=args.hedge_trigger_price,
            trigger_seconds_left=args.hedge_trigger_seconds_left,
            enabled=hedge_enabled,
            already_placed_or_attempted=bool(
                opened.get('hedge_placed') or opened.get('hedge_attempted')),
            hedge_token_id=opened.get('hedge_token_id'),
        ):
            opened['hedge_attempted'] = True
            hedge_side = 'DOWN' if opened.get('side') == 'UP' else 'UP'
            hedge_size = hedge_sizing(
                opened.get('cost_usdc'),
                share_pct=args.hedge_share_pct,
                min_notional_usdc=args.hedge_min_notional_usd,
                max_notional_usdc=args.hedge_max_notional_usd)
            try:
                _hout, _hobjs = run_open(
                    args.repo, opened['market_slug'], hedge_side, hedge_size,
                    args.execute, args.max_spread,
                    args.min_top_ask_notional_usd,
                    args.equity_usd, args.max_notional_usd)
                _hpost = None
                _hrunner = None
                for _ho in _hobjs:
                    if isinstance(_ho, dict) and 'order_post_result' in _ho:
                        _hrunner = _ho
                        _hpost = _ho.get('order_post_result') or {}
                if (_hpost and _hpost.get('success') is True
                        and str(_hpost.get('status', '')).lower() == 'matched'):
                    opened['hedge_placed'] = True
                    opened['hedge'] = {
                        'side': hedge_side,
                        'token_id': str((_hrunner or {}).get('token_id')
                                        or opened.get('hedge_token_id')),
                        'entry_price': float(
                            (_hrunner or {}).get('entry_price') or 0) or None,
                        'shares': float(_hpost.get('takingAmount') or 0),
                        'cost_usdc': float(_hpost.get('makingAmount') or 0),
                        'open_order_id': _hpost.get('orderID'),
                    }
                    log_attempt(report, {'ts': ts_utc(), 'status': 'hedge_placed',
                                         'side': hedge_side,
                                         'notional': hedge_size})
                    try:
                        alerts.emit(args.runtime_dir, 'hedge',
                                    {'side': hedge_side,
                                     'notional': hedge_size,
                                     'hedge': opened['hedge']},
                                    args.alert_webhook_url)
                    except Exception:
                        pass
                    try:
                        save_open_position(args.runtime_dir, opened)
                    except Exception as e:
                        report['position_state_warn'] = str(e)
                else:
                    log_attempt(report, {'ts': ts_utc(),
                                         'status': 'hedge_failed',
                                         'raw': _hout[-2000:]})
            except Exception as e:
                log_attempt(report, {'ts': ts_utc(), 'status': 'hedge_error',
                                     'error': str(e)})
        time.sleep(args.poll_sec)

    # SIGTERM/SIGINT during monitoring still runs the close cascade below
    # with this reason (#14) instead of orphaning the position.
    if close_reason is None:
        close_reason = 'shutdown'

    # Shared exit cascade (FAK -> GTC -> poll/cancel/force). The same
    # function closes the micro-hedge leg below.
    close_obj, out, close_debug, fallback_used, force_close_used = close_position(
        args.repo,
        opened['market_slug'],
        opened['side'],
        opened['token_id'],
        opened['shares'],
        args.execute,
        close_retry_max=args.close_retry_max,
        close_retry_delay_sec=args.close_retry_delay_sec,
        force_discount_pct=args.force_close_discount_pct,
        force_max_discount_abs=args.force_close_max_discount_abs,
        last_side_price=report.get('last_side_price'),
        entry_price=opened.get('entry_price'),
        label='main',
    )

    # Micro-hedge close leg (#13): the hedge is exited through the same
    # cascade, and its economics fold into the combined PnL below.
    hedge_closed = None
    hedge_close_usdc = 0.0
    hedge_cost_usdc = 0.0
    if opened.get('hedge_placed') and isinstance(opened.get('hedge'), dict):
        _h = opened['hedge']
        hedge_cost_usdc = float(_h.get('cost_usdc') or 0)
        (_h_close_obj, _h_out, _h_debug, _h_fallback,
         _h_force) = close_position(
            args.repo,
            opened['market_slug'],
            str(_h.get('side') or ''),
            str(_h.get('token_id') or ''),
            float(_h.get('shares') or 0),
            args.execute,
            close_retry_max=args.close_retry_max,
            close_retry_delay_sec=args.close_retry_delay_sec,
            force_discount_pct=args.force_close_discount_pct,
            force_max_discount_abs=args.force_close_max_discount_abs,
            last_side_price=None,
            entry_price=_h.get('entry_price'),
            label='hedge',
        )
        close_debug.extend(_h_debug)
        _h_post = _h_close_obj.get('order_post_result') or {}
        _h_status = str(_h_post.get('status') or '').lower()
        hedge_close_usdc = float(_h_post.get('takingAmount') or 0)
        hedge_closed = {
            'close_success': bool(
                _h_post.get('success') is True
                and (_h_status == 'matched' or hedge_close_usdc > 0)),
            'close_status': _h_post.get('status'),
            'close_order_id': _h_post.get('orderID'),
            'close_tx': (_h_post.get('transactionsHashes') or [None])[0],
            'close_shares': float(_h_post.get('makingAmount') or 0),
            'close_usdc': hedge_close_usdc,
            'close_skipped': _h_close_obj.get('close_skipped'),
        }
        report['hedge_closed'] = hedge_closed
        if _h_fallback:
            report['hedge_close_fallback'] = _h_fallback
        if _h_force:
            report['hedge_close_force'] = _h_force

    post = close_obj.get('order_post_result') or {}
    post_status = str(post.get('status') or '').lower()
    close_usdc = float(post.get('takingAmount') or 0)
    closed = {
        'close_reason': close_reason,
        'closed_at': ts_utc(),
        'close_success': bool(post.get('success') is True and (post_status == 'matched' or close_usdc > 0)),
        'close_status': post.get('status'),
        'close_order_id': post.get('orderID'),
        'close_tx': (post.get('transactionsHashes') or [None])[0],
        'close_shares': float(post.get('makingAmount') or 0),
        'close_usdc': close_usdc,
        'close_skipped': close_obj.get('close_skipped'),
    }
    report['close_debug'] = close_debug
    if fallback_used:
        report['close_fallback'] = fallback_used
    if force_close_used:
        report['close_force'] = force_close_used
    report['close_raw'] = out[-4000:]
    report['closed'] = closed

    pnl = None
    if closed['close_usdc']:
        # Combined economics (#13): main proceeds + hedge proceeds minus
        # both notionals. Without a hedge leg the extra terms are zero.
        pnl = round(closed['close_usdc'] + hedge_close_usdc
                    - opened['cost_usdc'] - hedge_cost_usdc, 6)
    report['realized_cashflow_pnl_usdc'] = pnl
    report['hedge_pnl_usdc'] = (round(hedge_close_usdc - hedge_cost_usdc, 6)
                                if hedge_closed else None)
    if ledger_file is not None:
        _ledger = load_ledger(ledger_file, utc_today())
        record_close(_ledger, pnl)
        if not save_ledger(ledger_file, _ledger):
            report['ledger_warn'] = 'save_failed'
    # Ops close hooks (#25 trade DB, #26 BTC context, #27 alert,
    # #33 clear crash-recovery state). Best-effort, like the entry hooks.
    btc_exit = btc_spot_usd()
    if btc_exit is not None:
        closed['btc_exit_usd'] = btc_exit
    if opened.get('trade_db_id') is not None:
        try:
            _con = tradedb.connect(args.runtime_dir)
            tradedb.record_close(
                _con, int(opened['trade_db_id']),
                close_reason=close_reason,
                close_usdc=close_usdc or None,
                pnl_usdc=pnl, btc_exit=btc_exit,
                close_tx=closed.get('close_tx'))
            _con.close()
        except Exception as e:
            report['tradedb_warn'] = str(e)
    try:
        clear_open_position(args.runtime_dir)
    except Exception as e:
        report['position_state_warn'] = str(e)
    try:
        alerts.emit(args.runtime_dir, 'close',
                    {'close_reason': close_reason,
                     'close_success': closed.get('close_success'),
                     'close_usdc': close_usdc, 'pnl_usdc': pnl,
                     'hedge_placed': bool(opened.get('hedge_placed')),
                     'hedge_close_usdc': hedge_close_usdc,
                     'hedge_pnl_usdc': report.get('hedge_pnl_usdc'),
                     'btc_exit_usd': btc_exit},
                    args.alert_webhook_url)
    except Exception:
        pass
    report['finished_at'] = ts_utc()
    report['result'] = 'done'

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
