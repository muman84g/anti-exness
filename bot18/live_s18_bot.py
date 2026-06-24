# -*- coding: utf-8 -*-
"""S18 GBPUSD Snowball anti-grid live bot.

This bot uses the copied EA bridge/executor stack and implements the fixed
GBPUSD policy saved from backtest24:
- virtual entries with spread gate
- H1 trend-gated cycle start
- inactive gate_false add-distance throttling
- weekend hold with Monday re-anchor
"""

from __future__ import annotations

import argparse
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

JST = timezone(timedelta(hours=9), "JST")
LONG = 1
SHORT = -1
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1

LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "s18_bot.log")
STATE_DIR = os.path.join(SCRIPT_DIR, "state")
STATE_FILE = os.path.join(STATE_DIR, "s18_bot_state.json")
PARAMS_FILE = os.path.join(SCRIPT_DIR, "s18_params.json")

DEFAULT_PARAMS: dict[str, Any] = {
    "enabled": True,
    "live_trading_enabled": False,
    "symbol": "GBPUSD",
    "magic": 180018,
    "comment_prefix": "s18_snowball",
    "lot": 0.01,
    "distance_pips": 5.0,
    "auto_tp_levels": 1,
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


def atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=True, sort_keys=True)
        handle.write("\n")
    os.replace(tmp_path, path)


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


class S18SnowballBot:
    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = DEFAULT_PARAMS.copy()
        self.params.update(params or load_params())
        self.symbol = str(self.params["symbol"])
        self.magic = int(self.params["magic"])
        self.dm = None
        self.executor = None
        self.state = self.load_state()
        self.last_regime_fetch_epoch = 0.0
        self.cached_regime = self.default_regime()
        self.last_status_log_epoch = 0.0
        self.sync_closed_count = 0

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
            "trend_direction": 0,
            "reason": "not_loaded",
            "signal_time": None,
        }

    def default_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "strategy": "s18_snowball_antigrid_inactiveadd23p5_gatefalse",
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
            "last_regime": self.default_regime(),
            "updated_at_jst": jst_now().isoformat(),
        }

    def load_state(self) -> dict[str, Any]:
        if not os.path.exists(STATE_FILE):
            return self.default_state()
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        if state.get("symbol") != self.symbol or int(state.get("magic", 0)) != self.magic:
            raise ValueError("State symbol/magic does not match s18_params.json")
        default = self.default_state()
        default.update(state)
        return default

    def save_state(self) -> None:
        self.recalculate_position_counts()
        self.state["updated_at_jst"] = jst_now().isoformat()
        atomic_write_json(STATE_FILE, self.state)

    def recalculate_position_counts(self) -> None:
        long_positions = [p for p in self.state["positions"] if p["direction"] == "LONG"]
        short_positions = [p for p in self.state["positions"] if p["direction"] == "SHORT"]
        self.state["long_count"] = len(long_positions)
        self.state["short_count"] = len(short_positions)
        self.state["long_entry_sum"] = sum(float(p["entry"]) for p in long_positions)
        self.state["short_entry_sum"] = sum(float(p["entry"]) for p in short_positions)

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
        logging.info(f"Started s18 cycle {self.state['cycle_id']} anchor={self.state['grid_anchor']}")

    def block_new_entries(self, reason: str) -> None:
        self.state["sync_block_new_entries"] = True
        self.state["sync_block_reason"] = reason
        logging.error(f"New entries blocked: {reason}")

    def clear_virtual_orders(self) -> None:
        self.state["virtual_orders"] = []

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
        if extra_tickets:
            self.block_new_entries(f"untracked live positions exist: {extra_tickets}")
            return False

        for ticket, position in list(state_by_ticket.items()):
            live_position = live_by_ticket.get(ticket)
            if live_position is None:
                if bool(self.params["assume_missing_state_position_is_sl"]):
                    exit_price = float(position["stop_loss"])
                    pnl = self.estimate_position_pnl(position, exit_price, tick["info"])
                    self.state["cycle_realized_usd"] = float(self.state["cycle_realized_usd"]) + pnl
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
                self.remove_position_by_ticket(ticket)
                pnl = float(getattr(result, "profit", 0.0) or 0.0)
                self.state["cycle_realized_usd"] = float(self.state["cycle_realized_usd"]) + pnl
                logging.info(f"SL close ticket={ticket} pnl={pnl:.2f}")
                closed += 1
            else:
                self.block_new_entries(f"SL close failed for ticket {ticket}: {getattr(result, 'status', 'UNKNOWN')}")
                break
        self.recalculate_position_counts()
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
        logging.info(
            f"Opened {direction_name(direction)} ticket={int(ticket)} entry={self.price(entry)} sl={self.price(float(order['stop_loss']))}"
        )
        return True

    def manage_orders_and_grid(self, bid: float, ask: float) -> None:
        if self.state["grid_anchor"] is None:
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

    def complete_cycle(self, bid: float, ask: float) -> bool:
        failures = []
        for position in list(self.state["positions"]):
            ticket = int(position["ticket"])
            result = self.executor.close_position(ticket, deviation=int(self.params["deviation_points"]))
            if result:
                self.remove_position_by_ticket(ticket)
                self.state["cycle_realized_usd"] = float(self.state["cycle_realized_usd"]) + float(getattr(result, "profit", 0.0) or 0.0)
            else:
                failures.append((ticket, getattr(result, "status", "UNKNOWN")))
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
                self.complete_cycle(tick["bid"], tick["ask"])

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
            self.start_cycle(tick["bid"])

        stop_count = self.process_stops(tick["bid"], tick["ask"], tick["info"])
        self.fill_virtual_orders(tick, regime)
        immediate_stops = self.process_stops(tick["bid"], tick["ask"], tick["info"])
        stop_count += immediate_stops
        self.manage_orders_and_grid(tick["bid"], tick["ask"])
        if self.state["positions"] and (stop_count or self.auto_tp_refresh_due() or self.state.get("auto_tp_price") is None):
            self.refresh_auto_tp(tick["bid"], tick["ask"], tick["info"])
        if self.state["positions"] and self.auto_tp_crossed(tick["bid"], tick["ask"]):
            self.complete_cycle(tick["bid"], tick["ask"])
        self.log_status(tick, regime)
        self.save_state()

    def log_status(self, tick: dict[str, Any], regime: dict[str, Any]) -> None:
        now = time.time()
        if now - self.last_status_log_epoch < float(self.params["status_log_interval_seconds"]):
            return
        self.last_status_log_epoch = now
        logging.info(
            "S18 status: "
            f"bid={tick['bid']:.5f} ask={tick['ask']:.5f} spread_points={tick['spread_points']:.1f} "
            f"cycle={self.state['cycle_id']} pos={len(self.state['positions'])} orders={len(self.state['virtual_orders'])} "
            f"auto_tp={self.state.get('auto_tp_price')} regime_allowed={regime.get('entry_allowed')} "
            f"fresh={regime.get('signal_fresh')} block={self.state.get('sync_block_new_entries')}"
        )

    def run_forever(self) -> None:
        if not bool(self.params.get("enabled", True)):
            logging.warning("s18 is disabled by params enabled=false")
            return
        if not bool(self.params.get("live_trading_enabled", False)):
            logging.warning("s18 live_trading_enabled=false; idle loop only, no bridge connection and no orders")
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
                logging.exception("Unhandled s18 loop error")
            time.sleep(float(self.params["poll_interval_seconds"]))

    def self_test(self) -> None:
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
        logging.info("s18 self-test passed")


def main() -> int:
    parser = argparse.ArgumentParser(description="S18 GBPUSD Snowball anti-grid live bot")
    parser.add_argument("--self-test", action="store_true", help="run pure logic checks without bridge connection")
    args = parser.parse_args()
    configure_logging()
    bot = S18SnowballBot()
    if args.self_test:
        bot.self_test()
        return 0
    bot.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
