# -*- coding: utf-8 -*-
"""S21 multi-symbol Ehlers trendline live/shadow runner.

Source candidates:
- US500_137_1h
- AUDUSD_021_1h
- USDJPY_035_1h
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import os
import shutil
import sys
import time
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from live_data_fetcher import MT5DataManager
from live_executor import (
    HEDGING_MARGIN_MODE,
    MT5Executor,
    ORDER_TYPE_BUY,
    ORDER_TYPE_SELL,
    REQUIRED_S21_COMMANDS,
    S21_BRIDGE_NAME,
    TRADE_PERMISSION_RETCODES,
)
from live_manual_alerts import notify_manual_action_required

UTC = timezone.utc

LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
STATE_DIR = os.path.join(SCRIPT_DIR, "state")
LOG_FILE = os.path.join(LOG_DIR, "s21_bot.log")
TRADE_LOG_FILE = os.path.join(LOG_DIR, "s21_trades.csv")
TRADE_ERROR_FILE = os.path.join(LOG_DIR, "s21_trade_errors.csv")
STATE_FILE = os.path.join(STATE_DIR, "s21_bot_state.json")
PARAMS_FILE = os.path.join(SCRIPT_DIR, "s21_params.json")


DEFAULT_PARAMS: dict[str, Any] = {
    "enabled": True,
    "live_trading_enabled": False,
    "shadow_forward_enabled": True,
    "strategy_id": "bot21_ehlers_top3_us500_audusd_usdjpy",
    "magic": 200021,
    "default_lot": 0.01,
    "poll_interval_seconds": 5,
    "status_log_interval_seconds": 60,
    "max_signal_delay_minutes": 10,
    "broker_timezone": "Europe/Athens",
    "max_deviation_points": 20,
    "open_retry_seconds": 60,
    "autotrading_reject_notify_after_count": 3,
    "require_hedging_account": True,
    "h1_timeframe": 16385,
    "h1_bars": 240,
    "drop_latest_h1_bar": True,
    "max_active_positions": 3,
    "symbols": [],
}


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
    "position_role",
    "reason",
    "signal_bar_time",
    "live",
    "note",
]


def utc_now() -> datetime:
    return datetime.now(UTC)


def dt_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            return ts.to_pydatetime().replace(tzinfo=UTC)
        return ts.tz_convert(UTC).to_pydatetime()
    except Exception:
        return None


def setup_logging(*, file_logging: bool = True) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if file_logging:
        os.makedirs(LOG_DIR, exist_ok=True)
        handlers.insert(0, logging.FileHandler(LOG_FILE, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
        force=True,
    )


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_params() -> dict[str, Any]:
    params = dict(DEFAULT_PARAMS)
    if os.path.exists(PARAMS_FILE):
        with open(PARAMS_FILE, "r", encoding="utf-8") as f:
            params = deep_merge(params, json.load(f))
    return params


def atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def append_csv(path: str, row: dict[str, Any], fieldnames: list[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow({name: row.get(name, "") for name in fieldnames})


def true_range_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(int(period), min_periods=int(period)).mean()


def normalize_bars(raw: pd.DataFrame | None, drop_latest: bool, broker_timezone: str = "UTC") -> pd.DataFrame | None:
    if raw is None or raw.empty:
        return None
    bars = raw.copy().sort_index()
    idx = pd.DatetimeIndex(bars.index)
    try:
        if idx.tz is None:
            # BotBridge_s21 HIST timestamps were verified on CentOS as UTC.
            idx = idx.tz_localize("UTC", ambiguous="infer", nonexistent="shift_forward")
        idx = idx.tz_convert("UTC")
    except Exception as exc:
        logging.error(
            "Could not normalize H1 HIST timestamps as UTC "
            "(configured broker_timezone=%s): %s",
            broker_timezone,
            exc,
        )
        return None
    bars.index = idx
    if drop_latest and len(bars) > 1:
        bars = bars.iloc[:-1]
    return bars if not bars.empty else None


def latest_ehlers_signal(bars: pd.DataFrame, spec: dict[str, Any]) -> dict[str, Any] | None:
    min_len = max(int(spec["period"]) + 5, 25)
    if len(bars) < min_len:
        return None
    close = bars["Close"].astype(float)
    atr = true_range_atr(bars, 14)
    trendline = close.ewm(span=int(spec["period"]), adjust=False).mean()
    cycle = (close - trendline).abs()
    i = len(bars) - 1
    if pd.isna(atr.iloc[i]) or pd.isna(trendline.iloc[i]) or pd.isna(trendline.iloc[i - 1]):
        return None
    long_sig = close.iloc[i - 1] <= trendline.iloc[i - 1] and close.iloc[i] > trendline.iloc[i] and cycle.iloc[i] > atr.iloc[i] * float(spec["cycle_atr"])
    short_sig = close.iloc[i - 1] >= trendline.iloc[i - 1] and close.iloc[i] < trendline.iloc[i] and cycle.iloc[i] > atr.iloc[i] * float(spec["cycle_atr"])
    if not long_sig and not short_sig:
        return None
    return {
        "side": "long" if long_sig else "short",
        "bar_time": str(bars.index[i]),
        "close": float(close.iloc[i]),
        "trendline": float(trendline.iloc[i]),
        "atr14": float(atr.iloc[i]),
        "cycle": float(cycle.iloc[i]),
    }


def normalize_price(price: float, digits: int) -> float:
    return round(float(price), int(digits))


def bar_timestamp(value: Any) -> pd.Timestamp | None:
    try:
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.tz_convert("UTC")
    except Exception:
        return None


def symbol_key(spec: dict[str, Any]) -> str:
    return str(spec["symbol"])


def mt5_symbol(spec: dict[str, Any]) -> str:
    return str(spec.get("mt5_symbol") or spec["symbol"])


def comment_prefix(spec: dict[str, Any]) -> str:
    return str(spec.get("comment_prefix") or f"s21_{symbol_key(spec).lower()}")[:20]


class S21EhlersRunner:
    def __init__(self, params: dict[str, Any]):
        self.params = params
        self.magic = int(params["magic"])
        self.live_enabled = bool(params.get("live_trading_enabled", False))
        self.shadow_enabled = bool(params.get("shadow_forward_enabled", True))
        self.dm = MT5DataManager()
        self.executor = MT5Executor(self.dm)
        self._suppress_manual_alerts = False
        self.state = self._load_state()
        self._last_status_log = 0.0

    def _default_state(self) -> dict[str, Any]:
        return {
            "version": 4,
            "strategy_id": self.params["strategy_id"],
            "magic": self.magic,
            "shadow_ticket_seq": -200021000,
            "symbols": {
                symbol_key(spec): {
                    "active_positions": [],
                    "active": None,
                    "last_signal_bar": None,
                    "sync_block_new_entries": False,
                    "sync_block_reason": None,
                    "sync_block_recoverable": False,
                    "sync_block_details": {},
                    "open_retry_after_utc": None,
                    "autotrading_reject_streak": 0,
                    "autotrading_reject_notified": False,
                    "manual_alert_last_signature": None,
                    "manual_alert_last_reason": None,
                    "manual_alert_last_at_utc": None,
                    "last_closed_side": None,
                    "last_closed_at_utc": None,
                    "last_closed_reason": None,
                    "last_closed_signal_bar": None,
                    "pending_reversals": [],
                    "pending_reversal": None,
                }
                for spec in self.params.get("symbols", [])
            },
            "updated_at": dt_text(utc_now()),
        }

    def _load_state(self) -> dict[str, Any]:
        base = self._default_state()
        if not os.path.exists(STATE_FILE):
            return base
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if loaded.get("strategy_id") and loaded.get("strategy_id") != self.params["strategy_id"]:
                raise RuntimeError(
                    f"State strategy_id mismatch: expected={self.params['strategy_id']} got={loaded.get('strategy_id')}"
                )
            if loaded.get("magic") is not None and int(loaded.get("magic")) != self.magic:
                raise RuntimeError(f"State magic mismatch: expected={self.magic} got={loaded.get('magic')}")
            base = deep_merge(base, loaded)
            base.setdefault("symbols", {})
            default_symbols = self._default_state()["symbols"]
            for spec in self.params.get("symbols", []):
                key = symbol_key(spec)
                base["symbols"][key] = deep_merge(default_symbols[key], base["symbols"].get(key, {}))
            # Normalize legacy single-active and single-pending state into the v4 list schema.
            base["version"] = 4
            return base
        except Exception as exc:
            raise RuntimeError(f"Could not load state file {STATE_FILE}: {exc}") from exc

    def _save_state(self) -> None:
        self.state["updated_at"] = dt_text(utc_now())
        atomic_write_json(STATE_FILE, self.state)

    def _sym_state(self, spec: dict[str, Any]) -> dict[str, Any]:
        self.state.setdefault("symbols", {})
        key = symbol_key(spec)
        default = self._default_state()["symbols"][key]
        current = self.state["symbols"].get(key)
        if current is None:
            self.state["symbols"][key] = default
            return self.state["symbols"][key]
        merged = deep_merge(default, current)
        current.clear()
        current.update(merged)
        active_positions = current.get("active_positions")
        if active_positions is None:
            active_positions = current.get("active_positions") if current.get("active_positions") is not None else None
        if not isinstance(active_positions, list):
            active_positions = []
        if not active_positions:
            legacy_active = current.get("active")
            if isinstance(legacy_active, dict):
                active_positions = [dict(legacy_active)]
            else:
                active_positions = []
        current["active_positions"] = active_positions
        current["active"] = active_positions[0] if active_positions else None
        pending_reversals = current.get("pending_reversals")
        if not isinstance(pending_reversals, list):
            pending_reversals = []
        legacy_pending = current.get("pending_reversal")
        if not pending_reversals and isinstance(legacy_pending, dict):
            pending_reversals = [dict(legacy_pending)]
        current["pending_reversals"] = pending_reversals
        current["pending_reversal"] = pending_reversals[0] if pending_reversals else None
        return current

    def _normalize_side(self, side: str) -> str:
        return str(side).strip().lower()

    def _normalize_position_role(self, role: Any) -> str:
        return "reversal" if self._normalize_side(str(role)) == "reversal" else "normal"

    def _position_role_from_comment(self, comment: str | None) -> str:
        text = str(comment or "")
        if not text:
            return "normal"
        suffix = text.rsplit("_", 1)[-1].lower()
        if suffix.startswith("r") and suffix[1:].isdigit():
            return "reversal"
        if suffix in {"r", "reversal"}:
            return "reversal"
        if suffix in {"n", "normal"}:
            return "normal"
        return "normal"

    def _reversal_origin_from_comment(self, comment: str | None) -> int | None:
        suffix = str(comment or "").rsplit("_", 1)[-1].lower()
        if suffix.startswith("r") and suffix[1:].isdigit():
            return int(suffix[1:])
        return None

    def _comment_candidates_for_position(self, spec: dict[str, Any], role: str | None = None) -> list[str]:
        role_normalized = self._normalize_position_role(role) if role is not None else None
        candidates = [self._legacy_comment_for_position(spec)]
        if role_normalized in {"normal", "reversal"}:
            role_comment = self._comment_for_position(spec, role_normalized)
            if role_comment not in candidates:
                candidates.append(role_comment)
        else:
            normal_comment = self._comment_for_position(spec, "normal")
            reversal_comment = self._comment_for_position(spec, "reversal")
            for comment in (normal_comment, reversal_comment):
                if comment not in candidates:
                    candidates.append(comment)
        return candidates

    def _max_active_positions(self, spec: dict[str, Any]) -> int:
        try:
            value = int(self.params.get("max_active_positions", 1))
        except Exception:
            value = 1
        if value <= 0:
            value = 1
        return value

    def _opposite_side(self, side: str) -> str:
        normalized = self._normalize_side(side)
        return "short" if normalized == "long" else "long"

    def _signal_entry_side(self, signal_side: str) -> str:
        return self._opposite_side(self._normalize_side(signal_side))

    def _comment_prefix_for_spec(self, spec: dict[str, Any]) -> str:
        base = comment_prefix(spec)
        spec_token = str(spec.get("spec_id", "spec")).split("_")[0].lower()
        return base

    def _comment_for_position(self, spec: dict[str, Any], role: str, reversal_of_ticket: int | None = None) -> str:
        base = self._comment_prefix_for_spec(spec)
        role_token = "n" if str(role).lower() == "normal" else "r"
        if role_token == "r" and reversal_of_ticket is not None:
            return f"{base}_r{int(reversal_of_ticket)}"[:31]
        spec_token = str(spec.get("spec_id", "spec")).split("_")[0].lower()
        return f"{base}_{spec_token}_{role_token}"[:31]

    def _legacy_comment_for_position(self, spec: dict[str, Any]) -> str:
        base = self._comment_prefix_for_spec(spec)
        spec_token = str(spec.get("spec_id", "spec")).split("_")[0].lower()
        return f"{base}_{spec_token}"[:31]

    def _active_positions(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        st = self._sym_state(spec)
        positions = st.get("active_positions")
        if not isinstance(positions, list):
            positions = []
            st["active_positions"] = positions
        return positions

    def _set_active_positions(self, spec: dict[str, Any], active_positions: list[dict[str, Any]]) -> None:
        st = self._sym_state(spec)
        filtered = [dict(pos) for pos in active_positions]
        st["active_positions"] = filtered
        st["active"] = filtered[0] if filtered else None

    def _pending_reversals(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        st = self._sym_state(spec)
        pending = st.get("pending_reversals")
        return pending if isinstance(pending, list) else []

    def _set_pending_reversals(self, spec: dict[str, Any], pending: list[dict[str, Any]]) -> None:
        st = self._sym_state(spec)
        st["pending_reversals"] = [dict(item) for item in pending]
        st["pending_reversal"] = st["pending_reversals"][0] if st["pending_reversals"] else None

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

    def _error_row(self, spec: dict[str, Any], reason: str, note: str = "") -> None:
        self._trade_row("ERROR", spec, reason=reason, note=note)
        append_csv(
            TRADE_ERROR_FILE,
            {
                "timestamp_utc": dt_text(utc_now()),
                "strategy_id": self.params["strategy_id"],
                "spec_id": spec.get("spec_id", ""),
                "symbol": symbol_key(spec),
                "mt5_symbol": mt5_symbol(spec),
                "reason": reason,
                "note": note,
            },
            ["timestamp_utc", "strategy_id", "spec_id", "symbol", "mt5_symbol", "reason", "note"],
        )

    def _alert_signature(self, reason: str, details: dict[str, Any]) -> str:
        payload = {"reason": reason, "details": details}
        text = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _notify_manual_action(self, spec: dict[str, Any], *, title: str, reason: str, action: str, key: str) -> None:
        if self._suppress_manual_alerts:
            return
        notify_manual_action_required(
            bot_id="bot21",
            symbol=mt5_symbol(spec),
            title=title,
            reason=reason,
            action=action,
            key=key,
        )

    def _notify_reconciliation_required(self, spec: dict[str, Any], reason: str, details: dict[str, Any]) -> None:
        st = self._sym_state(spec)
        signature = self._alert_signature(reason, details)
        if st.get("manual_alert_last_signature") == signature:
            return
        st["manual_alert_last_signature"] = signature
        st["manual_alert_last_reason"] = reason
        st["manual_alert_last_at_utc"] = dt_text(utc_now())
        self._notify_manual_action(
            spec,
            title="reconciliation_required",
            reason=f"{reason}; details={json.dumps(details, ensure_ascii=True, sort_keys=True, default=str)}",
            action="Inspect MT5 positions/orders and bot21 state/logs before clearing this block or enabling entries.",
            key=f"bot21:reconciliation:{symbol_key(spec)}:{reason}",
        )

    def _set_sync_block(
        self,
        spec: dict[str, Any],
        reason: str | None,
        details: dict[str, Any] | None = None,
        *,
        recoverable: bool = False,
    ) -> None:
        st = self._sym_state(spec)
        previous = st.get("sync_block_reason")
        if reason:
            clean_details = details or {}
            st["sync_block_new_entries"] = True
            st["sync_block_reason"] = reason
            st["sync_block_recoverable"] = bool(recoverable)
            st["sync_block_details"] = clean_details
            if previous != reason:
                logging.error("S21 new entries blocked for %s: %s", symbol_key(spec), reason)
            if not recoverable:
                self._notify_reconciliation_required(spec, reason, clean_details)
            return
        if st.get("sync_block_new_entries"):
            logging.warning("S21 new-entry block cleared for %s after clean sync: %s", symbol_key(spec), previous)
        st["sync_block_new_entries"] = False
        st["sync_block_reason"] = None
        st["sync_block_recoverable"] = False
        st["sync_block_details"] = {}

    def _is_broker_sl_residual_block(self, st: dict[str, Any]) -> bool:
        if st.get("sync_block_reason") != "same_magic_unexpected_order":
            return False
        details = st.get("sync_block_details") or {}
        comments = details.get("comments") or []
        if not comments:
            return False
        return all(re.fullmatch(r"\[sl [0-9]+(?:\.[0-9]+)?\]", str(comment or "")) for comment in comments)

    def _clear_stale_sync_block_if_flat(
        self,
        spec: dict[str, Any],
        positions: list[Any],
        orders: list[Any],
    ) -> bool:
        st = self._sym_state(spec)
        if not st.get("sync_block_new_entries"):
            return False
        if positions or orders:
            return False
        if bool(st.get("sync_block_recoverable")):
            reason = str(st.get("sync_block_reason") or "")
        elif self._is_broker_sl_residual_block(st):
            reason = "broker_sl_residual_flat"
        else:
            return False
        self._set_sync_block(spec, None)
        self._error_row(spec, "sync_block_cleared_flat", reason)
        self._save_state()
        return True

    def _trade_permission_error(self, error: str | None) -> bool:
        text = str(error or "")
        return any(f"ERR|{code}" in text for code in TRADE_PERMISSION_RETCODES)

    def _record_trade_permission_reject(self, spec: dict[str, Any], error: str | None) -> None:
        st = self._sym_state(spec)
        streak = int(st.get("autotrading_reject_streak") or 0) + 1
        st["autotrading_reject_streak"] = streak
        notify_after = int(self.params.get("autotrading_reject_notify_after_count", 3))
        if streak >= notify_after and not bool(st.get("autotrading_reject_notified")):
            self._notify_manual_action(
                spec,
                title="MT5 Algo Trading disabled or trading permission rejected",
                reason=f"{error}; consecutive={streak}; threshold={notify_after}",
                action="Enable MT5 Algo Trading / EA trading permission for BotBridge_s21, then inspect state and live exposure before clearing any block.",
                key=f"bot21:autotrading-disabled:{symbol_key(spec)}",
            )
            st["autotrading_reject_notified"] = True

    def _reset_trade_permission_rejects(self, spec: dict[str, Any]) -> None:
        st = self._sym_state(spec)
        st["autotrading_reject_streak"] = 0
        st["autotrading_reject_notified"] = False

    def _validate_lot(self, spec: dict[str, Any], info: Any) -> bool:
        lot = float(spec.get("lot", self.params.get("default_lot", 0.01)))
        volume_min = float(getattr(info, "volume_min", 0.0) or 0.0)
        volume_max = float(getattr(info, "volume_max", 0.0) or 0.0)
        volume_step = float(getattr(info, "volume_step", 0.0) or 0.0)
        if lot <= 0 or volume_min <= 0 or volume_max < volume_min or volume_step <= 0:
            logging.critical("S21 invalid volume metadata for %s: lot=%s min=%s max=%s step=%s", mt5_symbol(spec), lot, volume_min, volume_max, volume_step)
            return False
        if lot + 1e-12 < volume_min or lot - 1e-12 > volume_max:
            logging.critical("S21 lot outside broker limits for %s: lot=%s min=%s max=%s", mt5_symbol(spec), lot, volume_min, volume_max)
            return False
        nearest = round(lot / volume_step) * volume_step
        if abs(nearest - lot) > 1e-9:
            logging.critical("S21 lot does not align with broker step for %s: lot=%s step=%s", mt5_symbol(spec), lot, volume_step)
            return False
        return True

    def connect_and_preflight(self) -> bool:
        if not self.dm.connect():
            logging.critical("S21 bridge connection failed.")
            return False
        caps = self.executor.get_bridge_capabilities()
        if not caps or caps["name"] != S21_BRIDGE_NAME:
            logging.critical("Bridge name mismatch: expected=%s got=%s", S21_BRIDGE_NAME, caps["name"] if caps else None)
            return False
        missing = REQUIRED_S21_COMMANDS - caps["commands"]
        if missing:
            logging.critical("S21 bridge missing commands: %s", sorted(missing))
            return False
        account = self.executor.get_account_info()
        if account is None:
            return False
        if self.live_enabled:
            if bool(self.params.get("require_hedging_account", True)) and int(account["margin_mode"]) != HEDGING_MARGIN_MODE:
                logging.critical(
                    "S21 live trading requires a hedging account for shared-account ownership safety: mode=%s",
                    account.get("margin_mode_name"),
                )
                return False
            if not all(
                bool(account.get(name))
                for name in ("account_trade_allowed", "account_trade_expert", "terminal_trade_allowed", "mql_trade_allowed")
            ):
                logging.critical("S21 live trading permissions are not fully enabled in MT5/account metadata.")
                return False
        for spec in self.params.get("symbols", []):
            if not bool(spec.get("enabled", True)):
                continue
            sym = mt5_symbol(spec)
            info = self.executor.get_symbol_info(sym)
            if info is None:
                logging.critical("S21 symbol INFO preflight failed for %s", sym)
                return False
            if not self._validate_lot(spec, info):
                return False
            positions = self._owned_positions(spec)
            if positions is None:
                logging.critical("S21 POSITIONS preflight failed for %s", sym)
                return False
            orders = self._owned_orders(spec)
            if orders is None:
                logging.critical("S21 ORDERS preflight failed for %s", sym)
                return False
            self._clear_stale_sync_block_if_flat(spec, positions, orders)
            if orders:
                self._set_sync_block(spec, "owned_pending_orders_unsupported", {"tickets": [int(order.ticket) for order in orders]}, recoverable=False)
                self._save_state()
                return False
            raw = self.dm.get_historical_data(sym, int(self.params["h1_timeframe"]), 30, self.params.get("broker_timezone", "UTC"))
            bars = normalize_bars(raw, False, self.params.get("broker_timezone", "UTC"))
            if bars is None or bars.empty:
                logging.critical("S21 H1 HIST preflight failed for %s", sym)
                return False
        logging.info("S21 preflight ok.")
        return True

    def _next_shadow_ticket(self) -> int:
        ticket = int(self.state.get("shadow_ticket_seq", -200021000))
        self.state["shadow_ticket_seq"] = ticket - 1
        return ticket

    def _record_side(self, record: Any) -> str:
        direction = str(getattr(record, "direction", "")).upper()
        if direction == "LONG":
            return "long"
        if direction == "SHORT":
            return "short"
        return "long" if int(getattr(record, "type", ORDER_TYPE_BUY)) == ORDER_TYPE_BUY else "short"

    def _filter_owned_records(self, spec: dict[str, Any], records: list[Any], kind: str) -> list[Any] | None:
        prefix = comment_prefix(spec)
        owned = []
        unexpected = []
        for record in records:
            if str(getattr(record, "symbol", "")) != mt5_symbol(spec):
                continue
            if int(getattr(record, "magic", -1)) != self.magic:
                continue
            if str(getattr(record, "comment", "") or "").startswith(prefix):
                owned.append(record)
            else:
                unexpected.append(record)
        if unexpected:
            tickets = [int(getattr(record, "ticket", 0)) for record in unexpected]
            comments = [str(getattr(record, "comment", "") or "") for record in unexpected]
            self._set_sync_block(
                spec,
                f"same_magic_unexpected_{kind}",
                {"tickets": tickets, "comments": comments},
                recoverable=False,
            )
            self._error_row(spec, f"same_magic_unexpected_{kind}", f"tickets={tickets} comments={comments}")
            return None
        return owned

    def _owned_positions(self, spec: dict[str, Any]) -> list[Any] | None:
        positions = self.executor.get_positions(mt5_symbol(spec), self.magic)
        if positions is None:
            return None
        return self._filter_owned_records(spec, positions, "position")

    def _owned_orders(self, spec: dict[str, Any]) -> list[Any] | None:
        get_orders = getattr(self.executor, "get_orders", None)
        if get_orders is None:
            self._set_sync_block(spec, "orders_query_unavailable", {}, recoverable=False)
            self._error_row(spec, "orders_query_unavailable")
            return None
        orders = get_orders(mt5_symbol(spec), self.magic)
        if orders is None:
            return None
        return self._filter_owned_records(spec, orders, "order")

    def _is_owned_position(self, spec: dict[str, Any], pos: Any) -> bool:
        return (
            str(getattr(pos, "symbol", "")) == mt5_symbol(spec)
            and int(getattr(pos, "magic", -1)) == self.magic
            and str(getattr(pos, "comment", "") or "").startswith(comment_prefix(spec))
        )

    def _position_opened_at(self, pos: Any) -> str:
        try:
            return dt_text(datetime.fromtimestamp(int(getattr(pos, "open_time")), tz=UTC))
        except Exception:
            return dt_text(utc_now())

    def _expected_sl_tp(self, spec: dict[str, Any], side: str, entry: float, digits: int) -> tuple[float, float]:
        pip = float(spec["pip_size"])
        sl_dist = float(spec["sl_pips"]) * pip
        tp_dist = float(spec["tp_pips"]) * pip
        sl = normalize_price(entry - sl_dist if side == "long" else entry + sl_dist, digits)
        tp = normalize_price(entry + tp_dist if side == "long" else entry - tp_dist, digits)
        return sl, tp

    def _position_matches_record(self, spec: dict[str, Any], pos: Any, active: dict[str, Any]) -> bool:
        if not self._is_owned_position(spec, pos):
            return False
        active_comment = str(active.get("comment", "") or "")
        if active_comment and str(getattr(pos, "comment", "") or "") != active_comment:
            return False
        if str(active.get("side", "")) != self._record_side(pos):
            return False
        active_lot = float(active.get("lot", 0.0) or 0.0)
        return active_lot <= 0 or abs(float(getattr(pos, "volume", 0.0)) - active_lot) <= 1e-9

    def _positions_for_comment_side(
        self,
        spec: dict[str, Any],
        positions: list[Any],
        comment: str | list[str],
        side: str,
    ) -> list[Any]:
        wanted = [comment] if isinstance(comment, str) else list(comment)
        return [
            pos
            for pos in positions
            if self._is_owned_position(spec, pos)
            and str(getattr(pos, "comment", "") or "") in wanted
            and self._record_side(pos) == side
        ]

    def _set_active_from_position(
        self,
        spec: dict[str, Any],
        pos: Any,
        *,
        signal_bar_time: str | None,
        reason: str,
        role: str = "normal",
        reversal_of_ticket: int | None = None,
    ) -> None:
        st = self._sym_state(spec)
        role_normalized = self._normalize_position_role(role)
        built = {
            "ticket": int(pos.ticket),
            "position_identifier": int(getattr(pos, "identifier", pos.ticket)),
            "shadow": False,
            "side": self._record_side(pos),
            "position_role": role_normalized,
            "lot": float(pos.volume),
            "entry": float(pos.open_price),
            "last_price": float(pos.open_price),
            "sl": float(pos.sl),
            "tp": float(pos.tp),
            "opened_at": self._position_opened_at(pos),
            "signal_bar_time": signal_bar_time or "",
            "comment": str(pos.comment or ""),
            "recovered_reason": reason,
            "reversal_of_ticket": int(reversal_of_ticket) if reversal_of_ticket is not None else self._reversal_origin_from_comment(str(pos.comment or "")),
        }
        active_positions = self._active_positions(spec)
        replaced = False
        ticket = int(built["ticket"])
        for idx, record in enumerate(active_positions):
            if int(record.get("ticket", 0)) == ticket:
                active_positions[idx] = built
                replaced = True
                break
        if not replaced:
            active_positions.append(built)
        self._set_active_positions(spec, active_positions)

    def _repair_live_sl_tp(self, spec: dict[str, Any], pos: Any, digits: int) -> bool:
        side = self._record_side(pos)
        desired_sl, desired_tp = self._expected_sl_tp(spec, side, float(pos.open_price), digits)
        point = float(spec.get("point_size", 0.0) or 0.0)
        tolerance = max(point * 0.5, 1e-10)
        if abs(float(pos.sl) - desired_sl) <= tolerance and abs(float(pos.tp) - desired_tp) <= tolerance:
            return True
        if not self._is_owned_position(spec, pos):
            self._set_sync_block(spec, "sl_tp_repair_ownership_failed", {"ticket": int(pos.ticket)}, recoverable=False)
            self._error_row(spec, "sl_tp_repair_ownership_failed", f"ticket={pos.ticket}")
            return False
        ok = self.executor.modify_position_sl_tp(int(pos.ticket), sl=desired_sl, tp=desired_tp, digits=digits)
        if not ok:
            self._set_sync_block(
                spec,
                "sl_tp_repair_failed",
                {"ticket": int(pos.ticket), "desired_sl": desired_sl, "desired_tp": desired_tp},
                recoverable=False,
            )
            self._error_row(spec, "sl_tp_repair_failed", f"ticket={pos.ticket} sl={desired_sl} tp={desired_tp}")
            return False
        pos.sl = desired_sl
        pos.tp = desired_tp
        active_positions = self._active_positions(spec)
        for active in active_positions:
            if int(active.get("ticket", 0)) == int(pos.ticket):
                active["sl"] = desired_sl
                active["tp"] = desired_tp
                break
        self._set_active_positions(spec, active_positions)
        return True

    def _clear_position(
        self,
        spec: dict[str, Any],
        active: dict[str, Any],
        reason: str,
        profit: float = 0.0,
    ) -> None:
        st = self._sym_state(spec)
        active_positions = self._active_positions(spec)
        closed_ticket = int(active.get("ticket", 0))
        closed_side = str(active.get("side", "") or "")
        closed_at = utc_now()
        self._trade_row(
            "CLOSE",
            spec,
            ticket=active.get("ticket", ""),
            side=active.get("side", ""),
            lot=active.get("lot", ""),
            price=active.get("last_price", ""),
            sl=active.get("sl", ""),
            tp=active.get("tp", ""),
            position_role=active.get("position_role", ""),
            profit=profit,
            reason=reason,
            signal_bar_time=active.get("signal_bar_time", ""),
            note=f"position_identifier={active.get('position_identifier', '')} reversal_of_ticket={active.get('reversal_of_ticket', '')}",
        )
        st["active_positions"] = [record for record in active_positions if int(record.get("ticket", 0)) != closed_ticket]
        self._set_active_positions(spec, st["active_positions"])
        st["open_retry_after_utc"] = None
        if str(active.get("position_role", "normal")) == "normal":
            st["last_closed_side"] = closed_side
            st["last_closed_at_utc"] = dt_text(closed_at)
            st["last_closed_reason"] = reason
            st["last_closed_signal_bar"] = active.get("signal_bar_time", "")
            if reason in {"shadow_tp", "live_tp", "normal_tp"}:
                pending_item = {
                    "origin_ticket": closed_ticket,
                    "origin_side": closed_side,
                    "origin_reason": reason,
                    "origin_role": "normal",
                    "reversal_side": self._opposite_side(closed_side),
                    "created_at_utc": dt_text(closed_at),
                    "created_signal_bar": str(active.get("signal_bar_time", "") or ""),
                }
                pending = self._pending_reversals(spec)
                if not any(int(item.get("origin_ticket", 0)) == closed_ticket for item in pending):
                    pending.append(pending_item)
                self._set_pending_reversals(spec, pending)
        self._reset_trade_permission_rejects(spec)
        self._save_state()

    def _confirmed_live_close(self, spec: dict[str, Any], active: dict[str, Any]) -> tuple[str, float, float] | None:
        lookup = getattr(self.executor, "get_position_close_deal", None)
        if lookup is None:
            self._set_sync_block(spec, "close_deal_query_unavailable", {}, recoverable=False)
            return None
        position_id = int(active.get("position_identifier") or active.get("ticket") or 0)
        opened_at = parse_dt(active.get("opened_at"))
        from_epoch = int(opened_at.timestamp()) - 60 if opened_at is not None else 0
        deal = lookup(position_id, from_epoch)
        if not deal:
            self._set_sync_block(
                spec,
                "close_deal_not_confirmed",
                {"ticket": int(active.get("ticket", 0)), "position_identifier": position_id},
                recoverable=True,
            )
            return None
        if (
            int(getattr(deal, "position_id", 0)) != position_id
            or str(getattr(deal, "symbol", "")) != mt5_symbol(spec)
            or int(getattr(deal, "magic", -1)) != self.magic
        ):
            self._set_sync_block(
                spec,
                "close_deal_ownership_mismatch",
                {
                    "ticket": int(active.get("ticket", 0)),
                    "position_identifier": position_id,
                    "deal_position_id": int(getattr(deal, "position_id", 0)),
                    "deal_symbol": str(getattr(deal, "symbol", "")),
                    "deal_magic": int(getattr(deal, "magic", -1)),
                },
                recoverable=False,
            )
            return None
        reason_name = str(getattr(deal, "reason", "")).upper()
        reason = "live_tp" if reason_name == "DEAL_REASON_TP" else "live_sl" if reason_name == "DEAL_REASON_SL" else f"live_{reason_name.lower()}"
        return reason, float(getattr(deal, "net_profit", 0.0)), float(getattr(deal, "price", 0.0))

    def _same_direction_reentry_blocked_after_close(
        self,
        spec: dict[str, Any],
        signal: dict[str, Any],
        side: str,
    ) -> bool:
        st = self._sym_state(spec)
        closed_side = str(st.get("last_closed_side") or "")
        if not closed_side or closed_side != side:
            return False
        closed_at = parse_dt(st.get("last_closed_at_utc"))
        signal_time = bar_timestamp(str(signal.get("bar_time", "")))
        if closed_at is None or signal_time is None:
            return False
        if signal_time <= closed_at:
            st["last_signal_bar"] = signal["bar_time"]
            st["open_retry_after_utc"] = None
            self._error_row(
                spec,
                "same_direction_reentry_after_close_skip",
                f"side={side} signal_bar={signal['bar_time']} closed_at={dt_text(closed_at)} reason={st.get('last_closed_reason')}",
            )
            self._save_state()
            return True
        return False

    def _pending_reversal_for_origin(self, spec: dict[str, Any], origin_ticket: int | None) -> dict[str, Any] | None:
        active_positions = self._active_positions(spec)
        if origin_ticket is None:
            return None
        for active in active_positions:
            if self._normalize_position_role(active.get("position_role")) != "reversal":
                continue
            if int(active.get("reversal_of_ticket") or 0) == int(origin_ticket):
                return active
        return None

    def _process_pending_reversal(self, spec: dict[str, Any], info: Any) -> None:
        st = self._sym_state(spec)
        pending_queue = self._pending_reversals(spec)
        pending = pending_queue[0] if pending_queue else None
        if not isinstance(pending, dict):
            return
        if st.get("sync_block_new_entries"):
            return
        try:
            origin_ticket = pending.get("origin_ticket")
            origin_ticket_int = int(origin_ticket) if origin_ticket is not None else None
        except Exception:
            self._set_pending_reversals(spec, pending_queue[1:])
            self._save_state()
            return
        if self._pending_reversal_for_origin(spec, origin_ticket_int):
            self._set_pending_reversals(spec, pending_queue[1:])
            self._save_state()
            return

        reversal_side = self._normalize_side(pending.get("reversal_side", ""))
        if reversal_side not in {"long", "short"}:
            self._set_pending_reversals(spec, pending_queue[1:])
            self._save_state()
            return

        signal_bar_time = str(pending.get("created_signal_bar", "") or st.get("last_signal_bar", "") or "")
        opened = self._open_position(
            spec,
            info,
            side=reversal_side,
            signal_bar_time=signal_bar_time,
            reason="normal_tp_reversal",
            position_role="reversal",
            reversal_of_ticket=origin_ticket_int,
        )
        if opened:
            self._set_pending_reversals(spec, pending_queue[1:])
            self._save_state()

    def _manage_active(self, spec: dict[str, Any], info: Any, positions: list[Any]) -> None:
        st = self._sym_state(spec)
        active_positions = self._active_positions(spec)
        if not active_positions:
            return
        state_saved = False
        for active in list(active_positions):
            side = self._normalize_side(active.get("side", ""))
            if side not in {"long", "short"}:
                self._set_sync_block(
                    spec,
                    "state_corrupted_position_side",
                    {"ticket": int(active.get("ticket", 0)), "side": active.get("side")},
                    recoverable=False,
                )
                self._error_row(spec, "state_corrupted_position_side", f"ticket={active.get('ticket', '')}")
                self._save_state()
                return
            opened_at = parse_dt(active.get("opened_at"))
            hold_hours = float(spec["max_hold_bars"])
            due = opened_at + timedelta(hours=hold_hours) if opened_at else None
            ticket = int(active.get("ticket", 0))

            if bool(active.get("shadow", False)):
                price = float(info.bid if side == "long" else info.ask)
                active["last_price"] = price
                entry = float(active.get("entry", 0.0))
                sl = float(active.get("sl", 0.0))
                tp = float(active.get("tp", 0.0))
                close_reason = None
                if side == "long" and sl > 0 and price <= sl:
                    close_reason = "shadow_sl"
                elif side == "long" and tp > 0 and price >= tp:
                    close_reason = "shadow_tp"
                elif side == "short" and sl > 0 and price >= sl:
                    close_reason = "shadow_sl"
                elif side == "short" and tp > 0 and price <= tp:
                    close_reason = "shadow_tp"
                elif due and utc_now() >= due:
                    close_reason = "shadow_time"
                if close_reason:
                    pnl = (price - entry) / float(spec["pip_size"]) if side == "long" else (entry - price) / float(spec["pip_size"])
                    self._clear_position(spec, active, close_reason, pnl)
                else:
                    state_saved = True
                continue

            pos = None
            matching = [pos for pos in positions if int(pos.ticket) == ticket and self._position_matches_record(spec, pos, active)]
            if matching:
                pos = matching[0]
            else:
                direct = self.executor.get_position(ticket)
                if direct is not None:
                    if not self._position_matches_record(spec, direct, active):
                        self._set_sync_block(
                            spec,
                            "state_ticket_unowned_or_foreign",
                            {
                                "ticket": ticket,
                                "live_symbol": str(getattr(direct, "symbol", "")),
                                "live_magic": int(getattr(direct, "magic", -1)),
                                "live_comment": str(getattr(direct, "comment", "") or ""),
                            },
                            recoverable=False,
                        )
                        self._error_row(spec, "state_ticket_unowned_or_foreign", f"ticket={ticket}")
                        self._save_state()
                        return
                    pos = direct
                else:
                    comments = self._comment_candidates_for_position(spec, role=active.get("position_role"))
                    same_identity = self._positions_for_comment_side(spec, positions, comments, side) if comments else []
                    if len(same_identity) == 1:
                        pos = same_identity[0]
                        old_ticket = ticket
                        active_positions = [record for record in self._active_positions(spec) if int(record.get("ticket", 0)) != int(old_ticket)]
                        self._set_active_positions(spec, active_positions)
                        self._set_active_from_position(
                            spec,
                            pos,
                            signal_bar_time=str(active.get("signal_bar_time", "") or ""),
                            reason="ticket_drift_recovered",
                            role=self._position_role_from_comment(active.get("comment")),
                            reversal_of_ticket=int(active.get("reversal_of_ticket")) if active.get("reversal_of_ticket") is not None else None,
                        )
                        self._error_row(spec, "ticket_drift_recovered", f"old_ticket={old_ticket} new_ticket={pos.ticket}")
                        self._save_state()
                        state_saved = False
                        active_positions = self._active_positions(spec)
                        updated = [
                            a
                            for a in active_positions
                            if int(a.get("ticket", 0)) == int(pos.ticket)
                        ]
                        if not updated:
                            return
                        active = updated[0]
                        ticket = int(active.get("ticket", 0))
                    elif len(same_identity) > 1:
                        self._set_sync_block(
                            spec,
                            "ambiguous_ticket_drift",
                            {"old_ticket": ticket, "candidates": [int(pos.ticket) for pos in same_identity]},
                            recoverable=False,
                        )
                        self._error_row(spec, "ambiguous_ticket_drift", f"old_ticket={ticket} candidates={[pos.ticket for pos in same_identity]}")
                        self._save_state()
                        return
                    else:
                        absent = self.executor.confirm_position_absent(ticket)
                        if absent is True:
                            confirmed = self._confirmed_live_close(spec, active)
                            if confirmed is None:
                                self._save_state()
                                return
                            close_reason, pnl, close_price = confirmed
                            active["last_price"] = close_price
                            self._clear_position(spec, active, close_reason, pnl)
                        else:
                            self._set_sync_block(
                                spec,
                                "active_position_missing_unconfirmed",
                                {"ticket": ticket, "owned_tickets": [int(pos.ticket) for pos in positions], "absent_confirmed": absent},
                                recoverable=False,
                            )
                            self._error_row(spec, "active_position_missing_unconfirmed", f"ticket={ticket} absent_confirmed={absent}")
                            self._save_state()
                            return
                        continue

            if pos is None:
                continue
            if not self._position_matches_record(spec, pos, active):
                self._set_sync_block(
                    spec,
                    "active_position_missing_unconfirmed",
                    {"ticket": ticket, "owned_tickets": [int(pos.ticket) for pos in positions]},
                    recoverable=False,
                )
                self._error_row(spec, "active_position_missing_unconfirmed", f"ticket={ticket}")
                self._save_state()
                return

            if due and utc_now() >= due:
                if not self._is_owned_position(spec, pos):
                    self._error_row(spec, "time_close_ownership_failed", f"ticket={ticket}")
                    return
                result = self.executor.close_position(ticket, deviation=int(self.params.get("max_deviation_points", 20)))
                if result:
                    absent = self.executor.confirm_position_absent(ticket)
                    if absent is True:
                        close_price = float(getattr(result, "close_price", 0.0) or 0.0)
                        if close_price > 0:
                            entry = float(active.get("entry", getattr(result, "open_price", 0.0)) or 0.0)
                            pnl = (close_price - entry) / float(spec["pip_size"]) if side == "long" else (entry - close_price) / float(spec["pip_size"])
                        else:
                            pnl = float(getattr(result, "profit", 0.0))
                        self._clear_position(spec, active, "live_time_close", pnl)
                    else:
                        self._set_sync_block(
                            spec,
                            "live_time_close_unconfirmed",
                            {"ticket": ticket, "absent_confirmed": absent},
                            recoverable=False,
                        )
                        self._error_row(spec, "live_time_close_unconfirmed", f"ticket={ticket} absent_confirmed={absent}")
                        self._save_state()
                        return
                else:
                    status = str(getattr(result, "status", ""))
                    self._set_sync_block(spec, "live_time_close_failed", {"ticket": ticket, "status": status}, recoverable=False)
                    self._error_row(spec, "live_time_close_failed", status)
                    self._save_state()
                    return
                continue

            digits = int(spec.get("price_digits", getattr(info, "digits", 5)))
            active["position_identifier"] = int(getattr(pos, "identifier", pos.ticket))
            if not self._repair_live_sl_tp(spec, pos, digits):
                self._save_state()
                return
            active["last_price"] = float(info.bid if side == "long" else info.ask)
            active["sl"] = float(getattr(pos, "sl", 0.0))
            active["tp"] = float(getattr(pos, "tp", 0.0))
            state_saved = True
        if state_saved:
            self._save_state()
    def _spread_points(self, info: Any, spec: dict[str, Any]) -> float:
        point = float(getattr(info, "point", 0.0) or spec.get("point_size", 0.0))
        if point <= 0:
            return math.inf
        return float(info.ask - info.bid) / point

    def _open_retry_blocked(self, spec: dict[str, Any]) -> bool:
        retry_after = parse_dt(self._sym_state(spec).get("open_retry_after_utc"))
        return bool(retry_after and utc_now() < retry_after)

    def _set_open_retry(self, spec: dict[str, Any]) -> None:
        retry_seconds = max(1.0, float(self.params.get("open_retry_seconds", 60)))
        self._sym_state(spec)["open_retry_after_utc"] = dt_text(utc_now() + timedelta(seconds=retry_seconds))

    def _confirm_live_open_from_positions(
        self,
        spec: dict[str, Any],
        *,
        comment: str,
        side: str,
        signal_bar_time: str,
        reason: str,
        role: str = "normal",
        reversal_of_ticket: int | None = None,
    ) -> Any | None:
        positions = self._owned_positions(spec)
        if positions is None:
            self._set_sync_block(spec, "open_result_positions_unavailable", {"comment": comment, "side": side}, recoverable=False)
            self._error_row(spec, "open_result_positions_unavailable", f"comment={comment} side={side}")
            self._save_state()
            return None
        candidates = self._comment_candidates_for_position(spec, role=role)
        if comment not in candidates:
            candidates.append(comment)
        matches = self._positions_for_comment_side(spec, positions, candidates, side)
        if len(matches) == 1:
            pos = matches[0]
            self._set_active_from_position(
                spec,
                pos,
                signal_bar_time=signal_bar_time,
                reason=reason,
                role=role,
                reversal_of_ticket=reversal_of_ticket,
            )
            return pos
        if len(matches) > 1:
            self._set_sync_block(
                spec,
                "ambiguous_open_result_positions",
                {"comment": comment, "side": side, "tickets": [int(pos.ticket) for pos in matches]},
                recoverable=False,
            )
            self._error_row(spec, "ambiguous_open_result_positions", f"tickets={[pos.ticket for pos in matches]}")
            self._save_state()
            return None
        return None

    def _handle_live_open_failure(
        self,
        spec: dict[str, Any],
        signal: dict[str, Any] | None,
        comment: str,
        side: str,
        digits: int,
        *,
        signal_bar_time: str | None = None,
        position_role: str = "normal",
        reversal_of_ticket: int | None = None,
    ) -> None:
        st = self._sym_state(spec)
        error = str(getattr(self.executor, "last_order_error", "") or "UNKNOWN_OPEN_FAILURE")
        bar_time = str(signal_bar_time or (signal["bar_time"] if isinstance(signal, dict) else ""))
        recovered = self._confirm_live_open_from_positions(
            spec,
            comment=comment,
            side=side,
            signal_bar_time=bar_time,
            reason="open_failure_recovered_from_positions",
            role=position_role,
            reversal_of_ticket=reversal_of_ticket,
        )
        if recovered is not None:
            if not self._repair_live_sl_tp(spec, recovered, digits):
                self._save_state()
                return
            st = self._sym_state(spec)
            if position_role == "normal":
                st["last_signal_bar"] = bar_time
            st["open_retry_after_utc"] = None
            self._set_sync_block(spec, None)
            self._reset_trade_permission_rejects(spec)
            self._trade_row(
                "OPEN",
                spec,
                ticket=int(recovered.ticket),
                side=side,
                lot=float(recovered.volume),
                price=float(recovered.open_price),
                sl=float(recovered.sl),
                tp=float(recovered.tp),
                position_role=position_role,
                reason="open_failure_recovered_from_positions",
                signal_bar_time=bar_time,
                note=error,
            )
            self._save_state()
            return
        if self._sym_state(spec).get("sync_block_new_entries") and not bool(self._sym_state(spec).get("sync_block_recoverable")):
            self._save_state()
            return
        if error in {"NO_RESPONSE", "UNKNOWN_OPEN_FAILURE"} or error.startswith("MALFORMED_OK"):
            self._set_sync_block(spec, "ambiguous_open_result", {"error": error, "comment": comment, "side": side}, recoverable=False)
            self._error_row(spec, "ambiguous_open_result", error)
            self._save_state()
            return
        if self._trade_permission_error(error):
            self._record_trade_permission_reject(spec, error)
        else:
            self._reset_trade_permission_rejects(spec)
        self._set_open_retry(spec)
        self._error_row(spec, "live_open_failed_retry", error)
        self._save_state()

    def _open_position(
        self,
        spec: dict[str, Any],
        info: Any,
        *,
        side: str,
        signal_bar_time: str,
        position_role: str = "normal",
        reversal_of_ticket: int | None = None,
        reason: str = "ehlers_cross",
        signal: dict[str, Any] | None = None,
    ) -> bool:
        st = self._sym_state(spec)
        role_normalized = self._normalize_position_role(position_role)
        active_positions = self._active_positions(spec)
        if len(active_positions) >= self._max_active_positions(spec):
            self._error_row(
                spec,
                "max_active_positions_reached",
                f"max={self._max_active_positions(spec)} active={len(active_positions)}",
            )
            return False
        if self._open_retry_blocked(spec):
            return False
        spread_points = self._spread_points(info, spec)
        if spread_points > float(spec["max_entry_spread_points"]):
            if signal_bar_time and role_normalized == "normal":
                st["last_signal_bar"] = signal_bar_time
            self._error_row(spec, "spread_guard", f"spread_points={spread_points:.2f}")
            self._save_state()
            return False

        side = self._normalize_side(side)
        if side not in {"long", "short"}:
            self._error_row(spec, "invalid_open_side", f"side={side}")
            self._save_state()
            return False
        digits = int(spec.get("price_digits", getattr(info, "digits", 5)))
        lot = float(spec.get("lot", self.params.get("default_lot", 0.01)))
        entry = float(info.ask if side == "long" else info.bid)
        sl, tp = self._expected_sl_tp(spec, side, entry, digits)
        min_stop_distance = float(getattr(info, "stops_level", 0) or 0) * float(getattr(info, "point", spec["point_size"]))
        if min_stop_distance > 0 and (abs(entry - sl) < min_stop_distance or abs(entry - tp) < min_stop_distance):
            if signal_bar_time and role_normalized == "normal":
                st["last_signal_bar"] = signal_bar_time
            self._error_row(spec, "stop_level_guard", f"entry={entry} sl={sl} tp={tp} min_stop_distance={min_stop_distance}")
            self._save_state()
            return False

        order_type = ORDER_TYPE_BUY if side == "long" else ORDER_TYPE_SELL
        comment = self._comment_for_position(spec, role_normalized, reversal_of_ticket)
        ticket: int | None
        executed_price = entry
        opened_at = dt_text(utc_now())
        shadow = not self.live_enabled
        if self.live_enabled:
            ticket_obj = self.executor.open_position(
                mt5_symbol(spec),
                order_type,
                lot,
                sl,
                tp,
                deviation=int(self.params.get("max_deviation_points", 20)),
                magic=self.magic,
                comment=comment,
                digits=digits,
            )
            if ticket_obj is None:
                self._handle_live_open_failure(
                    spec,
                    signal,
                    comment,
                    side,
                    digits,
                    signal_bar_time=signal_bar_time,
                    position_role=role_normalized,
                    reversal_of_ticket=reversal_of_ticket,
                )
                return False
            recovered = self._confirm_live_open_from_positions(
                spec,
                comment=comment,
                side=side,
                signal_bar_time=signal_bar_time,
                reason="live_open_confirmed",
                role=role_normalized,
                reversal_of_ticket=reversal_of_ticket,
            )
            if recovered is None:
                self._set_sync_block(
                    spec,
                    "open_success_position_not_confirmed",
                    {"order_ticket": int(ticket_obj), "comment": comment, "side": side},
                    recoverable=False,
                )
                self._error_row(spec, "open_success_position_not_confirmed", f"order_ticket={int(ticket_obj)}")
                self._save_state()
                return False
            ticket = int(recovered.ticket)
            executed_price = float(recovered.open_price)
            if not self._repair_live_sl_tp(spec, recovered, digits):
                self._save_state()
                return False
            sl = float(recovered.sl)
            tp = float(recovered.tp)
            opened_at = self._position_opened_at(recovered)
        elif self.shadow_enabled:
            ticket = self._next_shadow_ticket()
            if st.get("sync_block_new_entries"):
                return False
        else:
            if signal_bar_time and role_normalized == "normal":
                st["last_signal_bar"] = signal_bar_time
            self._save_state()
            return False

        position_identifier = ticket
        if shadow:
            active = {
                "ticket": ticket,
                "position_identifier": ticket,
                "shadow": True,
                "side": side,
                "position_role": role_normalized,
                "reversal_of_ticket": int(reversal_of_ticket) if reversal_of_ticket is not None else None,
                "lot": lot,
                "entry": executed_price,
                "last_price": executed_price,
                "sl": sl,
                "tp": tp,
                "opened_at": opened_at,
                "signal_bar_time": signal_bar_time,
                "comment": comment,
            }
            active_positions.append(active)
            self._set_active_positions(spec, active_positions)
            active_positions = self._active_positions(spec)
        else:
            pos = self._owned_positions(spec)
            if pos is not None:
                matched = [p for p in pos if int(p.ticket) == int(ticket)]
                if matched:
                    position_identifier = int(getattr(matched[0], "identifier", matched[0].ticket))
                    self._set_active_from_position(
                        spec,
                        matched[0],
                        signal_bar_time=signal_bar_time,
                        reason=reason,
                        role=role_normalized,
                        reversal_of_ticket=reversal_of_ticket,
                    )
        st["open_retry_after_utc"] = None
        self._set_sync_block(spec, None)
        self._reset_trade_permission_rejects(spec)
        if signal_bar_time and role_normalized == "normal":
            st["last_signal_bar"] = signal_bar_time
        self._trade_row(
            "OPEN",
            spec,
            ticket=ticket,
            side=side,
            lot=lot,
            price=executed_price,
            sl=sl,
            tp=tp,
            position_role=role_normalized,
            reason=reason,
            signal_bar_time=signal_bar_time,
            note=f"spread_points={spread_points:.2f} position_identifier={position_identifier} reversal_of_ticket={reversal_of_ticket if reversal_of_ticket is not None else ''}",
        )
        self._save_state()
        return True

    def run_symbol_once(self, spec: dict[str, Any]) -> None:
        if not bool(spec.get("enabled", True)):
            return
        st = self._sym_state(spec)
        info = self.executor.get_symbol_info(mt5_symbol(spec))
        if info is None:
            self._error_row(spec, "symbol_info_failed")
            return
        positions = self._owned_positions(spec)
        if positions is None:
            if not st.get("sync_block_new_entries"):
                self._set_sync_block(spec, "positions_unavailable", recoverable=True)
            self._save_state()
            return
        orders = self._owned_orders(spec)
        if orders is None:
            if not st.get("sync_block_new_entries"):
                self._set_sync_block(spec, "orders_unavailable", recoverable=True)
            self._save_state()
            return
        if orders:
            self._set_sync_block(
                spec,
                "owned_pending_orders_unsupported",
                {"tickets": [int(order.ticket) for order in orders]},
                recoverable=False,
            )
            self._error_row(spec, "owned_pending_orders_unsupported", f"tickets={[order.ticket for order in orders]}")
            self._save_state()
            return
        if st.get("sync_block_new_entries") and bool(st.get("sync_block_recoverable")):
            self._set_sync_block(spec, None)
            self._save_state()
        self._clear_stale_sync_block_if_flat(spec, positions, orders)

        active_positions = self._active_positions(spec)
        if not active_positions and positions:
            if len(positions) > self._max_active_positions(spec):
                self._set_sync_block(
                    spec,
                    "owned_live_positions_without_state_ambiguous",
                    {"tickets": [int(pos.ticket) for pos in positions]},
                    recoverable=False,
                )
                self._error_row(spec, "owned_live_positions_without_state_ambiguous", f"tickets={[pos.ticket for pos in positions]}")
                self._save_state()
                return
            for pos in positions:
                self._set_active_from_position(
                    spec,
                    pos,
                    signal_bar_time=str(st.get("last_signal_bar", "") or ""),
                    reason="startup_or_state_recovery",
                    role=self._position_role_from_comment(str(getattr(pos, "comment", "") or "")),
                    reversal_of_ticket=self._reversal_origin_from_comment(str(getattr(pos, "comment", "") or "")),
                )
            self._error_row(spec, "owned_live_positions_adopted", f"tickets={[pos.ticket for pos in positions]}")
            active_positions = self._active_positions(spec)

        self._manage_active(spec, info, positions)
        self._process_pending_reversal(spec, info)
        if self._sym_state(spec).get("sync_block_new_entries"):
            return
        active_positions = self._active_positions(spec)
        if len(active_positions) >= self._max_active_positions(spec):
            self._error_row(spec, "max_active_positions_reached_pre_signal", f"max={self._max_active_positions(spec)} active={len(active_positions)}")
            self._save_state()
            return

        broker_tz = str(self.params.get("broker_timezone", "UTC"))
        raw = self.dm.get_historical_data(mt5_symbol(spec), int(self.params["h1_timeframe"]), int(self.params["h1_bars"]), broker_tz)
        bars = normalize_bars(raw, bool(self.params.get("drop_latest_h1_bar", True)), broker_tz)
        if bars is None:
            self._error_row(spec, "h1_bars_unavailable")
            return
        signal = latest_ehlers_signal(bars, spec)
        if signal is None:
            return
        if st.get("last_signal_bar") == signal["bar_time"]:
            return
        signal_time = bar_timestamp(signal["bar_time"])
        if signal_time is not None:
            entry_due = signal_time + pd.Timedelta(hours=1)
            max_delay = float(self.params.get("max_signal_delay_minutes", 0.0))
            if max_delay > 0:
                latest_allowed = entry_due + pd.Timedelta(minutes=max_delay)
                now_utc = pd.Timestamp(utc_now())
                if now_utc > latest_allowed:
                    st["last_signal_bar"] = signal["bar_time"]
                    st["open_retry_after_utc"] = None
                    self._error_row(spec, "stale_signal_skip", f"entry_due_utc={entry_due} latest_allowed_utc={latest_allowed} now_utc={now_utc}")
                    self._save_state()
                    return
        signal_side = self._normalize_side(signal.get("side", ""))
        side = self._signal_entry_side(signal_side)
        if self._same_direction_reentry_blocked_after_close(spec, signal, side):
            return
        self._open_position(
            spec,
            info,
            side=side,
            signal_bar_time=signal["bar_time"],
            reason="ehlers_cross",
            signal=signal,
            position_role="normal",
        )

    def run_once(self) -> None:
        if not bool(self.params.get("enabled", True)):
            logging.info("S21 disabled by params.")
            return
        for spec in self.params.get("symbols", []):
            try:
                self.run_symbol_once(spec)
            except Exception as exc:
                logging.exception("S21 symbol cycle failed for %s: %s", symbol_key(spec), exc)
                self._error_row(spec, "cycle_exception", str(exc))
        self._log_status()

    def _log_status(self) -> None:
        now = time.monotonic()
        if now - self._last_status_log < float(self.params["status_log_interval_seconds"]):
            return
        self._last_status_log = now
        compact = {
            key: {
                "active": bool(value.get("active")),
                "active_count": len(value.get("active_positions") or []),
                "pending_reversal_count": len(value.get("pending_reversals") or []),
                "last_signal_bar": value.get("last_signal_bar"),
                "sync_block": value.get("sync_block_new_entries"),
                "reason": value.get("sync_block_reason"),
            }
            for key, value in self.state.get("symbols", {}).items()
        }
        logging.info("S21 status: live=%s shadow=%s symbols=%s", self.live_enabled, self.shadow_enabled, compact)

    def run_forever(self) -> None:
        while True:
            self.run_once()
            time.sleep(float(self.params["poll_interval_seconds"]))


def self_test_params() -> dict[str, Any]:
    params = deep_merge(DEFAULT_PARAMS, load_params())
    params["max_signal_delay_minutes"] = 0
    params["live_trading_enabled"] = False
    params["shadow_forward_enabled"] = True
    params["symbols"] = [
        {
            "enabled": True,
            "symbol": "AUDUSD",
            "mt5_symbol": "AUDUSD",
            "spec_id": "AUDUSD_021_1h",
            "comment_prefix": "s21_audusd",
            "lot": 0.01,
            "period": 24,
            "cycle_atr": 0.05,
            "pip_size": 0.0001,
            "point_size": 0.00001,
            "price_digits": 5,
            "sl_pips": 32,
            "tp_pips": 32,
            "max_hold_bars": 12,
            "max_entry_spread_points": 40.0,
        }
    ]
    return params


class FakeInfo:
    symbol = "AUDUSD"
    ask = 1.10505
    bid = 1.10500
    point = 0.00001
    digits = 5
    stops_level = 0
    volume_min = 0.01
    volume_max = 100.0
    volume_step = 0.01


class FakeTicket(int):
    def __new__(cls, ticket_id: int, price: float = 0.0):
        obj = super().__new__(cls, ticket_id)
        obj.price = price
        obj.deal = ticket_id + 1000000
        obj.retcode = "10009"
        return obj


class FakePosition:
    def __init__(
        self,
        ticket: int,
        *,
        symbol: str = "AUDUSD",
        side: str = "long",
        volume: float = 0.01,
        open_price: float = 1.10505,
        sl: float = 1.10185,
        tp: float = 1.10825,
        magic: int = 200021,
        comment: str = "s21_audusd_audusd",
        open_time: int = 1767225600,
        identifier: int | None = None,
    ):
        self.ticket = int(ticket)
        self.symbol = symbol
        self.type = ORDER_TYPE_BUY if side == "long" else ORDER_TYPE_SELL
        self.direction = "LONG" if side == "long" else "SHORT"
        self.volume = float(volume)
        self.open_price = float(open_price)
        self.sl = float(sl)
        self.tp = float(tp)
        self.profit = 0.0
        self.magic = int(magic)
        self.open_time = int(open_time)
        self.identifier = int(identifier if identifier is not None else ticket)
        self.comment = comment


class FakeCloseDeal:
    def __init__(self, position_id: int, reason: str, *, symbol: str = "AUDUSD", magic: int = 200021, price: float = 1.10825, net_profit: float = 3.2):
        self.position_id = int(position_id)
        self.reason = reason
        self.symbol = symbol
        self.magic = int(magic)
        self.price = float(price)
        self.net_profit = float(net_profit)


class FakeOrder:
    def __init__(self, ticket: int, *, symbol: str = "AUDUSD", magic: int = 200021, comment: str = "s21_audusd_pending"):
        self.ticket = int(ticket)
        self.symbol = symbol
        self.type = 4
        self.direction = "LONG"
        self.volume = 0.01
        self.price_open = 1.10600
        self.sl = 1.10280
        self.tp = 1.10920
        self.magic = int(magic)
        self.comment = comment


class FakeDataManager:
    def __init__(self, bars: pd.DataFrame):
        self.bars = bars

    def connect(self) -> bool:
        return True

    def disconnect(self) -> None:
        return None

    def get_historical_data(self, mt5_symbol: str, timeframe: int, num_bars: int, broker_timezone: str = "UTC") -> pd.DataFrame:
        return self.bars.tail(num_bars)


class FakeExecutor:
    def __init__(
        self,
        *,
        positions: list[Any] | None = None,
        orders: list[Any] | None = None,
        open_error: str | None = None,
        open_adds_position: bool = False,
        account_mode: int = HEDGING_MARGIN_MODE,
        close_deal: Any = False,
    ) -> None:
        self.positions = positions if positions is not None else []
        self.orders = orders if orders is not None else []
        self.opened: list[dict[str, Any]] = []
        self.modified: list[dict[str, Any]] = []
        self.last_order_error = open_error
        self.open_error = open_error
        self.open_adds_position = open_adds_position
        self.account_mode = int(account_mode)
        self.close_deal = close_deal

    def get_bridge_capabilities(self) -> dict[str, Any]:
        return {"name": S21_BRIDGE_NAME, "version": "selftest", "commands": REQUIRED_S21_COMMANDS}

    def get_account_info(self) -> dict[str, Any]:
        return {
            "margin_mode": self.account_mode,
            "margin_mode_name": "RETAIL_HEDGING" if self.account_mode == HEDGING_MARGIN_MODE else "RETAIL_NETTING",
            "account_trade_allowed": True,
            "account_trade_expert": True,
            "terminal_trade_allowed": True,
            "mql_trade_allowed": True,
        }

    def get_symbol_info(self, symbol: str) -> FakeInfo:
        return FakeInfo()

    def get_positions(self, symbol: str, magic: int | None = None) -> list[Any]:
        return list(self.positions)

    def get_orders(self, symbol: str, magic: int | None = None) -> list[Any]:
        return list(self.orders)

    def get_position(self, ticket: int) -> Any | None:
        for pos in self.positions:
            if int(pos.ticket) == int(ticket):
                return pos
        return None

    def confirm_position_absent(self, ticket: int) -> bool:
        return self.get_position(ticket) is None

    def get_position_close_deal(self, position_id: int, opened_at_epoch: int) -> Any:
        return self.close_deal

    def open_position(self, *args: Any, **kwargs: Any) -> Any | None:
        self.opened.append({"args": args, "kwargs": kwargs})
        if self.open_error:
            self.last_order_error = self.open_error
            return None
        ticket = 3001
        order_type = int(args[1]) if len(args) > 1 else ORDER_TYPE_BUY
        fake_side = "long" if order_type == ORDER_TYPE_BUY else "short"
        if self.open_adds_position:
            self.positions.append(
                FakePosition(
                    ticket,
                    side=fake_side,
                    comment=kwargs.get("comment", "s21_audusd_audusd"),
                    sl=0.0,
                    tp=0.0,
                )
            )
        return FakeTicket(ticket, 1.10505)

    def modify_position_sl_tp(self, ticket: int, sl: float = 0.0, tp: float = 0.0, digits: int = 5) -> bool:
        self.modified.append({"ticket": int(ticket), "sl": float(sl), "tp": float(tp), "digits": int(digits)})
        for pos in self.positions:
            if int(pos.ticket) == int(ticket):
                pos.sl = float(sl)
                pos.tp = float(tp)
        return True


def make_self_test_bars() -> pd.DataFrame:
    idx = pd.date_range("2026-01-01 00:00:00", periods=81, freq="1h")
    close = [1.1000 + i * 0.00001 for i in range(81)]
    for i in range(55, 79):
        close[i] = 1.1000 - (79 - i) * 0.00008
    close[78] = 1.0990
    close[79] = 1.1060
    close[80] = 1.1061
    rows = []
    for c in close:
        rows.append({"Open": c - 0.0002, "High": c + 0.0010, "Low": c - 0.0010, "Close": c, "Volume": 100})
    return pd.DataFrame(rows, index=idx)


def make_no_signal_bars() -> pd.DataFrame:
    idx = pd.date_range("2026-01-01 00:00:00", periods=81, freq="1h")
    rows = [{"Open": 1.1000, "High": 1.1005, "Low": 1.0995, "Close": 1.1000, "Volume": 100} for _ in idx]
    return pd.DataFrame(rows, index=idx)


def make_self_test_runner(params: dict[str, Any], bars: pd.DataFrame, executor: FakeExecutor) -> S21EhlersRunner:
    runner = S21EhlersRunner(params)
    runner.dm = FakeDataManager(bars)
    runner.executor = executor
    runner._suppress_manual_alerts = True
    runner._save_state = lambda: None
    runner._trade_row = lambda *args, **kwargs: None
    runner._error_row = lambda *args, **kwargs: None
    runner.state = runner._default_state()
    return runner


def run_self_test() -> int:
    try:
        params = self_test_params()
        params["broker_timezone"] = "UTC"
        bars = make_self_test_bars()
        spec = params["symbols"][0]
        closed = normalize_bars(bars, True, "UTC")
        signal = latest_ehlers_signal(closed, spec) if closed is not None else None
        assert signal and signal["side"] == "long", "Ehlers long signal was not detected"

        runner = make_self_test_runner(params, bars, FakeExecutor())
        runner.run_once()
        active = runner.state["symbols"]["AUDUSD"]["active"]
        assert active and active["side"] == "short" and active["shadow"], "shadow short position was not opened"
        assert active["position_role"] == "normal", "first normal cycle entry must be recorded as normal role"
        assert float(active["sl"]) > float(active["entry"]) > float(active["tp"]), "shadow SL/TP are not around entry"

        runner = make_self_test_runner(params, make_no_signal_bars(), FakeExecutor())
        st = runner.state["symbols"]["AUDUSD"]
        st["active_positions"] = [
            {
                "ticket": 5001,
                "shadow": True,
                "side": "short",
                "position_role": "normal",
                "lot": 0.01,
                "entry": 1.10505,
                "last_price": 1.10505,
                "sl": 1.10185,
                "tp": 1.10825,
                "opened_at": dt_text(utc_now()),
                "signal_bar_time": "2026-01-01T00:00:00+00:00",
                "comment": "s21_audusd_audusd_n",
            }
        ]
        st["active"] = st["active_positions"][0]
        runner._clear_position(spec, st["active_positions"][0], "shadow_tp", 3.2)
        assert st["pending_reversal"], "normal TP close must prepare a pending reversal"
        assert st["pending_reversal"]["origin_side"] == "short", "pending reversal should remember origin side"
        assert st["pending_reversal"]["reversal_side"] == "long", "pending reversal should reverse side"
        runner.run_once()
        active = runner.state["symbols"]["AUDUSD"]["active"]
        assert active and active["position_role"] == "reversal" and active["reversal_of_ticket"] == 5001, "reversal open must be created from pending tp"
        assert runner.state["symbols"]["AUDUSD"]["pending_reversal"] is None, "pending reversal must be consumed after one execution"

        runner = make_self_test_runner(params, make_no_signal_bars(), FakeExecutor())
        st = runner.state["symbols"]["AUDUSD"]
        first = {
            "ticket": -5002, "shadow": True, "side": "long", "position_role": "normal", "lot": 0.01,
            "entry": 1.1000, "last_price": 1.1032, "sl": 1.0968, "tp": 1.1032,
            "opened_at": dt_text(utc_now()), "signal_bar_time": "2026-01-01T01:00:00+00:00", "comment": "s21_audusd_audusd_n",
        }
        second = {
            "ticket": -5003, "shadow": True, "side": "short", "position_role": "normal", "lot": 0.01,
            "entry": 1.1000, "last_price": 1.0968, "sl": 1.1032, "tp": 1.0968,
            "opened_at": dt_text(utc_now()), "signal_bar_time": "2026-01-01T02:00:00+00:00", "comment": "s21_audusd_audusd_n",
        }
        runner._set_active_positions(spec, [first, second])
        runner._clear_position(spec, first, "shadow_tp", 3.2)
        runner._clear_position(spec, second, "shadow_tp", 3.2)
        assert [item["origin_ticket"] for item in st["pending_reversals"]] == [-5002, -5003], "same-poll TP closes must queue both reversals"

        runner = make_self_test_runner(params, bars, FakeExecutor())
        st = runner.state["symbols"]["AUDUSD"]
        st["active_positions"] = [
            {
                "ticket": -200021100,
                "shadow": True,
                "side": "short",
                "position_role": "normal",
                "lot": 0.01,
                "entry": 1.10505,
                "last_price": 1.10505,
                "sl": 1.10905,
                "tp": 1.10105,
                "opened_at": dt_text(utc_now()),
                "signal_bar_time": "2026-01-01T00:00:00+00:00",
                "comment": "s21_audusd_audusd_n",
            }
        ]
        st["active"] = st["active_positions"][0]
        runner.run_once()
        st = runner.state["symbols"]["AUDUSD"]
        assert len(st["active_positions"]) == 2, "existing active should not block cycle entry scheduling"
        assert not st["sync_block_new_entries"], "existing active should not block cycle entry scheduling"

        runner = make_self_test_runner(params, make_no_signal_bars(), FakeExecutor())
        st = runner.state["symbols"]["AUDUSD"]
        st["active_positions"] = [
            {
                "ticket": 5201,
                "shadow": True,
                "side": "long",
                "position_role": "normal",
                "lot": 0.01,
                "entry": 1.10505,
                "last_price": 1.10505,
                "sl": 1.10185,
                "tp": 1.10825,
                "opened_at": dt_text(utc_now()),
                "signal_bar_time": "2026-01-01T00:00:00+00:00",
                "comment": "s21_audusd_audusd_n",
                "reversal_of_ticket": None,
            },
            {
                "ticket": 5202,
                "shadow": True,
                "side": "short",
                "position_role": "reversal",
                "lot": 0.01,
                "entry": 1.10505,
                "last_price": 1.10505,
                "sl": 1.10825,
                "tp": 1.10105,
                "opened_at": dt_text(utc_now()),
                "signal_bar_time": "2026-01-01T00:00:00+00:00",
                "comment": "s21_audusd_audusd_r",
                "reversal_of_ticket": 5201,
            },
        ]
        st["active"] = st["active_positions"][0]
        st["pending_reversal"] = {
            "origin_ticket": 5201,
            "origin_side": "long",
            "origin_reason": "shadow_tp",
            "origin_role": "normal",
            "reversal_side": "short",
            "created_at_utc": dt_text(utc_now()),
            "created_signal_bar": "2026-01-01T00:00:00+00:00",
        }
        runner.run_once()
        st = runner.state["symbols"]["AUDUSD"]
        assert st["pending_reversal"] is None, "pending reversal should be ignored when reversal already exists after restart"
        assert len(st["active_positions"]) == 2, "restart with stale pending reversal must not create duplicate reversal"

        recovered_reversal = FakePosition(5302, side="short", comment="s21_audusd_r5301", open_time=int(utc_now().timestamp()))
        runner = make_self_test_runner(params, make_no_signal_bars(), FakeExecutor(positions=[recovered_reversal]))
        st = runner.state["symbols"]["AUDUSD"]
        st["pending_reversal"] = {
            "origin_ticket": 5301,
            "origin_side": "long",
            "origin_reason": "live_tp",
            "origin_role": "normal",
            "reversal_side": "short",
            "created_at_utc": dt_text(utc_now()),
            "created_signal_bar": "2026-01-01T00:00:00+00:00",
        }
        runner.run_once()
        st = runner.state["symbols"]["AUDUSD"]
        assert st["pending_reversal"] is None, "broker recovery must consume stale pending reversal by origin comment"
        assert len(st["active_positions"]) == 1 and st["active_positions"][0]["reversal_of_ticket"] == 5301, "broker recovery must restore reversal origin"

        foreign = FakePosition(9101, magic=999999, comment="foreign_bot")
        runner = make_self_test_runner(params, bars, FakeExecutor(positions=[foreign], orders=[FakeOrder(9201, magic=999999)]))
        runner.run_once()
        active = runner.state["symbols"]["AUDUSD"]["active"]
        assert active and active["shadow"], "foreign same-symbol exposure should be ignored"

        unexpected = FakePosition(9102, magic=200021, comment="manual_audusd")
        runner = make_self_test_runner(params, bars, FakeExecutor(positions=[unexpected]))
        runner.run_once()
        st = runner.state["symbols"]["AUDUSD"]
        assert st["sync_block_new_entries"] and st["sync_block_reason"] == "same_magic_unexpected_position", "unexpected same-magic comment must block"
        assert st["active"] is None, "unexpected same-magic comment must not be adopted"

        unexpected_order = FakeOrder(9202, magic=200021, comment="manual_pending")
        runner = make_self_test_runner(params, bars, FakeExecutor(orders=[unexpected_order]))
        runner.run_once()
        st = runner.state["symbols"]["AUDUSD"]
        assert st["sync_block_new_entries"] and st["sync_block_reason"] == "same_magic_unexpected_order", "unexpected same-magic pending order must block"
        assert st["active"] is None, "unexpected same-magic pending order must not be adopted"

        live_params = deep_merge(params, {"live_trading_enabled": True, "shadow_forward_enabled": False})
        live_open_executor = FakeExecutor(open_adds_position=True)
        runner = make_self_test_runner(live_params, bars, live_open_executor)
        runner.run_once()
        st = runner.state["symbols"]["AUDUSD"]
        assert st["active"] and not st["active"]["shadow"] and st["active"]["ticket"] == 3001, "live OPEN must be confirmed from POSITIONS before active state"
        assert live_open_executor.modified, "live OPEN should repair SL/TP from confirmed fill when needed"
        assert st["active"]["position_role"] == "normal", "live open should be recorded as normal role"
        if st["active"]["side"] == "long":
            assert float(st["active"]["sl"]) < float(st["active"]["entry"]) < float(st["active"]["tp"]), "confirmed live SL/TP should bracket entry for long"
        else:
            assert float(st["active"]["sl"]) > float(st["active"]["entry"]) > float(st["active"]["tp"]), "confirmed live SL/TP should bracket entry for short"

        drift_pos = FakePosition(2200, comment="s21_audusd_audusd")
        runner = make_self_test_runner(live_params, make_no_signal_bars(), FakeExecutor(positions=[drift_pos]))
        runner.state["symbols"]["AUDUSD"]["active"] = {
            "ticket": 1100,
            "shadow": False,
            "side": "long",
            "lot": 0.01,
            "entry": 1.10505,
            "last_price": 1.10505,
            "sl": 1.10185,
            "tp": 1.10825,
            "opened_at": dt_text(utc_now()),
            "signal_bar_time": "2026-01-01T00:00:00+00:00",
            "comment": "s21_audusd_audusd",
        }
        runner.run_once()
        assert runner.state["symbols"]["AUDUSD"]["active"]["ticket"] == 2200, "ticket drift should recover a unique owned position"
        assert not runner.state["symbols"]["AUDUSD"]["sync_block_new_entries"], "ticket drift recovery should not leave a block"

        foreign_ticket_pos = FakePosition(3300, magic=999999, comment="foreign_bot")
        runner = make_self_test_runner(live_params, make_no_signal_bars(), FakeExecutor(positions=[foreign_ticket_pos]))
        runner.state["symbols"]["AUDUSD"]["active"] = {
            "ticket": 3300,
            "shadow": False,
            "side": "long",
            "lot": 0.01,
            "entry": 1.10505,
            "last_price": 1.10505,
            "sl": 1.10185,
            "tp": 1.10825,
            "opened_at": dt_text(utc_now()),
            "signal_bar_time": "2026-01-01T00:00:00+00:00",
            "comment": "s21_audusd_audusd",
        }
        runner.run_once()
        st = runner.state["symbols"]["AUDUSD"]
        assert st["sync_block_new_entries"] and st["sync_block_reason"] == "state_ticket_unowned_or_foreign", "foreign state ticket must fail closed"
        assert st["active"] and st["active"]["ticket"] == 3300, "foreign state ticket must not clear local active state"

        runner = make_self_test_runner(params, make_no_signal_bars(), FakeExecutor())
        st = runner.state["symbols"]["AUDUSD"]
        st["sync_block_new_entries"] = True
        st["sync_block_reason"] = "positions_unavailable"
        st["sync_block_recoverable"] = True
        save_calls = []
        runner._save_state = lambda: save_calls.append(True)
        runner.run_once()
        assert not runner.state["symbols"]["AUDUSD"]["sync_block_new_entries"], "recoverable sync block should clear after clean sync"
        assert save_calls, "recoverable sync block clear should be saved"

        runner = make_self_test_runner(params, make_no_signal_bars(), FakeExecutor())
        st = runner.state["symbols"]["AUDUSD"]
        st["sync_block_new_entries"] = True
        st["sync_block_reason"] = "same_magic_unexpected_order"
        st["sync_block_recoverable"] = False
        st["sync_block_details"] = {"tickets": [9103], "comments": ["[sl 0.69527]"]}
        save_calls = []
        runner._save_state = lambda: save_calls.append(True)
        runner.run_once()
        assert not runner.state["symbols"]["AUDUSD"]["sync_block_new_entries"], "flat broker SL residual block should clear"
        assert save_calls, "flat broker SL residual clear should be saved"

        hist_bars = make_no_signal_bars()
        converted = normalize_bars(hist_bars, False, "Europe/Athens")
        assert converted is not None and converted.index.tz is not None, "HIST normalization should produce tz-aware UTC bars"
        assert str(converted.index.tz) == "UTC", "HIST normalization should convert to UTC"
        assert converted.index[0].hour == 0, "naive EA HIST 00:00 should be treated as UTC, not broker local time"

        runner = make_self_test_runner(live_params, bars, FakeExecutor(open_error="ERR|10027"))
        runner.run_once()
        st = runner.state["symbols"]["AUDUSD"]
        assert not st["active"], "trade-permission reject must not create active state"
        assert not st["sync_block_new_entries"], "definitive trade-permission reject should use retry, not ambiguous block"
        assert st["open_retry_after_utc"], "trade-permission reject should set retry cooldown"
        assert int(st["autotrading_reject_streak"]) == 1, "trade-permission reject streak should increment"

        runner = make_self_test_runner(live_params, bars, FakeExecutor(open_error="NO_RESPONSE"))
        runner.run_once()
        st = runner.state["symbols"]["AUDUSD"]
        assert st["sync_block_new_entries"] and st["sync_block_reason"] == "ambiguous_open_result", "no-response open result must fail closed"

        runner = make_self_test_runner(live_params, make_no_signal_bars(), FakeExecutor())
        st = runner.state["symbols"]["AUDUSD"]
        st["last_closed_side"] = "short"
        st["last_closed_at_utc"] = "2026-01-01T08:00:11+00:00"
        st["last_closed_reason"] = "live_time_close"
        blocked = runner._same_direction_reentry_blocked_after_close(
            spec,
            {"side": "SHORT", "bar_time": "2026-01-01 07:00:00+00:00"},
            "short",
        )
        assert blocked, "same-direction signal known before close should be skipped"
        assert st["last_signal_bar"] == "2026-01-01 07:00:00+00:00", "skipped same-direction signal should be consumed"

        st["last_signal_bar"] = None
        allowed = runner._same_direction_reentry_blocked_after_close(
            spec,
            {"side": "LONG", "bar_time": "2026-01-01 07:00:00+00:00"},
            "long",
        )
        assert not allowed, "opposite-direction signal should not be blocked by same-direction close guard"

        live_tp_executor = FakeExecutor(close_deal=FakeCloseDeal(6101, "DEAL_REASON_TP"))
        runner = make_self_test_runner(live_params, make_no_signal_bars(), live_tp_executor)
        runner._set_active_from_position(
            spec,
            FakePosition(6101, identifier=6101, comment=runner._comment_for_position(spec, "normal")),
            signal_bar_time="2026-01-01 07:00:00+00:00",
            reason="test_live_tp",
        )
        runner._manage_active(spec, FakeInfo(), [])
        st = runner.state["symbols"]["AUDUSD"]
        assert st["pending_reversal"] and st["pending_reversal"]["origin_ticket"] == 6101, "confirmed MT5 TP deal must prepare reversal"

        manual_executor = FakeExecutor(close_deal=FakeCloseDeal(6201, "DEAL_REASON_CLIENT"))
        runner = make_self_test_runner(live_params, make_no_signal_bars(), manual_executor)
        runner._set_active_from_position(
            spec,
            FakePosition(6201, identifier=6201, comment=runner._comment_for_position(spec, "normal")),
            signal_bar_time="2026-01-01 07:00:00+00:00",
            reason="test_manual_close",
        )
        runner._manage_active(spec, FakeInfo(), [])
        assert runner.state["symbols"]["AUDUSD"]["pending_reversal"] is None, "non-TP close deal must never prepare reversal"

        pending_deal_executor = FakeExecutor(close_deal=False)
        runner = make_self_test_runner(live_params, make_no_signal_bars(), pending_deal_executor)
        runner._set_active_from_position(
            spec,
            FakePosition(6301, identifier=6301, comment=runner._comment_for_position(spec, "normal")),
            signal_bar_time="2026-01-01 07:00:00+00:00",
            reason="test_pending_deal",
        )
        runner._manage_active(spec, FakeInfo(), [])
        st = runner.state["symbols"]["AUDUSD"]
        assert st["active_positions"] and st["sync_block_reason"] == "close_deal_not_confirmed", "missing close deal must retain state and fail closed"

        runner = make_self_test_runner(params, make_no_signal_bars(), FakeExecutor())
        st = runner.state["symbols"]["AUDUSD"]
        st["last_signal_bar"] = "2026-01-02 07:00:00+00:00"
        opened = runner._open_position(
            spec,
            FakeInfo(),
            side="long",
            signal_bar_time="2026-01-01 07:00:00+00:00",
            reason="test_old_reversal",
            position_role="reversal",
            reversal_of_ticket=6401,
        )
        assert opened and st["last_signal_bar"] == "2026-01-02 07:00:00+00:00", "reversal must not rewind normal signal consumption"

        runner = make_self_test_runner(params, make_no_signal_bars(), FakeExecutor())
        st = runner.state["symbols"]["AUDUSD"]
        st["pending_reversal"] = {
            "origin_ticket": 6501,
            "reversal_side": "long",
            "created_signal_bar": "2026-01-01 07:00:00+00:00",
        }
        wide = FakeInfo()
        wide.ask = 1.10600
        wide.bid = 1.10500
        runner._process_pending_reversal(spec, wide)
        assert st["pending_reversal"] is not None, "failed reversal OPEN must remain pending"

        runner = make_self_test_runner(params, make_no_signal_bars(), FakeExecutor())
        st = runner.state["symbols"]["AUDUSD"]
        st["active_positions"] = [{
            "ticket": -6601,
            "position_identifier": -6601,
            "shadow": True,
            "side": "long",
            "position_role": "normal",
            "lot": 0.01,
            "entry": 1.10000,
            "last_price": 1.10000,
            "sl": 1.09000,
            "tp": 1.10400,
            "opened_at": "2026-01-01T00:00:00+00:00",
            "signal_bar_time": "2026-01-01 00:00:00+00:00",
            "comment": runner._comment_for_position(spec, "normal"),
            "reversal_of_ticket": None,
        }]
        st["active"] = st["active_positions"][0]
        runner._manage_active(spec, FakeInfo(), [])
        assert not st["active_positions"] and st["pending_reversal"], "shadow TP manage path must close once without stale single-active code"

        runner = make_self_test_runner(live_params, make_no_signal_bars(), FakeExecutor(account_mode=0))
        assert not runner.connect_and_preflight(), "live preflight must reject non-hedging accounts"

    except AssertionError as exc:
        print(f"self-test failed: {exc}")
        return 1
    print("self-test ok")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    setup_logging(file_logging=not args.self_test)
    if args.self_test:
        return run_self_test()
    params = load_params()
    runner = S21EhlersRunner(params)
    if not runner.connect_and_preflight():
        return 2
    if args.once:
        runner.run_once()
    else:
        runner.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
