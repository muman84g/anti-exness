# -*- coding: utf-8 -*-
"""Manual-action alert helpers for live bots.

Secrets are intentionally read only from environment variables. Do not store
Discord webhook URLs in repository files, state files, logs, or command output.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from argparse import ArgumentParser
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9), "JST")
DEFAULT_MIN_INTERVAL_SECONDS = 300.0
_LAST_SENT_EPOCH_BY_KEY: dict[str, float] = {}


def _env_truthy(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", "disable", "disabled"}


def _webhook_url() -> str:
    return (
        os.environ.get("BOT_MANUAL_ALERT_WEBHOOK_URL", "").strip()
        or os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    )


def _min_interval_seconds(key: str) -> float:
    if ":reconciliation:" in key:
        raw = os.environ.get("BOT_MANUAL_ALERT_RECONCILE_INTERVAL_SECONDS")
        if raw:
            try:
                return max(0.0, float(raw))
            except ValueError:
                logging.warning("Invalid manual alert reconciliation interval; using fallback")
    raw = os.environ.get("BOT_MANUAL_ALERT_MIN_INTERVAL_SECONDS") or os.environ.get(
        "DISCORD_NOTIFY_MIN_INTERVAL_SECONDS"
    )
    if not raw:
        return DEFAULT_MIN_INTERVAL_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        logging.warning("Invalid manual alert interval; using default")
        return DEFAULT_MIN_INTERVAL_SECONDS


def _jst_now() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def notify_manual_action_required(
    *,
    bot_id: str,
    symbol: str,
    title: str,
    reason: str,
    action: str,
    key: str,
) -> bool:
    """Send a rate-limited Discord alert for a manual intervention event."""

    if not _env_truthy("BOT_MANUAL_ALERT_ENABLED", True):
        return False
    url = _webhook_url()
    if not url:
        return False

    now = time.time()
    interval = _min_interval_seconds(key)
    previous = _LAST_SENT_EPOCH_BY_KEY.get(key)
    if previous is not None and now - previous < interval:
        return False
    _LAST_SENT_EPOCH_BY_KEY[key] = now

    content = (
        f"[manual-action] {bot_id} {symbol}: {title}\n"
        f"reason: {reason}\n"
        f"action: {action}\n"
        f"time_jst: {_jst_now()}"
    )
    if len(content) > 1900:
        content = content[:1897] + "..."
    payload = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "anti-exness-manual-alert/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            if 200 <= int(response.status) < 300:
                logging.warning(
                    "Manual-action Discord alert sent: bot=%s symbol=%s key=%s",
                    bot_id,
                    symbol,
                    key,
                )
                return True
            logging.error(
                "Manual-action Discord alert failed: bot=%s symbol=%s status=%s",
                bot_id,
                symbol,
                response.status,
            )
            return False
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        logging.error(
            "Manual-action Discord alert failed: bot=%s symbol=%s error=%s",
            bot_id,
            symbol,
            exc,
        )
        return False


def main() -> int:
    parser = ArgumentParser(description="Send a manual-action Discord alert test")
    parser.add_argument("--test", action="store_true", help="send a test alert using the configured webhook")
    parser.add_argument("--bot-id", default="bot18")
    parser.add_argument("--symbol", default="TEST")
    args = parser.parse_args()
    if not args.test:
        parser.print_help()
        return 2
    if not _webhook_url():
        print("DISCORD_WEBHOOK_URL or BOT_MANUAL_ALERT_WEBHOOK_URL is not set", file=sys.stderr)
        return 1
    sent = notify_manual_action_required(
        bot_id=str(args.bot_id),
        symbol=str(args.symbol).upper(),
        title="manual alert test",
        reason="operator requested Discord webhook test",
        action="No trading action required. This is a notification path test.",
        key=f"{args.bot_id}:{str(args.symbol).upper()}:manual-alert-test",
    )
    if not sent:
        print("manual alert test was not sent; check logs or rate limit", file=sys.stderr)
        return 1
    print("manual alert test sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
