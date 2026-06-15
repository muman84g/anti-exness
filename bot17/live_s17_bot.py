from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from live_data_fetcher import MT5DataManager
from live_executor import MT5Executor, ORDER_TYPE_BUY, ORDER_TYPE_SELL


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "s17_bot.log"
TRADE_LOG_FILE = LOG_DIR / "s17_trades.csv"
DEFAULT_PARAMS_FILE = BASE_DIR / "s17_params.json"
DEFAULT_STATE_FILE = BASE_DIR / "s17_bot_state.json"
JST = timezone(timedelta(hours=9), "JST")

CURRENCIES = ["EUR", "GBP", "USD", "JPY", "CHF", "AUD", "CAD", "NZD"]


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def save_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, ensure_ascii=False)
        file.write("\n")


def append_trade_event(row: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    columns = [
        "time_jst",
        "event",
        "symbol",
        "combo_id",
        "side",
        "ticket",
        "lot",
        "price",
        "sl",
        "tp",
        "profit",
        "reason",
        "signal_bar_time",
        "strength_diff",
        "strength_roc_diff",
        "spread_pips",
    ]
    exists = TRADE_LOG_FILE.exists()
    mode = "a" if exists else "w"
    encoding = "utf-8" if exists else "utf-8-sig"
    with TRADE_LOG_FILE.open(mode, newline="", encoding=encoding) as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in columns})


def now_jst() -> datetime:
    return datetime.now(JST)


def pair_currencies(symbol: str) -> tuple[str, str]:
    letters = "".join(ch for ch in str(symbol).upper() if ch.isalpha())
    stem = letters[:6]
    return stem[:3], stem[3:6]


def pip_size(symbol: str) -> float:
    base, quote = pair_currencies(symbol)
    return 0.01 if quote == "JPY" else 0.0001


def aggregate_currency_metric(metric: pd.DataFrame) -> pd.DataFrame:
    strength = pd.DataFrame(0.0, index=metric.index, columns=CURRENCIES)
    counts = pd.DataFrame(0.0, index=metric.index, columns=CURRENCIES)
    for symbol in metric.columns:
        base, quote = pair_currencies(symbol)
        if base not in CURRENCIES or quote not in CURRENCIES:
            continue
        values = metric[symbol]
        valid = values.notna().astype(float)
        strength[base] = strength[base] + values.fillna(0.0)
        strength[quote] = strength[quote] - values.fillna(0.0)
        counts[base] = counts[base] + valid
        counts[quote] = counts[quote] + valid
    return strength / counts.replace(0.0, np.nan)


def build_strength(profile: dict[str, Any], close: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    window = int(profile["strength_window_bars"])
    smooth = int(profile.get("smooth_bars", 1))
    roc_window = int(profile.get("roc_window_bars", 1))
    metric = close.pct_change(window) * 100.0
    strength = aggregate_currency_metric(metric)
    if smooth > 1:
        strength = strength.ewm(span=smooth, adjust=False, min_periods=smooth).mean()
    strength_roc = strength.diff(max(1, roc_window))
    return strength, strength_roc


def evaluate_signal(
    profile: dict[str, Any],
    target_index: pd.Index,
    strength: pd.DataFrame,
    strength_roc: pd.DataFrame,
) -> dict[str, Any] | None:
    if len(target_index) < 2:
        return None

    symbol = str(profile["symbol"])
    base, quote = pair_currencies(symbol)
    if base not in strength.columns or quote not in strength.columns:
        return None

    diff = (strength[base] - strength[quote]).reindex(target_index)
    roc_diff = (strength_roc[base] - strength_roc[quote]).reindex(target_index)
    entry = float(profile["entry_threshold"])
    exit_threshold = float(profile["exit_threshold"])
    roc_threshold = float(profile.get("roc_threshold", 0.0))
    top_n = int(profile.get("top_bottom_n", 2))
    use_rank_filter = bool(profile.get("use_rank_filter", True))
    mode = str(profile.get("mode", "rank_follow"))

    rank_strong = strength.rank(axis=1, ascending=False, method="min").reindex(target_index)
    rank_weak = strength.rank(axis=1, ascending=True, method="min").reindex(target_index)
    follow_long_filter = (rank_strong[base] <= top_n) & (rank_weak[quote] <= top_n)
    follow_short_filter = (rank_strong[quote] <= top_n) & (rank_weak[base] <= top_n)
    fade_long_filter = (rank_weak[base] <= top_n) & (rank_strong[quote] <= top_n)
    fade_short_filter = (rank_weak[quote] <= top_n) & (rank_strong[base] <= top_n)
    if not use_rank_filter:
        all_true = pd.Series(True, index=target_index)
        follow_long_filter = all_true
        follow_short_filter = all_true
        fade_long_filter = all_true
        fade_short_filter = all_true

    strategy = str(profile.get("strategy", "currency_strength_dashboard_rank"))
    if strategy != "currency_strength_dashboard_rank":
        raise ValueError(f"Unsupported s17 strategy: {strategy}")

    if mode == "fade_extreme":
        long_state = (diff <= -entry) & (roc_diff >= roc_threshold) & fade_long_filter
        short_state = (diff >= entry) & (roc_diff <= -roc_threshold) & fade_short_filter
        exit_long = (diff >= -exit_threshold) | (roc_diff < 0) | follow_short_filter
        exit_short = (diff <= exit_threshold) | (roc_diff > 0) | follow_long_filter
    else:
        long_state = (diff >= entry) & (roc_diff >= -roc_threshold) & follow_long_filter
        short_state = (diff <= -entry) & (roc_diff <= roc_threshold) & follow_short_filter
        exit_long = (diff <= exit_threshold) | short_state
        exit_short = (diff >= -exit_threshold) | long_state

    long_state = long_state.fillna(False).astype(bool)
    short_state = short_state.fillna(False).astype(bool)
    signal_bar_time = pd.Timestamp(target_index[-1])
    return {
        "signal_bar_time": signal_bar_time,
        "long_signal": bool(long_state.iloc[-1] and not long_state.iloc[-2]),
        "short_signal": bool(short_state.iloc[-1] and not short_state.iloc[-2]),
        "exit_long": bool(exit_long.fillna(False).iloc[-1]),
        "exit_short": bool(exit_short.fillna(False).iloc[-1]),
        "strength_diff": float(diff.iloc[-1]) if pd.notna(diff.iloc[-1]) else math.nan,
        "strength_roc_diff": float(roc_diff.iloc[-1]) if pd.notna(roc_diff.iloc[-1]) else math.nan,
    }


def timestamp_to_state(value: pd.Timestamp | datetime | None) -> str | None:
    if value is None:
        return None
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def timestamp_from_state(value: str | None) -> pd.Timestamp | None:
    if not value:
        return None
    return pd.Timestamp(value)


def is_after_jst_rule(rule: dict[str, Any], prefix: str, current: datetime) -> bool:
    weekday = int(rule[f"{prefix}_weekday_jst"])
    hour = int(rule[f"{prefix}_hour_jst"])
    minute = int(rule[f"{prefix}_minute_jst"])
    current_key = (current.weekday(), current.hour, current.minute)
    return current_key >= (weekday, hour, minute)


def weekend_entry_blocked(params: dict[str, Any], current: datetime) -> bool:
    rule = params.get("weekend_filter", {})
    return bool(rule.get("enabled", True)) and is_after_jst_rule(rule, "entry_stop", current)


def weekend_force_close(params: dict[str, Any], current: datetime) -> bool:
    rule = params.get("weekend_filter", {})
    return bool(rule.get("enabled", True)) and is_after_jst_rule(rule, "force_close", current)


def volume_for_profile(info: Any, profile: dict[str, Any]) -> float:
    raw_lot = float(profile.get("lot", 0.01))
    max_lot = float(profile.get("max_lot", raw_lot))
    volume_min = float(getattr(info, "volume_min", 0.01))
    volume_max = float(getattr(info, "volume_max", max_lot))
    volume_step = float(getattr(info, "volume_step", volume_min))
    lot = max(volume_min, min(raw_lot, max_lot, volume_max))
    if volume_step > 0:
        lot = round(lot / volume_step) * volume_step
    return round(lot, 2)


def market_context(executor: MT5Executor, profile: dict[str, Any]) -> dict[str, Any] | None:
    symbol = str(profile["symbol"])
    info = executor.get_symbol_info(symbol)
    if info is None:
        return None
    pip = pip_size(symbol)
    spread_pips = abs(float(info.ask) - float(info.bid)) / pip
    digits = int(getattr(info, "digits", 5))
    return {
        "info": info,
        "pip": pip,
        "spread_pips": spread_pips,
        "digits": digits,
    }


def order_prices(profile: dict[str, Any], side: str, context: dict[str, Any]) -> dict[str, float]:
    info = context["info"]
    pip = float(context["pip"])
    digits = int(context["digits"])
    tp_pips = float(profile["tp_pips"])
    sl_pips = float(profile["sl_pips"])
    if side == "LONG":
        entry_price = float(info.ask)
        sl = entry_price - sl_pips * pip
        tp = entry_price + tp_pips * pip
    else:
        entry_price = float(info.bid)
        sl = entry_price + sl_pips * pip
        tp = entry_price - tp_pips * pip
    return {
        "entry_price": round(entry_price, digits),
        "sl": round(sl, digits),
        "tp": round(tp, digits),
    }


class Bot17:
    def __init__(self, params_file: Path = DEFAULT_PARAMS_FILE, state_file: Path = DEFAULT_STATE_FILE):
        self.params_file = params_file
        self.state_file = state_file
        self.params = load_json(params_file, {})
        self.state = load_json(state_file, {"version": 1, "symbols": {}})
        self.data_manager = MT5DataManager()
        self.executor = MT5Executor(self.data_manager)
        self.bar_cache: dict[str, pd.DataFrame] = {}

    def connect(self) -> bool:
        return self.data_manager.connect()

    def disconnect(self) -> None:
        self.data_manager.disconnect()

    def save_state(self) -> None:
        save_json(self.state_file, self.state)

    def symbol_state(self, symbol: str) -> dict[str, Any]:
        symbols = self.state.setdefault("symbols", {})
        return symbols.setdefault(symbol, {"last_processed_bar_time": None, "position": None})

    def update_symbol_bars(
        self,
        symbol: str,
        history_bars: int,
        refresh_bars: int,
        fetch_retries: int,
        retry_sleep_seconds: float,
    ) -> pd.DataFrame | None:
        cached = self.bar_cache.get(symbol)
        fetch_bars = history_bars if cached is None or cached.empty else refresh_bars
        df = None
        max_attempts = max(1, fetch_retries + 1)
        for attempt in range(1, max_attempts + 1):
            df = self.data_manager.get_historical_data(symbol, 15, fetch_bars)
            if df is not None and not df.empty:
                break
            if attempt < max_attempts:
                logging.warning(
                    "Failed to fetch M15 bars for %s attempt %d/%d; retrying in %.1fs.",
                    symbol,
                    attempt,
                    max_attempts,
                    retry_sleep_seconds,
                )
                if retry_sleep_seconds > 0:
                    time.sleep(retry_sleep_seconds)
        if df is None or df.empty:
            if cached is not None and not cached.empty:
                logging.warning(
                    "Failed to refresh M15 bars for %s; using cached %d rows latest=%s.",
                    symbol,
                    len(cached),
                    cached.index[-1],
                )
                return cached
            logging.error("Failed to load initial M15 bars for %s.", symbol)
            return None

        fresh = df.sort_index()
        if cached is not None and not cached.empty:
            merged = pd.concat([cached, fresh]).sort_index()
            merged = merged[~merged.index.duplicated(keep="last")]
        else:
            merged = fresh
            logging.info("Seeded M15 cache for %s rows=%d latest=%s.", symbol, len(merged), merged.index[-1])

        merged = merged.tail(history_bars)
        self.bar_cache[symbol] = merged
        return merged

    def load_universe_bars(self) -> tuple[dict[str, pd.DataFrame], pd.DataFrame] | None:
        universe = list(self.params.get("universe", []))
        history_bars = int(self.params.get("history_bars", 360))
        refresh_bars = max(2, int(self.params.get("refresh_bars", 8)))
        fetch_retries = max(0, int(self.params.get("fetch_retries", 2)))
        retry_sleep_seconds = max(0.0, float(self.params.get("fetch_retry_sleep_seconds", 1.0)))
        if not universe:
            logging.error("No universe symbols configured.")
            return None

        frames: dict[str, pd.DataFrame] = {}
        for symbol in universe:
            df = self.update_symbol_bars(
                str(symbol),
                history_bars,
                refresh_bars,
                fetch_retries,
                retry_sleep_seconds,
            )
            if df is None:
                return None
            frames[str(symbol)] = df

        close = pd.concat({symbol: df["Close"] for symbol, df in frames.items()}, axis=1, join="inner").sort_index()
        close = close.dropna(how="any")
        if bool(self.params.get("drop_last_bar_as_forming", True)) and len(close) > 0:
            close = close.iloc[:-1]
        if len(close) < 30:
            logging.error("Not enough synchronized bars after alignment: %d", len(close))
            return None

        aligned = {symbol: frame.reindex(close.index).dropna() for symbol, frame in frames.items()}
        return aligned, close

    def run_once(self) -> None:
        loaded = self.load_universe_bars()
        if loaded is None:
            return
        bars, close = loaded
        current = now_jst()

        for profile in self.params.get("symbols", []):
            if not bool(profile.get("enabled", True)):
                continue
            try:
                self.process_profile(profile, bars, close, current)
            except Exception:
                logging.exception("Failed to process profile %s", profile.get("symbol"))
        self.save_state()

    def process_profile(
        self,
        profile: dict[str, Any],
        bars: dict[str, pd.DataFrame],
        close: pd.DataFrame,
        current: datetime,
    ) -> None:
        symbol = str(profile["symbol"])
        combo_id = str(profile.get("combo_id", ""))
        state = self.symbol_state(symbol)
        if symbol not in bars:
            logging.error("Target symbol %s is missing from universe bars.", symbol)
            return

        strength, strength_roc = build_strength(profile, close)
        signal = evaluate_signal(profile, bars[symbol].index, strength, strength_roc)
        if signal is None:
            logging.warning("No signal available for %s.", symbol)
            return

        signal_bar_time = signal["signal_bar_time"]
        last_processed = timestamp_from_state(state.get("last_processed_bar_time"))
        if last_processed is not None and signal_bar_time <= last_processed:
            logging.info("%s already processed signal bar %s.", symbol, signal_bar_time)
            return

        magic = int(profile["magic"])
        positions = self.executor.get_positions(symbol, magic)
        if positions is None:
            logging.error("Position query failed for %s magic=%s.", symbol, magic)
            return

        if weekend_force_close(self.params, current) and positions:
            for position in positions:
                self.close_position(profile, position, signal, "WEEKEND_CLOSE")
            state["last_processed_bar_time"] = timestamp_to_state(signal_bar_time)
            state["position"] = None
            return

        if len(positions) > 1:
            logging.error("%s has multiple managed positions for magic=%s; skipping.", symbol, magic)
            return

        live_position = positions[0] if positions else None
        if live_position is not None:
            state_position = state.get("position") or {}
            if not state_position:
                state["position"] = {
                    "ticket": int(live_position.ticket),
                    "side": live_position.direction,
                    "entry_price": float(live_position.open_price),
                    "lot": float(live_position.volume),
                    "entry_signal_bar_time": timestamp_to_state(signal_bar_time),
                }
                logging.warning("%s live position restored into s17 state.", symbol)
            self.manage_open_position(profile, live_position, signal, state)
            state["last_processed_bar_time"] = timestamp_to_state(signal_bar_time)
            return

        if state.get("position"):
            logging.warning("%s state had a position but MT5 has none; clearing local state.", symbol)
            state["position"] = None

        if last_processed is None and bool(self.params.get("prime_on_first_run", True)):
            state["last_processed_bar_time"] = timestamp_to_state(signal_bar_time)
            logging.info("%s primed at %s without catch-up entry.", symbol, signal_bar_time)
            return

        entry_side = None
        if bool(signal["long_signal"]):
            entry_side = "LONG"
        elif bool(signal["short_signal"]):
            entry_side = "SHORT"

        if entry_side is None:
            state["last_processed_bar_time"] = timestamp_to_state(signal_bar_time)
            logging.info(
                "%s no entry at %s diff=%.6f roc=%.6f.",
                symbol,
                signal_bar_time,
                signal["strength_diff"],
                signal["strength_roc_diff"],
            )
            return

        if weekend_entry_blocked(self.params, current):
            state["last_processed_bar_time"] = timestamp_to_state(signal_bar_time)
            logging.info("%s entry blocked by weekend rule.", symbol)
            return

        context = market_context(self.executor, profile)
        if context is None:
            return
        max_spread = float(profile.get("max_spread_pips", 999.0))
        if float(context["spread_pips"]) > max_spread:
            state["last_processed_bar_time"] = timestamp_to_state(signal_bar_time)
            logging.info(
                "%s entry skipped by spread %.3f > %.3f pips.",
                symbol,
                context["spread_pips"],
                max_spread,
            )
            append_trade_event(
                {
                    "time_jst": now_jst().isoformat(),
                    "event": "SKIP_SPREAD",
                    "symbol": symbol,
                    "combo_id": combo_id,
                    "side": entry_side,
                    "reason": "SPREAD",
                    "signal_bar_time": timestamp_to_state(signal_bar_time),
                    "strength_diff": signal["strength_diff"],
                    "strength_roc_diff": signal["strength_roc_diff"],
                    "spread_pips": context["spread_pips"],
                }
            )
            return

        if not bool(self.params.get("trading_enabled", False)):
            state["last_processed_bar_time"] = timestamp_to_state(signal_bar_time)
            logging.info("%s %s signal detected in dry-run mode.", symbol, entry_side)
            append_trade_event(
                {
                    "time_jst": now_jst().isoformat(),
                    "event": "SIGNAL_DRY_RUN",
                    "symbol": symbol,
                    "combo_id": combo_id,
                    "side": entry_side,
                    "reason": "DRY_RUN",
                    "signal_bar_time": timestamp_to_state(signal_bar_time),
                    "strength_diff": signal["strength_diff"],
                    "strength_roc_diff": signal["strength_roc_diff"],
                    "spread_pips": context["spread_pips"],
                }
            )
            return

        self.open_position(profile, entry_side, signal, context, state)
        state["last_processed_bar_time"] = timestamp_to_state(signal_bar_time)

    def manage_open_position(
        self,
        profile: dict[str, Any],
        live_position: Any,
        signal: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        side = str(getattr(live_position, "direction", "")).upper()
        should_signal_exit = (side == "LONG" and signal["exit_long"]) or (side == "SHORT" and signal["exit_short"])
        if should_signal_exit:
            self.close_position(profile, live_position, signal, "SIGNAL_EXIT")
            state["position"] = None
            return

        state_position = state.get("position") or {}
        entry_signal_time = timestamp_from_state(state_position.get("entry_signal_bar_time"))
        if entry_signal_time is not None:
            bars_held = int((pd.Timestamp(signal["signal_bar_time"]) - entry_signal_time) / pd.Timedelta(minutes=15))
            if bars_held >= int(profile["max_hold_bars"]):
                self.close_position(profile, live_position, signal, "TIME_EXIT")
                state["position"] = None
                return

        logging.info(
            "%s holding ticket=%s side=%s diff=%.6f roc=%.6f.",
            profile["symbol"],
            live_position.ticket,
            side,
            signal["strength_diff"],
            signal["strength_roc_diff"],
        )

    def open_position(
        self,
        profile: dict[str, Any],
        side: str,
        signal: dict[str, Any],
        context: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        symbol = str(profile["symbol"])
        order_type = ORDER_TYPE_BUY if side == "LONG" else ORDER_TYPE_SELL
        prices = order_prices(profile, side, context)
        lot = volume_for_profile(context["info"], profile)
        ticket = self.executor.open_position(
            symbol,
            order_type,
            lot,
            sl=prices["sl"],
            tp=prices["tp"],
            deviation=int(profile.get("deviation", 20)),
            magic=int(profile["magic"]),
            comment=str(profile.get("comment", "s17")),
        )
        if ticket is None:
            logging.error("%s failed to open %s.", symbol, side)
            return

        exec_price = float(getattr(ticket, "price", prices["entry_price"]))
        state["position"] = {
            "ticket": int(ticket),
            "side": side,
            "entry_price": exec_price,
            "lot": lot,
            "sl": prices["sl"],
            "tp": prices["tp"],
            "entry_signal_bar_time": timestamp_to_state(signal["signal_bar_time"]),
        }
        append_trade_event(
            {
                "time_jst": now_jst().isoformat(),
                "event": "OPEN",
                "symbol": symbol,
                "combo_id": profile.get("combo_id", ""),
                "side": side,
                "ticket": int(ticket),
                "lot": lot,
                "price": exec_price,
                "sl": prices["sl"],
                "tp": prices["tp"],
                "reason": "ENTRY_SIGNAL",
                "signal_bar_time": timestamp_to_state(signal["signal_bar_time"]),
                "strength_diff": signal["strength_diff"],
                "strength_roc_diff": signal["strength_roc_diff"],
                "spread_pips": context["spread_pips"],
            }
        )

    def close_position(self, profile: dict[str, Any], position: Any, signal: dict[str, Any], reason: str) -> None:
        symbol = str(profile["symbol"])
        result = self.executor.close_position(int(position.ticket), deviation=int(profile.get("deviation", 20)))
        if not result:
            logging.error("%s failed to close ticket=%s reason=%s.", symbol, position.ticket, reason)
            return
        append_trade_event(
            {
                "time_jst": now_jst().isoformat(),
                "event": "CLOSE",
                "symbol": symbol,
                "combo_id": profile.get("combo_id", ""),
                "side": getattr(position, "direction", ""),
                "ticket": int(position.ticket),
                "lot": getattr(result, "lot", ""),
                "price": getattr(result, "close_price", ""),
                "profit": getattr(result, "profit", ""),
                "reason": reason,
                "signal_bar_time": timestamp_to_state(signal["signal_bar_time"]),
                "strength_diff": signal["strength_diff"],
                "strength_roc_diff": signal["strength_roc_diff"],
            }
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="s17 live runner for v2 currency strength tick candidates.")
    parser.add_argument("--once", action="store_true", help="Run one polling cycle and exit.")
    parser.add_argument("--params", default=str(DEFAULT_PARAMS_FILE), help="Path to s17_params.json.")
    parser.add_argument("--state", default=str(DEFAULT_STATE_FILE), help="Path to s17_bot_state.json.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging()
    bot = Bot17(Path(args.params), Path(args.state))
    logging.info("Starting s17. trading_enabled=%s", bot.params.get("trading_enabled", False))
    if not bot.connect():
        logging.error("Could not connect to MT5 EA bridge.")
        return 1

    try:
        if args.once:
            bot.run_once()
            return 0
        poll_seconds = int(bot.params.get("poll_seconds", 5))
        while True:
            bot.run_once()
            time.sleep(max(1, poll_seconds))
    finally:
        bot.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
