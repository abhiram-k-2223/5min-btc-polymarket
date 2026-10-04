# VM Access — btc5m-paper (GCP)

Deployed 2026-10-04. Paper-trading bot, rolling 60-min sessions. Details in
`docs/DEPLOY_GCP.md`.

## Instance

| Item | Value |
|---|---|
| Project | `huier-4eb10` (billing OK; `crafty-coral-450709-f2` has no billing attached) |
| Account | `ajax40215@gmail.com` |
| VM | `btc5m-paper`, zone `us-central1-a`, `e2-micro`, Ubuntu 22.04, 10 GB pd-standard |
| Internal IP | `10.128.0.4` |
| External IP (ephemeral) | `136.114.24.207` — required for egress (apt, Gamma, CLOB, Coinbase); ~$3/mo, the VM itself is free-tier |
| OS user | `abhiram` (SSH key managed by `gcloud compute ssh`) |

## Connect

```bash
gcloud config set account ajax40215@gmail.com
gcloud config set project huier-4eb10
gcloud compute ssh btc5m-paper --zone=us-central1-a
```

No ingress firewall rules; SSH goes over the public IP. If SSH hangs with
exit 255 right after a reboot, wait ~1 min (guest sshd) and retry; `... ssh
--troubleshoot` diagnoses VPC/key issues.

## On-VM layout (`/home/abhiram/bots/`)

- `5min-btc-polymarket/` — this repo @ `main` (deployed commit `b6f2bfd`).
  Container mounts `.../runtime` at `/skill/runtime` (ledger, trade DB,
  alerts — survives restarts).
- `exec-repo/` — paper execution shim (`src/live/pm_live_trade_runner.py`
  = copy of `paper/pm_paper_trade_runner.py`; refuses `--execute`), its
  own `.venv` (requests only), `.env` (600, empty — no keys needed).
- Image `localhost/btc5m:latest` (podman 3.4.4, rootless + linger enabled).

## Service

- `container-btc5m-paper.service` + `.timer`: hourly rolling 60-min
  sessions (`--profile conservative --entry-mode both`), enabled + active.
- Reports land in the journal (containers use `--rm`, so no `podman logs`
  after exit).

## Health checks

```bash
podman ps                                            # btc5m-paper Up = polling
systemctl --user list-timers | grep btc5m             # timer armed
journalctl --user -u container-btc5m-paper.service --no-pager -o cat | tail -30
python3 ~/bots/5min-btc-polymarket/scripts/btc5m_tradedb.py recent --runtime-dir ~/bots/5min-btc-polymarket/runtime --limit 5
python3 ~/bots/5min-btc-polymarket/scripts/btc5m_tradedb.py exits --runtime-dir ~/bots/5min-btc-polymarket/runtime --limit 50
```

Healthy: `Up` container (quiet journal while polling is normal — the
runner prints only the final report), `[runtime] runtime_dir=/skill/runtime`
at startup, `heartbeat` attempts, rows appearing in the trade DB,
`book_fill_rate` in `exits`. Verified 2026-10-04: feed `rows: 60 fresh:
True` inside the image; first session started clean.

## Cost / lifecycle

- Expected ~$3–4/mo (ephemeral IP + a few GB egress). Stop billing with
  `gcloud compute instances stop btc5m-paper --zone=us-central1-a`
  (timer resumes on start) or delete with `... instances delete ...`.
- To redeploy a new commit: pull in `~/bots/5min-btc-polymarket`, rebuild
  the image (`podman build -t localhost/btc5m:latest .`), restart the
  service. No live orders are possible from this VM (paper shim).
