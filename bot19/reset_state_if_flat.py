#!/usr/bin/env python3
"""Safely reset bot19 state only when MT5 has no S19 live exposure.

Run inside the exness-bot-19 container:

    python3 /app/bot19/reset_state_if_flat.py --apply

The script refuses to write state if:
- another live_s19_bot.py process is running;
- MT5 reports any live position for the bot19 symbol/magic;
- MT5 reports any pending order for the bot19 symbol/magic.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

JST = timezone(timedelta(hours=9), "JST")
SCRIPT_DIR = Path(__file__).resolve().parent
PARAMS_FILE = SCRIPT_DIR / "s19_params.json"
STATE_FILE = SCRIPT_DIR / "state" / "s19_gbpusd_bot_state.json"
RUNNER_NAME = "live_s19_bot.py"


def jst_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone(JST).isoformat()


def load_symbol_magic() -> tuple[str, int]:
    with PARAMS_FILE.open("r", encoding="utf-8") as handle:
        params = json.load(handle)
    profiles = params.get("profiles") or []
    if not profiles:
        raise RuntimeError("s19_params.json has no profiles")
    profile = profiles[0]
    return str(profile["symbol"]).upper(), int(profile["magic"])


def default_regime() -> dict[str, Any]:
    return {
        "entry_allowed": False,
        "signal_fresh": False,
        "trend_allowed_raw": False,
        "trend_direction": 0,
        "efficiency_ratio": 0.0,
        "adx": 0.0,
        "displacement_atr": 0.0,
        "signal_age_minutes": 999999.0,
        "reason": "not_loaded",
        "signal_time": None,
    }


def default_state(symbol: str, magic: int) -> dict[str, Any]:
    return {
        "version": 1,
        "strategy": "s19_snowball_cycle_start_event_filter",
        "symbol": symbol,
        "magic": magic,
        "cycle_id": 0,
        "cycle_realized_usd": 0.0,
        "grid_anchor": None,
        "positions": [],
        "virtual_orders": [],
        "next_order_id": 1,
        "auto_tp_price": None,
        "estimated_auto_tp_profit_usd": 0.0,
        "last_break_even_refresh_epoch": None,
        "restart_next_tick": False,
        "weekend_resume_reanchor_pending": False,
        "sync_block_new_entries": False,
        "sync_block_reason": None,
        "pending_open": None,
        "reconciliation_required": None,
        "pending_grid_repair_wait": None,
        "last_regime": default_regime(),
        "last_policy_decision": None,
        "updated_at_jst": jst_now_iso(),
    }


def load_current_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def running_live_s19_processes() -> list[str]:
    proc = Path("/proc")
    if not proc.exists():
        return []
    current_pid = os.getpid()
    found: list[str] = []
    for child in proc.iterdir():
        if not child.name.isdigit():
            continue
        pid = int(child.name)
        if pid == current_pid:
            continue
        cmdline_path = child / "cmdline"
        try:
            raw = cmdline_path.read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        text = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        if RUNNER_NAME in text:
            found.append(f"{pid}: {text}")
    return found


def bridge_send(command: str, timeout: float) -> str:
    from ea_bridge import ea_bridge

    return str(ea_bridge.send_command(command, timeout=timeout))


def require_empty_mt5(symbol: str, magic: int, current_state: dict[str, Any], timeout: float) -> None:
    checks = [
        (f"ORDERS|{symbol}|{magic}", "pending orders"),
        (f"POSITIONS|{symbol}|{magic}", "positions"),
    ]
    for command, label in checks:
        response = bridge_send(command, timeout)
        print(f"{command} => {response}")
        if response != "OK":
            raise RuntimeError(f"MT5 still has S19 {label}, or bridge check failed: {response}")

    tickets: set[int] = set()
    for order in current_state.get("virtual_orders") or []:
        pending_ticket = order.get("pending_ticket")
        if pending_ticket:
            tickets.add(int(pending_ticket))
    for position in current_state.get("positions") or []:
        ticket = position.get("ticket")
        if ticket:
            tickets.add(int(ticket))

    for ticket in sorted(tickets):
        command = f"POSITION|{ticket}"
        response = bridge_send(command, timeout)
        print(f"{command} => {response}")
        if response.startswith("OK|"):
            raise RuntimeError(f"State ticket {ticket} exists as a live MT5 position; refusing reset")


def write_state(path: Path, payload: dict[str, Any]) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if path.exists():
        stamp = datetime.now(timezone.utc).astimezone(JST).strftime("%Y%m%d_%H%M%S")
        backup_path = path.with_name(f"{path.name}.bak.{stamp}")
        shutil.copy2(path, backup_path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=True, sort_keys=True)
        handle.write("\n")
    os.replace(tmp_path, path)
    os.chmod(path, 0o644)
    return backup_path


def self_test() -> None:
    symbol, magic = "GBPUSD", 190019
    state = default_state(symbol, magic)
    assert state["symbol"] == symbol
    assert state["magic"] == magic
    assert state["positions"] == []
    assert state["virtual_orders"] == []
    assert state["sync_block_new_entries"] is False
    print("self-test passed")


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset bot19 state only if MT5 has no S19 exposure")
    parser.add_argument("--apply", action="store_true", help="write the reset state after safety checks")
    parser.add_argument("--timeout", type=float, default=5.0, help="EA bridge command timeout seconds")
    parser.add_argument("--self-test", action="store_true", help="run local checks without touching MT5/state")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    running = running_live_s19_processes()
    if running:
        print("Refusing reset because live_s19_bot.py is running:", file=sys.stderr)
        for item in running:
            print(f"  {item}", file=sys.stderr)
        return 2

    symbol, magic = load_symbol_magic()
    current_state = load_current_state(STATE_FILE)
    if current_state:
        if str(current_state.get("symbol", "")).upper() != symbol or int(current_state.get("magic", 0)) != magic:
            raise RuntimeError("State symbol/magic does not match s19_params.json")

    require_empty_mt5(symbol, magic, current_state, args.timeout)

    if not args.apply:
        print("Safety checks passed. Re-run with --apply to reset state.")
        return 0

    backup_path = write_state(STATE_FILE, default_state(symbol, magic))
    print(f"State reset complete: {STATE_FILE}")
    if backup_path is not None:
        print(f"Backup created: {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
