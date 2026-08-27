# -*- coding: utf-8 -*-
"""Rate-limited operator alerts. Webhook secrets are environment-only."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone


JST = timezone(timedelta(hours=9), "JST")
_LAST_SENT: dict[str, float] = {}


def _truthy(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _url() -> str:
    return os.environ.get("BOT_MANUAL_ALERT_WEBHOOK_URL", "").strip() or os.environ.get("DISCORD_WEBHOOK_URL", "").strip()


def notify_manual_action_required(*, bot: str, reason: str, details: dict[str, object], action: str) -> bool:
    if not _truthy("BOT_MANUAL_ALERT_ENABLED", True):
        return False
    url = _url()
    if not url:
        return False
    key = f"{bot}:{reason}:{json.dumps(details, sort_keys=True, ensure_ascii=False)}"
    try:
        interval = max(0.0, float(os.environ.get("BOT_MANUAL_ALERT_MIN_INTERVAL_SECONDS", "300")))
    except ValueError:
        interval = 300.0
    now = time.time()
    if now - _LAST_SENT.get(key, 0.0) < interval:
        return False
    _LAST_SENT[key] = now
    content = (
        f"[manual-action] {bot} XAUUSD: {reason}\n"
        f"details: {json.dumps(details, ensure_ascii=False, sort_keys=True)}\n"
        f"action: {action}\n"
        f"time_jst: {datetime.now(JST).isoformat(timespec='seconds')}"
    )[:1900]
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps({"content": content}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "bot25-manual-alert/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5.0) as response:
            return 200 <= int(response.status) < 300
    except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        logging.error("bot25 manual alert failed: %s", exc)
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    if not args.test:
        parser.print_help()
        return 2
    if not _url():
        print("manual alert webhook is not configured", file=sys.stderr)
        return 1
    sent = notify_manual_action_required(bot="bot25", reason="manual_alert_test", details={}, action="No trading action required.")
    print("manual alert test sent" if sent else "manual alert test failed")
    return 0 if sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
