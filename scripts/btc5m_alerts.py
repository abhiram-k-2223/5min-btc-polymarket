#!/usr/bin/env python3
"""Alert sink for the BTC 5m runner (#27).

Two mechanisms, both best-effort and never raising into the trading path:
1. Append-only JSONL queue at <runtime>/alerts.log (always on). Tail it
   with ``python scripts/btc5m_alerts.py tail``.
2. Optional generic webhook POST (Slack-compatible payload) when
   ``BTC5M_ALERT_WEBHOOK`` (env) or ``--alert-webhook-url`` (flag) is set.

Events emitted by the runner: entry, close, blocked, aborted, resumed,
error, warn.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json as _json
import os as _os
import urllib.request as _request

ALERTS_FILENAME = "alerts.log"
WEBHOOK_ENV_VAR = "BTC5M_ALERT_WEBHOOK"


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def alerts_path(runtime_dir: str) -> str:
    return _os.path.join(str(runtime_dir), ALERTS_FILENAME)


def emit(runtime_dir, event: str, data=None, webhook_url: str | None = None) -> dict:
    """Record an alert; optionally POST to a webhook. Never raises."""
    alert = {"ts": utc_now_iso(), "event": str(event), "data": data or {}}
    try:
        _os.makedirs(str(runtime_dir), exist_ok=True)
        with open(alerts_path(runtime_dir), "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(alert) + "\n")
    except OSError:
        pass
    url = webhook_url or _os.environ.get(WEBHOOK_ENV_VAR)
    if url:
        _post_webhook(url, alert)
    return alert


def _post_webhook(url: str, alert: dict) -> bool:
    try:
        payload = _json.dumps(
            {"text": f"[btc5m] {alert['event']} @ {alert['ts']}: "
                     f"{_json.dumps(alert['data'])[:1500]}"}
        ).encode("utf-8")
        req = _request.Request(url, data=payload,
                               headers={"Content-Type": "application/json"},
                               method="POST")
        with _request.urlopen(req, timeout=8) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def tail(runtime_dir: str, limit: int = 20) -> list[dict]:
    try:
        with open(alerts_path(runtime_dir), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-int(limit):]:
        try:
            out.append(_json.loads(line))
        except ValueError:
            continue
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Tail the BTC 5m alert queue")
    ap.add_argument("--runtime-dir", default=None,
                    help="Runtime dir (default: <repo>/runtime)")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()
    runtime_dir = args.runtime_dir or _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "runtime")
    alerts = tail(runtime_dir, args.limit)
    for a in alerts:
        print(f"{a.get('ts')} {a.get('event')} {_json.dumps(a.get('data'))}")
    if not alerts:
        print("no alerts recorded")


if __name__ == "__main__":
    main()
