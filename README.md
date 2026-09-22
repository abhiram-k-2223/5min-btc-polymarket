# 5min BTC Polymarket Skill

Open-source OpenClaw skill for **BTC 5-minute Up/Down** markets on Polymarket.

Repository: https://github.com/Novals83/5min-btc-polymarket

## Recent Fixes
A repo audit surfaced ~30 execution, risk, and ops gaps; the following are
now fixed and covered by 110 unit tests (`scripts/tests/`):
- Daily loss cap + max trades/day enforced via a JSON risk ledger (live trades only)
- Pre-entry spread / liquidity / quote-staleness gates on the CLOB book
- Consecutive-error abort, and a machine-readable `decision` field the watcher stops on
- Stop-loss monitored at the executable CLOB bid (not the Gamma mid)
- SQLite trade ledger, spot-BTC entry/exit logging, `alerts.log` + webhook alerts
- Crash recovery (`open_position.json` + `--resume`) and a duplicate-position guard
- Pendulum-based backtest harness (`scripts/btc5m_backtest.py`), validated on a real market
- Pinned `requirements.txt`, working Dockerfile/compose, log-hygiene fixes
- Profiles actually differ (conservative: 0.70 threshold, strict guards, 8%-of-equity stake capped at $8; aggressive: 0.65 threshold, looser guards, 15% capped at $15) with equity-derived sizing (`--risk-per-trade-pct`, `--max-notional-usd`)
- Graceful shutdown (SIGTERM/SIGINT runs the close cascade; `ctl.sh stop` waits ~30s before SIGKILL), proportional force-close pricing, and a live fire-once micro-hedge with combined PnL
- Momentum entry matching the documented strategy: side follows the BTC move since market open (Binance 1m klines, `--btc-move-usd-min` 70/50), threshold is a price floor, skew veto blocks entries against crowd flow (`--skew-veto-threshold` 0.10/0.15); `--disable-momentum` restores the legacy trigger
- Bookless exits settle at resolution: time exit moved to 40s before end (makers pull 5m quotes in the final seconds), and when the book is gone the paper shim credits the resolved $/share instead of $0; every close-debug row carries the failure reason

More problems from the audit are still being worked through — strategy edges,
order-book depth modeling, and remaining ops items. Contributions welcome.

## Strategy (Momentum into Close)
This skill is aligned with a short-horizon momentum strategy:

1. Trade BTC 5m event markets near expiry.
2. Main entry window: around **2 minutes left**.
3. Confirm that BTC has already moved by about **$70-$100** in the active interval.
4. Check market skew (crowd positioning). If flow supports the move direction, enter **with** momentum.
5. Typical sizing: around **50% of trading allocation** (user-defined risk tolerance).
6. Optional micro-hedge when skew is extreme (for example, 95/5): place a small opposite position ($1-$2 equivalent) to reduce tail risk.

This is a momentum-following approach, not a reversal strategy.

## Repository Structure
- `SKILL.md` — skill definition and operating rules
- `config/` — profiles and risk parameters
- `scripts/` — runners/wrappers/hot commands
- `examples/` — practical command examples

## Deploy / Run
### Prerequisites
- OpenClaw environment
- Polymarket execution stack available at:
  - `<your-workspace>/pm-hl-conservative-plus-repo`
- Python virtual env for runner scripts
- Valid API credentials configured outside this repository

### Quick Start
```bash
git clone https://github.com/Novals83/5min-btc-polymarket.git
cd 5min-btc-polymarket
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Read:
- `SKILL.md`
- `config/btc_5m_profiles.yaml`

Run a conservative real test (example):
```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile conservative --execute
```

Run aggressive profile:
```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile aggressive --execute
```

Unified skill control (recommended):
```bash
scripts/btc5m_ctl.sh start --profile conservative
scripts/btc5m_ctl.sh status
scripts/btc5m_ctl.sh report --limit 20
scripts/btc5m_ctl.sh stop
```

Runtime isolation:
- skill runtime dir: `./runtime`
- auth/env source (default): `<your-workspace>/pm-hl-conservative-plus-repo/.env`
- overrides: `BTC5M_REPO`, `BTC5M_ENV_FILE`, `BTC5M_RUNNER`
- completion auto-report cron (topic 184): `btc5m-completion-autoreport-topic184`

Optional Docker isolation (builds `Dockerfile` with pinned deps; default
`up` runs a conservative **dry-run** session — no orders without `--execute`):
```bash
scripts/btc5m_docker.sh build
scripts/btc5m_docker.sh up
scripts/btc5m_docker.sh status
scripts/btc5m_docker.sh run -- --profile conservative --execute  # live
scripts/btc5m_docker.sh down
```
Env overrides: `BTC5M_ENV_FILE` (creds), `BTC5M_EXEC_REPO`
(external execution repo, default `../pm-hl-conservative-plus-repo`),
`BTC5M_RUNTIME_DIR` (default `./runtime`).

## Ops: Trade DB, Alerts, Recovery, Backtest

Every session (dry or live) writes to a SQLite trade ledger and an alert
queue inside the runtime dir:

```bash
python scripts/btc5m_tradedb.py recent --runtime-dir runtime --limit 10
python scripts/btc5m_tradedb.py summary --runtime-dir runtime --days 7
python scripts/btc5m_alerts.py --runtime-dir runtime
```

Entry/close/abort/blocked/resumed events also append to
`runtime/alerts.log`. Set `BTC5M_ALERT_WEBHOOK` (or
`--alert-webhook-url`) for a Slack-compatible webhook POST on top.
Spot BTC/USD at entry and exit is logged from Binance (Coinbase fallback).

Crash recovery: the runner writes `runtime/open_position.json` on entry
and deletes it on close. A leftover file blocks new runs — unless
`--resume` is passed, which monitors the recorded position to exit.
`--wallet-address` (or `BTC5M_WALLET_ADDRESS`) enables a pre-entry
duplicate-position check via the public data-api; lookup failures fail
open with a warning.

Backtest the entry/exit logic against real Pendulum orderbook archives
(no auth needed; hour files are large, fetch one market at a time):

```bash
# 1. find a past 5m market's token IDs (Gamma, no auth)
curl -s "https://gamma-api.polymarket.com/events?slug=btc-updown-5m-<ts>" | python3 -c "..."
# 2. export its book snapshots for the market hour (needs: pip install duckdb)
python scripts/btc5m_backtest.py fetch --up-token-id <UP> --dn-token-id <DN> \
  --hour 2026-09-18T19 --out data/backtest/mkt.jsonl
# 3. replay with runner-equivalent parameters
python scripts/btc5m_backtest.py replay --snapshots data/backtest/mkt.jsonl \
  --market-end <unix_ts> --threshold 0.70 --stop-loss-pct 0.30
```

Tops are derived from the bid/ask ladders (the archive's `best_*`
columns are null for these hours), and quote age is proxied by the gap
since the previous same-side snapshot. Positions still open at end of
data are flagged `end_of_data` and excluded from metrics.

## Execution Checklist (Before Live Trade)
Use this quick pre-flight checklist before any real order:

1. **Market validity** (automated: slot scan rejects closed/inactive/ending markets)
   - Confirm the BTC 5m market is active and not about to close unexpectedly.
2. **Time-to-close window** (operator judgment; runner enforces only the minimum)
   - Prefer entries around ~120 seconds left (with reasonable tolerance).
3. **Impulse confirmation** (automated: `--btc-move-usd-min` since market open)
   - Confirm the observed BTC move is meaningful (strategy reference: ~$70-$100).
4. **Skew confirmation** (automated: `--skew-veto-threshold` vetoes opposed entries)
   - Verify market skew supports the intended direction (do not fade strong momentum by default).
5. **Liquidity/spread checks** (automated: spread / top-ask-notional / quote-age gates)
   - Ensure spread and top-of-book notional pass your minimum thresholds.
6. **Sizing guardrails** (automated: equity-based stake cap + daily loss / max-trades ledger)
   - Validate stake, max notional, and daily loss limits before execution.
7. **Stop / exit controls** (automated: CLOB-bid stop-loss + `exit_before_sec`)
   - Confirm stop-loss and `exit_before_sec` are configured.
8. **Execution mode** (operator: pass `--execute` explicitly)
   - Start in dry-run when changing parameters; switch to `--execute` only after validation.

## Risk Controls Template
Suggested baseline controls (adapt to your risk profile):

- **Per-trade risk cap**: 1%-15% of account equity (profile dependent)
- **Daily max loss**: hard stop at 10%-15%
- **Max trades/day**: fixed ceiling to avoid overtrading
- **Max notional/trade**: strict upper bound
- **Quote staleness guard**: skip if market data is stale
- **Spread guard**: skip when spread exceeds threshold
- **Liquidity guard**: skip when top ask/bid notional is too thin
- **Extreme skew hedge**: optional small opposite hedge in 95/5-type scenarios
- **Operational kill switch**: immediate stop on repeated API/DNS/execution failures

## Risk Notice
This repository is educational/operational infrastructure, not financial advice.
Use your own risk limits, daily loss caps, and capital controls.

## Contributing
- Fork the repository
- Create a feature branch
- Commit changes
- Open a PR to `main`

PRs are welcome.
