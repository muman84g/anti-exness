# -*- coding: utf-8 -*-
"""Bot25 man_231 XAUUSD continuous bilateral core/satellite runner.

Strategy decisions use completed M5 bars. Order execution, 12-hour episode
expiry, feed-gap detection, spread-deferred full close, ownership sync, and
partial close confirmation run from fresh broker quotes on every poll.
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
import tempfile
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd

os.environ.setdefault("BOT_SUFFIX", "s25")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from live_data_fetcher import MT5DataManager
from live_config import MT5_LOGIN, MT5_SERVER
from live_executor import (
    CloseResult,
    HEDGING_MARGIN_MODE,
    REQUIRED_SHARED_ACCOUNT_COMMANDS,
    MT5Executor,
    ORDER_TYPE_BUY,
    ORDER_TYPE_SELL,
)
from live_manual_alerts import notify_manual_action_required
from live_safety import LiveSafetyOptions, clean_sync_block_if_flat


UTC = timezone.utc
EXPECTED_S25_MAGIC = 200025
STATE_VERSION = 5
FLAT_AUTO_CLEAR_SYNC_REASONS = {
    "open_success_position_not_confirmed",
    "close_unconfirmed",
    "close_failed",
}
FULL_SYNC_RECOVERABLE_REASONS = {
    "positions_or_orders_unavailable",
    "positions_unavailable",
    "orders_unavailable",
    "symbol_info_failed",
    "explicit_open_reject",
}
CLOSE_RECONCILIATION_RESOLVED_REASONS = {
    "close_deal_not_confirmed", "pending_close_ticket_unowned_or_unconfirmed",
    "close_retry_unconfirmed", "close_unconfirmed", "close_failed",
}

LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
STATE_DIR = os.path.join(SCRIPT_DIR, "state")
LOG_FILE = os.path.join(LOG_DIR, "s25_bot.log")
TRADE_LOG_FILE = os.path.join(LOG_DIR, "s25_trades.csv")
STATE_FILE = os.path.join(STATE_DIR, "s25_bot_state.json")
PARAMS_FILE = os.path.join(SCRIPT_DIR, "s25_params.json")

TRADE_FIELDS = [
    "timestamp_utc", "quote_time_utc", "event", "strategy_id", "magic", "symbol",
    "mt5_symbol", "opportunity_id", "basket_id", "episode_id", "ticket",
    "position_identifier", "deal_id", "ticket_set", "order_comment", "side", "lot",
    "price", "price_basis", "profit", "gross_profit", "commission", "swap", "fee",
    "profit_basis", "profit_currency", "reason", "broker_reason", "signal_bar_time",
    "event_time", "release_time", "available_time", "decision_time", "executable_at",
    "spread_points", "atr14", "ema200", "active_wave", "long_positions",
    "short_positions", "live", "repeat_count", "repeat_window_seconds", "note",
]
_CSV_SCHEMAS_VALIDATED: set[str] = set()
_CSV_EVENT_KEYS: dict[str, set[tuple[str, str]]] = {}
REPEATABLE_DIAGNOSTIC_EVENTS = {"m5_not_evaluated", "entry_blocked", "sync_block_retained"}


def utc_now() -> datetime:
    return datetime.now(UTC)


def dt_text(value: datetime | pd.Timestamp) -> str:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC").isoformat()


def parse_ts(value: Any) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return None
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def append_csv(path: str, row: dict[str, Any], fields: list[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    if exists and path not in _CSV_SCHEMAS_VALIDATED:
        with open(path, "r", newline="", encoding="utf-8") as existing:
            observed = next(csv.reader(existing), [])
        if observed != fields:
            old_dir = os.path.join(os.path.dirname(path), "old")
            os.makedirs(old_dir, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
            archived = os.path.join(old_dir, f"{os.path.splitext(os.path.basename(path))[0]}_schema_retired_{stamp}.csv")
            shutil.move(path, archived)
            logging.warning("S25 archived an incompatible prior trade CSV under logs/old")
            exists = False
        _CSV_SCHEMAS_VALIDATED.add(path)
    if path not in _CSV_EVENT_KEYS:
        keys: set[tuple[str, str]] = set()
        if exists:
            with open(path, "r", newline="", encoding="utf-8") as existing:
                for prior in csv.DictReader(existing):
                    if prior.get("deal_id"):
                        keys.add((str(prior.get("event") or ""), str(prior["deal_id"])))
        _CSV_EVENT_KEYS[path] = keys
    event_key = (str(row.get("event") or ""), str(row.get("deal_id") or ""))
    if event_key[1] and event_key in _CSV_EVENT_KEYS[path]:
        return
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
            _CSV_SCHEMAS_VALIDATED.add(path)
        writer.writerow({field: row.get(field, "") for field in fields})
    if event_key[1]:
        _CSV_EVENT_KEYS[path].add(event_key)


def add_man231_features(bars: pd.DataFrame) -> pd.DataFrame:
    """Reproduce the frozen M5 ATR14, EMA200 and strict-radius-2 pivot break."""
    out = bars.copy().sort_index()
    high = out["High"].astype(float)
    low = out["Low"].astype(float)
    close = out["Close"].astype(float)
    previous = close.shift(1)
    tr = pd.concat([high - low, (high - previous).abs(), (low - previous).abs()], axis=1).max(axis=1)
    out["atr14"] = tr.rolling(14, min_periods=14).mean()
    out["ema200"] = close.ewm(span=200, adjust=False, min_periods=200).mean()
    break_dir = np.zeros(len(out), dtype=np.int8)
    pivot_high = math.nan
    pivot_low = math.nan
    highs = high.to_numpy(float)
    lows = low.to_numpy(float)
    closes = close.to_numpy(float)
    atr = out["atr14"].to_numpy(float)
    ema = out["ema200"].to_numpy(float)
    for i in range(4, len(out)):
        pivot = i - 2
        if highs[pivot] > highs[pivot - 1] and highs[pivot] > highs[pivot - 2] and highs[pivot] > highs[pivot + 1] and highs[pivot] > highs[pivot + 2]:
            pivot_high = highs[pivot]
        if lows[pivot] < lows[pivot - 1] and lows[pivot] < lows[pivot - 2] and lows[pivot] < lows[pivot + 1] and lows[pivot] < lows[pivot + 2]:
            pivot_low = lows[pivot]
        if i < 200 or not math.isfinite(atr[i]) or not math.isfinite(ema[i]):
            continue
        buffer = 0.10 * atr[i]
        if math.isfinite(pivot_high) and closes[i - 1] <= pivot_high + buffer and closes[i] > pivot_high + buffer:
            break_dir[i] = 1
        elif math.isfinite(pivot_low) and closes[i - 1] >= pivot_low - buffer and closes[i] < pivot_low - buffer:
            break_dir[i] = -1
    out["break_dir"] = break_dir
    return out


def position_price_pnl(position: dict[str, Any], bid: float, ask: float) -> float:
    if position["side"] == "LONG":
        return float(bid) - float(position["entry_price"])
    return float(position["entry_price"]) - float(ask)


def position_key(position: dict[str, Any]) -> str:
    return str(position.get("position_identifier") or position.get("ticket") or position.get("shadow_id"))


def select_profitable_noncore(
    positions: list[dict[str, Any]], side: str, bid: float, ask: float,
    close_adverse_slippage: float = 0.0,
) -> list[dict[str, Any]]:
    """Protect the first best-priced ticket and return profitable satellites LIFO."""
    side_positions = [position for position in positions if position.get("side") == side]
    if not side_positions:
        return []
    core = side_positions[0]
    for position in side_positions[1:]:
        if side == "LONG" and float(position["entry_price"]) < float(core["entry_price"]):
            core = position
        elif side == "SHORT" and float(position["entry_price"]) > float(core["entry_price"]):
            core = position
    core_key = position_key(core)
    return [
        position for position in reversed(side_positions)
        if position_key(position) != core_key
        and position_price_pnl(position, bid - close_adverse_slippage, ask + close_adverse_slippage) > 0.0
    ]


class S25Man231Runner:
    def __init__(self, params: dict[str, Any]):
        self.params = params
        self.requested_live = bool(params.get("live_trading_enabled", False))
        gate_name = str(params.get("real_trading_activation_env") or "BOT25_ENABLE_REAL_TRADING")
        gate_value = str(params.get("real_trading_activation_value") or "MAN231_LIVE_ACK")
        self.activation_error = self.requested_live and os.getenv(gate_name) != gate_value
        self.live_enabled = self.requested_live and not self.activation_error
        self.shadow_enabled = bool(params.get("shadow_forward_enabled", True))
        self.safety = LiveSafetyOptions(**params.get("safety", {}))
        self.dm = MT5DataManager(self.safety)
        self.executor = MT5Executor()
        self.account_currency = str(params.get("backtest_profit_currency", "USD"))
        self.state = self._load_state()
        self._last_status_log = 0.0
        self._suppress_manual_alerts = False
        self._diagnostic_repeats: dict[str, dict[str, Any]] = {}

    def _default_strategy_state(self) -> dict[str, Any]:
        return {
            "positions": [],
            "episode_sequence": 0,
            "current_episode_id": None,
            "episode_start_quote_utc": None,
            "active_wave": 0,
            "last_atr": None,
            "last_ema": None,
            "last_long_frontier": None,
            "last_short_frontier": None,
            "last_processed_m5_bar": None,
            "last_quote_utc": None,
            "skip_seed_quote_utc": None,
            "pending_post_close_action": None,
            "pending_open": None,
            "pending_close_reason": None,
            "pending_close_m5_bar": None,
            "pending_close_requested_at_utc": None,
            "close_retry_after_utc": None,
            "close_defer": None,
            "shadow_sequence": 0,
            "entry_retry_until_utc": None,
            "trade_permission_reject_count": 0,
            "sync_block_new_entries": False,
            "sync_block_reason": None,
            "sync_block_recoverable": False,
            "sync_block_details": {},
            "flat_clear_confirmation_count": 0,
            "flat_clear_confirmation_reason": None,
            "manual_alert_last_signature": None,
            "manual_alert_last_reason": None,
            "manual_alert_last_at_utc": None,
            "last_decision_receipt_m5_bar": None,
            "startup_recovery_summary": None,
        }

    def _default_state(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "bot": "bot25",
            "strategy_id": self.params["strategy_id"],
            "last_saved_utc": None,
            "strategies": {strategy["id"]: self._default_strategy_state() for strategy in self.params["strategies"]},
        }

    def _load_state(self) -> dict[str, Any]:
        default = self._default_state()
        if not os.path.exists(STATE_FILE):
            return default
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except Exception:
            logging.exception("S25 state load failed; creating fail-closed state")
            state = {}
        if state.get("bot") != "bot25" or state.get("strategy_id") != self.params["strategy_id"] or int(state.get("version", 0)) != STATE_VERSION:
            logging.critical("S25 retired or foreign state identity detected; automatic adoption refused")
            default_state = default["strategies"][self.params["strategies"][0]["id"]]
            default_state["sync_block_new_entries"] = True
            default_state["sync_block_reason"] = "state_identity_mismatch"
            default_state["sync_block_recoverable"] = False
            return default
        for strategy in self.params["strategies"]:
            state.setdefault("strategies", {}).setdefault(strategy["id"], {})
            target = state["strategies"][strategy["id"]]
            for key, value in self._default_strategy_state().items():
                target.setdefault(key, value)
        return state

    def _save_state(self) -> None:
        self.state["last_saved_utc"] = dt_text(utc_now())
        atomic_write_json(STATE_FILE, self.state)

    def _st(self, strategy: dict[str, Any]) -> dict[str, Any]:
        return self.state["strategies"][strategy["id"]]

    def _trade_row(self, event: str, strategy: dict[str, Any], **kwargs: Any) -> None:
        state = self._st(strategy)
        now = utc_now()
        long_count, short_count = self._position_counts(strategy)
        row = {
            "timestamp_utc": dt_text(now),
            "event": event,
            "strategy_id": strategy["id"],
            "magic": int(strategy["magic"]),
            "symbol": self.params["symbol"],
            "mt5_symbol": self.params.get("mt5_symbol", self.params["symbol"]),
            "episode_id": state.get("current_episode_id") or "",
            "basket_id": state.get("current_episode_id") or "",
            "active_wave": state.get("active_wave"),
            "long_positions": long_count,
            "short_positions": short_count,
            "live": self.live_enabled,
        }
        row.update(kwargs)
        if any(row.get(field) not in (None, "") for field in ("profit", "gross_profit", "commission", "swap", "fee")):
            row.setdefault("profit_currency", self.account_currency)
        signature = (event, str(row.get("reason") or ""), str(row.get("note") or ""))
        active = self._diagnostic_repeats.get(strategy["id"])
        if event not in REPEATABLE_DIAGNOSTIC_EVENTS:
            self._flush_diagnostic_repeat(strategy, now)
            append_csv(TRADE_LOG_FILE, row, TRADE_FIELDS)
            return
        if active is None or active["signature"] != signature:
            self._flush_diagnostic_repeat(strategy, now)
            row["repeat_count"] = 1
            row["repeat_window_seconds"] = 0
            append_csv(TRADE_LOG_FILE, row, TRADE_FIELDS)
            self._diagnostic_repeats[strategy["id"]] = {"signature": signature, "first": now, "last": now, "suppressed": 0, "row": dict(row)}
            return
        active["last"] = now
        active["suppressed"] = int(active.get("suppressed", 0)) + 1
        if (now - active["first"]).total_seconds() >= float(self.params.get("diagnostic_repeat_summary_seconds", 300)):
            self._flush_diagnostic_repeat(strategy, now, keep_signature=True)

    def _flush_diagnostic_repeat(self, strategy: dict[str, Any], now: datetime | None = None, *, keep_signature: bool = False) -> None:
        active = self._diagnostic_repeats.get(strategy["id"])
        if active is None:
            return
        at = now or utc_now()
        suppressed = int(active.get("suppressed", 0))
        if suppressed > 0:
            row = dict(active["row"])
            row.update({
                "timestamp_utc": dt_text(at), "event": "diagnostic_repeat_summary", "repeat_count": suppressed,
                "repeat_window_seconds": round(max(0.0, (active["last"] - active["first"]).total_seconds()), 3),
                "note": f"source_event={active['signature'][0]};source_note={row.get('note') or ''}",
            })
            append_csv(TRADE_LOG_FILE, row, TRADE_FIELDS)
        if keep_signature:
            active["first"] = at
            active["last"] = at
            active["suppressed"] = 0
        else:
            self._diagnostic_repeats.pop(strategy["id"], None)

    def _manual_alert(self, strategy: dict[str, Any], reason: str, details: dict[str, Any]) -> None:
        if self._suppress_manual_alerts:
            return
        state = self._st(strategy)
        signature = json.dumps({"reason": reason, "details": details}, sort_keys=True, ensure_ascii=False)
        if state.get("manual_alert_last_signature") == signature:
            return
        state["manual_alert_last_signature"] = signature
        state["manual_alert_last_reason"] = reason
        state["manual_alert_last_at_utc"] = dt_text(utc_now())
        self._save_state()
        notify_manual_action_required(
            bot="bot25",
            reason=reason,
            details=details,
            action="Inspect only bot25-owned XAUUSD positions and state; do not restart or repair while exposure is ambiguous.",
        )

    def _set_sync_block(
        self, strategy: dict[str, Any], reason: str | None,
        details: dict[str, Any] | None = None, *, recoverable: bool = False,
    ) -> None:
        state = self._st(strategy)
        previous = state.get("sync_block_reason")
        if reason:
            if previous != reason:
                state["flat_clear_confirmation_count"] = 0
                state["flat_clear_confirmation_reason"] = None
                logging.error("S25 entry/add blocked: %s", reason)
            state["sync_block_new_entries"] = True
            state["sync_block_reason"] = reason
            state["sync_block_recoverable"] = bool(recoverable)
            state["sync_block_details"] = details or {}
            if not recoverable:
                self._manual_alert(strategy, reason, details or {})
            return
        state["sync_block_new_entries"] = False
        state["sync_block_reason"] = None
        state["sync_block_recoverable"] = False
        state["sync_block_details"] = {}
        state["flat_clear_confirmation_count"] = 0
        state["flat_clear_confirmation_reason"] = None
        state["manual_alert_last_signature"] = None

    @staticmethod
    def _side_from_record(record: Any) -> str:
        return "LONG" if int(getattr(record, "type", -1)) == ORDER_TYPE_BUY else "SHORT"

    def _owned_position(self, strategy: dict[str, Any], record: Any) -> bool:
        return (
            str(getattr(record, "symbol", "")) == str(self.params.get("mt5_symbol", self.params["symbol"]))
            and int(getattr(record, "magic", -1)) == int(strategy["magic"])
            and str(getattr(record, "comment", "") or "").startswith(str(strategy["comment_prefix"]))
        )

    def _state_ownership_proven(self, strategy: dict[str, Any], position: dict[str, Any]) -> bool:
        return (
            int(position.get("position_identifier") or position.get("ticket") or 0) != 0
            and str(position.get("owner_symbol") or "") == str(self.params.get("mt5_symbol", self.params["symbol"]))
            and int(position.get("owner_magic") or -1) == int(strategy["magic"])
            and str(position.get("owner_comment") or "").startswith(str(strategy["comment_prefix"]))
            and position.get("side") in {"LONG", "SHORT"}
        )

    def _state_matches_live(self, strategy: dict[str, Any], state_position: dict[str, Any], live_position: Any) -> bool:
        state_id = int(state_position.get("position_identifier") or state_position.get("ticket") or 0)
        live_id = int(getattr(live_position, "identifier", 0) or getattr(live_position, "ticket", 0))
        return state_id == live_id and state_position.get("side") == self._side_from_record(live_position) and self._owned_position(strategy, live_position)

    def _state_position_from_live(self, strategy: dict[str, Any], live_position: Any, *, entry_time_utc: str | None = None) -> dict[str, Any]:
        position_id = int(getattr(live_position, "identifier", 0) or live_position.ticket)
        return {
            "ticket": int(live_position.ticket), "position_identifier": position_id,
            "side": self._side_from_record(live_position), "lot": float(live_position.volume),
            "entry_price": float(live_position.open_price),
            "entry_time_utc": entry_time_utc or dt_text(pd.Timestamp(int(getattr(live_position, "open_time", 0)), unit="s", tz="UTC")),
            "open_time_epoch": int(getattr(live_position, "open_time", 0)),
            "owner_symbol": str(live_position.symbol), "owner_magic": int(live_position.magic),
            "owner_comment": str(getattr(live_position, "comment", "") or ""),
            "shadow": False, "close_requested": False,
        }

    def _reconcile_pending_open(self, strategy: dict[str, Any], positions: list[Any]) -> bool:
        state = self._st(strategy)
        pending = state.get("pending_open")
        if not pending:
            return True
        known = {int(value) for value in pending.get("known_position_ids", [])}
        candidates = [
            position for position in positions
            if int(getattr(position, "identifier", 0) or position.ticket) not in known
            and self._side_from_record(position) == pending.get("side")
            and abs(float(position.volume) - float(pending.get("lot", 0))) <= 1e-9
            and str(getattr(position, "comment", "") or "") == str(pending.get("comment", ""))
        ]
        if len(candidates) == 1:
            self._ensure_episode_identity(strategy)
            state["positions"].append(self._state_position_from_live(strategy, candidates[0], entry_time_utc=pending.get("quote_time_utc")))
            state["pending_open"] = None
            if state.get("sync_block_reason") in {None, "ambiguous_open_result", "pending_open_reconciliation_ambiguous"}:
                self._set_sync_block(strategy, None)
            recovery_quote = parse_ts(pending.get("quote_time_utc"))
            if recovery_quote is None:
                recovery_quote = pd.Timestamp(utc_now())
            self._trade_row(
                "entry_recovered_after_restart", strategy, quote_time_utc=dt_text(recovery_quote),
                opportunity_id=pending.get("opportunity_id"), ticket=int(candidates[0].ticket),
                position_identifier=int(getattr(candidates[0], "identifier", 0) or candidates[0].ticket),
                side=pending.get("side"), lot=pending.get("lot"), price=float(candidates[0].open_price),
                price_basis="broker_position_reconciliation", order_comment=pending.get("comment"),
                reason=pending.get("reason"), **self._causal_fields(pending.get("signal_bar_time"), recovery_quote),
            )
            self._save_state()
            return True
        unknown_ids = {int(getattr(position, "identifier", 0) or position.ticket) for position in positions} - known
        if not candidates and not unknown_ids:
            pending["flat_confirmation_count"] = int(pending.get("flat_confirmation_count", 0)) + 1
            if pending["flat_confirmation_count"] >= 2:
                state["pending_open"] = None
                if state.get("sync_block_reason") in {None, "ambiguous_open_result", "pending_open_reconciliation_ambiguous"}:
                    self._set_sync_block(strategy, None)
                self._trade_row(
                    "ambiguous_open_resolved_flat", strategy, opportunity_id=pending.get("opportunity_id"),
                    side=pending.get("side"), lot=pending.get("lot"), order_comment=pending.get("comment"),
                    reason=pending.get("reason"), note="two_consecutive_owned_flat_confirmations",
                )
            self._save_state()
            return state.get("pending_open") is None
        self._set_sync_block(strategy, "pending_open_reconciliation_ambiguous", {"candidate_count": len(candidates), "unknown_count": len(unknown_ids)}, recoverable=False)
        self._save_state()
        return False

    def _ownership_namespace_error(self) -> str | None:
        strategies = [strategy for strategy in self.params.get("strategies", []) if strategy.get("enabled", True)]
        if len(strategies) != 1:
            return f"expected_one_strategy:{len(strategies)}"
        strategy = strategies[0]
        if int(strategy.get("magic", 0)) != EXPECTED_S25_MAGIC or int(self.params.get("expected_magic", 0)) != EXPECTED_S25_MAGIC:
            return "unexpected_magic"
        if not str(strategy.get("comment_prefix") or "").startswith("s25_m231"):
            return "unexpected_comment_prefix"
        return None

    def _validate_symbol_contract(self, strategy: dict[str, Any], info: Any) -> str | None:
        lot = float(strategy.get("lot", self.params.get("default_lot", 0.01)))
        minimum = float(getattr(info, "volume_min", lot))
        maximum = float(getattr(info, "volume_max", lot))
        step = float(getattr(info, "volume_step", lot))
        if lot < minimum - 1e-12 or lot > maximum + 1e-12:
            return f"lot_out_of_range:{lot}:{minimum}:{maximum}"
        if step <= 0 or abs(round(lot / step) * step - lot) > 1e-9:
            return f"lot_off_step:{lot}:{step}"
        expected_point = float(self.params.get("point_size", 0.001))
        if abs(float(getattr(info, "point", expected_point)) - expected_point) > 1e-12:
            return f"point_mismatch:{getattr(info, 'point', None)}:{expected_point}"
        return None

    def connect_and_preflight(self) -> bool:
        if self.activation_error:
            logging.critical("S25 real-trading request lacks the independent activation environment gate")
            return False
        if not self.live_enabled and not self.shadow_enabled:
            logging.critical("S25 has neither live nor shadow mode enabled")
            return False
        namespace_error = self._ownership_namespace_error()
        if namespace_error:
            logging.critical("S25 ownership namespace invalid: %s", namespace_error)
            return False
        strategy = self.params["strategies"][0]
        if self._st(strategy).get("sync_block_reason") == "state_identity_mismatch":
            logging.critical("Archive retired bot25 state before starting man_231")
            return False
        if not bool(self.params.get("enabled", True)) or not self.dm.connect():
            return False
        caps = self.executor.get_bridge_capabilities()
        if not caps:
            return False
        if str(caps.get("name")) != str(self.params["expected_bridge_name"]):
            logging.critical("S25 wrong bridge: %s", caps)
            return False
        if str(caps.get("version")) != str(self.params["expected_bridge_version"]):
            logging.critical("S25 bridge version mismatch: got=%s expected=%s", caps.get("version"), self.params["expected_bridge_version"])
            return False
        missing = REQUIRED_SHARED_ACCOUNT_COMMANDS - {str(command).upper() for command in caps.get("commands", set())}
        if missing:
            logging.critical("S25 bridge missing commands: %s", sorted(missing))
            return False
        info = self.executor.get_symbol_info(str(self.params.get("mt5_symbol", self.params["symbol"])))
        if info is None or int(getattr(info, "quote_time_msc", 0)) <= 0:
            logging.critical("S25 fresh quote timestamp unavailable")
            return False
        symbol_error = self._validate_symbol_contract(strategy, info)
        if symbol_error:
            logging.critical("S25 symbol contract failed: %s", symbol_error)
            return False
        account = self.executor.get_account_info()
        if account is None:
            logging.critical("S25 account identity unavailable")
            return False
        try:
            account_identity_matches = int(account.get("login", -1)) == int(MT5_LOGIN) and str(account.get("server", "")) == str(MT5_SERVER)
        except (TypeError, ValueError):
            account_identity_matches = False
        if not account_identity_matches:
            logging.critical("S25 connected terminal does not match configured account identity")
            return False
        self.account_currency = str(account.get("currency") or "").strip()
        if not self.account_currency:
            logging.critical("S25 account currency unavailable")
            return False
        if self.live_enabled:
            if account is None or int(account.get("margin_mode", -1)) != HEDGING_MARGIN_MODE:
                logging.critical("S25 real orders require an MT5 hedging account")
                return False
            if not all(bool(account.get(key, False)) for key in ("account_trade_allowed", "account_trade_expert", "terminal_trade_allowed", "mql_trade_allowed")):
                logging.critical("S25 MT5 trading permission is incomplete")
                return False
        if not self._sync_strategy(strategy):
            return False
        state = self._st(strategy)
        if state["positions"]:
            self._ensure_episode_identity(strategy)
            if state.get("episode_start_quote_utc") is None:
                recovered_times = [parse_ts(position.get("entry_time_utc")) for position in state["positions"]]
                recovered_times = [value for value in recovered_times if value is not None]
                state["episode_start_quote_utc"] = dt_text(min(recovered_times) if recovered_times else pd.Timestamp(int(info.quote_time_msc), unit="ms", tz="UTC"))
        long_count, short_count = self._position_counts(strategy)
        summary = {
            "at_utc": dt_text(utc_now()), "live": self.live_enabled,
            "owned_positions": long_count + short_count, "owned_orders": 0,
            "long_positions": long_count, "short_positions": short_count,
            "pending_open": bool(state.get("pending_open")),
            "sync_block_reason": state.get("sync_block_reason"),
        }
        state["startup_recovery_summary"] = summary
        self._save_state()
        self._trade_row(
            "startup_recovery", strategy,
            quote_time_utc=dt_text(pd.Timestamp(int(info.quote_time_msc), unit="ms", tz="UTC")),
            opportunity_id=self._opportunity_id(None, pd.Timestamp(int(info.quote_time_msc), unit="ms", tz="UTC"), "startup"),
            reason="clean_owned_sync", note=json.dumps(summary, ensure_ascii=True, sort_keys=True),
        )
        return True

    def _get_m5(self) -> pd.DataFrame | None:
        bars = self.dm.get_historical_data(
            str(self.params.get("mt5_symbol", self.params["symbol"])),
            int(self.params.get("m5_timeframe", 5)),
            int(self.params.get("m5_bars", 260)),
            str(self.params.get("broker_timezone", "UTC")),
            drop_latest=bool(self.params.get("drop_latest_m5_bar", True)),
        )
        if bars is None or len(bars) < 205:
            return None
        return add_man231_features(bars)

    def _sync_strategy(self, strategy: dict[str, Any]) -> bool:
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        state = self._st(strategy)
        positions = self.executor.get_positions(symbol, int(strategy["magic"]))
        orders = self.executor.get_orders(symbol, int(strategy["magic"]))
        if positions is None:
            self._set_sync_block(strategy, "positions_unavailable", recoverable=True)
            self._save_state()
            return False
        orders_available = orders is not None
        if not orders_available:
            orders = []
            self._set_sync_block(strategy, "orders_unavailable", recoverable=True)
        unexpected = [record for record in [*positions, *orders] if not self._owned_position(strategy, record)]
        if unexpected:
            self._set_sync_block(strategy, "same_magic_unexpected_position_or_order", {"tickets": [int(record.ticket) for record in unexpected]}, recoverable=False)
            self._save_state()
            return False
        if orders:
            self._set_sync_block(strategy, "same_magic_unexpected_order", {"tickets": [int(order.ticket) for order in orders]}, recoverable=False)
            self._save_state()
            return False
        if not state.get("positions") and not state.get("pending_open") and orders_available and clean_sync_block_if_flat(
            symbol_key=strategy["id"], state=state, positions=positions, orders=orders,
            save_state=self._save_state, options=self.safety,
            audit=lambda _symbol, event, reason: self._trade_row(event, strategy, reason=reason, note=_symbol),
            flat_auto_clear_reasons=FLAT_AUTO_CLEAR_SYNC_REASONS,
            confirm_position_absent=self.executor.confirm_position_absent,
            required_flat_confirmations=2,
        ):
            logging.info("S25 clean sync cleared a recoverable block")
        if not self.live_enabled:
            return True
        state_positions = list(state.get("positions") or [])
        if state_positions and not state.get("current_episode_id"):
            self._ensure_episode_identity(strategy)
        if not state_positions and positions and not state.get("pending_open"):
            self._set_sync_block(strategy, "live_positions_without_state", {"tickets": [int(position.ticket) for position in positions]}, recoverable=False)
            self._save_state()
            return False
        live_by_id = {int(getattr(position, "identifier", 0) or position.ticket): position for position in positions}
        if not self._reconcile_pending_open(strategy, positions):
            return False
        state_positions = list(state.get("positions") or [])
        state_ids = {int(position.get("position_identifier") or position.get("ticket") or 0) for position in state_positions}
        if set(live_by_id) - state_ids:
            self._set_sync_block(strategy, "untracked_owned_position", {"state_ids": sorted(state_ids), "live_ids": sorted(live_by_id)}, recoverable=False)
            self._save_state()
            return False
        remaining: list[dict[str, Any]] = []
        confirmed_closes: list[tuple[dict[str, Any], Any]] = []
        for state_position in state_positions:
            position_id = int(state_position.get("position_identifier") or state_position.get("ticket") or 0)
            live_position = live_by_id.get(position_id)
            if live_position is not None:
                if not self._state_matches_live(strategy, state_position, live_position):
                    self._set_sync_block(strategy, "state_position_ownership_mismatch", {"ticket": position_id}, recoverable=False)
                    self._save_state()
                    return False
                remaining.append(state_position)
                continue
            opened_at = max(0, int(state_position.get("open_time_epoch") or 0) - 60)
            deal = self.executor.get_position_close_deal(position_id, opened_at)
            if deal is False:
                deal = self.executor.get_position_close_deal(position_id, max(0, opened_at - 86400))
            if deal is None or deal is False:
                self._set_sync_block(strategy, "close_deal_not_confirmed", {"ticket": position_id}, recoverable=True)
                self._save_state()
                return False
            if int(deal.position_id) != position_id or str(deal.symbol) != symbol or int(deal.magic) != int(strategy["magic"]) or not self._state_ownership_proven(strategy, state_position):
                self._set_sync_block(strategy, "close_deal_ownership_mismatch", {"ticket": position_id}, recoverable=False)
                self._save_state()
                return False
            confirmed_closes.append((state_position, deal))
        if len(remaining) != len(state_positions):
            state["positions"] = remaining
            pending_signal = state.get("pending_close_m5_bar")
            requested_at = parse_ts(state.get("pending_close_requested_at_utc"))
            decision_at = requested_at if requested_at is not None else pd.Timestamp(utc_now())
            for state_position, deal in confirmed_closes:
                deal_epoch = int(getattr(deal, "deal_time", 0) or 0)
                deal_time = pd.Timestamp(deal_epoch, unit="s", tz="UTC") if deal_epoch > 0 else pd.Timestamp(utc_now())
                if requested_at is None:
                    decision_at = deal_time
                causal = self._causal_fields(pending_signal, decision_at)
                causal["executable_at"] = dt_text(deal_time)
                causal["event_time"] = dt_text(deal_time)
                self._trade_row(
                    "position_close_confirmed", strategy, quote_time_utc=dt_text(deal_time),
                    opportunity_id=self._opportunity_id(pending_signal, decision_at, "m5" if pending_signal else "close"),
                    ticket=state_position.get("ticket"),
                    position_identifier=int(state_position.get("position_identifier") or state_position.get("ticket") or 0),
                    deal_id=getattr(deal, "deal", 0) or "", ticket_set=str(state_position.get("ticket") or ""),
                    order_comment=state_position.get("owner_comment"), side=state_position.get("side"),
                    lot=state_position.get("lot"), price=float(deal.price), price_basis="broker_close_deal",
                    profit=float(deal.net_profit), gross_profit=float(getattr(deal, "profit", deal.net_profit)),
                    commission=float(getattr(deal, "commission", 0.0)), swap=float(getattr(deal, "swap", 0.0)),
                    fee=float(getattr(deal, "fee", 0.0)), profit_basis="broker_net_account_currency",
                    reason=state.get("pending_close_reason") or "external_close_confirmed",
                    broker_reason=str(getattr(deal, "reason", "") or ""), **causal,
                )
            if not any(position.get("close_requested") for position in remaining):
                state["pending_close_reason"] = None
                state["pending_close_m5_bar"] = None
                state["pending_close_requested_at_utc"] = None
                state["close_retry_after_utc"] = None
            if state.get("sync_block_reason") in CLOSE_RECONCILIATION_RESOLVED_REASONS:
                self._set_sync_block(strategy, None)
            self._save_state()
        if orders_available and state.get("sync_block_new_entries") and state.get("sync_block_recoverable") and state.get("sync_block_reason") in FULL_SYNC_RECOVERABLE_REASONS:
            self._set_sync_block(strategy, None)
            self._save_state()
        return True

    def _position_counts(self, strategy: dict[str, Any]) -> tuple[int, int]:
        positions = self._st(strategy)["positions"]
        return sum(position["side"] == "LONG" for position in positions), sum(position["side"] == "SHORT" for position in positions)

    def _ensure_episode_identity(self, strategy: dict[str, Any]) -> str:
        state = self._st(strategy)
        if not state.get("current_episode_id"):
            state["episode_sequence"] = int(state.get("episode_sequence", 0)) + 1
            state["current_episode_id"] = f"s25_m231_e{int(state['episode_sequence']):06d}"
        return str(state["current_episode_id"])

    def _spread_points(self, info: Any) -> float:
        return max(0.0, (float(info.ask) - float(info.bid)) / float(self.params.get("point_size", 0.001)))

    @staticmethod
    def _opportunity_id(signal_bar_time: str | None, quote_time: pd.Timestamp, kind: str) -> str:
        signal = parse_ts(signal_bar_time)
        stamp = signal.strftime("%Y%m%dT%H%M%SZ") if signal is not None else quote_time.strftime("%Y%m%dT%H%M%S%fZ")
        return f"m231_{kind}_{stamp}"

    @staticmethod
    def _causal_fields(signal_bar_time: str | None, quote_time: pd.Timestamp) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "decision_time": dt_text(quote_time), "executable_at": dt_text(quote_time),
        }
        signal = parse_ts(signal_bar_time)
        if signal is not None:
            available = signal + pd.Timedelta(minutes=5)
            fields.update({
                "signal_bar_time": dt_text(signal), "event_time": dt_text(signal),
                "release_time": dt_text(available), "available_time": dt_text(available),
            })
        return fields

    def _entry_allowed(self, strategy: dict[str, Any], info: Any, quote_time: pd.Timestamp) -> bool:
        state = self._st(strategy)
        retry = parse_ts(state.get("entry_retry_until_utc"))
        return (
            not state.get("sync_block_new_entries")
            and not state.get("pending_open")
            and (retry is None or quote_time >= retry)
            and self._spread_points(info) <= float(self.params.get("max_entry_spread_points", 300.0))
            and self._validate_symbol_contract(strategy, info) is None
        )

    def _open_position(
        self, strategy: dict[str, Any], side: str, info: Any,
        quote_time: pd.Timestamp, reason: str, signal_bar_time: str | None = None,
        opportunity_id: str | None = None,
    ) -> bool:
        state = self._st(strategy)
        if not self._entry_allowed(strategy, info, quote_time):
            return False
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        lot = float(strategy.get("lot", self.params.get("default_lot", 0.01)))
        digits = int(self.params.get("price_digits", 3))
        state["shadow_sequence"] = int(state.get("shadow_sequence", 0)) + 1
        sequence = int(state["shadow_sequence"])
        comment = f"{strategy['comment_prefix']}_{'L' if side == 'LONG' else 'S'}{sequence:04d}"[:31]
        opportunity_id = opportunity_id or self._opportunity_id(signal_bar_time, quote_time, reason)
        causal = self._causal_fields(signal_bar_time, quote_time)
        ticket: int | None = None
        confirmed = None
        entry_price = float(info.ask if side == "LONG" else info.bid)
        if self.live_enabled:
            known = {int(position.get("position_identifier") or position.get("ticket") or 0) for position in state["positions"]}
            state["pending_open"] = {
                "side": side, "lot": lot, "comment": comment, "reason": reason,
                "quote_time_utc": dt_text(quote_time), "known_position_ids": sorted(known),
                "flat_confirmation_count": 0, "opportunity_id": opportunity_id,
                "signal_bar_time": signal_bar_time, "decision_time": dt_text(quote_time),
            }
            self._save_state()
            self._trade_row(
                "open_reserved", strategy, quote_time_utc=dt_text(quote_time),
                opportunity_id=opportunity_id, side=side, lot=lot,
                price=entry_price, price_basis="pre_submit_executable_quote",
                order_comment=comment, reason=reason, spread_points=self._spread_points(info),
                atr14=state.get("last_atr"), ema200=state.get("last_ema"), **causal,
            )
            ticket = self.executor.open_position(
                symbol, ORDER_TYPE_BUY if side == "LONG" else ORDER_TYPE_SELL, lot, 0.0, 0.0,
                deviation=int(self.params.get("deviation_points", 50)), magic=int(strategy["magic"]),
                comment=comment, digits=digits,
            )
            error = str(getattr(self.executor, "last_order_error", None) or "")
            positions = self.executor.get_positions(symbol, int(strategy["magic"]))
            if positions is None:
                self._set_sync_block(strategy, "positions_unavailable_after_open", {"ticket": ticket or 0}, recoverable=False)
                self._save_state()
                return False
            new_owned = [position for position in positions if self._owned_position(strategy, position) and int(getattr(position, "identifier", 0) or position.ticket) not in known]
            if ticket is not None:
                matches = [position for position in new_owned if int(position.ticket) == ticket or int(getattr(position, "identifier", 0) or 0) == ticket]
                if len(matches) == 1:
                    confirmed = matches[0]
            elif len(new_owned) == 1:
                confirmed = new_owned[0]
                ticket = int(confirmed.ticket)
            if confirmed is None:
                if not new_owned and error.startswith("ERR|"):
                    state["pending_open"] = None
                    state["entry_retry_until_utc"] = dt_text(quote_time + pd.Timedelta(seconds=float(self.params.get("trade_permission_retry_seconds", 30))))
                    if error.startswith("ERR|10026") or error.startswith("ERR|10027"):
                        state["trade_permission_reject_count"] = int(state.get("trade_permission_reject_count", 0)) + 1
                        if state["trade_permission_reject_count"] >= int(self.params.get("trade_permission_alert_threshold", 3)):
                            self._manual_alert(strategy, "repeated_trade_permission_reject", {"error": error, "count": state["trade_permission_reject_count"]})
                    self._trade_row(
                        "entry_rejected_retry", strategy, side=side, reason=error,
                        quote_time_utc=dt_text(quote_time), opportunity_id=opportunity_id,
                        order_comment=comment, spread_points=self._spread_points(info), **causal,
                    )
                    self._save_state()
                    return False
                self._trade_row(
                    "open_ambiguous", strategy, quote_time_utc=dt_text(quote_time),
                    opportunity_id=opportunity_id, ticket=ticket or "", side=side, lot=lot,
                    order_comment=comment, reason=error or "unknown_open_result", **causal,
                )
                self._set_sync_block(strategy, "ambiguous_open_result", {"ticket": ticket or 0, "new_owned": [int(position.ticket) for position in new_owned], "error": error}, recoverable=False)
                self._save_state()
                return False
            entry_price = float(confirmed.open_price)
            state["pending_open"] = None
            state["trade_permission_reject_count"] = 0
            state["entry_retry_until_utc"] = None
        else:
            ticket = -sequence
            proxy_slip = float(self.params.get("shadow_adverse_slippage_price", 0.030))
            entry_price += proxy_slip if side == "LONG" else -proxy_slip
        position_id = int(getattr(confirmed, "identifier", 0) or ticket or 0) if confirmed is not None else int(ticket or 0)
        if confirmed is not None:
            state["positions"].append(self._state_position_from_live(strategy, confirmed, entry_time_utc=dt_text(quote_time)))
        else:
            state["positions"].append({
                "ticket": int(ticket or 0), "position_identifier": position_id, "side": side, "lot": lot,
                "entry_price": entry_price, "entry_time_utc": dt_text(quote_time),
                "open_time_epoch": int(quote_time.timestamp()), "owner_symbol": symbol,
                "owner_magic": int(strategy["magic"]), "owner_comment": comment,
                "shadow": True, "close_requested": False,
            })
        logged_ticket = int(getattr(confirmed, "ticket", 0) or ticket or 0)
        price_basis = "broker_confirmed_open" if confirmed is not None else "shadow_adverse_cost_proxy"
        self._trade_row(
            "entry", strategy, quote_time_utc=dt_text(quote_time), opportunity_id=opportunity_id,
            ticket=logged_ticket,
            position_identifier=position_id, side=side, lot=lot, price=entry_price,
            price_basis=price_basis, deal_id=getattr(self.executor, "last_open_deal", None) or "",
            order_comment=str(getattr(confirmed, "comment", "") or comment), reason=reason,
            spread_points=self._spread_points(info), atr14=state.get("last_atr"),
            ema200=state.get("last_ema"), **causal,
        )
        self._save_state()
        return True

    def _close_positions(
        self, strategy: dict[str, Any], selected: list[dict[str, Any]], reason: str,
        info: Any, quote_time: pd.Timestamp, m5_bar: str | None,
    ) -> str:
        if not selected:
            return "nothing"
        state = self._st(strategy)
        close_causal = self._causal_fields(m5_bar, quote_time)
        opportunity_id = self._opportunity_id(m5_bar, quote_time, "m5" if m5_bar else "close")
        selected_keys = {position_key(position) for position in selected}
        ticket_set = ";".join(str(position.get("ticket") or "") for position in selected)
        mtm_profit = sum(
            position_price_pnl(position, float(info.bid), float(info.ask))
            * float(self.params.get("contract_size", 100.0)) * float(position["lot"])
            for position in selected
        )
        if self.live_enabled:
            for position in selected:
                ticket = int(position["ticket"])
                live_position = self.executor.get_position(ticket)
                if live_position is None or live_position is False or not self._owned_position(strategy, live_position):
                    self._set_sync_block(strategy, "state_ticket_unowned_or_foreign", {"ticket": ticket}, recoverable=False)
                    self._save_state()
                    return "blocked"
            state["pending_close_reason"] = reason
            state["pending_close_m5_bar"] = m5_bar
            state["pending_close_requested_at_utc"] = dt_text(quote_time)
            for position in state["positions"]:
                if position_key(position) in selected_keys:
                    position["close_requested"] = True
            self._save_state()
            self._trade_row(
                "close_reserved", strategy, quote_time_utc=dt_text(quote_time),
                opportunity_id=opportunity_id, ticket_set=ticket_set, profit=mtm_profit,
                profit_basis="executable_mtm_before_close", reason=reason,
                spread_points=self._spread_points(info), **close_causal,
            )
            market_closed = False
            for position in selected:
                result = self.executor.close_position(int(position["ticket"]), int(self.params.get("deviation_points", 50)))
                if result:
                    continue
                status = str(getattr(result, "status", "FAILED"))
                if status == "MARKET_CLOSED":
                    market_closed = True
                    position["close_requested"] = False
                    continue
                self._set_sync_block(strategy, "close_unconfirmed" if status in {"MISSING_UNCONFIRMED", "MALFORMED_OK"} else "close_failed", {"ticket": int(position["ticket"]), "status": status}, recoverable=False)
                self._save_state()
                return "blocked"
            if market_closed:
                defer = state.get("close_defer") or {"reason": reason, "armed_at_utc": dt_text(quote_time)}
                defer["next_retry_utc"] = dt_text(quote_time + pd.Timedelta(seconds=float(self.params.get("time_close_market_closed_retry_seconds", 60))))
                defer["first_wide_quote_utc"] = None
                defer["stable_quote_count"] = 0
                state["close_defer"] = defer
                self._trade_row(
                    "DEFER", strategy, reason="market_closed", quote_time_utc=dt_text(quote_time),
                    opportunity_id=opportunity_id, ticket_set=ticket_set,
                    spread_points=self._spread_points(info), **close_causal,
                )
                self._save_state()
                return "market_closed"
            state["close_retry_after_utc"] = dt_text(quote_time + pd.Timedelta(seconds=float(self.params.get("close_retry_seconds", 15))))
            self._trade_row(
                "close_requested", strategy, reason=reason, quote_time_utc=dt_text(quote_time),
                opportunity_id=opportunity_id, ticket_set=ticket_set, profit=mtm_profit,
                profit_basis="executable_mtm_before_close", spread_points=self._spread_points(info),
                note=f"tickets={len(selected)}", **close_causal,
            )
            self._save_state()
            return "requested"
        retained: list[dict[str, Any]] = []
        closed_rows: list[dict[str, Any]] = []
        for position in state["positions"]:
            if position_key(position) not in selected_keys:
                retained.append(position)
                continue
            proxy_slip = float(self.params.get("shadow_adverse_slippage_price", 0.030))
            proxy_bid = float(info.bid) - proxy_slip
            proxy_ask = float(info.ask) + proxy_slip
            price = proxy_bid if position["side"] == "LONG" else proxy_ask
            pnl = position_price_pnl(position, proxy_bid, proxy_ask) * float(self.params.get("contract_size", 100.0)) * float(position["lot"])
            closed_rows.append({
                "ticket": position["ticket"], "position_identifier": position["position_identifier"],
                "order_comment": position.get("owner_comment"), "side": position["side"],
                "lot": position["lot"], "price": price, "profit": pnl,
            })
        state["positions"] = retained
        for row in closed_rows:
            self._trade_row(
                "close", strategy, quote_time_utc=dt_text(quote_time),
                opportunity_id=opportunity_id, ticket_set=str(row["ticket"]),
                price_basis="shadow_adverse_cost_proxy", gross_profit=row["profit"],
                commission=0.0, swap=0.0, fee=0.0,
                profit_basis="shadow_cost_proxy_account_currency", reason=reason,
                spread_points=self._spread_points(info), **row, **close_causal,
            )
        self._save_state()
        return "completed"

    def _reset_episode(self, strategy: dict[str, Any], quote_time: pd.Timestamp) -> None:
        state = self._st(strategy)
        state["episode_start_quote_utc"] = None
        state["active_wave"] = 0
        state["last_long_frontier"] = None
        state["last_short_frontier"] = None
        state["pending_post_close_action"] = None
        state["pending_close_reason"] = None
        state["pending_close_m5_bar"] = None
        state["pending_close_requested_at_utc"] = None
        state["close_retry_after_utc"] = None
        state["close_defer"] = None
        state["current_episode_id"] = None
        state["skip_seed_quote_utc"] = dt_text(quote_time)
        self._save_state()

    def _ensure_bilateral_seed(self, strategy: dict[str, Any], info: Any, quote_time: pd.Timestamp) -> bool:
        state = self._st(strategy)
        if state.get("skip_seed_quote_utc") == dt_text(quote_time):
            return False
        episode_id = self._ensure_episode_identity(strategy)
        long_count, short_count = self._position_counts(strategy)
        if long_count == 0 and not self._open_position(strategy, "LONG", info, quote_time, "bilateral_seed", opportunity_id=f"m231_seed_{episode_id}_LONG"):
            return False
        long_count, short_count = self._position_counts(strategy)
        if short_count == 0 and not self._open_position(strategy, "SHORT", info, quote_time, "bilateral_seed", opportunity_id=f"m231_seed_{episode_id}_SHORT"):
            return False
        long_count, short_count = self._position_counts(strategy)
        if long_count >= 1 and short_count >= 1 and state.get("episode_start_quote_utc") is None:
            mid = 0.5 * (float(info.bid) + float(info.ask))
            state["episode_start_quote_utc"] = dt_text(quote_time)
            state["last_long_frontier"] = mid
            state["last_short_frontier"] = mid
            state["active_wave"] = 0
            self._trade_row(
                "episode_start", strategy, quote_time_utc=dt_text(quote_time),
                opportunity_id=f"m231_episode_{episode_id}", reason="bilateral_inventory_established",
                decision_time=dt_text(quote_time), executable_at=dt_text(quote_time),
            )
            self._save_state()
        return long_count >= 1 and short_count >= 1

    def _arm_full_close(self, strategy: dict[str, Any], reason: str, quote_time: pd.Timestamp) -> None:
        state = self._st(strategy)
        if state.get("close_defer") is None:
            state["close_defer"] = {
                "reason": reason,
                "armed_at_utc": dt_text(quote_time),
                "first_wide_quote_utc": None,
                "last_evaluated_quote_utc": None,
                "stable_quote_count": 0,
                "next_retry_utc": None,
            }
            self._trade_row("DEFER", strategy, quote_time_utc=dt_text(quote_time), reason=reason)
            self._save_state()

    def _full_close_quote_ready(self, strategy: dict[str, Any], info: Any, quote_time: pd.Timestamp) -> bool:
        state = self._st(strategy)
        defer = state.get("close_defer")
        if not defer:
            return False
        retry = parse_ts(defer.get("next_retry_utc"))
        if retry is not None and quote_time < retry:
            return False
        if defer.get("last_evaluated_quote_utc") == dt_text(quote_time):
            return False
        defer["last_evaluated_quote_utc"] = dt_text(quote_time)
        spread = self._spread_points(info)
        limit = float(self.params.get("time_close_spread_limit_points", 300.0))
        first_wide = parse_ts(defer.get("first_wide_quote_utc"))
        if first_wide is None:
            if spread <= limit:
                return True
            defer["first_wide_quote_utc"] = dt_text(quote_time)
            defer["stable_quote_count"] = 0
            self._save_state()
            return False
        timeout = float(self.params.get("time_close_wide_timeout_minutes", 30))
        if quote_time - first_wide >= pd.Timedelta(minutes=timeout):
            self._trade_row("DEFER_TIMEOUT", strategy, quote_time_utc=dt_text(quote_time), reason=defer["reason"])
            return True
        if spread <= limit:
            defer["stable_quote_count"] = int(defer.get("stable_quote_count", 0)) + 1
        else:
            defer["stable_quote_count"] = 0
        self._save_state()
        return int(defer["stable_quote_count"]) >= int(self.params.get("time_close_stable_quotes", 3))

    def _process_full_close(self, strategy: dict[str, Any], info: Any, quote_time: pd.Timestamp) -> bool:
        state = self._st(strategy)
        if not state.get("close_defer"):
            return False
        if not state["positions"]:
            self._reset_episode(strategy, quote_time)
            return True
        if not self._full_close_quote_ready(strategy, info, quote_time):
            return True
        reason = str(state["close_defer"]["reason"])
        result = self._close_positions(strategy, list(state["positions"]), reason, info, quote_time, None)
        if result == "completed":
            self._trade_row("RESUME", strategy, quote_time_utc=dt_text(quote_time), reason=reason)
            self._reset_episode(strategy, quote_time)
        return True

    def _apply_pending_post_close(self, strategy: dict[str, Any], info: Any, quote_time: pd.Timestamp) -> bool:
        state = self._st(strategy)
        action = state.get("pending_post_close_action")
        if not action:
            return False
        if any(position.get("close_requested") for position in state["positions"]):
            return True
        if not self._ensure_bilateral_seed(strategy, info, quote_time):
            return True
        state["active_wave"] = int(action.get("new_wave", 0))
        state["pending_post_close_action"] = None
        self._save_state()
        return False

    def _retry_pending_close_requests(self, strategy: dict[str, Any], quote_time: pd.Timestamp) -> bool:
        """Retry only previously reserved bot25 closes after a bounded confirmation wait."""
        state = self._st(strategy)
        pending = [position for position in state["positions"] if position.get("close_requested")]
        if not pending:
            return False
        retry_after = parse_ts(state.get("close_retry_after_utc"))
        if retry_after is not None and quote_time < retry_after:
            return True
        pending_signal = state.get("pending_close_m5_bar")
        requested_at = parse_ts(state.get("pending_close_requested_at_utc"))
        causal = self._causal_fields(pending_signal, requested_at if requested_at is not None else quote_time)
        causal["executable_at"] = dt_text(quote_time)
        opportunity_id = self._opportunity_id(pending_signal, quote_time, "m5" if pending_signal else "close")
        ticket_set = ";".join(str(position.get("ticket") or "") for position in pending)
        for position in pending:
            ticket = int(position["ticket"])
            live_position = self.executor.get_position(ticket)
            if live_position is None or live_position is False or not self._owned_position(strategy, live_position):
                self._set_sync_block(strategy, "pending_close_ticket_unowned_or_unconfirmed", {"ticket": ticket}, recoverable=False)
                self._save_state()
                return True
            result = self.executor.close_position(ticket, int(self.params.get("deviation_points", 50)))
            if result:
                continue
            status = str(getattr(result, "status", "FAILED"))
            if status == "MARKET_CLOSED":
                state["close_retry_after_utc"] = dt_text(quote_time + pd.Timedelta(seconds=float(self.params.get("time_close_market_closed_retry_seconds", 60))))
                self._trade_row(
                    "DEFER", strategy, quote_time_utc=dt_text(quote_time), reason="market_closed_close_retry",
                    opportunity_id=opportunity_id, ticket=ticket, ticket_set=ticket_set,
                    **causal,
                )
                self._save_state()
                return True
            self._set_sync_block(strategy, "close_retry_unconfirmed", {"ticket": ticket, "status": status}, recoverable=False)
            self._save_state()
            return True
        state["close_retry_after_utc"] = dt_text(quote_time + pd.Timedelta(seconds=float(self.params.get("close_retry_seconds", 15))))
        self._trade_row(
            "close_retry_requested", strategy, quote_time_utc=dt_text(quote_time),
            opportunity_id=opportunity_id, ticket_set=ticket_set,
            reason=state.get("pending_close_reason") or "pending_close",
            note=f"tickets={len(pending)}", **causal,
        )
        self._save_state()
        return True

    def _release_active_side(
        self, strategy: dict[str, Any], new_wave: int, reason: str,
        info: Any, quote_time: pd.Timestamp, m5_bar: str,
    ) -> bool:
        state = self._st(strategy)
        active = int(state.get("active_wave", 0))
        side = "LONG" if active == 1 else "SHORT"
        close_buffer = (
            float(self.params.get("live_release_profit_buffer_price", 0.030))
            if self.live_enabled else float(self.params.get("shadow_adverse_slippage_price", 0.030))
        )
        selected = select_profitable_noncore(
            state["positions"], side, float(info.bid), float(info.ask), close_buffer,
        )
        if not selected:
            state["active_wave"] = int(new_wave)
            self._save_state()
            return False
        result = self._close_positions(strategy, selected, reason, info, quote_time, m5_bar)
        if result == "completed":
            self._ensure_bilateral_seed(strategy, info, quote_time)
            state["active_wave"] = int(new_wave)
            self._save_state()
            return False
        if result == "requested":
            state["pending_post_close_action"] = {"new_wave": int(new_wave), "reason": reason, "m5_bar": m5_bar}
            self._save_state()
        return True

    def _m5_receipt(
        self, strategy: dict[str, Any], bar_time: pd.Timestamp, quote_time: pd.Timestamp,
        *, reason: str, side: str = "", note: str = "",
    ) -> None:
        state = self._st(strategy)
        bar_key = dt_text(bar_time)
        if state.get("last_decision_receipt_m5_bar") == bar_key:
            return
        available = bar_time + pd.Timedelta(minutes=5)
        state["last_decision_receipt_m5_bar"] = bar_key
        executable_at = "" if reason.startswith("not_evaluated") else dt_text(quote_time)
        self._trade_row(
            "m5_decision", strategy, quote_time_utc=dt_text(quote_time), side=side,
            opportunity_id=self._opportunity_id(bar_key, quote_time, "m5"),
            reason=reason, signal_bar_time=bar_key, event_time=bar_key,
            release_time=dt_text(available), available_time=dt_text(available),
            decision_time=dt_text(quote_time), executable_at=executable_at,
            atr14=state.get("last_atr"), ema200=state.get("last_ema"), note=note,
        )

    def _process_m5_event(self, strategy: dict[str, Any], row: pd.Series, info: Any, quote_time: pd.Timestamp) -> None:
        state = self._st(strategy)
        bar_time = parse_ts(row.name)
        if bar_time is None:
            return
        bar_key = dt_text(bar_time)
        if state.get("last_processed_m5_bar") == bar_key:
            if state.get("last_decision_receipt_m5_bar") != bar_key:
                self._m5_receipt(strategy, bar_time, quote_time, reason="recovered_after_restart", note="processed_bar_had_no_receipt")
                self._save_state()
            return
        state["last_processed_m5_bar"] = bar_key
        atr = float(row.get("atr14", math.nan))
        ema = float(row.get("ema200", math.nan))
        if math.isfinite(atr) and math.isfinite(ema):
            state["last_atr"] = atr
            state["last_ema"] = ema
        available_at = bar_time + pd.Timedelta(minutes=5)
        if quote_time < available_at:
            self._trade_row("m5_not_evaluated", strategy, quote_time_utc=dt_text(quote_time), signal_bar_time=bar_key, reason="future_or_unavailable_completed_bar")
            self._m5_receipt(strategy, bar_time, quote_time, reason="not_evaluated_future_bar", note="action=not_evaluated;future_or_unavailable_completed_bar")
            self._save_state()
            return
        if quote_time > available_at + pd.Timedelta(minutes=float(self.params.get("max_signal_delay_minutes", 7))):
            self._trade_row("m5_not_evaluated", strategy, quote_time_utc=dt_text(quote_time), signal_bar_time=bar_key, reason="stale_completed_bar")
            self._m5_receipt(strategy, bar_time, quote_time, reason="not_evaluated_stale", note="action=not_evaluated;stale_completed_bar")
            self._save_state()
            return
        if not math.isfinite(atr) or not math.isfinite(ema):
            self._trade_row("m5_not_evaluated", strategy, quote_time_utc=dt_text(quote_time), signal_bar_time=bar_key, reason="warmup")
            self._m5_receipt(strategy, bar_time, quote_time, reason="not_evaluated_warmup", note="action=not_evaluated;warmup")
            self._save_state()
            return
        if not self._ensure_bilateral_seed(strategy, info, quote_time):
            self._m5_receipt(strategy, bar_time, quote_time, reason="signal" if int(row.get("break_dir", 0)) else "no_signal", note="action=entry_blocked;bilateral_seed_incomplete")
            self._save_state()
            return
        active = int(state.get("active_wave", 0))
        new_break = int(row.get("break_dir", 0))
        if new_break != 0:
            if active != 0 and new_break != active:
                if self._release_active_side(strategy, new_break, "opposite_pivot_break", info, quote_time, bar_key):
                    self._m5_receipt(strategy, bar_time, quote_time, reason="signal", side="LONG" if active == 1 else "SHORT", note=f"action=close_requested;break_dir={new_break}")
                    self._save_state()
                    return
            elif new_break != active:
                state["active_wave"] = new_break
                self._save_state()
        active = int(state.get("active_wave", 0))
        release = (active == 1 and float(info.bid) <= ema) or (active == -1 and float(info.bid) >= ema)
        if release:
            if self._release_active_side(strategy, 0, "ema200_retouch", info, quote_time, bar_key):
                self._m5_receipt(strategy, bar_time, quote_time, reason="signal" if new_break else "no_signal", side="LONG" if active == 1 else "SHORT", note="action=close_requested;ema200_retouch")
                self._save_state()
                return
            active = 0
        active = int(state.get("active_wave", 0))
        if active == 0 or not self._entry_allowed(strategy, info, quote_time):
            self._m5_receipt(strategy, bar_time, quote_time, reason="signal" if new_break else "no_signal", note=f"action=no_add;break_dir={new_break};active_wave={active}")
            self._save_state()
            return
        long_count, short_count = self._position_counts(strategy)
        mid = 0.5 * (float(info.bid) + float(info.ask))
        step = float(strategy.get("frontier_add_atr", 0.50)) * atr
        max_side = int(strategy.get("max_positions_per_side", 6))
        ratio = int(strategy.get("max_active_to_opposite_ratio", 3))
        action = "no_add"
        opportunity_id = self._opportunity_id(bar_key, quote_time, "m5")
        if active == 1:
            frontier = float(state.get("last_long_frontier") or mid)
            if mid >= frontier + step and long_count < max_side and long_count < ratio * short_count:
                if self._open_position(strategy, "LONG", info, quote_time, "long_frontier_add", bar_key, opportunity_id):
                    state["last_long_frontier"] = mid
                    action = "entry_long"
                    self._save_state()
                else:
                    action = "entry_long_failed"
        else:
            frontier = float(state.get("last_short_frontier") or mid)
            if mid <= frontier - step and short_count < max_side and short_count < ratio * long_count:
                if self._open_position(strategy, "SHORT", info, quote_time, "short_frontier_add", bar_key, opportunity_id):
                    state["last_short_frontier"] = mid
                    action = "entry_short"
                    self._save_state()
                else:
                    action = "entry_short_failed"
        self._m5_receipt(
            strategy, bar_time, quote_time, reason="signal" if new_break else "no_signal",
            side="LONG" if active == 1 else "SHORT",
            note=f"action={action};break_dir={new_break};active_wave={active}",
        )
        self._save_state()

    def _run_strategy(self, strategy: dict[str, Any], bars: pd.DataFrame | None, info: Any, quote_time: pd.Timestamp) -> None:
        state = self._st(strategy)
        previous_quote = parse_ts(state.get("last_quote_utc"))
        if not self._sync_strategy(strategy):
            state["last_quote_utc"] = dt_text(quote_time)
            self._save_state()
            return
        if any(position.get("close_requested") for position in state["positions"]):
            self._retry_pending_close_requests(strategy, quote_time)
            state["last_quote_utc"] = dt_text(quote_time)
            self._save_state()
            return
        if state["positions"] and previous_quote is not None and quote_time - previous_quote > pd.Timedelta(minutes=float(self.params.get("feed_gap_minutes", 5))):
            self._arm_full_close(strategy, "feed_gap", quote_time)
        episode_start = parse_ts(state.get("episode_start_quote_utc"))
        if state["positions"] and episode_start is not None and quote_time - episode_start >= pd.Timedelta(minutes=float(self.params.get("episode_minutes", 720))):
            self._arm_full_close(strategy, "episode_12h", quote_time)
        if self._process_full_close(strategy, info, quote_time):
            state["last_quote_utc"] = dt_text(quote_time)
            self._save_state()
            return
        if self._apply_pending_post_close(strategy, info, quote_time):
            state["last_quote_utc"] = dt_text(quote_time)
            self._save_state()
            return
        if bars is not None and not bars.empty:
            self._process_m5_event(strategy, bars.iloc[-1], info, quote_time)
        elif not state["positions"] and not state.get("sync_block_new_entries"):
            self._trade_row("m5_not_evaluated", strategy, quote_time_utc=dt_text(quote_time), reason="m5_bars_unavailable")
        state["last_quote_utc"] = dt_text(quote_time)
        self._save_state()

    def run_once(self) -> None:
        strategy = self.params["strategies"][0]
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        info = self.executor.get_symbol_info(symbol)
        if info is None or int(getattr(info, "quote_time_msc", 0)) <= 0:
            self._set_sync_block(strategy, "symbol_info_failed", recoverable=True)
            self._save_state()
            return
        quote_time = pd.Timestamp(int(info.quote_time_msc), unit="ms", tz="UTC")
        bars = self._get_m5()
        self._run_strategy(strategy, bars, info, quote_time)
        now = time.time()
        if now - self._last_status_log >= float(self.params.get("status_log_interval_seconds", 300)):
            long_count, short_count = self._position_counts(strategy)
            logging.info("S25 man231 status live=%s shadow=%s long=%d short=%d wave=%s block=%s", self.live_enabled, self.shadow_enabled, long_count, short_count, self._st(strategy).get("active_wave"), self._st(strategy).get("sync_block_reason"))
            self._last_status_log = now


class FakeDM:
    def connect(self) -> bool:
        return True

    def get_historical_data(self, *_: Any, **__: Any) -> pd.DataFrame:
        index = pd.date_range(end="2026-08-27 00:20:00", periods=230, freq="5min", tz="UTC")
        values = np.linspace(4000.0, 4020.0, len(index))
        return pd.DataFrame({"Open": values, "High": values + 0.3, "Low": values - 0.3, "Close": values, "Volume": 10}, index=index)


class FakeExecutor:
    def __init__(self, *, positions: list[Any] | None = None, orders: list[Any] | None = None, margin_mode: int = HEDGING_MARGIN_MODE, quote_time: str = "2026-08-27T00:25:00Z", login: int | None = None, server: str | None = None):
        self.positions = list(positions or [])
        self.orders = list(orders or [])
        self.margin_mode = margin_mode
        self.login = int(MT5_LOGIN if login is None else login)
        self.server = str(MT5_SERVER if server is None else server)
        self.last_order_error = None
        self.last_open_deal = None
        self.last_open_price = None
        self.next_ticket = 1000
        self.next_deal = 12000
        self.deals: dict[int, Any] = {}
        self.info = SimpleNamespace(bid=4020.0, ask=4020.18, point=0.001, volume_min=0.01, volume_max=100.0, volume_step=0.01, digits=3, stops_level=0, quote_time_msc=int(pd.Timestamp(quote_time).timestamp() * 1000))

    def get_bridge_capabilities(self) -> dict[str, Any]:
        return {"name": "BotBridge_s25", "version": "2026-08-27-s25-man231-ops-v5", "commands": set(REQUIRED_SHARED_ACCOUNT_COMMANDS)}

    def get_account_info(self) -> dict[str, Any]:
        return {"login": self.login, "server": self.server, "currency": "USD", "margin_mode": self.margin_mode, "margin_mode_name": "RETAIL_HEDGING", "account_trade_allowed": True, "account_trade_expert": True, "terminal_trade_allowed": True, "mql_trade_allowed": True}

    def get_symbol_info(self, *_: Any) -> Any:
        return self.info

    def get_positions(self, symbol: str, magic: int) -> list[Any]:
        return [row for row in self.positions if row.symbol == symbol and int(row.magic) == int(magic)]

    def get_orders(self, symbol: str, magic: int) -> list[Any]:
        return [row for row in self.orders if row.symbol == symbol and int(row.magic) == int(magic)]

    def get_position(self, ticket: int) -> Any:
        return next((position for position in self.positions if int(position.ticket) == int(ticket)), False)

    def confirm_position_absent(self, ticket: int) -> bool:
        return self.get_position(ticket) is False

    def get_position_close_deal(self, position_id: int, *_: Any) -> Any:
        return self.deals.get(int(position_id), False)

    def open_position(self, symbol: str, order_type: int, lot: float, *_: Any, magic: int, comment: str, **__: Any) -> int:
        self.next_ticket += 1
        side_price = self.info.ask if order_type == ORDER_TYPE_BUY else self.info.bid
        record = SimpleNamespace(ticket=self.next_ticket, identifier=self.next_ticket + 5000, symbol=symbol, type=order_type, volume=lot, open_price=side_price, sl=0.0, tp=0.0, profit=0.0, magic=magic, open_time=int(self.info.quote_time_msc / 1000), comment=comment)
        self.positions.append(record)
        self.next_deal += 1
        self.last_open_deal = self.next_deal
        self.last_open_price = side_price
        return self.next_ticket

    def close_position(self, ticket: int, *_: Any, **__: Any) -> Any:
        position = self.get_position(ticket)
        if position is False:
            return CloseResult(False, status="MISSING_UNCONFIRMED")
        self.positions = [row for row in self.positions if int(row.ticket) != int(ticket)]
        self.next_deal += 1
        self.deals[int(position.identifier)] = SimpleNamespace(deal=self.next_deal, position_id=int(position.identifier), symbol=position.symbol, magic=position.magic, reason="EXPERT", price=self.info.bid if position.type == ORDER_TYPE_BUY else self.info.ask, profit=0.25, commission=-0.02, swap=-0.01, fee=0.0, net_profit=0.22, deal_time=int(self.info.quote_time_msc / 1000))
        return CloseResult(True, status="CONFIRMED")


def load_params(path: str = PARAMS_FILE) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def self_test() -> None:
    configured = load_params()
    assert configured["live_trading_enabled"] is True and configured["shadow_forward_enabled"] is False
    params = json.loads(json.dumps(configured))
    params["live_trading_enabled"] = False
    params["shadow_forward_enabled"] = True
    runner = S25Man231Runner(params)
    runner.state = runner._default_state()
    runner._save_state = lambda: None
    runner._suppress_manual_alerts = True
    runner.dm = FakeDM()
    runner.executor = FakeExecutor()
    strategy = params["strategies"][0]
    assert runner._ownership_namespace_error() is None
    assert runner.connect_and_preflight()
    runner.run_once()
    long_count, short_count = runner._position_counts(strategy)
    assert (long_count, short_count) == (1, 1), "shadow startup must seed both sides"
    state = runner._st(strategy)
    assert state["episode_start_quote_utc"] is not None
    assert state["current_episode_id"] == "s25_m231_e000001"
    assert state["last_decision_receipt_m5_bar"] is not None
    with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as handle:
        startup_rows = list(csv.DictReader(handle))
    startup_events = [row["event"] for row in startup_rows]
    assert startup_events.count("startup_recovery") == 1
    assert startup_events.count("entry") == 2 and startup_events.count("episode_start") == 1
    assert startup_events.count("m5_decision") == 1
    seed_entries = [row for row in startup_rows if row["event"] == "entry"]
    assert all(row["episode_id"] == "s25_m231_e000001" and row["basket_id"] == row["episode_id"] for row in seed_entries)
    assert len({row["opportunity_id"] for row in seed_entries}) == 2
    assert all(row["ticket"] and row["position_identifier"] and row["order_comment"] for row in seed_entries)
    assert all(row["price_basis"] == "shadow_adverse_cost_proxy" and not row["profit_currency"] for row in seed_entries)
    decision = next(row for row in startup_rows if row["event"] == "m5_decision")
    assert decision["reason"] in {"signal", "no_signal"}
    assert parse_ts(decision["available_time"]) <= parse_ts(decision["decision_time"]) <= parse_ts(decision["executable_at"])

    future_runner = S25Man231Runner(params)
    future_runner.state = future_runner._default_state()
    future_runner._save_state = lambda: None
    future_runner._suppress_manual_alerts = True
    future_row = pd.Series({"atr14": 1.0, "ema200": 4020.0, "break_dir": 0}, name=pd.Timestamp("2026-08-27T00:25:00Z"))
    future_runner._process_m5_event(strategy, future_row, FakeExecutor().info, pd.Timestamp("2026-08-27T00:25:00Z"))
    assert future_runner._st(strategy)["positions"] == []
    with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as handle:
        future_rows = list(csv.DictReader(handle))
    future_decision = next(row for row in future_rows if row["event"] == "m5_decision" and row["reason"] == "not_evaluated_future_bar")
    assert not future_decision["executable_at"]

    legacy_path = os.path.join(os.path.dirname(TRADE_LOG_FILE), "legacy_s25_trades.csv")
    with open(legacy_path, "w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerow(["old", "schema"])
    append_csv(legacy_path, {"event": "schema_restarted"}, TRADE_FIELDS)
    with open(legacy_path, "r", newline="", encoding="utf-8") as handle:
        assert next(csv.reader(handle)) == TRADE_FIELDS
    retired = os.path.join(os.path.dirname(legacy_path), "old")
    assert any(name.startswith("legacy_s25_trades_schema_retired_") for name in os.listdir(retired))

    positions = [
        {"position_identifier": 1, "ticket": 1, "side": "LONG", "entry_price": 4000.0},
        {"position_identifier": 2, "ticket": 2, "side": "LONG", "entry_price": 4005.0},
        {"position_identifier": 3, "ticket": 3, "side": "LONG", "entry_price": 4010.0},
    ]
    selected = select_profitable_noncore(positions, "LONG", 4011.0, 4011.2)
    assert [position["ticket"] for position in selected] == [3, 2], "LONG best-price core must be protected and satellites must close LIFO"
    short_positions = [
        {"position_identifier": 4, "ticket": 4, "side": "SHORT", "entry_price": 4010.0},
        {"position_identifier": 5, "ticket": 5, "side": "SHORT", "entry_price": 4005.0},
        {"position_identifier": 6, "ticket": 6, "side": "SHORT", "entry_price": 4000.0},
    ]
    selected = select_profitable_noncore(short_positions, "SHORT", 3998.8, 3999.0)
    assert [position["ticket"] for position in selected] == [6, 5], "SHORT best-price core must be protected"

    shadow_close = S25Man231Runner(params)
    shadow_close.state = shadow_close._default_state()
    shadow_close._save_state = lambda: None
    shadow_close._suppress_manual_alerts = True
    shadow_state = shadow_close._st(strategy)
    shadow_close._ensure_episode_identity(strategy)
    shadow_state["positions"] = [
        {"ticket": -11, "position_identifier": -11, "side": "LONG", "lot": 0.01, "entry_price": 4010.0, "owner_comment": "s25_m231_L0011"},
        {"ticket": -12, "position_identifier": -12, "side": "LONG", "lot": 0.01, "entry_price": 4015.0, "owner_comment": "s25_m231_L0012"},
        {"ticket": -13, "position_identifier": -13, "side": "SHORT", "lot": 0.01, "entry_price": 4030.0, "owner_comment": "s25_m231_S0013"},
    ]
    shadow_info = SimpleNamespace(bid=4020.0, ask=4020.18, point=0.001)
    assert shadow_close._close_positions(strategy, [shadow_state["positions"][1]], "shadow_log_test", shadow_info, pd.Timestamp("2026-08-27T00:25:00Z"), "2026-08-27T00:20:00Z") == "completed"
    with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as handle:
        shadow_rows = list(csv.DictReader(handle))
    shadow_logged = next(row for row in reversed(shadow_rows) if row["event"] == "close" and row["ticket"] == "-12")
    assert shadow_logged["long_positions"] == "1" and shadow_logged["short_positions"] == "1"
    assert shadow_logged["price_basis"] == "shadow_adverse_cost_proxy" and shadow_logged["profit_basis"] == "shadow_cost_proxy_account_currency"
    assert abs(float(shadow_logged["profit"]) - float(shadow_logged["gross_profit"])) < 1e-12

    full_close = S25Man231Runner(params)
    full_close.state = full_close._default_state()
    full_close._save_state = lambda: None
    full_close._suppress_manual_alerts = True
    full_state = full_close._st(strategy)
    full_close._ensure_episode_identity(strategy)
    full_state["episode_start_quote_utc"] = "2026-08-26T12:25:00+00:00"
    full_state["positions"] = [
        {"ticket": -21, "position_identifier": -21, "side": "LONG", "lot": 0.01, "entry_price": 4010.0, "owner_comment": "s25_m231_L0021"},
        {"ticket": -22, "position_identifier": -22, "side": "SHORT", "lot": 0.01, "entry_price": 4030.0, "owner_comment": "s25_m231_S0022"},
    ]
    full_close._arm_full_close(strategy, "episode_12h", pd.Timestamp("2026-08-27T00:25:00Z"))
    assert full_close._process_full_close(strategy, shadow_info, pd.Timestamp("2026-08-27T00:25:01Z"))
    assert full_state["positions"] == [] and full_state["current_episode_id"] is None
    with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as handle:
        full_rows = list(csv.DictReader(handle))
    assert any(row["event"] == "DEFER" and row["reason"] == "episode_12h" for row in full_rows)
    assert any(row["event"] == "RESUME" and row["reason"] == "episode_12h" for row in full_rows)

    defer_runner = S25Man231Runner(params)
    defer_runner.state = defer_runner._default_state()
    defer_runner._save_state = lambda: None
    defer_runner._suppress_manual_alerts = True
    defer_state = defer_runner._st(strategy)
    defer_state["close_defer"] = {"reason": "episode_12h", "armed_at_utc": "2026-08-27T00:00:00+00:00", "first_wide_quote_utc": None, "last_evaluated_quote_utc": None, "stable_quote_count": 0, "next_retry_utc": None}
    wide = SimpleNamespace(bid=4000.0, ask=4000.5)
    assert not defer_runner._full_close_quote_ready(strategy, wide, pd.Timestamp("2026-08-27T00:00:01Z"))
    narrow = SimpleNamespace(bid=4000.0, ask=4000.2)
    assert not defer_runner._full_close_quote_ready(strategy, narrow, pd.Timestamp("2026-08-27T00:00:02Z"))
    assert not defer_runner._full_close_quote_ready(strategy, narrow, pd.Timestamp("2026-08-27T00:00:02Z")), "same quote must not advance stability"
    assert not defer_runner._full_close_quote_ready(strategy, narrow, pd.Timestamp("2026-08-27T00:00:03Z"))
    assert defer_runner._full_close_quote_ready(strategy, narrow, pd.Timestamp("2026-08-27T00:00:04Z"))

    timeout_runner = S25Man231Runner(params)
    timeout_runner.state = timeout_runner._default_state()
    timeout_runner._save_state = lambda: None
    timeout_runner._suppress_manual_alerts = True
    timeout_state = timeout_runner._st(strategy)
    timeout_state["close_defer"] = {"reason": "episode_12h", "armed_at_utc": "2026-08-27T00:00:00+00:00", "first_wide_quote_utc": None, "last_evaluated_quote_utc": None, "stable_quote_count": 0, "next_retry_utc": None}
    assert not timeout_runner._full_close_quote_ready(strategy, wide, pd.Timestamp("2026-08-27T00:00:01Z"))
    assert timeout_runner._full_close_quote_ready(strategy, wide, pd.Timestamp("2026-08-27T00:30:01Z")), "wide-spread defer must time out"

    foreign = SimpleNamespace(ticket=99, identifier=99, symbol="XAUUSD", magic=EXPECTED_S25_MAGIC, comment="s23_foreign", type=ORDER_TYPE_BUY)
    foreign_runner = S25Man231Runner(params)
    foreign_runner.state = foreign_runner._default_state()
    foreign_runner._save_state = lambda: None
    foreign_runner._suppress_manual_alerts = True
    foreign_runner.executor = FakeExecutor(positions=[foreign])
    assert not foreign_runner._sync_strategy(strategy)
    assert foreign_runner._st(strategy)["sync_block_reason"] == "same_magic_unexpected_position_or_order"

    other_bot = SimpleNamespace(ticket=100, identifier=100, symbol="XAUUSD", magic=230023, comment="s23_owned", type=ORDER_TYPE_BUY)
    shared_runner = S25Man231Runner(params)
    shared_runner.state = shared_runner._default_state()
    shared_runner._save_state = lambda: None
    shared_runner._suppress_manual_alerts = True
    shared_runner.executor = FakeExecutor(positions=[other_bot])
    assert shared_runner._sync_strategy(strategy), "different-magic shared-account inventory must remain untouched"

    live_params = json.loads(json.dumps(params))
    live_params["live_trading_enabled"] = True
    live_params["shadow_forward_enabled"] = False
    old_gate = os.environ.pop(str(params["real_trading_activation_env"]), None)
    try:
        gated = S25Man231Runner(live_params)
        assert gated.activation_error and not gated.live_enabled, "legacy params alone must not enable real orders"
        os.environ[str(params["real_trading_activation_env"])] = str(params["real_trading_activation_value"])
        netting = S25Man231Runner(live_params)
        netting.state = netting._default_state()
        netting._save_state = lambda: None
        netting._suppress_manual_alerts = True
        netting.dm = FakeDM()
        netting.executor = FakeExecutor(margin_mode=0)
        assert not netting.connect_and_preflight(), "netting account must fail live preflight"

        wrong_account = S25Man231Runner(live_params)
        wrong_account.state = wrong_account._default_state()
        wrong_account._save_state = lambda: None
        wrong_account._suppress_manual_alerts = True
        wrong_account.dm = FakeDM()
        wrong_account.executor = FakeExecutor(login=int(MT5_LOGIN) + 1)
        assert not wrong_account.connect_and_preflight(), "wrong account identity must fail before trading"

        recovered = S25Man231Runner(live_params)
        recovered.state = recovered._default_state()
        recovered._save_state = lambda: None
        recovered._suppress_manual_alerts = True
        owned = SimpleNamespace(ticket=501, identifier=5501, symbol="XAUUSD", type=ORDER_TYPE_BUY, volume=0.01, open_price=4020.18, sl=0.0, tp=0.0, profit=0.0, magic=EXPECTED_S25_MAGIC, open_time=int(pd.Timestamp("2026-08-27T00:25:00Z").timestamp()), comment="s25_m231_L0001")
        recovered.executor = FakeExecutor(positions=[owned])
        recovered_state = recovered._st(strategy)
        recovered_state["pending_open"] = {"side": "LONG", "lot": 0.01, "comment": "s25_m231_L0001", "reason": "bilateral_seed", "quote_time_utc": "2026-08-27T00:25:00+00:00", "known_position_ids": [], "flat_confirmation_count": 0}
        assert recovered._sync_strategy(strategy)
        assert recovered_state["pending_open"] is None and len(recovered_state["positions"]) == 1

        class IdentifierReturnExecutor(FakeExecutor):
            def open_position(self, symbol: str, order_type: int, lot: float, *_: Any, magic: int, comment: str, **__: Any) -> int:
                record = SimpleNamespace(ticket=777, identifier=1777, symbol=symbol, type=order_type, volume=lot, open_price=self.info.ask if order_type == ORDER_TYPE_BUY else self.info.bid, sl=0.0, tp=0.0, profit=0.0, magic=magic, open_time=int(self.info.quote_time_msc / 1000), comment=comment)
                self.positions.append(record)
                self.last_open_deal = 2777
                self.last_open_price = record.open_price
                return 1777

        ticket_runner = S25Man231Runner(live_params)
        ticket_runner.state = ticket_runner._default_state()
        ticket_runner._save_state = lambda: None
        ticket_runner._suppress_manual_alerts = True
        ticket_runner.executor = IdentifierReturnExecutor()
        ticket_runner._ensure_episode_identity(strategy)
        assert ticket_runner._open_position(strategy, "LONG", ticket_runner.executor.info, pd.Timestamp("2026-08-27T00:25:00Z"), "ticket_identity_test", "2026-08-27T00:20:00Z", "m231_m5_ticket_test")
        with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as handle:
            ticket_rows = list(csv.DictReader(handle))
        ticket_entry = next(row for row in reversed(ticket_rows) if row["event"] == "entry" and row["opportunity_id"] == "m231_m5_ticket_test")
        assert (ticket_entry["ticket"], ticket_entry["position_identifier"], ticket_entry["deal_id"]) == ("777", "1777", "2777")

        class ExplicitRejectExecutor(FakeExecutor):
            def open_position(self, *_: Any, **__: Any) -> None:
                self.last_order_error = "ERR|10026|AUTOTRADING_DISABLED"
                return None

        rejected = S25Man231Runner(live_params)
        rejected.state = rejected._default_state()
        rejected._save_state = lambda: None
        rejected._suppress_manual_alerts = True
        rejected.executor = ExplicitRejectExecutor()
        rejected._ensure_episode_identity(strategy)
        assert not rejected._open_position(strategy, "LONG", rejected.executor.info, pd.Timestamp("2026-08-27T00:25:00Z"), "reject_test", "2026-08-27T00:20:00Z", "m231_reject_test")
        with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as handle:
            rejected_rows = list(csv.DictReader(handle))
        assert any(row["event"] == "open_reserved" and row["opportunity_id"] == "m231_reject_test" for row in rejected_rows)
        assert any(row["event"] == "entry_rejected_retry" and row["opportunity_id"] == "m231_reject_test" for row in rejected_rows)
        assert not any(row["event"] == "entry" and row["opportunity_id"] == "m231_reject_test" for row in rejected_rows)

        class AmbiguousOpenExecutor(FakeExecutor):
            def open_position(self, *_: Any, **__: Any) -> None:
                self.last_order_error = "NO_RESPONSE"
                return None

        ambiguous = S25Man231Runner(live_params)
        ambiguous.state = ambiguous._default_state()
        ambiguous._save_state = lambda: None
        ambiguous._suppress_manual_alerts = True
        ambiguous.executor = AmbiguousOpenExecutor()
        ambiguous._ensure_episode_identity(strategy)
        assert not ambiguous._open_position(strategy, "SHORT", ambiguous.executor.info, pd.Timestamp("2026-08-27T00:25:00Z"), "ambiguous_test", "2026-08-27T00:20:00Z", "m231_ambiguous_test")
        assert ambiguous._st(strategy)["pending_open"] is not None and ambiguous._st(strategy)["sync_block_reason"] == "ambiguous_open_result"
        with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as handle:
            ambiguous_rows = list(csv.DictReader(handle))
        assert any(row["event"] == "open_ambiguous" and row["opportunity_id"] == "m231_ambiguous_test" for row in ambiguous_rows)

        class MarketClosedExecutor(FakeExecutor):
            def close_position(self, *_: Any, **__: Any) -> CloseResult:
                return CloseResult(False, status="MARKET_CLOSED")

        market_position = SimpleNamespace(ticket=579, identifier=5579, symbol="XAUUSD", type=ORDER_TYPE_BUY, volume=0.01, open_price=4010.0, sl=0.0, tp=0.0, profit=0.0, magic=EXPECTED_S25_MAGIC, open_time=int(pd.Timestamp("2026-08-27T00:00:00Z").timestamp()), comment="s25_m231_L0579")
        market_closed = S25Man231Runner(live_params)
        market_closed.state = market_closed._default_state()
        market_closed._save_state = lambda: None
        market_closed._suppress_manual_alerts = True
        market_closed.executor = MarketClosedExecutor(positions=[market_position])
        market_state = market_closed._st(strategy)
        market_closed._ensure_episode_identity(strategy)
        market_state["positions"] = [market_closed._state_position_from_live(strategy, market_position)]
        assert market_closed._close_positions(strategy, list(market_state["positions"]), "market_closed_test", market_closed.executor.info, pd.Timestamp("2026-08-27T00:25:00Z"), None) == "market_closed"
        assert not market_state["positions"][0]["close_requested"] and market_state["close_defer"] is not None
        with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as handle:
            market_rows = list(csv.DictReader(handle))
        assert any(row["event"] == "DEFER" and row["reason"] == "market_closed" for row in market_rows)

        pending_flat = S25Man231Runner(live_params)
        pending_flat.state = pending_flat._default_state()
        pending_flat._save_state = lambda: None
        pending_flat._suppress_manual_alerts = True
        pending_flat.executor = FakeExecutor()
        pending_flat_state = pending_flat._st(strategy)
        pending_flat_state["pending_open"] = {"side": "SHORT", "lot": 0.01, "comment": "s25_m231_S0001", "reason": "bilateral_seed", "quote_time_utc": "2026-08-27T00:25:00+00:00", "known_position_ids": [], "flat_confirmation_count": 0}
        assert not pending_flat._sync_strategy(strategy)
        assert pending_flat._sync_strategy(strategy)
        assert pending_flat_state["pending_open"] is None

        live_close = S25Man231Runner(live_params)
        live_close.state = live_close._default_state()
        live_close._save_state = lambda: None
        live_close._suppress_manual_alerts = True
        live_owned = SimpleNamespace(ticket=580, identifier=5580, symbol="XAUUSD", type=ORDER_TYPE_BUY, volume=0.01, open_price=4010.0, sl=0.0, tp=0.0, profit=0.0, magic=EXPECTED_S25_MAGIC, open_time=int(pd.Timestamp("2026-08-27T00:00:00Z").timestamp()), comment="s25_m231_L0580")
        live_close.executor = FakeExecutor(positions=[live_owned])
        live_close.executor.next_deal = 15000
        live_close_state = live_close._st(strategy)
        live_close._ensure_episode_identity(strategy)
        live_close_state["positions"] = [live_close._state_position_from_live(strategy, live_owned)]
        assert live_close._close_positions(strategy, list(live_close_state["positions"]), "live_log_test", live_close.executor.info, pd.Timestamp("2026-08-27T00:25:00Z"), "2026-08-27T00:20:00Z") == "requested"
        with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as handle:
            request_rows = list(csv.DictReader(handle))
        reserved = next(row for row in reversed(request_rows) if row["event"] == "close_reserved" and row["ticket_set"] == "580")
        requested = next(row for row in reversed(request_rows) if row["event"] == "close_requested" and row["ticket_set"] == "580")
        assert reserved["opportunity_id"] == requested["opportunity_id"] == "m231_m5_20260827T002000Z"
        assert reserved["profit_basis"] == requested["profit_basis"] == "executable_mtm_before_close"
        assert parse_ts(requested["available_time"]) <= parse_ts(requested["decision_time"]) <= parse_ts(requested["executable_at"])
        assert live_close._sync_strategy(strategy)
        with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as handle:
            live_close_rows = list(csv.DictReader(handle))
        live_confirmed = next(row for row in reversed(live_close_rows) if row["event"] == "position_close_confirmed" and row["ticket"] == "580")
        assert live_confirmed["deal_id"] and live_confirmed["price_basis"] == "broker_close_deal"

        close_retry = S25Man231Runner(live_params)
        close_retry.state = close_retry._default_state()
        close_retry._save_state = lambda: None
        close_retry._suppress_manual_alerts = True
        close_owned = SimpleNamespace(ticket=601, identifier=6601, symbol="XAUUSD", type=ORDER_TYPE_BUY, volume=0.01, open_price=4020.18, sl=0.0, tp=0.0, profit=0.0, magic=EXPECTED_S25_MAGIC, open_time=int(pd.Timestamp("2026-08-27T00:25:00Z").timestamp()), comment="s25_m231_L0002")
        close_retry.executor = FakeExecutor(positions=[close_owned])
        close_retry_state = close_retry._st(strategy)
        retry_position = close_retry._state_position_from_live(strategy, close_owned)
        retry_position["close_requested"] = True
        close_retry_state["positions"] = [retry_position]
        close_retry_state["pending_close_reason"] = "test_close"
        assert close_retry._retry_pending_close_requests(strategy, pd.Timestamp("2026-08-27T00:25:00Z"))
        assert close_retry._sync_strategy(strategy)
        assert close_retry_state["positions"] == []

        two_phase = S25Man231Runner(live_params)
        two_phase.state = two_phase._default_state()
        two_phase._save_state = lambda: None
        two_phase._suppress_manual_alerts = True
        two_phase.executor = FakeExecutor()
        two_state = two_phase._st(strategy)
        two_phase._ensure_episode_identity(strategy)
        two_state["positions"] = [
            {"ticket": 701, "position_identifier": 7701, "side": "LONG", "lot": 0.01, "entry_price": 4010.0, "entry_time_utc": "2026-08-27T00:00:00+00:00", "open_time_epoch": int(pd.Timestamp("2026-08-27T00:00:00Z").timestamp()), "owner_symbol": "XAUUSD", "owner_magic": EXPECTED_S25_MAGIC, "owner_comment": "s25_m231_L0701", "shadow": False, "close_requested": True},
            {"ticket": 702, "position_identifier": 7702, "side": "SHORT", "lot": 0.01, "entry_price": 4030.0, "entry_time_utc": "2026-08-27T00:00:00+00:00", "open_time_epoch": int(pd.Timestamp("2026-08-27T00:00:00Z").timestamp()), "owner_symbol": "XAUUSD", "owner_magic": EXPECTED_S25_MAGIC, "owner_comment": "s25_m231_S0702", "shadow": False, "close_requested": True},
        ]
        two_state["pending_close_reason"] = "two_phase_test"
        two_state["pending_close_m5_bar"] = "2026-08-27T00:20:00+00:00"
        two_state["pending_close_requested_at_utc"] = "2026-08-27T00:25:00+00:00"
        def fake_deal(deal_id: int, position_id: int, price: float) -> Any:
            return SimpleNamespace(deal=deal_id, position_id=position_id, symbol="XAUUSD", magic=EXPECTED_S25_MAGIC, reason="EXPERT", price=price, profit=1.25, commission=-0.05, swap=-0.02, fee=0.0, net_profit=1.18, deal_time=int(pd.Timestamp("2026-08-27T00:25:01Z").timestamp()))
        two_phase.executor.deals[7701] = fake_deal(8701, 7701, 4021.0)
        before_rows = []
        with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as handle:
            before_rows = list(csv.DictReader(handle))
        assert not two_phase._sync_strategy(strategy)
        with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as handle:
            failed_rows = list(csv.DictReader(handle))
        assert not [row for row in failed_rows[len(before_rows):] if row["event"] == "position_close_confirmed"], "partial reconciliation must not emit realized profit"
        two_phase.executor.deals[7702] = fake_deal(8702, 7702, 4019.0)
        assert two_phase._sync_strategy(strategy)
        with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as handle:
            completed_rows = list(csv.DictReader(handle))
        confirmed = [row for row in completed_rows if row["event"] == "position_close_confirmed" and row["deal_id"] in {"8701", "8702"}]
        assert len(confirmed) == 2 and all(row["long_positions"] == "0" and row["short_positions"] == "0" for row in confirmed)
        assert all(abs(float(row["profit"]) - (float(row["gross_profit"]) + float(row["commission"]) + float(row["swap"]) + float(row["fee"]))) < 1e-12 for row in confirmed)
        assert all(row["price_basis"] == "broker_close_deal" and row["profit_basis"] == "broker_net_account_currency" and row["profit_currency"] == "USD" for row in confirmed)
        assert two_phase._sync_strategy(strategy)
        with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as handle:
            deduped_rows = list(csv.DictReader(handle))
        assert len([row for row in deduped_rows if row["event"] == "position_close_confirmed" and row["deal_id"] in {"8701", "8702"}]) == 2
    finally:
        if old_gate is None:
            os.environ.pop(str(params["real_trading_activation_env"]), None)
        else:
            os.environ[str(params["real_trading_activation_env"])] = old_gate

    bars = add_man231_features(FakeDM().get_historical_data())
    assert len(bars) == 230 and bars["ema200"].iloc[-1] == bars["ema200"].iloc[-1]


def main() -> int:
    global TRADE_LOG_FILE, STATE_FILE
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    os.makedirs(LOG_DIR, exist_ok=True)
    params = load_params()
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    if not args.self_test:
        rotating = RotatingFileHandler(
            LOG_FILE, maxBytes=int(params.get("bot_log_max_bytes", 10 * 1024 * 1024)),
            backupCount=int(params.get("bot_log_backup_count", 5)), encoding="utf-8",
        )
        rotating.setFormatter(formatter)
        root.addHandler(rotating)
    root.addHandler(stream)
    if args.self_test:
        original_trade_log = TRADE_LOG_FILE
        original_state_file = STATE_FILE
        with tempfile.TemporaryDirectory(prefix="s25-self-test-") as temp_dir:
            TRADE_LOG_FILE = os.path.join(temp_dir, "s25_trades.csv")
            STATE_FILE = os.path.join(temp_dir, "s25_bot_state.json")
            self_test()
        TRADE_LOG_FILE = original_trade_log
        STATE_FILE = original_state_file
        print("s25 man231 self-test ok")
        return 0
    runner = S25Man231Runner(params)
    if not runner.connect_and_preflight():
        return 1
    if args.once:
        runner.run_once()
        return 0
    while True:
        runner.run_once()
        time.sleep(float(params.get("poll_interval_seconds", 5)))


if __name__ == "__main__":
    raise SystemExit(main())
