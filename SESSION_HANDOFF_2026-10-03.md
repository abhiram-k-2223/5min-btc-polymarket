# Session Handoff — 2026-10-03 Favorite-Band Build

## Goal
$200 -> $2,000 via hybrid Polymarket 5m BTC bot. Paper-first. No live orders yet.

## Where we are
- Host repo: `~/projects/5min-btc-polymarket`, branch `audit-fixes-batch-1`, HEAD `71422e7` (spec commit).
- Your runner is proven: `data/vm-final-20260923/btc5m_trades.sqlite` = 18 trades, 16W/2L (88.9%), +$4.617 on $8 stakes. Entries 0.85-0.98, `time_exit_40s_before_end + resolution_settle/backfill`. Commit `7fd85b5` lowered threshold 0.70->0.60 after 672-market sweep (+$237, 78% WR).
- Bug: `runtime/btc5m_trades.sqlite` = 0 rows. Today's 30-min +$4.89 run did not persist. Likely `--runtime-dir` mismatch. Must fix first.
- Research done: `#1` Reddit $20->$180 Go bot (no repo, 91% WR unaudited) vs `#2` sillygoose mid-band (spec captured, repo 404 both `sillygoose` + `5-Min-Up-Down-Bot`). Verdict: #2 logic wins, use with #1 sizing ($10-15 not $30-60).
- Cloned reference-only in `~/projects/`: `polymarket-auto-trading-agent/` (#3 base), `polypaper-bot/` (#5 guards), `py_polymarket_hft_mm/` (BPS engine). Do not merge folders.

## Start building from here
1. Read `docs/superpowers/specs/2026-10-03-favorite-band-design.md` — approved direction, file list, acceptance.
2. Invoke skills in order: `brainstorming` is done (path: architectural, approach A approved). Next is `writing-plans` to break spec section 3-5 into steps. Do not write product code before plan approval.
3. Isolation: normal checkout (not worktree). Pending user approval to create `.worktrees/feat-favorite-band` on branch `feat-favorite-band`. Ask before creating. Fallback: `git worktree add .worktrees/feat-favorite-band -b feat-favorite-band`, then `pip install -r requirements.txt`, baseline `pytest scripts/tests/`.
4. Implement per spec: NEW `scripts/btc5m_favorite_band.py` + test, EDIT runner (`--entry-mode base|band|both`), EDIT `scripts/btc5m_guards.py` (rolling 55% WR pause + -3-clip kill), EDIT `config/btc_5m_profiles.yaml` (band block: 0.50-0.70, prefer 0.52-0.57, $10, no stop).
5. Verify: 30-min paper persists to `runtime/`, `scripts/btc5m_tradedb.py summary/exits` shows expectancy + book_fill_rate. Bar: >=200 trades, EV>0 after 1-2c slippage, DD inside daily stop. Then $1 live (separate spec).

## Key files
- `scripts/test_btc_5m_session_exit_sl.py` (2124 lines), `scripts/btc5m_guards.py` (327), `paper/pm_paper_trade_runner.py` (332), `scripts/btc5m_tradedb.py`, `config/btc_5m_profiles.yaml`
- Spec: `docs/superpowers/specs/2026-10-03-favorite-band-design.md`
- Prior notes: `polymarket-bots.md` (in `~/projects/`), `README.md` (129 tests, graduation bar)

## Constraints
- Paper simulator refuses `--execute`. Real execution needs private stack (unsolved `i2` in `problems.md`).
- CEX feed: Binance 1m cached 30s, Coinbase fallback (Binance 451 on US IPs). UA must be explicit.
- Do not touch keys, live stack, or reference clones except thin imports.
