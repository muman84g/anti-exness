# -*- coding: utf-8 -*-
"""S24 visual no-adverse C shadow/live runner.

Exact retryable OPEN no-fill outcomes retain an otherwise valid opportunity and
may retry only on a new broker quote at least 60 seconds later. Strategy signal,
sizing, add and exit parameters remain frozen. Non-retryable atomic OPEN guards
remain durable manual-reconciliation blocks after exact no-fill proof.
Passive shadow initialization and logging failures disable only that no-order
component; they do not interrupt core/v206 lifecycle management.
Unexpected lane exceptions retain valid durable v206 receipts, quarantine
malformed partial v206 state, and roll back partial no-order shadow mutations.
An OS-released singleton lock protects the shared bot24 state/order namespace
for the full live runner lifetime.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import logging
import math
import os
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from types import SimpleNamespace
from typing import Any

import pandas as pd

os.environ.setdefault("BOT_SUFFIX", "s24")

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
from live_safety import (
    LiveSafetyOptions,
    clean_sync_block_if_flat,
    clear_recoverable_sync_block_after_clean_sync,
    stale_signal_decision,
)
from live_manual_alerts import notify_manual_action_required
from shadow_opportunity_observer import ShadowOpportunityObserver
from shadow_state_tagger import ShadowStateTagger
from time_regime_wrapper import EVALUATED, NO_ACTIVE_REGIME, TimeRegimeRouter, TimeRegimeStrategyWrapper
from v206_live_lane import V206LiveLane, default_v206_state


UTC = timezone.utc
EXPECTED_S24_MAGIC = 200024
EXPECTED_BRIDGE_NAME = "BotBridge_s24"
EXPECTED_BRIDGE_VERSION = "2026-09-02-s24-core-atomic-v13"
FLAT_AUTO_CLEAR_SYNC_REASONS = {
    "open_success_position_not_confirmed",
    "unresolved_open_action",
    "live_time_close_failed",
    "live_time_close_unconfirmed",
}
CORE_RESOLVED_CLOSE_BLOCK_REASONS = {
    "live_time_close_failed",
    "live_time_close_unconfirmed",
    "close_response_identity_invalid",
    "position_absence_unconfirmed",
    "close_deal_query_unavailable",
    "close_deal_not_confirmed",
    "close_deal_ownership_mismatch",
    "market_closed_close_inventory_unconfirmed",
}
CORE_TIME_CLOSE_REASONS = {"failure_to_progress", "max_hold"}
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
STATE_DIR = os.path.join(SCRIPT_DIR, "state")
LOG_FILE = os.path.join(LOG_DIR, "s24_bot.log")
TRADE_LOG_FILE = os.path.join(LOG_DIR, "s24_trades.csv")
SHADOW_RUNNER_LOG_FILE = os.path.join(LOG_DIR, "s24_shadow_runner_trades.csv")
STATE_FILE = os.path.join(STATE_DIR, "s24_bot_state.json")
RUNNER_LOCK_FILE = os.path.join(STATE_DIR, "s24_runner.lock")
PARAMS_FILE = os.path.join(SCRIPT_DIR, "s24_params.json")

TRADE_FIELDS = [
    "timestamp_utc",
    "event",
    "strategy_id",
    "lane_id",
    "magic",
    "symbol",
    "mt5_symbol",
    "opportunity_id",
    "basket_id",
    "ticket",
    "position_identifier",
    "deal_id",
    "side",
    "lot",
    "entry_price",
    "exit_price",
    "price",
    "profit",
    "reason",
    "signal_bar_time",
    "event_time",
    "release_time",
    "available_time",
    "decision_time",
    "executable_at",
    "live",
    "repeat_count",
    "repeat_window_seconds",
    "note",
]
_CSV_SCHEMAS_VALIDATED: set[str] = set()
REPEATABLE_DIAGNOSTIC_REASONS = {
    "symbol_info_failed",
    "runtime_quote_clock_invalid",
    "positions_unavailable",
    "orders_unavailable",
    "m1_bars_unavailable",
    "close_deal_query_unavailable",
    "close_deal_not_confirmed",
}

SHADOW_RUNNER_FIELDS = [
    "timestamp_utc",
    "event",
    "lane_id",
    "opportunity_id",
    "strategy_id",
    "side",
    "position_count",
    "lot",
    "price",
    "pnl",
    "reason",
    "signal_bar_time",
    "entry_time_utc",
    "note",
]


def utc_now() -> datetime:
    return datetime.now(UTC)


def dt_text(value: datetime | pd.Timestamp) -> str:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC").isoformat()


def parse_ts(value: Any) -> pd.Timestamp | None:
    if not value:
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


def _strict_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"nonfinite JSON constant: {value}")


def strict_json_load(handle: Any) -> Any:
    return json.load(handle, object_pairs_hook=_strict_json_pairs, parse_constant=_reject_json_constant)


def _fsync_parent_directory(path: str) -> None:
    if os.name != "posix":
        return
    parent = os.path.dirname(os.path.abspath(path)) or "."
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0))
    directory_fd = os.open(parent, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        _fsync_parent_directory(path)
    except Exception:
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass
        raise


def acquire_runner_singleton_lock(lock_file: str | None = None) -> Any | None:
    """Hold an OS-released lock for the complete bot24 runner lifetime."""

    path = lock_file or RUNNER_LOCK_FILE
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    handle = open(path, "a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
        os.fsync(handle.fileno())
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        handle.close()
        return None
    return handle


def _execution_csv_row_error(row: dict[str, Any]) -> str | None:
    required_text = ("event", "strategy_id", "symbol", "mt5_symbol")
    for name in required_text:
        if not isinstance(row.get(name), str) or not str(row.get(name)).strip():
            return f"{name}_missing"
    timestamp_text = row.get("timestamp_utc")
    if not isinstance(timestamp_text, str) or not timestamp_text.strip():
        return "timestamp_utc_missing"
    try:
        timestamp = pd.Timestamp(timestamp_text)
    except Exception:
        return "timestamp_utc_invalid"
    if timestamp.tzinfo is None or timestamp.utcoffset() is None or timestamp.utcoffset().total_seconds() != 0:
        return "timestamp_utc_not_utc"
    signal_bar_text = row.get("signal_bar_time")
    if signal_bar_text not in (None, ""):
        try:
            signal_bar = pd.Timestamp(str(signal_bar_text))
        except Exception:
            return "signal_bar_time_invalid"
        if signal_bar.tzinfo is None or signal_bar.utcoffset() is None:
            return "signal_bar_time_timezone_missing"
    if str(row.get("live", "")).strip().lower() not in {"true", "false"}:
        return "live_invalid"
    side = str(row.get("side") or "").strip()
    if side and side not in {"LONG", "SHORT"}:
        return "side_invalid"
    ticket = str(row.get("ticket") or "").strip()
    if ticket:
        try:
            if int(ticket) <= 0:
                return "ticket_invalid"
        except (TypeError, ValueError, OverflowError):
            return "ticket_invalid"
    for name in ("lane_id", "magic", "ticket", "position_identifier", "deal_id", "repeat_count"):
        text = str(row.get(name) or "").strip()
        if not text:
            if name in {"lane_id", "magic"}:
                return f"{name}_missing"
            continue
        try:
            value = int(text)
        except (TypeError, ValueError, OverflowError):
            return f"{name}_invalid"
        if value <= 0 and name != "repeat_count":
            return f"{name}_nonpositive"
        if name == "repeat_count" and value < 0:
            return "repeat_count_negative"
    for name in ("lot", "entry_price", "exit_price", "price", "profit", "repeat_window_seconds"):
        text = str(row.get(name) or "").strip()
        if not text:
            continue
        try:
            value = float(text)
        except (TypeError, ValueError, OverflowError):
            return f"{name}_invalid"
        if not math.isfinite(value):
            return f"{name}_nonfinite"
        if name in {"lot", "entry_price", "exit_price", "price"} and value <= 0.0:
            return f"{name}_nonpositive"
        if name == "repeat_window_seconds" and value < 0.0:
            return "repeat_window_seconds_negative"
    for name in ("event_time", "release_time", "available_time", "decision_time", "executable_at"):
        text = str(row.get(name) or "").strip()
        if not text:
            continue
        try:
            value = pd.Timestamp(text)
        except Exception:
            return f"{name}_invalid"
        if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
            return f"{name}_not_utc"
    return None


def _validate_csv_stream(existing_file: Any, path: str, fields: list[str]) -> None:
    try:
        existing_file.seek(0)
        reader = csv.reader(existing_file, strict=True)
        observed_fields = next(reader, [])
        if observed_fields != fields:
            raise RuntimeError(f"CSV schema mismatch for {path}; archive/reset the old bot24 CSV before starting")
        for row_number, observed_row in enumerate(reader, start=2):
            if len(observed_row) != len(fields):
                raise RuntimeError(
                    f"CSV data row width mismatch for {path} at row {row_number}: "
                    f"expected={len(fields)} observed={len(observed_row)}; "
                    "archive/reset the old bot24 CSV before starting"
                )
            if fields == TRADE_FIELDS:
                row_error = _execution_csv_row_error(dict(zip(fields, observed_row)))
                if row_error is not None:
                    raise RuntimeError(
                        f"CSV execution row invalid for {path} at row {row_number}: {row_error}; "
                        "archive/reset the old bot24 CSV before starting"
                    )
    except csv.Error as exc:
        raise RuntimeError(
            f"CSV parse failure for {path}: {exc}; archive/reset the old bot24 CSV before starting"
        ) from exc


def _validate_csv_contents(path: str, fields: list[str]) -> None:
    with open(path, "r", newline="", encoding="utf-8") as existing_file:
        _validate_csv_stream(existing_file, path, fields)


def append_csv(path: str, row: dict[str, Any], fields: list[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    path_key = os.path.normcase(os.path.abspath(path))
    exists = os.path.exists(path)
    if not exists and path_key in _CSV_SCHEMAS_VALIDATED:
        raise RuntimeError(
            f"CSV disappeared after validation for {path}; archive/reset the old bot24 CSV before starting"
        )
    if exists and os.path.getsize(path) == 0:
        raise RuntimeError(
            f"CSV existing file is empty for {path}; archive/reset the old bot24 CSV before starting"
        )
    pending_row = {name: row.get(name, "") for name in fields}
    if not exists:
        if fields == TRADE_FIELDS:
            row_error = _execution_csv_row_error(pending_row)
            if row_error is not None:
                raise RuntimeError(f"CSV execution row invalid before append for {path}: {row_error}")
        try:
            output_file = open(path, "x", newline="", encoding="utf-8")
        except FileExistsError as exc:
            raise RuntimeError(
                f"CSV path was created concurrently for {path}; refusing unvalidated append"
            ) from exc
        with output_file as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerow(pending_row)
            if fields == TRADE_FIELDS:
                f.flush()
                os.fsync(f.fileno())
        _CSV_SCHEMAS_VALIDATED.add(path_key)
        if fields == TRADE_FIELDS:
            _fsync_parent_directory(path)
        return
    try:
        output_file = open(path, "r+", newline="", encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"CSV disappeared during append for {path}; refusing recreation"
        ) from exc
    with output_file as f:
        before_stat = os.fstat(f.fileno())
        _validate_csv_stream(f, path, fields)
        after_stat = os.fstat(f.fileno())
        if (before_stat.st_size, before_stat.st_mtime_ns) != (after_stat.st_size, after_stat.st_mtime_ns):
            raise RuntimeError(f"CSV changed during validation for {path}; refusing append")
        try:
            path_stat = os.stat(path)
        except FileNotFoundError as exc:
            raise RuntimeError(f"CSV disappeared during validation for {path}; refusing append") from exc
        if (before_stat.st_dev, before_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
            raise RuntimeError(f"CSV was replaced during validation for {path}; refusing append")
        if fields == TRADE_FIELDS:
            row_error = _execution_csv_row_error(pending_row)
            if row_error is not None:
                raise RuntimeError(f"CSV execution row invalid before append for {path}: {row_error}")
        f.seek(0, os.SEEK_END)
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writerow(pending_row)
        if fields == TRADE_FIELDS:
            f.flush()
            os.fsync(f.fileno())
    _CSV_SCHEMAS_VALIDATED.add(path_key)


def validate_csv_schema(path: str, fields: list[str]) -> None:
    if not os.path.exists(path):
        return
    if os.path.getsize(path) == 0:
        raise RuntimeError(
            f"CSV existing file is empty for {path}; archive/reset the old bot24 CSV before starting"
        )
    _validate_csv_contents(path, fields)
    _CSV_SCHEMAS_VALIDATED.add(os.path.normcase(os.path.abspath(path)))


def normalize_price(value: float, digits: int) -> float:
    return round(float(value), int(digits))


def add_features(bars: pd.DataFrame, point_size: float) -> pd.DataFrame:
    out = bars.copy()
    high = out["High"].astype(float)
    low = out["Low"].astype(float)
    close = out["Close"].astype(float)
    tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
    out["atr30"] = tr.rolling(30, min_periods=30).mean()
    out["atr90"] = tr.rolling(90, min_periods=90).mean()
    out["vol_ratio"] = out["atr30"] / out["atr90"]
    out["ret5"] = close - close.shift(5)
    out["ret10"] = close - close.shift(10)
    out["roll_high30"] = high.shift(1).rolling(30, min_periods=30).max()
    out["roll_low30"] = low.shift(1).rolling(30, min_periods=30).min()
    out["spread_points"] = ((out.get("AskOpen", out["Open"]) - out["Open"]) / point_size).clip(lower=0.0)
    return out


def in_session(ts: pd.Timestamp, start: int, end: int) -> bool:
    hour = int(ts.hour)
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


class S24NoAdverseRunner:
    def __init__(self, params: dict[str, Any]):
        self.params = params
        config_error = self._config_error(params)
        if config_error is not None:
            raise ValueError(f"invalid frozen bot24 configuration: {config_error}")
        self.live_enabled = bool(params.get("live_trading_enabled", False))
        self.shadow_enabled = bool(params.get("shadow_forward_enabled", True))
        self._suppress_manual_alerts = bool(params.get("_suppress_manual_alerts", False))
        self.safety = LiveSafetyOptions(**params.get("safety", {}))
        self.entry_router, self.entry_wrapper = self._build_entry_wrapper()
        self.dm = MT5DataManager(self.safety)
        self.executor = MT5Executor()
        self._diagnostic_repeats: dict[int, dict[str, Any]] = {}
        self._fatal_state_identity_mismatch = False
        self.state = self._load_state()
        self.v206_lane = V206LiveLane(self)
        runner_cfg = dict(params.get("runner_shadow") or {})
        self.passive_shadow_init_errors: dict[str, dict[str, str]] = {}
        self.passive_shadow_runner_enabled = bool(runner_cfg.get("enabled", False))
        observer_cfg = dict(runner_cfg.get("opportunity_observer") or {})
        try:
            self.shadow_observer = ShadowOpportunityObserver(
                observer_cfg,
                log_dir=LOG_DIR,
                state_dir=STATE_DIR,
                symbol=str(params["symbol"]),
                contract_size=params.get("contract_size", 100.0),
                lot=runner_cfg.get("lot", params.get("default_lot", 0.01)),
            )
        except Exception as exc:
            logging.exception("S24 passive shadow observer initialization failed; observer disabled and core execution retained")
            self.passive_shadow_init_errors["opportunity_observer"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
            # Do not retry construction with the malformed passive options or
            # evidence paths. A disabled observer is an inert no-order object.
            observer_cfg = {"enabled": False}
            self.shadow_observer = ShadowOpportunityObserver(
                observer_cfg,
                log_dir=LOG_DIR,
                state_dir=STATE_DIR,
                symbol=str(params["symbol"]),
                contract_size=100.0,
                lot=0.01,
            )
        tagger_cfg = dict(runner_cfg.get("state_tagger") or {})
        try:
            self.shadow_state_tagger = ShadowStateTagger(
                tagger_cfg,
                log_dir=LOG_DIR,
                symbol=str(params["symbol"]),
            )
        except Exception as exc:
            logging.exception("S24 passive shadow tagger initialization failed; tagger disabled and core execution retained")
            self.passive_shadow_init_errors["state_tagger"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
            tagger_cfg = {"enabled": False}
            self.shadow_state_tagger = ShadowStateTagger(
                tagger_cfg,
                log_dir=LOG_DIR,
                symbol=str(params["symbol"]),
            )
        self._last_status_log = 0.0

    @staticmethod
    def _config_error(params: dict[str, Any]) -> str | None:
        for key in ("enabled", "live_trading_enabled", "shadow_forward_enabled"):
            if not isinstance(params.get(key), bool):
                return f"{key}_not_boolean"
        if bool(params["enabled"]) and bool(params["live_trading_enabled"]) and bool(params["shadow_forward_enabled"]):
            return "execution_mode_contract"
        runner_cfg = params.get("runner_shadow")
        if not isinstance(runner_cfg, dict):
            return "runner_shadow_shape"
        if not isinstance(runner_cfg.get("enabled"), bool):
            return "runner_shadow.enabled_not_boolean"
        if runner_cfg.get("execution_mode") != "shadow":
            return "runner_shadow.execution_mode_contract"
        for component in ("opportunity_observer", "state_tagger"):
            component_cfg = runner_cfg.get(component)
            if not isinstance(component_cfg, dict):
                return f"runner_shadow.{component}_shape"
            if not isinstance(component_cfg.get("enabled"), bool):
                return f"runner_shadow.{component}.enabled_not_boolean"
        observer_cfg = runner_cfg["opportunity_observer"]
        expected_observer_artifacts = {
            "opportunity_csv": "s24_shadow_opportunities.csv",
            "markout_csv": "s24_shadow_markouts.csv",
            "state_file": "s24_shadow_observer_state.json",
        }
        for key, expected in expected_observer_artifacts.items():
            if observer_cfg.get(key) != expected:
                return f"runner_shadow.opportunity_observer.{key}_contract"
        if runner_cfg["state_tagger"].get("csv") != "s24_shadow_state_tags.csv":
            return "runner_shadow.state_tagger.csv_contract"
        exact_global = {
            "bot_number": "24", "bot_suffix": "s24", "strategy_id": "bot24_visual_no_adverse_c_target16",
            "expected_magic": 200024, "expected_bridge_name": EXPECTED_BRIDGE_NAME,
            "expected_bridge_version": EXPECTED_BRIDGE_VERSION,
            "symbol": "XAUUSD", "mt5_symbol": "XAUUSD", "require_hedging_account": True,
            "default_lot": 0.01, "contract_size": 100.0,
            "poll_interval_seconds": 5, "status_log_interval_seconds": 60,
            "diagnostic_repeat_summary_seconds": 300,
            "bot_log_max_bytes": 10 * 1024 * 1024, "bot_log_backup_count": 5,
            "broker_timezone": "UTC", "hist_timestamp_basis": "unix_seconds_utc",
            "m1_timeframe": 1, "m1_bars": 360, "drop_latest_m1_bar": True,
            "max_signal_delay_minutes": 2,
            "point_size": 0.001, "price_digits": 3, "max_entry_spread_points": 300.0,
            "deviation_points": 50, "time_close_max_spread_points": 300.0,
            "time_close_stable_quotes": 3, "time_close_force_after_minutes": 30.0,
            "time_close_market_closed_retry_seconds": 60.0,
        }
        for key, expected in exact_global.items():
            observed = params.get(key)
            if isinstance(expected, float):
                if isinstance(observed, bool) or not isinstance(observed, (int, float)) or not math.isclose(float(observed), expected, rel_tol=0.0, abs_tol=1e-12):
                    return f"{key}={observed!r}"
            elif observed != expected:
                return f"{key}={observed!r}"
        expected_safety = {
            "hist_timestamps_are_utc": True, "stale_signal_guard": True,
            "preflight_clean_sync": True, "periodic_clean_sync": True,
            "clear_recoverable_sync_block": True, "save_state_after_clear": True,
            "broker_sl_residual_clear": False, "audit_log": True,
        }
        if params.get("safety") != expected_safety:
            return "safety_contract"
        expected_routing = {
            "enabled": True,
            "regimes": [{
                "id": "utc_1300_1800_current", "timezone": "UTC",
                "start_local": "13:00", "end_local": "18:00",
                "strategy_ids": ["visual_no_adverse_c_target16"],
                "weekdays": [0, 1, 2, 3, 4, 5, 6], "enabled": True,
            }],
        }
        if params.get("entry_time_routing") != expected_routing:
            return "entry_time_routing_contract"
        strategies = params.get("strategies")
        if not isinstance(strategies, list) or len(strategies) != 1:
            return "strategies_shape"
        expected_strategy = {
            "enabled": True, "id": "visual_no_adverse_c_target16", "spec_id": "visual_no_adverse_c:target16",
            "magic": 200024, "comment_prefix": "s24_no_adverse", "lot": 0.01,
            "session_start_utc": 13, "session_end_utc": 18, "mode": "breakout_impulse",
            "impulse_bars": 10, "impulse_atr": 0.60, "add_atr": 0.45, "max_positions": 8,
            "basket_target_usd": 16.0, "basket_stop_usd": 48.0, "max_hold_bars": 120,
            "exit_clock": "confirmed_m1", "cooldown": 3, "vol_min": 1.05,
            "failure_to_progress_bars": 0, "failure_to_progress_peak_usd": 0.0, "reverse_on_fail": False,
        }
        strategy = strategies[0]
        for key, expected in expected_strategy.items():
            observed = strategy.get(key)
            if isinstance(expected, float):
                if isinstance(observed, bool) or not isinstance(observed, (int, float)) or not math.isclose(float(observed), expected, rel_tol=0.0, abs_tol=1e-12):
                    return f"strategy.{key}={observed!r}"
            elif observed != expected:
                return f"strategy.{key}={observed!r}"
        return None

    def _build_entry_wrapper(self) -> tuple[TimeRegimeRouter, TimeRegimeStrategyWrapper]:
        routing = self.params.get("entry_time_routing")
        if not isinstance(routing, dict) or not bool(routing.get("enabled", False)):
            raise RuntimeError("bot24 entry_time_routing must be explicitly enabled")
        raw_regimes = routing.get("regimes")
        if not isinstance(raw_regimes, list) or not raw_regimes:
            raise RuntimeError("bot24 entry_time_routing.regimes must be a non-empty list")
        router = TimeRegimeRouter.from_mappings(raw_regimes)
        enabled_strategies = {
            str(strat["id"]): strat
            for strat in self.params.get("strategies", [])
            if bool(strat.get("enabled", True))
        }
        routed_ids = {
            strategy_id
            for regime in router.regimes
            if regime.enabled
            for strategy_id in regime.strategy_ids
        }
        if routed_ids != set(enabled_strategies):
            raise RuntimeError(
                "bot24 entry routing identity mismatch: "
                f"routed={sorted(routed_ids)} enabled={sorted(enabled_strategies)}"
            )
        adapters = {
            strategy_id: (
                lambda context, strat=strat: self._strategy_signal_decision(context["row"], strat)
            )
            for strategy_id, strat in enabled_strategies.items()
        }
        return router, TimeRegimeStrategyWrapper(router, adapters)

    @staticmethod
    def _quote_time_utc(info: Any) -> datetime:
        quote_time_msc = getattr(info, "quote_time_msc", None)
        if quote_time_msc is None:
            return utc_now()
        try:
            value = int(quote_time_msc)
        except (TypeError, ValueError, OverflowError):
            return utc_now()
        if value <= 0:
            return utc_now()
        return datetime.fromtimestamp(value / 1000.0, UTC)

    def _runtime_info_clock_error(self, info: Any) -> str | None:
        """Validate every executable INFO payload before any strategy advances."""

        try:
            quote_msc = int(getattr(info, "quote_time_msc"))
            if quote_msc <= 0:
                return "missing_quote_time"
            quote_time = pd.Timestamp(quote_msc, unit="ms", tz="UTC")
        except (AttributeError, TypeError, ValueError, OverflowError):
            return "invalid_quote_time"
        host_now = pd.Timestamp(utc_now())
        tolerance = pd.Timedelta(minutes=float(self.params.get("max_signal_delay_minutes", 2.0)))
        if quote_time > host_now + pd.Timedelta(seconds=10):
            return "future_quote_time"
        if host_now - quote_time > tolerance:
            return "stale_quote_time"
        previous_times = [
            parse_ts(self._st(strat).get("last_core_quote_time_utc"))
            for strat in self.params.get("strategies", [])
            if bool(strat.get("enabled", True))
        ]
        previous_times.append(parse_ts((self.state.get("v206") or {}).get("last_quote_time_utc")))
        if any(previous is not None and quote_time < previous for previous in previous_times):
            return "nonmonotonic_quote_time"
        return None

    def _validated_core_quote_time(self, strat: dict[str, Any], info: Any) -> tuple[pd.Timestamp | None, str | None]:
        try:
            quote_msc = int(getattr(info, "quote_time_msc"))
            if quote_msc <= 0:
                return None, "missing_quote_time"
            quote_time = pd.Timestamp(quote_msc, unit="ms", tz="UTC")
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None, "invalid_quote_time"
        host_now = pd.Timestamp(utc_now())
        tolerance = pd.Timedelta(minutes=float(self.params.get("max_signal_delay_minutes", 2.0)))
        if quote_time > host_now + pd.Timedelta(seconds=10):
            return None, "future_quote_time"
        if host_now - quote_time > tolerance:
            return None, "stale_quote_time"
        previous = parse_ts(self._st(strat).get("last_core_quote_time_utc"))
        if previous is not None and quote_time < previous:
            return None, "nonmonotonic_quote_time"
        return quote_time, None

    def _core_broker_contract_error(self, strat: dict[str, Any], info: Any) -> str | None:
        try:
            lot = float(strat.get("lot", self.params.get("default_lot", 0.01)))
            volume_min = float(info.volume_min)
            volume_max = float(info.volume_max)
            volume_step = float(info.volume_step)
            point = float(info.point)
            digits = int(info.digits)
            values = (lot, volume_min, volume_max, volume_step, point)
            if not all(math.isfinite(value) for value in values):
                return "nonfinite_symbol_contract"
            if volume_min <= 0.0 or volume_max < volume_min or volume_step <= 0.0:
                return "invalid_volume_contract"
            if lot < volume_min - 1e-12 or lot > volume_max + 1e-12:
                return "lot_out_of_range"
            steps = (lot - volume_min) / volume_step
            if not math.isclose(steps, round(steps), rel_tol=0.0, abs_tol=1e-9):
                return "lot_off_step"
            if digits != int(self.params.get("price_digits", 3)):
                return "digits_mismatch"
            if not math.isclose(point, float(self.params.get("point_size", 0.001)), rel_tol=0.0, abs_tol=1e-12):
                return "point_mismatch"
        except (AttributeError, TypeError, ValueError, OverflowError):
            return "symbol_contract_unavailable"
        return None

    def _contain_v206_poll_exception(
        self,
        snapshot: dict[str, Any],
        *,
        reason: str,
        exc: Exception,
    ) -> None:
        """Retain valid receipts, but quarantine malformed partial lane state."""

        candidate = copy.deepcopy(self.state.get("v206"))
        try:
            candidate_error = self.v206_lane._state_shape_error(candidate)
        except (TypeError, ValueError, OverflowError):
            candidate_error = "value_parse_error"
        if candidate_error is None and isinstance(candidate, dict):
            contained = candidate
        else:
            contained = copy.deepcopy(snapshot)
            contained["quarantined_state_snapshot"] = candidate if isinstance(candidate, dict) else {
                "invalid_type": type(candidate).__name__,
            }
        contained["blocked_reason"] = reason
        contained["blocked_details"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            **(
                {"partial_state_error": candidate_error, "quarantined": True}
                if candidate_error is not None
                else {}
            ),
        }
        self.state["v206"] = contained

    def _default_state(self) -> dict[str, Any]:
        return {
            "version": 2,
            "bot": "bot24",
            "strategy_id": self.params["strategy_id"],
            "last_saved_utc": None,
            "quarantined_strategy_states": {},
            "quarantined_shadow_runner_states": {},
            "strategies": {
                s["id"]: {
                    "basket": [],
                    "cooldown_until_bar": -1,
                    "last_add_price": None,
                    "last_signal_bar": None,
                    "last_closed_at_utc": None,
                    "last_closed_side": None,
                    "last_closed_reason": None,
                    "last_closed_signal_bar": None,
                    "last_closed_entry_signal_bars": [],
                    "position_signal_identity_required": True,
                    "last_consumed_signal_bar": None,
                    "reverse_used": False,
                    "sync_block_new_entries": False,
                    "sync_block_reason": None,
                    "sync_block_recoverable": False,
                    "sync_block_details": {},
                    "flat_clear_confirmation_count": 0,
                    "flat_clear_confirmation_reason": None,
                    "basket_peak_pnl_usd": None,
                    "pending_close_reason": None,
                    "pending_close_signal_bar": None,
                    "cooldown_until_utc": None,
                    "last_evaluated_bar": None,
                    "last_decision": None,
                    "last_exit_evaluated_bar": None,
                    "pending_open_opportunity_id": None,
                    "pending_open_started_utc": None,
                    "entry_retry_after_utc": None,
                    "entry_retry_signal_bar": None,
                    "entry_retry_reason": None,
                    "entry_permission_reject_count": 0,
                    "last_core_quote_time_utc": None,
                    "close_retry_after_utc": None,
                    "close_permission_reject_count": 0,
                    "time_close_defer_started_utc": None,
                    "time_close_last_quote_msc": None,
                    "time_close_stable_count": 0,
                    "time_close_wide_seen": False,
                    "manual_alert_last_signature": None,
                    "manual_alert_last_reason": None,
                    "manual_alert_last_at_utc": None,
                    "shadow_runner": {
                        "basket": [],
                        "last_add_price": None,
                        "last_evaluated_bar": None,
                        "last_exit_evaluated_bar": None,
                        "basket_peak_pnl_usd": None,
                        "last_closed_at_utc": None,
                        "last_closed_reason": None,
                        "cooldown_until_utc": None,
                    },
                }
                for s in self.params["strategies"]
            },
            "v206": default_v206_state(),
        }

    def _core_state_shape_error(self, strat: dict[str, Any], state: Any) -> str | None:
        if not isinstance(state, dict):
            return "not_object"
        basket = state.get("basket")
        if not isinstance(basket, list) or len(basket) > int(strat.get("max_positions", 0)):
            return "basket_shape"
        signal_identity_required = state.get("position_signal_identity_required")
        if not isinstance(signal_identity_required, bool):
            return "position_signal_identity_required_invalid"
        try:
            basket_modes: set[bool] = set()
            basket_sides: set[str] = set()
            for row in basket:
                if not isinstance(row, dict):
                    return "basket_row_shape"
                comment = str(row.get("owner_comment") or "")
                shadow = row.get("shadow")
                if not isinstance(shadow, bool):
                    return "basket_shadow_mode_invalid"
                basket_modes.add(shadow)
                basket_sides.add(str(row.get("side")))
                broker_identity_invalid = (
                    not shadow
                    and (
                        isinstance(row.get("ticket"), bool)
                        or not isinstance(row.get("ticket"), int)
                        or int(row.get("ticket") or 0) <= 0
                        or isinstance(row.get("position_identifier"), bool)
                        or not isinstance(row.get("position_identifier"), int)
                        or int(row.get("position_identifier") or 0) <= 0
                        or isinstance(row.get("open_time_epoch"), bool)
                        or not isinstance(row.get("open_time_epoch"), int)
                        or int(row.get("open_time_epoch") or 0) <= 0
                    )
                )
                shadow_identity_invalid = shadow and (
                    row.get("ticket") not in (None, 0)
                    or int(row.get("position_identifier") or 0) != 0
                    or int(row.get("open_time_epoch") or 0) != 0
                )
                if (
                    broker_identity_invalid
                    or shadow_identity_invalid
                    or row.get("side") not in {"LONG", "SHORT"}
                    or isinstance(row.get("lot"), bool)
                    or not math.isclose(float(row.get("lot") or 0.0), float(strat.get("lot", self.params.get("default_lot", 0.01))), rel_tol=0.0, abs_tol=1e-12)
                    or isinstance(row.get("entry_price"), bool)
                    or not math.isfinite(float(row.get("entry_price") or 0.0))
                    or float(row.get("entry_price") or 0.0) <= 0.0
                    or not isinstance(row.get("entry_time_utc"), str)
                    or parse_ts(row.get("entry_time_utc")) is None
                    or (
                        not isinstance(row.get("signal_bar_time"), str) or parse_ts(row.get("signal_bar_time")) is None
                        if signal_identity_required
                        else row.get("signal_bar_time") is not None and (
                            not isinstance(row.get("signal_bar_time"), str)
                            or parse_ts(row.get("signal_bar_time")) is None
                        )
                    )
                    or row.get("owner_symbol") != str(self.params.get("mt5_symbol", self.params["symbol"]))
                    or isinstance(row.get("owner_magic"), bool)
                    or not isinstance(row.get("owner_magic"), int)
                    or int(row.get("owner_magic") or 0) != int(strat["magic"])
                    or not self._owned_comment(strat["comment_prefix"], comment)
                    or not isinstance(row.get("close_requested", False), bool)
                    or (
                        row.get("close_submission_started_utc") is not None
                        and (
                            not isinstance(row.get("close_submission_started_utc"), str)
                            or parse_ts(row.get("close_submission_started_utc")) is None
                        )
                    )
                ):
                    return "basket_row_invalid"
            if len(basket_modes) > 1:
                return "basket_execution_mode_mixed"
            if len(basket_sides) > 1:
                return "basket_side_mixed"
            if not signal_identity_required and (
                not basket or all(parse_ts(row.get("signal_bar_time")) is not None for row in basket)
            ):
                return "legacy_position_signal_identity_marker_invalid"
        except (TypeError, ValueError, OverflowError):
            return "basket_row_value_invalid"
        for key in ("sync_block_new_entries", "sync_block_recoverable", "time_close_wide_seen"):
            if not isinstance(state.get(key), bool):
                return f"{key}_invalid"
        if not isinstance(state.get("reverse_used"), bool):
            return "reverse_used_invalid"
        cooldown_bar = state.get("cooldown_until_bar")
        if isinstance(cooldown_bar, bool) or not isinstance(cooldown_bar, int) or cooldown_bar < -1:
            return "cooldown_until_bar_invalid"
        if state.get("sync_block_reason") is not None and not isinstance(state.get("sync_block_reason"), str):
            return "sync_block_reason_invalid"
        if not isinstance(state.get("sync_block_details"), dict):
            return "sync_block_details_invalid"
        for key in ("flat_clear_confirmation_count", "close_permission_reject_count", "entry_permission_reject_count", "time_close_stable_count"):
            value = state.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return f"{key}_invalid"
        for key in (
            "last_closed_at_utc", "last_closed_signal_bar", "last_consumed_signal_bar", "cooldown_until_utc", "last_evaluated_bar",
            "last_exit_evaluated_bar", "pending_open_started_utc", "last_core_quote_time_utc",
            "entry_retry_after_utc", "entry_retry_signal_bar", "close_retry_after_utc", "time_close_defer_started_utc", "last_signal_bar",
            "manual_alert_last_at_utc",
        ):
            if state.get(key) not in (None, "") and (
                not isinstance(state.get(key), str) or parse_ts(state.get(key)) is None
            ):
                return f"{key}_invalid"
        for key in (
            "last_closed_reason", "flat_clear_confirmation_reason", "manual_alert_last_signature",
            "manual_alert_last_reason", "entry_retry_reason",
        ):
            if state.get(key) is not None and not isinstance(state.get(key), str):
                return f"{key}_invalid"
        close_at = parse_ts(state.get("last_closed_at_utc"))
        close_side = state.get("last_closed_side")
        close_reason = state.get("last_closed_reason")
        closed_entry_signal_bars = state.get("last_closed_entry_signal_bars")
        if (
            not isinstance(closed_entry_signal_bars, list)
            or len(closed_entry_signal_bars) > int(strat.get("max_positions", 0))
            or any(not isinstance(value, str) for value in closed_entry_signal_bars)
            or len(closed_entry_signal_bars) != len(set(closed_entry_signal_bars))
            or any(parse_ts(value) is None for value in closed_entry_signal_bars)
        ):
            return "last_closed_entry_signal_bars_invalid"
        close_identity_present = any(
            value not in (None, "")
            for value in (state.get("last_closed_at_utc"), close_side, close_reason)
        )
        if close_identity_present and (
            close_at is None
            or close_side not in {"LONG", "SHORT"}
            or not isinstance(close_reason, str)
            or not close_reason
        ):
            return "last_closed_identity_invalid"
        if not close_identity_present and any(
            value not in (None, "")
            for value in (state.get("last_closed_signal_bar"), state.get("last_consumed_signal_bar"))
        ):
            return "last_closed_identity_invalid"
        if not close_identity_present and closed_entry_signal_bars:
            return "last_closed_identity_invalid"
        decision = state.get("last_decision")
        if decision is not None and (
            not isinstance(decision, dict)
            or not isinstance(decision.get("signal_bar_time"), str)
            or parse_ts(decision.get("signal_bar_time")) is None
            or not isinstance(decision.get("outcome"), str)
            or decision.get("outcome") not in {"signal", "no_signal", "not_evaluated", "not_evaluated_data_unavailable"}
            or not isinstance(decision.get("reason"), str)
            or (
                decision.get("side") is not None
                and (not isinstance(decision.get("side"), str) or decision.get("side") not in {"LONG", "SHORT"})
            )
        ):
            return "last_decision_invalid"
        pending_id = state.get("pending_open_opportunity_id")
        pending_started = state.get("pending_open_started_utc")
        if bool(pending_id) != bool(pending_started) or (pending_id is not None and not isinstance(pending_id, str)):
            return "pending_open_identity_invalid"
        retry_after = parse_ts(state.get("entry_retry_after_utc"))
        retry_bar = parse_ts(state.get("entry_retry_signal_bar"))
        retry_reason = state.get("entry_retry_reason")
        if (
            bool(retry_after) != bool(retry_bar)
            or bool(retry_after) != bool(retry_reason)
            or retry_reason not in {None, "market_closed", "trade_permission"}
        ):
            return "entry_retry_identity_invalid"
        pending_close_reason = state.get("pending_close_reason")
        pending_close_bar = state.get("pending_close_signal_bar")
        if pending_close_reason is not None and (
            not isinstance(pending_close_reason, str)
            or pending_close_reason not in {"basket_target", "basket_stop", "failure_to_progress", "max_hold"}
        ):
            return "pending_close_reason_invalid"
        if bool(pending_close_reason) != bool(pending_close_bar) or (
            pending_close_bar is not None and parse_ts(pending_close_bar) is None
        ):
            return "pending_close_identity_invalid"
        has_close_receipt = any(
            isinstance(row, dict)
            and (
                row.get("close_requested") is True
                or row.get("close_submission_started_utc") is not None
            )
            for row in basket
        )
        if has_close_receipt and pending_close_reason is None:
            return "close_receipt_without_intent"
        if pending_close_reason is not None and not basket:
            return "pending_close_without_basket"
        last_add_price = state.get("last_add_price")
        if last_add_price is not None:
            try:
                if isinstance(last_add_price, bool) or not math.isfinite(float(last_add_price)) or float(last_add_price) <= 0.0:
                    return "last_add_price_invalid"
            except (TypeError, ValueError, OverflowError):
                return "last_add_price_invalid"
        last_quote_msc = state.get("time_close_last_quote_msc")
        if last_quote_msc is not None and (isinstance(last_quote_msc, bool) or not isinstance(last_quote_msc, int) or last_quote_msc <= 0):
            return "time_close_last_quote_msc_invalid"
        defer_started = parse_ts(state.get("time_close_defer_started_utc"))
        if (state.get("time_close_wide_seen") and defer_started is None) or (
            not state.get("time_close_wide_seen") and (defer_started is not None or state.get("time_close_stable_count") != 0)
        ):
            return "time_close_spread_state_invalid"
        peak = state.get("basket_peak_pnl_usd")
        if peak is not None:
            try:
                if not math.isfinite(float(peak)):
                    return "basket_peak_invalid"
            except (TypeError, ValueError, OverflowError):
                return "basket_peak_invalid"
        runner = state.get("shadow_runner")
        runner_error = self._shadow_runner_state_error(runner)
        if runner_error is not None:
            return f"shadow_runner_{runner_error}"
        return None

    def _shadow_runner_state_error(self, state: Any) -> str | None:
        """Validate passive lane state before it can run ahead of core management."""

        if not isinstance(state, dict):
            return "shape"
        basket = state.get("basket")
        cfg = dict(self.params.get("runner_shadow") or {})
        if not isinstance(basket, list) or len(basket) > int(cfg.get("max_positions", 0)):
            return "basket_shape"
        expected_lot = float(cfg.get("lot", self.params.get("default_lot", 0.01)))
        sides: set[str] = set()
        try:
            for row in basket:
                if not isinstance(row, dict):
                    return "basket_row_shape"
                side = row.get("side")
                sides.add(str(side))
                if (
                    not isinstance(side, str)
                    or side not in {"LONG", "SHORT"}
                    or isinstance(row.get("lot"), bool)
                    or not math.isclose(float(row.get("lot") or 0.0), expected_lot, rel_tol=0.0, abs_tol=1e-12)
                    or isinstance(row.get("entry_price"), bool)
                    or not math.isfinite(float(row.get("entry_price") or 0.0))
                    or float(row.get("entry_price") or 0.0) <= 0.0
                    or not isinstance(row.get("entry_time_utc"), str)
                    or parse_ts(row.get("entry_time_utc")) is None
                    or not isinstance(row.get("signal_bar_time"), str)
                    or parse_ts(row.get("signal_bar_time")) is None
                    or not isinstance(row.get("opportunity_id"), str)
                    or not row.get("opportunity_id")
                    or row.get("shadow") is not True
                ):
                    return "basket_row_invalid"
        except (TypeError, ValueError, OverflowError):
            return "basket_row_value_invalid"
        if len(sides) > 1:
            return "mixed_sides"
        last_add = state.get("last_add_price")
        if bool(basket) != (last_add is not None):
            return "last_add_presence_invalid"
        if last_add is not None:
            try:
                if isinstance(last_add, bool) or not math.isfinite(float(last_add)) or float(last_add) <= 0.0:
                    return "last_add_price_invalid"
            except (TypeError, ValueError, OverflowError):
                return "last_add_price_invalid"
        peak = state.get("basket_peak_pnl_usd")
        if peak is not None:
            try:
                if isinstance(peak, bool) or not math.isfinite(float(peak)):
                    return "basket_peak_invalid"
            except (TypeError, ValueError, OverflowError):
                return "basket_peak_invalid"
        for key in ("last_evaluated_bar", "last_exit_evaluated_bar", "last_closed_at_utc", "cooldown_until_utc"):
            if state.get(key) not in (None, "") and (
                not isinstance(state.get(key), str) or parse_ts(state.get(key)) is None
            ):
                return f"{key}_invalid"
        if state.get("last_closed_reason") is not None and not isinstance(state.get("last_closed_reason"), str):
            return "last_closed_reason_invalid"
        return None

    @staticmethod
    def _normalize_time_close_spread_container(state: dict[str, Any]) -> bool:
        """Repair only time-close auxiliaries; never discard owned basket state."""
        defaults = {
            "time_close_defer_started_utc": None,
            "time_close_last_quote_msc": None,
            "time_close_stable_count": 0,
            "time_close_wide_seen": False,
        }
        changed = False
        for key, value in defaults.items():
            if key not in state:
                state[key] = value
                changed = True
        started = parse_ts(state.get("time_close_defer_started_utc"))
        last_msc = state.get("time_close_last_quote_msc")
        count = state.get("time_close_stable_count")
        wide = state.get("time_close_wide_seen")
        valid = (
            (state.get("time_close_defer_started_utc") is None or started is not None)
            and (last_msc is None or (isinstance(last_msc, int) and not isinstance(last_msc, bool) and last_msc > 0))
            and isinstance(count, int) and not isinstance(count, bool) and count >= 0
            and isinstance(wide, bool)
            and ((wide and started is not None) or (not wide and started is None and count == 0))
        )
        if not valid:
            for key, value in defaults.items():
                if state.get(key) != value:
                    state[key] = value
                    changed = True
        return changed

    @staticmethod
    def _normalize_core_peak_container(state: dict[str, Any]) -> bool:
        """Preserve active inventory while making a corrupt peak rebuildable."""

        peak = state.get("basket_peak_pnl_usd")
        if peak is None:
            return False
        try:
            valid = not isinstance(peak, bool) and math.isfinite(float(peak))
        except (TypeError, ValueError, OverflowError):
            valid = False
        if valid:
            return False
        state["basket_peak_pnl_usd"] = None
        return True

    def _load_state(self) -> dict[str, Any]:
        default = self._default_state()
        if not os.path.exists(STATE_FILE):
            return default
        load_error: str | None = None
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = strict_json_load(f)
        except Exception as exc:
            logging.exception("Could not load state; refusing to start from an unverified default")
            state = None
            load_error = f"{type(exc).__name__}: {exc}"
        observed = state if isinstance(state, dict) else {}
        try:
            version_matches = int(observed.get("version", 0)) == int(default["version"])
        except (TypeError, ValueError, OverflowError):
            version_matches = False
        strategies = observed.get("strategies")
        # Top-level corruption has unknown ownership and remains fatal.  One
        # malformed strategy container can be isolated to that lane below.
        quarantine = observed.get("quarantined_strategy_states")
        shadow_quarantine = observed.get("quarantined_shadow_runner_states", {})
        shape_matches = (
            isinstance(strategies, dict)
            and isinstance(quarantine, dict)
            and isinstance(shadow_quarantine, dict)
        )
        identity_matches = (
            observed.get("bot") == default["bot"]
            and observed.get("strategy_id") == default["strategy_id"]
            and version_matches
        )
        if not identity_matches or not shape_matches:
            self._fatal_state_identity_mismatch = True
            observed_identity = {
                "bot": observed.get("bot"),
                "strategy_id": observed.get("strategy_id"),
                "version": observed.get("version"),
                "state_type": type(state).__name__,
                "shape_valid": shape_matches,
                "load_error": load_error,
            }
            logging.critical(
                "S24 state identity/shape invalid; refusing legacy, corrupt, or foreign state: bot=%s strategy_id=%s version=%s type=%s",
                observed.get("bot"), observed.get("strategy_id"), observed.get("version"), type(state).__name__,
            )
            state = default
            for strat in self.params["strategies"]:
                st = state["strategies"][strat["id"]]
                st["sync_block_new_entries"] = True
                st["sync_block_reason"] = "state_identity_mismatch"
                st["sync_block_recoverable"] = False
                st["sync_block_details"] = {
                    "observed": observed_identity,
                    "expected": {"bot": default["bot"], "strategy_id": default["strategy_id"], "version": default["version"]},
                }
        elif isinstance(state, dict):
            state.setdefault("quarantined_shadow_runner_states", {})
            for strat in self.params["strategies"]:
                sid = str(strat["id"])
                candidate_state = state["strategies"].get(sid)
                if isinstance(candidate_state, dict):
                    # V10 state files predate exact entry-signal closure evidence.
                    # Adding the empty evidence container is lossless and keeps
                    # any broker-owned basket available for reconciliation.
                    candidate_state.setdefault("last_closed_entry_signal_bars", [])
                    candidate_state.setdefault("entry_retry_after_utc", None)
                    candidate_state.setdefault("entry_retry_signal_bar", None)
                    candidate_state.setdefault("entry_retry_reason", None)
                    candidate_state.setdefault("entry_permission_reject_count", 0)
                    if "position_signal_identity_required" not in candidate_state:
                        legacy_basket = candidate_state.get("basket")
                        has_missing_signal_identity = (
                            isinstance(legacy_basket, list)
                            and bool(legacy_basket)
                            and any(
                                isinstance(row, dict) and parse_ts(row.get("signal_bar_time")) is None
                                for row in legacy_basket
                            )
                        )
                        candidate_state["position_signal_identity_required"] = not has_missing_signal_identity
                    self._normalize_time_close_spread_container(candidate_state)
                    if self._normalize_core_peak_container(candidate_state):
                        logging.warning(
                            "S24 invalid basket peak normalized for conservative quote rebuild: strategy=%s active=%s",
                            sid,
                            bool(candidate_state.get("basket")),
                        )
                    shadow_error = self._shadow_runner_state_error(candidate_state.get("shadow_runner"))
                    if shadow_error is not None:
                        # This lane is explicitly no-order.  Preserve its raw
                        # evidence, reset only the passive lane, and keep the
                        # independently owned core basket available for broker
                        # reconciliation and exit management.
                        state["quarantined_shadow_runner_states"][sid] = copy.deepcopy(candidate_state.get("shadow_runner"))
                        candidate_state["shadow_runner"] = copy.deepcopy(default["strategies"][sid]["shadow_runner"])
                        logging.error(
                            "S24 passive shadow runner state quarantined and reset: strategy=%s cause=%s",
                            sid,
                            shadow_error,
                        )
                try:
                    state_error = self._core_state_shape_error(strat, candidate_state)
                except Exception as exc:
                    state_error = f"shape_check_exception:{type(exc).__name__}"
                if state_error is not None:
                    state.setdefault("quarantined_strategy_states", {})[sid] = copy.deepcopy(candidate_state)
                    replacement = default["strategies"][sid]
                    replacement["sync_block_new_entries"] = True
                    replacement["sync_block_reason"] = "state_container_invalid"
                    replacement["sync_block_recoverable"] = False
                    replacement["sync_block_details"] = {"cause": state_error, "quarantine_key": sid}
                    state["strategies"][sid] = replacement
                    basket_snapshot = candidate_state.get("basket") if isinstance(candidate_state, dict) else None
                    basket_rows = basket_snapshot if isinstance(basket_snapshot, list) else []
                    if isinstance(candidate_state, dict) and (
                        bool(basket_snapshot)
                        or candidate_state.get("pending_open_opportunity_id") is not None
                        or candidate_state.get("pending_open_started_utc") is not None
                        or candidate_state.get("pending_close_reason") is not None
                        or any(
                            isinstance(row, dict)
                            and (
                                row.get("close_requested") is True
                                or row.get("close_submission_started_utc") is not None
                            )
                            for row in basket_rows
                        )
                    ):
                        # Invalid state that still carries ownership or an
                        # in-flight order/close receipt cannot be safely
                        # reduced to an empty lane. Preserve the raw JSON and
                        # fail preflight until it is reconciled.
                        self._fatal_state_identity_mismatch = True
                        replacement["sync_block_details"]["active_lifecycle_quarantined"] = True
                        logging.critical(
                            "S24 strategy state has active lifecycle evidence; quarantined and refusing startup: strategy=%s cause=%s",
                            sid,
                            state_error,
                        )
        state.setdefault("quarantined_strategy_states", {})
        state.setdefault("quarantined_shadow_runner_states", {})
        state.setdefault("strategies", {})
        for sid, st in default["strategies"].items():
            state["strategies"].setdefault(sid, st)
            for key, value in st.items():
                state["strategies"][sid].setdefault(key, value)
            runner_default = st["shadow_runner"]
            runner_state = state["strategies"][sid].setdefault("shadow_runner", {})
            for key, value in runner_default.items():
                runner_state.setdefault(key, value)
        return state

    def _save_state(self) -> None:
        self.state["last_saved_utc"] = dt_text(utc_now())
        atomic_write_json(STATE_FILE, self.state)

    def _st(self, strat: dict[str, Any]) -> dict[str, Any]:
        if strat.get("id") == "range_rotation_v206_lane_1":
            return self.state.setdefault("v206", default_v206_state())
        return self.state["strategies"][strat["id"]]

    def _trade_row(self, event: str, strat: dict[str, Any], **kwargs: Any) -> None:
        now = utc_now()
        lane_id = int(strat.get("lane_id") or (206 if int(strat.get("magic", 0) or 0) == 240206 else 1))
        signal_bar = parse_ts(kwargs.get("signal_bar_time"))
        row = {
            "timestamp_utc": dt_text(now),
            "event": event,
            "strategy_id": strat["id"],
            "lane_id": lane_id,
            "magic": int(strat["magic"]),
            "symbol": self.params["symbol"],
            "mt5_symbol": self.params.get("mt5_symbol", self.params["symbol"]),
            "basket_id": self._basket_id(strat),
            "event_time": dt_text(signal_bar) if signal_bar is not None else "",
            "release_time": dt_text(signal_bar + pd.Timedelta(minutes=1)) if signal_bar is not None else "",
            "available_time": dt_text(signal_bar + pd.Timedelta(minutes=1)) if signal_bar is not None else "",
            "decision_time": dt_text(now),
            "live": self.live_enabled,
        }
        row.update(kwargs)
        price = row.get("price")
        if price not in (None, "") and event in {"entry", "v206_entry_confirmed"}:
            row.setdefault("entry_price", price)
        if price not in (None, "") and ("close_deal" in event or event == "v206_close_submitted"):
            row.setdefault("exit_price", price)
        reason = str(row.get("reason") or "")
        coalesce = event in {"entry_skip", "v206_entry_skip"} and (
            reason in REPEATABLE_DIAGNOSTIC_REASONS or str(row.get("note") or "") == "sync_block"
        )
        active = self._diagnostic_repeats.get(lane_id)
        signature = (event, reason, str(row.get("note") or ""))
        if not coalesce:
            self._flush_diagnostic_repeat(lane_id, now)
            append_csv(TRADE_LOG_FILE, row, TRADE_FIELDS)
            return
        if active is None or active["signature"] != signature:
            self._flush_diagnostic_repeat(lane_id, now)
            row["repeat_count"] = 1
            row["repeat_window_seconds"] = 0
            append_csv(TRADE_LOG_FILE, row, TRADE_FIELDS)
            self._diagnostic_repeats[lane_id] = {
                "signature": signature,
                "first": now,
                "last": now,
                "suppressed": 0,
                "row": dict(row),
            }
            return
        active["last"] = now
        active["suppressed"] = int(active.get("suppressed", 0)) + 1
        interval = float(self.params.get("diagnostic_repeat_summary_seconds", 300.0))
        if (now - active["first"]).total_seconds() >= interval:
            self._flush_diagnostic_repeat(lane_id, now, keep_signature=True)

    def _basket_id(self, strat: dict[str, Any], basket: list[dict[str, Any]] | None = None) -> str:
        rows = list(basket if basket is not None else (self._st(strat).get("basket") or []))
        if not rows:
            return ""
        first = rows[0]
        signal_bar = str(first.get("signal_bar_time") or "")
        side = str(first.get("side") or "")
        if not signal_bar or side not in {"LONG", "SHORT"}:
            return ""
        return f"s24-basket:{strat['id']}:{signal_bar}:{side}"

    def _flush_diagnostic_repeat(
        self,
        lane_id: int,
        now: datetime | None = None,
        *,
        keep_signature: bool = False,
    ) -> None:
        active = self._diagnostic_repeats.get(int(lane_id))
        if active is None:
            return
        at = now or utc_now()
        suppressed = int(active.get("suppressed", 0))
        if suppressed > 0:
            row = dict(active["row"])
            original_note = str(row.get("note") or "")
            row.update({
                "timestamp_utc": dt_text(at),
                "event": "diagnostic_repeat_summary",
                "repeat_count": suppressed,
                "repeat_window_seconds": round(max(0.0, (active["last"] - active["first"]).total_seconds()), 3),
                "decision_time": dt_text(at),
                "note": f"source_event={active['signature'][0]};source_note={original_note}",
            })
            append_csv(TRADE_LOG_FILE, row, TRADE_FIELDS)
        if keep_signature:
            active["first"] = at
            active["last"] = at
            active["suppressed"] = 0
        else:
            self._diagnostic_repeats.pop(int(lane_id), None)

    def _shadow_runner_row(self, event: str, strat: dict[str, Any], **kwargs: Any) -> None:
        runner = self._st(strat)["shadow_runner"]
        row = {
            "timestamp_utc": dt_text(utc_now()),
            "event": event,
            "lane_id": int((self.params.get("runner_shadow") or {}).get("lane_id", 2)),
            "strategy_id": strat["id"],
            "position_count": len(runner["basket"]),
        }
        row.update(kwargs)
        append_csv(SHADOW_RUNNER_LOG_FILE, row, SHADOW_RUNNER_FIELDS)

    @staticmethod
    def _alert_signature(reason: str, details: dict[str, Any]) -> str:
        encoded = json.dumps({"reason": reason, "details": details}, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _notify_reconciliation_required(self, strat: dict[str, Any], reason: str, details: dict[str, Any]) -> None:
        if self._suppress_manual_alerts:
            return
        st = self._st(strat)
        signature = self._alert_signature(reason, details)
        if st.get("manual_alert_last_signature") == signature:
            return
        delivered = notify_manual_action_required(
            bot_id="bot24",
            symbol=str(self.params.get("mt5_symbol", self.params["symbol"])),
            title="reconciliation_required",
            reason=f"{reason}; details={json.dumps(details, ensure_ascii=True, sort_keys=True, default=str)}",
            action="Inspect bot24-owned MT5 inventory and state before clearing the block.",
            key=f"bot24:reconciliation:{strat['id']}:{reason}",
        )
        if delivered:
            st["manual_alert_last_signature"] = signature
            st["manual_alert_last_reason"] = reason
            st["manual_alert_last_at_utc"] = dt_text(utc_now())

    def _set_sync_block(
        self,
        strat: dict[str, Any],
        reason: str | None,
        details: dict[str, Any] | None = None,
        *,
        recoverable: bool = False,
    ) -> None:
        st = self._st(strat)
        previous = st.get("sync_block_reason")
        if reason:
            if recoverable and st.get("sync_block_new_entries") and not st.get("sync_block_recoverable"):
                logging.warning("S24 retained non-recoverable block for %s: %s", strat["id"], previous)
                return
            if previous != reason:
                st["flat_clear_confirmation_count"] = 0
                st["flat_clear_confirmation_reason"] = None
                logging.error("S24 new entries blocked for %s: %s", strat["id"], reason)
            st["sync_block_new_entries"] = True
            st["sync_block_reason"] = reason
            st["sync_block_recoverable"] = bool(recoverable)
            st["sync_block_details"] = details or {}
            if not recoverable:
                self._notify_reconciliation_required(strat, reason, st["sync_block_details"])
            return
        if st.get("sync_block_new_entries"):
            logging.warning("S24 new-entry block cleared for %s after clean sync: %s", strat["id"], previous)
        st["sync_block_new_entries"] = False
        st["sync_block_reason"] = None
        st["sync_block_recoverable"] = False
        st["sync_block_details"] = {}
        st["flat_clear_confirmation_count"] = 0
        st["flat_clear_confirmation_reason"] = None
        st["manual_alert_last_signature"] = None
        st["manual_alert_last_reason"] = None

    def _replace_with_recoverable_sync_block(
        self,
        strat: dict[str, Any],
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Replace only a close ambiguity that has just been affirmatively resolved."""

        st = self._st(strat)
        previous = str(st.get("sync_block_reason") or "")
        if previous and previous not in CORE_RESOLVED_CLOSE_BLOCK_REASONS:
            return
        st["sync_block_new_entries"] = True
        st["sync_block_reason"] = reason
        st["sync_block_recoverable"] = True
        st["sync_block_details"] = details or {}
        st["flat_clear_confirmation_count"] = 0
        st["flat_clear_confirmation_reason"] = None

    def _reset_time_close_spread_state(self, strat: dict[str, Any]) -> None:
        st = self._st(strat)
        st["time_close_defer_started_utc"] = None
        st["time_close_last_quote_msc"] = None
        st["time_close_stable_count"] = 0
        st["time_close_wide_seen"] = False

    def _time_close_spread_ready(
        self,
        strat: dict[str, Any],
        reason: str,
        quote_time: pd.Timestamp,
        info: Any,
        signal_bar: str,
    ) -> bool:
        if reason not in CORE_TIME_CLOSE_REASONS:
            return True
        st = self._st(strat)
        quote_msc = int(quote_time.timestamp() * 1000)
        raw_started = st.get("time_close_defer_started_utc")
        started = parse_ts(raw_started) if isinstance(raw_started, str) else None
        raw_last = st.get("time_close_last_quote_msc")
        raw_count = st.get("time_close_stable_count")
        raw_wide = st.get("time_close_wide_seen")
        valid = (
            (raw_started is None or (started is not None and started <= quote_time))
            and (raw_last is None or (isinstance(raw_last, int) and not isinstance(raw_last, bool) and 0 < raw_last <= quote_msc))
            and isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 0
            and isinstance(raw_wide, bool)
            and ((raw_wide and started is not None) or (not raw_wide and started is None and raw_count == 0))
        )
        if not valid:
            self._reset_time_close_spread_state(strat)
            self._trade_row("position_lifecycle_recovered", strat, reason="time_close_spread_state_invalid_reset", signal_bar_time=signal_bar)
            self._save_state()
            started = None
            raw_last = None
            raw_count = 0
            raw_wide = False
        if raw_last is not None and quote_msc <= int(raw_last):
            return False
        st["time_close_last_quote_msc"] = quote_msc
        try:
            bid = float(info.bid)
            ask = float(info.ask)
            point = float(self.params.get("point_size", 0.001))
            spread = (ask - bid) / point
        except (AttributeError, TypeError, ValueError, OverflowError):
            spread = math.inf
        cap = float(self.params.get("time_close_max_spread_points", 300.0))
        if not math.isfinite(spread) or spread < 0.0 or not math.isfinite(cap) or cap <= 0.0:
            return False
        if not raw_wide and spread > cap:
            st["time_close_wide_seen"] = True
            st["time_close_defer_started_utc"] = dt_text(quote_time)
            st["time_close_stable_count"] = 0
            self._trade_row("time_close_deferred", strat, reason="spread_wide", signal_bar_time=signal_bar, note=f"spread_points={spread:.3f};cap={cap:.3f}")
            self._save_state()
            return False
        if raw_wide:
            force_after = started + pd.Timedelta(minutes=float(self.params.get("time_close_force_after_minutes", 30.0)))
            if quote_time < force_after:
                st["time_close_stable_count"] = int(raw_count) + 1 if spread <= cap else 0
                self._save_state()
                if int(st["time_close_stable_count"]) < int(self.params.get("time_close_stable_quotes", 3)):
                    return False
        return True

    @staticmethod
    def _side_from_record(record: Any) -> str:
        return "LONG" if int(getattr(record, "type", -1)) == ORDER_TYPE_BUY else "SHORT"

    @staticmethod
    def _owned_comment(comment_prefix: Any, comment: Any) -> bool:
        prefix = str(comment_prefix or "")
        observed = str(comment or "")
        if not prefix:
            return False
        if observed == prefix:
            return True
        suffix = observed[len(prefix) + 1:] if observed.startswith(f"{prefix}:") else ""
        return len(suffix) == 10 and all(char in "0123456789abcdef" for char in suffix)

    def _owned_position(self, strat: dict[str, Any], record: Any) -> bool:
        return (
            str(getattr(record, "symbol", "")) == str(self.params.get("mt5_symbol", self.params["symbol"]))
            and int(getattr(record, "magic", -1)) == int(strat["magic"])
            and self._owned_comment(strat["comment_prefix"], getattr(record, "comment", ""))
        )

    def _state_matches_live(self, strat: dict[str, Any], state_pos: dict[str, Any], live_pos: Any) -> bool:
        state_ticket = int(state_pos.get("ticket") or 0)
        live_ticket = int(getattr(live_pos, "ticket", 0) or 0)
        position_id = int(state_pos.get("position_identifier") or state_pos.get("ticket") or 0)
        live_position_id = int(getattr(live_pos, "identifier", 0) or getattr(live_pos, "ticket", 0))
        return (
            state_ticket > 0
            and state_ticket == live_ticket
            and position_id > 0
            and position_id == live_position_id
            and str(state_pos.get("side")) == self._side_from_record(live_pos)
            and math.isclose(float(state_pos.get("lot") or 0.0), float(getattr(live_pos, "volume", 0.0)), rel_tol=0.0, abs_tol=1e-9)
            and str(state_pos.get("owner_comment") or "") == str(getattr(live_pos, "comment", "") or "")
            and self._owned_position(strat, live_pos)
        )

    def _state_ownership_proven(self, strat: dict[str, Any], state_pos: dict[str, Any]) -> bool:
        return (
            int(state_pos.get("position_identifier") or state_pos.get("ticket") or 0) > 0
            and str(state_pos.get("owner_symbol") or "") == str(self.params.get("mt5_symbol", self.params["symbol"]))
            and int(state_pos.get("owner_magic") or -1) == int(strat["magic"])
            and self._owned_comment(strat["comment_prefix"], state_pos.get("owner_comment"))
            and str(state_pos.get("side") or "") in {"LONG", "SHORT"}
        )

    def _clear_basket_state(
        self,
        strat: dict[str, Any],
        reason: str,
        signal_bar: str | None = None,
        *,
        close_side: str | None = None,
        close_time: Any = None,
    ) -> None:
        st = self._st(strat)
        basket_sides = {str(row.get("side")) for row in st.get("basket", []) if isinstance(row, dict)}
        resolved_side = close_side if close_side in {"LONG", "SHORT"} else (
            next(iter(basket_sides)) if len(basket_sides) == 1 else None
        )
        resolved_close_time = parse_ts(close_time) or utc_now()
        closed_entry_signal_bars = sorted({
            dt_text(parsed)
            for row in st.get("basket", [])
            if isinstance(row, dict)
            for parsed in [parse_ts(row.get("signal_bar_time"))]
            if parsed is not None
        })
        consumed_candidates = []
        for candidate in (st.get("last_evaluated_bar"), signal_bar):
            parsed = parse_ts(candidate)
            if parsed is not None and parsed + pd.Timedelta(minutes=1) <= resolved_close_time:
                consumed_candidates.append(parsed)
        st["basket"] = []
        st["last_add_price"] = None
        st["basket_peak_pnl_usd"] = None
        st["pending_close_reason"] = None
        st["pending_close_signal_bar"] = None
        st["last_exit_evaluated_bar"] = None
        st["cooldown_until_bar"] = -1
        closed_bar = parse_ts(signal_bar)
        st["cooldown_until_utc"] = dt_text(closed_bar + pd.Timedelta(minutes=int(strat.get("cooldown", 0)))) if closed_bar is not None else None
        st["last_closed_at_utc"] = dt_text(resolved_close_time)
        st["last_closed_side"] = resolved_side
        st["last_closed_reason"] = reason
        st["last_closed_signal_bar"] = signal_bar
        st["last_closed_entry_signal_bars"] = closed_entry_signal_bars
        st["position_signal_identity_required"] = True
        st["last_consumed_signal_bar"] = dt_text(max(consumed_candidates)) if consumed_candidates else None
        self._reset_time_close_spread_state(strat)

    def _clear_pending_open(self, strat: dict[str, Any]) -> None:
        st = self._st(strat)
        st["pending_open_opportunity_id"] = None
        st["pending_open_started_utc"] = None

    def _entry_submission_block_reason(self, strat: dict[str, Any]) -> str | None:
        st = self._st(strat)
        if st.get("sync_block_new_entries"):
            return str(st.get("sync_block_reason") or "sync_block_new_entries")
        if not st.get("position_signal_identity_required", False):
            return "legacy_position_signal_identity_migration_pending"
        if st.get("pending_open_opportunity_id"):
            return "unresolved_open_action"
        return None

    @staticmethod
    def _core_open_no_fill_retcode(error: str) -> int | None:
        parts = str(error or "").split("|")
        if len(parts) != 5 or parts[0] != "ERR":
            return None
        try:
            retcode = int(parts[1])
            order = int(parts[2].removeprefix("ORDER=")) if parts[2].startswith("ORDER=") else -1
            deal = int(parts[3].removeprefix("DEAL=")) if parts[3].startswith("DEAL=") else -1
            last_error = int(parts[4].removeprefix("LAST=")) if parts[4].startswith("LAST=") else -1
        except (TypeError, ValueError, OverflowError):
            return None
        return retcode if retcode in {10018, 10026, 10027} and order == 0 and deal == 0 and last_error >= 0 else None

    @classmethod
    def _core_open_definitive_no_fill(cls, error: str) -> bool:
        return cls._core_open_no_fill_retcode(error) is not None

    @staticmethod
    def _account_identity_error(account: dict[str, Any]) -> str | None:
        observed_login = account.get("login")
        observed_server = str(account.get("server") or "")
        if observed_login is None or not observed_server:
            return "account_identity_unavailable; recompile and attach the current BotBridge_s24"
        if int(observed_login) != int(MT5_LOGIN) or observed_server.casefold() != str(MT5_SERVER).casefold():
            return "configured account identity does not match the attached terminal"
        return None

    def connect_and_preflight(self) -> bool:
        namespace_error = self._ownership_namespace_error()
        if namespace_error:
            logging.critical("S24 ownership namespace invalid: %s", namespace_error)
            return False
        if self._fatal_state_identity_mismatch:
            logging.critical("S24 legacy/foreign state must be archived before this runner can start.")
            return False
        try:
            validate_csv_schema(TRADE_LOG_FILE, TRADE_FIELDS)
        except RuntimeError as exc:
            logging.critical("S24 execution audit CSV schema preflight failed: %s", exc)
            return False
        if self.passive_shadow_runner_enabled:
            try:
                validate_csv_schema(SHADOW_RUNNER_LOG_FILE, SHADOW_RUNNER_FIELDS)
            except RuntimeError as exc:
                logging.error("S24 passive shadow runner CSV invalid; lane disabled and core preflight retained: %s", exc)
                self.passive_shadow_runner_enabled = False
                self.passive_shadow_init_errors["shadow_runner_csv"] = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
        if not bool(self.params.get("enabled", True)):
            logging.info("S24 disabled by params.")
            return False
        if not self.dm.connect():
            logging.error("S24 EA bridge connect failed.")
            return False
        caps = self.executor.get_bridge_capabilities()
        logging.info("S24 bridge caps: %s", caps)
        if not caps:
            logging.critical("S24 bridge capability query failed.")
            return False
        expected_bridge = str(self.params.get("expected_bridge_name") or EXPECTED_BRIDGE_NAME)
        if str(caps.get("name") or "") != expected_bridge:
            logging.critical("S24 wrong bridge attached: got=%s expected=%s", caps.get("name"), expected_bridge)
            return False
        expected_version = str(self.params.get("expected_bridge_version") or EXPECTED_BRIDGE_VERSION)
        if str(caps.get("version") or "") != expected_version:
            logging.critical("S24 wrong bridge version: got=%s expected=%s", caps.get("version"), expected_version)
            return False
        missing = REQUIRED_SHARED_ACCOUNT_COMMANDS - {str(x).upper() for x in caps.get("commands", set())}
        if missing:
            logging.critical("S24 bridge missing required commands: %s", sorted(missing))
            return False
        # Account and quote identity guard every broker-state read, not only a
        # new OPEN.  With live entry disabled the runner still reconciles
        # durable core/v206 ownership, and v206 may retain an existing managed
        # position.  Starting that lifecycle against an unverified terminal can
        # otherwise consume or discard evidence from the wrong account.
        account = self.executor.get_account_info()
        if account is None:
            logging.critical("S24 account execution metadata unavailable.")
            return False
        account_identity_error = self._account_identity_error(account)
        if account_identity_error is not None:
            logging.critical("S24 account identity mismatch: %s", account_identity_error)
            return False
        if bool(self.params.get("require_hedging_account", True)) and int(account.get("margin_mode", -1)) != HEDGING_MARGIN_MODE:
            logging.critical("S24 lifecycle reconciliation requires a hedging account: mode=%s", account.get("margin_mode_name"))
            return False
        symbol_info = self.executor.get_symbol_info(str(self.params.get("mt5_symbol", self.params["symbol"])))
        if symbol_info is None or getattr(symbol_info, "quote_time_msc", None) is None:
            logging.critical("S24 bridge INFO response lacks broker quote timestamp; compile and attach the updated BotBridge_s24 before lifecycle reconciliation.")
            return False
        return True

    def _ownership_namespace_error(self) -> str | None:
        strategies = [row for row in self.params.get("strategies", []) if bool(row.get("enabled", True))]
        v206 = dict(self.params.get("v206_strategy") or {})
        # The namespace remains reserved and monitored when new v206 entries are disabled.
        if v206:
            strategies.append(v206)
        magics = [int(row.get("magic") or 0) for row in strategies]
        prefixes = [str(row.get("comment_prefix") or "") for row in strategies]
        if magics != [EXPECTED_S24_MAGIC, 240206]:
            return f"invalid_magics={magics} expected={[EXPECTED_S24_MAGIC, 240206]}"
        if len(magics) != len(set(magics)):
            return f"duplicate_magics={magics}"
        if any(not prefix.startswith("s24_") for prefix in prefixes) or len(prefixes) != len(set(prefixes)):
            return f"invalid_or_duplicate_comment_prefixes={prefixes}"
        return None

    def _get_m1(self) -> pd.DataFrame | None:
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        bars = self.dm.get_historical_data(
            symbol,
            int(self.params.get("m1_timeframe", 1)),
            int(self.params.get("m1_bars", 240)),
            str(self.params.get("broker_timezone", "UTC")),
            drop_latest=bool(self.params.get("drop_latest_m1_bar", True)),
        )
        if bars is None or len(bars) < 100:
            return None
        point = float(self.params.get("point_size", 0.01))
        return add_features(bars, point)

    def _strategy_signal_decision(self, row: pd.Series, strat: dict[str, Any]) -> tuple[str | None, str]:
        if float(row.get("spread_points", 0.0)) > float(self.params.get("max_entry_spread_points", 300.0)):
            return None, "spread_guard"
        atr30 = float(row.get("atr30", math.nan))
        vol_ratio = float(row.get("vol_ratio", math.nan))
        if not math.isfinite(atr30) or not math.isfinite(vol_ratio):
            return None, "features_unavailable"
        if vol_ratio < float(strat.get("vol_min", 1.0)):
            return None, "vol_ratio_below_min"
        imp = float(row["ret5"] if int(strat["impulse_bars"]) <= 6 else row["ret10"])
        long_imp = imp >= float(strat["impulse_atr"]) * atr30
        short_imp = -imp >= float(strat["impulse_atr"]) * atr30
        long_break = float(row["Close"]) >= float(row["roll_high30"])
        short_break = float(row["Close"]) <= float(row["roll_low30"])
        mode = str(strat["mode"])
        if mode == "impulse":
            long_ok, short_ok = long_imp, short_imp
        elif mode == "breakout_impulse":
            long_ok, short_ok = long_imp and long_break, short_imp and short_break
        else:
            return None, "unsupported_mode"
        if long_ok:
            return "LONG", "long_signal"
        if short_ok:
            return "SHORT", "short_signal"
        if not long_imp and not short_imp:
            return None, "impulse_not_met"
        if mode == "breakout_impulse":
            return None, "breakout_not_met"
        return None, "no_signal"

    def _signal_decision(self, row: pd.Series, strat: dict[str, Any]) -> tuple[str | None, str]:
        ts = pd.Timestamp(row.name)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        ts = ts.tz_convert("UTC")
        evaluation = self.entry_wrapper.evaluate(ts.to_pydatetime(), {"row": row})
        if evaluation.status != EVALUATED:
            if evaluation.decision.status == NO_ACTIVE_REGIME:
                return None, "outside_session"
            return None, f"entry_routing_blocked:{evaluation.decision.status}"
        for outcome in evaluation.outcomes:
            if outcome.strategy_id == str(strat["id"]):
                side, reason = outcome.value
                return side, reason
        return None, "entry_routing_strategy_inactive"

    def _signal(self, row: pd.Series, strat: dict[str, Any]) -> str | None:
        side, _reason = self._signal_decision(row, strat)
        return side

    def _basket_pnl(self, strat: dict[str, Any], bid: float, ask: float) -> float:
        pnl = 0.0
        contract = float(self.params.get("contract_size", 100.0))
        for pos in self._st(strat)["basket"]:
            lot = float(pos["lot"])
            if pos["side"] == "LONG":
                pnl += (bid - float(pos["entry_price"])) * contract * lot
            else:
                pnl += (float(pos["entry_price"]) - ask) * contract * lot
        return pnl

    def _shadow_runner_pnl(self, strat: dict[str, Any], bid: float, ask: float) -> float:
        pnl = 0.0
        contract = float(self.params.get("contract_size", 100.0))
        for pos in self._st(strat)["shadow_runner"]["basket"]:
            lot = float(pos["lot"])
            if pos["side"] == "LONG":
                pnl += (bid - float(pos["entry_price"])) * contract * lot
            else:
                pnl += (float(pos["entry_price"]) - ask) * contract * lot
        return pnl

    def _shadow_runner_context(self, strat: dict[str, Any]) -> dict[str, Any]:
        core = self._st(strat)["basket"]
        runner = self._st(strat)["shadow_runner"]["basket"]
        all_positions = list(core) + list(runner)
        return {
            "portfolio_positions": len(all_positions),
            "long_positions": sum(1 for pos in all_positions if pos.get("side") == "LONG"),
            "short_positions": sum(1 for pos in all_positions if pos.get("side") == "SHORT"),
            "lane_positions": {"core": len(core), "runner_shadow": len(runner)},
            "lane_pending": {"core": False, "runner_shadow": False},
            "lane_readiness": {"core": "live", "runner_shadow": "shadow_only"},
        }

    def _shadow_route(
        self,
        opportunity_id: str,
        *,
        status: str,
        reason: str,
        at: datetime,
        consumed: bool = False,
    ) -> None:
        try:
            self.shadow_observer.record_route(
                opportunity_id,
                at=at,
                status=status,
                consumed_lane_id=int((self.params.get("runner_shadow") or {}).get("lane_id", 2)) if consumed else None,
                reason=reason,
            )
        except Exception as exc:
            logging.exception("S24 passive shadow route recording failed; core execution is unchanged")
            self.shadow_observer.enabled = False
            self.passive_shadow_init_errors["opportunity_observer_runtime"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }

    def _run_shadow_runner(self, strat: dict[str, Any], bars: pd.DataFrame, info: Any) -> None:
        cfg = dict(self.params.get("runner_shadow") or {})
        if not self.passive_shadow_runner_enabled:
            return
        if str(cfg.get("execution_mode", "shadow")) != "shadow":
            logging.error("S24 runner lane refused non-shadow execution_mode=%s", cfg.get("execution_mode"))
            return
        if len(bars) < 2:
            return
        row = bars.iloc[-1]
        bar_time = parse_ts(row.name)
        if bar_time is None:
            self._shadow_runner_row("runner_error", strat, reason="signal_bar_time_invalid", signal_bar_time=str(row.name))
            return
        runner = self._st(strat)["shadow_runner"]
        bid = float(getattr(info, "bid", row["Close"]))
        ask = float(getattr(info, "ask", row.get("AskOpen", row["Open"])))
        now = self._quote_time_utc(info)
        bar_text = dt_text(bar_time)

        exit_clock = str(cfg.get("exit_clock", "confirmed_m1"))
        evaluate_exit = exit_clock == "poll" or runner.get("last_exit_evaluated_bar") != bar_text
        if evaluate_exit:
            runner["last_exit_evaluated_bar"] = bar_text
        if runner["basket"] and evaluate_exit:
            pnl = self._shadow_runner_pnl(strat, bid, ask)
            previous_peak = runner.get("basket_peak_pnl_usd")
            runner["basket_peak_pnl_usd"] = float(pnl) if previous_peak is None else max(float(previous_peak), float(pnl))
            entry_times = [parse_ts(pos.get("entry_time_utc")) for pos in runner["basket"]]
            valid_entry_times = [value for value in entry_times if value is not None]
            if not valid_entry_times:
                self._shadow_runner_row("runner_error", strat, pnl=round(pnl, 4), reason="state_entry_time_invalid", signal_bar_time=bar_text)
                return
            held_minutes = max(0, int((pd.Timestamp(now) - min(valid_entry_times)).total_seconds() // 60))
            reason = None
            if pnl >= float(cfg.get("basket_target_usd", 32.0)):
                reason = "basket_target"
            elif pnl <= -float(cfg.get("basket_stop_usd", 48.0)):
                reason = "basket_stop"
            elif held_minutes >= int(cfg.get("max_hold_bars", 120)):
                reason = "max_hold"
            if reason:
                side = str(runner["basket"][0]["side"])
                count = len(runner["basket"])
                runner["basket"] = []
                runner["last_add_price"] = None
                runner["basket_peak_pnl_usd"] = None
                runner["last_closed_at_utc"] = dt_text(now)
                runner["last_closed_reason"] = reason
                runner["cooldown_until_utc"] = dt_text(bar_time + pd.Timedelta(minutes=int(cfg.get("cooldown", 3))))
                self._shadow_runner_row(
                    "runner_basket_close",
                    strat,
                    side=side,
                    position_count=count,
                    pnl=round(pnl, 4),
                    reason=reason,
                    signal_bar_time=bar_text,
                )
                self._save_state()
                return

        if runner.get("last_evaluated_bar") == bar_text:
            return
        runner["last_evaluated_bar"] = bar_text
        side, signal_reason = self._signal_decision(row, strat)
        if not side:
            self._shadow_runner_row("runner_decision", strat, reason=signal_reason, signal_bar_time=bar_text)
            self._save_state()
            return

        opportunity_id = f"s24:{strat['id']}:{bar_text}:{side}"
        context = self._shadow_runner_context(strat)
        context.update(
            {
                "spread_points": float(row.get("spread_points", 0.0)),
                "atr30": float(row.get("atr30", math.nan)),
                "ret10": float(row.get("ret10", math.nan)),
                "vol_ratio": float(row.get("vol_ratio", math.nan)),
            }
        )
        opportunity = {
            "opportunity_id": opportunity_id,
            "raw_side": side,
            "effective_side": side,
            "entry_policy": {
                "policy_id": "s24_runner_shadow_v1",
                "action": "shadow_route",
                "reason": "confirmed_core_signal",
            },
            "event_time": bar_text,
            "release_time": dt_text(bar_time + pd.Timedelta(minutes=1)),
            "decision_time": dt_text(now),
        }
        try:
            self.shadow_observer.register_opportunity(
                opportunity,
                at=now,
                bid=bid,
                ask=ask,
                context=context,
            )
        except Exception as exc:
            logging.exception("S24 passive shadow opportunity registration failed; core execution is unchanged")
            self.shadow_observer.enabled = False
            self.passive_shadow_init_errors["opportunity_observer_runtime"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
        try:
            self.shadow_state_tagger.tag_opportunity(
                opportunity,
                at=now,
                bars=bars,
                bid=bid,
                ask=ask,
                context=context,
            )
        except Exception as exc:
            logging.exception("S24 passive shadow state tagging failed; core execution is unchanged")
            self.shadow_state_tagger.enabled = False
            self.passive_shadow_init_errors["state_tagger_runtime"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }

        stale = stale_signal_decision(
            str(row.name),
            timeframe_hours=1.0 / 60.0,
            max_delay_minutes=float(self.params.get("max_signal_delay_minutes", 2.0)),
            options=self.safety,
        )
        if stale.stale:
            self._shadow_runner_row("runner_decision", strat, opportunity_id=opportunity_id, side=side, reason="stale_signal_skip", signal_bar_time=bar_text)
            self._shadow_route(opportunity_id, status="not_consumed", reason="stale_signal_skip", at=now)
            self._save_state()
            return
        cooldown_until = parse_ts(runner.get("cooldown_until_utc"))
        if cooldown_until is not None and bar_time < cooldown_until:
            self._shadow_runner_row("runner_decision", strat, opportunity_id=opportunity_id, side=side, reason="cooldown", signal_bar_time=bar_text)
            self._shadow_route(opportunity_id, status="not_consumed", reason="cooldown", at=now)
            self._save_state()
            return
        if len(runner["basket"]) >= int(cfg.get("max_positions", 2)):
            self._shadow_runner_row("runner_decision", strat, opportunity_id=opportunity_id, side=side, reason="lane_capacity_full", signal_bar_time=bar_text)
            self._shadow_route(opportunity_id, status="not_consumed", reason="lane_capacity_full", at=now)
            self._save_state()
            return
        if runner["basket"]:
            if any(pos.get("side") != side for pos in runner["basket"]):
                self._shadow_runner_row("runner_decision", strat, opportunity_id=opportunity_id, side=side, reason="opposite_side_open", signal_bar_time=bar_text)
                self._shadow_route(opportunity_id, status="not_consumed", reason="opposite_side_open", at=now)
                self._save_state()
                return
            atr30 = float(row.get("atr30", math.nan))
            last_add = runner.get("last_add_price")
            favorable = last_add is not None and math.isfinite(atr30) and (
                (side == "LONG" and float(row["Close"]) >= float(last_add) + float(cfg.get("add_atr", 0.85)) * atr30)
                or (side == "SHORT" and float(row["Close"]) <= float(last_add) - float(cfg.get("add_atr", 0.85)) * atr30)
            )
            if not favorable:
                self._shadow_runner_row("runner_decision", strat, opportunity_id=opportunity_id, side=side, reason="add_threshold_not_met", signal_bar_time=bar_text)
                self._shadow_route(opportunity_id, status="not_consumed", reason="add_threshold_not_met", at=now)
                self._save_state()
                return

        lot = float(cfg.get("lot", self.params.get("default_lot", 0.01)))
        entry_price = normalize_price(ask if side == "LONG" else bid, int(self.params.get("price_digits", 3)))
        entry_time = dt_text(now)
        runner["basket"].append(
            {
                "side": side,
                "lot": lot,
                "entry_price": entry_price,
                "entry_time_utc": entry_time,
                "signal_bar_time": bar_text,
                "opportunity_id": opportunity_id,
                "shadow": True,
            }
        )
        if len(runner["basket"]) == 1:
            runner["basket_peak_pnl_usd"] = None
        runner["last_add_price"] = entry_price
        self._shadow_runner_row(
            "runner_entry",
            strat,
            opportunity_id=opportunity_id,
            side=side,
            lot=lot,
            price=entry_price,
            reason="initial" if len(runner["basket"]) == 1 else "favorable_add",
            signal_bar_time=bar_text,
            entry_time_utc=entry_time,
        )
        self._shadow_route(opportunity_id, status="consumed", reason="shadow_runner_entry", at=now, consumed=True)
        self._save_state()

    def _get_confirmed_close_deal(self, position_id: int, opened_at_epoch: int) -> Any:
        deal = self.executor.get_position_close_deal(position_id, opened_at_epoch)
        if deal is False and opened_at_epoch > 0:
            deal = self.executor.get_position_close_deal(position_id, 0)
        return deal

    def _sync_strategy(self, strat: dict[str, Any]) -> bool:
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        st = self._st(strat)
        positions = self.executor.get_positions(symbol, int(strat["magic"]))
        if positions is None:
            self._set_sync_block(strat, "positions_unavailable", recoverable=True)
            return False
        queried_orders = self.executor.get_orders(symbol, int(strat["magic"]))
        orders_available = queried_orders is not None
        orders = list(queried_orders or [])
        if not orders_available:
            self._set_sync_block(strat, "orders_unavailable", recoverable=True)
        unexpected_positions = [record for record in positions if not self._owned_position(strat, record)]
        if unexpected_positions:
            self._set_sync_block(
                strat,
                "same_magic_unexpected_position_or_order",
                {"tickets": [int(record.ticket) for record in unexpected_positions], "comments": [str(record.comment or "") for record in unexpected_positions]},
                recoverable=False,
            )
            return False
        unexpected_orders = [record for record in orders if not self._owned_position(strat, record)]
        if unexpected_orders:
            self._set_sync_block(
                strat,
                "same_magic_unexpected_order",
                {"tickets": [int(record.ticket) for record in unexpected_orders], "comments": [str(record.comment or "") for record in unexpected_orders]},
                recoverable=False,
            )
            return False
        position_tickets = [int(getattr(record, "ticket", 0) or 0) for record in positions]
        position_ids = [int(getattr(record, "identifier", 0) or 0) for record in positions]
        order_tickets = [int(getattr(record, "ticket", 0) or 0) for record in orders]
        if (
            any(value <= 0 for value in position_tickets + position_ids + order_tickets)
            or len(position_tickets) != len(set(position_tickets))
            or len(position_ids) != len(set(position_ids))
            or len(order_tickets) != len(set(order_tickets))
        ):
            self._set_sync_block(strat, "duplicate_or_invalid_live_identity", recoverable=False)
            return False
        if clean_sync_block_if_flat(
            symbol_key=strat["id"],
            state=st,
            positions=positions,
            orders=orders if orders_available else None,
            save_state=self._save_state,
            options=self.safety,
            audit=lambda _symbol, event, reason: self._trade_row(event, strat, reason=reason),
            flat_auto_clear_reasons=FLAT_AUTO_CLEAR_SYNC_REASONS,
            confirm_position_absent=self.executor.confirm_position_absent,
            required_flat_confirmations=3 if st.get("sync_block_reason") == "unresolved_open_action" else 2,
        ):
            logging.info("S24 clean sync cleared: %s", strat["id"])
            self._clear_pending_open(strat)
            self._save_state()
        if orders and not unexpected_orders:
            self._set_sync_block(strat, "same_magic_unexpected_order", {"tickets": [int(o.ticket) for o in orders]}, recoverable=False)
            return False
        state_basket = list(st.get("basket") or [])
        shadow_basket = bool(state_basket) and all(pos.get("shadow") is True for pos in state_basket)
        pending_open_id = st.get("pending_open_opportunity_id")
        pending_open_started = st.get("pending_open_started_utc")
        if bool(pending_open_id) != bool(pending_open_started) or (
            pending_open_started is not None
            and (not isinstance(pending_open_started, str) or parse_ts(pending_open_started) is None)
        ):
            self._set_sync_block(strat, "invalid_pending_open_state", recoverable=False)
            return False
        close_retry_after = st.get("close_retry_after_utc")
        try:
            close_reject_count = int(st.get("close_permission_reject_count", 0))
        except (TypeError, ValueError, OverflowError):
            close_reject_count = -1
        if (
            (close_retry_after is not None and (not isinstance(close_retry_after, str) or parse_ts(close_retry_after) is None))
            or close_reject_count < 0
        ):
            self._set_sync_block(strat, "invalid_close_retry_state", recoverable=False)
            return False
        if shadow_basket:
            if positions:
                self._set_sync_block(
                    strat,
                    "live_positions_conflict_with_shadow_state",
                    {"tickets": [int(pos.ticket) for pos in positions]},
                    recoverable=False,
                )
                return False
            return not bool(st.get("sync_block_new_entries"))
        state_tickets = [int(pos.get("ticket") or 0) for pos in state_basket]
        state_ids = [int(pos.get("position_identifier") or 0) for pos in state_basket]
        if (
            any(value <= 0 for value in state_tickets + state_ids)
            or len(state_tickets) != len(set(state_tickets))
            or len(state_ids) != len(set(state_ids))
        ):
            self._set_sync_block(strat, "duplicate_or_invalid_state_identity", recoverable=False)
            return False
        for state_pos in state_basket:
            close_requested = state_pos.get("close_requested", False)
            submission_started = state_pos.get("close_submission_started_utc")
            if not isinstance(close_requested, bool) or (
                submission_started is not None
                and (not isinstance(submission_started, str) or parse_ts(submission_started) is None)
            ):
                self._set_sync_block(strat, "invalid_close_submission_state", {"ticket": state_pos.get("ticket")}, recoverable=False)
                return False
        unresolved_open = bool(st.get("pending_open_opportunity_id"))
        if not state_basket and positions:
            self._set_sync_block(
                strat,
                "live_positions_without_state",
                {"tickets": [int(pos.ticket) for pos in positions]},
                recoverable=False,
            )
            return False
        if not state_basket and unresolved_open:
            self._set_sync_block(
                strat,
                "unresolved_open_action",
                {
                    "opportunity_id": str(st.get("pending_open_opportunity_id") or ""),
                    "started_utc": st.get("pending_open_started_utc"),
                    "live_tickets": [int(pos.ticket) for pos in positions],
                },
                recoverable=False,
            )
            return False
        if not state_basket:
            return not bool(st.get("sync_block_new_entries"))
        if state_basket:
            live_by_id = {int(getattr(pos, "identifier", 0) or pos.ticket): pos for pos in positions}
            state_ids = {int(pos.get("position_identifier") or pos.get("ticket") or 0) for pos in state_basket}
            unexpected_live_ids = set(live_by_id) - state_ids
            if unexpected_live_ids:
                self._set_sync_block(
                    strat,
                    "state_ticket_unowned_or_foreign",
                    {"state_ids": sorted(state_ids), "live_ids": sorted(live_by_id)},
                    recoverable=False,
                )
                return False
            remaining_state: list[dict[str, Any]] = []
            confirmed_deals = []
            for state_pos in state_basket:
                position_id = int(state_pos.get("position_identifier") or state_pos.get("ticket") or 0)
                live_pos = live_by_id.get(position_id)
                if live_pos is not None:
                    if not self._state_matches_live(strat, state_pos, live_pos):
                        self._set_sync_block(strat, "state_position_ownership_mismatch", {"ticket": position_id}, recoverable=False)
                        return False
                    broker_open_epoch = int(getattr(live_pos, "open_time", 0) or 0)
                    if broker_open_epoch <= 0:
                        self._set_sync_block(strat, "confirmed_fill_time_unavailable", {"ticket": position_id}, recoverable=True)
                        return False
                    try:
                        broker_entry_price = float(getattr(live_pos, "open_price", 0.0) or 0.0)
                    except (TypeError, ValueError, OverflowError):
                        broker_entry_price = 0.0
                    if not math.isfinite(broker_entry_price) or broker_entry_price <= 0.0:
                        self._set_sync_block(strat, "confirmed_fill_price_unavailable", {"ticket": position_id}, recoverable=True)
                        return False
                    broker_entry_time = pd.Timestamp(broker_open_epoch, unit="s", tz="UTC")
                    persisted_entry_time = parse_ts(state_pos.get("entry_time_utc"))
                    previous_entry_price = state_pos.get("entry_price")
                    if (
                        persisted_entry_time != broker_entry_time
                        or int(state_pos.get("open_time_epoch") or 0) != broker_open_epoch
                        or not math.isclose(float(previous_entry_price or 0.0), broker_entry_price, rel_tol=0.0, abs_tol=1e-12)
                    ):
                        previous_entry_time = state_pos.get("entry_time_utc")
                        state_pos["entry_time_utc"] = dt_text(broker_entry_time)
                        state_pos["open_time_epoch"] = broker_open_epoch
                        state_pos["entry_price"] = broker_entry_price
                        self._trade_row(
                            "position_lifecycle_recovered",
                            strat,
                            ticket=int(state_pos.get("ticket") or position_id),
                            reason="confirmed_broker_fill_identity_restored",
                            note=(
                                f"previous_entry_time_utc={previous_entry_time};broker_entry_time_utc={dt_text(broker_entry_time)};"
                                f"previous_entry_price={previous_entry_price};broker_entry_price={broker_entry_price}"
                            ),
                        )
                        self._save_state()
                    if (
                        state_pos.get("close_submission_started_utc") is not None
                        and st.get("sync_block_reason") == "market_closed_close_inventory_unconfirmed"
                    ):
                        state_pos["close_submission_started_utc"] = None
                        prior_quote = parse_ts(st.get("last_core_quote_time_utc"))
                        if prior_quote is not None:
                            st["close_retry_after_utc"] = dt_text(
                                prior_quote + pd.Timedelta(seconds=float(self.params.get("time_close_market_closed_retry_seconds", 60.0)))
                            )
                        if orders_available:
                            self._set_sync_block(strat, None)
                        else:
                            self._replace_with_recoverable_sync_block(
                                strat,
                                "orders_unavailable_after_market_closed_recheck",
                                {"ticket": int(state_pos.get("ticket") or 0)},
                            )
                        self._save_state()
                    remaining_state.append(state_pos)
                    continue
                opened_at_epoch = max(0, int(state_pos.get("open_time_epoch") or 0) - 60)
                direct_absence = self.executor.confirm_position_absent(int(state_pos.get("ticket") or 0))
                if direct_absence is not True:
                    self._set_sync_block(strat, "position_absence_unconfirmed", {"ticket": state_pos.get("ticket")}, recoverable=False)
                    return False
                deal = self._get_confirmed_close_deal(position_id, opened_at_epoch)
                if deal is None:
                    self._set_sync_block(strat, "close_deal_query_unavailable", {"ticket": position_id}, recoverable=False)
                    return False
                if deal is False:
                    self._set_sync_block(strat, "close_deal_not_confirmed", {"ticket": position_id}, recoverable=False)
                    return False
                if (
                    int(deal.position_id) != position_id
                    or str(deal.symbol) != symbol
                    or not math.isclose(float(getattr(deal, "exit_volume", 0.0) or 0.0), float(state_pos.get("lot") or 0.0), rel_tol=0.0, abs_tol=1e-9)
                    or int(getattr(deal, "deal_time", 0) or 0) < int(state_pos.get("open_time_epoch") or 0)
                    or not self._state_ownership_proven(strat, state_pos)
                ):
                    self._set_sync_block(
                        strat,
                        "close_deal_ownership_mismatch",
                        {"ticket": position_id, "deal_position_id": int(deal.position_id), "deal_magic": int(deal.magic), "deal_symbol": str(deal.symbol)},
                        recoverable=False,
                    )
                    return False
                confirmed_deals.append(deal)
            if remaining_state:
                newest = max(remaining_state, key=lambda row: int(row.get("open_time_epoch") or 0))
                restored_last_add = float(newest.get("entry_price") or 0.0)
                if not math.isclose(float(st.get("last_add_price") or 0.0), restored_last_add, rel_tol=0.0, abs_tol=1e-12):
                    st["last_add_price"] = restored_last_add
                    self._save_state()
            if confirmed_deals:
                confirmed_deals.sort(
                    key=lambda deal: (
                        int(getattr(deal, "deal_time", 0) or 0),
                        int(getattr(deal, "deal", 0) or 0),
                    )
                )
                reason = str(st.get("pending_close_reason") or "broker_or_external_close_confirmed")
                signal_bar = st.get("pending_close_signal_bar")
                # Keep the complete pre-close basket available until a full
                # close identity has been derived.  _clear_basket_state uses
                # it to persist every originating signal bar.
                if remaining_state:
                    st["basket"] = remaining_state
                state_by_position_id = {
                    int(pos.get("position_identifier") or pos.get("ticket") or 0): pos for pos in state_basket
                }
                closed_basket_id = self._basket_id(strat, state_basket)
                if not remaining_state:
                    closed_sides = {
                        str(state_by_position_id[int(deal.position_id)].get("side"))
                        for deal in confirmed_deals
                    }
                    close_side = next(iter(closed_sides)) if len(closed_sides) == 1 else None
                    close_time = max(
                        pd.Timestamp(int(deal.deal_time), unit="s", tz="UTC")
                        for deal in confirmed_deals
                    )
                    self._clear_basket_state(
                        strat,
                        reason,
                        signal_bar,
                        close_side=close_side,
                        close_time=close_time,
                    )
                    if orders_available:
                        self._set_sync_block(strat, None)
                    else:
                        self._replace_with_recoverable_sync_block(
                            strat,
                            "orders_unavailable_after_confirmed_close",
                            {"closed_position_ids": sorted(int(deal.position_id) for deal in confirmed_deals)},
                        )
                self._save_state()
                for deal in confirmed_deals:
                    closed_state = state_by_position_id[int(deal.position_id)]
                    deal_time_utc = datetime.fromtimestamp(int(deal.deal_time), UTC).isoformat()
                    self._trade_row(
                        "position_close_deal",
                        strat,
                        basket_id=closed_basket_id,
                        ticket=int(closed_state.get("ticket") or 0),
                        position_identifier=int(deal.position_id),
                        deal_id=int(deal.deal),
                        side=str(closed_state.get("side") or ""),
                        lot=float(closed_state.get("lot") or 0.0),
                        entry_price=float(closed_state.get("entry_price") or 0.0),
                        exit_price=float(deal.price),
                        price=float(deal.price),
                        profit=float(deal.net_profit),
                        reason=reason,
                        signal_bar_time=signal_bar,
                        note=(
                            f"deal={int(deal.deal)} deal_time_utc={deal_time_utc} gross={float(deal.profit):.2f} "
                            f"commission={float(deal.commission):.2f} swap={float(deal.swap):.2f} "
                            f"fee={float(deal.fee):.2f} broker_reason={deal.reason}"
                        ),
                    )
                self._trade_row(
                    "position_close_confirmed",
                    strat,
                    basket_id=closed_basket_id,
                    profit=sum(float(deal.net_profit) for deal in confirmed_deals),
                    reason=reason,
                    signal_bar_time=signal_bar,
                )
                if unresolved_open:
                    self._set_sync_block(
                        strat,
                        "unresolved_open_action",
                        {
                            "opportunity_id": str(st.get("pending_open_opportunity_id") or ""),
                            "started_utc": st.get("pending_open_started_utc"),
                            "live_tickets": [int(pos.ticket) for pos in positions],
                        },
                        recoverable=False,
                    )
                    self._save_state()
                    return bool(remaining_state)
                return not bool(st.get("sync_block_new_entries"))
            if unresolved_open:
                self._set_sync_block(
                    strat,
                    "unresolved_open_action",
                    {
                        "opportunity_id": str(st.get("pending_open_opportunity_id") or ""),
                        "started_utc": st.get("pending_open_started_utc"),
                        "live_tickets": [int(pos.ticket) for pos in positions],
                    },
                    recoverable=False,
                )
                self._save_state()
                return bool(remaining_state) and len(remaining_state) == len(state_basket)
            if remaining_state and len(remaining_state) == len(state_basket) and orders_available and not orders:
                if not (
                    not self.live_enabled
                    and st.get("sync_block_reason") == "live_disabled_with_owned_inventory"
                ):
                    clear_recoverable_sync_block_after_clean_sync(
                        symbol_key=strat["id"],
                        state=st,
                        save_state=self._save_state,
                        options=self.safety,
                        audit=lambda symbol_key, event, reason: self._trade_row(event, strat, reason=reason, note=symbol_key),
                    )
            if (
                remaining_state
                and len(remaining_state) == len(state_basket)
                and not orders_available
                and st.get("sync_block_reason") == "orders_unavailable"
                and bool(st.get("sync_block_recoverable"))
            ):
                return True
        return not bool(st.get("sync_block_new_entries"))

    def _close_basket(self, strat: dict[str, Any], reason: str, price_row: pd.Series, pnl: float) -> None:
        st = self._st(strat)
        broker_backed = bool(st.get("basket")) and all(pos.get("shadow") is False for pos in st["basket"])
        if broker_backed and not self.live_enabled:
            st["pending_close_reason"] = reason
            st["pending_close_signal_bar"] = str(price_row.name)
            self._set_sync_block(
                strat,
                "live_disabled_with_owned_inventory",
                {"tickets": [int(pos.get("ticket") or 0) for pos in st["basket"]]},
                recoverable=True,
            )
            self._trade_row(
                "basket_close_deferred",
                strat,
                profit=round(float(pnl), 2),
                reason="live_disabled_with_owned_inventory",
                signal_bar_time=str(price_row.name),
            )
            self._save_state()
            return
        if broker_backed:
            # Atomic bridge guards prove that CTrade was never reached.  The
            # submission marker is therefore cleared below, while this durable
            # block prevents the same unsafe request from being retried.
            if st.get("sync_block_reason") == "atomic_close_guard_rejected":
                self._save_state()
                return
            symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
            fresh_info = self.executor.get_symbol_info(symbol)
            quote_time, quote_error = self._validated_core_quote_time(strat, fresh_info)
            if quote_time is None:
                self._set_sync_block(strat, "pre_close_quote_clock_invalid", {"cause": quote_error}, recoverable=True)
                self._save_state()
                return
            st["last_core_quote_time_utc"] = dt_text(quote_time)
            retry_after = parse_ts(st.get("close_retry_after_utc"))
            if retry_after is not None and quote_time < retry_after:
                self._save_state()
                return
            if not self._time_close_spread_ready(strat, reason, quote_time, fresh_info, str(price_row.name)):
                self._save_state()
                return
            for pos in list(st["basket"]):
                ticket = int(pos.get("ticket") or 0)
                position_id = int(pos.get("position_identifier") or ticket)
                live_pos = self.executor.get_position(ticket)
                if live_pos is None:
                    self._set_sync_block(strat, "position_query_unavailable_before_close", {"ticket": ticket}, recoverable=False)
                    self._save_state()
                    return
                if live_pos is False or not self._owned_position(strat, live_pos) or int(getattr(live_pos, "identifier", 0) or live_pos.ticket) != position_id:
                    self._set_sync_block(strat, "state_ticket_unowned_or_foreign", {"ticket": ticket, "position_identifier": position_id}, recoverable=False)
                    self._save_state()
                    return
                if not self._state_matches_live(strat, pos, live_pos):
                    self._set_sync_block(strat, "state_position_ownership_mismatch_before_close", {"ticket": ticket}, recoverable=False)
                    self._save_state()
                    return
            st["pending_close_reason"] = reason
            st["pending_close_signal_bar"] = str(price_row.name)
            self._save_state()
            submitted_any = False
            for pos in list(st["basket"]):
                if pos.get("close_requested") is True or pos.get("close_submission_started_utc") is not None:
                    continue
                ticket = int(pos.get("ticket") or 0)
                position_id = int(pos.get("position_identifier") or ticket)
                pos["close_submission_started_utc"] = dt_text(quote_time)
                self._save_state()
                close_result = self.executor.close_position(
                    ticket,
                    int(self.params.get("deviation_points", 50)),
                    expected_login=int(MT5_LOGIN),
                    expected_server=str(MT5_SERVER),
                    expected_symbol=symbol,
                    expected_magic=int(strat["magic"]),
                    expected_comment=str(pos.get("owner_comment") or ""),
                    expected_identifier=position_id,
                    expected_type=ORDER_TYPE_BUY if str(pos.get("side")) == "LONG" else ORDER_TYPE_SELL,
                    expected_volume=float(pos.get("lot") or 0.0),
                )
                if not close_result:
                    close_status = str(getattr(close_result, "status", "FAILED"))
                    if close_status == "MARKET_CLOSED":
                        self._reset_time_close_spread_state(strat)
                        live_after = self.executor.get_position(ticket)
                        if live_after is None or live_after is False or not self._state_matches_live(strat, pos, live_after):
                            self._set_sync_block(
                                strat,
                                "market_closed_close_inventory_unconfirmed",
                                {"ticket": ticket, "position_identifier": position_id},
                                recoverable=False,
                            )
                            self._save_state()
                            return
                        pos["close_submission_started_utc"] = None
                        st["close_retry_after_utc"] = dt_text(
                            quote_time + pd.Timedelta(seconds=float(self.params.get("time_close_market_closed_retry_seconds", 60.0)))
                        )
                        self._save_state()
                        self._trade_row("basket_close_deferred", strat, ticket=ticket, reason=close_status, signal_bar_time=str(price_row.name))
                        return
                    if close_status == "TRADE_PERMISSION_GUARD":
                        pos["close_submission_started_utc"] = None
                        st["close_retry_after_utc"] = dt_text(quote_time + pd.Timedelta(seconds=60))
                        st["close_permission_reject_count"] = int(st.get("close_permission_reject_count", 0)) + 1
                        if st["close_permission_reject_count"] >= 3:
                            self._set_sync_block(
                                strat,
                                "core_trade_permission_rejected_repeatedly",
                                {"count": st["close_permission_reject_count"]},
                                recoverable=False,
                            )
                        self._save_state()
                        self._trade_row("basket_close_deferred", strat, ticket=ticket, reason=close_status, signal_bar_time=str(price_row.name))
                        return
                    if close_status in {"ACCOUNT_IDENTITY_GUARD", "ACCOUNT_MODE_GUARD", "POSITION_OWNERSHIP_GUARD", "CLOSE_POLICY_GUARD", "INVALID_REQUEST"}:
                        pos["close_submission_started_utc"] = None
                        block_reason = "atomic_close_guard_rejected"
                    elif close_status == "IPC_NOT_PUBLISHED":
                        block_reason = "core_ipc_namespace_not_clean"
                    else:
                        block_reason = "live_time_close_unconfirmed"
                    self._set_sync_block(strat, block_reason, {"ticket": ticket, "status": close_status}, recoverable=False)
                    self._save_state()
                    return
                if not math.isclose(float(close_result.lot), float(pos.get("lot") or 0.0), rel_tol=0.0, abs_tol=1e-9):
                    self._set_sync_block(strat, "close_response_identity_invalid", {"ticket": ticket}, recoverable=False)
                    self._save_state()
                    return
                pos["close_submission_started_utc"] = None
                pos["close_requested"] = True
                submitted_any = True
                self._save_state()
                self._trade_row(
                    "position_close_requested",
                    strat,
                    ticket=ticket,
                    position_identifier=position_id,
                    deal_id=int(getattr(close_result, "deal_id", 0) or 0),
                    side=str(pos.get("side") or ""),
                    lot=float(pos.get("lot") or 0.0),
                    entry_price=float(pos.get("entry_price") or 0.0),
                    exit_price=float(getattr(close_result, "close_price", 0.0) or 0.0),
                    price=float(getattr(close_result, "close_price", 0.0) or 0.0),
                    profit=round(float(pnl), 2),
                    reason=reason,
                    signal_bar_time=str(price_row.name),
                    executable_at=dt_text(quote_time),
                )
            if submitted_any:
                st["close_permission_reject_count"] = 0
                st["close_retry_after_utc"] = None
                self._save_state()
                self._trade_row("basket_close_requested", strat, profit=round(float(pnl), 2), reason=reason, signal_bar_time=str(price_row.name))
            else:
                self._save_state()
            return
        self._trade_row("basket_close", strat, profit=round(float(pnl), 2), reason=reason, signal_bar_time=str(price_row.name))
        close_bar = parse_ts(price_row.name)
        close_time = close_bar + pd.Timedelta(minutes=1) if close_bar is not None else utc_now()
        self._clear_basket_state(strat, reason, str(price_row.name), close_time=close_time)
        self._save_state()

    def _open_entry(self, strat: dict[str, Any], side: str, price_row: pd.Series, info: Any, note: str = "") -> None:
        st = self._st(strat)
        if not self.live_enabled and not self.shadow_enabled:
            st["last_signal_bar"] = str(price_row.name)
            signal_bar = parse_ts(price_row.name)
            if signal_bar is not None:
                st["last_consumed_signal_bar"] = dt_text(signal_bar)
            self._trade_row(
                "entry_skip",
                strat,
                side=side,
                reason="execution_disabled",
                signal_bar_time=str(price_row.name),
                note=note,
            )
            self._save_state()
            return
        signal_bar = parse_ts(price_row.name)
        last_close = parse_ts(st.get("last_closed_at_utc"))
        if (
            signal_bar is not None
            and last_close is not None
            and st.get("last_closed_side") == side
            and signal_bar + pd.Timedelta(minutes=1) <= last_close
        ):
            self._trade_row(
                "entry_skip",
                strat,
                reason="known_same_direction_signal_after_close",
                signal_bar_time=str(price_row.name),
            )
            st["last_consumed_signal_bar"] = dt_text(signal_bar)
            self._save_state()
            return
        if note != "reverse_after_stop" and st.get("last_closed_signal_bar") == str(price_row.name):
            self._trade_row("entry_skip", strat, reason="same_bar_reentry_after_close", signal_bar_time=str(price_row.name))
            return
        existing_modes = {bool(pos.get("shadow")) for pos in st.get("basket", [])}
        requested_shadow = self.shadow_enabled
        if existing_modes and existing_modes != {requested_shadow}:
            self._trade_row(
                "entry_skip",
                strat,
                reason="execution_mode_transition_with_open_basket",
                signal_bar_time=str(price_row.name),
            )
            self._save_state()
            return
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        digits = int(self.params.get("price_digits", 2))
        lot = float(strat.get("lot", self.params.get("default_lot", 0.01)))
        ask = float(getattr(info, "ask", price_row.get("AskOpen", price_row["Open"])))
        bid = float(getattr(info, "bid", price_row["Open"]))
        entry_price = normalize_price(ask if side == "LONG" else bid, digits)
        ticket = None
        confirmed = None
        broker_entry_time: pd.Timestamp | None = None
        entry_opportunity_id = f"s24-signal:{strat['id']}:{dt_text(parse_ts(price_row.name) or pd.Timestamp(utc_now()))}:{side}"
        if self.live_enabled:
            fresh_info = self.executor.get_symbol_info(symbol)
            quote_time, quote_error = self._validated_core_quote_time(strat, fresh_info)
            contract_error = self._core_broker_contract_error(strat, fresh_info) if fresh_info is not None else "symbol_info_unavailable"
            if quote_time is None or contract_error is not None:
                self._set_sync_block(
                    strat,
                    "pre_open_contract_invalid",
                    {"quote_error": quote_error, "contract_error": contract_error},
                    recoverable=True,
                )
                self._save_state()
                return
            signal_bar = parse_ts(price_row.name)
            if signal_bar is None:
                self._set_sync_block(strat, "pre_open_signal_clock_invalid", recoverable=False)
                self._save_state()
                return
            entry_due = signal_bar + pd.Timedelta(minutes=1)
            entry_expiry = entry_due + pd.Timedelta(minutes=float(self.params.get("max_signal_delay_minutes", 2.0)))
            if quote_time < entry_due or quote_time > entry_expiry:
                st["entry_retry_after_utc"] = None
                st["entry_retry_signal_bar"] = None
                st["entry_retry_reason"] = None
                self._trade_row("entry_skip", strat, reason="signal_expired_at_submit", signal_bar_time=str(price_row.name))
                self._save_state()
                return
            retry_bar = parse_ts(st.get("entry_retry_signal_bar"))
            retry_after = parse_ts(st.get("entry_retry_after_utc"))
            if retry_bar is not None and retry_bar != signal_bar:
                st["entry_retry_after_utc"] = None
                st["entry_retry_signal_bar"] = None
                st["entry_retry_reason"] = None
                retry_after = None
            elif retry_after is not None and quote_time < retry_after:
                st["last_evaluated_bar"] = None
                self._trade_row("entry_deferred", strat, reason="entry_retry_cooldown", signal_bar_time=str(price_row.name))
                self._save_state()
                return
            st["last_core_quote_time_utc"] = dt_text(quote_time)
            ask = float(fresh_info.ask)
            bid = float(fresh_info.bid)
            point = float(self.params.get("point_size", 0.001))
            if not all(math.isfinite(value) and value > 0.0 for value in (ask, bid, point)) or ask < bid:
                self._set_sync_block(strat, "pre_open_quote_invalid", recoverable=True)
                self._save_state()
                return
            if (ask - bid) / point > float(self.params.get("max_entry_spread_points", 300.0)):
                self._trade_row("entry_skip", strat, reason="spread_guard_at_submit", signal_bar_time=str(price_row.name))
                self._save_state()
                return
            positions_before = self.executor.get_positions(symbol, int(strat["magic"]))
            orders_before = self.executor.get_orders(symbol, int(strat["magic"]))
            if positions_before is None or orders_before is None:
                self._set_sync_block(strat, "pre_open_inventory_unavailable", recoverable=True)
                self._save_state()
                return
            if orders_before or any(not self._owned_position(strat, pos) for pos in positions_before):
                self._set_sync_block(strat, "pre_open_inventory_mismatch", recoverable=False)
                self._save_state()
                return
            before_tickets = [int(getattr(pos, "ticket", 0) or 0) for pos in positions_before]
            before_position_ids = [int(getattr(pos, "identifier", 0) or 0) for pos in positions_before]
            state_basket = list(st.get("basket") or [])
            state_by_identifier = {
                int(pos.get("position_identifier") or 0): pos
                for pos in state_basket
                if isinstance(pos, dict)
            }
            if (
                any(value <= 0 for value in before_tickets + before_position_ids)
                or len(before_tickets) != len(set(before_tickets))
                or len(before_position_ids) != len(set(before_position_ids))
                or len(state_by_identifier) != len(state_basket)
                or len(positions_before) != len(state_basket)
                or any(
                    identifier not in state_by_identifier
                    or not self._state_matches_live(strat, state_by_identifier[identifier], pos)
                    for identifier, pos in zip(before_position_ids, positions_before)
                )
            ):
                self._set_sync_block(strat, "pre_open_inventory_identity_invalid", recoverable=False)
                self._save_state()
                return
            before_ids = set(before_position_ids)
            pending_open_id = f"s24-open:{strat['id']}:{dt_text(parse_ts(price_row.name) or pd.Timestamp(utc_now()))}:{side}:{len(st.get('basket') or []) + 1}"
            entry_opportunity_id = pending_open_id
            order_comment = f"{strat['comment_prefix']}:{hashlib.sha256(pending_open_id.encode('utf-8')).hexdigest()[:10]}"
            st["pending_open_opportunity_id"] = pending_open_id
            st["pending_open_started_utc"] = dt_text(quote_time)
            self._save_state()
            order_type = ORDER_TYPE_BUY if side == "LONG" else ORDER_TYPE_SELL
            ticket = self.executor.open_position(
                symbol,
                order_type,
                lot,
                0.0,
                0.0,
                deviation=int(self.params.get("deviation_points", 50)),
                magic=int(strat["magic"]),
                comment=order_comment,
                digits=digits,
                expected_login=int(MT5_LOGIN),
                expected_server=str(MT5_SERVER),
                expected_owned_positions=len(positions_before),
            )
            if ticket is None:
                error = str(getattr(self.executor, "last_order_error", None) or "UNKNOWN_OPEN_FAILURE")
            else:
                error = ""
            positions = self.executor.get_positions(symbol, int(strat["magic"]))
            orders = self.executor.get_orders(symbol, int(strat["magic"]))
            if positions is None or orders is None:
                self._set_sync_block(
                    strat,
                    "unresolved_open_action",
                    {"opportunity_id": pending_open_id, "ticket": int(ticket or 0), "error": error, "reason": "inventory_unavailable_after_open"},
                    recoverable=False,
                )
                self._save_state()
                return
            owned = [pos for pos in positions if self._owned_position(strat, pos)]
            post_tickets = [int(getattr(pos, "ticket", 0) or 0) for pos in owned]
            post_ids = [int(getattr(pos, "identifier", 0) or 0) for pos in owned]
            if (
                orders
                or len(owned) != len(positions)
                or any(value <= 0 for value in post_tickets + post_ids)
                or len(post_tickets) != len(set(post_tickets))
                or len(post_ids) != len(set(post_ids))
            ):
                self._set_sync_block(strat, "post_open_inventory_identity_invalid", {"positions": len(positions), "orders": len(orders)}, recoverable=False)
                self._save_state()
                return
            new_owned = [pos for pos in owned if int(getattr(pos, "identifier", 0) or 0) not in before_ids]
            if ticket is not None:
                expected_identifier = int(getattr(self.executor, "last_open_identifier", 0) or 0)
                matches = [
                    pos for pos in new_owned
                    if int(pos.ticket) == int(ticket)
                    and int(getattr(pos, "identifier", 0) or 0) == expected_identifier
                    and str(getattr(pos, "comment", "") or "") == order_comment
                ]
                if len(matches) != 1 or len(owned) != len(positions_before) + 1:
                    self._set_sync_block(
                        strat,
                        "unresolved_open_action",
                        {"opportunity_id": pending_open_id, "ticket": int(ticket), "matches": len(matches), "reason": "open_success_position_not_confirmed"},
                        recoverable=False,
                    )
                    self._save_state()
                    return
                confirmed = matches[0]
            else:
                no_fill_retcode = self._core_open_no_fill_retcode(error)
                definitive_no_fill = error in {
                    "INVALID_OPEN_REQUEST", "OPEN_POLICY_GUARD", "ERR|BAD_OPEN_GUARD",
                    "ERR|OPEN_INVENTORY_GUARD", "ERR|OPEN_INVENTORY_QUERY", "ERR|OPEN_ORDER_QUERY",
                    "ERR|ACCOUNT_IDENTITY_GUARD", "ERR|ACCOUNT_MODE_GUARD", "ERR|TRADE_PERMISSION_GUARD",
                    "ERR|SYMBOL_ADMISSION_GUARD", "ERR|MARGIN_ADMISSION_GUARD",
                } or self._core_open_definitive_no_fill(error)
                if definitive_no_fill and not new_owned and len(owned) == len(positions_before):
                    self._clear_pending_open(strat)
                    retry_reason = (
                        "market_closed" if no_fill_retcode == 10018 else
                        "trade_permission" if no_fill_retcode in {10026, 10027} or error == "ERR|TRADE_PERMISSION_GUARD" else
                        None
                    )
                    retry_at = quote_time + pd.Timedelta(seconds=60)
                    atomic_guard_error = error in {
                        "INVALID_OPEN_REQUEST", "OPEN_POLICY_GUARD", "ERR|BAD_OPEN_GUARD",
                        "ERR|OPEN_INVENTORY_GUARD", "ERR|OPEN_INVENTORY_QUERY", "ERR|OPEN_ORDER_QUERY",
                        "ERR|ACCOUNT_IDENTITY_GUARD", "ERR|ACCOUNT_MODE_GUARD",
                        "ERR|SYMBOL_ADMISSION_GUARD", "ERR|MARGIN_ADMISSION_GUARD",
                    }
                    if atomic_guard_error:
                        st["entry_retry_after_utc"] = None
                        st["entry_retry_signal_bar"] = None
                        st["entry_retry_reason"] = None
                        st["entry_permission_reject_count"] = 0
                        self._set_sync_block(
                            strat,
                            "core_open_atomic_guard_rejected",
                            {"error": error, "opportunity_id": pending_open_id},
                            recoverable=False,
                        )
                        self._save_state()
                        self._trade_row(
                            "entry_rejected",
                            strat,
                            reason="core_open_atomic_guard_rejected",
                            signal_bar_time=str(price_row.name),
                            note=error,
                        )
                        return
                    if retry_reason == "trade_permission":
                        st["entry_permission_reject_count"] = int(st.get("entry_permission_reject_count", 0)) + 1
                    elif retry_reason is not None:
                        st["entry_permission_reject_count"] = 0
                    if retry_reason == "trade_permission" and st["entry_permission_reject_count"] >= 3:
                        st["entry_retry_after_utc"] = None
                        st["entry_retry_signal_bar"] = None
                        st["entry_retry_reason"] = None
                        self._set_sync_block(
                            strat,
                            "core_entry_trade_permission_rejected_repeatedly",
                            {"count": st["entry_permission_reject_count"]},
                            recoverable=False,
                        )
                        self._save_state()
                        self._trade_row("entry_deferred", strat, reason=retry_reason, signal_bar_time=str(price_row.name), note=error)
                        return
                    if retry_reason is not None and retry_at <= entry_expiry:
                        st["entry_retry_after_utc"] = dt_text(retry_at)
                        st["entry_retry_signal_bar"] = dt_text(signal_bar)
                        st["entry_retry_reason"] = retry_reason
                        st["last_evaluated_bar"] = None
                        self._save_state()
                        self._trade_row("entry_deferred", strat, reason=retry_reason, signal_bar_time=str(price_row.name), note=error)
                    else:
                        st["entry_retry_after_utc"] = None
                        st["entry_retry_signal_bar"] = None
                        st["entry_retry_reason"] = None
                        st["entry_permission_reject_count"] = 0
                        self._save_state()
                        self._trade_row("entry_skip", strat, reason="definitive_no_fill", signal_bar_time=str(price_row.name), note=error)
                    return
                reason = "ambiguous_open_result_positions" if new_owned else "ambiguous_open_result"
                self._set_sync_block(
                    strat,
                    "unresolved_open_action",
                    {"opportunity_id": pending_open_id, "tickets": [int(pos.ticket) for pos in new_owned], "error": error, "reason": reason},
                    recoverable=False,
                )
                self._save_state()
                return
            entry_price = float(confirmed.open_price)
            broker_open_epoch = int(getattr(confirmed, "open_time", 0) or 0)
            if broker_open_epoch > 0:
                broker_entry_time = pd.Timestamp(broker_open_epoch, unit="s", tz="UTC")
        persisted_entry_time = (
            broker_entry_time
            if broker_entry_time is not None
            else (parse_ts(price_row.name) or pd.Timestamp(utc_now())) + pd.Timedelta(minutes=1)
        )
        st["basket"].append(
            {
                "ticket": ticket,
                "position_identifier": int(getattr(confirmed, "identifier", 0) or ticket or 0) if confirmed is not None else 0,
                "side": side,
                "lot": lot,
                "entry_price": entry_price,
                "entry_time_utc": dt_text(persisted_entry_time),
                "open_time_epoch": int(getattr(confirmed, "open_time", 0) or 0) if confirmed is not None else 0,
                "owner_symbol": symbol,
                "owner_magic": int(strat["magic"]),
                "owner_comment": str(getattr(confirmed, "comment", "") or strat["comment_prefix"]) if confirmed is not None else str(strat["comment_prefix"]),
                "signal_bar_time": dt_text(parse_ts(price_row.name)),
                "close_submission_started_utc": None,
                "close_requested": False,
                "shadow": self.shadow_enabled,
            }
        )
        if len(st["basket"]) == 1:
            st["basket_peak_pnl_usd"] = None
        st["last_add_price"] = entry_price
        st["last_signal_bar"] = str(price_row.name)
        st["last_exit_evaluated_bar"] = dt_text(parse_ts(price_row.name) or pd.Timestamp(utc_now()))
        if self.live_enabled:
            self._clear_pending_open(strat)
            st["entry_retry_after_utc"] = None
            st["entry_retry_signal_bar"] = None
            st["entry_retry_reason"] = None
            st["entry_permission_reject_count"] = 0
            if broker_entry_time is None:
                self._set_sync_block(
                    strat,
                    "confirmed_fill_time_unavailable",
                    {"ticket": int(ticket or 0), "fallback_entry_time_utc": dt_text(persisted_entry_time)},
                    recoverable=True,
                )
        self._save_state()
        self._trade_row(
            "entry",
            strat,
            opportunity_id=entry_opportunity_id,
            ticket=ticket or "",
            position_identifier=int(getattr(confirmed, "identifier", 0) or ticket or 0) if confirmed is not None else "",
            deal_id=int(getattr(self.executor, "last_open_deal", 0) or 0) if confirmed is not None else "",
            side=side,
            lot=lot,
            entry_price=entry_price,
            price=entry_price,
            signal_bar_time=str(price_row.name),
            executable_at=dt_text(broker_entry_time) if broker_entry_time is not None else dt_text(persisted_entry_time),
            note=note,
        )

    def _run_strategy(self, strat: dict[str, Any], bars: pd.DataFrame, info: Any) -> None:
        st = self._st(strat)
        if not self._sync_strategy(strat):
            self._trade_row("entry_skip", strat, reason=st.get("sync_block_reason"), note="sync_block")
            self._save_state()
            return
        if len(bars) < 2:
            return
        now = bars.iloc[-1]
        now_bar = parse_ts(now.name)
        if now_bar is None:
            self._set_sync_block(strat, "signal_bar_time_invalid", {"bar_time": str(now.name)}, recoverable=True)
            self._save_state()
            return
        bid = float(getattr(info, "bid", now["Close"]))
        ask = float(getattr(info, "ask", now.get("AskOpen", now["Open"])))
        exit_clock = str(strat.get("exit_clock", "confirmed_m1"))
        evaluate_exit = exit_clock == "poll" or st.get("last_exit_evaluated_bar") != dt_text(now_bar)
        if st["basket"] and evaluate_exit:
            st["last_exit_evaluated_bar"] = dt_text(now_bar)
            pnl = self._basket_pnl(strat, bid, ask)
            entry_times = [parse_ts(pos.get("entry_time_utc")) for pos in st["basket"]]
            valid_entry_times = [ts for ts in entry_times if ts is not None]
            if not valid_entry_times:
                self._set_sync_block(strat, "state_entry_time_invalid", recoverable=False)
                self._save_state()
                return
            held = max(0, int((now_bar - min(valid_entry_times)).total_seconds() // 60))
            previous_peak = st.get("basket_peak_pnl_usd")
            peak = float(pnl) if previous_peak is None else max(float(previous_peak), float(pnl))
            st["basket_peak_pnl_usd"] = peak
            reason = str(st.get("pending_close_reason") or "") or None
            if reason is None and pnl >= float(strat["basket_target_usd"]):
                reason = "basket_target"
            elif reason is None and pnl <= -float(strat["basket_stop_usd"]):
                reason = "basket_stop"
            elif reason is None and int(strat.get("failure_to_progress_bars", 0)) > 0 and held >= int(strat["failure_to_progress_bars"]) and peak < float(strat.get("failure_to_progress_peak_usd", 0.0)):
                reason = "failure_to_progress"
            elif reason is None and held >= int(strat["max_hold_bars"]):
                reason = "max_hold"
            if reason:
                self._close_basket(strat, reason, now, pnl)
                return
        if st.get("last_evaluated_bar") == dt_text(now_bar):
            return
        st["last_evaluated_bar"] = dt_text(now_bar)
        entry_block = self._entry_submission_block_reason(strat)
        if entry_block:
            self._trade_row("entry_skip", strat, reason=entry_block, note="final_open_guard")
            self._save_state()
            return
        side, signal_reason = self._signal_decision(now, strat)
        outcome = "signal" if side else (
            "not_evaluated"
            if signal_reason == "outside_session" or signal_reason.startswith("entry_routing_")
            else "no_signal"
        )
        st["last_decision"] = {
            "signal_bar_time": dt_text(now_bar),
            "outcome": outcome,
            "reason": signal_reason,
            "side": side,
        }
        self._trade_row(
            "strategy_decision",
            strat,
            side=side or "",
            reason=signal_reason,
            signal_bar_time=dt_text(now_bar),
            note=f"outcome={outcome}",
        )
        self._save_state()
        if not side:
            return
        stale = stale_signal_decision(
            str(now.name),
            timeframe_hours=1.0 / 60.0,
            max_delay_minutes=float(self.params.get("max_signal_delay_minutes", 2.0)),
            options=self.safety,
        )
        if stale.stale:
            st["last_signal_bar"] = str(now.name)
            self._trade_row("entry_skip", strat, reason="stale_signal_skip", signal_bar_time=str(now.name), note=f"entry_due={stale.entry_due_utc} latest={stale.latest_allowed_utc}")
            self._save_state()
            return
        cooldown_until = parse_ts(st.get("cooldown_until_utc"))
        if cooldown_until is not None and now_bar < cooldown_until:
            self._trade_row("entry_skip", strat, reason="cooldown", signal_bar_time=str(now.name))
            return
        if len(st["basket"]) >= int(strat["max_positions"]):
            return
        if st["basket"]:
            if any(p["side"] != side for p in st["basket"]):
                return
            last_add = st.get("last_add_price")
            atr30 = float(now["atr30"])
            if last_add is None or not math.isfinite(atr30):
                return
            favorable = (side == "LONG" and float(now["Close"]) >= float(last_add) + float(strat["add_atr"]) * atr30) or (
                side == "SHORT" and float(now["Close"]) <= float(last_add) - float(strat["add_atr"]) * atr30
            )
            if not favorable:
                return
        self._open_entry(strat, side, now, info)

    def run_once(self) -> None:
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        info = self.executor.get_symbol_info(symbol)
        runtime_quote_error = "symbol_info_unavailable" if info is None else self._runtime_info_clock_error(info)
        if runtime_quote_error is not None:
            v206_snapshot = copy.deepcopy(self.state.get("v206", default_v206_state()))
            try:
                self.v206_lane.reconcile_without_quote(runtime_quote_error)
            except Exception as exc:
                logging.exception("S24 v206 quote-less reconciliation contained")
                self._contain_v206_poll_exception(
                    v206_snapshot,
                    reason="v206_quote_less_reconciliation_exception",
                    exc=exc,
                )
            for strat in self.params["strategies"]:
                if bool(strat.get("enabled", True)):
                    self._sync_strategy(strat)
                    self._set_sync_block(
                        strat,
                        "symbol_info_failed" if info is None else "runtime_quote_clock_invalid",
                        {"cause": runtime_quote_error},
                        recoverable=True,
                    )
            self._save_state()
            return
        v206_snapshot = copy.deepcopy(self.state.get("v206", default_v206_state()))
        try:
            self.v206_lane.run_once(info)
        except Exception as exc:
            logging.exception("S24 v206 poll contained; existing strategy paths continue")
            self._contain_v206_poll_exception(
                v206_snapshot,
                reason="v206_poll_exception",
                exc=exc,
            )
            self._save_state()
        try:
            self.shadow_observer.observe_quote(
                at=self._quote_time_utc(info),
                bid=float(getattr(info, "bid", 0.0)),
                ask=float(getattr(info, "ask", 0.0)),
            )
        except Exception as exc:
            logging.exception("S24 passive shadow markout observation failed; core execution is unchanged")
            self.shadow_observer.enabled = False
            self.passive_shadow_init_errors["opportunity_observer_runtime"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
        bars = self._get_m1()
        if bars is None or bars.empty:
            self._manage_core_without_history(info)
            return
        point = float(self.params.get("point_size", 0.01))
        current_spread_points = max(0.0, (float(getattr(info, "ask", 0.0)) - float(getattr(info, "bid", 0.0))) / point)
        bars["spread_points"] = current_spread_points
        for strat in self.params["strategies"]:
            if bool(strat.get("enabled", True)):
                if self.passive_shadow_runner_enabled:
                    shadow_runner_snapshot = copy.deepcopy(self._st(strat)["shadow_runner"])
                    try:
                        self._run_shadow_runner(strat, bars, info)
                    except Exception as exc:
                        logging.exception("S24 passive shadow runner poll failed; lane disabled and core execution retained")
                        self._st(strat)["shadow_runner"] = shadow_runner_snapshot
                        self.passive_shadow_runner_enabled = False
                        self.passive_shadow_init_errors["shadow_runner_runtime"] = {
                            "type": type(exc).__name__,
                            "message": str(exc),
                        }
                self._run_strategy(strat, bars, info)
        now = time.time()
        if now - self._last_status_log >= float(self.params.get("status_log_interval_seconds", 60)):
            logging.info(
                "S24 status: live=%s shadow=%s strategies=%s runner_shadow=%s",
                self.live_enabled,
                self.shadow_enabled,
                {s["id"]: len(self._st(s)["basket"]) for s in self.params["strategies"]},
                {s["id"]: len(self._st(s)["shadow_runner"]["basket"]) for s in self.params["strategies"]},
            )
            self._last_status_log = now

    def _manage_core_without_history(self, info: Any) -> None:
        """Reconcile inventory and retry only an already-persisted close intent."""
        for strat in self.params["strategies"]:
            if not bool(strat.get("enabled", True)):
                continue
            st = self._st(strat)
            synced = self._sync_strategy(strat)
            quote_time, quote_error = self._validated_core_quote_time(strat, info)
            if quote_time is not None:
                signal_bar = quote_time.floor("min") - pd.Timedelta(minutes=1)
                receipt = {
                    "signal_bar_time": dt_text(signal_bar),
                    "outcome": "not_evaluated_data_unavailable",
                    "reason": "m1_bars_unavailable",
                    "side": None,
                }
                if st.get("last_decision") != receipt:
                    st["last_decision"] = receipt
                    self._trade_row(
                        "strategy_decision",
                        strat,
                        reason="m1_bars_unavailable",
                        signal_bar_time=dt_text(signal_bar),
                        note="outcome=not_evaluated_data_unavailable",
                    )
                    self._save_state()
            reason = st.get("pending_close_reason")
            if not synced or not self.live_enabled or not st.get("basket") or not isinstance(reason, str) or not reason:
                continue
            bid = float(getattr(info, "bid", 0.0) or 0.0)
            ask = float(getattr(info, "ask", 0.0) or 0.0)
            if quote_time is None or not all(math.isfinite(value) and value > 0.0 for value in (bid, ask)) or ask < bid:
                self._set_sync_block(
                    strat,
                    "history_outage_close_quote_invalid",
                    {"quote_error": quote_error},
                    recoverable=True,
                )
                self._save_state()
                continue
            price_row = pd.Series({"Open": bid, "Close": bid, "AskOpen": ask}, name=quote_time)
            self._close_basket(strat, reason, price_row, self._basket_pnl(strat, bid, ask))


class FakeDM:
    def __init__(self, *_: Any):
        pass

    def connect(self) -> bool:
        return True

    def get_historical_data(self, *_: Any, **__: Any) -> pd.DataFrame:
        idx = pd.date_range("2026-01-01 12:00:00", periods=160, freq="1min", tz="UTC")
        close = pd.Series([2000.0 + i * 0.4 for i in range(160)], index=idx)
        high = close + 0.2
        low = close - 0.2
        low.iloc[-30:] = close.iloc[-30:] - 0.8
        return pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close, "AskOpen": close + 0.03, "Volume": 10}, index=idx)


class FakeExecutor:
    def __init__(self, *, positions: list[Any] | None = None, orders: list[Any] | None = None, margin_mode: int = HEDGING_MARGIN_MODE):
        self.positions = [] if positions is None else positions
        self.orders = [] if orders is None else orders
        self.margin_mode = margin_mode
        self.last_order_error = None

    def get_bridge_capabilities(self) -> dict[str, Any]:
        return {"name": EXPECTED_BRIDGE_NAME, "version": EXPECTED_BRIDGE_VERSION, "commands": set(REQUIRED_SHARED_ACCOUNT_COMMANDS)}

    def get_account_info(self) -> dict[str, Any]:
        return {
            "margin_mode": self.margin_mode,
            "margin_mode_name": "RETAIL_HEDGING" if self.margin_mode == HEDGING_MARGIN_MODE else "RETAIL_NETTING",
            "login": MT5_LOGIN,
            "server": MT5_SERVER,
        }

    def get_symbol_info(self, *_: Any) -> Any:
        return type(
            "Info",
            (),
            {
                "bid": 2064.0,
                "ask": 2064.03,
                "quote_time_msc": int(pd.Timestamp(utc_now()).timestamp() * 1000),
                "point": 0.001,
                "volume_min": 0.01,
                "volume_max": 100.0,
                "volume_step": 0.01,
                "digits": 3,
            },
        )()

    def get_positions(self, *_: Any) -> list[Any]:
        return list(self.positions)

    def get_orders(self, *_: Any) -> list[Any]:
        return list(self.orders)

    def get_position(self, ticket: int) -> Any:
        for pos in self.positions:
            if int(pos.ticket) == int(ticket):
                return pos
        return False

    def confirm_position_absent(self, ticket: int) -> bool:
        return self.get_position(ticket) is False

    def get_position_close_deal(self, position_id: int, *_: Any) -> Any:
        return SimpleNamespace(
            deal=8000 + position_id, position_id=position_id, symbol="XAUUSD", magic=EXPECTED_S24_MAGIC,
            reason="DEAL_REASON_EXPERT", price=2066.0, profit=1.0, commission=-0.1, swap=0.0, fee=0.0,
            deal_time=1767272520, exit_volume=0.01, net_profit=0.9,
        )

    def open_position(self, *_: Any, **kwargs: Any) -> int:
        position = self.positions[0] if self.positions else None
        if position is not None:
            position.comment = str(kwargs.get("comment") or position.comment)
            self.last_open_identifier = int(position.identifier)
            self.last_open_deal = 9001
            self.last_open_price = float(position.open_price)
            self.last_open_time = int(position.open_time)
            return int(position.ticket)
        self.last_order_error = "NO_RESPONSE"
        return None

    def close_position(self, ticket: int, *_: Any, **__: Any) -> CloseResult:
        position = self.get_position(ticket)
        lot = float(getattr(position, "volume", 0.01)) if position is not False else 0.01
        return CloseResult(True, "CONFIRMED", lot=lot, open_price=2064.0, close_price=2066.0, profit=1.0, deal_id=9001, retcode=10009)


def load_params(path: str = PARAMS_FILE) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return strict_json_load(f)


def self_test() -> None:
    global STATE_FILE
    original_state_file = STATE_FILE
    with tempfile.TemporaryDirectory(prefix="s24-self-test-") as directory:
        STATE_FILE = os.path.join(directory, "s24_bot_state.json")
        try:
            _run_self_test()
        finally:
            STATE_FILE = original_state_file


def _run_self_test() -> None:
    params = load_params()
    params["_suppress_manual_alerts"] = True
    params["live_trading_enabled"] = False
    params["shadow_forward_enabled"] = True
    params["runner_shadow"]["opportunity_observer"]["enabled"] = False
    params["runner_shadow"]["state_tagger"]["enabled"] = False
    runner = S24NoAdverseRunner(params)
    runner.safety = replace(runner.safety, stale_signal_guard=False)
    runner.state = runner._default_state()
    assert runner._ownership_namespace_error() is None, "valid S24 ownership namespaces must pass"
    wrong_magic_params = json.loads(json.dumps(params))
    wrong_magic_params["strategies"][0]["magic"] = 200022
    assert S24NoAdverseRunner._config_error(wrong_magic_params) == "strategy.magic=200022", "wrong S24 magic must fail frozen config validation"
    runner.dm = FakeDM()
    runner.executor = FakeExecutor()
    runner._save_state = lambda: None
    self_test_bars = add_features(runner.dm.get_historical_data(), float(params["point_size"]))
    self_test_bars["spread_points"] = 30.0
    self_test_bars.iloc[-1, self_test_bars.columns.get_loc("Close")] = float(self_test_bars.iloc[-1]["roll_high30"]) + 1.0
    runner._get_m1 = lambda: self_test_bars.copy()
    rows: list[tuple[str, str, str]] = []
    runner_rows: list[tuple[str, str]] = []
    runner._trade_row = lambda event, strat, **kw: rows.append((event, strat["id"], str(kw.get("reason", ""))))
    runner._shadow_runner_row = lambda event, _strat, **kw: runner_rows.append((event, str(kw.get("reason", ""))))
    runner.run_once()
    assert any(row[0] == "entry" for row in rows), "expected at least one shadow entry"
    assert any(row[0] == "runner_entry" for row in runner_rows), "shadow runner must consume the confirmed core signal without broker execution"
    assert len(runner._st(params["strategies"][0])["shadow_runner"]["basket"]) == 1, "shadow runner state must be isolated from the core basket"
    strategy = params["strategies"][0]
    st = runner._st(strategy)
    st["sync_block_new_entries"] = True
    st["sync_block_reason"] = "positions_unavailable"
    st["sync_block_recoverable"] = True
    save_calls: list[bool] = []
    runner._save_state = lambda: save_calls.append(True)
    runner._sync_strategy(strategy)
    assert not st["sync_block_new_entries"], "recoverable clean sync should clear"
    assert save_calls, "recoverable clear must persist state"
    assert any(row[0] == "sync_block_cleared_flat" and row[2] == "positions_unavailable" for row in rows), "clean-sync audit event and reason must use their CSV columns"

    st["sync_block_new_entries"] = True
    st["sync_block_reason"] = "open_success_position_not_confirmed"
    st["sync_block_recoverable"] = False
    st["sync_block_details"] = {"ticket": 9001}
    runner._sync_strategy(strategy)
    assert st["sync_block_new_entries"], "high-risk open block requires two flat confirmations"
    runner._sync_strategy(strategy)
    assert not st["sync_block_new_entries"], "high-risk open block should clear after two proven-flat confirmations"

    foreign = SimpleNamespace(ticket=9100, identifier=9100, symbol="XAUUSD", magic=EXPECTED_S24_MAGIC, comment="s22_foreign", type=ORDER_TYPE_BUY)
    runner.executor = FakeExecutor(positions=[foreign])
    assert not runner._sync_strategy(strategy), "same-magic foreign comment must block"
    assert st["sync_block_reason"] == "same_magic_unexpected_position_or_order"

    live_params = json.loads(json.dumps(params))
    live_params["live_trading_enabled"] = True
    live_params["shadow_forward_enabled"] = False
    live_runner = S24NoAdverseRunner(live_params)
    live_runner.state = live_runner._default_state()
    live_runner.dm = FakeDM()
    live_runner.executor = FakeExecutor(margin_mode=0)
    assert not live_runner.connect_and_preflight(), "live S24 must reject netting accounts"

    valid_live_runner = S24NoAdverseRunner(live_params)
    valid_live_runner.state = valid_live_runner._default_state()
    valid_live_runner.dm = FakeDM()
    valid_live_runner.executor = FakeExecutor()
    assert valid_live_runner.connect_and_preflight(), "live S24 must accept the expected hedging account and timestamped bridge"

    wrong_account_runner = S24NoAdverseRunner(live_params)
    wrong_account_runner.state = wrong_account_runner._default_state()
    wrong_account_runner.dm = FakeDM()
    wrong_account_runner.executor = FakeExecutor()
    wrong_account_runner.executor.get_account_info = lambda: {
        "margin_mode": HEDGING_MARGIN_MODE,
        "margin_mode_name": "RETAIL_HEDGING",
        "login": int(MT5_LOGIN) + 1,
        "server": MT5_SERVER,
    }
    assert not wrong_account_runner.connect_and_preflight(), "live S24 must reject the wrong MT5 account identity"

    missing_quote_time_runner = S24NoAdverseRunner(live_params)
    missing_quote_time_runner.state = missing_quote_time_runner._default_state()
    missing_quote_time_runner.dm = FakeDM()
    missing_quote_time_runner.executor = FakeExecutor()
    missing_quote_time_runner.executor.get_symbol_info = lambda *_args: type("Info", (), {"bid": 2064.0, "ask": 2064.03})()
    assert not missing_quote_time_runner.connect_and_preflight(), "live S24 must require broker quote timestamps"

    confirmed_params = json.loads(json.dumps(live_params))
    confirmed_runner = S24NoAdverseRunner(confirmed_params)
    confirmed_runner.state = confirmed_runner._default_state()
    confirmed_runner._save_state = lambda: None
    confirmed_runner._trade_row = lambda *_args, **_kwargs: None
    confirmed_strategy = confirmed_params["strategies"][0]
    owned = SimpleNamespace(
        ticket=1, identifier=7001, symbol="XAUUSD", magic=EXPECTED_S24_MAGIC,
        comment="s24_no_adverse", type=ORDER_TYPE_BUY, volume=0.01,
        open_price=2064.03, open_time=1767272400,
    )
    confirmed_runner.executor = FakeExecutor(positions=[])
    sample_bars = add_features(FakeDM().get_historical_data(), float(params["point_size"]))
    sample_row = sample_bars.iloc[-1].copy()
    sample_signal_time = pd.Timestamp(utc_now()).floor("min") - pd.Timedelta(minutes=1)
    sample_row.name = sample_signal_time
    owned.open_time = int((sample_signal_time + pd.Timedelta(minutes=1)).timestamp())
    sample_info = confirmed_runner.executor.get_symbol_info("XAUUSD")
    sample_info.quote_time_msc = int((sample_signal_time + pd.Timedelta(minutes=1)).timestamp() * 1000)
    confirmed_runner.executor.get_symbol_info = lambda *_args: sample_info
    def confirmed_open(*_args: Any, **kwargs: Any) -> int:
        owned.comment = str(kwargs["comment"])
        confirmed_runner.executor.positions.append(owned)
        confirmed_runner.executor.last_open_identifier = int(owned.identifier)
        confirmed_runner.executor.last_open_deal = 9001
        confirmed_runner.executor.last_open_price = float(owned.open_price)
        confirmed_runner.executor.last_open_time = int(owned.open_time)
        return int(owned.ticket)
    confirmed_runner.executor.open_position = confirmed_open
    confirmed_runner._open_entry(confirmed_strategy, "LONG", sample_row, sample_info)
    confirmed_state = confirmed_runner._st(confirmed_strategy)
    assert len(confirmed_state["basket"]) == 1 and confirmed_state["basket"][0]["position_identifier"] == 7001, "OPEN must persist broker-confirmed position ownership"

    ambiguous_runner = S24NoAdverseRunner(confirmed_params)
    ambiguous_runner.state = ambiguous_runner._default_state()
    ambiguous_runner._save_state = lambda: None
    ambiguous_runner._trade_row = lambda *_args, **_kwargs: None
    ambiguous_runner.executor = FakeExecutor(positions=[])
    ambiguous_runner.executor.get_symbol_info = lambda *_args: sample_info
    ambiguous_runner._open_entry(confirmed_strategy, "LONG", sample_row, sample_info)
    assert ambiguous_runner._st(confirmed_strategy)["sync_block_reason"] == "unresolved_open_action", "unconfirmed successful OPEN must fail closed with persistent pending-open evidence"

    partial_runner = S24NoAdverseRunner(confirmed_params)
    partial_runner.state = partial_runner._default_state()
    partial_runner._save_state = lambda: None
    live_remaining = SimpleNamespace(
        ticket=2, identifier=7002, symbol="XAUUSD", magic=EXPECTED_S24_MAGIC,
        comment="s24_no_adverse", type=ORDER_TYPE_BUY, volume=0.01,
        open_price=2065.0, open_time=1767272460,
    )
    partial_runner.executor = FakeExecutor(positions=[live_remaining])
    partial_rows: list[tuple[str, dict[str, Any]]] = []
    partial_runner._trade_row = lambda event, *_args, **kw: partial_rows.append((event, kw))
    partial_state = partial_runner._st(confirmed_strategy)
    partial_state["basket"] = [
        {"ticket": 1, "position_identifier": 7001, "side": "LONG", "lot": 0.01, "entry_price": 2064.0, "entry_time_utc": "2026-01-01T13:00:00Z", "open_time_epoch": 1767272400, "owner_symbol": "XAUUSD", "owner_magic": EXPECTED_S24_MAGIC, "owner_comment": "s24_no_adverse"},
        {"ticket": 2, "position_identifier": 7002, "side": "LONG", "lot": 0.01, "entry_price": 2065.0, "entry_time_utc": "2026-01-01T13:01:00Z", "open_time_epoch": 1767272460, "owner_symbol": "XAUUSD", "owner_magic": EXPECTED_S24_MAGIC, "owner_comment": "s24_no_adverse"},
    ]
    assert partial_runner._sync_strategy(confirmed_strategy), "partially completed basket close must reconcile owned tickets"
    assert [pos["position_identifier"] for pos in partial_state["basket"]] == [7002], "confirmed closed ticket must be removed without losing remaining owned state"
    assert any(
        event == "position_close_deal"
        and row.get("ticket") == 1
        and row.get("position_identifier") == 7001
        and row.get("price") == 2066.0
        for event, row in partial_rows
    ), "confirmed close audit must retain separate ticket, position identifier, and broker deal price"

    fail_runner = S24NoAdverseRunner(params)
    fail_runner.state = fail_runner._default_state()
    fail_runner.executor = FakeExecutor()
    fail_runner._save_state = lambda: None
    fail_st = fail_runner._st(strategy)
    fail_st["basket"] = [{"ticket": None, "position_identifier": 0, "side": "LONG", "lot": 0.01, "entry_price": 2064.0, "entry_time_utc": "2026-01-01T13:00:00+00:00", "shadow": True}]
    fail_st["basket_peak_pnl_usd"] = 1.0
    original_max_hold = strategy["max_hold_bars"]
    strategy["max_hold_bars"] = 10
    bars = add_features(FakeDM().get_historical_data(), float(params["point_size"]))
    bars["spread_points"] = 3.0
    bars = bars.loc[bars.index <= pd.Timestamp("2026-01-01T13:11:00Z")]
    events: list[str] = []
    fail_runner._trade_row = lambda event, *_args, **kw: events.append(str(kw.get("reason") or event))
    fail_runner._run_strategy(strategy, bars, FakeExecutor().get_symbol_info("XAUUSD"))
    assert "max_hold" in events, "no-adverse candidate must retain its max-hold exit"
    strategy["max_hold_bars"] = original_max_hold

    poll_runner = S24NoAdverseRunner(params)
    poll_runner.state = poll_runner._default_state()
    poll_runner.executor = FakeExecutor()
    poll_runner._save_state = lambda: None
    poll_st = poll_runner._st(strategy)
    poll_st["basket"] = [{"ticket": None, "position_identifier": 0, "side": "LONG", "lot": 0.01, "entry_price": 2064.0, "entry_time_utc": "2026-01-01T13:00:00+00:00", "shadow": True}]
    poll_st["last_evaluated_bar"] = dt_text(parse_ts(bars.iloc[-1].name))
    poll_st["last_exit_evaluated_bar"] = dt_text(parse_ts(bars.iloc[-1].name))
    poll_events: list[str] = []
    poll_runner._trade_row = lambda event, *_args, **kw: poll_events.append(str(kw.get("reason") or event))
    poll_info = type("Info", (), {"bid": 2081.0, "ask": 2081.03})()
    poll_runner._run_strategy(strategy, bars, poll_info)
    assert "basket_target" not in poll_events, "bot24 must not exit again within the same confirmed M1 bar"
    next_bar = bars.iloc[[-1]].copy()
    next_bar.index = pd.DatetimeIndex([bars.index[-1] + pd.Timedelta(minutes=1)])
    poll_runner._run_strategy(strategy, pd.concat([bars, next_bar]), poll_info)
    assert "basket_target" in poll_events, "bot24 must evaluate basket exits on the next confirmed M1 bar"

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    os.makedirs(LOG_DIR, exist_ok=True)
    if args.self_test:
        logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", force=True)
        self_test()
        print("s24 self-test ok")
        return 0
    params = load_params()
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=int(params.get("bot_log_max_bytes", 10 * 1024 * 1024)),
        backupCount=int(params.get("bot_log_backup_count", 5)),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)
    runner_lock = acquire_runner_singleton_lock()
    if runner_lock is None:
        logging.critical("Another bot24 runner already owns the state/order namespace; refusing to start")
        return 1
    try:
        runner = S24NoAdverseRunner(params)
        if not runner.connect_and_preflight():
            return 1
        if args.once:
            runner.run_once()
            return 0
        while True:
            runner.run_once()
            time.sleep(float(params.get("poll_interval_seconds", 5)))
    finally:
        runner_lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
