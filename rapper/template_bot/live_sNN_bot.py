# -*- coding: utf-8 -*-
"""Template live/shadow runner for botNN.

Edit bot number, filenames, params, and `signal_adapters.build_signal` before
using this as a real bot. The default mode is shadow/no-order.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from live_data_fetcher import MT5DataManager
from live_executor import HEDGING_MARGIN_MODE, REQUIRED_SHARED_ACCOUNT_COMMANDS, MT5Executor, ORDER_TYPE_BUY, ORDER_TYPE_SELL
from live_safety import LiveSafetyOptions, clean_sync_block_if_flat, stale_signal_decision
from signal_adapters import build_signal
from timeframe_config import build_signal_bars, load_timeframe_profile


UTC = timezone.utc
BOT_SUFFIX = os.environ.get("BOT_SUFFIX", "sNN")
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
STATE_DIR = os.path.join(SCRIPT_DIR, "state")
LOG_FILE = os.path.join(LOG_DIR, f"{BOT_SUFFIX}_bot.log")
TRADE_LOG_FILE = os.path.join(LOG_DIR, f"{BOT_SUFFIX}_trades.csv")
STATE_FILE = os.path.join(STATE_DIR, f"{BOT_SUFFIX}_bot_state.json")
PARAMS_FILE = os.path.join(SCRIPT_DIR, "sNN_params.json")


TRADE_FIELDS = [
    "timestamp_utc",
    "event",
    "strategy_id",
    "spec_id",
    "symbol",
    "mt5_symbol",
    "ticket",
    "side",
    "lot",
    "price",
    "sl",
    "tp",
    "profit",
    "reason",
    "signal_bar_time",
    "live",
    "note",
]


def utc_now() -> datetime:
    return datetime.now(UTC)


def dt_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.tz_convert("UTC").to_pydatetime()
    except Exception:
        return None


def atomic_write_json(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def append_csv(path: str, row: dict[str, Any], fields: list[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path)
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in fields})


def symbol_key(spec: dict[str, Any]) -> str:
    return str(spec["symbol"])


def mt5_symbol(spec: dict[str, Any]) -> str:
    return str(spec.get("mt5_symbol") or spec["symbol"])


def comment_prefix(spec: dict[str, Any]) -> str:
    return str(spec.get("comment_prefix") or f"{BOT_SUFFIX}_{symbol_key(spec).lower()}")[:20]


def safety_options(params: dict[str, Any]) -> LiveSafetyOptions:
    cfg = dict(params.get("safety") or {})
    return LiveSafetyOptions(**{key: cfg.get(key, getattr(LiveSafetyOptions(), key)) for key in LiveSafetyOptions.__dataclass_fields__})


class TemplateRunner:
    def __init__(self, params: dict[str, Any]):
        self.params = params
        self.magic = int(params["magic"])
        self.live_enabled = bool(params.get("live_trading_enabled", False))
        self.shadow_enabled = bool(params.get("shadow_forward_enabled", True))
        self.safety = safety_options(params)
        self.timeframe = load_timeframe_profile(params)
        self.dm = MT5DataManager(self.safety)
        self.executor = MT5Executor()
        self.state = self._load_state()
        self._last_status_log = 0.0

    def _default_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "strategy_id": self.params["strategy_id"],
            "magic": self.magic,
            "shadow_ticket_seq": -int(self.magic) * 1000,
            "symbols": {
                symbol_key(spec): {
                    "active": None,
                    "last_signal_bar": None,
                    "sync_block_new_entries": False,
                    "sync_block_reason": None,
                    "sync_block_recoverable": False,
                    "sync_block_details": {},
                    "flat_clear_confirmation_count": 0,
                    "flat_clear_confirmation_reason": None,
                    "open_retry_after_utc": None,
                    "last_closed_side": None,
                    "last_closed_at_utc": None,
                    "last_closed_reason": None,
                    "last_closed_signal_bar": None,
                }
                for spec in self.params.get("symbols", [])
            },
            "updated_at": dt_text(utc_now()),
        }

    def _load_state(self) -> dict[str, Any]:
        base = self._default_state()
        if not os.path.exists(STATE_FILE):
            return base
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if loaded.get("strategy_id") not in {None, self.params["strategy_id"]}:
            raise RuntimeError("state strategy_id mismatch")
        if loaded.get("magic") is not None and int(loaded["magic"]) != self.magic:
            raise RuntimeError("state magic mismatch")
        base.update(loaded)
        base.setdefault("symbols", {})
        for spec in self.params.get("symbols", []):
            base["symbols"].setdefault(symbol_key(spec), self._default_state()["symbols"][symbol_key(spec)])
        return base

    def _save_state(self) -> None:
        self.state["updated_at"] = dt_text(utc_now())
        atomic_write_json(STATE_FILE, self.state)

    def _sym_state(self, spec: dict[str, Any]) -> dict[str, Any]:
        self.state.setdefault("symbols", {})
        self.state["symbols"].setdefault(symbol_key(spec), self._default_state()["symbols"][symbol_key(spec)])
        return self.state["symbols"][symbol_key(spec)]

    def _trade_row(self, event: str, spec: dict[str, Any], **kwargs: Any) -> None:
        row = {
            "timestamp_utc": dt_text(utc_now()),
            "event": event,
            "strategy_id": self.params["strategy_id"],
            "spec_id": spec.get("spec_id", ""),
            "symbol": symbol_key(spec),
            "mt5_symbol": mt5_symbol(spec),
            "live": self.live_enabled,
        }
        row.update(kwargs)
        append_csv(TRADE_LOG_FILE, row, TRADE_FIELDS)

    def _set_sync_block(self, spec: dict[str, Any], reason: str, *, recoverable: bool = False, details: dict[str, Any] | None = None) -> None:
        st = self._sym_state(spec)
        st["sync_block_new_entries"] = True
        st["sync_block_reason"] = reason
        st["sync_block_recoverable"] = bool(recoverable)
        st["sync_block_details"] = details or {}
        self._trade_row("ERROR", spec, reason=reason, note=json.dumps(details or {}, ensure_ascii=True))

    def _ownership_namespace_error(self) -> str | None:
        prefixes = [comment_prefix(spec) for spec in self.params.get("symbols", []) if bool(spec.get("enabled", True))]
        if self.magic <= 0:
            return f"invalid_magic={self.magic}"
        if not prefixes or any(not prefix for prefix in prefixes) or len(prefixes) != len(set(prefixes)):
            return f"invalid_or_duplicate_comment_prefixes={prefixes}"
        expected_magic = self.params.get("expected_magic")
        if expected_magic is not None and self.magic != int(expected_magic):
            return f"magic={self.magic} expected={int(expected_magic)}"
        return None

    def _owned_records(self, spec: dict[str, Any]) -> tuple[list[Any] | None, list[Any] | None]:
        positions = self.executor.get_positions(mt5_symbol(spec), self.magic)
        orders = self.executor.get_orders(mt5_symbol(spec), self.magic)
        prefix = comment_prefix(spec)
        for records, kind in ((positions, "position"), (orders, "order")):
            if records is None:
                continue
            unexpected = [
                rec for rec in records
                if int(getattr(rec, "magic", -1)) == self.magic and not str(getattr(rec, "comment", "") or "").startswith(prefix)
            ]
            if unexpected:
                self._set_sync_block(
                    spec,
                    f"same_magic_unexpected_{kind}",
                    details={"tickets": [int(rec.ticket) for rec in unexpected], "comments": [str(rec.comment or "") for rec in unexpected]},
                )
                return None, None
        return positions, orders

    def connect_and_preflight(self) -> bool:
        namespace_error = self._ownership_namespace_error()
        if namespace_error:
            logging.critical("ownership namespace invalid: %s", namespace_error)
            return False
        if not self.dm.connect():
            logging.critical("bridge connection failed")
            return False
        caps = self.executor.get_bridge_capabilities()
        if not caps:
            logging.critical("CAPS failed")
            return False
        expected_bridge = str(self.params.get("expected_bridge_name") or "")
        if expected_bridge and str(caps.get("name") or "") != expected_bridge:
            logging.critical("wrong bridge: got=%s expected=%s", caps.get("name"), expected_bridge)
            return False
        missing = REQUIRED_SHARED_ACCOUNT_COMMANDS - {str(command).upper() for command in caps.get("commands", set())}
        if missing:
            logging.critical("bridge commands missing: %s", sorted(missing))
            return False
        if self.live_enabled and bool(self.params.get("require_hedging_account", True)):
            account = self.executor.get_account_info()
            if account is None or int(account.get("margin_mode", -1)) != HEDGING_MARGIN_MODE:
                logging.critical("live shared-account runner requires hedging mode")
                return False
        for spec in self.params.get("symbols", []):
            if not bool(spec.get("enabled", True)):
                continue
            info = self.executor.get_symbol_info(mt5_symbol(spec))
            if info is None:
                logging.critical("INFO failed for %s", mt5_symbol(spec))
                return False
            positions, orders = self._owned_records(spec)
            if positions is None or orders is None:
                logging.critical("POSITIONS/ORDERS failed for %s", mt5_symbol(spec))
                return False
            clean_sync_block_if_flat(
                symbol_key=symbol_key(spec),
                state=self._sym_state(spec),
                positions=positions,
                orders=orders,
                save_state=self._save_state,
                audit=lambda _sym, reason, note: self._trade_row("ERROR", spec, reason=reason, note=note),
                options=self.safety,
                flat_auto_clear_reasons={"open_success_position_not_confirmed", "live_time_close_failed", "live_time_close_unconfirmed"},
                confirm_position_absent=self.executor.confirm_position_absent,
                required_flat_confirmations=2,
            )
        return True

    def _next_shadow_ticket(self) -> int:
        ticket = int(self.state.get("shadow_ticket_seq", -int(self.magic) * 1000))
        self.state["shadow_ticket_seq"] = ticket - 1
        return ticket

    def _expected_sl_tp(self, spec: dict[str, Any], side: str, entry: float) -> tuple[float, float]:
        pip = float(spec["pip_size"])
        digits = int(spec.get("price_digits", 5))
        sl_pips = float(spec.get("sl_pips", 30))
        tp_pips = float(spec.get("tp_pips", 60))
        if side == "long":
            return round(entry - sl_pips * pip, digits), round(entry + tp_pips * pip, digits)
        return round(entry + sl_pips * pip, digits), round(entry - tp_pips * pip, digits)

    def _open_position(self, spec: dict[str, Any], info: Any, signal: dict[str, Any]) -> None:
        st = self._sym_state(spec)
        side = signal["side"]
        entry = float(info.ask if side == "long" else info.bid)
        sl, tp = self._expected_sl_tp(spec, side, entry)
        lot = float(spec.get("lot", self.params.get("default_lot", 0.01)))
        if self.live_enabled:
            comment = f"{comment_prefix(spec)}_{str(spec.get('spec_id', 'x')).split('_')[0].lower()}"[:31]
            ticket = self.executor.open_position(
                mt5_symbol(spec),
                ORDER_TYPE_BUY if side == "long" else ORDER_TYPE_SELL,
                lot,
                sl,
                tp,
                deviation=int(self.params.get("max_deviation_points", 20)),
                magic=self.magic,
                comment=comment,
                digits=int(spec.get("price_digits", 5)),
            )
            if ticket is None:
                error = str(getattr(self.executor, "last_order_error", None) or "UNKNOWN_OPEN_FAILURE")
            else:
                error = ""
            positions = self.executor.get_positions(mt5_symbol(spec), self.magic)
            if positions is None:
                self._set_sync_block(spec, "positions_unavailable_after_open", recoverable=True, details={"ticket": int(ticket or 0), "error": error})
                self._save_state()
                return
            owned = [record for record in positions if int(record.magic) == self.magic and str(record.comment or "").startswith(comment_prefix(spec))]
            if ticket is not None:
                matches = [record for record in owned if int(record.ticket) == int(ticket) or int(getattr(record, "identifier", 0) or 0) == int(ticket)]
                if len(matches) != 1:
                    self._set_sync_block(spec, "open_success_position_not_confirmed", details={"ticket": int(ticket)})
                    self._save_state()
                    return
                confirmed = matches[0]
            elif len(owned) == 1 and not st.get("active"):
                confirmed = owned[0]
                ticket = int(confirmed.ticket)
            else:
                self._set_sync_block(spec, "ambiguous_open_result", details={"side": side, "error": error, "tickets": [int(record.ticket) for record in owned]})
                self._save_state()
                return
            entry = float(confirmed.open_price)
        else:
            ticket = self._next_shadow_ticket()
            confirmed = None
        st["active"] = {
            "ticket": ticket,
            "shadow": not self.live_enabled,
            "side": side,
            "lot": lot,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "signal_bar_time": signal["bar_time"],
            "opened_at": dt_text(utc_now()),
            "position_identifier": int(getattr(confirmed, "identifier", 0) or ticket) if confirmed is not None else int(ticket),
            "owner_symbol": mt5_symbol(spec),
            "owner_magic": self.magic,
            "owner_comment": str(getattr(confirmed, "comment", "") or comment_prefix(spec)) if confirmed is not None else comment_prefix(spec),
        }
        st["last_signal_bar"] = signal["bar_time"]
        st["sync_block_new_entries"] = False
        st["sync_block_reason"] = None
        self._trade_row("OPEN", spec, ticket=ticket, side=side, lot=lot, price=entry, sl=sl, tp=tp, reason="signal", signal_bar_time=signal["bar_time"])
        self._save_state()

    def _same_direction_reentry_blocked_after_close(self, spec: dict[str, Any], signal: dict[str, Any], side: str) -> bool:
        st = self._sym_state(spec)
        if str(st.get("last_closed_side") or "") != side:
            return False
        closed_at = parse_dt(st.get("last_closed_at_utc"))
        signal_time = parse_dt(str(signal.get("bar_time", "")))
        if closed_at is None or signal_time is None:
            return False
        if signal_time <= closed_at:
            st["last_signal_bar"] = signal["bar_time"]
            self._trade_row(
                "ERROR",
                spec,
                reason="same_direction_reentry_after_close_skip",
                note=f"side={side} signal_bar={signal['bar_time']} closed_at={dt_text(closed_at)}",
            )
            self._save_state()
            return True
        return False

    def run_symbol_once(self, spec: dict[str, Any]) -> None:
        if not bool(spec.get("enabled", True)):
            return
        st = self._sym_state(spec)
        info = self.executor.get_symbol_info(mt5_symbol(spec))
        if info is None:
            self._trade_row("ERROR", spec, reason="symbol_info_failed")
            return
        positions, orders = self._owned_records(spec)
        if positions is None:
            self._set_sync_block(spec, "positions_unavailable", recoverable=True)
            self._save_state()
            return
        if orders is None:
            self._set_sync_block(spec, "orders_unavailable", recoverable=True)
            self._save_state()
            return
        clean_sync_block_if_flat(
            symbol_key=symbol_key(spec),
            state=st,
            positions=positions,
            orders=orders,
            save_state=self._save_state,
            audit=lambda _sym, reason, note: self._trade_row("ERROR", spec, reason=reason, note=note),
            options=self.safety,
            flat_auto_clear_reasons={"open_success_position_not_confirmed", "live_time_close_failed", "live_time_close_unconfirmed"},
            confirm_position_absent=self.executor.confirm_position_absent,
            required_flat_confirmations=2,
        )
        if st.get("active") or st.get("sync_block_new_entries"):
            return
        raw = self.dm.get_historical_data(
            mt5_symbol(spec),
            self.timeframe.hist_timeframe,
            self.timeframe.hist_bars,
            self.params.get("broker_timezone", "UTC"),
            drop_latest=False,
        )
        if raw is None:
            self._trade_row("ERROR", spec, reason="hist_unavailable")
            return
        bars = build_signal_bars(raw, self.timeframe)
        if self.timeframe.drop_latest_signal_bar and len(bars) > 1:
            bars = bars.iloc[:-1]
        signal = build_signal(bars, spec)
        if signal is None or st.get("last_signal_bar") == signal["bar_time"]:
            return
        stale = stale_signal_decision(
            signal["bar_time"],
            timeframe_hours=self.timeframe.signal_minutes / 60.0,
            max_delay_minutes=self.timeframe.max_signal_delay_minutes,
            options=self.safety,
        )
        if stale.stale:
            st["last_signal_bar"] = signal["bar_time"]
            self._trade_row("ERROR", spec, reason="stale_signal_skip", note=f"entry_due_utc={stale.entry_due_utc} latest_allowed_utc={stale.latest_allowed_utc} now_utc={stale.now_utc}")
            self._save_state()
            return
        if self._same_direction_reentry_blocked_after_close(spec, signal, str(signal["side"])):
            return
        self._open_position(spec, info, signal)

    def run_once(self) -> None:
        for spec in self.params.get("symbols", []):
            try:
                self.run_symbol_once(spec)
            except Exception as exc:
                self._trade_row("ERROR", spec, reason="cycle_exception", note=str(exc))
        self._log_status()

    def _log_status(self) -> None:
        now = time.monotonic()
        if now - self._last_status_log < float(self.params.get("status_log_interval_seconds", 60)):
            return
        self._last_status_log = now
        compact = {
            key: {
                "active": bool(value.get("active")),
                "last_signal_bar": value.get("last_signal_bar"),
                "sync_block": value.get("sync_block_new_entries"),
                "reason": value.get("sync_block_reason"),
            }
            for key, value in self.state.get("symbols", {}).items()
        }
        logging.info("template status: live=%s shadow=%s symbols=%s", self.live_enabled, self.shadow_enabled, compact)


class FakeInfo:
    bid = 1.10000
    ask = 1.10002
    point = 0.00001


class FakeExecutor(MT5Executor):
    def get_bridge_capabilities(self) -> dict[str, Any]:
        return {"name": "BotBridge_sNN", "commands": set(REQUIRED_SHARED_ACCOUNT_COMMANDS)}

    def get_account_info(self) -> dict[str, Any]:
        return {"margin_mode": HEDGING_MARGIN_MODE, "margin_mode_name": "RETAIL_HEDGING"}

    def get_symbol_info(self, symbol: str) -> FakeInfo:
        return FakeInfo()

    def get_positions(self, symbol: str, magic: int) -> list[Any]:
        return []

    def get_orders(self, symbol: str, magic: int) -> list[Any]:
        return []

    def confirm_position_absent(self, ticket: int) -> bool:
        return True


class FakeDataManager(MT5DataManager):
    def connect(self) -> bool:
        return True

    def get_historical_data(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        idx = pd.date_range("2026-01-01", periods=80, freq="1h", tz="UTC")
        close = pd.Series([1.10 + i * 0.0001 for i in range(80)], index=idx)
        return pd.DataFrame({"Open": close, "High": close + 0.0002, "Low": close - 0.0002, "Close": close, "Volume": 100}, index=idx)


def load_params(path: str = PARAMS_FILE) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def self_test() -> None:
    params = load_params()
    params["shadow_forward_enabled"] = True
    params["live_trading_enabled"] = False
    params["symbols"][0]["signal_adapter"] = "EHLERS_CROSS"
    params["symbols"][0]["signal_params"] = {"period": 8, "cycle_atr": 0.01}
    runner = TemplateRunner(params)
    runner.dm = FakeDataManager(runner.safety)
    runner.executor = FakeExecutor()
    runner._save_state = lambda: None
    runner._trade_row = lambda *args, **kwargs: None
    st = runner._sym_state(params["symbols"][0])
    st["sync_block_new_entries"] = True
    st["sync_block_reason"] = "positions_unavailable"
    st["sync_block_recoverable"] = True
    runner.run_once()
    assert not st["sync_block_new_entries"], "recoverable clean sync should clear"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if args.self_test:
        self_test()
        print("template self-test ok")
        return 0
    params = load_params()
    runner = TemplateRunner(params)
    if not runner.connect_and_preflight():
        return 1
    runner.run_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
