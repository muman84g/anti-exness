# -*- coding: utf-8 -*-
"""S19 GBPUSD D10 pending-stop live runner.

This file is derived from the S19 snowball runner, but is fixed to the
backtest34 D10 / TP1 / e75 / fs_m80_b2k candidate and uses server-side
Buy Stop / Sell Stop orders for breakout entries.
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
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from live_manual_alerts import notify_manual_action_required

JST = timezone(timedelta(hours=9), "JST")
LONG = 1
SHORT = -1
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TYPE_BUY_STOP = 4
ORDER_TYPE_SELL_STOP = 5

LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "s19_bot.log")
TRADE_LOG_FILE = os.path.join(LOG_DIR, "s19_trades.csv")
POLICY_LOG_FILE = os.path.join(LOG_DIR, "s19_policy_decisions.csv")
STATE_DIR = os.path.join(SCRIPT_DIR, "state")
PARAMS_FILE = os.path.join(SCRIPT_DIR, "s19_params.json")
ARTIFACTS_DIR = os.path.join(SCRIPT_DIR, "artifacts")
BOT_SOURCE_REVISION = "2026-07-17-startup-state-reconcile-v1"

DEFAULT_PARAMS: dict[str, Any] = {
    "enabled": True,
    "live_trading_enabled": False,
    "shadow_forward_enabled": True,
    "symbol": "GBPUSD",
    "magic": 190019,
    "comment_prefix": "s19_gbp",
    "strategy_id": "bot19_d10_tp1_e75_fs_m80_b2k_pending_stop",
    "lot": 0.01,
    "distance_pips": 10.0,
    "auto_tp_levels": 1,
    "min_auto_tp_cycle_profit_usd": 1.00,
    "base_add_distance_pips": 20.0,
    "inactive_add_distance_pips": 23.5,
    "inactive_distance_mode": "gate_false",
    "max_entry_spread_points": 9.0,
    "pip_size": 0.0001,
    "point_size": 0.00001,
    "price_digits": 5,
    "deviation_points": 20,
    "max_open_positions": 40,
    "max_virtual_orders": 80,
    "break_even_refresh_seconds": 300,
    "poll_interval_seconds": 1.0,
    "flat_position_sync_interval_seconds": 5.0,
    "status_log_interval_seconds": 60,
    "regime_timeframe": 16385,
    "regime_bars": 240,
    "regime_refresh_seconds": 60,
    "drop_latest_h1_bar": True,
    "m1_timeframe": 1,
    "m1_bars": 80,
    "drop_latest_m1_bar": True,
    "efficiency_lookback": 24,
    "min_efficiency_ratio": 0.30,
    "adx_period": 14,
    "min_adx": 20.0,
    "displacement_lookback": 12,
    "min_displacement_atr": 1.5,
    "max_signal_age_minutes": 65,
    "weekend_hold": True,
    "force_weekend_flat": False,
    "weekend_entry_stop_weekday_jst": 5,
    "weekend_entry_stop_hour_jst": 2,
    "weekend_entry_stop_minute_jst": 0,
    "monday_start_hour_jst": 8,
    "monday_start_minute_jst": 0,
    "reanchor_after_weekend_resume": True,
    "short_auto_tp_uses_ask": True,
    "exact_cycle_equity_auto_tp": False,
    "carry_unrecovered_cycle_loss": False,
    "block_rollover_entries": False,
    "directional_cycle_start": False,
    "close_on_inactive_regime_cycle_equity": False,
    "use_server_sl": True,
    "repair_missing_sl_on_sync": True,
    "sl_mismatch_tolerance_pips": 0.2,
    "assume_missing_state_position_is_sl": True,
    "use_server_pending_entry": True,
    "flat_pending_grid_repair_enabled": True,
    "pending_fill_sync_grace_seconds": 5.0,
    "dynamic_recovery_close_enabled": True,
    "dynamic_recovery_sl_streak": 6,
    "dynamic_recovery_min_cycle_loss_usd": -10.0,
    "dynamic_recovery_close_equity_usd": 0.50,
    "dynamic_exposure_cap_enabled": True,
    "dynamic_exposure_cap_side": "SHORT",
    "dynamic_exposure_cap_base": 10,
    "dynamic_exposure_cap_base_gate_false": True,
    "dynamic_exposure_cap_base_sl_streak": 4,
    "dynamic_exposure_cap_base_equity_usd": -20.0,
    "dynamic_exposure_cap_deep": 8,
    "dynamic_exposure_cap_deep_equity_usd": -55.0,
    "dynamic_exposure_cap_extreme": 6,
    "dynamic_exposure_cap_extreme_equity_usd": -75.0,
    "dynamic_failsafe_close_enabled": True,
    "dynamic_failsafe_block_count": 2000,
    "dynamic_failsafe_equity_usd": -80.0,
    "policy_enabled": False,
    "policy_fail_closed": False,
    "policy_artifacts_dir": ARTIFACTS_DIR,
    "policy_spread_add_points": 2.0,
    "policy_max_entry_spread_points": 9.0,
    "policy_registry_file": "candidate_registry.json",
    "policy_selected_features_file": "selected_features.csv",
    "profiles": [
        {
            "symbol": "GBPUSD",
            "magic": 190019,
            "comment_prefix": "s19_gbp",
            "strategy_id": "bot19_d10_tp1_e75_fs_m80_b2k_pending_stop",
        },
    ],
}


def configure_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def jst_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(JST)


def load_params() -> dict[str, Any]:
    params = DEFAULT_PARAMS.copy()
    if os.path.exists(PARAMS_FILE):
        with open(PARAMS_FILE, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise ValueError("s19_params.json must contain a JSON object")
        params.update(loaded)
    return params


def build_profile_params(raw_params: dict[str, Any]) -> list[dict[str, Any]]:
    profiles = raw_params.get("profiles")
    if not profiles:
        return [raw_params.copy()]
    if not isinstance(profiles, list):
        raise ValueError("profiles must be a list")
    profile_params: list[dict[str, Any]] = []
    base = raw_params.copy()
    base.pop("profiles", None)
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ValueError("each profile must be a JSON object")
        merged = base.copy()
        merged.update(profile)
        profile_params.append(merged)
    return profile_params


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=True, sort_keys=True)
        handle.write("\n")
    if os.path.isfile(path):
        try:
            shutil.copy2(path, path + ".bak")
        except Exception as exc:
            logging.error(f"Failed to create state backup for {path}: {exc}")
    os.replace(tmp_path, path)


def append_csv_row(path: str, header: list[str], row: list[Any]) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        file_exists = os.path.isfile(path) and os.path.getsize(path) > 0
        encoding = "utf-8" if file_exists else "utf-8-sig"
        with open(path, "a", newline="", encoding=encoding) as handle:
            writer = csv.writer(handle)
            if not file_exists:
                writer.writerow(header)
            writer.writerow(row)
        return True
    except Exception as exc:
        logging.error(f"Failed to append CSV row to {path}: {exc}")
        return False


def direction_name(direction: int) -> str:
    return "LONG" if int(direction) == LONG else "SHORT"


def direction_from_name(value: str) -> int:
    text = str(value).upper()
    if text == "LONG":
        return LONG
    if text == "SHORT":
        return SHORT
    raise ValueError(f"unknown direction: {value}")


def weekly_minute(t_jst: datetime) -> int:
    return t_jst.weekday() * 24 * 60 + t_jst.hour * 60 + t_jst.minute


def is_in_weekly_window(
    t_jst: datetime,
    start_weekday: int,
    start_hour: int,
    start_minute: int,
    end_weekday: int,
    end_hour: int,
    end_minute: int,
) -> bool:
    current = weekly_minute(t_jst)
    start = int(start_weekday) * 24 * 60 + int(start_hour) * 60 + int(start_minute)
    end = int(end_weekday) * 24 * 60 + int(end_hour) * 60 + int(end_minute)
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def session_features(decision_time_utc: datetime) -> dict[str, int]:
    ts = decision_time_utc.astimezone(JST)
    minutes = ts.hour * 60 + ts.minute

    def minutes_since(start_hour: int) -> int:
        start = start_hour * 60
        return int((minutes - start) % (24 * 60))

    return {
        "session_tokyo_core_jst": int(9 * 60 <= minutes < 15 * 60),
        "session_london_core_jst": int(16 * 60 <= minutes < 24 * 60),
        "session_newyork_core_jst": int(minutes < 6 * 60 or minutes >= 21 * 60),
        "minutes_since_tokyo_open_jst": minutes_since(9),
        "minutes_since_london_open_jst": minutes_since(16),
        "minutes_since_newyork_open_jst": minutes_since(21),
        "day_of_week_jst": int(ts.weekday()),
        "hour_jst": int(ts.hour),
        "minute_of_day_jst": int(minutes),
    }


def build_m1_feature_row(m1: pd.DataFrame, pip_size: float) -> dict[str, float]:
    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = sorted(required.difference(m1.columns))
    if missing:
        raise ValueError(f"M1 bars missing columns: {missing}")
    if len(m1) < 16:
        raise ValueError("not enough M1 bars")
    high = m1["High"].astype(float)
    low = m1["Low"].astype(float)
    close = m1["Close"].astype(float)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    last = m1.iloc[-1]
    atr = true_range.rolling(14, min_periods=14).mean().div(float(pip_size)).iloc[-1]
    ret_1 = close.diff().div(float(pip_size)).iloc[-1]
    return {
        "m1_atr_pips": float(atr),
        "m1_range_pips": float((float(last["High"]) - float(last["Low"])) / float(pip_size)),
        "m1_ret_1_pips": float(ret_1),
        "m1_tick_volume": float(last["Volume"]),
    }


def build_trend_regime_from_h1(bars: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
    required = {"Open", "High", "Low", "Close"}
    missing = sorted(required.difference(bars.columns))
    if missing:
        raise ValueError(f"H1 bars missing columns: {missing}")
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise ValueError("H1 bars index must be DatetimeIndex")
    if len(bars) < 60:
        return pd.DataFrame(index=bars.index)

    high = bars["High"].astype(float)
    low = bars["Low"].astype(float)
    close = bars["Close"].astype(float)
    efficiency_lookback = int(params["efficiency_lookback"])
    adx_period = int(params["adx_period"])
    displacement_lookback = int(params["displacement_lookback"])

    close_change = close.diff()
    path = close_change.abs().rolling(efficiency_lookback, min_periods=efficiency_lookback).sum()
    efficiency = close.diff(efficiency_lookback).abs().div(path)

    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0.0), up_move, 0.0),
        index=bars.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0.0), down_move, 0.0),
        index=bars.index,
    )
    alpha = 1.0 / adx_period
    atr = true_range.ewm(alpha=alpha, adjust=False, min_periods=adx_period).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=adx_period).mean().div(atr)
    minus_di = 100.0 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=adx_period).mean().div(atr)
    denominator = plus_di + minus_di
    dx = 100.0 * (plus_di - minus_di).abs().div(denominator.where(denominator > 0.0))
    adx = dx.ewm(alpha=alpha, adjust=False, min_periods=adx_period).mean()
    displacement = close.diff(displacement_lookback)
    displacement_atr = displacement.abs().div(atr)
    trend_direction = pd.Series(
        np.where(displacement > 0.0, 1, np.where(displacement < 0.0, -1, 0)),
        index=bars.index,
    )
    trend_allowed = (
        (efficiency >= float(params["min_efficiency_ratio"]))
        & (adx >= float(params["min_adx"]))
        & (displacement_atr >= float(params["min_displacement_atr"]))
    ).fillna(False)
    return pd.DataFrame(
        {
            "EfficiencyRatio": efficiency,
            "ADX": adx,
            "ATR": atr,
            "DisplacementATR": displacement_atr,
            "TrendDirection": trend_direction.astype("int8"),
            "TrendAllowed": trend_allowed.astype(bool),
        },
        index=bars.index,
    )


class EventFilterPolicy:
    def __init__(self, params: dict[str, Any]) -> None:
        self.artifacts_dir = os.path.abspath(str(params.get("policy_artifacts_dir", ARTIFACTS_DIR)))
        registry_path = os.path.join(self.artifacts_dir, str(params["policy_registry_file"]))
        features_path = os.path.join(self.artifacts_dir, str(params["policy_selected_features_file"]))
        with open(registry_path, "r", encoding="utf-8") as handle:
            self.registry = json.load(handle)
        self.selected_features = pd.read_csv(features_path, encoding="utf-8-sig")["feature"].astype(str).tolist()
        self.candidates_by_symbol = {
            str(candidate["symbol"]).upper(): candidate
            for candidate in self.registry.get("candidates", [])
        }
        self.models: dict[str, Any] = {}
        self.medians: dict[str, pd.Series] = {}
        self._load_model_assets("event_filter_catboost")
        self._load_model_assets("event_filter_lgbm")

    def _asset_path(self, relative_path: str) -> str:
        return os.path.join(self.artifacts_dir, relative_path)

    def _load_model_assets(self, model_name: str) -> None:
        model_meta = self.registry.get("models", {}).get(model_name)
        if not isinstance(model_meta, dict):
            return
        if model_name == "event_filter_catboost":
            model_path = self._asset_path(os.path.join("models", "event_filter_catboost.cbm"))
            medians_path = self._asset_path("feature_medians_event_filter_catboost.csv")
            from catboost import CatBoostClassifier  # type: ignore

            model = CatBoostClassifier()
            model.load_model(model_path)
        elif model_name == "event_filter_lgbm":
            model_path = self._asset_path(os.path.join("models", "event_filter_lgbm.txt"))
            medians_path = self._asset_path("feature_medians_event_filter_lgbm.csv")
            import lightgbm as lgb  # type: ignore

            model = lgb.Booster(model_file=model_path)
        else:
            return

        expected_model_sha = str(model_meta.get("model_sha256", ""))
        expected_medians_sha = str(model_meta.get("medians_sha256", ""))
        if expected_model_sha and file_sha256(model_path) != expected_model_sha:
            raise ValueError(f"{model_name} model sha256 mismatch")
        if expected_medians_sha and file_sha256(medians_path) != expected_medians_sha:
            raise ValueError(f"{model_name} medians sha256 mismatch")

        medians_df = pd.read_csv(medians_path, encoding="utf-8-sig")
        medians = pd.Series(
            pd.to_numeric(medians_df["median"], errors="coerce").to_numpy(dtype="float64"),
            index=medians_df["feature"].astype(str),
        )
        self.models[model_name] = model
        self.medians[model_name] = medians

    def candidate_for_symbol(self, symbol: str) -> dict[str, Any] | None:
        return self.candidates_by_symbol.get(str(symbol).upper())

    def predict(self, symbol: str, features: dict[str, Any]) -> dict[str, Any]:
        candidate = self.candidate_for_symbol(symbol)
        if candidate is None:
            raise ValueError(f"policy candidate missing for symbol={symbol}")
        model_name = str(candidate["model"])
        if model_name not in self.models:
            raise ValueError(f"policy model not loaded: {model_name}")
        medians = self.medians[model_name]
        values: list[float] = []
        missing: list[str] = []
        for feature in self.selected_features:
            raw_value = features.get(feature, np.nan)
            value = pd.to_numeric(pd.Series([raw_value]), errors="coerce").iloc[0]
            if pd.isna(value):
                missing.append(feature)
                value = medians.get(feature, 0.0)
            values.append(float(value))
        matrix = pd.DataFrame([values], columns=self.selected_features)
        if model_name == "event_filter_catboost":
            pred_proba = float(self.models[model_name].predict_proba(matrix)[:, 1][0])
        elif model_name == "event_filter_lgbm":
            pred_proba = float(self.models[model_name].predict(matrix)[0])
        else:
            raise ValueError(f"unsupported policy model: {model_name}")
        threshold = float(candidate["threshold"])
        return {
            "allow": bool(pred_proba >= threshold),
            "candidate_id": str(candidate["candidate_id"]),
            "model": model_name,
            "threshold": threshold,
            "pred_proba": pred_proba,
            "missing_features": "|".join(missing),
        }

    def self_test(self) -> None:
        for symbol, candidate in sorted(self.candidates_by_symbol.items()):
            model_name = str(candidate["model"])
            medians = self.medians[model_name]
            features = {feature: float(medians.get(feature, 0.0)) for feature in self.selected_features}
            result = self.predict(symbol, features)
            assert "pred_proba" in result


class S19SnowballBot:
    def __init__(self, params: dict[str, Any] | None = None, policy: EventFilterPolicy | None = None) -> None:
        self.params = DEFAULT_PARAMS.copy()
        self.params.update(params or load_params())
        self.symbol = str(self.params["symbol"]).upper()
        self.magic = int(self.params["magic"])
        safe_symbol = "".join(ch.lower() for ch in self.symbol if ch.isalnum())
        self.state_file = str(
            self.params.get(
                "state_file",
                os.path.join(STATE_DIR, f"s19_{safe_symbol}_bot_state.json"),
            )
        )
        self.trade_log_file = str(self.params.get("trade_log_file", TRADE_LOG_FILE))
        self.policy_log_file = str(self.params.get("policy_log_file", POLICY_LOG_FILE))
        self.policy = policy
        self.dm = None
        self.executor = None
        self.state = self.load_state()
        self.last_regime_fetch_epoch = 0.0
        self.cached_regime = self.default_regime()
        self.last_status_log_epoch = 0.0
        self.last_auto_tp_profit_guard_log_epoch = 0.0
        self.last_flat_pending_grid_repair_log_epoch = 0.0
        self.last_position_sync_epoch = 0.0
        self.sync_closed_count = 0
        self._suppress_manual_alerts = False

    @property
    def pip_size(self) -> float:
        return float(self.params["pip_size"])

    @property
    def point_size(self) -> float:
        return float(self.params["point_size"])

    @property
    def distance_price(self) -> float:
        return float(self.params["distance_pips"]) * self.pip_size

    @property
    def digits(self) -> int:
        return int(self.params["price_digits"])

    def price(self, value: float) -> float:
        return round(float(value), self.digits)

    def default_regime(self) -> dict[str, Any]:
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

    def default_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "strategy": "s19_snowball_cycle_start_event_filter",
            "symbol": self.symbol,
            "magic": self.magic,
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
            "pending_fill_sync_wait": None,
            "manual_alert_last_reconciliation_signature": None,
            "manual_alert_last_reconciliation_reason": None,
            "manual_alert_last_reconciliation_at_jst": None,
            "startup_state_reconciled_at_jst": None,
            "startup_state_recovery": None,
            "last_regime": self.default_regime(),
            "last_policy_decision": None,
            "updated_at_jst": jst_now().isoformat(),
        }

    def load_state(self) -> dict[str, Any]:
        if not os.path.exists(self.state_file):
            return self.default_state()
        with open(self.state_file, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        if state.get("symbol") != self.symbol or int(state.get("magic", 0)) != self.magic:
            raise ValueError("State symbol/magic does not match s19_params.json")
        default = self.default_state()
        default.update(state)
        return default

    def save_state(self) -> None:
        self.recalculate_position_counts()
        self.state["updated_at_jst"] = jst_now().isoformat()
        atomic_write_json(self.state_file, self.state)

    def recalculate_position_counts(self) -> None:
        long_positions = [p for p in self.state["positions"] if p["direction"] == "LONG"]
        short_positions = [p for p in self.state["positions"] if p["direction"] == "SHORT"]
        self.state["long_count"] = len(long_positions)
        self.state["short_count"] = len(short_positions)
        self.state["long_entry_sum"] = sum(float(p["entry"]) for p in long_positions)
        self.state["short_entry_sum"] = sum(float(p["entry"]) for p in short_positions)

    def log_trade_csv(
        self,
        action: str,
        ticket: int,
        direction: str = "",
        lot_size: float | str = "",
        price: float | str | None = "",
        stop_loss: float | str | None = "",
        pnl: float | str | None = "",
        reason: str = "",
        source_order_id: int | str | None = "",
        comment: str = "",
    ) -> bool:
        header = [
            "Timestamp_JST",
            "Action",
            "Ticket",
            "Symbol",
            "Direction",
            "LotSize",
            "Price",
            "StopLoss",
            "PnL",
            "Reason",
            "CycleId",
            "SourceOrderId",
            "Comment",
        ]

        def clean(value: Any) -> Any:
            return "" if value is None else value

        row = [
            jst_now().strftime("%Y-%m-%d %H:%M:%S"),
            action,
            int(ticket),
            self.symbol,
            direction,
            clean(lot_size),
            clean(price),
            clean(stop_loss),
            clean(pnl),
            reason,
            int(self.state.get("cycle_id", 0)),
            clean(source_order_id),
            comment,
        ]

        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            file_exists = os.path.isfile(self.trade_log_file) and os.path.getsize(self.trade_log_file) > 0
            encoding = "utf-8" if file_exists else "utf-8-sig"
            with open(self.trade_log_file, mode="a", newline="", encoding=encoding) as handle:
                writer = csv.writer(handle)
                if not file_exists:
                    writer.writerow(header)
                writer.writerow(row)
            return True
        except Exception as exc:
            logging.error(f"Failed to write trade log to CSV: {exc}")
            return False

    def ensure_bridge(self) -> None:
        if self.dm is not None and self.executor is not None:
            return
        from live_data_fetcher import MT5DataManager
        from live_executor import MT5Executor

        self.dm = MT5DataManager()
        self.executor = MT5Executor(self.dm)

    def connect(self) -> bool:
        self.ensure_bridge()
        return bool(self.dm.connect())

    def verify_bridge_capabilities(self) -> None:
        self.ensure_bridge()
        if bool(self.params.get("use_server_pending_entry", False)):
            from live_executor import REQUIRED_S19_PENDING_COMMANDS, S19_BRIDGE_NAME

            caps = self.executor.get_bridge_capabilities()
            if caps is None:
                raise RuntimeError(
                    "Bridge CAPS preflight failed. Update/attach BotBridge_s19.ex5 and confirm Files directory."
                )
            bridge_name = str(caps.get("name", ""))
            commands = set(caps.get("commands", set()))
            missing = sorted(REQUIRED_S19_PENDING_COMMANDS.difference(commands))
            if bridge_name != S19_BRIDGE_NAME:
                raise RuntimeError(f"Unexpected EA bridge name: {bridge_name}")
            if missing:
                raise RuntimeError(f"EA bridge missing required commands: {missing}")
            logging.info(
                "EA bridge capabilities verified: name=%s version=%s commands=%s",
                bridge_name,
                caps.get("version"),
                ",".join(sorted(commands)),
            )
        tick = self.get_tick()
        if tick is None:
            raise RuntimeError(f"Bridge INFO preflight failed for {self.symbol}")
        bars_required = max(80, min(int(self.params["regime_bars"]), 120))
        bars = self.dm.get_historical_data(
            self.symbol,
            int(self.params["regime_timeframe"]),
            bars_required,
        )
        if bars is None or len(bars) < 2:
            raise RuntimeError(f"Bridge HIST preflight failed for {self.symbol}")
        if self.executor.get_positions(self.symbol, self.magic) is None:
            raise RuntimeError(f"Bridge POSITIONS preflight failed for {self.symbol}")
        if bool(self.params.get("use_server_pending_entry", False)):
            if self.executor.get_orders(self.symbol, self.magic) is None:
                raise RuntimeError(f"Bridge ORDERS preflight failed for {self.symbol}")

    def startup_reconcile_state(self) -> dict[str, Any]:
        before = {
            "cycle_id": int(self.state.get("cycle_id", 0) or 0),
            "positions": len(self.state.get("positions", [])),
            "virtual_orders": len(self.state.get("virtual_orders", [])),
            "pending_open": isinstance(self.state.get("pending_open"), dict),
            "pending_grid_repair_wait": isinstance(self.state.get("pending_grid_repair_wait"), dict),
            "pending_fill_sync_wait": isinstance(self.state.get("pending_fill_sync_wait"), dict),
            "sync_block_new_entries": bool(self.state.get("sync_block_new_entries", False)),
            "reconciliation_required": isinstance(self.state.get("reconciliation_required"), dict),
        }
        recovery: dict[str, Any] = {
            "status": "not_started",
            "reason": "",
            "before": before,
            "after": {},
            "history_warmup": {},
            "created_at_jst": jst_now().isoformat(),
        }
        tick = self.get_tick()
        if tick is None:
            recovery["status"] = "deferred"
            recovery["reason"] = "startup tick unavailable"
            self.state["startup_state_reconciled_at_jst"] = jst_now().isoformat()
            self.state["startup_state_recovery"] = recovery
            logging.warning("S19 startup state reconcile deferred: tick unavailable for %s", self.symbol)
            return recovery

        regime = self.get_regime()
        recovery["history_warmup"]["regime_ok"] = bool(regime.get("reason") == "ok")
        recovery["history_warmup"]["regime_reason"] = str(regime.get("reason"))
        try:
            features = self.get_m1_policy_features()
            recovery["history_warmup"]["m1_ok"] = True
            recovery["history_warmup"]["m1_decision_time_utc"] = str(
                features.get("m1_decision_time_utc") or ""
            )
        except Exception as exc:
            recovery["history_warmup"]["m1_ok"] = False
            recovery["history_warmup"]["m1_reason"] = str(exc)
            logging.warning("S19 startup M1 warmup failed for %s: %s", self.symbol, exc)

        sync_ok = self.sync_live_positions(tick, regime, force=True)
        recovery["status"] = "reconciled" if sync_ok else "blocked"
        recovery["reason"] = "clean startup sync" if sync_ok else str(self.state.get("sync_block_reason") or "")
        if sync_ok and self.state.get("positions") and (
            self.auto_tp_refresh_due() or self.state.get("auto_tp_price") is None
        ):
            self.refresh_auto_tp(float(tick["bid"]), float(tick["ask"]), tick.get("info"))
        after = {
            "cycle_id": int(self.state.get("cycle_id", 0) or 0),
            "positions": len(self.state.get("positions", [])),
            "virtual_orders": len(self.state.get("virtual_orders", [])),
            "pending_open": isinstance(self.state.get("pending_open"), dict),
            "pending_grid_repair_wait": isinstance(self.state.get("pending_grid_repair_wait"), dict),
            "pending_fill_sync_wait": isinstance(self.state.get("pending_fill_sync_wait"), dict),
            "sync_block_new_entries": bool(self.state.get("sync_block_new_entries", False)),
            "reconciliation_required": isinstance(self.state.get("reconciliation_required"), dict),
            "auto_tp_price": self.state.get("auto_tp_price"),
        }
        recovery["after"] = after
        self.state["startup_state_reconciled_at_jst"] = jst_now().isoformat()
        self.state["startup_state_recovery"] = recovery
        logging.info(
            "S19 startup state reconcile: symbol=%s status=%s before_pos=%s before_orders=%s "
            "after_pos=%s after_orders=%s reason=%s",
            self.symbol,
            recovery["status"],
            before["positions"],
            before["virtual_orders"],
            after["positions"],
            after["virtual_orders"],
            recovery["reason"],
        )
        return recovery

    def live_server_pending_enabled(self) -> bool:
        return bool(self.params.get("use_server_pending_entry", False)) and bool(
            self.params.get("live_trading_enabled", False)
        )

    def normalize_lot(self, info: Any) -> float:
        lot = float(self.params["lot"])
        min_lot = float(getattr(info, "volume_min", lot))
        max_lot = float(getattr(info, "volume_max", lot))
        step = float(getattr(info, "volume_step", min_lot)) or min_lot
        lot = max(min_lot, min(lot, max_lot))
        return round(lot / step) * step

    def usd_per_pip_for_lot(self, volume: float, info: Any | None = None) -> float:
        contract_size = float(getattr(info, "contract_size", 100000.0) or 100000.0)
        return contract_size * float(volume) * self.pip_size

    def get_tick(self) -> dict[str, Any] | None:
        self.ensure_bridge()
        info = self.executor.get_symbol_info(self.symbol)
        if info is None:
            return None
        self.params["point_size"] = float(getattr(info, "point", self.params["point_size"]))
        self.params["price_digits"] = int(getattr(info, "digits", self.params["price_digits"]))
        return {
            "bid": float(info.bid),
            "ask": float(info.ask),
            "spread_points": (float(info.ask) - float(info.bid)) / float(info.point),
            "info": info,
        }

    def get_regime(self) -> dict[str, Any]:
        self.ensure_bridge()
        now_epoch = time.time()
        if now_epoch - self.last_regime_fetch_epoch < float(self.params["regime_refresh_seconds"]):
            return self.cached_regime
        self.last_regime_fetch_epoch = now_epoch
        try:
            h1 = self.dm.get_historical_data(
                self.symbol,
                int(self.params["regime_timeframe"]),
                int(self.params["regime_bars"]),
            )
            if h1 is None or len(h1) < 80:
                raise ValueError("not enough H1 bars")
            h1 = h1.sort_index()
            if bool(self.params["drop_latest_h1_bar"]):
                if len(h1) < 2:
                    raise ValueError("not enough H1 bars after dropping current bar")
                closed = h1.iloc[:-1].copy()
                current_open = h1.index[-1]
                signal_time = h1.index[-2] + pd.Timedelta(hours=1)
                age_minutes = (current_open - signal_time).total_seconds() / 60.0
                signal_fresh = 0.0 <= age_minutes <= float(self.params["max_signal_age_minutes"])
            else:
                closed = h1.copy()
                signal_time = h1.index[-1]
                age_minutes = 0.0
                signal_fresh = True
            regime = build_trend_regime_from_h1(closed, self.params)
            if regime.empty:
                raise ValueError("regime output is empty")
            last = regime.iloc[-1]
            allowed = bool(last.get("TrendAllowed", False)) and bool(signal_fresh)
            result = {
                "entry_allowed": allowed,
                "signal_fresh": bool(signal_fresh),
                "trend_allowed_raw": bool(last.get("TrendAllowed", False)),
                "trend_direction": int(last.get("TrendDirection", 0)),
                "efficiency_ratio": float(last.get("EfficiencyRatio", 0.0) or 0.0),
                "adx": float(last.get("ADX", 0.0) or 0.0),
                "displacement_atr": float(last.get("DisplacementATR", 0.0) or 0.0),
                "signal_age_minutes": float(age_minutes),
                "signal_time": str(signal_time),
                "reason": "ok",
            }
        except Exception as exc:
            logging.warning(f"Regime fetch failed: {exc}")
            result = self.default_regime()
            result["reason"] = str(exc)
        self.cached_regime = result
        self.state["last_regime"] = result
        return result

    def get_m1_policy_features(self) -> dict[str, float]:
        self.ensure_bridge()
        m1 = self.dm.get_historical_data(
            self.symbol,
            int(self.params["m1_timeframe"]),
            int(self.params["m1_bars"]),
        )
        if m1 is None or len(m1) < 16:
            raise ValueError("not enough M1 bars")
        m1 = m1.sort_index()
        if bool(self.params.get("drop_latest_m1_bar", True)):
            if len(m1) < 17:
                raise ValueError("not enough M1 bars after dropping current bar")
            m1 = m1.iloc[:-1].copy()
        return build_m1_feature_row(m1, self.pip_size)

    def build_cycle_start_event_features(
        self,
        tick: dict[str, Any],
        regime: dict[str, Any],
    ) -> dict[str, Any]:
        decision_time_utc = datetime.now(timezone.utc)
        effective_spread_points = float(tick["spread_points"]) + float(self.params["policy_spread_add_points"])
        features: dict[str, Any] = {
            "spread_gate_pass": int(
                effective_spread_points <= float(self.params["policy_max_entry_spread_points"]) + 1e-9
            ),
            "spread_points_decision": effective_spread_points,
            "h1_signal_age_minutes": float(regime.get("signal_age_minutes", 999999.0) or 999999.0),
            "h1_adx": float(regime.get("adx", 0.0) or 0.0),
            "h1_efficiency_ratio": float(regime.get("efficiency_ratio", 0.0) or 0.0),
            "h1_displacement_atr": float(regime.get("displacement_atr", 0.0) or 0.0),
            "h1_trend_direction": int(regime.get("trend_direction", 0) or 0),
            "bid_decision": float(tick["bid"]),
        }
        features.update(self.get_m1_policy_features())
        features.update(session_features(decision_time_utc))
        return features

    def log_policy_decision(self, decision: dict[str, Any]) -> None:
        header = [
            "Timestamp_JST",
            "Symbol",
            "CandidateId",
            "Model",
            "Allowed",
            "PredProba",
            "Threshold",
            "Reason",
            "ActualSpreadPoints",
            "EffectiveSpreadPoints",
            "H1ADX",
            "H1AgeMinutes",
            "M1ATRPips",
        ]
        row = [
            jst_now().strftime("%Y-%m-%d %H:%M:%S"),
            self.symbol,
            decision.get("candidate_id", ""),
            decision.get("model", ""),
            int(bool(decision.get("allow", False))),
            decision.get("pred_proba", ""),
            decision.get("threshold", ""),
            decision.get("reason", ""),
            decision.get("actual_spread_points", ""),
            decision.get("spread_points_decision", ""),
            decision.get("h1_adx", ""),
            decision.get("h1_signal_age_minutes", ""),
            decision.get("m1_atr_pips", ""),
        ]
        append_csv_row(self.policy_log_file, header, row)

    def evaluate_cycle_start_policy(
        self,
        tick: dict[str, Any],
        regime: dict[str, Any],
    ) -> dict[str, Any]:
        if not bool(self.params.get("policy_enabled", True)):
            return {"allow": True, "reason": "policy_disabled"}
        if self.policy is None:
            reason = "policy_not_loaded"
            return {"allow": not bool(self.params.get("policy_fail_closed", True)), "reason": reason}
        try:
            features = self.build_cycle_start_event_features(tick, regime)
            result = self.policy.predict(self.symbol, features)
            result.update(features)
            result["reason"] = "threshold_pass" if bool(result["allow"]) else "threshold_block"
            result["actual_spread_points"] = float(tick["spread_points"])
        except Exception as exc:
            result = {
                "allow": not bool(self.params.get("policy_fail_closed", True)),
                "reason": f"policy_error:{exc}",
                "actual_spread_points": float(tick.get("spread_points", 0.0)),
            }
        self.state["last_policy_decision"] = {
            key: (float(value) if isinstance(value, np.floating) else value)
            for key, value in result.items()
            if key in {
                "allow",
                "candidate_id",
                "model",
                "threshold",
                "pred_proba",
                "reason",
                "actual_spread_points",
                "spread_points_decision",
            }
        }
        self.log_policy_decision(result)
        return result

    def is_weekend_entry_blocked(self) -> bool:
        if not bool(self.params["weekend_hold"]):
            return False
        return is_in_weekly_window(
            jst_now(),
            int(self.params["weekend_entry_stop_weekday_jst"]),
            int(self.params["weekend_entry_stop_hour_jst"]),
            int(self.params["weekend_entry_stop_minute_jst"]),
            0,
            int(self.params["monday_start_hour_jst"]),
            int(self.params["monday_start_minute_jst"]),
        )

    def coverage_counts(self) -> dict[tuple[int, float], int]:
        coverage: dict[tuple[int, float], int] = {}
        for position in self.state["positions"]:
            key = (direction_from_name(position["direction"]), self.price(position["stop_loss"]))
            coverage[key] = coverage.get(key, 0) + 1
        for order in self.state["virtual_orders"]:
            key = (direction_from_name(order["direction"]), self.price(order["stop_loss"]))
            coverage[key] = coverage.get(key, 0) + 1
        return coverage

    def add_virtual_order(
        self,
        direction: int,
        entry: float,
        stop_loss: float,
        info: Any | None = None,
        bid: float | None = None,
        ask: float | None = None,
        regime: dict[str, Any] | None = None,
    ) -> bool:
        if len(self.state["virtual_orders"]) >= int(self.params["max_virtual_orders"]):
            self.block_new_entries("max virtual orders reached")
            return False
        entry = self.price(entry)
        stop_loss = self.price(stop_loss)
        key = (direction, stop_loss)
        if self.coverage_counts().get(key, 0) > 0:
            return True
        order = {
            "order_id": int(self.state["next_order_id"]),
            "symbol": self.symbol,
            "magic": self.magic,
            "direction": direction_name(direction),
            "entry": entry,
            "stop_loss": stop_loss,
            "crossed_while_spread_blocked": False,
            "created_at_jst": jst_now().isoformat(),
            "pending_ticket": None,
        }
        if self.live_server_pending_enabled():
            if info is None or bid is None or ask is None or regime is None:
                self.block_new_entries("pending entry requires tick/regime context")
                return False
            if self.state.get("sync_block_new_entries"):
                return False
            spread_points = (float(ask) - float(bid)) / self.point_size
            if spread_points > float(self.params["max_entry_spread_points"]) + 1e-9:
                return True
            if not self.virtual_order_fill_allowed(order, float(bid), float(ask), regime):
                return True
            lot = self.normalize_lot(info)
            order_type = ORDER_TYPE_BUY_STOP if direction == LONG else ORDER_TYPE_SELL_STOP
            price_block_reason = self.pending_stop_price_block_reason(
                direction,
                entry,
                stop_loss,
                float(bid),
                float(ask),
                info,
            )
            if price_block_reason is not None:
                logging.warning("S19 pending stop price gate blocked order send: %s", price_block_reason)
                return False
            sl = stop_loss if bool(self.params["use_server_sl"]) else 0.0
            comment = f"{self.params['comment_prefix']}_{self.state['cycle_id']}_{order['order_id']}"
            request_id = f"{self.symbol}-{self.magic}-{self.state['cycle_id']}-{order['order_id']}"
            self.state["pending_open"] = {
                "type": "server_pending_stop",
                "request_id": request_id,
                "symbol": self.symbol,
                "magic": self.magic,
                "order_id": int(order["order_id"]),
                "direction": direction_name(direction),
                "entry": entry,
                "stop_loss": stop_loss,
                "volume": lot,
                "comment": comment,
                "status": "REQUEST_PREPARED",
                "created_at_jst": jst_now().isoformat(),
            }
            try:
                self.save_state()
            except Exception as exc:
                logging.critical("Could not persist pending_open before order send: %s", exc)
                self.block_new_entries("pending_open persistence failed before order send")
                return False
            pending_ticket = self.executor.place_stop_order(
                self.symbol,
                order_type,
                lot,
                entry,
                sl=sl,
                tp=0.0,
                magic=self.magic,
                comment=comment,
                digits=self.digits,
            )
            if pending_ticket is None:
                error = getattr(self.executor, "last_order_error", "UNKNOWN")
                pending_open = self.state.get("pending_open")
                if isinstance(pending_open, dict):
                    pending_open["status"] = "OPEN_RESPONSE_UNCONFIRMED"
                    pending_open["last_error"] = error
                    pending_open["last_checked_at_jst"] = jst_now().isoformat()
                self.state["reconciliation_required"] = {
                    "type": "pending_open",
                    "request_id": request_id,
                    "symbol": self.symbol,
                    "magic": self.magic,
                    "reason": f"pending stop placement failed: {error}",
                    "created_at_jst": jst_now().isoformat(),
                }
                self.block_new_entries(f"pending stop placement failed for virtual order {order['order_id']}")
                try:
                    self.save_state()
                except Exception:
                    logging.exception("Could not persist pending_open failure state")
                return False
            order["pending_ticket"] = int(pending_ticket)
            order["symbol"] = self.symbol
            order["magic"] = self.magic
            order["volume"] = lot
            order["comment"] = comment
            self.state["pending_open"] = None
            self.log_trade_csv(
                "PENDING_ENTRY",
                int(pending_ticket),
                direction=direction_name(direction),
                lot_size=lot,
                price=entry,
                stop_loss=stop_loss,
                pnl="",
                reason="server_stop_order_placed",
                source_order_id=int(order["order_id"]),
                comment=comment,
            )
        self.state["next_order_id"] = int(self.state["next_order_id"]) + 1
        self.state["virtual_orders"].append(order)
        if self.live_server_pending_enabled():
            self.save_state()
        return True

    def ensure_orders(
        self,
        direction: int,
        info: Any | None = None,
        bid: float | None = None,
        ask: float | None = None,
        regime: dict[str, Any] | None = None,
    ) -> bool:
        anchor = float(self.state["grid_anchor"])
        distance = self.distance_price
        if direction == LONG:
            first_ok = self.add_virtual_order(LONG, anchor + distance, anchor, info, bid, ask, regime)
            second_ok = self.add_virtual_order(LONG, anchor + 2.0 * distance, anchor + distance, info, bid, ask, regime)
            return bool(first_ok and second_ok)
        first_ok = self.add_virtual_order(SHORT, anchor - distance, anchor, info, bid, ask, regime)
        second_ok = self.add_virtual_order(SHORT, anchor - 2.0 * distance, anchor - distance, info, bid, ask, regime)
        return bool(first_ok and second_ok)

    def start_cycle(
        self,
        bid: float,
        info: Any | None = None,
        ask: float | None = None,
        regime: dict[str, Any] | None = None,
    ) -> bool:
        if self.state["positions"] or self.state["virtual_orders"]:
            raise RuntimeError("cycle start requires flat state")
        if self.live_server_pending_enabled() and (
            info is None or bid is None or ask is None or regime is None
        ):
            self.block_new_entries("pending cycle start requires tick/regime context")
            return False
        self.state["cycle_id"] = int(self.state["cycle_id"]) + 1
        self.state["cycle_realized_usd"] = 0.0
        self.state["grid_anchor"] = self.price(bid)
        self.state["auto_tp_price"] = None
        self.state["estimated_auto_tp_profit_usd"] = 0.0
        self.state["restart_next_tick"] = False
        self.state["dynamic_sl_streak"] = 0
        self.state["dynamic_cycle_exposure_blocks"] = 0
        ok = self.ensure_orders(LONG, info, bid, ask, regime)
        ok = self.ensure_orders(SHORT, info, bid, ask, regime) and ok
        if not ok:
            logging.critical(
                "S19 cycle start did not place a complete pending grid; canceling created pending orders"
            )
            self.clear_virtual_orders()
            self.clear_auto_tp()
            self.state["grid_anchor"] = None
            self.state["restart_next_tick"] = False
            self.block_new_entries("cycle start pending grid placement failed")
            return False
        logging.info(f"Started s19 cycle {self.state['cycle_id']} anchor={self.state['grid_anchor']}")
        return True

    def block_new_entries(self, reason: str) -> None:
        self.state["sync_block_new_entries"] = True
        self.state["sync_block_reason"] = reason
        logging.error(f"New entries blocked: {reason}")
        reconciliation = self.state.get("reconciliation_required")
        if isinstance(reconciliation, dict):
            self.notify_reconciliation_required(reason, reconciliation)

    def reconciliation_block_reason(self) -> str | None:
        reconciliation = self.state.get("reconciliation_required")
        if isinstance(reconciliation, dict):
            return str(reconciliation.get("reason") or reconciliation.get("type") or "reconciliation required")
        pending_open = self.state.get("pending_open")
        if isinstance(pending_open, dict):
            return f"unresolved pending_open request: {pending_open.get('request_id')}"
        return None

    def clear_new_entry_block_if_reason(self, reason: str) -> None:
        if not self.state.get("sync_block_new_entries"):
            return
        if self.state.get("sync_block_reason") != reason:
            return
        self.state["sync_block_new_entries"] = False
        self.state["sync_block_reason"] = None
        logging.warning(f"New-entry block cleared after recovery: {reason}")

    def pending_order_matches_position(self, order: dict[str, Any], position: Any) -> bool:
        if int(getattr(position, "magic", -1)) != int(self.magic):
            return False
        if str(getattr(position, "symbol", "")) != self.symbol:
            return False
        comment = str(order.get("comment") or "")
        if comment and str(getattr(position, "comment", "")) == comment:
            return True
        expected_direction = direction_name(direction_from_name(order["direction"]))
        if str(getattr(position, "direction", "")) != expected_direction:
            return False
        expected_sl = self.price(float(order["stop_loss"]))
        live_sl = self.price(float(getattr(position, "sl", 0.0) or 0.0))
        if abs(expected_sl - live_sl) > self.pip_size * 0.2:
            return False
        return abs(float(getattr(position, "open_price", 0.0)) - float(order["entry"])) <= self.distance_price * 0.5

    def state_position_identity_mismatch_reason(self, state_position: dict[str, Any]) -> str | None:
        state_symbol = str(state_position.get("symbol", "") or "").upper()
        if state_symbol and state_symbol != self.symbol:
            return f"state_symbol={state_symbol} expected={self.symbol}"
        state_magic = state_position.get("magic")
        if state_magic not in (None, ""):
            try:
                if int(state_magic) != int(self.magic):
                    return f"state_magic={int(state_magic)} expected={int(self.magic)}"
            except (TypeError, ValueError):
                return f"state_magic_unreadable={state_magic}"
        return None

    def live_position_ownership_mismatch_reason(
        self,
        live_position: Any,
        state_position: dict[str, Any] | None = None,
    ) -> str | None:
        if state_position is not None:
            state_mismatch = self.state_position_identity_mismatch_reason(state_position)
            if state_mismatch:
                return state_mismatch
        live_symbol = str(getattr(live_position, "symbol", "") or "").upper()
        if live_symbol != self.symbol:
            return f"live_symbol={live_symbol} expected={self.symbol}"
        try:
            live_magic = int(getattr(live_position, "magic", -1))
        except (TypeError, ValueError):
            return f"live_magic_unreadable={getattr(live_position, 'magic', None)}"
        if live_magic != int(self.magic):
            return f"live_magic={live_magic} expected={int(self.magic)}"
        live_comment = str(getattr(live_position, "comment", "") or "")
        expected_comment = str((state_position or {}).get("comment", "") or "")
        if expected_comment and live_comment != expected_comment:
            return f"live_comment={live_comment} expected={expected_comment}"
        if not expected_comment:
            prefix = str(self.params["comment_prefix"])
            if live_comment and not live_comment.startswith(f"{prefix}_"):
                return f"live_comment_prefix={live_comment} expected_prefix={prefix}_"
        return None

    def state_pending_order_identity_mismatch_reason(self, order: dict[str, Any]) -> str | None:
        state_symbol = str(order.get("symbol", "") or "").upper()
        if state_symbol and state_symbol != self.symbol:
            return f"state_symbol={state_symbol} expected={self.symbol}"
        state_magic = order.get("magic")
        if state_magic not in (None, ""):
            try:
                if int(state_magic) != int(self.magic):
                    return f"state_magic={int(state_magic)} expected={int(self.magic)}"
            except (TypeError, ValueError):
                return f"state_magic_unreadable={state_magic}"
        return None

    def live_pending_order_ownership_mismatch_reason(self, live_order: Any, order: dict[str, Any] | None = None) -> str | None:
        if order is not None:
            state_mismatch = self.state_pending_order_identity_mismatch_reason(order)
            if state_mismatch:
                return state_mismatch
        live_symbol = str(getattr(live_order, "symbol", "") or "").upper()
        if live_symbol != self.symbol:
            return f"live_symbol={live_symbol} expected={self.symbol}"
        try:
            live_magic = int(getattr(live_order, "magic", -1))
        except (TypeError, ValueError):
            return f"live_magic_unreadable={getattr(live_order, 'magic', None)}"
        if live_magic != int(self.magic):
            return f"live_magic={live_magic} expected={int(self.magic)}"
        live_comment = str(getattr(live_order, "comment", "") or "")
        expected_comment = str((order or {}).get("comment", "") or "")
        if expected_comment and live_comment != expected_comment:
            return f"live_comment={live_comment} expected={expected_comment}"
        if not expected_comment:
            prefix = str(self.params["comment_prefix"])
            if live_comment and not live_comment.startswith(f"{prefix}_"):
                return f"live_comment_prefix={live_comment} expected_prefix={prefix}_"
        return None

    def confirm_state_ticket_absent_for_assumed_sl(self, ticket: int, state_position: dict[str, Any]) -> bool:
        state_mismatch = self.state_position_identity_mismatch_reason(state_position)
        if state_mismatch:
            self.block_new_entries(f"state position identity mismatch for ticket {ticket}: {state_mismatch}")
            return False
        get_position = getattr(self.executor, "get_position", None)
        confirm_absent = getattr(self.executor, "confirm_position_absent", None)
        if not callable(get_position) or not callable(confirm_absent):
            self.block_new_entries(f"ticket owner check unavailable for state ticket {ticket}")
            return False
        live_position = get_position(ticket)
        if live_position is not None:
            mismatch = self.live_position_ownership_mismatch_reason(live_position, state_position)
            if mismatch:
                self.block_new_entries(f"state ticket ownership mismatch for ticket {ticket}: {mismatch}")
            else:
                self.block_new_entries(f"state ticket still exists but was missing from bot position list: {ticket}")
            return False
        absent = confirm_absent(ticket)
        if absent is True:
            return True
        if absent is False:
            self.block_new_entries(f"state ticket still exists but could not be read: {ticket}")
        else:
            self.block_new_entries(f"state ticket absence not confirmed: {ticket}")
        return False

    def confirm_state_ticket_owned_for_action(self, state_position: dict[str, Any], action: str) -> bool:
        ticket = int(state_position["ticket"])
        state_mismatch = self.state_position_identity_mismatch_reason(state_position)
        if state_mismatch:
            self.block_new_entries(f"{action} refused; state position identity mismatch for ticket {ticket}: {state_mismatch}")
            return False
        get_position = getattr(self.executor, "get_position", None)
        confirm_absent = getattr(self.executor, "confirm_position_absent", None)
        if not callable(get_position) or not callable(confirm_absent):
            self.block_new_entries(f"{action} refused; ticket owner check unavailable for ticket {ticket}")
            return False
        live_position = get_position(ticket)
        if live_position is None:
            absent = confirm_absent(ticket)
            if absent is True:
                self.block_new_entries(f"state ticket missing on MT5: {ticket}")
            elif absent is False:
                self.block_new_entries(f"{action} refused; ticket exists but could not be read: {ticket}")
            else:
                self.block_new_entries(f"{action} refused; ticket absence not confirmed: {ticket}")
            return False
        mismatch = self.live_position_ownership_mismatch_reason(live_position, state_position)
        if mismatch:
            self.block_new_entries(f"{action} refused; ticket ownership mismatch for ticket {ticket}: {mismatch}")
            return False
        return True

    def confirm_pending_ticket_owned_for_action(self, order: dict[str, Any], action: str) -> bool:
        pending_ticket = order.get("pending_ticket")
        if pending_ticket in (None, ""):
            return True
        ticket = int(pending_ticket)
        state_mismatch = self.state_pending_order_identity_mismatch_reason(order)
        if state_mismatch:
            self.block_new_entries(f"{action} refused; state pending identity mismatch for ticket {ticket}: {state_mismatch}")
            return False
        live_orders = self.executor.get_orders(self.symbol, self.magic)
        if live_orders is None:
            self.block_new_entries(f"{action} refused; pending order owner check failed for ticket {ticket}")
            return False
        matches = [live_order for live_order in live_orders if int(getattr(live_order, "ticket", -1)) == ticket]
        if len(matches) != 1:
            self.block_new_entries(f"{action} refused; pending ticket not confirmed as bot-owned: {ticket}")
            return False
        mismatch = self.live_pending_order_ownership_mismatch_reason(matches[0], order)
        if mismatch:
            self.block_new_entries(f"{action} refused; pending ticket ownership mismatch for ticket {ticket}: {mismatch}")
            return False
        return True

    def normalize_pending_recovery_order(self, raw_order: dict[str, Any], source: str) -> dict[str, Any] | None:
        try:
            direction = direction_name(direction_from_name(str(raw_order["direction"])))
            entry = self.price(float(raw_order["entry"]))
            stop_loss = self.price(float(raw_order["stop_loss"]))
            order_id = int(raw_order["order_id"])
        except (KeyError, TypeError, ValueError):
            return None
        pending_ticket_raw = raw_order.get("pending_ticket", raw_order.get("ticket"))
        pending_ticket = None
        if pending_ticket_raw not in (None, ""):
            try:
                pending_ticket = int(pending_ticket_raw)
            except (TypeError, ValueError):
                pending_ticket = None
        try:
            raw_magic = int(raw_order.get("magic", self.magic) or self.magic)
        except (TypeError, ValueError):
            raw_magic = self.magic
        return {
            "order_id": order_id,
            "symbol": str(raw_order.get("symbol", self.symbol) or self.symbol).upper(),
            "magic": raw_magic,
            "direction": direction,
            "entry": entry,
            "stop_loss": stop_loss,
            "pending_ticket": pending_ticket,
            "volume": float(raw_order.get("volume", self.params["lot"]) or self.params["lot"]),
            "comment": str(raw_order.get("comment", "") or ""),
            "recovery_source": source,
        }

    def add_pending_recovery_candidates(
        self,
        candidates: list[dict[str, Any]],
        seen: set[tuple[Any, ...]],
        rows: Any,
        source: str,
    ) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            candidate = self.normalize_pending_recovery_order(row, source)
            if candidate is None:
                continue
            key = (
                candidate.get("order_id"),
                candidate.get("pending_ticket"),
                candidate.get("direction"),
                candidate.get("entry"),
                candidate.get("stop_loss"),
                candidate.get("comment"),
            )
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)

    def pending_recovery_candidate_orders(self) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        self.add_pending_recovery_candidates(candidates, seen, self.state.get("virtual_orders"), "state.virtual_orders")

        pending_open = self.state.get("pending_open")
        if isinstance(pending_open, dict):
            self.add_pending_recovery_candidates(candidates, seen, [pending_open], "state.pending_open")

        reconciliation = self.state.get("reconciliation_required")
        if isinstance(reconciliation, dict):
            details = reconciliation.get("details")
            if isinstance(details, dict):
                self.add_pending_recovery_candidates(
                    candidates,
                    seen,
                    details.get("state"),
                    "reconciliation_required.details.state",
                )
                self.add_pending_recovery_candidates(
                    candidates,
                    seen,
                    details.get("state_after_reissue"),
                    "reconciliation_required.details.state_after_reissue",
                )

        pending_wait = self.state.get("pending_grid_repair_wait")
        if isinstance(pending_wait, dict):
            details = pending_wait.get("details")
            if isinstance(details, dict):
                self.add_pending_recovery_candidates(
                    candidates,
                    seen,
                    details.get("state"),
                    "pending_grid_repair_wait.details.state",
                )
        return candidates

    def pending_recovery_order_match_score(self, order: dict[str, Any], position: Any) -> int:
        if int(getattr(position, "magic", -1)) != int(self.magic):
            return 0
        if str(getattr(position, "symbol", "")) != self.symbol:
            return 0
        try:
            expected_direction = direction_from_name(str(order["direction"]))
            live_direction = direction_from_name(str(getattr(position, "direction", "")))
        except ValueError:
            return 0
        if live_direction != expected_direction:
            return 0
        expected_sl = self.price(float(order["stop_loss"]))
        live_sl = self.price(float(getattr(position, "sl", 0.0) or 0.0))
        if abs(expected_sl - live_sl) > self.pip_size * 0.2:
            return 0
        pending_ticket = order.get("pending_ticket")
        if pending_ticket not in (None, "") and int(getattr(position, "ticket", -1)) == int(pending_ticket):
            return 3
        comment = str(order.get("comment") or "")
        if comment and str(getattr(position, "comment", "")) == comment:
            return 2
        if abs(float(getattr(position, "open_price", 0.0)) - float(order["entry"])) <= self.distance_price * 0.5:
            return 1
        return 0

    def pending_recovery_order_matches_position(self, order: dict[str, Any], position: Any) -> bool:
        return self.pending_recovery_order_match_score(order, position) > 0

    def recover_untracked_live_positions_from_pending_history(
        self,
        live_positions: list[Any],
        reason: str,
    ) -> bool:
        if not live_positions:
            return True
        candidates = self.pending_recovery_candidate_orders()
        if not candidates:
            return False

        recoveries: list[tuple[int, dict[str, Any], Any]] = []
        used_candidate_indexes: set[int] = set()
        for live_position in live_positions:
            matches: list[tuple[int, int, dict[str, Any]]] = []
            for index, candidate in enumerate(candidates):
                if index in used_candidate_indexes:
                    continue
                score = self.pending_recovery_order_match_score(candidate, live_position)
                if score > 0:
                    matches.append((score, index, candidate))
            best_score = max((score for score, _index, _candidate in matches), default=0)
            best_matches = [
                (index, candidate)
                for score, index, candidate in matches
                if score == best_score
            ]
            if len(best_matches) != 1:
                logging.error(
                    "S19 cannot auto-recover untracked live position ticket=%s; best_score=%s matches=%s candidates=%s",
                    int(getattr(live_position, "ticket", 0)),
                    best_score,
                    len(best_matches),
                    [
                        {
                            "order_id": item.get("order_id"),
                            "pending_ticket": item.get("pending_ticket"),
                            "comment": item.get("comment"),
                            "source": item.get("recovery_source"),
                        }
                        for item in candidates
                    ],
                )
                return False
            index, candidate = best_matches[0]
            used_candidate_indexes.add(index)
            recoveries.append((index, candidate, live_position))

        for _index, order, live_position in recoveries:
            self.adopt_filled_pending_order(
                order,
                live_position,
                event="ENTRY_RECOVERED_SYNC",
                reason=reason,
                recovered=True,
            )
        self.state["pending_open"] = None
        self.state["pending_grid_repair_wait"] = None
        self.state["pending_fill_sync_wait"] = None
        self.state["reconciliation_required"] = None
        self.state["sync_block_new_entries"] = False
        self.state["sync_block_reason"] = None
        self.recalculate_position_counts()
        logging.warning(
            "S19 recovered %s untracked live position(s) from pending history reason=%s",
            len(recoveries),
            reason,
        )
        return True

    def clear_flat_reconciliation_if_confirmed(self, live_positions: list[Any], live_orders: list[Any]) -> bool:
        if self.state["positions"] or self.state["virtual_orders"]:
            return False
        if live_positions or live_orders:
            return False
        has_reconciliation = isinstance(self.state.get("reconciliation_required"), dict)
        has_pending_open = isinstance(self.state.get("pending_open"), dict)
        has_repair_wait = isinstance(self.state.get("pending_grid_repair_wait"), dict)
        if not (has_reconciliation or has_pending_open or has_repair_wait):
            return False

        logging.warning(
            "S19 cleared unresolved reconciliation after MT5 confirmed flat: reconciliation=%s pending_open=%s repair_wait=%s reason=%s",
            has_reconciliation,
            has_pending_open,
            has_repair_wait,
            self.state.get("sync_block_reason"),
        )
        self.state["reconciliation_required"] = None
        self.state["pending_open"] = None
        self.state["pending_grid_repair_wait"] = None
        self.state["pending_fill_sync_wait"] = None
        self.state["grid_anchor"] = None
        self.state["cycle_realized_usd"] = 0.0
        self.state["restart_next_tick"] = False
        self.state["sync_block_new_entries"] = False
        self.state["sync_block_reason"] = None
        self.clear_auto_tp()
        return True

    def clear_pending_fill_sync_wait(self) -> None:
        self.state["pending_fill_sync_wait"] = None

    def defer_flat_pending_grid_repair_for_fill_sync(self, anomaly: dict[str, Any]) -> bool:
        missing_tickets = sorted(
            int(ticket)
            for ticket in anomaly.get("missing_tickets", [])
            if ticket not in (None, "")
        )
        if not missing_tickets:
            self.clear_pending_fill_sync_wait()
            return False
        if anomaly.get("extra_tickets") or self.state.get("positions"):
            self.clear_pending_fill_sync_wait()
            return False
        state_count = int(anomaly.get("state_count") or 0)
        live_count = int(anomaly.get("live_count") or 0)
        if state_count <= 0 or live_count >= state_count:
            self.clear_pending_fill_sync_wait()
            return False
        grace_seconds = max(0.0, float(self.params.get("pending_fill_sync_grace_seconds", 5.0) or 0.0))
        if grace_seconds <= 0.0:
            self.clear_pending_fill_sync_wait()
            return False

        now = time.time()
        wait = self.state.get("pending_fill_sync_wait")
        same_wait = (
            isinstance(wait, dict)
            and sorted(int(ticket) for ticket in wait.get("missing_tickets", [])) == missing_tickets
            and int(wait.get("cycle_id", 0)) == int(self.state.get("cycle_id", 0))
        )
        if not same_wait:
            self.state["pending_fill_sync_wait"] = {
                "reason": "pending_order_missing_waiting_for_position_sync",
                "missing_tickets": missing_tickets,
                "cycle_id": int(self.state.get("cycle_id", 0)),
                "first_seen_epoch": now,
                "first_seen_jst": jst_now().isoformat(),
                "details": anomaly,
            }
            logging.warning(
                "S19 pending order missing from live ORDERS; deferring flat grid repair for fill sync: tickets=%s grace=%.1fs",
                missing_tickets,
                grace_seconds,
            )
            return True

        first_seen_epoch = float(wait.get("first_seen_epoch", now))
        elapsed = now - first_seen_epoch
        wait["last_seen_epoch"] = now
        wait["last_seen_jst"] = jst_now().isoformat()
        wait["details"] = anomaly
        if elapsed < grace_seconds:
            logging.warning(
                "S19 pending order still missing; waiting for position sync before flat grid repair: tickets=%s elapsed=%.1fs grace=%.1fs",
                missing_tickets,
                elapsed,
                grace_seconds,
            )
            return True

        logging.warning(
            "S19 pending fill sync grace expired; continuing flat grid repair: tickets=%s elapsed=%.1fs grace=%.1fs",
            missing_tickets,
            elapsed,
            grace_seconds,
        )
        self.clear_pending_fill_sync_wait()
        return False

    def adopt_filled_pending_order(
        self,
        order: dict[str, Any],
        live_position: Any,
        *,
        event: str = "ENTRY",
        reason: str = "server_pending_stop_filled",
        recovered: bool = False,
    ) -> None:
        ticket = int(getattr(live_position, "ticket"))
        direction = direction_from_name(order["direction"])
        self.state["virtual_orders"] = [
            item for item in self.state["virtual_orders"] if int(item["order_id"]) != int(order["order_id"])
        ]
        position = {
            "ticket": ticket,
            "symbol": self.symbol,
            "magic": self.magic,
            "direction": direction_name(direction),
            "entry": self.price(float(getattr(live_position, "open_price"))),
            "stop_loss": self.price(float(order["stop_loss"])),
            "volume": float(getattr(live_position, "volume", order.get("volume", self.params["lot"]))),
            "opened_at_jst": jst_now().isoformat(),
            "source_order_id": int(order["order_id"]),
            "comment": str(getattr(live_position, "comment", order.get("comment", ""))),
        }
        if recovered:
            position["recovered_from_live"] = True
            position["recovery_reason"] = reason
            position["recovery_source"] = str(order.get("recovery_source", ""))
        self.state["positions"].append(position)
        self.state["pending_fill_sync_wait"] = None
        self.log_trade_csv(
            event,
            ticket,
            direction=direction_name(direction),
            lot_size=float(getattr(live_position, "volume", order.get("volume", self.params["lot"]))),
            price=self.price(float(getattr(live_position, "open_price"))),
            stop_loss=self.price(float(order["stop_loss"])),
            pnl="",
            reason=reason,
            source_order_id=int(order["order_id"]),
            comment=str(getattr(live_position, "comment", order.get("comment", ""))),
        )
        logging.info(
            "Adopted filled pending order %s as position ticket=%s reason=%s",
            int(order.get("pending_ticket") or 0),
            ticket,
            reason,
        )

    def cancel_server_pending_order(self, order: dict[str, Any]) -> bool:
        pending_ticket = order.get("pending_ticket")
        if not pending_ticket:
            return True
        if not self.confirm_pending_ticket_owned_for_action(order, "pending cancel"):
            return False
        if self.executor.cancel_order(int(pending_ticket)):
            self.log_trade_csv(
                "PENDING_CANCEL",
                int(pending_ticket),
                direction=str(order.get("direction", "")),
                lot_size=float(order.get("volume", self.params["lot"])),
                price=self.price(float(order.get("entry", 0.0))),
                stop_loss=self.price(float(order.get("stop_loss", 0.0))),
                pnl="",
                reason="local_virtual_order_removed",
                source_order_id=order.get("order_id", ""),
                comment=str(order.get("comment", "")),
            )
            return True
        self.block_new_entries(f"failed to cancel pending order {pending_ticket}")
        return False

    def clear_virtual_orders(self) -> bool:
        remaining_orders = []
        for order in list(self.state["virtual_orders"]):
            if not self.cancel_server_pending_order(order):
                remaining_orders.append(order)
        self.state["virtual_orders"] = remaining_orders
        return not remaining_orders

    def expected_flat_pending_grid_tuples(self) -> list[tuple[str, float, float]]:
        anchor = self.price(float(self.state["grid_anchor"]))
        distance = self.distance_price
        return sorted(
            [
                ("LONG", self.price(anchor + distance), anchor),
                ("LONG", self.price(anchor + 2.0 * distance), self.price(anchor + distance)),
                ("SHORT", self.price(anchor - distance), anchor),
                ("SHORT", self.price(anchor - 2.0 * distance), self.price(anchor - distance)),
            ]
        )

    def state_pending_grid_tuples(self) -> list[tuple[str, float, float]]:
        return sorted(
            [
                (
                    str(order.get("direction", "")),
                    self.price(float(order.get("entry", 0.0))),
                    self.price(float(order.get("stop_loss", 0.0))),
                )
                for order in self.state["virtual_orders"]
            ]
        )

    def live_pending_grid_tuples(self, live_orders: list[Any]) -> list[tuple[str, float, float]]:
        rows: list[tuple[str, float, float]] = []
        for order in live_orders:
            order_type = int(getattr(order, "type", -1))
            if order_type == ORDER_TYPE_BUY_STOP:
                direction = "LONG"
            elif order_type == ORDER_TYPE_SELL_STOP:
                direction = "SHORT"
            else:
                direction = f"TYPE_{order_type}"
            rows.append(
                (
                    direction,
                    self.price(float(getattr(order, "price_open", 0.0))),
                    self.price(float(getattr(order, "sl", 0.0) or 0.0)),
                )
            )
        return sorted(rows)

    def state_pending_snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "order_id": order.get("order_id"),
                "ticket": order.get("pending_ticket"),
                "direction": order.get("direction"),
                "entry": order.get("entry"),
                "stop_loss": order.get("stop_loss"),
                "comment": order.get("comment"),
            }
            for order in self.state["virtual_orders"]
        ]

    def live_pending_snapshot(self, live_orders: list[Any]) -> list[dict[str, Any]]:
        return [
            {
                "ticket": int(getattr(order, "ticket", 0)),
                "type": int(getattr(order, "type", -1)),
                "direction": str(getattr(order, "direction", "")),
                "entry": self.price(float(getattr(order, "price_open", 0.0))),
                "stop_loss": self.price(float(getattr(order, "sl", 0.0) or 0.0)),
                "comment": str(getattr(order, "comment", "")),
            }
            for order in live_orders
        ]

    def pending_stop_price_block_reason(
        self,
        direction: int,
        entry: float,
        stop_loss: float,
        bid: float,
        ask: float,
        info: Any,
    ) -> str | None:
        point = float(getattr(info, "point", self.point_size) or self.point_size)
        if point <= 0.0:
            point = self.point_size
        try:
            stops_level = max(0, int(float(getattr(info, "stops_level", 0) or 0)))
        except (TypeError, ValueError):
            stops_level = 0
        min_distance = stops_level * point
        epsilon = max(point * 0.1, 10 ** (-self.digits - 1))
        entry = self.price(entry)
        stop_loss = self.price(stop_loss)
        bid = self.price(bid)
        ask = self.price(ask)
        if direction == LONG:
            min_entry = self.price(ask + min_distance)
            if entry <= min_entry + epsilon:
                return (
                    "pending_price_block direction=LONG "
                    f"entry={entry:.{self.digits}f} ask={ask:.{self.digits}f} "
                    f"min_entry={min_entry:.{self.digits}f} stops_level={stops_level}"
                )
            if bool(self.params.get("use_server_sl", False)) and stop_loss:
                max_sl = self.price(entry - min_distance)
                if stop_loss >= max_sl - epsilon:
                    return (
                        "pending_sl_block direction=LONG "
                        f"entry={entry:.{self.digits}f} stop_loss={stop_loss:.{self.digits}f} "
                        f"max_sl={max_sl:.{self.digits}f} stops_level={stops_level}"
                    )
            return None
        if direction == SHORT:
            max_entry = self.price(bid - min_distance)
            if entry >= max_entry - epsilon:
                return (
                    "pending_price_block direction=SHORT "
                    f"entry={entry:.{self.digits}f} bid={bid:.{self.digits}f} "
                    f"max_entry={max_entry:.{self.digits}f} stops_level={stops_level}"
                )
            if bool(self.params.get("use_server_sl", False)) and stop_loss:
                min_sl = self.price(entry + min_distance)
                if stop_loss <= min_sl + epsilon:
                    return (
                        "pending_sl_block direction=SHORT "
                        f"entry={entry:.{self.digits}f} stop_loss={stop_loss:.{self.digits}f} "
                        f"min_sl={min_sl:.{self.digits}f} stops_level={stops_level}"
                    )
            return None
        return f"pending_price_block unsupported_direction={direction}"

    def flat_pending_grid_price_block_reason(self, tick: dict[str, Any]) -> str | None:
        info = tick.get("info")
        if info is None:
            return "pending_price_block missing_symbol_info"
        for direction_text, entry, stop_loss in self.expected_flat_pending_grid_tuples():
            reason = self.pending_stop_price_block_reason(
                direction_from_name(direction_text),
                entry,
                stop_loss,
                float(tick["bid"]),
                float(tick["ask"]),
                info,
            )
            if reason is not None:
                return reason
        return None

    def reanchor_flat_pending_grid_for_reissue(self, tick: dict[str, Any], reason: str) -> None:
        old_anchor = self.state.get("grid_anchor")
        old_cycle_id = int(self.state.get("cycle_id", 0))
        new_anchor = self.price(float(tick["bid"]))
        self.state["cycle_id"] = old_cycle_id + 1
        self.state["cycle_realized_usd"] = 0.0
        self.state["grid_anchor"] = new_anchor
        self.state["auto_tp_price"] = None
        self.state["estimated_auto_tp_profit_usd"] = 0.0
        self.state["restart_next_tick"] = False
        self.state["dynamic_sl_streak"] = 0
        self.state["dynamic_cycle_exposure_blocks"] = 0
        repair_wait = self.state.get("pending_grid_repair_wait")
        if isinstance(repair_wait, dict):
            repair_wait["reanchor_reason"] = reason
            repair_wait["reanchored_from_cycle_id"] = old_cycle_id
            repair_wait["reanchored_to_cycle_id"] = int(self.state["cycle_id"])
            repair_wait["reanchored_from_anchor"] = old_anchor
            repair_wait["reanchored_to_anchor"] = new_anchor
            repair_wait["reanchored_at_jst"] = jst_now().isoformat()
        logging.warning(
            "S19 reanchored flat pending grid before reissue: old_cycle=%s new_cycle=%s old_anchor=%s new_anchor=%s reason=%s",
            old_cycle_id,
            self.state["cycle_id"],
            old_anchor,
            new_anchor,
            reason,
        )

    def flat_pending_grid_expected(self) -> bool:
        return (
            bool(self.params.get("flat_pending_grid_repair_enabled", True))
            and self.live_server_pending_enabled()
            and int(self.state.get("cycle_id", 0)) > 0
            and self.state.get("grid_anchor") is not None
            and not bool(self.state.get("restart_next_tick"))
            and not self.state.get("positions")
            and not isinstance(self.state.get("pending_open"), dict)
            and not isinstance(self.state.get("reconciliation_required"), dict)
        )

    def flat_pending_grid_anomaly(self, live_orders: list[Any]) -> dict[str, Any] | None:
        if not self.flat_pending_grid_expected():
            return None

        state_orders = list(self.state["virtual_orders"])
        pending_tickets = {
            int(order["pending_ticket"])
            for order in state_orders
            if order.get("pending_ticket")
        }
        live_order_tickets = {int(order.ticket) for order in live_orders}
        expected_tuples = self.expected_flat_pending_grid_tuples()
        state_tuples = self.state_pending_grid_tuples()
        live_tuples = self.live_pending_grid_tuples(live_orders)
        details = {
            "reason": "flat_pending_grid_integrity_failed",
            "expected": expected_tuples,
            "state": self.state_pending_snapshot(),
            "live": self.live_pending_snapshot(live_orders),
            "missing_tickets": sorted(pending_tickets.difference(live_order_tickets)),
            "extra_tickets": sorted(live_order_tickets.difference(pending_tickets)),
            "state_count": len(state_orders),
            "live_count": len(live_orders),
            "state_tuples": state_tuples,
            "live_tuples": live_tuples,
        }
        state_ok = state_tuples == expected_tuples
        live_ok = live_tuples == expected_tuples
        tickets_ok = not details["missing_tickets"] and not details["extra_tickets"]
        if state_ok and live_ok and tickets_ok:
            return None
        return details

    def flat_pending_grid_reissue_block_reason(self, tick: dict[str, Any], regime: dict[str, Any]) -> str | None:
        if not bool(self.params.get("live_trading_enabled", False)):
            return "live_trading_disabled"
        if self.is_weekend_entry_blocked():
            return "weekend_entry_blocked"
        if float(tick["spread_points"]) > float(self.params["max_entry_spread_points"]) + 1e-9:
            return (
                f"spread_block spread_points={float(tick['spread_points']):.1f} "
                f"max={float(self.params['max_entry_spread_points']):.1f}"
            )
        if not bool(regime.get("signal_fresh", False)):
            return f"regime_stale reason={regime.get('reason')}"
        if not bool(regime.get("entry_allowed", False)):
            reason = str(regime.get("reason") or "unknown")
            if reason == "ok":
                return "regime_block entry_allowed=false"
            return f"regime_block entry_allowed=false reason={reason}"
        return None

    def cancel_live_pending_orders(self, live_orders: list[Any], reason: str) -> bool:
        for live_order in list(live_orders):
            ticket = int(getattr(live_order, "ticket", 0))
            if not ticket:
                continue
            mismatch = self.live_pending_order_ownership_mismatch_reason(live_order)
            if mismatch:
                self.block_new_entries(f"live pending cancel refused; pending ticket ownership mismatch for ticket {ticket}: {mismatch}")
                return False
            if not self.executor.cancel_order(ticket):
                self.block_new_entries(f"failed to cancel pending order {ticket}")
                return False
            self.log_trade_csv(
                "PENDING_CANCEL",
                ticket,
                direction=str(getattr(live_order, "direction", "")),
                lot_size=float(getattr(live_order, "volume", self.params["lot"])),
                price=self.price(float(getattr(live_order, "price_open", 0.0))),
                stop_loss=self.price(float(getattr(live_order, "sl", 0.0) or 0.0)),
                pnl="",
                reason=reason,
                source_order_id="",
                comment=str(getattr(live_order, "comment", "")),
            )
        return True

    def set_reconciliation_required(self, reason: str, details: dict[str, Any]) -> None:
        self.state["reconciliation_required"] = {
            "type": "flat_pending_grid_repair",
            "symbol": self.symbol,
            "magic": self.magic,
            "cycle_id": int(self.state.get("cycle_id", 0)),
            "grid_anchor": self.state.get("grid_anchor"),
            "reason": reason,
            "details": details,
            "created_at_jst": jst_now().isoformat(),
        }
        self.block_new_entries(reason)

    def notify_manual_action(self, *, title: str, reason: str, action: str, key: str) -> None:
        if self._suppress_manual_alerts:
            return
        notify_manual_action_required(
            bot_id="bot19",
            symbol=self.symbol,
            title=title,
            reason=reason,
            action=action,
            key=key,
        )

    def reconciliation_alert_signature(self, reason: str, details: dict[str, Any]) -> str:
        payload = {
            "reason": reason,
            "details": details,
        }
        serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def mark_reconciliation_alert_if_new(self, reason: str, details: dict[str, Any]) -> bool:
        signature = self.reconciliation_alert_signature(reason, details)
        if self.state.get("manual_alert_last_reconciliation_signature") == signature:
            return False
        self.state["manual_alert_last_reconciliation_signature"] = signature
        self.state["manual_alert_last_reconciliation_reason"] = reason
        self.state["manual_alert_last_reconciliation_at_jst"] = jst_now().isoformat()
        return True

    def notify_reconciliation_required(self, reason: str, details: dict[str, Any]) -> None:
        text = f"{reason}; details={json.dumps(details, ensure_ascii=True, sort_keys=True)}"
        error = ""
        raw_details = details.get("details") if isinstance(details.get("details"), dict) else details
        if isinstance(raw_details, dict) and raw_details.get("error"):
            error = f"; error={raw_details.get('error')}"
        alert_reason = f"{reason}{error}"
        if "ERR|10026" in text or "ERR|10027" in text:
            if not self.mark_reconciliation_alert_if_new(reason, details):
                return
            self.notify_manual_action(
                title="MT5 Algo Trading disabled or trading permission rejected",
                reason=alert_reason,
                action=(
                    "Turn on MT5 Algo Trading for exness-bot-19/BotBridge_s19 and verify "
                    "the EA Allow Algo Trading setting; then inspect pending orders/state before clearing any block."
                ),
                key="bot19:autotrading-disabled",
            )
            return
        if "position sync failed" in reason:
            return
        if not self.mark_reconciliation_alert_if_new(reason, details):
            return
        self.notify_manual_action(
            title="reconciliation_required",
            reason=alert_reason,
            action=(
                "Inspect MT5 pending orders/positions and bot19 state/logs before clearing the block or restarting entries."
            ),
            key=f"bot19:reconciliation:{reason}",
        )

    def repair_flat_pending_grid(
        self,
        tick: dict[str, Any],
        regime: dict[str, Any],
        live_orders: list[Any],
        anomaly: dict[str, Any],
    ) -> bool:
        already_waiting = isinstance(self.state.get("pending_grid_repair_wait"), dict)
        now = time.time()
        should_log = (
            not already_waiting
            or now - self.last_flat_pending_grid_repair_log_epoch
            >= float(self.params["status_log_interval_seconds"])
        )
        if should_log:
            logging.warning(
                "S19 flat pending grid anomaly detected; repairing: %s",
                json.dumps(anomaly, ensure_ascii=True, sort_keys=True),
            )
            self.last_flat_pending_grid_repair_log_epoch = now
        if self.state.get("positions"):
            self.set_reconciliation_required("flat pending grid repair found state positions", anomaly)
            return False
        if not self.cancel_live_pending_orders(live_orders, "flat_pending_grid_repair"):
            self.set_reconciliation_required("flat pending grid live pending cancel failed", anomaly)
            return False

        self.state["virtual_orders"] = []
        self.state["pending_open"] = None
        self.state["sync_block_new_entries"] = False
        self.state["sync_block_reason"] = None
        self.state["pending_grid_repair_wait"] = {
            "reason": anomaly.get("reason", "flat_pending_grid_integrity_failed"),
            "details": anomaly,
            "created_at_jst": jst_now().isoformat(),
        }

        wait_reason = self.flat_pending_grid_reissue_block_reason(tick, regime)
        if wait_reason is None:
            price_block_reason = self.flat_pending_grid_price_block_reason(tick)
            if price_block_reason is not None:
                self.reanchor_flat_pending_grid_for_reissue(tick, price_block_reason)
                wait_reason = self.flat_pending_grid_reissue_block_reason(tick, regime)
                if wait_reason is None:
                    wait_reason = self.flat_pending_grid_price_block_reason(tick)
        if wait_reason is not None:
            self.state["pending_grid_repair_wait"]["wait_reason"] = wait_reason
            if should_log:
                logging.warning("S19 flat pending grid canceled residual orders; waiting to reissue: %s", wait_reason)
            return False

        ok = self.ensure_orders(LONG, tick["info"], tick["bid"], tick["ask"], regime)
        ok = self.ensure_orders(SHORT, tick["info"], tick["bid"], tick["ask"], regime) and ok
        if ok and self.state_pending_grid_tuples() == self.expected_flat_pending_grid_tuples():
            self.state["pending_grid_repair_wait"] = None
            self.state["sync_block_new_entries"] = False
            self.state["sync_block_reason"] = None
            logging.warning(
                "S19 flat pending grid repaired and reissued at anchor=%s",
                self.state.get("grid_anchor"),
            )
            return True

        failure_details = anomaly.copy()
        failure_details["state_after_reissue"] = self.state_pending_snapshot()
        self.clear_virtual_orders()
        self.set_reconciliation_required("flat pending grid reissue failed", failure_details)
        return False

    def clear_auto_tp(self) -> None:
        self.state["auto_tp_price"] = None
        self.state["estimated_auto_tp_profit_usd"] = 0.0

    def remove_position_by_ticket(self, ticket: int) -> dict[str, Any] | None:
        for index, position in enumerate(list(self.state["positions"])):
            if int(position["ticket"]) == int(ticket):
                return self.state["positions"].pop(index)
        return None

    def estimate_position_pnl(self, position: dict[str, Any], exit_price: float, info: Any | None = None) -> float:
        entry = float(position["entry"])
        volume = float(position.get("volume", self.params["lot"]))
        usd_per_pip = self.usd_per_pip_for_lot(volume, info)
        if position["direction"] == "LONG":
            pips = (float(exit_price) - entry) / self.pip_size
        else:
            pips = (entry - float(exit_price)) / self.pip_size
        return pips * usd_per_pip

    def sync_live_positions(self, tick: dict[str, Any], regime: dict[str, Any], force: bool = False) -> bool:
        self.sync_closed_count = 0
        flat_local_state = not self.state["positions"] and not self.state["virtual_orders"]
        active_flat_cycle = self.flat_pending_grid_expected() or isinstance(
            self.state.get("pending_grid_repair_wait"), dict
        )
        if flat_local_state and not force and not active_flat_cycle:
            interval = float(self.params.get("flat_position_sync_interval_seconds", 0.0) or 0.0)
            if interval > 0.0 and time.time() - self.last_position_sync_epoch < interval:
                return True
        live_positions = self.executor.get_positions(self.symbol, self.magic)
        if live_positions is None:
            self.block_new_entries("position sync failed")
            return False
        self.last_position_sync_epoch = time.time()
        live_by_ticket = {int(p.ticket): p for p in live_positions}
        state_by_ticket = {int(p["ticket"]): p for p in self.state["positions"]}

        extra_tickets = sorted(set(live_by_ticket).difference(state_by_ticket))
        for ticket in list(extra_tickets):
            live_position = live_by_ticket[ticket]
            matching_order = None
            for order in list(self.state["virtual_orders"]):
                if self.pending_order_matches_position(order, live_position):
                    matching_order = order
                    break
            if matching_order is None:
                continue
            self.adopt_filled_pending_order(matching_order, live_position)
            state_by_ticket[ticket] = self.state["positions"][-1]
            extra_tickets.remove(ticket)
        if extra_tickets:
            extra_positions = [live_by_ticket[ticket] for ticket in extra_tickets]
            if not self.recover_untracked_live_positions_from_pending_history(
                extra_positions,
                f"untracked live positions recovered from pending history: {extra_tickets}",
            ):
                self.block_new_entries(f"untracked live positions exist: {extra_tickets}")
                return False
            state_by_ticket = {int(p["ticket"]): p for p in self.state["positions"]}

        for ticket, position in list(state_by_ticket.items()):
            live_position = live_by_ticket.get(ticket)
            if live_position is None:
                if bool(self.params["assume_missing_state_position_is_sl"]):
                    if not self.confirm_state_ticket_absent_for_assumed_sl(ticket, position):
                        return False
                    exit_price = float(position["stop_loss"])
                    pnl = self.estimate_position_pnl(position, exit_price, tick["info"])
                    self.state["cycle_realized_usd"] = float(self.state["cycle_realized_usd"]) + pnl
                    self.note_stop_loss_exit()
                    self.log_trade_csv(
                        "EXIT_ASSUMED_SERVER_SL",
                        ticket,
                        direction=str(position.get("direction", "")),
                        lot_size=float(position.get("volume", self.params["lot"])),
                        price=self.price(exit_price),
                        stop_loss=self.price(exit_price),
                        pnl=pnl,
                        reason="position_missing_on_mt5_assumed_server_sl",
                        source_order_id=position.get("source_order_id", ""),
                        comment=str(position.get("comment", "")),
                    )
                    self.remove_position_by_ticket(ticket)
                    self.sync_closed_count += 1
                    logging.warning(
                        f"Ticket {ticket} is absent on MT5; assumed server-side SL at {exit_price}, pnl={pnl:.2f}"
                    )
                    continue
                self.block_new_entries(f"state ticket missing on MT5: {ticket}")
                return False
            mismatch = self.live_position_ownership_mismatch_reason(live_position, position)
            if mismatch:
                self.block_new_entries(f"state ticket ownership mismatch for ticket {ticket}: {mismatch}")
                return False
            position["entry"] = self.price(float(live_position.open_price))
            position["volume"] = float(live_position.volume)
            expected_sl = self.price(float(position["stop_loss"]))
            live_sl = self.price(float(getattr(live_position, "sl", 0.0) or 0.0))
            mismatch = abs(expected_sl - live_sl) / self.pip_size if live_sl else 999.0
            if bool(self.params["repair_missing_sl_on_sync"]) and mismatch > float(self.params["sl_mismatch_tolerance_pips"]):
                if not self.executor.modify_position_sl_tp(ticket, sl=expected_sl, tp=0.0):
                    self.block_new_entries(f"failed to repair SL for ticket {ticket}")
                    return False
        self.recalculate_position_counts()
        pending_tickets = [
            int(order["pending_ticket"])
            for order in self.state["virtual_orders"]
            if order.get("pending_ticket")
        ]
        live_orders: list[Any] = []
        if bool(self.params.get("use_server_pending_entry", False)):
            live_orders = self.executor.get_orders(self.symbol, self.magic)
            if live_orders is None:
                self.block_new_entries("pending order sync failed")
                return False
            live_order_tickets = {int(order.ticket) for order in live_orders}
            if self.clear_flat_reconciliation_if_confirmed(live_positions, live_orders):
                self.clear_pending_fill_sync_wait()
            else:
                flat_grid_anomaly = self.flat_pending_grid_anomaly(live_orders)
                if flat_grid_anomaly is not None:
                    if self.defer_flat_pending_grid_repair_for_fill_sync(flat_grid_anomaly):
                        return False
                    return self.repair_flat_pending_grid(tick, regime, live_orders, flat_grid_anomaly)
                self.clear_pending_fill_sync_wait()
            extra_order_tickets = sorted(live_order_tickets.difference(set(pending_tickets)))
            if extra_order_tickets:
                self.block_new_entries(f"untracked live pending orders exist: {extra_order_tickets}")
                return False
            for pending_ticket in pending_tickets:
                if pending_ticket not in live_order_tickets:
                    self.block_new_entries(f"pending order missing on MT5: {pending_ticket}")
                    return False

        self.recalculate_position_counts()
        if not self.state["positions"]:
            self.clear_auto_tp()
        self.clear_new_entry_block_if_reason("position sync failed")
        self.clear_new_entry_block_if_reason("pending order sync failed")
        reason = self.state.get("sync_block_reason")
        if isinstance(reason, str) and reason.startswith("pending order missing on MT5:"):
            self.clear_new_entry_block_if_reason(reason)
        if self.state.get("pending_grid_repair_wait") and not self.flat_pending_grid_anomaly(live_orders):
            self.state["pending_grid_repair_wait"] = None
        return True

    def process_stops(self, bid: float, ask: float, info: Any) -> int:
        closed = 0
        epsilon = 10 ** (-self.digits - 1)
        for position in list(self.state["positions"]):
            direction = direction_from_name(position["direction"])
            stop_loss = float(position["stop_loss"])
            crossed = (direction == LONG and bid <= stop_loss + epsilon) or (
                direction == SHORT and ask >= stop_loss - epsilon
            )
            if not crossed:
                continue
            ticket = int(position["ticket"])
            if not self.confirm_state_ticket_owned_for_action(position, "local SL close"):
                break
            result = self.executor.close_position(ticket, deviation=int(self.params["deviation_points"]))
            if result:
                close_price = float(getattr(result, "close_price", 0.0) or stop_loss)
                self.remove_position_by_ticket(ticket)
                pnl = float(getattr(result, "profit", 0.0) or 0.0)
                self.state["cycle_realized_usd"] = float(self.state["cycle_realized_usd"]) + pnl
                self.note_stop_loss_exit()
                self.log_trade_csv(
                    "EXIT_SL",
                    ticket,
                    direction=str(position.get("direction", "")),
                    lot_size=float(getattr(result, "lot", 0.0) or position.get("volume", self.params["lot"])),
                    price=self.price(close_price),
                    stop_loss=self.price(stop_loss),
                    pnl=pnl,
                    reason="local_stop_crossed",
                    source_order_id=position.get("source_order_id", ""),
                    comment=str(position.get("comment", "")),
                )
                logging.info(f"SL close ticket={ticket} pnl={pnl:.2f}")
                closed += 1
            else:
                status = getattr(result, "status", "UNKNOWN")
                self.log_trade_csv(
                    "EXIT_FAIL_SL",
                    ticket,
                    direction=str(position.get("direction", "")),
                    lot_size=float(position.get("volume", self.params["lot"])),
                    price="",
                    stop_loss=self.price(stop_loss),
                    pnl="",
                    reason=f"SL close failed: {status}",
                    source_order_id=position.get("source_order_id", ""),
                    comment=str(position.get("comment", "")),
                )
                self.block_new_entries(f"SL close failed for ticket {ticket}: {getattr(result, 'status', 'UNKNOWN')}")
                break
        self.recalculate_position_counts()
        if not self.state["positions"]:
            self.clear_auto_tp()
        return closed

    def inactive_distance_applies(self, regime: dict[str, Any]) -> bool:
        mode = str(self.params["inactive_distance_mode"])
        if mode == "inactive":
            return not bool(regime.get("entry_allowed", False))
        if mode == "gate_false":
            return bool(regime.get("signal_fresh", False)) and not bool(regime.get("entry_allowed", False))
        if mode == "stale":
            return not bool(regime.get("signal_fresh", False))
        raise ValueError(f"unsupported inactive_distance_mode: {mode}")

    def virtual_order_fill_allowed(self, order: dict[str, Any], bid: float, ask: float, regime: dict[str, Any]) -> bool:
        direction = direction_from_name(order["direction"])
        if bool(self.params.get("dynamic_exposure_cap_enabled", False)):
            cap_side = str(self.params.get("dynamic_exposure_cap_side", "")).upper()
            cap_direction = SHORT if cap_side == "SHORT" else LONG if cap_side == "LONG" else None
            if cap_direction is not None and direction == cap_direction:
                cycle_equity = self.cycle_equity_usd(bid, ask, None)
                cap = None
                if cycle_equity <= float(self.params.get("dynamic_exposure_cap_extreme_equity_usd", -1e9)):
                    cap = int(self.params.get("dynamic_exposure_cap_extreme", 999999))
                elif cycle_equity <= float(self.params.get("dynamic_exposure_cap_deep_equity_usd", -1e9)):
                    cap = int(self.params.get("dynamic_exposure_cap_deep", 999999))
                else:
                    gate_false_ok = (not bool(regime.get("entry_allowed", False))) if bool(
                        self.params.get("dynamic_exposure_cap_base_gate_false", False)
                    ) else True
                    streak_ok = int(self.state.get("dynamic_sl_streak", 0)) >= int(
                        self.params.get("dynamic_exposure_cap_base_sl_streak", 0)
                    )
                    equity_ok = cycle_equity <= float(self.params.get("dynamic_exposure_cap_base_equity_usd", -1e9))
                    if gate_false_ok and streak_ok and equity_ok:
                        cap = int(self.params.get("dynamic_exposure_cap_base", 999999))
                if cap is not None:
                    side_count = int(self.state.get("short_count" if direction == SHORT else "long_count", 0))
                    same_side_orders = sum(
                        1 for item in self.state["virtual_orders"] if direction_from_name(item["direction"]) == direction
                    )
                    if side_count + same_side_orders >= cap:
                        self.state["dynamic_cycle_exposure_blocks"] = int(
                            self.state.get("dynamic_cycle_exposure_blocks", 0)
                        ) + 1
                        return False

        minimum_pips = float(self.params["base_add_distance_pips"])
        if self.state["positions"] and self.inactive_distance_applies(regime):
            minimum_pips = max(minimum_pips, float(self.params["inactive_add_distance_pips"]))
        if minimum_pips <= 0.0:
            return True
        minimum_price = minimum_pips * self.pip_size
        long_count = int(self.state.get("long_count", 0))
        short_count = int(self.state.get("short_count", 0))
        if direction == LONG and long_count > 0:
            average_entry = float(self.state.get("long_entry_sum", 0.0)) / long_count
            return ask - average_entry >= minimum_price - 1e-12
        if direction == SHORT and short_count > 0:
            average_entry = float(self.state.get("short_entry_sum", 0.0)) / short_count
            return average_entry - bid >= minimum_price - 1e-12
        return True

    def fill_virtual_orders(self, tick: dict[str, Any], regime: dict[str, Any]) -> int:
        if bool(self.params.get("use_server_pending_entry", False)):
            return 0
        if not bool(self.params.get("live_trading_enabled", False)):
            return 0
        if self.state.get("sync_block_new_entries"):
            return 0
        if tick["spread_points"] > float(self.params["max_entry_spread_points"]) + 1e-9:
            for order in self.state["virtual_orders"]:
                direction = direction_from_name(order["direction"])
                crossed = (direction == LONG and tick["ask"] >= float(order["entry"])) or (
                    direction == SHORT and tick["bid"] <= float(order["entry"])
                )
                if crossed:
                    order["crossed_while_spread_blocked"] = True
            return 0

        filled = 0
        while True:
            buy_orders = sorted(
                [o for o in self.state["virtual_orders"] if o["direction"] == "LONG"],
                key=lambda item: float(item["entry"]),
            )
            order = buy_orders[0] if buy_orders and float(buy_orders[0]["entry"]) <= tick["ask"] + 1e-12 else None
            if order is None or not self.virtual_order_fill_allowed(order, tick["bid"], tick["ask"], regime):
                break
            if not self.open_from_virtual_order(order, tick):
                break
            filled += 1

        while True:
            sell_orders = sorted(
                [o for o in self.state["virtual_orders"] if o["direction"] == "SHORT"],
                key=lambda item: float(item["entry"]),
                reverse=True,
            )
            order = sell_orders[0] if sell_orders and float(sell_orders[0]["entry"]) >= tick["bid"] - 1e-12 else None
            if order is None or not self.virtual_order_fill_allowed(order, tick["bid"], tick["ask"], regime):
                break
            if not self.open_from_virtual_order(order, tick):
                break
            filled += 1
        return filled

    def open_from_virtual_order(self, order: dict[str, Any], tick: dict[str, Any]) -> bool:
        if len(self.state["positions"]) >= int(self.params["max_open_positions"]):
            self.block_new_entries("max open positions reached")
            return False
        direction = direction_from_name(order["direction"])
        order_type = ORDER_TYPE_BUY if direction == LONG else ORDER_TYPE_SELL
        lot = self.normalize_lot(tick["info"])
        sl = float(order["stop_loss"]) if bool(self.params["use_server_sl"]) else 0.0
        comment = f"{self.params['comment_prefix']}_{self.state['cycle_id']}_{order['order_id']}"
        ticket = self.executor.open_position(
            self.symbol,
            order_type,
            lot,
            sl=sl,
            tp=0.0,
            deviation=int(self.params["deviation_points"]),
            magic=self.magic,
            comment=comment,
            digits=self.digits,
        )
        if ticket is None:
            self.block_new_entries(f"open failed for virtual order {order['order_id']}")
            return False
        entry = float(getattr(ticket, "price", 0.0) or (tick["ask"] if direction == LONG else tick["bid"]))
        self.state["virtual_orders"] = [
            item for item in self.state["virtual_orders"] if int(item["order_id"]) != int(order["order_id"])
        ]
        self.state["positions"].append(
            {
                "ticket": int(ticket),
                "symbol": self.symbol,
                "magic": self.magic,
                "direction": direction_name(direction),
                "entry": self.price(entry),
                "stop_loss": self.price(float(order["stop_loss"])),
                "volume": lot,
                "opened_at_jst": jst_now().isoformat(),
                "source_order_id": int(order["order_id"]),
                "comment": comment,
            }
        )
        self.recalculate_position_counts()
        self.log_trade_csv(
            "ENTRY",
            int(ticket),
            direction=direction_name(direction),
            lot_size=lot,
            price=self.price(entry),
            stop_loss=self.price(float(order["stop_loss"])),
            pnl="",
            reason="virtual_order_fill",
            source_order_id=int(order["order_id"]),
            comment=comment,
        )
        logging.info(
            f"Opened {direction_name(direction)} ticket={int(ticket)} entry={self.price(entry)} sl={self.price(float(order['stop_loss']))}"
        )
        return True

    def manage_orders_and_grid(
        self,
        bid: float,
        ask: float,
        info: Any | None = None,
        regime: dict[str, Any] | None = None,
    ) -> None:
        if self.state["grid_anchor"] is None:
            return
        level = int(self.state.get("long_count", 0)) - int(self.state.get("short_count", 0))
        if level == 0:
            self.ensure_orders(LONG, info, bid, ask, regime)
            self.ensure_orders(SHORT, info, bid, ask, regime)
        elif level > 0:
            self.ensure_orders(LONG, info, bid, ask, regime)
        else:
            self.ensure_orders(SHORT, info, bid, ask, regime)

        anchor = float(self.state["grid_anchor"])
        distance = self.distance_price
        if level != 0:
            if ask + distance / 6.0 >= anchor + distance:
                self.state["grid_anchor"] = self.price(anchor + distance)
            elif bid - distance / 6.0 <= anchor - distance:
                self.state["grid_anchor"] = self.price(anchor - distance)
        else:
            if ask >= anchor + distance:
                self.state["grid_anchor"] = self.price(anchor + distance)
            elif bid <= anchor - distance:
                self.state["grid_anchor"] = self.price(anchor - distance)

    def floating_usd(self, bid: float, ask: float, info: Any | None = None) -> float:
        pnl = 0.0
        for position in self.state["positions"]:
            exit_price = bid if position["direction"] == "LONG" else ask
            pnl += self.estimate_position_pnl(position, exit_price, info)
        return pnl

    def cycle_equity_usd(self, bid: float, ask: float, info: Any | None = None) -> float:
        return float(self.state["cycle_realized_usd"]) + self.floating_usd(bid, ask, info)

    def note_stop_loss_exit(self) -> None:
        self.state["dynamic_sl_streak"] = int(self.state.get("dynamic_sl_streak", 0)) + 1

    def force_close_cycle(self, bid: float, ask: float, info: Any | None, reason: str) -> bool:
        failures = []
        for position in list(self.state["positions"]):
            if not self.confirm_state_ticket_owned_for_action(position, "dynamic close"):
                return False
        for position in list(self.state["positions"]):
            ticket = int(position["ticket"])
            result = self.executor.close_position(ticket, deviation=int(self.params["deviation_points"]))
            if result:
                close_price = float(
                    getattr(result, "close_price", 0.0)
                    or (bid if position["direction"] == "LONG" else ask)
                )
                pnl = float(getattr(result, "profit", 0.0) or 0.0)
                self.log_trade_csv(
                    "EXIT_DYNAMIC_CLOSE",
                    ticket,
                    direction=str(position.get("direction", "")),
                    lot_size=float(getattr(result, "lot", 0.0) or position.get("volume", self.params["lot"])),
                    price=self.price(close_price),
                    stop_loss=self.price(float(position.get("stop_loss", 0.0) or 0.0)),
                    pnl=pnl,
                    reason=reason,
                    source_order_id=position.get("source_order_id", ""),
                    comment=str(position.get("comment", "")),
                )
                self.remove_position_by_ticket(ticket)
                self.state["cycle_realized_usd"] = float(self.state["cycle_realized_usd"]) + pnl
            else:
                failures.append((ticket, getattr(result, "status", "UNKNOWN")))
        if failures:
            self.block_new_entries(f"dynamic close failures: {failures}")
            return False
        if not self.clear_virtual_orders():
            return False
        self.state["auto_tp_price"] = None
        self.state["estimated_auto_tp_profit_usd"] = 0.0
        self.state["restart_next_tick"] = True
        self.recalculate_position_counts()
        logging.info(
            "Cycle %s dynamically closed by %s; realized=%.2f",
            self.state["cycle_id"],
            reason,
            float(self.state["cycle_realized_usd"]),
        )
        return True

    def dynamic_cycle_close_if_due(self, bid: float, ask: float, info: Any | None) -> bool:
        if not self.state["positions"]:
            return False
        cycle_equity = self.cycle_equity_usd(bid, ask, info)
        if bool(self.params.get("dynamic_recovery_close_enabled", False)):
            if (
                int(self.state.get("dynamic_sl_streak", 0)) >= int(self.params.get("dynamic_recovery_sl_streak", 0))
                and float(self.state.get("cycle_realized_usd", 0.0)) <= float(
                    self.params.get("dynamic_recovery_min_cycle_loss_usd", 0.0)
                )
                and cycle_equity >= float(self.params.get("dynamic_recovery_close_equity_usd", 0.0))
            ):
                return self.force_close_cycle(bid, ask, info, "dynamic_recovery_close")
        if bool(self.params.get("dynamic_failsafe_close_enabled", False)):
            if (
                int(self.state.get("dynamic_cycle_exposure_blocks", 0))
                >= int(self.params.get("dynamic_failsafe_block_count", 999999999))
                and cycle_equity <= float(self.params.get("dynamic_failsafe_equity_usd", -1e9))
            ):
                return self.force_close_cycle(bid, ask, info, "dynamic_failsafe_close")
        return False

    def refresh_pending_order_filters(self, tick: dict[str, Any], regime: dict[str, Any]) -> None:
        if not bool(self.params.get("use_server_pending_entry", False)):
            return
        if self.state.get("sync_block_new_entries"):
            return
        for order in list(self.state["virtual_orders"]):
            if order.get("pending_ticket"):
                if not self.virtual_order_fill_allowed(order, tick["bid"], tick["ask"], regime):
                    if self.cancel_server_pending_order(order):
                        self.state["virtual_orders"] = [
                            item
                            for item in self.state["virtual_orders"]
                            if int(item["order_id"]) != int(order["order_id"])
                        ]
                    else:
                        return
        level = int(self.state.get("long_count", 0)) - int(self.state.get("short_count", 0))
        if level == 0:
            self.ensure_orders(LONG, tick["info"], tick["bid"], tick["ask"], regime)
            self.ensure_orders(SHORT, tick["info"], tick["bid"], tick["ask"], regime)
        elif level > 0:
            self.ensure_orders(LONG, tick["info"], tick["bid"], tick["ask"], regime)
        else:
            self.ensure_orders(SHORT, tick["info"], tick["bid"], tick["ask"], regime)

    def theoretic_profit_usd(self, distance_price: float, info: Any | None = None) -> float:
        distance_price = max(0.0, float(distance_price))
        grid = self.distance_price
        levels = int(math.floor((distance_price + 1e-12) / grid))
        remain_price = max(0.0, distance_price - levels * grid)
        triangular = levels * (levels + 1) / 2.0
        usd_per_pip = self.usd_per_pip_for_lot(float(self.params["lot"]), info)
        full_levels_usd = float(self.params["distance_pips"]) * usd_per_pip * triangular
        remain_usd = remain_price / self.pip_size * usd_per_pip * (levels + 1)
        return full_levels_usd + remain_usd

    def break_even_distance_price(self, loss_usd: float, info: Any | None = None) -> float:
        low = 0
        high = 1
        while self.theoretic_profit_usd(high * 0.1 * self.pip_size, info) <= loss_usd:
            high *= 2
            if high > 10_000_000:
                raise RuntimeError("break-even search exceeded safety range")
        while low + 1 < high:
            middle = (low + high) // 2
            if self.theoretic_profit_usd(middle * 0.1 * self.pip_size, info) > loss_usd:
                high = middle
            else:
                low = middle
        return high * 0.1 * self.pip_size

    def pyramid_base(self, close_price: float) -> float:
        farthest: tuple[float, dict[str, Any]] | None = None
        for position in self.state["positions"]:
            distance = abs(float(close_price) - float(position["stop_loss"]))
            if farthest is None or distance > farthest[0]:
                farthest = (distance, position)
        if farthest is None:
            return 0.0
        position = farthest[1]
        if position["direction"] == "LONG":
            return float(position["stop_loss"]) + self.distance_price
        return float(position["stop_loss"]) - self.distance_price

    def refresh_auto_tp(self, bid: float, ask: float, info: Any) -> None:
        self.state["last_break_even_refresh_epoch"] = time.time()
        level = int(self.state.get("long_count", 0)) - int(self.state.get("short_count", 0))
        reference_price = ask if bool(self.params["short_auto_tp_uses_ask"]) and level < 0 else bid
        base = self.pyramid_base(reference_price)
        if level == 0 or base == 0.0:
            self.state["auto_tp_price"] = None
            self.state["estimated_auto_tp_profit_usd"] = 0.0
            return
        distance = abs(reference_price - base)
        if (level > 0 and reference_price < base) or (level < 0 and reference_price > base):
            distance = 0.0
        cycle_equity = self.cycle_equity_usd(bid, ask, info)
        loss = -(cycle_equity - self.theoretic_profit_usd(distance, info))
        if loss <= 0.0:
            self.state["auto_tp_price"] = None
            self.state["estimated_auto_tp_profit_usd"] = 0.0
            return
        break_even = self.break_even_distance_price(loss, info)
        target_offset = break_even + int(self.params["auto_tp_levels"]) * self.distance_price
        auto_tp = base + target_offset if level > 0 else base - target_offset
        self.state["auto_tp_price"] = self.price(auto_tp)
        self.state["estimated_auto_tp_profit_usd"] = self.theoretic_profit_usd(target_offset, info) - loss

    def auto_tp_crossed(self, bid: float, ask: float) -> bool:
        auto_tp = self.state.get("auto_tp_price")
        if auto_tp is None or int(self.params["auto_tp_levels"]) < 1:
            return False
        level = int(self.state.get("long_count", 0)) - int(self.state.get("short_count", 0))
        reference_price = ask if bool(self.params["short_auto_tp_uses_ask"]) and level < 0 else bid
        return (level > 0 and reference_price >= float(auto_tp)) or (
            level < 0 and reference_price <= float(auto_tp)
        )

    def auto_tp_profit_guard_passed(self, bid: float, ask: float, info: Any | None = None) -> bool:
        minimum_profit = float(self.params.get("min_auto_tp_cycle_profit_usd", 0.0) or 0.0)
        if minimum_profit <= 0.0:
            return True
        cycle_equity = self.cycle_equity_usd(bid, ask, info)
        if cycle_equity + 1e-9 >= minimum_profit:
            return True
        now = time.time()
        if now - self.last_auto_tp_profit_guard_log_epoch >= float(self.params["status_log_interval_seconds"]):
            logging.info(
                "autoTP crossed but cycle equity %.2f USD is below minimum %.2f USD; hold positions",
                cycle_equity,
                minimum_profit,
            )
            self.last_auto_tp_profit_guard_log_epoch = now
        return False

    def complete_cycle(self, bid: float, ask: float, info: Any | None = None) -> bool:
        if not self.auto_tp_profit_guard_passed(bid, ask, info):
            return False
        failures = []
        for position in list(self.state["positions"]):
            if not self.confirm_state_ticket_owned_for_action(position, "autoTP close"):
                return False
        for position in list(self.state["positions"]):
            ticket = int(position["ticket"])
            result = self.executor.close_position(ticket, deviation=int(self.params["deviation_points"]))
            if result:
                close_price = float(
                    getattr(result, "close_price", 0.0)
                    or (bid if position["direction"] == "LONG" else ask)
                )
                pnl = float(getattr(result, "profit", 0.0) or 0.0)
                self.log_trade_csv(
                    "EXIT_AUTO_TP",
                    ticket,
                    direction=str(position.get("direction", "")),
                    lot_size=float(getattr(result, "lot", 0.0) or position.get("volume", self.params["lot"])),
                    price=self.price(close_price),
                    stop_loss=self.price(float(position.get("stop_loss", 0.0) or 0.0)),
                    pnl=pnl,
                    reason="auto_tp",
                    source_order_id=position.get("source_order_id", ""),
                    comment=str(position.get("comment", "")),
                )
                self.remove_position_by_ticket(ticket)
                self.state["cycle_realized_usd"] = float(self.state["cycle_realized_usd"]) + pnl
            else:
                status = getattr(result, "status", "UNKNOWN")
                self.log_trade_csv(
                    "EXIT_FAIL_AUTO_TP",
                    ticket,
                    direction=str(position.get("direction", "")),
                    lot_size=float(position.get("volume", self.params["lot"])),
                    price="",
                    stop_loss=self.price(float(position.get("stop_loss", 0.0) or 0.0)),
                    pnl="",
                    reason=f"autoTP close failed: {status}",
                    source_order_id=position.get("source_order_id", ""),
                    comment=str(position.get("comment", "")),
                )
                failures.append((ticket, status))
        if failures:
            self.block_new_entries(f"autoTP close failures: {failures}")
            return False
        if not self.clear_virtual_orders():
            return False
        logging.info(
            f"Cycle {self.state['cycle_id']} completed by autoTP; realized={float(self.state['cycle_realized_usd']):.2f}"
        )
        self.state["auto_tp_price"] = None
        self.state["estimated_auto_tp_profit_usd"] = 0.0
        self.state["restart_next_tick"] = True
        self.recalculate_position_counts()
        return True

    def process_entry_blocked_tick(self, tick: dict[str, Any]) -> None:
        if bool(self.params["reanchor_after_weekend_resume"]):
            self.state["weekend_resume_reanchor_pending"] = True
        if not self.clear_virtual_orders():
            return
        stop_count = int(self.sync_closed_count)
        stop_count += self.process_stops(tick["bid"], tick["ask"], tick["info"])
        if self.state["positions"]:
            due = self.auto_tp_refresh_due() or bool(stop_count)
            if due:
                self.refresh_auto_tp(tick["bid"], tick["ask"], tick["info"])
            if self.auto_tp_crossed(tick["bid"], tick["ask"]):
                self.complete_cycle(tick["bid"], tick["ask"], tick["info"])

    def auto_tp_refresh_due(self) -> bool:
        last = self.state.get("last_break_even_refresh_epoch")
        if last is None:
            return True
        return time.time() - float(last) >= float(self.params["break_even_refresh_seconds"])

    def run_once(self) -> None:
        tick = self.get_tick()
        if tick is None:
            return
        regime = self.get_regime()
        if not self.sync_live_positions(tick, regime):
            self.save_state()
            return
        reconciliation_reason = self.reconciliation_block_reason()
        if reconciliation_reason:
            self.block_new_entries(reconciliation_reason)
            self.save_state()
            return
        if not bool(self.params.get("live_trading_enabled", False)) and (
            self.state["positions"] or self.state["virtual_orders"]
        ):
            logging.critical("S19 shadow mode requires flat state; refusing to manage positions or virtual orders")
            self.save_state()
            return

        if self.is_weekend_entry_blocked():
            self.process_entry_blocked_tick(tick)
            self.save_state()
            return

        if self.state.get("weekend_resume_reanchor_pending"):
            had_positions = bool(self.state["positions"])
            self.process_entry_blocked_tick(tick)
            self.state["weekend_resume_reanchor_pending"] = False
            if self.state["positions"]:
                self.state["grid_anchor"] = self.price(tick["bid"])
                logging.info(f"Weekend resume re-anchor: {self.state['grid_anchor']}")
                self.save_state()
                return
            if had_positions:
                self.save_state()
                return

        if (
            int(self.state["cycle_id"]) == 0
            or bool(self.state.get("restart_next_tick"))
            or (not self.state["positions"] and not self.state["virtual_orders"])
        ):
            if self.state.get("sync_block_new_entries"):
                self.log_status(tick, regime)
                self.save_state()
                return
            if not bool(regime.get("entry_allowed", False)):
                self.log_status(tick, regime)
                self.save_state()
                return
            policy_decision = self.evaluate_cycle_start_policy(tick, regime)
            if not bool(policy_decision.get("allow", False)):
                logging.info(
                    "S19 policy blocked cycle start: "
                    f"symbol={self.symbol} reason={policy_decision.get('reason')} "
                    f"proba={policy_decision.get('pred_proba')} threshold={policy_decision.get('threshold')}"
                )
                self.log_status(tick, regime)
                self.save_state()
                return
            if not bool(self.params.get("live_trading_enabled", False)):
                logging.info(
                    "S19 shadow policy allowed cycle start but live_trading_enabled=false; "
                    f"symbol={self.symbol} proba={policy_decision.get('pred_proba')} "
                    f"threshold={policy_decision.get('threshold')}"
                )
                self.log_status(tick, regime)
                self.save_state()
                return
            if tick["spread_points"] > float(self.params["max_entry_spread_points"]) + 1e-9:
                self.log_status(tick, regime)
                self.save_state()
                return
            if not self.sync_live_positions(tick, regime, force=True):
                self.save_state()
                return
            if not self.start_cycle(tick["bid"], tick["info"], tick["ask"], regime):
                self.save_state()
                return

        stop_count = int(self.sync_closed_count)
        stop_count += self.process_stops(tick["bid"], tick["ask"], tick["info"])
        self.refresh_pending_order_filters(tick, regime)
        self.fill_virtual_orders(tick, regime)
        immediate_stops = self.process_stops(tick["bid"], tick["ask"], tick["info"])
        stop_count += immediate_stops
        if self.dynamic_cycle_close_if_due(tick["bid"], tick["ask"], tick["info"]):
            self.log_status(tick, regime)
            self.save_state()
            return
        self.manage_orders_and_grid(tick["bid"], tick["ask"], tick["info"], regime)
        if self.state["positions"] and (stop_count or self.auto_tp_refresh_due() or self.state.get("auto_tp_price") is None):
            self.refresh_auto_tp(tick["bid"], tick["ask"], tick["info"])
        if self.state["positions"] and self.auto_tp_crossed(tick["bid"], tick["ask"]):
            self.complete_cycle(tick["bid"], tick["ask"], tick["info"])
        self.log_status(tick, regime)
        self.save_state()

    def log_status(self, tick: dict[str, Any], regime: dict[str, Any]) -> None:
        now = time.time()
        if now - self.last_status_log_epoch < float(self.params["status_log_interval_seconds"]):
            return
        self.last_status_log_epoch = now
        last_policy = self.state.get("last_policy_decision") or {}
        logging.info(
            "S19 status: "
            f"bid={tick['bid']:.5f} ask={tick['ask']:.5f} spread_points={tick['spread_points']:.1f} "
            f"cycle={self.state['cycle_id']} pos={len(self.state['positions'])} orders={len(self.state['virtual_orders'])} "
            f"auto_tp={self.state.get('auto_tp_price')} regime_allowed={regime.get('entry_allowed')} "
            f"fresh={regime.get('signal_fresh')} block={self.state.get('sync_block_new_entries')} "
            f"policy_allowed={last_policy.get('allow')} policy_reason={last_policy.get('reason')}"
        )

    def run_forever(self) -> None:
        if not bool(self.params.get("enabled", True)):
            logging.warning("s19 is disabled by params enabled=false")
            return
        live_trading_enabled = bool(self.params.get("live_trading_enabled", False))
        shadow_forward_enabled = bool(self.params.get("shadow_forward_enabled", False))
        if not live_trading_enabled and not shadow_forward_enabled:
            logging.warning("s19 live_trading_enabled=false and shadow_forward_enabled=false; idle loop only")
            while True:
                time.sleep(max(10.0, float(self.params["status_log_interval_seconds"])))
        try:
            self.save_state()
        except Exception as exc:
            logging.critical(
                "State persistence is unavailable. Refusing to connect to bridge: %s",
                exc,
            )
            raise RuntimeError("State persistence is unavailable") from exc
        if not self.connect():
            raise RuntimeError("Failed to connect to MT5 EA bridge")
        self.verify_bridge_capabilities()
        self.startup_reconcile_state()
        self.save_state()
        if not live_trading_enabled:
            logging.warning("S19 shadow forward mode: bridge connected, policy decisions logged, no orders")
        logging.info(
            "S19 started: "
            f"symbol={self.symbol} magic={self.magic} lot={self.params['lot']} "
            f"distance={self.params['distance_pips']} inactive_add={self.params['inactive_add_distance_pips']}"
        )
        logging.info("S19 source revision: revision=%s file=%s", BOT_SOURCE_REVISION, __file__)
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                raise
            except Exception:
                logging.exception("Unhandled s19 loop error")
            time.sleep(float(self.params["poll_interval_seconds"]))

    def self_test(self) -> None:
        original_params = self.params
        original_state = self.state
        original_state_file = self.state_file
        original_trade_log_file = self.trade_log_file
        original_executor = self.executor
        original_suppress_manual_alerts = self._suppress_manual_alerts
        temp_dir = tempfile.mkdtemp(prefix="s19_selftest_")
        self._suppress_manual_alerts = True
        self.params = self.params.copy()
        self.params["live_trading_enabled"] = False
        self.params["use_server_pending_entry"] = False
        self.state_file = os.path.join(temp_dir, "state.json")
        self.trade_log_file = os.path.join(temp_dir, "trades.csv")
        try:
            original_notify_manual_action = self.notify_manual_action
            manual_alert_calls: list[dict[str, Any]] = []

            def capture_manual_alert(**kwargs: Any) -> None:
                manual_alert_calls.append(kwargs)

            self.notify_manual_action = capture_manual_alert  # type: ignore[method-assign]
            try:
                self.state = self.default_state()
                reconciliation_details = {
                    "type": "flat_pending_grid_repair",
                    "reason": "untracked live positions exist: [111]",
                    "details": {"tickets": [111]},
                    "created_at_jst": "2026-07-16T00:00:00+09:00",
                }
                self.notify_reconciliation_required("untracked live positions exist: [111]", reconciliation_details)
                self.notify_reconciliation_required("untracked live positions exist: [111]", reconciliation_details)
                assert len(manual_alert_calls) == 1
                changed_reconciliation_details = {
                    **reconciliation_details,
                    "details": {"tickets": [222]},
                }
                self.notify_reconciliation_required("untracked live positions exist: [222]", changed_reconciliation_details)
                assert len(manual_alert_calls) == 2
            finally:
                self.notify_manual_action = original_notify_manual_action  # type: ignore[method-assign]

            self.state = self.default_state()
            assert self.start_cycle(1.25000)
            assert len(self.state["virtual_orders"]) == 4, self.state["virtual_orders"]
            long_order = min(
                [o for o in self.state["virtual_orders"] if o["direction"] == "LONG"],
                key=lambda item: item["entry"],
            )
            assert long_order["entry"] == 1.25100
            assert long_order["stop_loss"] == 1.25000
            self.state["positions"] = [
                {
                    "ticket": 1,
                    "direction": "LONG",
                    "entry": 1.25100,
                    "stop_loss": 1.25000,
                    "volume": 0.01,
                }
            ]
            self.recalculate_position_counts()
            order = {"direction": "LONG", "entry": 1.25200, "stop_loss": 1.25100}
            assert not self.virtual_order_fill_allowed(
                order,
                1.25091,
                1.25100,
                {"entry_allowed": True, "signal_fresh": True},
            )
            assert self.virtual_order_fill_allowed(
                order,
                1.25326,
                1.25335,
                {"entry_allowed": False, "signal_fresh": True},
            )
            assert not self.auto_tp_profit_guard_passed(1.25099, 1.25108)
            assert self.auto_tp_profit_guard_passed(1.26000, 1.26009)

            class FakeInfo:
                volume_min = 0.01
                volume_max = 100.0
                volume_step = 0.01
                contract_size = 100000.0

            class FakePendingOrder:
                def __init__(self, ticket: int, type_int: int, price_open: float, sl: float, comment: str) -> None:
                    self.ticket = ticket
                    self.symbol = "GBPUSD"
                    self.type = type_int
                    self.direction = "LONG" if type_int == ORDER_TYPE_BUY_STOP else "SHORT"
                    self.volume = 0.01
                    self.price_open = price_open
                    self.sl = sl
                    self.tp = 0.0
                    self.magic = 190019
                    self.comment = comment

            class FakeLivePosition:
                def __init__(
                    self,
                    ticket: int,
                    direction: str,
                    open_price: float,
                    sl: float,
                    comment: str,
                    volume: float = 0.01,
                ) -> None:
                    self.ticket = ticket
                    self.symbol = "GBPUSD"
                    self.magic = 190019
                    self.direction = direction
                    self.open_price = open_price
                    self.sl = sl
                    self.volume = volume
                    self.comment = comment

            class FakeExecutor:
                def __init__(self) -> None:
                    self.cancelled: list[int] = []
                    self.placed: list[tuple[int, float, float]] = []
                    self.positions: list[FakeLivePosition] = []
                    self.orders: list[FakePendingOrder] = []
                    self.last_order_error = None

                def cancel_order(self, ticket: int) -> bool:
                    self.cancelled.append(int(ticket))
                    return True

                def place_stop_order(
                    self,
                    symbol: str,
                    order_type: int,
                    lot_size: float,
                    price: float,
                    sl: float = 0.0,
                    tp: float = 0.0,
                    magic: int = 0,
                    comment: str = "",
                    digits: int | None = None,
                ) -> int:
                    ticket = 9001 + len(self.placed)
                    self.placed.append((int(order_type), self.price(price), self.price(sl)))
                    return ticket

                @staticmethod
                def price(value: float) -> float:
                    return round(float(value), 5)

                def get_positions(self, symbol: str, magic: int) -> list[FakeLivePosition]:
                    return [
                        position
                        for position in self.positions
                        if position.symbol == symbol and int(position.magic) == int(magic)
                    ]

                def get_position(self, ticket: int) -> FakeLivePosition | None:
                    for position in self.positions:
                        if int(position.ticket) == int(ticket):
                            return position
                    return None

                def confirm_position_absent(self, ticket: int) -> bool:
                    return self.get_position(ticket) is None

                def get_orders(self, symbol: str, magic: int) -> list[FakePendingOrder]:
                    return [
                        order
                        for order in self.orders
                        if order.symbol == symbol and int(order.magic) == int(magic)
                    ]

                def modify_position_sl_tp(self, ticket: int, sl: float, tp: float = 0.0) -> bool:
                    del tp
                    for position in self.positions:
                        if int(position.ticket) == int(ticket):
                            position.sl = sl
                            return True
                    return False

                def close_position(self, ticket: int, deviation: int = 20) -> Any | None:
                    del deviation
                    for index, position in enumerate(list(self.positions)):
                        if int(position.ticket) != int(ticket):
                            continue
                        self.positions.pop(index)

                        class CloseResult:
                            close_price = position.sl
                            profit = 0.0
                            lot = position.volume
                            status = "OK"

                        return CloseResult()
                    return None

            def seed_pending_grid_state() -> None:
                self.state = self.default_state()
                self.state["cycle_id"] = 1
                self.state["grid_anchor"] = 1.25000
                self.state["next_order_id"] = 5
                self.state["virtual_orders"] = [
                    {
                        "order_id": 1,
                        "direction": "LONG",
                        "entry": 1.25100,
                        "stop_loss": 1.25000,
                        "pending_ticket": 1001,
                        "volume": 0.01,
                        "comment": "s19_gbp_1_1",
                    },
                    {
                        "order_id": 2,
                        "direction": "LONG",
                        "entry": 1.25200,
                        "stop_loss": 1.25100,
                        "pending_ticket": 1002,
                        "volume": 0.01,
                        "comment": "s19_gbp_1_2",
                    },
                    {
                        "order_id": 3,
                        "direction": "SHORT",
                        "entry": 1.24900,
                        "stop_loss": 1.25000,
                        "pending_ticket": 1003,
                        "volume": 0.01,
                        "comment": "s19_gbp_1_3",
                    },
                    {
                        "order_id": 4,
                        "direction": "SHORT",
                        "entry": 1.24800,
                        "stop_loss": 1.24900,
                        "pending_ticket": 1004,
                        "volume": 0.01,
                        "comment": "s19_gbp_1_4",
                    },
                ]

            tick = {
                "bid": 1.25000,
                "ask": 1.25009,
                "spread_points": 9.0,
                "info": FakeInfo(),
            }
            regime = {"entry_allowed": True, "signal_fresh": True, "reason": "ok"}
            blocked_regime = {"entry_allowed": False, "signal_fresh": True, "reason": "ok"}
            old_live_trading_enabled = self.params.get("live_trading_enabled")
            self.params["live_trading_enabled"] = True
            try:
                assert (
                    self.flat_pending_grid_reissue_block_reason(tick, blocked_regime)
                    == "regime_block entry_allowed=false"
                )
            finally:
                self.params["live_trading_enabled"] = old_live_trading_enabled

            self.state = self.default_state()
            self.state["positions"] = [
                {
                    "ticket": 910001,
                    "direction": "LONG",
                    "entry": 1.25100,
                    "stop_loss": 1.25000,
                    "volume": 0.01,
                    "source_order_id": 1,
                    "comment": "foreign_bot_position",
                }
            ]
            self.recalculate_position_counts()
            self.executor = FakeExecutor()
            foreign_position = FakeLivePosition(910001, "LONG", 1.25100, 1.25000, "foreign_bot_position")
            foreign_position.magic = 180218
            self.executor.positions = [foreign_position]
            assert not self.sync_live_positions(tick, regime, force=True)
            assert len(self.state["positions"]) == 1
            assert self.state["sync_block_new_entries"]
            assert "ownership mismatch" in str(self.state["sync_block_reason"])

            self.state = self.default_state()
            self.executor = FakeExecutor()
            foreign_pending = FakePendingOrder(910101, ORDER_TYPE_BUY_STOP, 1.25100, 1.25000, "foreign_pending")
            foreign_pending.magic = 180218
            self.executor.orders = [foreign_pending]
            assert not self.cancel_server_pending_order(
                {
                    "order_id": 1,
                    "direction": "LONG",
                    "entry": 1.25100,
                    "stop_loss": 1.25000,
                    "pending_ticket": 910101,
                    "volume": 0.01,
                    "comment": "foreign_pending",
                }
            )
            assert self.executor.cancelled == []
            assert self.state["sync_block_new_entries"]
            assert "pending ticket not confirmed as bot-owned" in str(self.state["sync_block_reason"])

            self.params["live_trading_enabled"] = True
            self.params["use_server_pending_entry"] = True
            self.state = self.default_state()
            self.state["cycle_id"] = 1
            self.state["grid_anchor"] = 1.25000
            self.state["next_order_id"] = 5
            self.state["virtual_orders"] = [
                {
                    "order_id": 1,
                    "direction": "LONG",
                    "entry": 1.25100,
                    "stop_loss": 1.25000,
                    "pending_ticket": 1001,
                    "volume": 0.01,
                    "comment": "s19_gbp_1_1",
                },
                {
                    "order_id": 2,
                    "direction": "LONG",
                    "entry": 1.25200,
                    "stop_loss": 1.25100,
                    "pending_ticket": 1002,
                    "volume": 0.01,
                    "comment": "s19_gbp_1_2",
                },
                {
                    "order_id": 3,
                    "direction": "SHORT",
                    "entry": 1.24900,
                    "stop_loss": 1.25000,
                    "pending_ticket": 1003,
                    "volume": 0.01,
                    "comment": "s19_gbp_1_3",
                },
                {
                    "order_id": 4,
                    "direction": "SHORT",
                    "entry": 1.24800,
                    "stop_loss": 1.24900,
                    "pending_ticket": 1004,
                    "volume": 0.01,
                    "comment": "s19_gbp_1_4",
                },
            ]
            self.executor = FakeExecutor()
            live_orders = [
                FakePendingOrder(1002, ORDER_TYPE_BUY_STOP, 1.25200, 1.25100, "s19_gbp_1_2"),
                FakePendingOrder(1003, ORDER_TYPE_SELL_STOP, 1.24900, 1.25000, "s19_gbp_1_3"),
                FakePendingOrder(1004, ORDER_TYPE_SELL_STOP, 1.24800, 1.24900, "s19_gbp_1_4"),
            ]
            anomaly = self.flat_pending_grid_anomaly(live_orders)
            assert anomaly is not None
            tick = {
                "bid": 1.25000,
                "ask": 1.25009,
                "spread_points": 9.0,
                "info": FakeInfo(),
            }
            regime = {"entry_allowed": True, "signal_fresh": True, "reason": "ok"}
            assert self.repair_flat_pending_grid(tick, regime, live_orders, anomaly)
            assert self.executor.cancelled == [1002, 1003, 1004]
            assert len(self.state["virtual_orders"]) == 4
            assert self.state_pending_grid_tuples() == self.expected_flat_pending_grid_tuples()
            assert self.state["pending_grid_repair_wait"] is None

            # A filled pending stop can disappear from ORDERS before it appears in POSITIONS.
            # Defer flat-grid repair briefly so the original pending metadata can be adopted.
            seed_pending_grid_state()
            self.executor = FakeExecutor()
            self.executor.orders = list(live_orders)
            assert not self.sync_live_positions(tick, regime, force=True)
            assert self.executor.cancelled == []
            assert len(self.state["virtual_orders"]) == 4
            assert isinstance(self.state.get("pending_fill_sync_wait"), dict)
            self.executor.positions = [FakeLivePosition(1001, "LONG", 1.25100, 1.25000, "s19_gbp_1_1")]
            assert self.sync_live_positions(tick, regime, force=True)
            assert self.executor.cancelled == []
            assert len(self.state["positions"]) == 1
            assert int(self.state["positions"][0]["ticket"]) == 1001
            assert len(self.state["virtual_orders"]) == 3
            assert self.state["pending_fill_sync_wait"] is None
            self.state = self.default_state()
            self.state["cycle_id"] = 1
            self.state["grid_anchor"] = 1.25000
            self.state["next_order_id"] = 5
            self.state["virtual_orders"] = [
                {
                    "order_id": 1,
                    "direction": "LONG",
                    "entry": 1.25100,
                    "stop_loss": 1.25000,
                    "pending_ticket": 1001,
                    "volume": 0.01,
                    "comment": "s19_gbp_1_1",
                },
                {
                    "order_id": 2,
                    "direction": "LONG",
                    "entry": 1.25200,
                    "stop_loss": 1.25100,
                    "pending_ticket": 1002,
                    "volume": 0.01,
                    "comment": "s19_gbp_1_2",
                },
                {
                    "order_id": 3,
                    "direction": "SHORT",
                    "entry": 1.24900,
                    "stop_loss": 1.25000,
                    "pending_ticket": 1003,
                    "volume": 0.01,
                    "comment": "s19_gbp_1_3",
                },
                {
                    "order_id": 4,
                    "direction": "SHORT",
                    "entry": 1.24800,
                    "stop_loss": 1.24900,
                    "pending_ticket": 1004,
                    "volume": 0.01,
                    "comment": "s19_gbp_1_4",
                },
            ]
            self.executor = FakeExecutor()
            anomaly = self.flat_pending_grid_anomaly(live_orders)
            assert anomaly is not None
            wide_tick = {
                "bid": 1.25000,
                "ask": 1.25012,
                "spread_points": 12.0,
                "info": FakeInfo(),
            }
            assert not self.repair_flat_pending_grid(wide_tick, regime, live_orders, anomaly)
            assert self.executor.cancelled == [1002, 1003, 1004]
            assert self.executor.placed == []
            assert self.state["virtual_orders"] == []
            assert self.state["grid_anchor"] == 1.25000
            assert str(self.state["pending_grid_repair_wait"]["wait_reason"]).startswith("spread_block")
            anomaly = self.flat_pending_grid_anomaly([])
            assert anomaly is not None
            assert self.repair_flat_pending_grid(tick, regime, [], anomaly)
            assert len(self.state["virtual_orders"]) == 4
            assert self.state_pending_grid_tuples() == self.expected_flat_pending_grid_tuples()
            assert self.state["pending_grid_repair_wait"] is None

            self.state = self.default_state()
            self.state["cycle_id"] = 3
            self.state["grid_anchor"] = 1.34990
            self.state["sync_block_new_entries"] = True
            self.state["sync_block_reason"] = "untracked live positions exist: [27643208]"
            self.state["pending_open"] = {
                "type": "server_pending_stop",
                "request_id": "GBPUSD-190019-3-13",
                "symbol": "GBPUSD",
                "magic": 190019,
                "order_id": 13,
                "direction": "LONG",
                "entry": 1.35090,
                "stop_loss": 1.34990,
                "volume": 0.01,
                "comment": "s19_gbp_3_13",
                "status": "OPEN_RESPONSE_UNCONFIRMED",
                "last_error": "ERR|10006",
            }
            self.state["reconciliation_required"] = {
                "type": "flat_pending_grid_repair",
                "symbol": "GBPUSD",
                "magic": 190019,
                "cycle_id": 3,
                "grid_anchor": 1.34990,
                "reason": "flat pending grid reissue failed",
                "details": {
                    "reason": "flat_pending_grid_integrity_failed",
                    "state": [
                        {
                            "order_id": 9,
                            "ticket": 27643208,
                            "direction": "LONG",
                            "entry": 1.35090,
                            "stop_loss": 1.34990,
                            "comment": "s19_gbp_3_9",
                        }
                    ],
                    "state_after_reissue": [],
                },
                "created_at_jst": "2026-07-16T01:27:02+09:00",
            }
            self.executor = FakeExecutor()
            self.executor.positions = [
                FakeLivePosition(27643208, "LONG", 1.35120, 1.34990, "s19_gbp_3_9")
            ]
            self.executor.orders = []
            assert self.sync_live_positions(tick, regime, force=True)
            assert len(self.state["positions"]) == 1
            recovered_position = self.state["positions"][0]
            assert int(recovered_position["ticket"]) == 27643208
            assert recovered_position["direction"] == "LONG"
            assert recovered_position["recovered_from_live"] is True
            assert self.state["reconciliation_required"] is None
            assert self.state["pending_open"] is None
            assert self.state["pending_grid_repair_wait"] is None
            assert not self.state["sync_block_new_entries"]

            self.state = self.default_state()
            self.state["cycle_id"] = 3
            self.state["grid_anchor"] = 1.34990
            self.state["sync_block_new_entries"] = True
            self.state["sync_block_reason"] = "untracked live positions exist: [27643208]"
            self.state["pending_open"] = {
                "type": "server_pending_stop",
                "request_id": "GBPUSD-190019-3-13",
                "symbol": "GBPUSD",
                "magic": 190019,
                "order_id": 13,
                "direction": "LONG",
                "entry": 1.35090,
                "stop_loss": 1.34990,
                "volume": 0.01,
                "comment": "s19_gbp_3_13",
                "status": "OPEN_RESPONSE_UNCONFIRMED",
                "last_error": "ERR|10006",
            }
            self.state["reconciliation_required"] = {
                "type": "flat_pending_grid_repair",
                "reason": "flat pending grid reissue failed",
                "details": {"state": []},
            }
            self.state["pending_grid_repair_wait"] = {
                "reason": "flat_pending_grid_integrity_failed",
                "details": {"state": []},
            }
            self.executor = FakeExecutor()
            assert self.sync_live_positions(tick, regime, force=True)
            assert self.state["positions"] == []
            assert self.state["virtual_orders"] == []
            assert self.state["reconciliation_required"] is None
            assert self.state["pending_open"] is None
            assert self.state["pending_grid_repair_wait"] is None
            assert self.state["grid_anchor"] is None
            assert self.flat_pending_grid_anomaly([]) is None
            assert not self.state["sync_block_new_entries"]

            original_get_tick = self.get_tick
            original_get_regime = self.get_regime
            original_get_m1_policy_features = self.get_m1_policy_features
            try:
                self.params["live_trading_enabled"] = True
                self.params["use_server_pending_entry"] = True
                self.get_tick = lambda: tick  # type: ignore[method-assign]
                self.get_regime = lambda: regime  # type: ignore[method-assign]
                self.get_m1_policy_features = lambda: {  # type: ignore[method-assign]
                    "m1_decision_time_utc": "2026-07-17T01:00:00+00:00",
                }
                self.state = self.default_state()
                self.state["cycle_id"] = 7
                self.state["grid_anchor"] = 1.24950
                self.state["sync_block_new_entries"] = True
                self.state["sync_block_reason"] = "untracked live positions exist: [333]"
                self.state["pending_open"] = {
                    "type": "server_pending_stop",
                    "symbol": "GBPUSD",
                    "magic": 190019,
                    "order_id": 21,
                    "direction": "SHORT",
                    "entry": 1.24850,
                    "stop_loss": 1.24950,
                    "volume": 0.01,
                    "comment": "s19_gbp_7_21",
                    "status": "OPEN_RESPONSE_UNCONFIRMED",
                }
                self.state["reconciliation_required"] = {
                    "type": "flat_pending_grid_repair",
                    "reason": "flat pending grid reissue failed",
                    "details": {"state": []},
                }
                self.state["pending_grid_repair_wait"] = {
                    "reason": "flat_pending_grid_integrity_failed",
                    "details": {"state": []},
                }
                self.executor = FakeExecutor()
                recovery = self.startup_reconcile_state()
                assert recovery["status"] == "reconciled"
                assert recovery["before"]["pending_open"] is True
                assert recovery["history_warmup"]["regime_ok"] is True
                assert recovery["history_warmup"]["m1_ok"] is True
                assert self.state["startup_state_recovery"]["status"] == "reconciled"
                assert self.state["pending_open"] is None
                assert self.state["reconciliation_required"] is None
                assert self.state["pending_grid_repair_wait"] is None
                assert self.state["grid_anchor"] is None
                assert not self.state["sync_block_new_entries"]

                self.get_tick = lambda: None  # type: ignore[method-assign]
                self.state = self.default_state()
                recovery = self.startup_reconcile_state()
                assert recovery["status"] == "deferred"
                assert recovery["reason"] == "startup tick unavailable"
                assert self.state["startup_state_recovery"]["status"] == "deferred"
            finally:
                self.get_tick = original_get_tick  # type: ignore[method-assign]
                self.get_regime = original_get_regime  # type: ignore[method-assign]
                self.get_m1_policy_features = original_get_m1_policy_features  # type: ignore[method-assign]
        finally:
            self.params = original_params
            self.state = original_state
            self.state_file = original_state_file
            self.trade_log_file = original_trade_log_file
            self.executor = original_executor
            self._suppress_manual_alerts = original_suppress_manual_alerts
            shutil.rmtree(temp_dir, ignore_errors=True)
        logging.info("s19 self-test passed")

class S19BasketRunner:
    def __init__(self, raw_params: dict[str, Any] | None = None) -> None:
        self.raw_params = raw_params or load_params()
        self.policy = EventFilterPolicy(self.raw_params) if bool(self.raw_params.get("policy_enabled", True)) else None
        self.bots = [
            S19SnowballBot(profile_params, policy=self.policy)
            for profile_params in build_profile_params(self.raw_params)
        ]

    def self_test(self, include_policy: bool = False) -> None:
        for bot in self.bots:
            bot.self_test()
        if include_policy and self.policy is not None:
            self.policy.self_test()
        logging.info("s19 basket self-test passed symbols=%s", [bot.symbol for bot in self.bots])

    def run_forever(self) -> None:
        if not bool(self.raw_params.get("enabled", True)):
            logging.warning("s19 basket is disabled by params enabled=false")
            return
        live_trading_enabled = bool(self.raw_params.get("live_trading_enabled", False))
        shadow_forward_enabled = bool(self.raw_params.get("shadow_forward_enabled", False))
        if not live_trading_enabled and not shadow_forward_enabled:
            logging.warning("s19 basket live_trading_enabled=false and shadow_forward_enabled=false; idle loop only")
            while True:
                time.sleep(max(10.0, float(self.raw_params["status_log_interval_seconds"])))
        for bot in self.bots:
            try:
                bot.save_state()
            except Exception as exc:
                logging.critical(
                    "State persistence is unavailable for %s. Refusing to connect to bridge: %s",
                    bot.symbol,
                    exc,
                )
                raise RuntimeError(f"State persistence is unavailable for {bot.symbol}") from exc
            if not bot.connect():
                raise RuntimeError(f"Failed to connect to MT5 EA bridge for {bot.symbol}")
            bot.verify_bridge_capabilities()
            bot.startup_reconcile_state()
            bot.save_state()
        if not live_trading_enabled:
            logging.warning("S19 basket shadow forward mode: policy decisions logged, no orders")
        logging.info(
            "S19 basket started: symbols=%s live_trading_enabled=%s shadow_forward_enabled=%s",
            [bot.symbol for bot in self.bots],
            live_trading_enabled,
            shadow_forward_enabled,
        )
        logging.info("S19 source revision: revision=%s file=%s", BOT_SOURCE_REVISION, __file__)
        sleep_seconds = min(float(bot.params["poll_interval_seconds"]) for bot in self.bots)
        while True:
            for bot in self.bots:
                try:
                    bot.run_once()
                except KeyboardInterrupt:
                    raise
                except Exception:
                    logging.exception("Unhandled s19 basket loop error for %s", bot.symbol)
            time.sleep(sleep_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="S19 basket Snowball event-filter live/shadow bot")
    parser.add_argument("--self-test", action="store_true", help="run pure logic checks without bridge connection")
    parser.add_argument("--policy-self-test", action="store_true", help="also load frozen ML artifacts and test predict")
    args = parser.parse_args()
    configure_logging()
    raw_params = load_params()
    if args.policy_self_test:
        raw_params = raw_params.copy()
        raw_params["policy_enabled"] = True
    elif args.self_test:
        raw_params = raw_params.copy()
        raw_params["policy_enabled"] = False
    runner = S19BasketRunner(raw_params)
    if args.self_test or args.policy_self_test:
        runner.self_test(include_policy=bool(args.policy_self_test))
        return 0
    runner.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
