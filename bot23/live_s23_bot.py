# -*- coding: utf-8 -*-
"""S23 Chisiki/ReactVol fixed4 shadow/live runner.

Default params are shadow/no-order. This runner keeps the four fixed candidates
isolated by strategy id, magic, comment prefix, and state.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd

os.environ.setdefault("BOT_SUFFIX", "s23")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from live_data_fetcher import MT5DataManager
from live_executor import MT5Executor, ORDER_TYPE_BUY, ORDER_TYPE_SELL
from live_safety import LiveSafetyOptions, clean_sync_block_if_flat


UTC = timezone.utc
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
STATE_DIR = os.path.join(SCRIPT_DIR, "state")
LOG_FILE = os.path.join(LOG_DIR, "s23_bot.log")
TRADE_LOG_FILE = os.path.join(LOG_DIR, "s23_trades.csv")
STATE_FILE = os.path.join(STATE_DIR, "s23_bot_state.json")
PARAMS_FILE = os.path.join(SCRIPT_DIR, "s23_params.json")

TRADE_FIELDS = [
    "timestamp_utc",
    "event",
    "strategy_id",
    "symbol",
    "mt5_symbol",
    "ticket",
    "side",
    "lot",
    "price",
    "profit",
    "reason",
    "signal_bar_time",
    "live",
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


def atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def append_csv(path: str, row: dict[str, Any], fields: list[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({name: row.get(name, "") for name in fields})


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


class S23Fixed4Runner:
    def __init__(self, params: dict[str, Any]):
        self.params = params
        self.live_enabled = bool(params.get("live_trading_enabled", False))
        self.shadow_enabled = bool(params.get("shadow_forward_enabled", True))
        self.safety = LiveSafetyOptions(**params.get("safety", {}))
        self.dm = MT5DataManager(self.safety)
        self.executor = MT5Executor()
        self.state = self._load_state()
        self._last_status_log = 0.0

    def _default_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "bot": "bot23",
            "strategy_id": self.params["strategy_id"],
            "last_saved_utc": None,
            "strategies": {
                s["id"]: {
                    "basket": [],
                    "cooldown_until_bar": -1,
                    "last_add_price": None,
                    "last_signal_bar": None,
                    "last_closed_at_utc": None,
                    "last_closed_reason": None,
                    "last_closed_signal_bar": None,
                    "reverse_used": False,
                    "sync_block_new_entries": False,
                    "sync_block_reason": None,
                    "sync_block_recoverable": False,
                    "sync_block_details": {},
                }
                for s in self.params["strategies"]
            },
        }

    def _load_state(self) -> dict[str, Any]:
        if not os.path.exists(STATE_FILE):
            return self._default_state()
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            logging.exception("Could not load state; using fail-closed default")
            state = self._default_state()
        default = self._default_state()
        state.setdefault("strategies", {})
        for sid, st in default["strategies"].items():
            state["strategies"].setdefault(sid, st)
            for key, value in st.items():
                state["strategies"][sid].setdefault(key, value)
        return state

    def _save_state(self) -> None:
        self.state["last_saved_utc"] = dt_text(utc_now())
        atomic_write_json(STATE_FILE, self.state)

    def _st(self, strat: dict[str, Any]) -> dict[str, Any]:
        return self.state["strategies"][strat["id"]]

    def _trade_row(self, event: str, strat: dict[str, Any], **kwargs: Any) -> None:
        row = {
            "timestamp_utc": dt_text(utc_now()),
            "event": event,
            "strategy_id": strat["id"],
            "symbol": self.params["symbol"],
            "mt5_symbol": self.params.get("mt5_symbol", self.params["symbol"]),
            "live": self.live_enabled,
        }
        row.update(kwargs)
        append_csv(TRADE_LOG_FILE, row, TRADE_FIELDS)

    def connect_and_preflight(self) -> bool:
        if not bool(self.params.get("enabled", True)):
            logging.info("S23 disabled by params.")
            return False
        if not self.dm.connect():
            logging.error("S23 EA bridge connect failed.")
            return False
        caps = self.executor.get_bridge_capabilities()
        logging.info("S23 bridge caps: %s", caps)
        return True

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

    def _signal(self, row: pd.Series, strat: dict[str, Any]) -> str | None:
        ts = pd.Timestamp(row.name)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        ts = ts.tz_convert("UTC")
        if not in_session(ts, int(strat["session_start_utc"]), int(strat["session_end_utc"])):
            return None
        if float(row.get("spread_points", 0.0)) > float(self.params.get("max_entry_spread_points", 300.0)):
            return None
        atr30 = float(row.get("atr30", math.nan))
        vol_ratio = float(row.get("vol_ratio", math.nan))
        if not math.isfinite(atr30) or not math.isfinite(vol_ratio) or vol_ratio < float(strat.get("vol_min", 1.0)):
            return None
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
            return None
        if long_ok:
            return "LONG"
        if short_ok:
            return "SHORT"
        return None

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

    def _sync_strategy(self, strat: dict[str, Any]) -> bool:
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        st = self._st(strat)
        positions = self.executor.get_positions(symbol, int(strat["magic"]))
        orders = self.executor.get_orders(symbol, int(strat["magic"]))
        if positions is None or orders is None:
            st["sync_block_new_entries"] = True
            st["sync_block_reason"] = "positions_or_orders_unavailable"
            st["sync_block_recoverable"] = True
            st["sync_block_details"] = {}
            return False
        if clean_sync_block_if_flat(
            symbol_key=strat["id"],
            state=st,
            positions=positions,
            orders=orders,
            save_state=self._save_state,
            options=self.safety,
            audit=lambda event, reason, note: self._trade_row(event, strat, reason=reason, note=note),
        ):
            logging.info("S23 clean sync cleared: %s", strat["id"])
        if orders:
            st["sync_block_new_entries"] = True
            st["sync_block_reason"] = "same_magic_unexpected_order"
            st["sync_block_recoverable"] = False
            st["sync_block_details"] = {"tickets": [int(o.ticket) for o in orders]}
            return False
        return not bool(st.get("sync_block_new_entries"))

    def _close_basket(self, strat: dict[str, Any], reason: str, price_row: pd.Series, pnl: float) -> None:
        st = self._st(strat)
        if self.live_enabled:
            for pos in list(st["basket"]):
                ticket = int(pos.get("ticket") or 0)
                if ticket and not self.executor.close_position(ticket, int(self.params.get("deviation_points", 50))):
                    st["sync_block_new_entries"] = True
                    st["sync_block_reason"] = f"close_failed_{ticket}"
                    st["sync_block_recoverable"] = True
                    self._save_state()
                    return
        self._trade_row("basket_close", strat, profit=round(float(pnl), 2), reason=reason, signal_bar_time=str(price_row.name))
        if bool(strat.get("reverse_on_fail", False)) and reason == "basket_stop" and not bool(st.get("reverse_used", False)):
            side = "SHORT" if sum(1 for p in st["basket"] if p["side"] == "LONG") >= sum(1 for p in st["basket"] if p["side"] == "SHORT") else "LONG"
            st["basket"] = []
            st["last_add_price"] = None
            st["reverse_used"] = True
            self._open_entry(strat, side, price_row, note="reverse_after_stop")
            return
        st["basket"] = []
        st["last_add_price"] = None
        st["reverse_used"] = False
        st["cooldown_until_bar"] = int(st.get("bar_index", 0)) + int(strat.get("cooldown", 0))
        st["last_closed_at_utc"] = dt_text(utc_now())
        st["last_closed_reason"] = reason
        st["last_closed_signal_bar"] = str(price_row.name)
        self._save_state()

    def _open_entry(self, strat: dict[str, Any], side: str, price_row: pd.Series, note: str = "") -> None:
        st = self._st(strat)
        if note != "reverse_after_stop" and st.get("last_closed_signal_bar") == str(price_row.name):
            self._trade_row("entry_skip", strat, reason="same_bar_reentry_after_close", signal_bar_time=str(price_row.name))
            return
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        digits = int(self.params.get("price_digits", 2))
        lot = float(strat.get("lot", self.params.get("default_lot", 0.01)))
        ask = float(price_row.get("AskOpen", price_row["Open"]))
        bid = float(price_row["Open"])
        entry_price = normalize_price(ask if side == "LONG" else bid, digits)
        ticket = None
        if self.live_enabled:
            order_type = ORDER_TYPE_BUY if side == "LONG" else ORDER_TYPE_SELL
            ticket = self.executor.open_position(
                symbol,
                order_type,
                lot,
                0.0,
                0.0,
                deviation=int(self.params.get("deviation_points", 50)),
                magic=int(strat["magic"]),
                comment=str(strat["comment_prefix"]),
                digits=digits,
            )
            if ticket is None:
                st["sync_block_new_entries"] = True
                st["sync_block_reason"] = "open_position_failed"
                st["sync_block_recoverable"] = True
                self._save_state()
                return
        st["basket"].append(
            {
                "ticket": ticket,
                "side": side,
                "lot": lot,
                "entry_price": entry_price,
                "entry_bar_index": int(st.get("bar_index", 0)),
                "entry_time_utc": str(price_row.name),
            }
        )
        st["last_add_price"] = entry_price
        st["last_signal_bar"] = str(price_row.name)
        self._trade_row("entry", strat, ticket=ticket or "", side=side, lot=lot, price=entry_price, signal_bar_time=str(price_row.name), note=note)
        self._save_state()

    def _run_strategy(self, strat: dict[str, Any], bars: pd.DataFrame, info: Any) -> None:
        st = self._st(strat)
        if not self._sync_strategy(strat):
            self._trade_row("entry_skip", strat, reason=st.get("sync_block_reason"), note="sync_block")
            self._save_state()
            return
        if len(bars) < 2:
            return
        now = bars.iloc[-1]
        st["bar_index"] = int(st.get("bar_index", -1)) + 1
        bid = float(getattr(info, "bid", now["Close"]))
        ask = float(getattr(info, "ask", now.get("AskOpen", now["Open"])))
        if st["basket"]:
            pnl = self._basket_pnl(strat, bid, ask)
            first_i = min(int(p.get("entry_bar_index", st["bar_index"])) for p in st["basket"])
            held = int(st["bar_index"]) - first_i
            reason = None
            if pnl >= float(strat["basket_target_usd"]):
                reason = "basket_target"
            elif pnl <= -float(strat["basket_stop_usd"]):
                reason = "basket_stop"
            elif held >= int(strat["max_hold_bars"]):
                reason = "max_hold"
            if reason:
                self._close_basket(strat, reason, now, pnl)
                return
        side = self._signal(now, strat)
        if not side:
            return
        if int(st.get("bar_index", 0)) < int(st.get("cooldown_until_bar", -1)):
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
        self._open_entry(strat, side, now)

    def run_once(self) -> None:
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        info = self.executor.get_symbol_info(symbol)
        if info is None:
            for strat in self.params["strategies"]:
                st = self._st(strat)
                st["sync_block_new_entries"] = True
                st["sync_block_reason"] = "symbol_info_failed"
                st["sync_block_recoverable"] = True
            self._save_state()
            return
        bars = self._get_m1()
        if bars is None or bars.empty:
            for strat in self.params["strategies"]:
                self._trade_row("entry_skip", strat, reason="m1_bars_unavailable")
            return
        point = float(self.params.get("point_size", 0.01))
        current_spread_points = max(0.0, (float(getattr(info, "ask", 0.0)) - float(getattr(info, "bid", 0.0))) / point)
        bars["spread_points"] = current_spread_points
        for strat in self.params["strategies"]:
            if bool(strat.get("enabled", True)):
                self._run_strategy(strat, bars, info)
        now = time.time()
        if now - self._last_status_log >= float(self.params.get("status_log_interval_seconds", 60)):
            logging.info("S23 status: live=%s shadow=%s strategies=%s", self.live_enabled, self.shadow_enabled, {s["id"]: len(self._st(s)["basket"]) for s in self.params["strategies"]})
            self._last_status_log = now


class FakeDM:
    def __init__(self, *_: Any):
        pass

    def connect(self) -> bool:
        return True

    def get_historical_data(self, *_: Any, **__: Any) -> pd.DataFrame:
        idx = pd.date_range("2026-01-01 12:00:00", periods=160, freq="1min", tz="UTC")
        close = pd.Series([2000.0 + i * 0.4 for i in range(160)], index=idx)
        return pd.DataFrame({"Open": close, "High": close + 0.2, "Low": close - 0.2, "Close": close, "AskOpen": close + 0.03, "Volume": 10}, index=idx)


class FakeExecutor:
    def get_bridge_capabilities(self) -> dict[str, Any]:
        return {"commands": {"INFO", "HIST", "POSITIONS", "ORDERS"}}

    def get_symbol_info(self, *_: Any) -> Any:
        return type("Info", (), {"bid": 2064.0, "ask": 2064.03})()

    def get_positions(self, *_: Any) -> list[Any]:
        return []

    def get_orders(self, *_: Any) -> list[Any]:
        return []

    def open_position(self, *_: Any, **__: Any) -> int:
        return 1

    def close_position(self, *_: Any, **__: Any) -> bool:
        return True


def load_params(path: str = PARAMS_FILE) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def self_test() -> None:
    params = load_params()
    params["live_trading_enabled"] = False
    params["shadow_forward_enabled"] = True
    for strat in params["strategies"]:
        strat["vol_min"] = 0.9
    runner = S23Fixed4Runner(params)
    runner.dm = FakeDM()
    runner.executor = FakeExecutor()
    runner._save_state = lambda: None
    rows: list[tuple[str, str, str]] = []
    runner._trade_row = lambda event, strat, **kw: rows.append((event, strat["id"], str(kw.get("reason", ""))))
    runner.run_once()
    assert any(row[0] == "entry" for row in rows), "expected at least one shadow entry"
    first = params["strategies"][0]
    st = runner._st(first)
    st["sync_block_new_entries"] = True
    st["sync_block_reason"] = "positions_unavailable"
    st["sync_block_recoverable"] = True
    runner._sync_strategy(first)
    assert not st["sync_block_new_entries"], "recoverable clean sync should clear"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))
    if args.self_test:
        self_test()
        print("s23 self-test ok")
        return 0
    params = load_params()
    runner = S23Fixed4Runner(params)
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
