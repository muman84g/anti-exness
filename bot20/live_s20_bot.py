# -*- coding: utf-8 -*-
"""S20 XAUUSD H1 large-candle short basket live/shadow runner.

Backtest source:
backtest43_gold_regime_crossasset_ideas / large_candle_short_basket_m1 /
confirm_refine_top_45_04 candidate.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from live_data_fetcher import MT5DataManager
from live_executor import MT5Executor, ORDER_TYPE_SELL, REQUIRED_S20_COMMANDS, S20_BRIDGE_NAME

JST = timezone(timedelta(hours=9), "JST")

LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
STATE_DIR = os.path.join(SCRIPT_DIR, "state")
LOG_FILE = os.path.join(LOG_DIR, "s20_bot.log")
TRADE_LOG_FILE = os.path.join(LOG_DIR, "s20_trades.csv")
TRADE_ERROR_FILE = os.path.join(LOG_DIR, "s20_trade_errors.csv")
STATE_FILE = os.path.join(STATE_DIR, "s20_bot_state.json")
PARAMS_FILE = os.path.join(SCRIPT_DIR, "s20_params.json")

DEFAULT_PARAMS: dict[str, Any] = {
    "enabled": True,
    "live_trading_enabled": False,
    "shadow_forward_enabled": True,
    "symbol": "XAUUSD",
    "magic": 200020,
    "comment_prefix": "s20_xau",
    "strategy_id": "bot20_xau_h1_large_candle_short_confirm_refine_top_45_04",
    "lot": 0.01,
    "max_positions": 10,
    "max_entry_spread_points": 250.0,
    "deviation_points": 50,
    "price_digits": 3,
    "point_size": 0.001,
    "h1_timeframe": 16385,
    "h1_bars": 240,
    "m1_timeframe": 1,
    "m1_bars": 420,
    "drop_latest_h1_bar": True,
    "drop_latest_m1_bar_for_atr": True,
    "h1_atr_period": 14,
    "m1_atr_period": 30,
    "h1_body_atr_mult": 2.0,
    "h1_close_lower_frac": 0.35,
    "signal_session_hours_utc": [13, 14, 15, 16],
    "max_signal_delay_minutes": 4,
    "entry_confirm_window_minutes": 45,
    "entry_confirm_atr_mult": 0.4,
    "stop_atr_mult": 1.4,
    "add_atr_mult": 0.8,
    "entry_interval_minutes": 5,
    "active_window_minutes": 240,
    "close_after_minutes": 240,
    "basket_dd_r": 3.0,
    "bar_refresh_seconds": 20,
    "poll_interval_seconds": 1.0,
    "status_log_interval_seconds": 60,
    "weekend_hold": True,
    "force_weekend_flat": False,
    "weekend_entry_stop_weekday_jst": 5,
    "weekend_entry_stop_hour_jst": 2,
    "weekend_entry_stop_minute_jst": 0,
    "monday_start_hour_jst": 8,
    "monday_start_minute_jst": 0,
}


def setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def dt_text(dt: datetime | pd.Timestamp | None) -> str:
    if dt is None:
        return ""
    if isinstance(dt, pd.Timestamp):
        return dt.isoformat()
    return dt.astimezone(timezone.utc).isoformat()


def bar_timestamp(value: Any) -> pd.Timestamp | None:
    if value is None:
        return None
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return None
    if pd.isna(ts):
        return None
    if ts.tzinfo is not None:
        return ts.tz_convert("UTC").tz_localize(None)
    return ts


def deep_update(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_update(out[key], value)
        else:
            out[key] = value
    return out


def load_params() -> dict[str, Any]:
    params = dict(DEFAULT_PARAMS)
    if os.path.exists(PARAMS_FILE):
        with open(PARAMS_FILE, "r", encoding="utf-8") as f:
            params = deep_update(params, json.load(f))
    return params


def atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    bak_path = f"{path}.bak"
    if os.path.exists(path):
        try:
            shutil.copy2(path, bak_path)
        except OSError as exc:
            raise RuntimeError(f"Could not backup state file {path}: {exc}") from exc
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


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
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(int(period), min_periods=int(period)).mean()


def normalize_price(price: float, digits: int) -> float:
    return round(float(price), int(digits))


class S20GoldBasketRunner:
    def __init__(self, params: dict[str, Any]):
        self.params = params
        self.symbol = str(params["symbol"])
        self.magic = int(params["magic"])
        self.live_enabled = bool(params.get("live_trading_enabled", False))
        self.shadow_enabled = bool(params.get("shadow_forward_enabled", True))
        self.dm = MT5DataManager()
        self.executor = MT5Executor(self.dm)
        self.state = self._load_state()
        self._bar_cache: dict[str, tuple[float, pd.DataFrame | None]] = {}
        self._last_status_log = 0.0

    def _default_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "strategy_id": self.params["strategy_id"],
            "symbol": self.symbol,
            "magic": self.magic,
            "basket": None,
            "positions": [],
            "last_signal_h1_time": None,
            "pending_entry_signal": None,
            "sync_block_new_entries": False,
            "sync_block_reason": None,
            "reconciliation_required": False,
            "shadow_ticket_seq": -200020000,
            "updated_at": dt_text(utc_now()),
        }

    def _load_state(self) -> dict[str, Any]:
        if not os.path.exists(STATE_FILE):
            return self._default_state()
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            base = self._default_state()
            base.update(state)
            return base
        except Exception as exc:
            raise RuntimeError(f"Could not load state file {STATE_FILE}: {exc}") from exc

    def _save_state(self) -> None:
        self.state["updated_at"] = dt_text(utc_now())
        atomic_write_json(STATE_FILE, self.state)

    def _trade_row(self, event: str, **kwargs: Any) -> None:
        fieldnames = [
            "timestamp_utc",
            "event",
            "strategy_id",
            "symbol",
            "ticket",
            "side",
            "lot",
            "price",
            "sl",
            "profit",
            "reason",
            "basket_id",
            "add_index",
            "live",
            "note",
        ]
        row = {
            "timestamp_utc": dt_text(utc_now()),
            "event": event,
            "strategy_id": self.params["strategy_id"],
            "symbol": self.symbol,
            "live": self.live_enabled,
        }
        row.update(kwargs)
        append_csv(TRADE_LOG_FILE, row, fieldnames)

    def _error_row(self, event: str, reason: str, note: str = "") -> None:
        append_csv(
            TRADE_ERROR_FILE,
            {
                "timestamp_utc": dt_text(utc_now()),
                "event": event,
                "strategy_id": self.params["strategy_id"],
                "symbol": self.symbol,
                "reason": reason,
                "note": note,
            },
            ["timestamp_utc", "event", "strategy_id", "symbol", "reason", "note"],
        )

    def connect_and_preflight(self) -> bool:
        if not self.dm.connect():
            logging.critical("S20 failed to connect to EA bridge.")
            return False

        caps = self.executor.get_bridge_capabilities()
        if not caps:
            return False
        missing = sorted(REQUIRED_S20_COMMANDS - set(caps["commands"]))
        if caps["name"] != S20_BRIDGE_NAME:
            logging.critical("Bridge name mismatch: expected=%s got=%s", S20_BRIDGE_NAME, caps["name"])
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

        info = self.executor.get_symbol_info(self.symbol)
        if info is None:
            logging.critical("S20 symbol INFO preflight failed for %s", self.symbol)
            return False
        positions = self.executor.get_positions(self.symbol, self.magic)
        if positions is None:
            logging.critical("S20 POSITIONS preflight failed for %s", self.symbol)
            return False
        if self._get_bars("h1", int(self.params["h1_timeframe"]), int(self.params["h1_bars"])) is None:
            logging.critical("S20 H1 HIST preflight failed for %s", self.symbol)
            return False
        if self._get_bars("m1", int(self.params["m1_timeframe"]), int(self.params["m1_bars"])) is None:
            logging.critical("S20 M1 HIST preflight failed for %s", self.symbol)
            return False

        logging.info(
            "S20 gold runner started: symbol=%s live_trading_enabled=%s shadow_forward_enabled=%s",
            self.symbol,
            self.live_enabled,
            self.shadow_enabled,
        )
        return True

    def _get_bars(self, key: str, timeframe: int, bars: int) -> pd.DataFrame | None:
        now_monotonic = time.monotonic()
        refresh = float(self.params.get("bar_refresh_seconds", 20))
        cached = self._bar_cache.get(key)
        if cached and now_monotonic - cached[0] < refresh:
            return cached[1]
        df = self.dm.get_historical_data(self.symbol, timeframe, bars)
        if df is None or df.empty:
            self._bar_cache[key] = (now_monotonic, None)
            return None
        df = df.sort_index()
        self._bar_cache[key] = (now_monotonic, df)
        return df

    def _weekend_entry_blocked(self) -> bool:
        now_jst = datetime.now(JST)
        stop_weekday = int(self.params.get("weekend_entry_stop_weekday_jst", 5))
        stop_hour = int(self.params.get("weekend_entry_stop_hour_jst", 2))
        stop_minute = int(self.params.get("weekend_entry_stop_minute_jst", 0))
        monday_hour = int(self.params.get("monday_start_hour_jst", 8))
        monday_minute = int(self.params.get("monday_start_minute_jst", 0))
        if now_jst.weekday() == stop_weekday and now_jst.time() >= datetime(
            now_jst.year, now_jst.month, now_jst.day, stop_hour, stop_minute, tzinfo=JST
        ).time():
            return True
        if now_jst.weekday() == 6:
            return True
        if now_jst.weekday() == 0 and now_jst.time() < datetime(
            now_jst.year, now_jst.month, now_jst.day, monday_hour, monday_minute, tzinfo=JST
        ).time():
            return True
        return False

    def _current_spread_points(self, info: Any) -> float:
        point = float(getattr(info, "point", self.params.get("point_size", 0.001)) or 0.001)
        return (float(info.ask) - float(info.bid)) / point

    def _price_unit_value(self, info: Any) -> float:
        value = float(getattr(info, "price_unit_value", 0.0) or 0.0)
        if value > 0:
            return value
        contract = float(getattr(info, "contract_size", 0.0) or 0.0)
        return contract if contract > 0 else 100.0

    def _risk_usd(self, info: Any, entry_price: float, sl: float, lot: float) -> float:
        return abs(float(sl) - float(entry_price)) * self._price_unit_value(info) * float(lot)

    def _shadow_floating_pnl(self, info: Any) -> float:
        unit = self._price_unit_value(info)
        close_price = float(info.ask)
        total = 0.0
        for pos in self.state.get("positions", []):
            total += (float(pos["entry_price"]) - close_price) * unit * float(pos["lot"])
        return total

    def _set_sync_block(self, reason: str | None) -> None:
        previous = self.state.get("sync_block_reason")
        self.state["sync_block_new_entries"] = bool(reason)
        self.state["sync_block_reason"] = reason
        if reason and previous != reason:
            logging.error("New entries blocked: %s", reason)
        elif not reason and previous:
            logging.warning("New-entry block cleared after recovery: %s", previous)

    def _sync_positions(self, info: Any) -> tuple[list[Any], float]:
        if not self.live_enabled:
            self._set_sync_block(None)
            return [], self._shadow_floating_pnl(info)

        live_positions = self.executor.get_positions(self.symbol, self.magic)
        if live_positions is None:
            self._set_sync_block("position sync failed")
            self._save_state()
            return [], 0.0

        live_tickets = {int(pos.ticket) for pos in live_positions}
        tracked_tickets = {int(pos["ticket"]) for pos in self.state.get("positions", [])}

        if not live_positions and tracked_tickets:
            logging.warning("All tracked S20 positions are absent on MT5; resetting local basket state.")
            self.state["positions"] = []
            self.state["basket"] = None
            self.state["reconciliation_required"] = False
            self._set_sync_block(None)
            self._save_state()
            return [], 0.0

        if live_tickets != tracked_tickets:
            if live_positions:
                logging.error(
                    "S20 position/state mismatch; reconstructing from MT5 and blocking new entries. "
                    "live=%s tracked=%s",
                    sorted(live_tickets),
                    sorted(tracked_tickets),
                )
                reconstructed = []
                for idx, pos in enumerate(sorted(live_positions, key=lambda item: int(item.open_time))):
                    risk = self._risk_usd(info, pos.open_price, pos.sl or pos.open_price, pos.volume)
                    reconstructed.append(
                        {
                            "ticket": int(pos.ticket),
                            "side": "SHORT",
                            "lot": float(pos.volume),
                            "entry_price": float(pos.open_price),
                            "sl": float(pos.sl),
                            "risk_usd": max(0.0, risk),
                            "open_time": datetime.fromtimestamp(int(pos.open_time), tz=timezone.utc).isoformat(),
                            "add_index": idx,
                            "comment": str(pos.comment),
                        }
                    )
                first_time = min(parse_dt(pos["open_time"]) for pos in reconstructed if parse_dt(pos["open_time"]))
                self.state["positions"] = reconstructed
                self.state["basket"] = {
                    "id": self.state.get("basket", {}).get("id") if self.state.get("basket") else f"s20_recon_{int(time.time())}",
                    "first_entry_time": dt_text(first_time),
                    "last_add_time": reconstructed[-1]["open_time"],
                    "last_add_price": reconstructed[-1]["entry_price"],
                    "risk_sum_usd": sum(float(pos["risk_usd"]) for pos in reconstructed),
                    "max_floating_pnl": 0.0,
                    "signal_h1_time": None,
                }
                self.state["reconciliation_required"] = True
                self._set_sync_block("reconciliation_required")
                self._save_state()
            else:
                self._set_sync_block(None)
        elif self.state.get("sync_block_reason") == "position sync failed":
            self._set_sync_block(None)
            self._save_state()

        floating = sum(float(pos.profit) for pos in live_positions)
        if self.state.get("basket"):
            self.state["basket"]["max_floating_pnl"] = max(
                float(self.state["basket"].get("max_floating_pnl", 0.0)),
                floating,
            )
        return live_positions, floating

    def _closed_m1(self, m1_raw: pd.DataFrame | None) -> pd.DataFrame | None:
        if m1_raw is None or m1_raw.empty:
            return None
        m1 = m1_raw.copy()
        m1 = m1.sort_index()
        try:
            idx = pd.DatetimeIndex(m1.index)
            if idx.tz is not None:
                idx = idx.tz_convert("UTC").tz_localize(None)
            m1.index = idx
        except Exception:
            pass
        if bool(self.params.get("drop_latest_m1_bar_for_atr", True)) and len(m1) > 1:
            m1 = m1.iloc[:-1]
        if m1.empty:
            return None
        return m1

    def _m1_atr(self, m1_raw: pd.DataFrame | None) -> float | None:
        if m1_raw is None or len(m1_raw) < int(self.params["m1_atr_period"]) + 2:
            return None
        m1 = self._closed_m1(m1_raw)
        if m1 is None or len(m1) < int(self.params["m1_atr_period"]):
            return None
        atr = true_range_atr(m1, int(self.params["m1_atr_period"])).dropna()
        if atr.empty or not math.isfinite(float(atr.iloc[-1])):
            return None
        return float(atr.iloc[-1])

    def _entry_confirm_enabled(self) -> bool:
        return (
            float(self.params.get("entry_confirm_window_minutes", 0.0)) > 0.0
            and float(self.params.get("entry_confirm_atr_mult", 0.0)) > 0.0
        )

    def _build_pending_entry(self, signal: dict[str, Any], m1_raw: pd.DataFrame | None) -> dict[str, Any] | None:
        if m1_raw is None or m1_raw.empty:
            return None
        h1_time = bar_timestamp(signal.get("h1_time"))
        if h1_time is None:
            return None
        expected_entry_time = h1_time + pd.Timedelta(hours=1)
        m1 = m1_raw.copy().sort_index()
        try:
            idx = pd.DatetimeIndex(m1.index)
            if idx.tz is not None:
                idx = idx.tz_convert("UTC").tz_localize(None)
            m1.index = idx
        except Exception:
            pass
        candidates = m1.loc[m1.index >= expected_entry_time]
        if candidates.empty:
            return None
        reference_time = candidates.index[0]
        reference_open = float(candidates.iloc[0]["Open"])
        if not math.isfinite(reference_open):
            return None
        window_minutes = int(float(self.params.get("entry_confirm_window_minutes", 0)))
        expires_at = reference_time + pd.Timedelta(minutes=window_minutes)
        return {
            "h1_time": str(signal["h1_time"]),
            "expected_entry_time": str(expected_entry_time),
            "reference_bar_time": str(reference_time),
            "reference_open": reference_open,
            "expires_at": str(expires_at),
            "h1_atr": float(signal.get("h1_atr", 0.0)),
            "body": float(signal.get("body", 0.0)),
            "close_lower_frac": float(signal.get("close_lower_frac", 0.0)),
        }

    def _pending_entry_expired(self, pending: dict[str, Any], m1_raw: pd.DataFrame | None) -> bool:
        expires_at = bar_timestamp(pending.get("expires_at"))
        if expires_at is None:
            return True
        if m1_raw is None or m1_raw.empty:
            return False
        latest = bar_timestamp(m1_raw.index[-1])
        if latest is None:
            return False
        return latest > expires_at

    def _entry_confirm_signal(self, pending: dict[str, Any], m1_raw: pd.DataFrame | None) -> dict[str, Any] | None:
        if not self._entry_confirm_enabled():
            return None
        m1 = self._closed_m1(m1_raw)
        if m1 is None or len(m1) < int(self.params["m1_atr_period"]):
            return None
        reference_time = bar_timestamp(pending.get("reference_bar_time") or pending.get("expected_entry_time"))
        if reference_time is None:
            return None
        reference_open = float(pending.get("reference_open", float("nan")))
        if not math.isfinite(reference_open):
            return None
        window_minutes = int(float(self.params.get("entry_confirm_window_minutes", 0)))
        last_confirm_time = reference_time + pd.Timedelta(minutes=max(0, window_minutes - 1))
        atr_series = true_range_atr(m1, int(self.params["m1_atr_period"]))
        check = m1.loc[(m1.index >= reference_time) & (m1.index <= last_confirm_time)]
        if check.empty:
            return None
        point = float(self.params.get("point_size", 0.001))
        confirm_mult = float(self.params.get("entry_confirm_atr_mult", 0.0))
        for bar_time, row in check.iterrows():
            atr_value = float(atr_series.loc[bar_time])
            if not math.isfinite(atr_value) or atr_value <= 0:
                continue
            atr_floor = max(atr_value, point)
            threshold = reference_open - confirm_mult * atr_floor
            if float(row["Close"]) <= threshold:
                return {
                    "h1_time": pending.get("h1_time"),
                    "entry_bar_time": bar_time + pd.Timedelta(minutes=1),
                    "confirm_bar_time": bar_time,
                    "reference_open": reference_open,
                    "entry_confirm_threshold": threshold,
                    "entry_confirm_atr": atr_floor,
                    "h1_atr": float(pending.get("h1_atr", 0.0)),
                    "body": float(pending.get("body", 0.0)),
                    "close_lower_frac": float(pending.get("close_lower_frac", 0.0)),
                }
        return None

    def _signal(self, h1_raw: pd.DataFrame | None, m1_raw: pd.DataFrame | None) -> dict[str, Any] | None:
        if h1_raw is None or m1_raw is None:
            return None
        min_h1 = int(self.params["h1_atr_period"]) + 3
        if len(h1_raw) < min_h1 or m1_raw.empty:
            return None
        h1 = h1_raw.copy()
        if bool(self.params.get("drop_latest_h1_bar", True)) and len(h1) > 1:
            h1 = h1.iloc[:-1]
        if len(h1) < min_h1:
            return None

        h1_atr = true_range_atr(h1, int(self.params["h1_atr_period"]))
        bar = h1.iloc[-1]
        h1_time = h1.index[-1]
        atr_value = float(h1_atr.iloc[-1])
        if not math.isfinite(atr_value) or atr_value <= 0:
            return None

        body = float(bar["Open"]) - float(bar["Close"])
        candle_range = float(bar["High"]) - float(bar["Low"])
        if candle_range <= 0:
            return None
        close_lower_frac = (float(bar["Close"]) - float(bar["Low"])) / candle_range
        if body <= 0:
            return None
        if body < float(self.params["h1_body_atr_mult"]) * atr_value:
            return None
        if close_lower_frac > float(self.params["h1_close_lower_frac"]):
            return None

        current_bar_time = m1_raw.index[-1]
        expected_entry_time = h1_time + pd.Timedelta(hours=1)
        delay = current_bar_time - expected_entry_time
        if delay < pd.Timedelta(0):
            return None
        if delay > pd.Timedelta(minutes=float(self.params["max_signal_delay_minutes"])):
            return None

        allowed_hours = {int(hour) for hour in self.params["signal_session_hours_utc"]}
        if int(current_bar_time.hour) not in allowed_hours:
            return None

        h1_text = str(h1_time)
        if self.state.get("last_signal_h1_time") == h1_text:
            return None

        return {
            "h1_time": h1_time,
            "entry_bar_time": current_bar_time,
            "h1_atr": atr_value,
            "body": body,
            "close_lower_frac": close_lower_frac,
        }

    def _new_basket(self, signal: dict[str, Any], entry_price: float, risk_usd: float) -> dict[str, Any]:
        return {
            "id": f"s20_{int(time.time())}",
            "first_entry_time": dt_text(utc_now()),
            "last_add_time": dt_text(utc_now()),
            "last_add_price": float(entry_price),
            "risk_sum_usd": float(risk_usd),
            "max_floating_pnl": 0.0,
            "signal_h1_time": str(signal["h1_time"]),
            "signal_entry_bar_time": str(signal["entry_bar_time"]),
        }

    def _next_shadow_ticket(self) -> int:
        ticket = int(self.state.get("shadow_ticket_seq", -200020000)) - 1
        self.state["shadow_ticket_seq"] = ticket
        return ticket

    def _open_short(self, info: Any, m1_atr: float, signal: dict[str, Any] | None, reason: str) -> bool:
        spread_points = self._current_spread_points(info)
        if spread_points > float(self.params["max_entry_spread_points"]):
            logging.info(
                "S20 entry skipped by spread: spread_points=%.1f max=%.1f",
                spread_points,
                float(self.params["max_entry_spread_points"]),
            )
            return False

        lot = float(self.params["lot"])
        digits = int(getattr(info, "digits", self.params.get("price_digits", 3)) or self.params.get("price_digits", 3))
        point = float(getattr(info, "point", self.params.get("point_size", 0.001)) or self.params.get("point_size", 0.001))
        entry_price = float(info.bid)
        stop_distance = float(self.params["stop_atr_mult"]) * float(m1_atr)
        min_sl = float(info.ask) + (int(getattr(info, "stops_level", 0) or 0) + 2) * point
        sl = normalize_price(max(entry_price + stop_distance, min_sl), digits)
        risk = self._risk_usd(info, entry_price, sl, lot)
        add_index = len(self.state.get("positions", []))
        comment = f"{self.params['comment_prefix']}_{add_index:02d}"

        if self.live_enabled:
            ticket = self.executor.open_position(
                self.symbol,
                ORDER_TYPE_SELL,
                lot,
                sl=sl,
                tp=0.0,
                deviation=int(self.params["deviation_points"]),
                magic=self.magic,
                comment=comment,
                digits=digits,
            )
            if ticket is None:
                last_error = getattr(self.executor, "last_order_error", "OPEN_FAILED")
                self._error_row("OPEN_FAILED", str(last_error), reason)
                return False
            ticket_id = int(ticket)
            if float(getattr(ticket, "price", 0.0) or 0.0) > 0:
                entry_price = float(ticket.price)
                risk = self._risk_usd(info, entry_price, sl, lot)
        else:
            if not self.shadow_enabled:
                logging.info("S20 signal skipped because live and shadow are both disabled.")
                return False
            ticket_id = self._next_shadow_ticket()

        if not self.state.get("basket"):
            self.state["basket"] = self._new_basket(signal or {}, entry_price, risk)
        else:
            self.state["basket"]["last_add_time"] = dt_text(utc_now())
            self.state["basket"]["last_add_price"] = float(entry_price)
            self.state["basket"]["risk_sum_usd"] = float(self.state["basket"].get("risk_sum_usd", 0.0)) + float(risk)

        pos = {
            "ticket": ticket_id,
            "side": "SHORT",
            "lot": lot,
            "entry_price": float(entry_price),
            "sl": float(sl),
            "risk_usd": float(risk),
            "open_time": dt_text(utc_now()),
            "add_index": add_index,
            "comment": comment,
        }
        self.state.setdefault("positions", []).append(pos)
        if signal:
            self.state["last_signal_h1_time"] = str(signal["h1_time"])
        self._save_state()
        self._trade_row(
            "ENTRY" if self.live_enabled else "SHADOW_ENTRY",
            ticket=ticket_id,
            side="SHORT",
            lot=lot,
            price=entry_price,
            sl=sl,
            reason=reason,
            basket_id=self.state["basket"]["id"],
            add_index=add_index,
        )
        logging.info(
            "S20 %s short opened: ticket=%s lot=%.2f price=%.3f sl=%.3f add_index=%d reason=%s",
            "live" if self.live_enabled else "shadow",
            ticket_id,
            lot,
            entry_price,
            sl,
            add_index,
            reason,
        )
        return True

    def _close_all(self, info: Any, reason: str) -> bool:
        positions = list(self.state.get("positions", []))
        if not positions:
            self.state["basket"] = None
            self._save_state()
            return True

        all_closed = True
        for pos in positions:
            ticket = int(pos["ticket"])
            profit = 0.0
            close_price = float(info.ask)
            if self.live_enabled:
                result = self.executor.close_position(ticket, deviation=int(self.params["deviation_points"]))
                if not result:
                    all_closed = False
                    self._error_row("CLOSE_FAILED", getattr(result, "status", "FAILED"), f"{reason} ticket={ticket}")
                    continue
                close_price = float(result.close_price or close_price)
                profit = float(result.profit)
            else:
                profit = (float(pos["entry_price"]) - close_price) * self._price_unit_value(info) * float(pos["lot"])

            self._trade_row(
                "EXIT" if self.live_enabled else "SHADOW_EXIT",
                ticket=ticket,
                side="SHORT",
                lot=pos["lot"],
                price=close_price,
                sl=pos.get("sl", 0.0),
                profit=profit,
                reason=reason,
                basket_id=self.state.get("basket", {}).get("id", ""),
                add_index=pos.get("add_index", ""),
            )

        if all_closed:
            logging.info("S20 basket closed: reason=%s positions=%d", reason, len(positions))
            self.state["positions"] = []
            self.state["basket"] = None
            self.state["reconciliation_required"] = False
            self._set_sync_block(None)
            self._save_state()
        else:
            logging.error("S20 basket close incomplete; keeping state and blocking new entries.")
            self._set_sync_block("close_failed")
            self._save_state()
        return all_closed

    def _basket_elapsed_minutes(self) -> float:
        basket = self.state.get("basket")
        if not basket:
            return 0.0
        first_time = parse_dt(basket.get("first_entry_time"))
        if first_time is None:
            return 0.0
        return max(0.0, (utc_now() - first_time).total_seconds() / 60.0)

    def _can_add(self, info: Any, floating_pnl: float, m1_atr: float | None) -> bool:
        basket = self.state.get("basket")
        if not basket or m1_atr is None or m1_atr <= 0:
            return False
        if self.state.get("sync_block_new_entries") or self.state.get("reconciliation_required"):
            return False
        if len(self.state.get("positions", [])) >= int(self.params["max_positions"]):
            return False
        if self._basket_elapsed_minutes() >= float(self.params["active_window_minutes"]):
            return False
        last_add_time = parse_dt(basket.get("last_add_time"))
        if last_add_time and (utc_now() - last_add_time).total_seconds() < float(self.params["entry_interval_minutes"]) * 60:
            return False
        if floating_pnl <= 0:
            return False
        last_add_price = float(basket.get("last_add_price", 0.0))
        favorable_move = last_add_price - float(info.bid)
        return favorable_move >= float(self.params["add_atr_mult"]) * float(m1_atr)

    def _manage_open_basket(self, info: Any, floating_pnl: float, m1_atr: float | None) -> None:
        basket = self.state.get("basket")
        if not basket:
            return
        risk_sum = max(0.01, float(basket.get("risk_sum_usd", 0.0)))
        if floating_pnl <= -float(self.params["basket_dd_r"]) * risk_sum:
            self._close_all(info, "basket_dd")
            return
        if self._basket_elapsed_minutes() >= float(self.params["close_after_minutes"]):
            self._close_all(info, "time_exit_4h")
            return
        if self._can_add(info, floating_pnl, m1_atr):
            self._open_short(info, float(m1_atr), signal=None, reason="profit_only_add")

    def _maybe_open_pending_entry(self, info: Any, m1: pd.DataFrame | None, m1_atr: float | None) -> None:
        pending = self.state.get("pending_entry_signal")
        if not pending:
            return
        confirm_signal = self._entry_confirm_signal(pending, m1)
        if confirm_signal:
            entry_atr = float(confirm_signal.get("entry_confirm_atr") or m1_atr or 0.0)
            if entry_atr <= 0:
                return
            if self._open_short(info, entry_atr, signal=confirm_signal, reason="entry_confirm_short"):
                self.state["pending_entry_signal"] = None
                self._save_state()
            return
        if self._pending_entry_expired(pending, m1):
            logging.info(
                "S20 pending entry expired: h1_time=%s reference_open=%.3f expires_at=%s",
                pending.get("h1_time"),
                float(pending.get("reference_open", 0.0)),
                pending.get("expires_at"),
            )
            self.state["pending_entry_signal"] = None
            self._save_state()

    def _maybe_open_initial(self, info: Any, h1: pd.DataFrame | None, m1: pd.DataFrame | None, m1_atr: float | None) -> None:
        if self.state.get("basket") or self.state.get("sync_block_new_entries") or self.state.get("reconciliation_required"):
            return
        if self.state.get("pending_entry_signal"):
            if not self._weekend_entry_blocked():
                self._maybe_open_pending_entry(info, m1, m1_atr)
            elif self._pending_entry_expired(self.state["pending_entry_signal"], m1):
                self.state["pending_entry_signal"] = None
                self._save_state()
            return
        if self._weekend_entry_blocked():
            return
        if m1_atr is None or m1_atr <= 0:
            return
        signal = self._signal(h1, m1)
        if not signal:
            return
        if self._entry_confirm_enabled():
            pending = self._build_pending_entry(signal, m1)
            if not pending:
                logging.info("S20 entry confirmation pending was not created because reference M1 bar was unavailable.")
                return
            self.state["pending_entry_signal"] = pending
            self.state["last_signal_h1_time"] = str(signal["h1_time"])
            self._save_state()
            logging.info(
                "S20 pending entry confirmation created: h1_time=%s reference_open=%.3f expires_at=%s",
                pending.get("h1_time"),
                float(pending.get("reference_open", 0.0)),
                pending.get("expires_at"),
            )
            return
        self._open_short(info, m1_atr, signal=signal, reason="h1_large_candle_short")

    def run_once(self) -> None:
        if not bool(self.params.get("enabled", True)):
            logging.info("S20 disabled by params.")
            return

        info = self.executor.get_symbol_info(self.symbol)
        if info is None:
            self._set_sync_block("symbol info failed")
            self._save_state()
            return

        h1 = self._get_bars("h1", int(self.params["h1_timeframe"]), int(self.params["h1_bars"]))
        m1 = self._get_bars("m1", int(self.params["m1_timeframe"]), int(self.params["m1_bars"]))
        m1_atr = self._m1_atr(m1)
        _, floating_pnl = self._sync_positions(info)

        if self.state.get("basket"):
            self._manage_open_basket(info, floating_pnl, m1_atr)
        else:
            self._maybe_open_initial(info, h1, m1, m1_atr)

        self._log_status(info, floating_pnl, m1_atr)

    def _log_status(self, info: Any, floating_pnl: float, m1_atr: float | None) -> None:
        now_monotonic = time.monotonic()
        if now_monotonic - self._last_status_log < float(self.params["status_log_interval_seconds"]):
            return
        self._last_status_log = now_monotonic
        basket = self.state.get("basket") or {}
        pending = self.state.get("pending_entry_signal") or {}
        logging.info(
            "S20 status: bid=%.3f ask=%.3f spread_points=%.1f pos=%d floating=%.2f "
            "m1_atr=%s block=%s reason=%s recon=%s basket=%s pending=%s live=%s",
            float(info.bid),
            float(info.ask),
            self._current_spread_points(info),
            len(self.state.get("positions", [])),
            floating_pnl,
            f"{m1_atr:.4f}" if m1_atr else "None",
            bool(self.state.get("sync_block_new_entries")),
            self.state.get("sync_block_reason"),
            bool(self.state.get("reconciliation_required")),
            basket.get("id"),
            pending.get("h1_time"),
            self.live_enabled,
        )

    def run_forever(self) -> None:
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                raise
            except Exception:
                logging.exception("S20 run loop failed")
            time.sleep(float(self.params["poll_interval_seconds"]))


def run_self_test() -> int:
    base_time = pd.Timestamp("2026-05-24 22:00")
    rows = []
    price = 2400.0
    for i in range(40):
        t = base_time + pd.Timedelta(hours=i)
        rows.append(
            {
                "time": t,
                "Open": price,
                "High": price + 4.0,
                "Low": price - 4.0,
                "Close": price + (1.0 if i % 2 else -1.0),
                "Volume": 100,
            }
        )
        price += 0.2
    rows[-2].update({"Open": 2410.0, "High": 2412.0, "Low": 2380.0, "Close": 2381.0})
    h1 = pd.DataFrame(rows).set_index("time")
    expected_entry_time = rows[-2]["time"] + pd.Timedelta(hours=1)

    def make_m1(end_offset_minutes: int) -> pd.DataFrame:
        m1_rows = []
        first_time = expected_entry_time - pd.Timedelta(minutes=419)
        last_time = expected_entry_time + pd.Timedelta(minutes=end_offset_minutes)
        count = int((last_time - first_time).total_seconds() / 60) + 1
        for i in range(count):
            t = first_time + pd.Timedelta(minutes=i)
            close = 2382.0
            low = 2381.6
            if t == expected_entry_time + pd.Timedelta(minutes=5):
                close = 2381.0
                low = 2380.9
            m1_rows.append({"time": t, "Open": 2382.0, "High": 2382.4, "Low": low, "Close": close, "Volume": 10})
        return pd.DataFrame(m1_rows).set_index("time")

    m1_signal = make_m1(0)
    m1_confirm = make_m1(10)
    params = dict(DEFAULT_PARAMS)
    runner = S20GoldBasketRunner(params)
    runner.state = runner._default_state()
    signal = runner._signal(h1, m1_signal)
    if not signal:
        print("self-test failed: signal was not detected")
        return 1
    pending = runner._build_pending_entry(signal, m1_signal)
    if not pending:
        print("self-test failed: pending entry was not created")
        return 1
    confirm = runner._entry_confirm_signal(pending, m1_confirm)
    if not confirm:
        print("self-test failed: entry confirmation was not detected")
        return 1
    atr = runner._m1_atr(m1_confirm)
    if atr is None or atr <= 0:
        print("self-test failed: M1 ATR was invalid")
        return 1
    runner._save_state = lambda: None
    runner.state = runner._default_state()
    runner._maybe_open_initial(object(), h1, m1_signal, atr)
    if not runner.state.get("pending_entry_signal"):
        print("self-test failed: pending entry path did not store state")
        return 1
    opened = []

    def fake_open_short(info: Any, entry_atr: float, signal: dict[str, Any] | None, reason: str) -> bool:
        opened.append({"entry_atr": entry_atr, "signal": signal, "reason": reason})
        return True

    runner._open_short = fake_open_short
    runner._maybe_open_pending_entry(object(), m1_confirm, atr)
    if runner.state.get("pending_entry_signal") is not None:
        print("self-test failed: confirmed pending entry was not cleared")
        return 1
    if not opened or opened[0]["reason"] != "entry_confirm_short":
        print("self-test failed: confirmed pending entry did not call open")
        return 1
    print("self-test ok")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    setup_logging()
    if args.self_test:
        return run_self_test()

    params = load_params()
    runner = S20GoldBasketRunner(params)
    if not runner.connect_and_preflight():
        return 2
    if args.once:
        runner.run_once()
    else:
        runner.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
