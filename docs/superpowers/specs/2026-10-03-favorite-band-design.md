# 5m BTC Favorite-Band Build Spec — $200 Paper-First to $2k Path

Date: 2026-10-03
Host repo: `5min-btc-polymarket` (branch `audit-fixes-batch-1`, HEAD `7fd85b5`)
Mode: paper-first. No live orders in this spec.

## 1. Agreed intent

- Goal: reproduce deleted `#2 sillygoose` mid-band edge on top of proven local runner to compound $200 -> $2k.
- Constraints: `#1` (Reddit Go $20->$180) has no repo. `#2` (`slimopump/sillygoose`, `5-Min-Up-Down-Bot`) 404s on both jina + `gh repo view`. Do not depend on them. Use captured spec + local code.
- Evidence: `data/vm-final-20260923/btc5m_trades.sqlite` = 18 trades, 16W/2L (88.9%), +$4.617 on $8 stakes. Entries 0.85-0.98, `time_exit_40s_before_end + resolution_settle/backfill`, 2x `stop_loss_25pct`. `runtime/btc5m_trades.sqlite` = 0 rows — today's 30-min run did not persist, must fix runtime-dir handling.
- Success: paper ledger shows positive expectancy after 1-2c slippage over >=200 trades, max DD inside daily-stop envelope, `book_fill_rate` reported. Only then $1-stake live validation.

## 2. What we keep (do not regress)

- Runner: `scripts/test_btc_5m_session_exit_sl.py` (2124 lines) — momentum since market open (Binance 1m, cached 30s), `btc_move_usd_min` 70 cons / 50 aggr, skew veto 0.10/0.15, 40s exit, 300s linger + backfill, clock-fixed wake, executable-bid stop-loss.
- Risk: `scripts/btc5m_guards.py` (327 lines) — spread gate 0.03, top-ask notional >=$30, quote-stale 8s, 3-error abort, UTC-day ledger (max trades/day, daily max loss, live only).
- Paper: `paper/pm_paper_trade_runner.py` (332 lines) — same CLI/JSON as private live runner, buys lift ask / sells hit bid, refuses `--execute`.
- Ledger: `scripts/btc5m_tradedb.py` — `recent / summary / exits` (book vs redemption mix).
- Profiles: `config/btc_5m_profiles.yaml` — conservative 0.60 floor / 8% capped $8 / 12 trades/day, aggressive 0.70 / 15% capped $15 / 20 trades/day.
- Tests: `scripts/tests/` — 129+ unit tests must stay green.

Reference-only (do not copy folders):
- `~/projects/polypaper-bot/` — borrow rolling-WR pause + kill-switch semantics.
- `~/projects/polymarket-auto-trading-agent/polymarket/` — Gamma/CLOB read client.
- `~/projects/py_polymarket_hft_mm/utils/orderbook.py` — WebSocket pattern.

## 3. Change: favorite-band volume leg (growth) + base leg (survival)

Work in place on new branch `feat-favorite-band` (worktree `.worktrees/feat-favorite-band`). No new merged folder.

### 3a. New module `scripts/btc5m_favorite_band.py` (~150 lines)

```python
def allow_entry(*, side_gap_usd: float, fav_ask: float, min_gap_usd: float) -> bool
# True iff abs(gap) >= min_gap_usd AND 0.50 <= fav_ask <= 0.70 (prefer 0.52-0.57)
def size_for_band(*, equity_usd: float) -> float
# fixed $10 paper, capped at 5% equity once equity>200, floored $1
```

Rules (ports #2 spec with #1 sizing):
- Direction = sign of `spot - slot_open` (Binance 1m close). Skip if |gap| < min_gap (70 cons).
- Favorite = side the book leans to. Require `fav_ask` in [0.50, 0.70]. Log `prefer_zone` if 0.52-0.57 else `edge_thin`.
- Order: FOK taker buy $10 (paper lifts ask). Never sell. Never hedge opposite side in this leg. Hold to 40s exit, else linger 300s -> redeem $1/$0 via backfill.
- Disable stop-loss for this leg (stop turns mid-band edge into noise loss). Keep stop only for base leg.

### 3b. Runner wiring (minimal diff)

- Add `--entry-mode {base,band,both}` (default `both` in paper). `base` = current 0.60+ behavior with stop. `band` = section 3a with no stop. `both` = try band first when `fav_ask<=0.70`, else base when `ask>=0.85`.
- Add `--band-min-gap-usd`, `--band-max-ask` (default 0.70), `--band-stake-usd` (default 10).
- Per-trade log: `gap_usd, fav_ask, prefer_zone(bool), mode(base|band), decision(JSON)`.
- Fix: require explicit `--runtime-dir` or default `<repo>/runtime` consistently so paper trades persist (today's 0-row bug).

### 3c. Profiles

```yaml
conservative:
  signal: {threshold_price: 0.60}  # unchanged base
  band: {enabled: true, max_ask: 0.70, prefer_low: 0.52, prefer_high: 0.57, stake_usd: 10, stop: false}
  sizing: {max_notional_usd: 8}  # base cap stays; band uses own $10 cap
aggressive:
  band: {enabled: true, max_ask: 0.70, stake_usd: 12, stop: false}
```

### 3d. Guards (borrow from polypaper, implement in `btc5m_guards.py`)

- Rolling WR pause: if last 50 paper trades WR <55%, skip band entries, base only.
- Daily kill: -3 band clips or `daily_max_loss_pct` hit -> stop with machine-readable `decision: kill_switch`.
- Stale-data gate already exists (8s) — reuse for band.

## 4. Data flow

1. Scan `candidate_slots [prev,current,next,next2]` -> closest valid (active, not closed, >=60s left).
2. Fetch Binance 1m `spot`, slot `openPrice` -> gap. Fetch CLOB book -> `fav_ask`, spread, top-ask notional.
3. Guards: spread<=0.03, notional>=$30, quote age<=8s, errors<3, day ledger OK, rolling WR OK.
4. `allow_entry()` -> mode select -> paper FOK -> monitor to 40s exit -> linger/backfill -> ledger write with `btc_entry/btc_exit`.
5. `btc5m_tradedb.py exits` reports book_fill_rate; regime shift triggers review.

## 5. Testing & graduation (paper-first)

- Unit: `scripts/tests/test_favorite_band.py` — band edges (0.49 reject, 0.50 allow, 0.57 prefer, 0.70 allow, 0.71 reject), gap filter, sizing caps, no-stop invariant, decision JSON.
- Regression: full `pytest scripts/tests/` green (129+ N new).
- Paper bar (all must hold): >=200 paper trades, net expectancy >0 after 1-2c slippage, max DD inside daily-stop, `book_fill_rate` reported. Then $1-stake live path test (post + cancel clean, no PnL claim).
- 10x sanity: at 55c avg, 65% WR -> EV +18%/clip (~125 trades to 10x at 15% sizing). At 60% WR -> ~518 trades. If rolling WR <60% over 200, stay paper.

## 6. Files touched

- NEW `scripts/btc5m_favorite_band.py`, `scripts/tests/test_favorite_band.py`
- EDIT `scripts/test_btc_5m_session_exit_sl.py` (entry-mode + flags + logging + runtime-dir fix)
- EDIT `scripts/btc5m_guards.py` (rolling WR + band kill)
- EDIT `config/btc_5m_profiles.yaml` (band block)
- NEW `docs/superpowers/specs/2026-10-03-favorite-band-design.md` (this file)

Explicitly NOT touched: live execution stack, keys, `polypaper-bot/` / `polymarket-auto-trading-agent/` (reference only).

## 7. Acceptance

- [ ] Worktree `.worktrees/feat-favorite-band` on branch `feat-favorite-band`, baseline pytest green recorded.
- [ ] Band entries only fire in 0.50-0.70 with gap agreement; >0.70 rejected in logs.
- [ ] 30-min paper run persists rows in `runtime/btc5m_trades.sqlite` (fixes 0-row bug).
- [ ] `summary` + `exits` show expectancy and book_fill_rate.
- [ ] No live orders possible from paper path (`--execute` refused by simulator).

## 8. Rollout

1. Worktree + baseline tests.
2. Implement 3a-3d + tests.
3. 30-min paper, verify persistence.
4. 200-trade paper graduation.
5. Then (separate spec) $1 live validation.
