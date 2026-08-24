from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pandas as pd

os.environ.setdefault("BOT_SUFFIX", "s26")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from live_data_fetcher import MT5DataManager
from live_executor import (
    HEDGING_MARGIN_MODE,
    REQUIRED_SHARED_ACCOUNT_COMMANDS,
    MT5Executor,
    ORDER_TYPE_BUY,
)
from live_safety import (
    LiveSafetyOptions,
    clean_sync_block_if_flat,
    clear_recoverable_sync_block_after_clean_sync,
    lot_contract_error,
    stale_signal_decision,
)
try:
    from live_config import MIN_LOT_OVERRIDES
except ImportError:
    MIN_LOT_OVERRIDES: dict[str, float] = {}
from protocol_v2_strategy import latest_signal


UTC = timezone.utc
BOT_SUFFIX = "s26"
EXPECTED_MAGIC = 200026
TIME_CLOSE_RETRYABLE_RETCODES = {"10018"}
STATE_SCHEMA_VERSION = 2
PREVIOUS_STRATEGY_IDS = {"PV2C520_C4535_CONT1_WINDOW60_H75_FORWARD_R2"}
PARAMS_FILE = os.path.join(SCRIPT_DIR, "s26_params.json")
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
STATE_DIR = os.path.join(SCRIPT_DIR, "state")
LOG_FILE = os.path.join(LOG_DIR, "s26_bot.log")
TRADE_LOG_FILE = os.path.join(LOG_DIR, "s26_trades.csv")
STATE_FILE = os.path.join(STATE_DIR, "s26_bot_state.json")
FLAT_AUTO_CLEAR_SYNC_REASONS = {
    "open_success_position_not_confirmed",
    "live_time_close_failed",
    "live_time_close_unconfirmed",
}
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
    "profit_bps",
    "reason",
    "signal_bar_time",
    "context_reference_time",
    "context_stale_seconds",
    "live",
    "note",
]


def utc_now() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(UTC))


def utc_timestamp(value: Any) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    try:
        timestamp = pd.Timestamp(value)
    except Exception:
        return None
    if pd.isna(timestamp):
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def timestamp_text(value: Any) -> str:
    timestamp = utc_timestamp(value)
    return timestamp.isoformat() if timestamp is not None else ""


def close_failure_retcode(status: str) -> str | None:
    for part in str(status).split("|"):
        if part.isdigit():
            return part
    return None


def atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def append_csv(path: str, row: dict[str, Any], fields: list[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in fields})


def safety_options(params: dict[str, Any]) -> LiveSafetyOptions:
    configured = dict(params.get("safety") or {})
    defaults = LiveSafetyOptions()
    return LiveSafetyOptions(
        **{
            field: configured.get(field, getattr(defaults, field))
            for field in LiveSafetyOptions.__dataclass_fields__
        }
    )


class ProtocolV2FixedHoldRunner:
    def __init__(self, params: dict[str, Any]):
        self.params = params
        self.strategy = dict(params["strategy"])
        self.magic = int(params["magic"])
        self.live_enabled = bool(params.get("live_trading_enabled", False))
        self.shadow_enabled = bool(params.get("shadow_forward_enabled", True))
        self.safety = safety_options(params)
        self.dm = MT5DataManager(self.safety)
        self.executor = MT5Executor()
        self.state = self._load_state()
        self.last_status_log = 0.0

    def _now(self) -> pd.Timestamp:
        return utc_now()

    def _default_state(self) -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "bot_suffix": BOT_SUFFIX,
            "strategy_id": self.strategy["id"],
            "position": None,
            "pending_entry": None,
            "last_evaluated_bar": None,
            "last_signal_bar": None,
            "last_close_time_utc": None,
            "sync_block_new_entries": False,
            "sync_block_reason": None,
            "sync_block_details": None,
            "sync_block_recoverable": False,
            "flat_clear_confirmation_count": 0,
            "flat_clear_confirmation_reason": None,
            "shadow_ticket_seq": -EXPECTED_MAGIC * 1000,
        }

    def _load_state(self) -> dict[str, Any]:
        baseline = self._default_state()
        if not os.path.exists(STATE_FILE):
            return baseline
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            return self._merge_loaded_state(loaded)
        except Exception as exc:
            baseline["sync_block_new_entries"] = True
            baseline["sync_block_reason"] = "state_load_failed"
            baseline["sync_block_details"] = {"error": str(exc)}
            return baseline

    def _merge_loaded_state(self, loaded: dict[str, Any]) -> dict[str, Any]:
        baseline = self._default_state()
        if loaded.get("bot_suffix") != BOT_SUFFIX:
            raise ValueError("state identity mismatch")
        loaded_strategy_id = str(loaded.get("strategy_id") or "")
        if loaded_strategy_id in PREVIOUS_STRATEGY_IDS:
            loaded = dict(loaded)
            loaded["migrated_from_strategy_id"] = loaded_strategy_id
            loaded["strategy_id"] = self.strategy["id"]
            loaded["schema_version"] = STATE_SCHEMA_VERSION
            loaded["pending_entry"] = None
            position = loaded.get("position")
            opened_at = int((position or {}).get("open_time_epoch") or 0)
            if position is not None and opened_at > 0:
                position = dict(position)
                actual_entry = pd.Timestamp(opened_at, unit="s", tz="UTC")
                position["entry_due_utc"] = timestamp_text(actual_entry)
                position["exit_due_utc"] = timestamp_text(
                    actual_entry + pd.Timedelta(minutes=int(self.strategy["hold_min"]))
                )
                loaded["position"] = position
        elif loaded_strategy_id != self.strategy["id"]:
            raise ValueError("state identity mismatch")
        baseline.update(loaded)
        return baseline

    def _save_state(self) -> None:
        atomic_write_json(STATE_FILE, self.state)

    def _trade_row(self, event: str, **kwargs: Any) -> None:
        row = {
            "timestamp_utc": timestamp_text(self._now()),
            "event": event,
            "strategy_id": self.strategy["id"],
            "symbol": self.params["symbol"],
            "mt5_symbol": self.params["mt5_symbol"],
            "live": self.live_enabled,
            **kwargs,
        }
        append_csv(TRADE_LOG_FILE, row, TRADE_FIELDS)

    def _set_sync_block(
        self,
        reason: str,
        details: dict[str, Any] | None = None,
        *,
        recoverable: bool = False,
    ) -> None:
        if recoverable and self.state.get("sync_block_new_entries") and not self.state.get("sync_block_recoverable"):
            logging.warning("S26 retained non-recoverable block: %s", self.state.get("sync_block_reason"))
            return
        self.state["sync_block_new_entries"] = True
        self.state["sync_block_reason"] = reason
        self.state["sync_block_details"] = details or {}
        self.state["sync_block_recoverable"] = bool(recoverable)
        self.state["flat_clear_confirmation_count"] = 0
        self.state["flat_clear_confirmation_reason"] = None
        self._trade_row("ERROR", reason=reason, note=json.dumps(details or {}, ensure_ascii=False, sort_keys=True))

    def _ownership_namespace_error(self) -> str | None:
        prefix = str(self.strategy.get("comment_prefix") or "")
        if self.magic != EXPECTED_MAGIC or int(self.params.get("expected_magic", 0)) != EXPECTED_MAGIC:
            return f"invalid_magic={self.magic} expected={EXPECTED_MAGIC}"
        if not prefix.startswith(f"{BOT_SUFFIX}_") or len(prefix) > 20:
            return f"invalid_comment_prefix={prefix}"
        if str(self.params.get("bot_suffix")) != BOT_SUFFIX:
            return f"invalid_bot_suffix={self.params.get('bot_suffix')}"
        return None

    def _owned_position(self, record: Any) -> bool:
        return (
            str(getattr(record, "symbol", "")) == str(self.params["mt5_symbol"])
            and int(getattr(record, "magic", -1)) == self.magic
            and str(getattr(record, "comment", "")).startswith(str(self.strategy["comment_prefix"]))
        )

    def _state_ownership_proven(self, position: dict[str, Any]) -> bool:
        return (
            str(position.get("owner_symbol")) == str(self.params["mt5_symbol"])
            and int(position.get("owner_magic", -1)) == self.magic
            and str(position.get("owner_comment", "")).startswith(str(self.strategy["comment_prefix"]))
        )

    def connect_and_preflight(self) -> bool:
        namespace_error = self._ownership_namespace_error()
        if namespace_error:
            logging.critical("S26 ownership namespace invalid: %s", namespace_error)
            return False
        if not self.shadow_enabled and not self.live_enabled:
            logging.critical("S26 neither shadow nor live mode is enabled")
            return False
        if not self.dm.connect():
            logging.critical("S26 bridge connection failed")
            return False
        capabilities = self.executor.get_bridge_capabilities()
        if capabilities is None:
            logging.critical("S26 bridge capability query failed")
            return False
        expected_bridge = str(self.params["expected_bridge_name"])
        if str(capabilities.get("name") or "") != expected_bridge:
            logging.critical("S26 wrong bridge: got=%s expected=%s", capabilities.get("name"), expected_bridge)
            return False
        missing = REQUIRED_SHARED_ACCOUNT_COMMANDS - {
            str(command).upper() for command in capabilities.get("commands", set())
        }
        if missing:
            logging.critical("S26 bridge missing commands: %s", sorted(missing))
            return False
        if self.live_enabled:
            account = self.executor.get_account_info()
            if account is None:
                logging.critical("S26 account metadata unavailable")
                return False
            if bool(self.params.get("require_hedging_account", True)) and int(account.get("margin_mode", -1)) != HEDGING_MARGIN_MODE:
                logging.critical("S26 live mode requires hedging account")
                return False
        synced = self._sync_position()
        if synced and self.state.get("migrated_from_strategy_id"):
            self._save_state()
        return synced

    def _sync_position(self) -> bool:
        positions = self.executor.get_positions(str(self.params["mt5_symbol"]), self.magic)
        if positions is None:
            self._set_sync_block("positions_unavailable", recoverable=True)
            self._save_state()
            return False
        queried_orders = self.executor.get_orders(str(self.params["mt5_symbol"]), self.magic)
        orders_available = queried_orders is not None
        orders = list(queried_orders or [])
        if not orders_available:
            self._set_sync_block("orders_unavailable", recoverable=True)
            self._save_state()
        unexpected_positions = [record for record in positions if not self._owned_position(record)]
        if unexpected_positions:
            self._set_sync_block(
                "same_magic_unexpected_position_or_order",
                {"tickets": [int(record.ticket) for record in unexpected_positions]},
            )
            self._save_state()
            return False
        unexpected_orders = [record for record in orders if not self._owned_position(record)]
        if unexpected_orders:
            self._set_sync_block(
                "same_magic_unexpected_order",
                {"tickets": [int(record.ticket) for record in unexpected_orders]},
            )
            self._save_state()
        clean_sync_block_if_flat(
            symbol_key=self.strategy["id"],
            state=self.state,
            positions=positions,
            orders=orders if orders_available else None,
            save_state=self._save_state,
            options=self.safety,
            audit=lambda _symbol, event, reason: self._trade_row(event, reason=reason),
            flat_auto_clear_reasons=FLAT_AUTO_CLEAR_SYNC_REASONS,
            confirm_position_absent=self.executor.confirm_position_absent,
            required_flat_confirmations=2,
        )
        if orders and not unexpected_orders:
            self._set_sync_block("same_magic_unexpected_order", {"tickets": [int(order.ticket) for order in orders]})
            self._save_state()
        if not self.live_enabled:
            return True
        state_position = self.state.get("position")
        if state_position is None:
            if positions:
                self._set_sync_block("live_position_without_state", {"tickets": [int(position.ticket) for position in positions]})
                self._save_state()
                return False
            return not bool(self.state.get("sync_block_new_entries"))
        if len(positions) == 1:
            live_position = positions[0]
            expected_identifier = int(state_position.get("position_identifier") or state_position.get("ticket") or 0)
            actual_identifier = int(getattr(live_position, "identifier", 0) or live_position.ticket)
            expected_side = str(state_position.get("side"))
            actual_side = "LONG" if int(live_position.type) == ORDER_TYPE_BUY else "SHORT"
            if (
                not self._owned_position(live_position)
                or expected_identifier != actual_identifier
                or expected_side != actual_side
            ):
                self._set_sync_block("state_position_ownership_mismatch", {"ticket": int(live_position.ticket)})
                self._save_state()
                return False
            if orders_available and not orders:
                clear_recoverable_sync_block_after_clean_sync(
                    symbol_key=self.strategy["id"],
                    state=self.state,
                    save_state=self._save_state,
                    options=self.safety,
                    audit=lambda _symbol, event, reason: self._trade_row(event, reason=reason),
                )
            return True
        if len(positions) > 1:
            self._set_sync_block("multiple_owned_positions", {"tickets": [int(position.ticket) for position in positions]})
            self._save_state()
            return False
        position_id = int(state_position.get("position_identifier") or state_position.get("ticket") or 0)
        opened_at_epoch = max(0, int(state_position.get("open_time_epoch") or 0) - 60)
        close_deal = self._get_confirmed_close_deal(position_id, opened_at_epoch)
        if close_deal is None:
            self._set_sync_block("close_deal_query_unavailable", {"ticket": position_id})
            self._save_state()
            return False
        if close_deal is False:
            self._set_sync_block("close_deal_not_confirmed", {"ticket": position_id})
            self._save_state()
            return False
        if (
            int(close_deal.position_id) != position_id
            or str(close_deal.symbol) != str(self.params["mt5_symbol"])
            or not self._state_ownership_proven(state_position)
        ):
            self._set_sync_block("close_deal_ownership_mismatch", {"ticket": position_id})
            self._save_state()
            return False
        entry_price = float(state_position["entry_price"])
        close_price = float(close_deal.price)
        profit_bps = (close_price / entry_price - 1.0) * 10000.0
        self._trade_row(
            "position_close_deal",
            ticket=position_id,
            side="LONG",
            lot=state_position.get("lot"),
            price=close_price,
            profit_bps=profit_bps,
            reason=state_position.get("pending_close_reason") or "broker_or_external_close_confirmed",
            signal_bar_time=state_position.get("signal_bar_time"),
            note=f"deal={int(close_deal.deal)} net_profit={float(close_deal.net_profit)}",
        )
        self.state["last_close_time_utc"] = timestamp_text(self._now())
        self.state["position"] = None
        self._save_state()
        return True

    def _get_confirmed_close_deal(self, position_id: int, opened_at_epoch: int) -> Any:
        close_deal = self.executor.get_position_close_deal(position_id, opened_at_epoch)
        if close_deal is False and opened_at_epoch > 0:
            close_deal = self.executor.get_position_close_deal(position_id, 0)
        return close_deal

    def _get_closed_m1(self, symbol: str) -> pd.DataFrame | None:
        bars = self.dm.get_historical_data(
            symbol,
            int(self.params.get("m1_timeframe", 1)),
            int(self.params["history_bars"]),
            str(self.params.get("broker_timezone", "UTC")),
            drop_latest=True,
        )
        if bars is None or bars.empty:
            return None
        bars = bars.sort_index()
        if bool(self.params.get("require_midpoint_close", True)):
            if "MidClose" not in bars.columns or bars["MidClose"].isna().any():
                self._trade_row("ERROR", reason="midpoint_close_unavailable", note=f"symbol={symbol}")
                return None
            bars = bars.copy()
            bars["Close"] = bars["MidClose"].astype(float)
        return bars

    def _next_shadow_ticket(self) -> int:
        ticket = int(self.state.get("shadow_ticket_seq") or -EXPECTED_MAGIC * 1000)
        self.state["shadow_ticket_seq"] = ticket - 1
        return ticket

    def _open_entry(self, info: Any, signal: dict[str, Any]) -> bool:
        signal_bar = utc_timestamp(signal["bar_time"])
        if signal_bar is None:
            self._set_sync_block("signal_bar_time_invalid", recoverable=True)
            self._save_state()
            return False
        requested_at = self._now()
        lot = float(self.strategy["lot"])
        entry_price = float(info.ask)
        ticket: int | None = None
        confirmed = None
        if self.live_enabled:
            before = self.executor.get_positions(str(self.params["mt5_symbol"]), self.magic)
            if before is None:
                self._set_sync_block("positions_unavailable_before_open", recoverable=True)
                self._save_state()
                return False
            known_ids = {int(getattr(position, "identifier", 0) or position.ticket) for position in before}
            ticket = self.executor.open_position(
                str(self.params["mt5_symbol"]),
                ORDER_TYPE_BUY,
                lot,
                0.0,
                0.0,
                deviation=int(self.params.get("deviation_points", 50)),
                magic=self.magic,
                comment=str(self.strategy["comment_prefix"]),
                digits=int(self.params["price_digits"]),
            )
            error = str(getattr(self.executor, "last_order_error", None) or "")
            after = self.executor.get_positions(str(self.params["mt5_symbol"]), self.magic)
            if after is None:
                self._set_sync_block("positions_unavailable_after_open", {"ticket": int(ticket or 0), "error": error}, recoverable=True)
                self._save_state()
                return False
            new_owned = [
                position
                for position in after
                if self._owned_position(position)
                and int(getattr(position, "identifier", 0) or position.ticket) not in known_ids
            ]
            matches = [
                position
                for position in new_owned
                if ticket is not None
                and (int(position.ticket) == int(ticket) or int(getattr(position, "identifier", 0) or 0) == int(ticket))
            ]
            if len(matches) == 1:
                confirmed = matches[0]
            elif ticket is None and len(new_owned) == 1:
                confirmed = new_owned[0]
                ticket = int(confirmed.ticket)
            else:
                self._set_sync_block(
                    "open_success_position_not_confirmed",
                    {"ticket": int(ticket or 0), "new_tickets": [int(position.ticket) for position in new_owned], "error": error},
                )
                self._save_state()
                return False
            entry_price = float(confirmed.open_price)
        else:
            ticket = self._next_shadow_ticket()
        opened_at_epoch = int(getattr(confirmed, "open_time", 0) or 0)
        actual_entry = pd.Timestamp(opened_at_epoch, unit="s", tz="UTC") if opened_at_epoch > 0 else requested_at
        close_due = actual_entry + pd.Timedelta(minutes=int(self.strategy["hold_min"]))
        self.state["position"] = {
            "ticket": ticket,
            "position_identifier": int(getattr(confirmed, "identifier", 0) or ticket or 0),
            "side": "LONG",
            "lot": lot,
            "entry_price": entry_price,
            "entry_due_utc": timestamp_text(actual_entry),
            "exit_due_utc": timestamp_text(close_due),
            "signal_bar_time": timestamp_text(signal_bar),
            "open_time_epoch": opened_at_epoch,
            "owner_symbol": str(self.params["mt5_symbol"]),
            "owner_magic": self.magic,
            "owner_comment": str(getattr(confirmed, "comment", "") or self.strategy["comment_prefix"]),
            "shadow": not self.live_enabled,
            "pending_close_reason": None,
            "close_requested": False,
        }
        self.state["pending_entry"] = None
        self.state["last_signal_bar"] = timestamp_text(signal_bar)
        self._trade_row(
            "entry",
            ticket=ticket,
            side="LONG",
            lot=lot,
            price=entry_price,
            reason="protocol_v2_signal",
            signal_bar_time=timestamp_text(signal_bar),
            context_reference_time=timestamp_text(signal.get("context_reference_time")),
            context_stale_seconds=signal.get("context_stale_seconds", ""),
            note=json.dumps(
                {
                    key: value
                    for key, value in signal.items()
                    if key not in {"bar_time", "context_reference_time", "context_reference_bar_start"}
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        self._save_state()
        return True

    def _start_pending_entry(self, info: Any, signal: dict[str, Any]) -> bool:
        signal_bar = utc_timestamp(signal.get("bar_time"))
        reference_ask = float(getattr(info, "ask", 0.0) or 0.0)
        confirmation = dict(self.strategy["entry_confirmation"])
        if signal_bar is None or reference_ask <= 0.0:
            self._set_sync_block("entry_confirmation_reference_invalid", recoverable=True)
            self._save_state()
            return False
        decision_due = signal_bar + pd.Timedelta(minutes=1)
        expires_at = decision_due + pd.Timedelta(seconds=int(confirmation["window_seconds"]))
        now = self._now()
        if now > expires_at:
            self._trade_row("entry_skip", reason="entry_confirmation_expired", signal_bar_time=timestamp_text(signal_bar))
            self._save_state()
            return False
        continuation_bps = float(confirmation["continuation_bps"])
        trigger_ask = reference_ask * (1.0 + continuation_bps / 10000.0)
        self.state["pending_entry"] = {
            "signal": {
                "bar_time": timestamp_text(signal_bar),
                "side": str(signal.get("side") or "LONG"),
                "eligible": True,
                "reason": str(signal.get("reason") or "signal"),
                "context_reference_time": timestamp_text(signal.get("context_reference_time")),
                "context_stale_seconds": float(signal.get("context_stale_seconds") or 0.0),
            },
            "reference_ask": reference_ask,
            "trigger_ask": trigger_ask,
            "reference_time_utc": timestamp_text(now),
            "decision_due_utc": timestamp_text(decision_due),
            "expires_utc": timestamp_text(expires_at),
        }
        self.state["last_signal_bar"] = timestamp_text(signal_bar)
        self._trade_row(
            "entry_pending",
            reason="continuation_confirmation_wait",
            signal_bar_time=timestamp_text(signal_bar),
            price=reference_ask,
            note=f"trigger_ask={trigger_ask:.8f} expires={timestamp_text(expires_at)}",
        )
        self._save_state()
        return True

    def _process_pending_entry(self, info: Any) -> bool:
        pending = self.state.get("pending_entry")
        if pending is None:
            return False
        expires_at = utc_timestamp(pending.get("expires_utc"))
        trigger_ask = float(pending.get("trigger_ask") or 0.0)
        if expires_at is None or trigger_ask <= 0.0:
            self._set_sync_block("pending_entry_invalid")
            self._save_state()
            return True
        now = self._now()
        if now > expires_at:
            signal = dict(pending.get("signal") or {})
            self.state["pending_entry"] = None
            self._trade_row(
                "entry_skip",
                reason="entry_confirmation_expired",
                signal_bar_time=timestamp_text(signal.get("bar_time")),
                price=float(getattr(info, "ask", 0.0) or 0.0),
            )
            self._save_state()
            return True
        if float(getattr(info, "ask", 0.0) or 0.0) < trigger_ask:
            return True
        self._open_entry(info, dict(pending["signal"]))
        return True

    def _time_close_spread_action(self, position: dict[str, Any], info: Any, exit_due: pd.Timestamp) -> str:
        if not bool(self.params.get("time_close_spread_guard_enabled", True)):
            return "close"
        tick_time_msc = int(getattr(info, "tick_time_msc", 0) or 0)
        quote_time = pd.Timestamp(tick_time_msc, unit="ms", tz="UTC") if tick_time_msc > 0 else None
        last_quote_time = utc_timestamp(position.get("time_close_last_quote_utc"))
        if quote_time is None or quote_time < exit_due or (last_quote_time is not None and quote_time <= last_quote_time):
            if not bool(position.get("time_close_quote_wait_logged", False)):
                position["time_close_quote_wait_logged"] = True
                self._trade_row(
                    "DEFER",
                    ticket=position.get("ticket"),
                    side=position.get("side"),
                    reason="time_close_wait_fresh_quote",
                    note=f"exit_due={timestamp_text(exit_due)} quote_time={timestamp_text(quote_time) if quote_time is not None else 'missing'}",
                )
            return "defer"
        position["time_close_last_quote_utc"] = timestamp_text(quote_time)
        position["time_close_quote_wait_logged"] = False
        bid = float(getattr(info, "bid", 0.0) or 0.0)
        ask = float(getattr(info, "ask", 0.0) or 0.0)
        point_size = float(self.params.get("point_size", 0.0) or 0.0)
        spread_cap = float(self.strategy.get("max_time_close_spread_points", 0.0) or 0.0)
        if bid <= 0 or ask < bid or point_size <= 0 or spread_cap <= 0:
            self._set_sync_block(
                "time_close_quote_contract_invalid",
                {"bid": bid, "ask": ask, "point_size": point_size, "spread_cap": spread_cap},
                recoverable=True,
            )
            return "defer"
        spread_points = (ask - bid) / point_size
        defer_started = utc_timestamp(position.get("time_close_spread_defer_started_at"))
        max_defer_minutes = max(0.0, float(self.params.get("time_close_max_defer_minutes", 30)))
        stable_required = max(1, int(self.params.get("time_close_spread_stable_polls", 3)))
        if spread_points > spread_cap:
            if defer_started is None:
                defer_started = quote_time
                position["time_close_spread_defer_started_at"] = timestamp_text(quote_time)
                position["time_close_spread_stable_count"] = 0
                position["time_close_spread_timeout_logged"] = False
                self._trade_row(
                    "DEFER",
                    ticket=position.get("ticket"),
                    side=position.get("side"),
                    reason="time_close_spread_high",
                    note=f"spread_points={spread_points:.2f} cap={spread_cap:.2f} max_defer_minutes={max_defer_minutes:.0f}",
                )
            else:
                position["time_close_spread_stable_count"] = 0
        elif defer_started is None:
            return "close"
        position["time_close_spread_last_points"] = spread_points
        if defer_started is not None and quote_time >= defer_started + pd.Timedelta(minutes=max_defer_minutes):
            if not bool(position.get("time_close_spread_timeout_logged", False)):
                position["time_close_spread_timeout_logged"] = True
                self._trade_row(
                    "DEFER_TIMEOUT",
                    ticket=position.get("ticket"),
                    side=position.get("side"),
                    reason="time_close_spread_timeout",
                    note=f"spread_points={spread_points:.2f} cap={spread_cap:.2f} max_defer_minutes={max_defer_minutes:.0f}",
                )
            return "close_timeout"
        if spread_points > spread_cap:
            return "defer"
        stable_count = int(position.get("time_close_spread_stable_count", 0)) + 1
        position["time_close_spread_stable_count"] = stable_count
        if stable_count < stable_required:
            return "defer"
        self._trade_row(
            "RESUME",
            ticket=position.get("ticket"),
            side=position.get("side"),
            reason="time_close_spread_settled",
            note=f"spread_points={spread_points:.2f} cap={spread_cap:.2f} stable_polls={stable_count}",
        )
        return "close_settled"

    def _handle_time_exit(self, info: Any) -> bool:
        position = self.state.get("position")
        if position is None:
            return False
        if bool(position.get("close_requested")):
            return True
        exit_due = utc_timestamp(position.get("exit_due_utc"))
        if exit_due is None:
            self._set_sync_block("exit_due_invalid", {"ticket": int(position.get("ticket") or 0)})
            self._save_state()
            return True
        if self._now() < exit_due:
            return False
        wall_now = self._now()
        retry_after = utc_timestamp(position.get("time_close_retry_after_utc"))
        if retry_after is not None and wall_now < retry_after:
            return True
        if self._time_close_spread_action(position, info, exit_due) == "defer":
            self._save_state()
            return True
        ticket = int(position.get("ticket") or 0)
        close_price = float(info.bid)
        profit_bps = (close_price / float(position["entry_price"]) - 1.0) * 10000.0
        hold_reason = f"fixed_hold_{int(self.strategy['hold_min'])}m"
        if self.live_enabled:
            live_position = self.executor.get_position(ticket)
            position_id = int(position.get("position_identifier") or ticket)
            if (
                live_position is None
                or live_position is False
                or not self._owned_position(live_position)
                or int(getattr(live_position, "identifier", 0) or live_position.ticket) != position_id
            ):
                self._set_sync_block("state_ticket_unowned_or_foreign", {"ticket": ticket, "position_identifier": position_id})
                self._save_state()
                return True
            position["pending_close_reason"] = hold_reason
            self._save_state()
            close_result = self.executor.close_position(ticket, int(self.params.get("deviation_points", 50)))
            if not close_result:
                status = str(getattr(close_result, "status", "FAILED"))
                if close_failure_retcode(status) in TIME_CLOSE_RETRYABLE_RETCODES:
                    retry_seconds = max(1.0, float(self.params.get("time_close_market_closed_retry_seconds", 60)))
                    position["time_close_retry_after_utc"] = timestamp_text(wall_now + pd.Timedelta(seconds=retry_seconds))
                    position["time_close_last_retry_status"] = status
                    self._trade_row(
                        "DEFER",
                        ticket=ticket,
                        side="LONG",
                        reason="time_close_market_closed_retry",
                        note=f"status={status} retry_seconds={retry_seconds:.0f}",
                    )
                    self._save_state()
                    return True
                reason = "live_time_close_unconfirmed" if status in {"MISSING_UNCONFIRMED", "MALFORMED_OK"} else "live_time_close_failed"
                self._set_sync_block(reason, {"ticket": ticket, "status": status})
                self._save_state()
                return True
            position["close_requested"] = True
            self._trade_row(
                "position_close_requested",
                ticket=ticket,
                side="LONG",
                lot=position.get("lot"),
                price=close_price,
                profit_bps=profit_bps,
                reason=hold_reason,
                signal_bar_time=position.get("signal_bar_time"),
            )
            self._save_state()
            return True
        self._trade_row(
            "position_close",
            ticket=ticket,
            side="LONG",
            lot=position.get("lot"),
            price=close_price,
            profit_bps=profit_bps,
            reason=hold_reason,
            signal_bar_time=position.get("signal_bar_time"),
        )
        self.state["position"] = None
        self.state["last_close_time_utc"] = timestamp_text(self._now())
        self._save_state()
        return True

    def run_once(self) -> None:
        info = self.executor.get_symbol_info(str(self.params["mt5_symbol"]))
        if info is None:
            self._set_sync_block("symbol_info_failed", recoverable=True)
            self._save_state()
            return
        if not self._sync_position():
            return
        if self._handle_time_exit(info):
            return
        if self.state.get("position") is not None:
            if self.state.get("pending_entry") is not None:
                self.state["pending_entry"] = None
                self._save_state()
            return
        if self.state.get("pending_entry") is not None:
            pending = dict(self.state.get("pending_entry") or {})
            signal = dict(pending.get("signal") or {})
            self.state["pending_entry"] = None
            self._trade_row(
                "entry_skip",
                reason="retired_continuation_pending_cancelled",
                signal_bar_time=timestamp_text(signal.get("bar_time")),
            )
            self._save_state()
        if self.state.get("sync_block_new_entries"):
            return
        mt5_symbol = str(self.params["mt5_symbol"])
        lot_error = lot_contract_error(
            float(self.strategy["lot"]),
            float(info.volume_min),
            float(info.volume_max),
            float(info.volume_step),
            float(MIN_LOT_OVERRIDES.get(mt5_symbol, 0.0)),
        )
        if lot_error:
            self._set_sync_block(
                "invalid_lot_contract",
                {"symbol": mt5_symbol, "error": lot_error},
                recoverable=False,
            )
            self._save_state()
            return
        target_bars = self._get_closed_m1(str(self.params["mt5_symbol"]))
        if target_bars is None or len(target_bars) < int(self.strategy["minimum_bars"]):
            self._trade_row("entry_skip", reason="target_m1_unavailable")
            return
        context_bars = None
        context_symbol = self.strategy.get("context_mt5_symbol")
        if context_symbol:
            context_bars = self._get_closed_m1(str(context_symbol))
            if context_bars is None or len(context_bars) < int(self.strategy["minimum_bars"]):
                self._trade_row("entry_skip", reason="context_m1_unavailable")
                return
        signal = latest_signal(target_bars, context_bars, self.strategy)
        signal_bar = utc_timestamp(signal.get("bar_time"))
        if signal_bar is None:
            self._set_sync_block("signal_bar_time_invalid", recoverable=True)
            self._save_state()
            return
        signal_bar_text = timestamp_text(signal_bar)
        if self.state.get("last_evaluated_bar") == signal_bar_text:
            return
        self.state["last_evaluated_bar"] = signal_bar_text
        stale_decision = stale_signal_decision(
            signal_bar,
            timeframe_hours=1.0 / 60.0,
            max_delay_minutes=float(self.params["max_signal_delay_seconds"]) / 60.0,
            options=self.safety,
            now_utc=self._now(),
        )
        if stale_decision.stale:
            self._trade_row("decision_receipt", reason="not_evaluated_stale_signal", signal_bar_time=signal_bar_text)
            self._trade_row(
                "entry_skip",
                reason="stale_signal_skip",
                signal_bar_time=signal_bar_text,
                note=f"entry_due={stale_decision.entry_due_utc} latest={stale_decision.latest_allowed_utc} now={stale_decision.now_utc}",
            )
            self._save_state()
            return
        if not bool(signal.get("eligible")):
            decision_reason = "not_evaluated_context_stale" if signal.get("reason") == "context_stale_fail_closed" else "no_signal"
            self._trade_row("decision_receipt", reason=decision_reason, signal_bar_time=signal_bar_text)
            if signal.get("reason") == "context_stale_fail_closed":
                self._trade_row(
                    "entry_skip",
                    reason="context_stale_fail_closed",
                    signal_bar_time=signal_bar_text,
                    context_reference_time=timestamp_text(signal.get("context_reference_time")),
                    context_stale_seconds=signal.get("context_stale_seconds"),
                )
            self._save_state()
            return
        self._trade_row("decision_receipt", reason="signal", signal_bar_time=signal_bar_text)
        last_close = utc_timestamp(self.state.get("last_close_time_utc"))
        entry_due = signal_bar + pd.Timedelta(minutes=1)
        if last_close is not None and entry_due <= last_close:
            self._trade_row("entry_skip", reason="same_direction_reentry_after_close_skip", signal_bar_time=signal_bar_text)
            self._save_state()
            return
        self._open_entry(info, signal)

    def log_status(self) -> None:
        now = time.time()
        if now - self.last_status_log < float(self.params.get("status_log_interval_seconds", 60)):
            return
        logging.info(
            "S26 status live=%s shadow=%s active=%s blocked=%s reason=%s",
            self.live_enabled,
            self.shadow_enabled,
            self.state.get("position") is not None,
            self.state.get("sync_block_new_entries"),
            self.state.get("sync_block_reason"),
        )
        self.last_status_log = now


class FakeExecutor:
    def __init__(self) -> None:
        self.positions: list[Any] = []
        self.orders: list[Any] = []
        self.orders_available = True
        self.close_deal_results: list[Any] = []
        self.close_deal_calls: list[tuple[int, int]] = []
        self.next_open_time = int(pd.Timestamp("2026-08-21 10:01:23", tz="UTC").timestamp())
        self.tick_time_msc = self.next_open_time * 1000

    def get_symbol_info(self, _symbol: str) -> Any:
        return SimpleNamespace(
            bid=28646.45,
            ask=28648.97,
            tick_time_msc=self.tick_time_msc,
            volume_min=0.05,
            volume_max=500.0,
            volume_step=0.01,
        )

    def get_positions(self, _symbol: str, _magic: int) -> list[Any]:
        return list(self.positions)

    def get_orders(self, _symbol: str, _magic: int) -> list[Any] | None:
        return list(self.orders) if self.orders_available else None

    def open_position(
        self,
        symbol: str,
        order_type: int,
        lot: float,
        _sl: float,
        _tp: float,
        *,
        deviation: int,
        magic: int,
        comment: str,
        digits: int,
    ) -> int:
        del lot, deviation, digits
        ticket = 26001
        self.positions.append(
            SimpleNamespace(
                symbol=symbol,
                magic=magic,
                comment=comment,
                ticket=ticket,
                identifier=ticket,
                type=order_type,
                open_price=28651.90,
                open_time=self.next_open_time,
            )
        )
        return ticket

    def confirm_position_absent(self, _ticket: int) -> bool:
        return True

    def get_position_close_deal(self, position_id: int, opened_at_epoch: int) -> Any:
        self.close_deal_calls.append((position_id, opened_at_epoch))
        return self.close_deal_results.pop(0)


def load_params(path: str = PARAMS_FILE) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def self_test() -> None:
    params = load_params()
    assert params["live_trading_enabled"] is True
    assert params["shadow_forward_enabled"] is False
    test_params = json.loads(json.dumps(params))
    test_params["live_trading_enabled"] = False
    test_params["shadow_forward_enabled"] = True
    runner = ProtocolV2FixedHoldRunner(test_params)
    runner.state = runner._default_state()
    runner.executor = FakeExecutor()
    runner._save_state = lambda: None
    events: list[tuple[str, dict[str, Any]]] = []
    runner._trade_row = lambda event, **kwargs: events.append((event, kwargs))
    signal_bar = pd.Timestamp("2026-08-21 10:00:00", tz="UTC")
    entry_time = pd.Timestamp("2026-08-21 10:01:05", tz="UTC")
    runner._now = lambda: entry_time
    reference_info = runner.executor.get_symbol_info("USTEC")
    assert runner._open_entry(reference_info, {"bar_time": signal_bar, "side": "LONG", "eligible": True})
    position = runner.state["position"]
    assert position is not None
    shadow_position = dict(position)
    expected_exit_due = entry_time + pd.Timedelta(minutes=int(runner.strategy["hold_min"]))
    assert runner.state["pending_entry"] is None
    assert position["entry_due_utc"] == timestamp_text(entry_time)
    assert position["exit_due_utc"] == timestamp_text(expected_exit_due)
    migrated = runner._default_state()
    migrated["strategy_id"] = "PV2C520_C4535_CONT1_WINDOW60_H75_FORWARD_R2"
    migrated["pending_entry"] = {"signal": {"bar_time": timestamp_text(signal_bar)}}
    migrated["position"] = dict(position, open_time_epoch=int(entry_time.timestamp()))
    migrated = runner._merge_loaded_state(migrated)
    assert migrated["migrated_from_strategy_id"] == "PV2C520_C4535_CONT1_WINDOW60_H75_FORWARD_R2"
    assert migrated["strategy_id"] == runner.strategy["id"]
    assert migrated["pending_entry"] is None
    assert migrated["position"]["exit_due_utc"] == timestamp_text(expected_exit_due)
    live_runner = ProtocolV2FixedHoldRunner(test_params)
    live_runner.state = live_runner._default_state()
    live_runner.live_enabled = True
    live_runner.executor = FakeExecutor()
    live_runner._save_state = lambda: None
    live_runner._trade_row = lambda *_args, **_kwargs: None
    assert live_runner._open_entry(reference_info, {"bar_time": signal_bar, "side": "LONG", "eligible": True})
    broker_fill_time = pd.Timestamp(live_runner.executor.next_open_time, unit="s", tz="UTC")
    assert live_runner.state["position"]["entry_due_utc"] == timestamp_text(broker_fill_time)
    assert live_runner.state["position"]["exit_due_utc"] == timestamp_text(
        broker_fill_time + pd.Timedelta(minutes=int(live_runner.strategy["hold_min"]))
    )
    runner._now = lambda: expected_exit_due + pd.Timedelta(seconds=1)
    runner.executor.tick_time_msc = int((expected_exit_due + pd.Timedelta(seconds=1)).timestamp() * 1000)
    assert runner._handle_time_exit(runner.executor.get_symbol_info("USTEC"))
    assert runner.state["position"] is None
    assert any(event == "position_close" for event, _kwargs in events)
    waiting_events = len(events)
    runner.state["position"] = {"close_requested": True}
    assert runner._handle_time_exit(runner.executor.get_symbol_info("USTEC"))
    assert len(events) == waiting_events
    runner.state["position"] = None
    guard_due = pd.Timestamp("2026-03-08 22:00:00", tz="UTC")
    guard_position = {"ticket": -1, "side": "LONG"}
    stale_info = SimpleNamespace(bid=100.0, ask=102.52, tick_time_msc=int((guard_due - pd.Timedelta(seconds=1)).timestamp() * 1000))
    assert runner._time_close_spread_action(guard_position, stale_info, guard_due) == "defer"
    wide_info = SimpleNamespace(bid=100.0, ask=103.02, tick_time_msc=int((guard_due + pd.Timedelta(seconds=1)).timestamp() * 1000))
    assert runner._time_close_spread_action(guard_position, wide_info, guard_due) == "defer"
    assert runner._time_close_spread_action(guard_position, wide_info, guard_due) == "defer"
    for seconds in (6, 11):
        settled_info = SimpleNamespace(bid=100.0, ask=102.52, tick_time_msc=int((guard_due + pd.Timedelta(seconds=seconds)).timestamp() * 1000))
        assert runner._time_close_spread_action(guard_position, settled_info, guard_due) == "defer"
    settled_info = SimpleNamespace(bid=100.0, ask=102.52, tick_time_msc=int((guard_due + pd.Timedelta(seconds=16)).timestamp() * 1000))
    assert runner._time_close_spread_action(guard_position, settled_info, guard_due) == "close_settled"
    timeout_position = {"ticket": -2, "side": "LONG"}
    assert runner._time_close_spread_action(timeout_position, wide_info, guard_due) == "defer"
    timeout_info = SimpleNamespace(bid=100.0, ask=103.02, tick_time_msc=int((guard_due + pd.Timedelta(minutes=30, seconds=1)).timestamp() * 1000))
    assert runner._time_close_spread_action(timeout_position, timeout_info, guard_due) == "close_timeout"
    retry_params = json.loads(json.dumps(params))
    retry_runner = ProtocolV2FixedHoldRunner(retry_params)
    retry_runner.state = retry_runner._default_state()
    retry_position = dict(shadow_position, close_requested=False, exit_due_utc=timestamp_text(expected_exit_due))
    retry_runner.state["position"] = retry_position
    retry_executor = FakeExecutor()
    retry_live_position = SimpleNamespace(
        symbol=params["mt5_symbol"], magic=EXPECTED_MAGIC, comment=params["strategy"]["comment_prefix"],
        ticket=retry_position["ticket"], identifier=retry_position["position_identifier"], type=ORDER_TYPE_BUY,
    )
    retry_executor.get_position = lambda _ticket: retry_live_position
    retry_executor.close_position = lambda _ticket, _deviation: type("RetryResult", (), {"status": "ERR|10018", "__bool__": lambda self: False})()
    retry_executor.tick_time_msc = int((expected_exit_due + pd.Timedelta(seconds=1)).timestamp() * 1000)
    retry_runner.executor = retry_executor
    retry_runner._save_state = lambda: None
    retry_runner._trade_row = lambda _event, **_kwargs: None
    retry_runner._now = lambda: expected_exit_due + pd.Timedelta(seconds=1)
    assert retry_runner._handle_time_exit(retry_executor.get_symbol_info("USTEC"))
    assert retry_runner.state["position"] is retry_position
    assert utc_timestamp(retry_position["time_close_retry_after_utc"]) == expected_exit_due + pd.Timedelta(seconds=61)
    assert close_failure_retcode("ERR|10018") == "10018"
    foreign = SimpleNamespace(symbol=params["mt5_symbol"], magic=EXPECTED_MAGIC + 1, comment=params["strategy"]["comment_prefix"], ticket=1)
    assert not runner._owned_position(foreign)
    wrong_comment = SimpleNamespace(symbol=params["mt5_symbol"], magic=EXPECTED_MAGIC, comment="foreign", ticket=2)
    assert not runner._owned_position(wrong_comment)
    runner.live_enabled = True
    runner.state["position"] = shadow_position
    runner.executor.positions = [
        SimpleNamespace(
            symbol=params["mt5_symbol"],
            magic=EXPECTED_MAGIC,
            comment=params["strategy"]["comment_prefix"],
            ticket=shadow_position["ticket"],
            identifier=shadow_position["position_identifier"],
            type=ORDER_TYPE_BUY,
        )
    ]
    runner.executor.orders_available = False
    assert runner._sync_position(), "pending-order inventory failure must not suppress an exactly-owned exit"
    assert runner.state["sync_block_reason"] == "orders_unavailable"
    runner.executor.orders_available = True
    assert runner._sync_position()
    assert not runner.state["sync_block_new_entries"], "clean active sync must clear a recoverable block"
    runner._set_sync_block("ownership_ambiguous")
    runner._set_sync_block("orders_unavailable", recoverable=True)
    assert runner.state["sync_block_reason"] == "ownership_ambiguous", "recoverable read failure must not downgrade a hard block"
    runner.state = runner._default_state()
    marker = object()
    runner.executor.close_deal_results = [False, marker]
    assert runner._get_confirmed_close_deal(123, 456) is marker
    assert runner.executor.close_deal_calls[-2:] == [(123, 456), (123, 0)]
    runner.live_enabled = False
    runner.state["position"] = None
    import live_data_fetcher as fetcher_module

    original_send = fetcher_module.ea_bridge.send_command
    fetcher_module.ea_bridge.send_command = lambda *_args, **_kwargs: "OK|2026.08.21 10:00,100,101,99,100,5,100.5"
    try:
        midpoint_bars = MT5DataManager(runner.safety).get_historical_data("USTEC", 1, 1, "UTC")
    finally:
        fetcher_module.ea_bridge.send_command = original_send
    assert midpoint_bars is not None and float(midpoint_bars.MidClose.iloc[-1]) == 100.5


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args()
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))
    if arguments.self_test:
        self_test()
        print("s26 self-test ok")
        return 0
    params = load_params()
    runner = ProtocolV2FixedHoldRunner(params)
    if not runner.connect_and_preflight():
        return 1
    if arguments.once:
        runner.run_once()
        runner.log_status()
        return 0
    while True:
        runner.run_once()
        runner.log_status()
        time.sleep(float(params.get("poll_interval_seconds", 5)))


if __name__ == "__main__":
    raise SystemExit(main())
