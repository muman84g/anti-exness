# -*- coding: utf-8 -*-
"""S18 basket Snowball anti-grid live/shadow bot.

This runner keeps the bot18 execution model and adds a frozen cycle-start
event filter for the forward-test basket:
- GBPUSD CatBoost
- EURUSD LightGBM
- AUDUSD CatBoost
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import os
import sys
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
MARKET_OPEN_DEFINITIVE_UNFILLED_RETCODES = {
    "10004",  # requote
    "10006",  # rejected
    "10007",  # canceled by trader/server
    "10011",  # generic request error
    "10013",  # invalid request
    "10014",  # invalid volume
    "10015",  # invalid price
    "10016",  # invalid stops
    "10017",  # trade disabled
    "10018",  # market closed
    "10019",  # not enough money
    "10020",  # price changed
    "10021",  # no quotes
    "10022",  # invalid expiration
    "10024",  # too many requests
    "10026",  # server disables autotrading
    "10027",  # client disables autotrading
    "10029",  # order/position frozen
    "10030",  # invalid filling mode
    "10032",  # only real accounts allowed
    "10033",  # pending order limit reached
    "10034",  # volume limit reached
    "10035",  # invalid order type
    "10036",  # position already closed
}

LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "s18_bot.log")
TRADE_LOG_FILE = os.path.join(LOG_DIR, "s18_trades.csv")
POLICY_LOG_FILE = os.path.join(LOG_DIR, "s18_policy_decisions.csv")
STATE_DIR = os.path.join(SCRIPT_DIR, "state")
PARAMS_FILE = os.path.join(SCRIPT_DIR, "s18_params.json")
ARTIFACTS_DIR = os.path.join(SCRIPT_DIR, "artifacts")

DEFAULT_PARAMS: dict[str, Any] = {
    "enabled": True,
    "live_trading_enabled": False,
    "shadow_forward_enabled": True,
    "symbol": "GBPUSD",
    "magic": 180218,
    "comment_prefix": "s18v2_snow",
    "strategy_id": "bot18_snowball_fixed_cycle_start_v1",
    "lot": 0.01,
    "distance_pips": 5.0,
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
    "market_open_reconcile_enabled": True,
    "market_open_retry_cooldown_seconds": 30,
    "policy_enabled": True,
    "policy_fail_closed": True,
    "policy_decision_log_enabled": True,
    "policy_decision_log_interval_seconds": 300,
    "policy_decision_log_pass_always": True,
    "policy_decision_log_error_always": True,
    "policy_artifacts_dir": ARTIFACTS_DIR,
    "policy_spread_add_points": 2.0,
    "policy_max_entry_spread_points": 9.0,
    "policy_registry_file": "candidate_registry.json",
    "policy_selected_features_file": "selected_features.csv",
    "profiles": [
        {
            "symbol": "GBPUSD",
            "magic": 180218,
            "comment_prefix": "s18v2_gbp",
        },
        {
            "symbol": "EURUSD",
            "magic": 180219,
            "comment_prefix": "s18v2_eur",
        },
        {
            "symbol": "AUDUSD",
            "magic": 180220,
            "comment_prefix": "s18v2_aud",
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
            raise ValueError("s18_params.json must contain a JSON object")
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


class S18SnowballBot:
    def __init__(self, params: dict[str, Any] | None = None, policy: EventFilterPolicy | None = None) -> None:
        self.params = DEFAULT_PARAMS.copy()
        self.params.update(params or load_params())
        self.symbol = str(self.params["symbol"]).upper()
        self.magic = int(self.params["magic"])
        safe_symbol = "".join(ch.lower() for ch in self.symbol if ch.isalnum())
        self.state_file = str(
            self.params.get(
                "state_file",
                os.path.join(STATE_DIR, f"s18_{safe_symbol}_bot_state.json"),
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
        self.last_policy_decision_log_epoch = 0.0
        self.last_policy_decision_log_signature: tuple[Any, ...] | None = None
        self.last_market_open_failure_log_epoch = 0.0
        self.sync_closed_count = 0
        self._suppress_manual_alerts = False

    @property
    def pip_size(self) -> float:
        return float(self.params["pip_size"])

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
            "strategy": "s18_snowball_cycle_start_event_filter",
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
            "market_open_retry_after_epoch": None,
            "last_market_open_failure": None,
            "reconciliation_required": None,
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
            raise ValueError("State symbol/magic does not match s18_params.json")
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
        if not bool(self.dm.connect()):
            logging.critical("S18 failed to connect to EA bridge for %s.", self.symbol)
            return False

        from live_executor import REQUIRED_S18_COMMANDS, S18_BRIDGE_NAME

        caps = self.executor.get_bridge_capabilities()
        if not caps:
            return False
        missing = sorted(REQUIRED_S18_COMMANDS - set(caps["commands"]))
        if caps["name"] != S18_BRIDGE_NAME:
            logging.critical("Bridge name mismatch: expected=%s got=%s", S18_BRIDGE_NAME, caps["name"])
            return False
        if missing:
            logging.critical("Bridge command mismatch: missing=%s caps=%s", missing, sorted(caps["commands"]))
            return False
        logging.info(
            "EA bridge capabilities verified: name=%s version=%s commands=%s",
            caps["name"],
            caps["version"],
            ",".join(sorted(caps["commands"])),
        )
        return True

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
        if not bool(self.params.get("policy_decision_log_enabled", True)):
            return
        reason = str(decision.get("reason", ""))
        allowed = bool(decision.get("allow", False))
        signature = (
            self.symbol,
            str(decision.get("candidate_id", "")),
            str(decision.get("model", "")),
            allowed,
            reason,
        )
        now = time.time()
        interval = float(self.params.get("policy_decision_log_interval_seconds", 300.0) or 0.0)
        should_log = False
        if allowed and bool(self.params.get("policy_decision_log_pass_always", True)):
            should_log = True
        elif reason.startswith("policy_error") and bool(self.params.get("policy_decision_log_error_always", True)):
            should_log = True
        elif signature != self.last_policy_decision_log_signature:
            should_log = True
        elif interval <= 0.0 or now - self.last_policy_decision_log_epoch >= interval:
            should_log = True
        if not should_log:
            return

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
        if append_csv_row(self.policy_log_file, header, row):
            self.last_policy_decision_log_epoch = now
            self.last_policy_decision_log_signature = signature

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

    def add_virtual_order(self, direction: int, entry: float, stop_loss: float) -> None:
        if len(self.state["virtual_orders"]) >= int(self.params["max_virtual_orders"]):
            self.block_new_entries("max virtual orders reached")
            return
        entry = self.price(entry)
        stop_loss = self.price(stop_loss)
        key = (direction, stop_loss)
        if self.coverage_counts().get(key, 0) > 0:
            return
        order = {
            "order_id": int(self.state["next_order_id"]),
            "direction": direction_name(direction),
            "entry": entry,
            "stop_loss": stop_loss,
            "crossed_while_spread_blocked": False,
            "created_at_jst": jst_now().isoformat(),
        }
        self.state["next_order_id"] = int(self.state["next_order_id"]) + 1
        self.state["virtual_orders"].append(order)

    def ensure_orders(self, direction: int) -> None:
        anchor = float(self.state["grid_anchor"])
        distance = self.distance_price
        if direction == LONG:
            self.add_virtual_order(LONG, anchor + distance, anchor)
            self.add_virtual_order(LONG, anchor + 2.0 * distance, anchor + distance)
        else:
            self.add_virtual_order(SHORT, anchor - distance, anchor)
            self.add_virtual_order(SHORT, anchor - 2.0 * distance, anchor - distance)

    def start_cycle(self, bid: float) -> None:
        if self.state["positions"] or self.state["virtual_orders"]:
            raise RuntimeError("cycle start requires flat state")
        self.state["cycle_id"] = int(self.state["cycle_id"]) + 1
        self.state["cycle_realized_usd"] = 0.0
        self.state["grid_anchor"] = self.price(bid)
        self.state["auto_tp_price"] = None
        self.state["estimated_auto_tp_profit_usd"] = 0.0
        self.state["restart_next_tick"] = False
        self.ensure_orders(LONG)
        self.ensure_orders(SHORT)
        logging.info(f"Started s18 v2 cycle {self.state['cycle_id']} anchor={self.state['grid_anchor']}")

    def block_new_entries(self, reason: str) -> None:
        self.state["sync_block_new_entries"] = True
        self.state["sync_block_reason"] = reason
        logging.error(f"New entries blocked: {reason}")
        reconciliation = self.state.get("reconciliation_required")
        if isinstance(reconciliation, dict):
            self.notify_reconciliation_required(reason, reconciliation)

    def clear_new_entry_block_if_reason(self, reason: str) -> None:
        if not self.state.get("sync_block_new_entries"):
            return
        if self.state.get("sync_block_reason") != reason:
            return
        self.state["sync_block_new_entries"] = False
        self.state["sync_block_reason"] = None
        logging.warning(f"New-entry block cleared after recovery: {reason}")

    def clear_virtual_orders(self) -> None:
        self.state["virtual_orders"] = []

    def clear_auto_tp(self) -> None:
        self.state["auto_tp_price"] = None
        self.state["estimated_auto_tp_profit_usd"] = 0.0

    def set_reconciliation_required(self, reason: str, details: dict[str, Any]) -> None:
        self.state["reconciliation_required"] = {
            "type": "market_open_result",
            "symbol": self.symbol,
            "magic": self.magic,
            "cycle_id": int(self.state.get("cycle_id", 0)),
            "grid_anchor": self.state.get("grid_anchor"),
            "reason": reason,
            "details": details,
            "created_at_jst": jst_now().isoformat(),
        }
        self.block_new_entries(reason)

    def autotrading_disabled_error(self, error: str | None) -> bool:
        text = str(error or "").strip()
        return text in {"ERR|10026", "ERR|10027"}

    def notify_manual_action(self, *, title: str, reason: str, action: str, key: str) -> None:
        if self._suppress_manual_alerts:
            return
        notify_manual_action_required(
            bot_id="bot18",
            symbol=self.symbol,
            title=title,
            reason=reason,
            action=action,
            key=key,
        )

    def notify_reconciliation_required(self, reason: str, details: dict[str, Any]) -> None:
        text = f"{reason}; details={json.dumps(details, ensure_ascii=True, sort_keys=True)}"
        if "ERR|10026" in text or "ERR|10027" in text:
            self.notify_manual_action(
                title="MT5 Algo Trading disabled or trading permission rejected",
                reason=reason,
                action=(
                    "Turn on MT5 Algo Trading for exness-bot-18/BotBridge_s18 and verify "
                    "the EA Allow Algo Trading setting; then inspect state/orders before clearing any block."
                ),
                key=f"bot18:{self.symbol}:autotrading-disabled",
            )
            return
        self.notify_manual_action(
            title="reconciliation_required",
            reason=reason,
            action=(
                "Inspect MT5 positions/orders and bot18 state/logs before clearing the block or restarting entries."
            ),
            key=f"bot18:{self.symbol}:reconciliation:{reason}",
        )

    def market_open_retry_block_reason(self) -> str | None:
        retry_after = self.state.get("market_open_retry_after_epoch")
        if retry_after is None:
            return None
        remaining = float(retry_after) - time.time()
        if remaining <= 0.0:
            self.state["market_open_retry_after_epoch"] = None
            return None
        return f"market_open_retry_cooldown remaining_seconds={remaining:.1f}"

    def broker_reject_definitively_unfilled(self, error: str | None) -> bool:
        text = str(error or "").strip()
        if not text:
            return False
        if text in {"INFO_UNAVAILABLE", "ERR|INFO_TICK"}:
            return True
        if text in {"NO_RESPONSE", "ERR|TIMEOUT", "ERR|LOCK_TIMEOUT", "ERR|WRITE_FAILED"}:
            return False
        if text.startswith("ERR|"):
            code = text.split("|", 1)[1]
            return code in MARKET_OPEN_DEFINITIVE_UNFILLED_RETCODES
        return False

    def sanitized_comment_prefix(self) -> str:
        return str(self.params["comment_prefix"]).replace("|", "_").replace(",", "_")

    def market_order_comment(self, order: dict[str, Any]) -> str:
        prefix = self.sanitized_comment_prefix()
        suffix = f"_{int(self.state['cycle_id'])}_{int(order['order_id'])}"
        if len(suffix) >= 31:
            return suffix[-31:]
        return f"{prefix[: 31 - len(suffix)]}{suffix}"

    def market_order_matches_position(self, order: dict[str, Any], position: Any, comment: str) -> bool:
        if int(getattr(position, "magic", -1)) != int(self.magic):
            return False
        if str(getattr(position, "symbol", "")).upper() != self.symbol:
            return False
        expected_direction = direction_name(direction_from_name(order["direction"]))
        if str(getattr(position, "direction", "")) != expected_direction:
            return False
        return bool(comment) and str(getattr(position, "comment", "")) == comment

    def parse_market_order_comment(self, comment: str) -> tuple[int | None, int | None]:
        parts = str(comment or "").rsplit("_", 2)
        if len(parts) < 3:
            return None, None
        try:
            return int(parts[-2]), int(parts[-1])
        except ValueError:
            return None, None

    def live_position_is_recoverable(self, position: Any) -> bool:
        if int(getattr(position, "magic", -1)) != int(self.magic):
            return False
        if str(getattr(position, "symbol", "")).upper() != self.symbol:
            return False
        comment = str(getattr(position, "comment", "") or "")
        prefix = self.sanitized_comment_prefix()
        if not comment.startswith(f"{prefix}_"):
            return False
        try:
            direction_from_name(str(getattr(position, "direction", "")))
        except ValueError:
            return False
        sl = float(getattr(position, "sl", 0.0) or 0.0)
        return sl > 0.0

    def adopt_untracked_live_position(self, live_position: Any, reason: str) -> None:
        ticket = int(getattr(live_position, "ticket"))
        if any(int(position["ticket"]) == ticket for position in self.state["positions"]):
            return
        direction_text = direction_name(direction_from_name(str(getattr(live_position, "direction", ""))))
        comment = str(getattr(live_position, "comment", "") or "")
        parsed_cycle_id, parsed_order_id = self.parse_market_order_comment(comment)
        if parsed_cycle_id is not None:
            self.state["cycle_id"] = max(int(self.state.get("cycle_id", 0)), int(parsed_cycle_id))
        stop_loss = self.price(float(getattr(live_position, "sl", 0.0) or 0.0))
        if self.state.get("grid_anchor") is None:
            self.state["grid_anchor"] = stop_loss
        self.state["positions"].append(
            {
                "ticket": ticket,
                "direction": direction_text,
                "entry": self.price(float(getattr(live_position, "open_price"))),
                "stop_loss": stop_loss,
                "volume": float(getattr(live_position, "volume", self.params["lot"])),
                "opened_at_jst": jst_now().isoformat(),
                "source_order_id": int(parsed_order_id) if parsed_order_id is not None else "",
                "comment": comment,
                "recovered_from_live": True,
            }
        )
        self.recalculate_position_counts()
        self.log_trade_csv(
            "ENTRY_RECOVERED_SYNC",
            ticket,
            direction=direction_text,
            lot_size=float(getattr(live_position, "volume", self.params["lot"])),
            price=self.price(float(getattr(live_position, "open_price"))),
            stop_loss=stop_loss,
            pnl="",
            reason=reason,
            source_order_id=int(parsed_order_id) if parsed_order_id is not None else "",
            comment=comment,
        )
        logging.warning(
            "Recovered untracked live position into state ticket=%s reason=%s comment=%s",
            ticket,
            reason,
            comment,
        )

    def recover_untracked_live_positions(self, live_positions: list[Any], reason: str) -> bool:
        if not live_positions:
            return True
        for position in live_positions:
            if not self.live_position_is_recoverable(position):
                return False
        self.clear_virtual_orders()
        for position in live_positions:
            self.adopt_untracked_live_position(position, reason)
        if isinstance(self.state.get("reconciliation_required"), dict) and (
            self.state["reconciliation_required"].get("type") == "market_open_result"
        ):
            self.state["reconciliation_required"] = None
        sync_reason = str(self.state.get("sync_block_reason") or "")
        if sync_reason.startswith("market open result") or sync_reason.startswith("untracked live positions"):
            self.state["sync_block_new_entries"] = False
            self.state["sync_block_reason"] = None
        return True

    def adopt_market_open_position(self, order: dict[str, Any], live_position: Any, comment: str, reason: str) -> None:
        ticket = int(getattr(live_position, "ticket"))
        direction = direction_from_name(order["direction"])
        self.state["virtual_orders"] = [
            item for item in self.state["virtual_orders"] if int(item["order_id"]) != int(order["order_id"])
        ]
        self.state["positions"].append(
            {
                "ticket": ticket,
                "direction": direction_name(direction),
                "entry": self.price(float(getattr(live_position, "open_price"))),
                "stop_loss": self.price(float(order["stop_loss"])),
                "volume": float(getattr(live_position, "volume", self.params["lot"])),
                "opened_at_jst": jst_now().isoformat(),
                "source_order_id": int(order["order_id"]),
                "comment": str(getattr(live_position, "comment", comment)),
            }
        )
        self.state["market_open_retry_after_epoch"] = None
        self.state["last_market_open_failure"] = None
        if isinstance(self.state.get("reconciliation_required"), dict) and (
            self.state["reconciliation_required"].get("type") == "market_open_result"
        ):
            self.state["reconciliation_required"] = None
        sync_reason = str(self.state.get("sync_block_reason") or "")
        if sync_reason.startswith("market open result"):
            self.state["sync_block_new_entries"] = False
            self.state["sync_block_reason"] = None
        self.recalculate_position_counts()
        self.log_trade_csv(
            "ENTRY",
            ticket,
            direction=direction_name(direction),
            lot_size=float(getattr(live_position, "volume", self.params["lot"])),
            price=self.price(float(getattr(live_position, "open_price"))),
            stop_loss=self.price(float(order["stop_loss"])),
            pnl="",
            reason=reason,
            source_order_id=int(order["order_id"]),
            comment=str(getattr(live_position, "comment", comment)),
        )
        logging.warning(
            "Adopted market open result as position ticket=%s source_order_id=%s reason=%s",
            ticket,
            int(order["order_id"]),
            reason,
        )

    def reconcile_market_open_result(self, order: dict[str, Any], comment: str, error: str | None) -> bool | None:
        if not bool(self.params.get("market_open_reconcile_enabled", True)):
            return None
        live_positions = self.executor.get_positions(self.symbol, self.magic)
        if live_positions is None:
            self.log_unresolved_market_open(order, comment, error, "position_sync_failed_after_market_open_error")
            self.set_reconciliation_required(
                "market open result unresolved: position sync failed",
                {
                    "order": order.copy(),
                    "comment": comment,
                    "error": error,
                },
            )
            return None
        matches = [
            position
            for position in live_positions
            if self.market_order_matches_position(order, position, comment)
        ]
        state_tickets = {int(position["ticket"]) for position in self.state["positions"]}
        match_tickets = {int(getattr(position, "ticket")) for position in matches}
        untracked_positions = [
            position
            for position in live_positions
            if int(getattr(position, "ticket")) not in state_tickets
            and int(getattr(position, "ticket")) not in match_tickets
        ]
        if untracked_positions and not self.recover_untracked_live_positions(
            untracked_positions,
            "untracked_live_position_recovered_after_market_open_error",
        ):
            untracked_tickets = sorted(int(getattr(position, "ticket")) for position in untracked_positions)
            self.log_unresolved_market_open(order, comment, error, "unrecoverable_untracked_positions_after_market_open_error")
            self.set_reconciliation_required(
                "market open result unresolved: unrecoverable untracked live positions",
                {
                    "order": order.copy(),
                    "comment": comment,
                    "error": error,
                    "untracked_tickets": untracked_tickets,
                },
            )
            return None
        if untracked_positions and not matches:
            return True
        if not matches:
            return False
        if len(matches) > 1:
            self.log_unresolved_market_open(order, comment, error, "ambiguous_market_open_result")
            self.set_reconciliation_required(
                "market open result ambiguous: multiple matching positions",
                {
                    "order": order.copy(),
                    "comment": comment,
                    "error": error,
                    "tickets": [int(getattr(position, "ticket")) for position in matches],
                },
            )
            return None
        self.adopt_market_open_position(order, matches[0], comment, "market_open_reconciled_after_error")
        return True

    def record_definitive_market_open_reject(
        self,
        order: dict[str, Any],
        comment: str,
        error: str | None,
        tick: dict[str, Any],
    ) -> None:
        cooldown = max(0.0, float(self.params.get("market_open_retry_cooldown_seconds", 30.0) or 0.0))
        retry_after = time.time() + cooldown if cooldown > 0.0 else None
        self.state["market_open_retry_after_epoch"] = retry_after
        self.state["last_market_open_failure"] = {
            "type": "definitive_reject",
            "order_id": int(order["order_id"]),
            "direction": str(order.get("direction", "")),
            "entry": self.price(float(order.get("entry", 0.0))),
            "stop_loss": self.price(float(order.get("stop_loss", 0.0))),
            "comment": comment,
            "error": str(error or "UNKNOWN"),
            "retry_after_epoch": retry_after,
            "created_at_jst": jst_now().isoformat(),
        }
        self.log_trade_csv(
            "ENTRY_FAIL_MARKET",
            0,
            direction=str(order.get("direction", "")),
            lot_size=float(self.normalize_lot(tick["info"])),
            price=self.price(float(tick["ask"] if order["direction"] == "LONG" else tick["bid"])),
            stop_loss=self.price(float(order["stop_loss"])),
            pnl="",
            reason=f"market_open_rejected:{error or 'UNKNOWN'}",
            source_order_id=int(order["order_id"]),
            comment=comment,
        )
        logging.warning(
            "S18 market OPEN rejected without fill; keeping virtual order for retry after cooldown: "
            "symbol=%s order_id=%s error=%s cooldown=%.1fs",
            self.symbol,
            int(order["order_id"]),
            error,
            cooldown,
        )
        if self.autotrading_disabled_error(error):
            self.notify_manual_action(
                title="MT5 Algo Trading disabled or trading permission rejected",
                reason=f"market OPEN rejected with {error}",
                action=(
                    "Turn on MT5 Algo Trading for exness-bot-18/BotBridge_s18 and verify "
                    "the EA Allow Algo Trading setting."
                ),
                key=f"bot18:{self.symbol}:autotrading-disabled",
            )

    def log_unresolved_market_open(
        self,
        order: dict[str, Any],
        comment: str,
        error: str | None,
        reason: str,
    ) -> None:
        self.log_trade_csv(
            "ENTRY_UNRESOLVED_MARKET",
            0,
            direction=str(order.get("direction", "")),
            lot_size=float(self.params.get("lot", 0.0)),
            price=self.price(float(order.get("entry", 0.0))),
            stop_loss=self.price(float(order.get("stop_loss", 0.0))),
            pnl="",
            reason=f"{reason}:{error or 'UNKNOWN'}",
            source_order_id=int(order["order_id"]),
            comment=comment,
        )

    def handle_market_open_failure(self, order: dict[str, Any], comment: str, tick: dict[str, Any]) -> bool:
        error = getattr(self.executor, "last_order_error", "UNKNOWN")
        reconcile_result = self.reconcile_market_open_result(order, comment, str(error))
        if reconcile_result is True:
            return True
        if reconcile_result is None:
            if not isinstance(self.state.get("reconciliation_required"), dict):
                self.log_unresolved_market_open(
                    order,
                    comment,
                    str(error),
                    "market_open_reconciliation_unavailable",
                )
                self.set_reconciliation_required(
                    "market open result unresolved: reconciliation disabled",
                    {
                        "order": order.copy(),
                        "comment": comment,
                        "error": str(error),
                    },
                )
            return False
        if self.broker_reject_definitively_unfilled(str(error)):
            self.record_definitive_market_open_reject(order, comment, str(error), tick)
            return False
        self.log_unresolved_market_open(order, comment, str(error), "ambiguous_market_open_error")
        self.set_reconciliation_required(
            "market open result unresolved",
            {
                "order": order.copy(),
                "comment": comment,
                "error": str(error),
            },
        )
        return False

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

    def sync_live_positions(self, tick: dict[str, Any]) -> bool:
        self.sync_closed_count = 0
        live_positions = self.executor.get_positions(self.symbol, self.magic)
        if live_positions is None:
            self.block_new_entries("position sync failed")
            return False
        live_by_ticket = {int(p.ticket): p for p in live_positions}
        state_by_ticket = {int(p["ticket"]): p for p in self.state["positions"]}

        extra_tickets = sorted(set(live_by_ticket).difference(state_by_ticket))
        for ticket in list(extra_tickets):
            live_position = live_by_ticket[ticket]
            matching_order = None
            matching_comment = ""
            for order in list(self.state["virtual_orders"]):
                comment = self.market_order_comment(order)
                if self.market_order_matches_position(order, live_position, comment):
                    matching_order = order
                    matching_comment = comment
                    break
            if matching_order is None:
                continue
            self.adopt_market_open_position(
                matching_order,
                live_position,
                matching_comment,
                "market_open_reconciled_on_sync",
            )
            state_by_ticket[ticket] = self.state["positions"][-1]
            extra_tickets.remove(ticket)
        if extra_tickets:
            extra_positions = [live_by_ticket[ticket] for ticket in extra_tickets]
            if not self.recover_untracked_live_positions(
                extra_positions,
                "untracked_live_position_recovered_on_sync",
            ):
                self.block_new_entries(f"unrecoverable untracked live positions exist: {extra_tickets}")
                return False
            state_by_ticket = {int(p["ticket"]): p for p in self.state["positions"]}

        for ticket, position in list(state_by_ticket.items()):
            live_position = live_by_ticket.get(ticket)
            if live_position is None:
                if bool(self.params["assume_missing_state_position_is_sl"]):
                    exit_price = float(position["stop_loss"])
                    pnl = self.estimate_position_pnl(position, exit_price, tick["info"])
                    self.state["cycle_realized_usd"] = float(self.state["cycle_realized_usd"]) + pnl
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
        if not self.state["positions"]:
            self.clear_auto_tp()
        self.clear_new_entry_block_if_reason("position sync failed")
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
            result = self.executor.close_position(ticket, deviation=int(self.params["deviation_points"]))
            if result:
                close_price = float(getattr(result, "close_price", 0.0) or stop_loss)
                self.remove_position_by_ticket(ticket)
                pnl = float(getattr(result, "profit", 0.0) or 0.0)
                self.state["cycle_realized_usd"] = float(self.state["cycle_realized_usd"]) + pnl
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
        minimum_pips = float(self.params["base_add_distance_pips"])
        if self.state["positions"] and self.inactive_distance_applies(regime):
            minimum_pips = max(minimum_pips, float(self.params["inactive_add_distance_pips"]))
        if minimum_pips <= 0.0:
            return True
        minimum_price = minimum_pips * self.pip_size
        direction = direction_from_name(order["direction"])
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
        if not bool(self.params.get("live_trading_enabled", False)):
            return 0
        if self.state.get("sync_block_new_entries"):
            return 0
        if isinstance(self.state.get("reconciliation_required"), dict):
            return 0
        retry_block = self.market_open_retry_block_reason()
        if retry_block is not None:
            now = time.time()
            if now - self.last_market_open_failure_log_epoch >= float(self.params["status_log_interval_seconds"]):
                logging.warning("S18 market OPEN retry is temporarily blocked: %s", retry_block)
                self.last_market_open_failure_log_epoch = now
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
        comment = self.market_order_comment(order)
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
            return self.handle_market_open_failure(order, comment, tick)
        entry = float(getattr(ticket, "price", 0.0) or (tick["ask"] if direction == LONG else tick["bid"]))
        self.state["market_open_retry_after_epoch"] = None
        self.state["last_market_open_failure"] = None
        self.state["virtual_orders"] = [
            item for item in self.state["virtual_orders"] if int(item["order_id"]) != int(order["order_id"])
        ]
        self.state["positions"].append(
            {
                "ticket": int(ticket),
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

    def manage_orders_and_grid(self, bid: float, ask: float) -> None:
        if self.state["grid_anchor"] is None:
            return
        if self.state.get("sync_block_new_entries") or isinstance(self.state.get("reconciliation_required"), dict):
            return
        level = int(self.state.get("long_count", 0)) - int(self.state.get("short_count", 0))
        if level == 0:
            self.ensure_orders(LONG)
            self.ensure_orders(SHORT)
        elif level > 0:
            self.ensure_orders(LONG)
        else:
            self.ensure_orders(SHORT)

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
        self.clear_virtual_orders()
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
        self.clear_virtual_orders()
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
        if not self.sync_live_positions(tick):
            self.save_state()
            return
        if not bool(self.params.get("live_trading_enabled", False)) and (
            self.state["positions"] or self.state["virtual_orders"]
        ):
            logging.critical("S18 shadow mode requires flat state; refusing to manage positions or virtual orders")
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
            if self.state.get("sync_block_new_entries") or isinstance(self.state.get("reconciliation_required"), dict):
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
                    "S18 policy blocked cycle start: "
                    f"symbol={self.symbol} reason={policy_decision.get('reason')} "
                    f"proba={policy_decision.get('pred_proba')} threshold={policy_decision.get('threshold')}"
                )
                self.log_status(tick, regime)
                self.save_state()
                return
            if not bool(self.params.get("live_trading_enabled", False)):
                logging.info(
                    "S18 shadow policy allowed cycle start but live_trading_enabled=false; "
                    f"symbol={self.symbol} proba={policy_decision.get('pred_proba')} "
                    f"threshold={policy_decision.get('threshold')}"
                )
                self.log_status(tick, regime)
                self.save_state()
                return
            self.start_cycle(tick["bid"])

        stop_count = int(self.sync_closed_count)
        stop_count += self.process_stops(tick["bid"], tick["ask"], tick["info"])
        self.fill_virtual_orders(tick, regime)
        immediate_stops = self.process_stops(tick["bid"], tick["ask"], tick["info"])
        stop_count += immediate_stops
        self.manage_orders_and_grid(tick["bid"], tick["ask"])
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
            "S18 status: "
            f"bid={tick['bid']:.5f} ask={tick['ask']:.5f} spread_points={tick['spread_points']:.1f} "
            f"cycle={self.state['cycle_id']} pos={len(self.state['positions'])} orders={len(self.state['virtual_orders'])} "
            f"auto_tp={self.state.get('auto_tp_price')} regime_allowed={regime.get('entry_allowed')} "
            f"fresh={regime.get('signal_fresh')} block={self.state.get('sync_block_new_entries')} "
            f"reconcile_block={isinstance(self.state.get('reconciliation_required'), dict)} "
            f"policy_allowed={last_policy.get('allow')} policy_reason={last_policy.get('reason')}"
        )

    def run_forever(self) -> None:
        if not bool(self.params.get("enabled", True)):
            logging.warning("s18 v2 is disabled by params enabled=false")
            return
        live_trading_enabled = bool(self.params.get("live_trading_enabled", False))
        shadow_forward_enabled = bool(self.params.get("shadow_forward_enabled", False))
        if not live_trading_enabled and not shadow_forward_enabled:
            logging.warning("s18 live_trading_enabled=false and shadow_forward_enabled=false; idle loop only")
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
        if not live_trading_enabled:
            logging.warning("S18 shadow forward mode: bridge connected, policy decisions logged, no orders")
        logging.info(
            "S18 started: "
            f"symbol={self.symbol} magic={self.magic} lot={self.params['lot']} "
            f"distance={self.params['distance_pips']} inactive_add={self.params['inactive_add_distance_pips']}"
        )
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                raise
            except Exception:
                logging.exception("Unhandled s18 v2 loop error")
            time.sleep(float(self.params["poll_interval_seconds"]))

    def self_test(self) -> None:
        original_suppress_manual_alerts = self._suppress_manual_alerts
        self._suppress_manual_alerts = True
        self.state = self.default_state()
        self.start_cycle(1.25000)
        assert len(self.state["virtual_orders"]) == 4, self.state["virtual_orders"]
        long_order = min(
            [o for o in self.state["virtual_orders"] if o["direction"] == "LONG"],
            key=lambda item: item["entry"],
        )
        assert long_order["entry"] == 1.25050
        assert long_order["stop_loss"] == 1.25000
        self.state["positions"] = [
            {
                "ticket": 1,
                "direction": "LONG",
                "entry": 1.25050,
                "stop_loss": 1.25000,
                "volume": 0.01,
            }
        ]
        self.recalculate_position_counts()
        order = {"direction": "LONG", "entry": 1.25100, "stop_loss": 1.25050}
        assert not self.virtual_order_fill_allowed(
            order,
            1.25091,
            1.25100,
            {"entry_allowed": True, "signal_fresh": True},
        )
        assert self.virtual_order_fill_allowed(
            order,
            1.25276,
            1.25285,
            {"entry_allowed": False, "signal_fresh": True},
        )
        assert not self.auto_tp_profit_guard_passed(1.25099, 1.25108)
        assert self.auto_tp_profit_guard_passed(1.26000, 1.26009)
        assert self.broker_reject_definitively_unfilled("ERR|10016")
        assert self.broker_reject_definitively_unfilled("ERR|10020")
        assert not self.broker_reject_definitively_unfilled("ERR|10009")
        assert not self.broker_reject_definitively_unfilled("ERR|10010")
        assert not self.broker_reject_definitively_unfilled("ERR|10012")
        assert not self.broker_reject_definitively_unfilled("ERR|10028")
        assert not self.broker_reject_definitively_unfilled("ERR|10031")
        original_prefix = self.params["comment_prefix"]
        self.params["comment_prefix"] = "s18|very,long,prefix_that_should_be_truncated"
        safe_comment = self.market_order_comment({"order_id": 123456789})
        assert len(safe_comment) == 31
        assert "|" not in safe_comment and "," not in safe_comment
        assert safe_comment.startswith("s18_very_long")
        assert safe_comment.endswith("_1_123456789")
        assert self.market_order_comment({"order_id": 123456788}) != safe_comment
        self.params["comment_prefix"] = original_prefix

        class FakeInfo:
            volume_min = 0.01
            volume_max = 10.0
            volume_step = 0.01

        class FakePosition:
            def __init__(
                self,
                ticket: int,
                symbol: str,
                magic: int,
                direction: str,
                volume: float,
                open_price: float,
                sl: float,
                comment: str,
            ) -> None:
                self.ticket = ticket
                self.symbol = symbol
                self.magic = magic
                self.direction = direction
                self.volume = volume
                self.open_price = open_price
                self.sl = sl
                self.comment = comment

        class FakeExecutor:
            def __init__(self, error: str, create_position_after_error: bool = False) -> None:
                self.last_order_error: str | None = None
                self.error = error
                self.create_position_after_error = create_position_after_error
                self.positions: list[FakePosition] = []
                self.open_calls = 0
                self.position_ticket = 900001

            def open_position(
                self,
                symbol: str,
                order_type: int,
                lot_size: float,
                sl: float = 0.0,
                tp: float = 0.0,
                deviation: int = 20,
                magic: int = 123456,
                comment: str = "",
                digits: int | None = None,
            ) -> None:
                del tp, deviation, digits
                self.open_calls += 1
                self.last_order_error = self.error
                if self.create_position_after_error:
                    direction = "LONG" if order_type == ORDER_TYPE_BUY else "SHORT"
                    open_price = 1.25055 if direction == "LONG" else 1.24945
                    self.positions = [
                        FakePosition(
                            self.position_ticket,
                            symbol,
                            magic,
                            direction,
                            lot_size,
                            open_price,
                            sl,
                            comment,
                        )
                    ]
                return None

            def get_positions(self, symbol: str, magic: int) -> list[FakePosition]:
                return [
                    position
                    for position in self.positions
                    if position.symbol == symbol and int(position.magic) == int(magic)
                ]

        class SyncFailExecutor(FakeExecutor):
            def get_positions(self, symbol: str, magic: int) -> None:
                del symbol, magic
                return None

        original_log_trade_csv = self.log_trade_csv
        trade_events: list[tuple[str, int, dict[str, Any]]] = []

        def capture_trade(action: str, ticket: int, **kwargs: Any) -> bool:
            trade_events.append((action, ticket, kwargs))
            return True

        self.log_trade_csv = capture_trade  # type: ignore[method-assign]
        try:
            self.state = self.default_state()
            self.start_cycle(1.25000)
            market_tick = {
                "bid": 1.25043,
                "ask": 1.25050,
                "spread_points": 7.0,
                "info": FakeInfo(),
            }
            market_regime = {"entry_allowed": True, "signal_fresh": True}
            reject_order = min(
                [o for o in self.state["virtual_orders"] if o["direction"] == "LONG"],
                key=lambda item: item["entry"],
            )
            reject_executor = FakeExecutor("ERR|10016")
            self.executor = reject_executor
            assert not self.open_from_virtual_order(reject_order, market_tick)
            assert reject_executor.open_calls == 1
            assert len(self.state["virtual_orders"]) == 4
            assert not self.state["sync_block_new_entries"]
            assert self.state["market_open_retry_after_epoch"] is not None
            assert any(event[0] == "ENTRY_FAIL_MARKET" for event in trade_events)
            assert self.fill_virtual_orders(market_tick, market_regime) == 0
            assert reject_executor.open_calls == 1
            self.state["market_open_retry_after_epoch"] = None
            self.state["reconciliation_required"] = {"type": "market_open_result", "reason": "test"}
            self.state["sync_block_new_entries"] = False
            assert self.fill_virtual_orders(market_tick, market_regime) == 0
            assert reject_executor.open_calls == 1
            self.state["reconciliation_required"] = None

            original_get_tick = self.get_tick
            original_get_regime = self.get_regime
            original_sync_live_positions = self.sync_live_positions
            original_save_state = self.save_state
            original_live_trading_enabled = self.params.get("live_trading_enabled")
            original_policy_enabled = self.params.get("policy_enabled")
            try:
                self.state = self.default_state()
                self.state["reconciliation_required"] = {"type": "market_open_result", "reason": "test"}
                self.state["sync_block_new_entries"] = False
                self.params["live_trading_enabled"] = True
                self.params["policy_enabled"] = False
                self.get_tick = lambda: market_tick  # type: ignore[method-assign]
                self.get_regime = lambda: market_regime  # type: ignore[method-assign]
                self.sync_live_positions = lambda tick: True  # type: ignore[method-assign]
                self.save_state = lambda: None  # type: ignore[method-assign]
                self.run_once()
                assert int(self.state["cycle_id"]) == 0
                assert len(self.state["virtual_orders"]) == 0
                assert reject_executor.open_calls == 1
            finally:
                self.get_tick = original_get_tick  # type: ignore[method-assign]
                self.get_regime = original_get_regime  # type: ignore[method-assign]
                self.sync_live_positions = original_sync_live_positions  # type: ignore[method-assign]
                self.save_state = original_save_state  # type: ignore[method-assign]
                self.params["live_trading_enabled"] = original_live_trading_enabled
                self.params["policy_enabled"] = original_policy_enabled

            opposite_position = FakePosition(
                900111,
                self.symbol,
                self.magic,
                "SHORT",
                0.01,
                1.25055,
                float(reject_order["stop_loss"]),
                self.market_order_comment(reject_order),
            )
            assert not self.market_order_matches_position(
                reject_order,
                opposite_position,
                self.market_order_comment(reject_order),
            )

            trade_events.clear()
            self.state = self.default_state()
            self.start_cycle(1.25000)
            timeout_adopt_order = min(
                [o for o in self.state["virtual_orders"] if o["direction"] == "LONG"],
                key=lambda item: item["entry"],
            )
            timeout_adopt_executor = FakeExecutor("ERR|TIMEOUT", create_position_after_error=True)
            self.executor = timeout_adopt_executor
            assert self.open_from_virtual_order(timeout_adopt_order, market_tick)
            assert len(self.state["positions"]) == 1
            assert len(self.state["virtual_orders"]) == 3
            assert not self.state["sync_block_new_entries"]
            assert self.state["reconciliation_required"] is None

            trade_events.clear()
            self.state = self.default_state()
            self.start_cycle(1.25000)
            sync_fail_order = min(
                [o for o in self.state["virtual_orders"] if o["direction"] == "LONG"],
                key=lambda item: item["entry"],
            )
            sync_fail_executor = SyncFailExecutor("ERR|10016")
            self.executor = sync_fail_executor
            assert not self.open_from_virtual_order(sync_fail_order, market_tick)
            assert self.state["sync_block_new_entries"]
            assert isinstance(self.state["reconciliation_required"], dict)
            assert "position sync failed" in str(self.state["reconciliation_required"].get("reason"))
            assert not any(event[0] == "ENTRY_FAIL_MARKET" for event in trade_events)
            assert any(event[0] == "ENTRY_UNRESOLVED_MARKET" for event in trade_events)

            trade_events.clear()
            self.state = self.default_state()
            self.start_cycle(1.25000)
            untracked_order = min(
                [o for o in self.state["virtual_orders"] if o["direction"] == "LONG"],
                key=lambda item: item["entry"],
            )
            untracked_executor = FakeExecutor("ERR|10016")
            untracked_executor.positions = [
                FakePosition(
                    900333,
                    self.symbol,
                    self.magic,
                    "LONG",
                    0.01,
                    1.25055,
                    float(untracked_order["stop_loss"]),
                    f"{self.sanitized_comment_prefix()}_{int(self.state['cycle_id'])}_999",
                )
            ]
            self.executor = untracked_executor
            assert self.open_from_virtual_order(untracked_order, market_tick)
            assert len(self.state["positions"]) == 1
            assert len(self.state["virtual_orders"]) == 0
            assert not self.state["sync_block_new_entries"]
            assert self.state["reconciliation_required"] is None
            assert self.state["market_open_retry_after_epoch"] is None
            assert not any(event[0] == "ENTRY_FAIL_MARKET" for event in trade_events)
            assert any(event[0] == "ENTRY_RECOVERED_SYNC" for event in trade_events)

            trade_events.clear()
            self.state = self.default_state()
            self.start_cycle(1.25000)
            unrecoverable_order = min(
                [o for o in self.state["virtual_orders"] if o["direction"] == "LONG"],
                key=lambda item: item["entry"],
            )
            unrecoverable_executor = FakeExecutor("ERR|10016")
            unrecoverable_executor.positions = [
                FakePosition(
                    900334,
                    self.symbol,
                    self.magic,
                    "LONG",
                    0.01,
                    1.25055,
                    float(unrecoverable_order["stop_loss"]),
                    "manual_or_other_bot",
                )
            ]
            self.executor = unrecoverable_executor
            assert not self.open_from_virtual_order(unrecoverable_order, market_tick)
            assert self.state["sync_block_new_entries"]
            assert isinstance(self.state["reconciliation_required"], dict)
            assert "unrecoverable untracked live positions" in str(self.state["reconciliation_required"].get("reason"))
            assert not any(event[0] == "ENTRY_FAIL_MARKET" for event in trade_events)
            assert any(event[0] == "ENTRY_UNRESOLVED_MARKET" for event in trade_events)

            trade_events.clear()
            self.state = self.default_state()
            self.start_cycle(1.25000)
            timeout_unresolved_order = min(
                [o for o in self.state["virtual_orders"] if o["direction"] == "LONG"],
                key=lambda item: item["entry"],
            )
            timeout_unresolved_executor = FakeExecutor("ERR|TIMEOUT")
            self.executor = timeout_unresolved_executor
            assert not self.open_from_virtual_order(timeout_unresolved_order, market_tick)
            assert len(self.state["virtual_orders"]) == 4
            assert self.state["sync_block_new_entries"]
            assert isinstance(self.state["reconciliation_required"], dict)
            assert any(event[0] == "ENTRY_UNRESOLVED_MARKET" for event in trade_events)
            delayed_comment = self.market_order_comment(timeout_unresolved_order)
            timeout_unresolved_executor.positions = [
                FakePosition(
                    900777,
                    self.symbol,
                    self.magic,
                    "LONG",
                    0.01,
                    1.25055,
                    float(timeout_unresolved_order["stop_loss"]),
                    delayed_comment,
                )
            ]
            assert self.sync_live_positions(market_tick)
            assert len(self.state["positions"]) == 1
            assert len(self.state["virtual_orders"]) == 3
            assert not self.state["sync_block_new_entries"]
            assert self.state["reconciliation_required"] is None
        finally:
            self.log_trade_csv = original_log_trade_csv  # type: ignore[method-assign]
            self.executor = None
            self._suppress_manual_alerts = original_suppress_manual_alerts
        logging.info("s18 self-test passed")


class S18V2BasketRunner:
    def __init__(self, raw_params: dict[str, Any] | None = None) -> None:
        self.raw_params = raw_params or load_params()
        self.policy = EventFilterPolicy(self.raw_params) if bool(self.raw_params.get("policy_enabled", True)) else None
        self.bots = [
            S18SnowballBot(profile_params, policy=self.policy)
            for profile_params in build_profile_params(self.raw_params)
        ]

    def self_test(self, include_policy: bool = False) -> None:
        for bot in self.bots:
            bot.self_test()
        if include_policy and self.policy is not None:
            self.policy.self_test()
        logging.info("s18 basket self-test passed symbols=%s", [bot.symbol for bot in self.bots])

    def run_forever(self) -> None:
        if not bool(self.raw_params.get("enabled", True)):
            logging.warning("s18 v2 basket is disabled by params enabled=false")
            return
        live_trading_enabled = bool(self.raw_params.get("live_trading_enabled", False))
        shadow_forward_enabled = bool(self.raw_params.get("shadow_forward_enabled", False))
        if not live_trading_enabled and not shadow_forward_enabled:
            logging.warning("s18 basket live_trading_enabled=false and shadow_forward_enabled=false; idle loop only")
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
        if not live_trading_enabled:
            logging.warning("S18 basket shadow forward mode: policy decisions logged, no orders")
        logging.info(
            "S18 basket started: symbols=%s live_trading_enabled=%s shadow_forward_enabled=%s",
            [bot.symbol for bot in self.bots],
            live_trading_enabled,
            shadow_forward_enabled,
        )
        sleep_seconds = min(float(bot.params["poll_interval_seconds"]) for bot in self.bots)
        while True:
            for bot in self.bots:
                try:
                    bot.run_once()
                except KeyboardInterrupt:
                    raise
                except Exception:
                    logging.exception("Unhandled s18 v2 basket loop error for %s", bot.symbol)
            time.sleep(sleep_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="S18 basket Snowball event-filter live/shadow bot")
    parser.add_argument("--self-test", action="store_true", help="run pure logic checks without bridge connection")
    parser.add_argument("--policy-self-test", action="store_true", help="also load frozen ML artifacts and test predict")
    args = parser.parse_args()
    configure_logging()
    raw_params = load_params()
    if args.self_test and not args.policy_self_test:
        raw_params = raw_params.copy()
        raw_params["policy_enabled"] = False
    runner = S18V2BasketRunner(raw_params)
    if args.self_test:
        runner.self_test(include_policy=bool(args.policy_self_test))
        return 0
    runner.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
