# -*- coding: utf-8 -*-
"""S23 loss-abort failure-to-progress shadow/live runner."""

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
from types import SimpleNamespace
from typing import Any

import pandas as pd

os.environ.setdefault("BOT_SUFFIX", "s23")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from live_data_fetcher import MT5DataManager
from live_executor import (
    HEDGING_MARGIN_MODE,
    REQUIRED_SHARED_ACCOUNT_COMMANDS,
    MT5Executor,
    ORDER_TYPE_BUY,
    ORDER_TYPE_SELL,
)
from live_safety import LiveSafetyOptions, clean_sync_block_if_flat, stale_signal_decision


UTC = timezone.utc
EXPECTED_S23_MAGIC = 200023
FLAT_AUTO_CLEAR_SYNC_REASONS = {
    "open_success_position_not_confirmed",
    "live_time_close_failed",
    "live_time_close_unconfirmed",
}
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


class S23LossAbortRunner:
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
            "version": 2,
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
                    "flat_clear_confirmation_count": 0,
                    "flat_clear_confirmation_reason": None,
                    "basket_peak_pnl_usd": None,
                    "pending_close_reason": None,
                    "pending_close_signal_bar": None,
                    "cooldown_until_utc": None,
                    "last_evaluated_bar": None,
                    "daily_realized_date_utc": None,
                    "daily_realized_pnl_usd": 0.0,
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
        if state.get("bot") != default["bot"] or state.get("strategy_id") != default["strategy_id"] or int(state.get("version", 0)) != int(default["version"]):
            logging.critical(
                "S23 state identity mismatch; refusing legacy or foreign state: bot=%s strategy_id=%s version=%s",
                state.get("bot"), state.get("strategy_id"), state.get("version"),
            )
            state = default
            for strat in self.params["strategies"]:
                st = state["strategies"][strat["id"]]
                st["sync_block_new_entries"] = True
                st["sync_block_reason"] = "state_identity_mismatch"
                st["sync_block_recoverable"] = False
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

    def _roll_daily_realized(self, strat: dict[str, Any], at_utc: datetime | pd.Timestamp | None = None) -> dict[str, Any]:
        st = self._st(strat)
        stamp = pd.Timestamp(at_utc if at_utc is not None else utc_now())
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        else:
            stamp = stamp.tz_convert("UTC")
        day = stamp.strftime("%Y-%m-%d")
        if st.get("daily_realized_date_utc") != day:
            st["daily_realized_date_utc"] = day
            st["daily_realized_pnl_usd"] = 0.0
        return st

    def _record_daily_realized(self, strat: dict[str, Any], pnl: float, at_utc: datetime | pd.Timestamp | None = None) -> None:
        st = self._roll_daily_realized(strat, at_utc)
        st["daily_realized_pnl_usd"] = float(st.get("daily_realized_pnl_usd", 0.0)) + float(pnl)

    def _new_basket_block_reason(self, strat: dict[str, Any], at_utc: datetime | pd.Timestamp) -> str | None:
        stamp = pd.Timestamp(at_utc)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        else:
            stamp = stamp.tz_convert("UTC")
        blocked_hours = {int(hour) for hour in self.params.get("new_basket_blocked_hours_utc", [])}
        if int(stamp.hour) in blocked_hours:
            return "new_basket_blocked_hour"
        st = self._roll_daily_realized(strat, stamp)
        limit = float(self.params.get("daily_realized_loss_limit_usd", 0.0))
        if limit > 0 and float(st.get("daily_realized_pnl_usd", 0.0)) <= -limit:
            return "daily_realized_loss_limit"
        return None

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
            if previous != reason:
                st["flat_clear_confirmation_count"] = 0
                st["flat_clear_confirmation_reason"] = None
                logging.error("S23 new entries blocked for %s: %s", strat["id"], reason)
            st["sync_block_new_entries"] = True
            st["sync_block_reason"] = reason
            st["sync_block_recoverable"] = bool(recoverable)
            st["sync_block_details"] = details or {}
            return
        if st.get("sync_block_new_entries"):
            logging.warning("S23 new-entry block cleared for %s after clean sync: %s", strat["id"], previous)
        st["sync_block_new_entries"] = False
        st["sync_block_reason"] = None
        st["sync_block_recoverable"] = False
        st["sync_block_details"] = {}
        st["flat_clear_confirmation_count"] = 0
        st["flat_clear_confirmation_reason"] = None

    @staticmethod
    def _side_from_record(record: Any) -> str:
        return "LONG" if int(getattr(record, "type", -1)) == ORDER_TYPE_BUY else "SHORT"

    def _owned_position(self, strat: dict[str, Any], record: Any) -> bool:
        return (
            str(getattr(record, "symbol", "")) == str(self.params.get("mt5_symbol", self.params["symbol"]))
            and int(getattr(record, "magic", -1)) == int(strat["magic"])
            and str(getattr(record, "comment", "") or "").startswith(str(strat["comment_prefix"]))
        )

    def _state_matches_live(self, strat: dict[str, Any], state_pos: dict[str, Any], live_pos: Any) -> bool:
        position_id = int(state_pos.get("position_identifier") or state_pos.get("ticket") or 0)
        live_position_id = int(getattr(live_pos, "identifier", 0) or getattr(live_pos, "ticket", 0))
        return (
            position_id > 0
            and position_id == live_position_id
            and str(state_pos.get("side")) == self._side_from_record(live_pos)
            and self._owned_position(strat, live_pos)
        )

    def _state_ownership_proven(self, strat: dict[str, Any], state_pos: dict[str, Any]) -> bool:
        return (
            int(state_pos.get("position_identifier") or state_pos.get("ticket") or 0) > 0
            and str(state_pos.get("owner_symbol") or "") == str(self.params.get("mt5_symbol", self.params["symbol"]))
            and int(state_pos.get("owner_magic") or -1) == int(strat["magic"])
            and str(state_pos.get("owner_comment") or "").startswith(str(strat["comment_prefix"]))
            and str(state_pos.get("side") or "") in {"LONG", "SHORT"}
        )

    def _clear_basket_state(self, strat: dict[str, Any], reason: str, signal_bar: str | None = None) -> None:
        st = self._st(strat)
        st["basket"] = []
        st["last_add_price"] = None
        st["basket_peak_pnl_usd"] = None
        st["pending_close_reason"] = None
        st["pending_close_signal_bar"] = None
        st["cooldown_until_bar"] = -1
        closed_bar = parse_ts(signal_bar)
        st["cooldown_until_utc"] = dt_text(closed_bar + pd.Timedelta(minutes=int(strat.get("cooldown", 0)))) if closed_bar is not None else None
        st["last_closed_at_utc"] = dt_text(utc_now())
        st["last_closed_reason"] = reason
        st["last_closed_signal_bar"] = signal_bar

    def connect_and_preflight(self) -> bool:
        namespace_error = self._ownership_namespace_error()
        if namespace_error:
            logging.critical("S23 ownership namespace invalid: %s", namespace_error)
            return False
        if any(self._st(strat).get("sync_block_reason") == "state_identity_mismatch" for strat in self.params.get("strategies", [])):
            logging.critical("S23 legacy/foreign state must be archived before this runner can start.")
            return False
        if not bool(self.params.get("enabled", True)):
            logging.info("S23 disabled by params.")
            return False
        if not self.dm.connect():
            logging.error("S23 EA bridge connect failed.")
            return False
        caps = self.executor.get_bridge_capabilities()
        logging.info("S23 bridge caps: %s", caps)
        if not caps:
            logging.critical("S23 bridge capability query failed.")
            return False
        expected_bridge = str(self.params.get("expected_bridge_name") or "BotBridge_s23")
        if str(caps.get("name") or "") != expected_bridge:
            logging.critical("S23 wrong bridge attached: got=%s expected=%s", caps.get("name"), expected_bridge)
            return False
        missing = REQUIRED_SHARED_ACCOUNT_COMMANDS - {str(x).upper() for x in caps.get("commands", set())}
        if missing:
            logging.critical("S23 bridge missing required commands: %s", sorted(missing))
            return False
        if self.live_enabled:
            account = self.executor.get_account_info()
            if account is None:
                logging.critical("S23 account execution metadata unavailable.")
                return False
            if bool(self.params.get("require_hedging_account", True)) and int(account.get("margin_mode", -1)) != HEDGING_MARGIN_MODE:
                logging.critical("S23 live trading requires a hedging account: mode=%s", account.get("margin_mode_name"))
                return False
        return True

    def _ownership_namespace_error(self) -> str | None:
        strategies = [row for row in self.params.get("strategies", []) if bool(row.get("enabled", True))]
        magics = [int(row.get("magic") or 0) for row in strategies]
        prefixes = [str(row.get("comment_prefix") or "") for row in strategies]
        if len(magics) != 1 or magics[0] != EXPECTED_S23_MAGIC:
            return f"invalid_magics={magics} expected={[EXPECTED_S23_MAGIC]}"
        if len(magics) != len(set(magics)):
            return f"duplicate_magics={magics}"
        if any(not prefix.startswith("s23_") for prefix in prefixes) or len(prefixes) != len(set(prefixes)):
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

    def _get_confirmed_close_deal(self, position_id: int, opened_at_epoch: int) -> Any:
        """Retry an explicit no-deal result with the bridge's bounded history window."""
        deal = self.executor.get_position_close_deal(position_id, opened_at_epoch)
        if deal is False and opened_at_epoch > 0:
            deal = self.executor.get_position_close_deal(position_id, 0)
        return deal

    def _sync_strategy(self, strat: dict[str, Any]) -> bool:
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        st = self._st(strat)
        positions = self.executor.get_positions(symbol, int(strat["magic"]))
        orders = self.executor.get_orders(symbol, int(strat["magic"]))
        if positions is None or orders is None:
            self._set_sync_block(strat, "positions_or_orders_unavailable", recoverable=True)
            return False
        unexpected = [record for record in [*positions, *orders] if not self._owned_position(strat, record)]
        if unexpected:
            self._set_sync_block(
                strat,
                "same_magic_unexpected_position_or_order",
                {"tickets": [int(record.ticket) for record in unexpected], "comments": [str(record.comment or "") for record in unexpected]},
                recoverable=False,
            )
            return False
        if clean_sync_block_if_flat(
            symbol_key=strat["id"],
            state=st,
            positions=positions,
            orders=orders,
            save_state=self._save_state,
            options=self.safety,
            audit=lambda event, reason, note: self._trade_row(event, strat, reason=reason, note=note),
            flat_auto_clear_reasons=FLAT_AUTO_CLEAR_SYNC_REASONS,
            confirm_position_absent=self.executor.confirm_position_absent,
            required_flat_confirmations=2,
        ):
            logging.info("S23 clean sync cleared: %s", strat["id"])
        if orders:
            self._set_sync_block(strat, "same_magic_unexpected_order", {"tickets": [int(o.ticket) for o in orders]}, recoverable=False)
            return False
        if not self.live_enabled:
            return True
        state_basket = list(st.get("basket") or [])
        if not state_basket and positions:
            self._set_sync_block(
                strat,
                "live_positions_without_state",
                {"tickets": [int(pos.ticket) for pos in positions]},
                recoverable=False,
            )
            return False
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
                    remaining_state.append(state_pos)
                    continue
                opened_at_epoch = max(0, int(state_pos.get("open_time_epoch") or 0) - 60)
                deal = self._get_confirmed_close_deal(position_id, opened_at_epoch)
                if deal is None:
                    self._set_sync_block(strat, "close_deal_query_unavailable", {"ticket": position_id}, recoverable=False)
                    return False
                if deal is False:
                    self._set_sync_block(strat, "close_deal_not_confirmed", {"ticket": position_id}, recoverable=False)
                    return False
                if int(deal.position_id) != position_id or str(deal.symbol) != symbol or not self._state_ownership_proven(strat, state_pos):
                    self._set_sync_block(
                        strat,
                        "close_deal_ownership_mismatch",
                        {"ticket": position_id, "deal_position_id": int(deal.position_id), "deal_magic": int(deal.magic), "deal_symbol": str(deal.symbol)},
                        recoverable=False,
                    )
                    return False
                confirmed_deals.append(deal)
            if confirmed_deals:
                reason = str(st.get("pending_close_reason") or "broker_or_external_close_confirmed")
                signal_bar = st.get("pending_close_signal_bar")
                confirmed_net = sum(float(deal.net_profit) for deal in confirmed_deals)
                st["basket"] = remaining_state
                self._record_daily_realized(strat, confirmed_net)
                self._trade_row("position_close_confirmed", strat, profit=confirmed_net, reason=reason, signal_bar_time=signal_bar)
                if not remaining_state:
                    self._clear_basket_state(strat, reason, signal_bar)
                    self._set_sync_block(strat, None)
                self._save_state()
                return True
        return True

    def _close_basket(self, strat: dict[str, Any], reason: str, price_row: pd.Series, pnl: float) -> None:
        st = self._st(strat)
        if self.live_enabled:
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
            st["pending_close_reason"] = reason
            st["pending_close_signal_bar"] = str(price_row.name)
            self._save_state()
            for pos in list(st["basket"]):
                ticket = int(pos.get("ticket") or 0)
                close_result = self.executor.close_position(ticket, int(self.params.get("deviation_points", 50)))
                if not close_result:
                    close_status = str(getattr(close_result, "status", "FAILED"))
                    block_reason = "live_time_close_unconfirmed" if close_status in {"MISSING_UNCONFIRMED", "MALFORMED_OK"} else "live_time_close_failed"
                    self._set_sync_block(strat, block_reason, {"ticket": ticket, "status": close_status}, recoverable=False)
                    self._save_state()
                    return
                pos["close_requested"] = True
            self._trade_row("basket_close_requested", strat, profit=round(float(pnl), 2), reason=reason, signal_bar_time=str(price_row.name))
            self._save_state()
            return
        self._trade_row("basket_close", strat, profit=round(float(pnl), 2), reason=reason, signal_bar_time=str(price_row.name))
        self._record_daily_realized(strat, pnl, price_row.name)
        self._clear_basket_state(strat, reason, str(price_row.name))
        self._save_state()

    def _open_entry(self, strat: dict[str, Any], side: str, price_row: pd.Series, info: Any, note: str = "") -> None:
        st = self._st(strat)
        if note != "reverse_after_stop" and st.get("last_closed_signal_bar") == str(price_row.name):
            self._trade_row("entry_skip", strat, reason="same_bar_reentry_after_close", signal_bar_time=str(price_row.name))
            return
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        digits = int(self.params.get("price_digits", 2))
        lot = float(strat.get("lot", self.params.get("default_lot", 0.01)))
        ask = float(getattr(info, "ask", price_row.get("AskOpen", price_row["Open"])))
        bid = float(getattr(info, "bid", price_row["Open"]))
        entry_price = normalize_price(ask if side == "LONG" else bid, digits)
        ticket = None
        confirmed = None
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
                error = str(getattr(self.executor, "last_order_error", None) or "UNKNOWN_OPEN_FAILURE")
            else:
                error = ""
            positions = self.executor.get_positions(symbol, int(strat["magic"]))
            if positions is None:
                self._set_sync_block(strat, "positions_unavailable_after_open", {"ticket": int(ticket or 0), "error": error}, recoverable=True)
                self._save_state()
                return
            owned = [pos for pos in positions if self._owned_position(strat, pos)]
            known_ids = {int(pos.get("position_identifier") or pos.get("ticket") or 0) for pos in st.get("basket", [])}
            new_owned = [pos for pos in owned if int(getattr(pos, "identifier", 0) or pos.ticket) not in known_ids]
            if ticket is not None:
                matches = [pos for pos in new_owned if int(pos.ticket) == int(ticket) or int(getattr(pos, "identifier", 0) or 0) == int(ticket)]
                if len(matches) != 1:
                    self._set_sync_block(strat, "open_success_position_not_confirmed", {"ticket": int(ticket)}, recoverable=False)
                    self._save_state()
                    return
                confirmed = matches[0]
            elif len(new_owned) == 1:
                confirmed = new_owned[0]
                ticket = int(confirmed.ticket)
            else:
                reason = "ambiguous_open_result_positions" if new_owned else "ambiguous_open_result"
                self._set_sync_block(strat, reason, {"tickets": [int(pos.ticket) for pos in new_owned], "error": error}, recoverable=False)
                self._save_state()
                return
            entry_price = float(confirmed.open_price)
        st["basket"].append(
            {
                "ticket": ticket,
                "position_identifier": int(getattr(confirmed, "identifier", 0) or ticket or 0) if confirmed is not None else 0,
                "side": side,
                "lot": lot,
                "entry_price": entry_price,
                "entry_time_utc": dt_text((parse_ts(price_row.name) or pd.Timestamp(utc_now())) + pd.Timedelta(minutes=1)),
                "open_time_epoch": int(getattr(confirmed, "open_time", 0) or 0) if confirmed is not None else 0,
                "owner_symbol": symbol,
                "owner_magic": int(strat["magic"]),
                "owner_comment": str(getattr(confirmed, "comment", "") or strat["comment_prefix"]) if confirmed is not None else str(strat["comment_prefix"]),
                "shadow": not self.live_enabled,
            }
        )
        if len(st["basket"]) == 1:
            st["basket_peak_pnl_usd"] = None
        st["last_add_price"] = entry_price
        st["last_signal_bar"] = str(price_row.name)
        self._trade_row("entry", strat, ticket=ticket or "", side=side, lot=lot, price=entry_price, signal_bar_time=str(price_row.name), note=note)
        self._save_state()

    def _monitor_open_basket(self, strat: dict[str, Any], info: Any, price_row: pd.Series, poll_time: datetime | None = None) -> bool:
        st = self._st(strat)
        if st.get("pending_close_reason"):
            return True
        if not st["basket"]:
            return False
        at_utc = poll_time or utc_now()
        bid = float(getattr(info, "bid", price_row["Close"]))
        ask = float(getattr(info, "ask", price_row.get("AskOpen", price_row["Open"])))
        pnl = self._basket_pnl(strat, bid, ask)
        entry_times = [parse_ts(pos.get("entry_time_utc")) for pos in st["basket"]]
        valid_entry_times = [ts for ts in entry_times if ts is not None]
        if not valid_entry_times:
            self._set_sync_block(strat, "state_entry_time_invalid", recoverable=False)
            self._save_state()
            return True
        held = max(0, int((at_utc - min(valid_entry_times)).total_seconds() // 60))
        previous_peak = st.get("basket_peak_pnl_usd")
        peak = float(pnl) if previous_peak is None else max(float(previous_peak), float(pnl))
        st["basket_peak_pnl_usd"] = peak
        reason = None
        if pnl >= float(strat["basket_target_usd"]):
            reason = "basket_target"
        elif pnl <= -float(strat["basket_stop_usd"]):
            reason = "basket_stop"
        elif int(strat.get("failure_to_progress_bars", 0)) > 0 and held >= int(strat["failure_to_progress_bars"]) and peak < float(strat.get("failure_to_progress_peak_usd", 0.0)):
            reason = "failure_to_progress"
        elif held >= int(strat["max_hold_bars"]):
            reason = "max_hold"
        if reason:
            close_row = price_row.copy()
            close_row.name = pd.Timestamp(at_utc)
            self._close_basket(strat, reason, close_row, pnl)
            return True
        return False

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
        poll_time = utc_now()
        if self._monitor_open_basket(strat, info, now, poll_time):
            return
        bid = float(getattr(info, "bid", now["Close"]))
        ask = float(getattr(info, "ask", now.get("AskOpen", now["Open"])))
        if st.get("last_evaluated_bar") == dt_text(now_bar):
            return
        st["last_evaluated_bar"] = dt_text(now_bar)
        if st.get("sync_block_new_entries"):
            self._trade_row("entry_skip", strat, reason=st.get("sync_block_reason"), note="sync_block")
            self._save_state()
            return
        side = self._signal(now, strat)
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
            guard_ratio = float(strat.get("add_profit_guard_ratio", 99.0))
            if self._basket_pnl(strat, bid, ask) >= float(strat["basket_target_usd"]) * guard_ratio:
                self._trade_row("entry_skip", strat, reason="add_profit_guard", signal_bar_time=str(now.name))
                return
        else:
            block_reason = self._new_basket_block_reason(strat, now_bar)
            if block_reason:
                self._trade_row("entry_skip", strat, reason=block_reason, signal_bar_time=str(now.name), note=f"daily_realized={self._st(strat).get('daily_realized_pnl_usd', 0.0)}")
                self._save_state()
                return
        self._open_entry(strat, side, now, info)

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
                if not bool(strat.get("enabled", True)) or not self._st(strat)["basket"]:
                    continue
                if not self._sync_strategy(strat):
                    self._save_state()
                    continue
                quote_time = utc_now()
                quote_row = pd.Series({"Open": float(info.bid), "Close": float(info.bid), "AskOpen": float(info.ask)}, name=pd.Timestamp(quote_time))
                self._monitor_open_basket(strat, info, quote_row, quote_time)
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
    def __init__(self, *, positions: list[Any] | None = None, orders: list[Any] | None = None, margin_mode: int = HEDGING_MARGIN_MODE):
        self.positions = [] if positions is None else positions
        self.orders = [] if orders is None else orders
        self.margin_mode = margin_mode
        self.last_order_error = None

    def get_bridge_capabilities(self) -> dict[str, Any]:
        return {"name": "BotBridge_s23", "commands": set(REQUIRED_SHARED_ACCOUNT_COMMANDS)}

    def get_account_info(self) -> dict[str, Any]:
        return {"margin_mode": self.margin_mode, "margin_mode_name": "RETAIL_HEDGING" if self.margin_mode == HEDGING_MARGIN_MODE else "RETAIL_NETTING"}

    def get_symbol_info(self, *_: Any) -> Any:
        return type("Info", (), {"bid": 2064.0, "ask": 2064.03})()

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
        return SimpleNamespace(position_id=position_id, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC, net_profit=0.0)

    def open_position(self, *_: Any, **__: Any) -> int:
        return 1

    def close_position(self, *_: Any, **__: Any) -> bool:
        return True


def load_params(path: str = PARAMS_FILE) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def self_test() -> None:
    configured_params = load_params()
    configured_strategy = configured_params["strategies"][0]
    assert int(configured_strategy["max_positions"]) == 2, "forward candidate must cap the basket at two positions"
    assert abs(float(configured_strategy["add_atr"]) - 0.65) < 1e-12, "forward candidate add distance must be 0.65 ATR30"
    assert abs(float(configured_strategy["add_profit_guard_ratio"]) - 0.30) < 1e-12, "adds must stop at 30% of basket target"
    assert configured_params["new_basket_blocked_hours_utc"] == [14], "14 UTC new baskets must be blocked"
    assert float(configured_params["daily_realized_loss_limit_usd"]) == 27.0, "daily realized loss limit must be 27 USD"
    params = json.loads(json.dumps(configured_params))
    params["live_trading_enabled"] = False
    params["shadow_forward_enabled"] = True
    params["new_basket_blocked_hours_utc"] = []
    params["safety"]["stale_signal_guard"] = False
    params["strategies"][0]["vol_min"] = 0.9
    runner = S23LossAbortRunner(params)
    runner.state = runner._default_state()
    assert runner._ownership_namespace_error() is None, "valid S23 ownership namespaces must pass"
    params["strategies"][0]["magic"] = 200022
    wrong_magic_runner = S23LossAbortRunner(params)
    assert "expected=[200023]" in str(wrong_magic_runner._ownership_namespace_error()), "wrong S23 magic must fail preflight"
    params["strategies"][0]["magic"] = EXPECTED_S23_MAGIC
    runner.dm = FakeDM()
    runner.executor = FakeExecutor()
    runner._save_state = lambda: None
    rows: list[tuple[str, str, str]] = []
    runner._trade_row = lambda event, strat, **kw: rows.append((event, strat["id"], str(kw.get("reason", ""))))
    runner.run_once()
    assert any(row[0] == "entry" for row in rows), "expected at least one shadow entry"
    strategy = params["strategies"][0]
    gate_params = json.loads(json.dumps(params))
    gate_params["new_basket_blocked_hours_utc"] = [14]
    gate_runner = S23LossAbortRunner(gate_params)
    gate_runner.state = gate_runner._default_state()
    gate_runner._record_daily_realized(strategy, -27.0, pd.Timestamp("2026-01-01T13:30:00Z"))
    assert gate_runner._new_basket_block_reason(strategy, pd.Timestamp("2026-01-01T13:45:00Z")) == "daily_realized_loss_limit"
    assert gate_runner._new_basket_block_reason(strategy, pd.Timestamp("2026-01-01T14:15:00Z")) == "new_basket_blocked_hour"
    assert gate_runner._new_basket_block_reason(strategy, pd.Timestamp("2026-01-02T13:15:00Z")) is None, "daily budget must reset on UTC day change"
    st = runner._st(strategy)
    st["sync_block_new_entries"] = True
    st["sync_block_reason"] = "positions_unavailable"
    st["sync_block_recoverable"] = True
    save_calls: list[bool] = []
    runner._save_state = lambda: save_calls.append(True)
    runner._sync_strategy(strategy)
    assert not st["sync_block_new_entries"], "recoverable clean sync should clear"
    assert save_calls, "recoverable clear must persist state"

    st["sync_block_new_entries"] = True
    st["sync_block_reason"] = "open_success_position_not_confirmed"
    st["sync_block_recoverable"] = False
    st["sync_block_details"] = {"ticket": 9001}
    runner._sync_strategy(strategy)
    assert st["sync_block_new_entries"], "high-risk open block requires two flat confirmations"
    runner._sync_strategy(strategy)
    assert not st["sync_block_new_entries"], "high-risk open block should clear after two proven-flat confirmations"

    foreign = SimpleNamespace(ticket=9100, identifier=9100, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC, comment="s22_foreign", type=ORDER_TYPE_BUY)
    runner.executor = FakeExecutor(positions=[foreign])
    assert not runner._sync_strategy(strategy), "same-magic foreign comment must block"
    assert st["sync_block_reason"] == "same_magic_unexpected_position_or_order"

    live_params = json.loads(json.dumps(params))
    live_params["live_trading_enabled"] = True
    live_params["shadow_forward_enabled"] = False
    live_runner = S23LossAbortRunner(live_params)
    live_runner.state = live_runner._default_state()
    live_runner.dm = FakeDM()
    live_runner.executor = FakeExecutor(margin_mode=0)
    assert not live_runner.connect_and_preflight(), "live S23 must reject netting accounts"

    confirmed_params = json.loads(json.dumps(live_params))
    confirmed_runner = S23LossAbortRunner(confirmed_params)
    confirmed_runner.state = confirmed_runner._default_state()
    confirmed_runner._save_state = lambda: None
    confirmed_strategy = confirmed_params["strategies"][0]
    owned = SimpleNamespace(
        ticket=1, identifier=7001, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
        comment="s23_loss_abort", type=ORDER_TYPE_BUY, volume=0.01,
        open_price=2064.03, open_time=1767272400,
    )
    confirmed_runner.executor = FakeExecutor(positions=[owned])
    sample_bars = add_features(FakeDM().get_historical_data(), float(params["point_size"]))
    sample_row = sample_bars.iloc[-1]
    confirmed_runner._open_entry(confirmed_strategy, "LONG", sample_row, confirmed_runner.executor.get_symbol_info("XAUUSD"))
    confirmed_state = confirmed_runner._st(confirmed_strategy)
    assert len(confirmed_state["basket"]) == 1 and confirmed_state["basket"][0]["position_identifier"] == 7001, "OPEN must persist broker-confirmed position ownership"

    ambiguous_runner = S23LossAbortRunner(confirmed_params)
    ambiguous_runner.state = ambiguous_runner._default_state()
    ambiguous_runner._save_state = lambda: None
    ambiguous_runner.executor = FakeExecutor(positions=[])
    ambiguous_runner._open_entry(confirmed_strategy, "LONG", sample_row, ambiguous_runner.executor.get_symbol_info("XAUUSD"))
    assert ambiguous_runner._st(confirmed_strategy)["sync_block_reason"] == "open_success_position_not_confirmed", "unconfirmed successful OPEN must fail closed"

    partial_runner = S23LossAbortRunner(confirmed_params)
    partial_runner.state = partial_runner._default_state()
    partial_runner._save_state = lambda: None
    live_remaining = SimpleNamespace(
        ticket=2, identifier=7002, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
        comment="s23_loss_abort", type=ORDER_TYPE_BUY, volume=0.01,
        open_price=2065.0, open_time=1767272460,
    )
    partial_runner.executor = FakeExecutor(positions=[live_remaining])
    partial_state = partial_runner._st(confirmed_strategy)
    partial_state["basket"] = [
        {"ticket": 1, "position_identifier": 7001, "side": "LONG", "lot": 0.01, "entry_price": 2064.0, "entry_time_utc": "2026-01-01T13:00:00Z", "open_time_epoch": 1767272400, "owner_symbol": "XAUUSD", "owner_magic": EXPECTED_S23_MAGIC, "owner_comment": "s23_loss_abort"},
        {"ticket": 2, "position_identifier": 7002, "side": "LONG", "lot": 0.01, "entry_price": 2065.0, "entry_time_utc": "2026-01-01T13:01:00Z", "open_time_epoch": 1767272460, "owner_symbol": "XAUUSD", "owner_magic": EXPECTED_S23_MAGIC, "owner_comment": "s23_loss_abort"},
    ]
    assert partial_runner._sync_strategy(confirmed_strategy), "partially completed basket close must reconcile owned tickets"
    assert [pos["position_identifier"] for pos in partial_state["basket"]] == [7002], "confirmed closed ticket must be removed without losing remaining owned state"

    class DelayedManualCloseExecutor(FakeExecutor):
        def __init__(self) -> None:
            super().__init__(positions=[live_remaining])
            self.history_windows: list[int] = []

        def get_position_close_deal(self, position_id: int, opened_at_epoch: int) -> Any:
            self.history_windows.append(opened_at_epoch)
            if opened_at_epoch > 0:
                return False
            return SimpleNamespace(position_id=position_id, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC, net_profit=-1.25)

    delayed_runner = S23LossAbortRunner(confirmed_params)
    delayed_runner.state = delayed_runner._default_state()
    delayed_runner._save_state = lambda: None
    delayed_runner._trade_row = lambda *_args, **_kwargs: None
    delayed_state = delayed_runner._st(confirmed_strategy)
    delayed_state["basket"] = [
        {"ticket": 1, "position_identifier": 7001, "side": "LONG", "lot": 0.01, "entry_price": 2064.0, "entry_time_utc": "2026-01-01T13:00:00Z", "open_time_epoch": 1767272400, "owner_symbol": "XAUUSD", "owner_magic": EXPECTED_S23_MAGIC, "owner_comment": "s23_loss_abort"},
        {"ticket": 2, "position_identifier": 7002, "side": "LONG", "lot": 0.01, "entry_price": 2065.0, "entry_time_utc": "2026-01-01T13:01:00Z", "open_time_epoch": 1767272460, "owner_symbol": "XAUUSD", "owner_magic": EXPECTED_S23_MAGIC, "owner_comment": "s23_loss_abort"},
    ]
    delayed_runner.executor = DelayedManualCloseExecutor()
    assert delayed_runner._sync_strategy(confirmed_strategy), "manual close must reconcile through bounded fallback history"
    assert delayed_runner.executor.history_windows == [1767272340, 0], "manual-close lookup must retry with the bounded fallback window"
    assert [pos["position_identifier"] for pos in delayed_state["basket"]] == [7002]
    assert not delayed_state["sync_block_new_entries"], "confirmed manual close must not leave a stale entry block"

    fail_runner = S23LossAbortRunner(params)
    fail_runner.state = fail_runner._default_state()
    fail_runner.executor = FakeExecutor()
    fail_runner._save_state = lambda: None
    fail_st = fail_runner._st(strategy)
    fail_st["basket"] = [{"ticket": None, "position_identifier": 0, "side": "LONG", "lot": 0.01, "entry_price": 2064.0, "entry_time_utc": "2026-01-01T13:00:00+00:00", "shadow": True}]
    fail_st["basket_peak_pnl_usd"] = 1.0
    bars = add_features(FakeDM().get_historical_data(), float(params["point_size"]))
    bars["spread_points"] = 3.0
    bars = bars.loc[bars.index <= pd.Timestamp("2026-01-01T13:11:00Z")]
    events: list[str] = []
    fail_runner._trade_row = lambda event, *_args, **kw: events.append(str(kw.get("reason") or event))
    fail_runner._run_strategy(strategy, bars, FakeExecutor().get_symbol_info("XAUUSD"))
    assert "failure_to_progress" in events, "failure-to-progress exit must fire after 10 bars without 3 USD peak"

    poll_runner = S23LossAbortRunner(params)
    poll_runner.state = poll_runner._default_state()
    poll_runner._save_state = lambda: None
    poll_events: list[str] = []
    poll_runner._trade_row = lambda event, *_args, **kw: poll_events.append(str(kw.get("reason") or event))
    poll_state = poll_runner._st(strategy)
    poll_state["basket"] = [{"ticket": None, "position_identifier": 0, "side": "LONG", "lot": 0.01, "entry_price": 2064.0, "entry_time_utc": "2026-01-01T13:00:00+00:00", "shadow": True}]
    poll_state["last_evaluated_bar"] = "2026-01-01T13:01:00+00:00"
    poll_row = pd.Series({"Open": 2075.0, "Close": 2075.0, "AskOpen": 2075.03}, name=pd.Timestamp("2026-01-01T13:01:00Z"))
    poll_info = type("Info", (), {"bid": 2075.0, "ask": 2075.03})()
    assert poll_runner._monitor_open_basket(strategy, poll_info, poll_row, pd.Timestamp("2026-01-01T13:01:05Z")), "poll-time TP must trigger without a new M1 bar"
    assert not poll_state["basket"] and "basket_target" in poll_events, "poll-time TP must close the shadow basket"

    no_bars_runner = S23LossAbortRunner(params)
    no_bars_runner.state = no_bars_runner._default_state()
    no_bars_runner.executor = FakeExecutor()
    no_bars_runner.dm = type("NoBarsDM", (), {"get_historical_data": lambda self, *_args, **_kwargs: None})()
    no_bars_runner._save_state = lambda: None
    no_bars_events: list[str] = []
    no_bars_runner._trade_row = lambda event, *_args, **kw: no_bars_events.append(str(kw.get("reason") or event))
    no_bars_state = no_bars_runner._st(strategy)
    no_bars_state["basket"] = [{"ticket": None, "position_identifier": 0, "side": "LONG", "lot": 0.01, "entry_price": 2050.0, "entry_time_utc": dt_text(utc_now()), "shadow": True}]
    no_bars_runner.run_once()
    assert not no_bars_state["basket"] and "basket_target" in no_bars_events, "open exposure must retain quote-based TP monitoring when M1 history is unavailable"


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
    runner = S23LossAbortRunner(params)
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
