# -*- coding: utf-8 -*-
"""Bot25 V24 XAUUSD virtual bilateral core/satellite runner.

Strategy decisions use completed M5 bars. Order execution, 12-hour episode
expiry, feed-gap detection, spread-deferred full close, ownership sync, and
partial close confirmation run from fresh broker quotes on every poll.

One core per side exists only in logical inventory. Broker positions are opened
only by frontier adds; the virtual cores never create, close, or value orders.
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
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from contextlib import contextmanager
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

try:
    from shadow_opportunity_observer import S25ShadowOpportunityObserver
except ImportError:
    S25ShadowOpportunityObserver = None  # type: ignore[assignment]

try:
    from shadow_state_tagger import S25ShadowStateTagger
except ImportError:
    S25ShadowStateTagger = None  # type: ignore[assignment]


UTC = timezone.utc
EXPECTED_S25_MAGIC = 200025
S25_COMMENT_RE = re.compile(r"s25_m231_[LS][0-9]{4,}")
STATE_VERSION = 7
PREVIOUS_STATE_VERSION = 6
PREVIOUS_STRATEGY_ID = "bot25_v23_xauusd_drought_minority_pause_v001"
PREVIOUS_STRATEGY_KEY = "v23_bilateral_book"
LEGACY_STATE_SPECS = (
    (PREVIOUS_STATE_VERSION, PREVIOUS_STRATEGY_ID, PREVIOUS_STRATEGY_KEY),
    (5, "bot25_man231_xauusd_bilateral_core_satellite_v001", "man231_bilateral_book"),
)
V24_CANDIDATE_SPEC = {
    "change": "physical_bilateral_seeds_to_virtual_core",
    "drought_minutes": 120,
    "episode_minutes": 720,
    "frontier_add_atr": 0.5,
    "max_active_to_opposite_ratio": 3,
    "max_logical_positions_per_side": 6,
    "parent_hash": "12dc94c78f5fb6bb01710e40a8f5f199af472f2323ab0f2bb02063fda427ca10",
    "physical_seed_orders": 0,
    "release": "all_profitable_real_tickets_lifo",
    "virtual_core_per_side": 1,
}
V24_CANDIDATE_HASH = "788e7b076cd49f67cc0f2f87677f350b8ac88bcc13b15bd26cde7589105cca36"
PRODUCTIVE_CLOSE_REASONS = {"opposite_pivot_break", "ema200_retouch"}
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
RUNNER_LOCK_FILE = os.path.join(STATE_DIR, "s25_runner.lock")

TRADE_FIELDS = [
    "timestamp_utc", "quote_time_utc", "event", "strategy_id", "magic", "symbol",
    "mt5_symbol", "opportunity_id", "basket_id", "episode_id", "ticket",
    "position_identifier", "deal_id", "ticket_set", "order_comment", "side", "lot",
    "price", "price_basis", "profit", "gross_profit", "commission", "swap", "fee",
    "profit_basis", "profit_currency", "reason", "broker_reason", "signal_bar_time",
    "event_time", "release_time", "available_time", "decision_time", "executable_at",
    "spread_points", "atr14", "ema200", "active_wave", "long_positions",
    "short_positions", "logical_long_positions", "logical_short_positions",
    "virtual_core_long", "virtual_core_short", "live", "repeat_count",
    "repeat_window_seconds", "note",
]
_CSV_SCHEMAS_VALIDATED: set[str] = set()
_CSV_EVENT_KEYS: dict[str, set[tuple[str, str]]] = {}
_CSV_FILE_IDENTITIES: dict[str, tuple[int, int, int, int] | None] = {}
REPEATABLE_DIAGNOSTIC_EVENTS = {"m5_not_evaluated", "entry_blocked", "sync_block_retained"}
_SELF_TEST_HISTORICAL_QUOTES = False


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


def _strict_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"nonfinite JSON constant: {value}")


def strict_json_load_bytes(raw: bytes) -> Any:
    return json.loads(
        raw.decode("utf-8-sig"), object_pairs_hook=_strict_json_pairs,
        parse_constant=_reject_json_constant,
    )


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
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_parent_directory(path)
    except Exception:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        raise


def acquire_runner_singleton_lock(lock_file: str | None = None) -> Any | None:
    """Hold an OS-released lock for the complete bot25 runner lifetime."""
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


def append_csv(path: str, row: dict[str, Any], fields: list[str]) -> None:
    def durable_key(record):
        event = str(record.get("event") or "")
        if event == "productive_close_confirmed":
            tickets = str(record.get("ticket_set") or "")
            return (event, str(record.get("episode_id") or "") + ":" + tickets) if tickets else (event, "")
        if event == "entry_recovered_after_restart":
            return event, str(record.get("position_identifier") or "")
        if event == "ambiguous_open_resolved_flat":
            return event, str(record.get("order_comment") or "")
        return event, str(record.get("deal_id") or "")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    stat = os.stat(path) if exists else None
    identity = None if stat is None else (int(stat.st_dev), int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns))
    if _CSV_FILE_IDENTITIES.get(path) != identity:
        _CSV_SCHEMAS_VALIDATED.discard(path)
        _CSV_EVENT_KEYS.pop(path, None)
    archived_name: str | None = None
    if exists and path not in _CSV_SCHEMAS_VALIDATED:
        with open(path, "rb") as raw_existing:
            raw_csv = raw_existing.read()
        if raw_csv and not raw_csv.endswith((b"\n", b"\r")):
            raise RuntimeError("S25 trade CSV has an unterminated final row")
        with open(path, "r", newline="", encoding="utf-8") as existing:
            parsed_rows = list(csv.reader(existing))
        observed = parsed_rows[0] if parsed_rows else []
        if observed != fields:
            old_dir = os.path.join(os.path.dirname(path), "old")
            os.makedirs(old_dir, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
            archived = os.path.join(old_dir, f"{os.path.splitext(os.path.basename(path))[0]}_schema_retired_{stamp}.csv")
            shutil.move(path, archived)
            archived_name = os.path.basename(archived)
            logging.warning("S25 archived an incompatible prior trade CSV under logs/old")
            exists = False
            identity = None
        elif any(len(parsed) != len(fields) for parsed in parsed_rows[1:]):
            raise RuntimeError("S25 trade CSV contains a malformed row width")
        _CSV_SCHEMAS_VALIDATED.add(path)
    if path not in _CSV_EVENT_KEYS:
        keys: set[tuple[str, str]] = set()
        if exists:
            with open(path, "r", newline="", encoding="utf-8") as existing:
                for prior in csv.DictReader(existing):
                    prior_key = durable_key(prior)
                    if prior_key[1]:
                        if prior_key in keys:
                            raise RuntimeError("S25 duplicate durable CSV event identity")
                        keys.add(prior_key)
        _CSV_EVENT_KEYS[path] = keys
    event_key = durable_key(row)
    if event_key[1] and event_key in _CSV_EVENT_KEYS[path]:
        with open(path, "r", newline="", encoding="utf-8") as existing:
            prior = next(record for record in csv.DictReader(existing) if durable_key(record) == event_key)
        for field in ("strategy_id", "magic", "symbol", "episode_id", "position_identifier", "ticket", "order_comment", "side", "ticket_set"):
            if str(prior.get(field) or "") != str(row.get(field) or ""):
                raise RuntimeError("S25 conflicting durable CSV ownership: " + field)
        for field in ("lot", "price", "profit", "gross_profit", "commission", "swap", "fee"):
            left, right = prior.get(field), row.get(field)
            if left in (None, "") and right in (None, ""):
                continue
            try:
                agrees = math.isfinite(float(left)) and math.isfinite(float(right)) and float(left) == float(right)
            except (TypeError, ValueError, OverflowError):
                agrees = False
            if not agrees:
                raise RuntimeError("S25 conflicting durable CSV accounting: " + field)
        return
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
            _CSV_SCHEMAS_VALIDATED.add(path)
        if archived_name:
            rollover = {
                "timestamp_utc": dt_text(utc_now()),
                "event": "schema_rollover",
                "reason": "incompatible_header_archived",
                "note": f"archive={archived_name}",
            }
            writer.writerow({field: rollover.get(field, "") for field in fields})
        writer.writerow({field: row.get(field, "") for field in fields})
        handle.flush()
        os.fsync(handle.fileno())
    if event_key[1]:
        _CSV_EVENT_KEYS[path].add(event_key)
    stat = os.stat(path)
    _CSV_FILE_IDENTITIES[path] = (int(stat.st_dev), int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns))


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


def select_profitable_real_positions(
    positions: list[dict[str, Any]], side: str, bid: float, ask: float,
    close_adverse_slippage: float = 0.0,
) -> list[dict[str, Any]]:
    """Return every profitable broker ticket on the released side, newest first."""
    return [
        position for position in reversed(positions)
        if position.get("side") == side
        and position_price_pnl(position, bid - close_adverse_slippage, ask + close_adverse_slippage) > 0.0
    ]


class S25V24Runner:
    def __init__(self, params: dict[str, Any]):
        self.params = params
        self.requested_live = bool(params.get("live_trading_enabled", False))
        gate_name = str(params.get("real_trading_activation_env") or "BOT25_ENABLE_REAL_TRADING")
        gate_value = str(params.get("real_trading_activation_value") or "V24_VIRTUAL_CORE_LIVE_ACK")
        self.activation_error = self.requested_live and os.getenv(gate_name) != gate_value
        self.live_enabled = self.requested_live and not self.activation_error
        self.shadow_enabled = bool(params.get("shadow_forward_enabled", True))
        self.safety = LiveSafetyOptions(**params.get("safety", {}))
        self.dm = MT5DataManager(self.safety)
        self.executor = MT5Executor()
        self.account_currency = str(params.get("backtest_profit_currency", "USD"))
        self._state_identity_status = "missing"
        self._retired_state_sha256: str | None = None
        self._compatible_source_identity: dict[str, Any] | None = None
        self.state = self._load_state()
        self._last_status_log = 0.0
        self._suppress_manual_alerts = False
        self._diagnostic_repeats: dict[str, dict[str, Any]] = {}
        self._last_retained_block_warning: tuple[str, str] | None = None
        self.shadow_observer: Any = None
        self.shadow_state_tagger: Any = None
        self._shadow_observer_error_signature: str | None = None
        self._shadow_tagger_error_signature: str | None = None
        self._initialize_passive_evidence()

    def _initialize_passive_evidence(self) -> None:
        log_dir = os.path.dirname(TRADE_LOG_FILE)
        state_dir = os.path.dirname(STATE_FILE)
        observer_cfg = self.params.get("shadow_opportunity_observer", {})
        tagger_cfg = self.params.get("shadow_state_tagger", {})
        try:
            if bool(observer_cfg.get("enabled", False)):
                if S25ShadowOpportunityObserver is None:
                    raise RuntimeError("shadow opportunity observer module unavailable")
                self.shadow_observer = S25ShadowOpportunityObserver(observer_cfg, log_dir=log_dir, state_dir=state_dir)
        except Exception as exc:
            logging.error("S25 passive observer disabled after initialization failure; trading continues: %s", exc)
            self.shadow_observer = None
        try:
            if bool(tagger_cfg.get("enabled", False)):
                if S25ShadowStateTagger is None:
                    raise RuntimeError("shadow state tagger module unavailable")
                self.shadow_state_tagger = S25ShadowStateTagger(tagger_cfg, log_dir=log_dir)
        except Exception as exc:
            logging.error("S25 passive state tagger disabled after initialization failure; trading continues: %s", exc)
            self.shadow_state_tagger = None

    def _observer_failure(self, *, tagger: bool, exc: Exception) -> None:
        signature = f"{type(exc).__name__}:{exc}"
        attribute = "_shadow_tagger_error_signature" if tagger else "_shadow_observer_error_signature"
        if getattr(self, attribute) != signature:
            logging.error(
                "S25 passive %s failure ignored by trading path: %s",
                "tagger" if tagger else "observer",
                signature,
            )
            setattr(self, attribute, signature)

    def _observe_quote(self, info: Any, quote_time: pd.Timestamp) -> None:
        if self.shadow_observer is None:
            return
        try:
            self.shadow_observer.observe_quote(at=dt_text(quote_time), bid=float(info.bid), ask=float(info.ask))
            self._shadow_observer_error_signature = None
        except Exception as exc:
            self._observer_failure(tagger=False, exc=exc)

    def _inventory_observer_context(
        self, strategy: dict[str, Any], info: Any, quote_time: pd.Timestamp,
    ) -> dict[str, Any]:
        state = self._st(strategy)
        positions = list(state.get("positions") or [])
        contract_size = float(self.params.get("contract_size", 100.0))
        inventory_mtm = sum(
            position_price_pnl(position, float(info.bid), float(info.ask))
            * contract_size * float(position.get("lot") or 0.0)
            for position in positions
        )
        real_long_count, real_short_count = self._position_counts(strategy)
        long_count, short_count = self._logical_position_counts(strategy)
        virtual_active = state.get("episode_start_quote_utc") is not None
        virtual_long, virtual_short = self._virtual_core_flags(strategy)
        core_positions = 2 if virtual_active else 0
        episode_start = parse_ts(state.get("episode_start_quote_utc"))
        productive = parse_ts(state.get("last_productive_close_utc"))
        return {
            "episode_id": state.get("current_episode_id") or "",
            "active_wave": int(state.get("active_wave", 0)),
            "long_positions": long_count,
            "short_positions": short_count,
            "side_imbalance": long_count - short_count,
            "episode_age_minutes": "" if episode_start is None else max(0.0, (quote_time - episode_start).total_seconds() / 60.0),
            "minutes_since_productive_close": "" if productive is None else max(0.0, (quote_time - productive).total_seconds() / 60.0),
            "inventory_mtm_usd": inventory_mtm,
            "core_positions": core_positions,
            "satellite_positions": real_long_count + real_short_count,
            "note": (
                f"inventory_counts=logical;inventory_mtm=real_only;virtual_core={virtual_long}/{virtual_short}"
                if virtual_active else "inventory_counts=real;episode_inactive"
            ),
        }

    def _register_frontier_observation(
        self, strategy: dict[str, Any], row: pd.Series, info: Any,
        quote_time: pd.Timestamp, *, opportunity_id: str, side: str,
        frontier: float, atr: float, capacity_allowed: bool,
        ratio_allowed: bool, v23_allowed: bool, execution_allowed: bool,
    ) -> None:
        if self.shadow_observer is None and self.shadow_state_tagger is None:
            return
        state = self._st(strategy)
        mid = 0.5 * (float(info.bid) + float(info.ask))
        distance = (mid - frontier) if side == "LONG" else (frontier - mid)
        payload = {
            "opportunity_id": opportunity_id,
            "symbol": self.params["symbol"],
            "opportunity_type": "frontier_add",
            "side": side,
            "signal_bar_time": dt_text(row.name),
            "registered_at": dt_text(quote_time),
            "entry_bid": float(info.bid),
            "entry_ask": float(info.ask),
            "spread_price": float(info.ask) - float(info.bid),
            "spread_points": self._spread_points(info),
            "lot": float(strategy.get("lot", self.params.get("default_lot", 0.01))),
            "contract_size": float(self.params.get("contract_size", 100.0)),
            "atr14": atr,
            "ema200": float(row.get("ema200", math.nan)),
            "ema_distance_atr": (mid - float(row.get("ema200"))) / atr if atr > 0 else "",
            "frontier": frontier,
            "frontier_distance_atr": distance / atr if atr > 0 else "",
            "capacity_allowed": capacity_allowed,
            "ratio_allowed": ratio_allowed,
            "v23_allowed": v23_allowed,
            "execution_allowed": execution_allowed,
            "bar_open": float(row.get("Open", math.nan)),
            "bar_high": float(row.get("High", math.nan)),
            "bar_low": float(row.get("Low", math.nan)),
            "bar_close": float(row.get("Close", math.nan)),
            "bar_volume": float(row.get("Volume", math.nan)),
            "break_dir": int(row.get("break_dir", 0)),
            **self._inventory_observer_context(strategy, info, quote_time),
        }
        if self.shadow_observer is not None:
            try:
                self.shadow_observer.register_opportunity(payload)
                self._shadow_observer_error_signature = None
            except Exception as exc:
                self._observer_failure(tagger=False, exc=exc)
        if self.shadow_state_tagger is not None:
            try:
                self.shadow_state_tagger.record(payload)
                self._shadow_tagger_error_signature = None
            except Exception as exc:
                self._observer_failure(tagger=True, exc=exc)

    def _record_frontier_route(
        self, opportunity_id: str, *, status: str, reason: str, quote_time: pd.Timestamp,
    ) -> None:
        if self.shadow_observer is None:
            return
        try:
            self.shadow_observer.record_route(
                opportunity_id,
                status=status,
                reason=reason,
                at=dt_text(quote_time),
            )
            self._shadow_observer_error_signature = None
        except Exception as exc:
            self._observer_failure(tagger=False, exc=exc)

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
            "last_productive_close_utc": None,
            "pending_productive_close": None,
            "drought_blocked_adds": 0,
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
            "legacy_physical_core_position_ids": {"LONG": None, "SHORT": None},
        }

    def _default_state(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "bot": "bot25",
            "strategy_id": self.params["strategy_id"],
            "last_saved_utc": None,
            "strategies": {strategy["id"]: self._default_strategy_state() for strategy in self.params["strategies"]},
        }

    def _current_state_shape_error(self, state: Any) -> str | None:
        """Validate current V24 lifecycle state before any defaults are injected."""
        if not isinstance(state, dict) or not isinstance(state.get("strategies"), dict):
            return "state_or_strategies_not_object"
        if (
            state.get("version") != STATE_VERSION
            or state.get("bot") != "bot25"
            or state.get("strategy_id") != self.params.get("strategy_id")
        ):
            return "state_identity_invalid"
        expected = {str(strategy["id"]) for strategy in self.params.get("strategies", [])}
        if set(state["strategies"]) != expected:
            return "strategy_key_set_mismatch"
        for strategy in self.params.get("strategies", []):
            target = state["strategies"].get(strategy["id"])
            if not isinstance(target, dict) or not isinstance(target.get("positions"), list):
                return "strategy_or_positions_shape_invalid"
            for field in (
                "episode_sequence", "shadow_sequence", "drought_blocked_adds",
                "trade_permission_reject_count", "flat_clear_confirmation_count",
            ):
                value = target.get(field, 0)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    return f"invalid_nonnegative_counter:{field}"
            if target.get("active_wave", 0) not in {-1, 0, 1}:
                return "active_wave_invalid"
            for field in ("sync_block_new_entries", "sync_block_recoverable"):
                if not isinstance(target.get(field, False), bool):
                    return f"invalid_boolean:{field}"
            if not isinstance(target.get("sync_block_details", {}), dict):
                return "sync_block_details_invalid"
            for field in ("last_atr", "last_ema", "last_long_frontier", "last_short_frontier"):
                value = target.get(field)
                if value not in (None, ""):
                    try:
                        numeric_value = float(value)
                    except (TypeError, ValueError, OverflowError):
                        return f"invalid_numeric_state:{field}"
                    if not math.isfinite(numeric_value) or numeric_value <= 0.0:
                        return f"invalid_numeric_state:{field}"
            seen_ids: set[int] = set()
            ids_by_side: dict[str, set[int]] = {"LONG": set(), "SHORT": set()}
            for position in target["positions"]:
                if not isinstance(position, dict):
                    return "position_not_object"
                try:
                    ticket = int(position.get("ticket") or 0)
                    position_id = int(position.get("position_identifier") or ticket)
                    lot = float(position.get("lot"))
                    entry_price = float(position.get("entry_price"))
                    open_time_epoch = int(position.get("open_time_epoch"))
                    owner_magic = int(position.get("owner_magic"))
                except (TypeError, ValueError, OverflowError):
                    return "position_numeric_field_invalid"
                shadow_value = position.get("shadow", False)
                shadow = bool(shadow_value)
                close_requested = position.get("close_requested", False)
                if (
                    ticket == 0 or position_id == 0 or position_id in seen_ids
                    or (not shadow and (ticket < 0 or position_id < 0))
                    or not isinstance(shadow_value, bool)
                    or position.get("side") not in {"LONG", "SHORT"}
                    or not math.isfinite(lot) or not math.isclose(lot, float(strategy.get("lot", 0.01)), rel_tol=0.0, abs_tol=1e-9)
                    or not math.isfinite(entry_price) or entry_price <= 0.0
                    or open_time_epoch <= 0
                    or parse_ts(position.get("entry_time_utc")) is None
                    or str(position.get("owner_symbol") or "") != str(self.params.get("mt5_symbol", self.params["symbol"]))
                    or owner_magic != int(strategy["magic"])
                    or S25_COMMENT_RE.fullmatch(str(position.get("owner_comment") or "")) is None
                    or not isinstance(close_requested, bool)
                ):
                    return "position_identity_or_ownership_invalid"
                marker = position.get("close_submission_started_utc")
                if marker not in (None, "") and (parse_ts(marker) is None or not close_requested):
                    return "close_submission_marker_invalid"
                seen_ids.add(position_id)
                ids_by_side[str(position["side"])].add(position_id)
            pending_open = target.get("pending_open")
            if pending_open is not None and not isinstance(pending_open, dict):
                return "pending_open_shape_invalid"
            if isinstance(pending_open, dict):
                known_values = pending_open.get("known_position_ids", [])
                if not isinstance(known_values, list):
                    return "pending_open_values_invalid"
                try:
                    pending_lot = float(pending_open.get("lot"))
                    known_ids = [int(value) for value in known_values]
                except (TypeError, ValueError, OverflowError):
                    return "pending_open_values_invalid"
                if (
                    pending_open.get("side") not in {"LONG", "SHORT"}
                    or not math.isfinite(pending_lot)
                    or not math.isclose(pending_lot, float(strategy.get("lot", 0.01)), rel_tol=0.0, abs_tol=1e-9)
                    or 0 in known_ids or len(known_ids) != len(set(known_ids))
                    or S25_COMMENT_RE.fullmatch(str(pending_open.get("comment") or "")) is None
                    or parse_ts(pending_open.get("quote_time_utc")) is None
                ):
                    return "pending_open_values_invalid"
            if (target.get("current_episode_id") is None) != (target.get("episode_start_quote_utc") is None):
                return "episode_identity_incomplete"
            episode_id = target.get("current_episode_id")
            if episode_id is not None and re.fullmatch(r"s25_(?:v24|m231)_e[0-9]{6,}", str(episode_id)) is None:
                return "episode_identity_invalid"
            legacy_core = target.get("legacy_physical_core_position_ids")
            if not isinstance(legacy_core, dict) or set(legacy_core) != {"LONG", "SHORT"}:
                return "legacy_core_mapping_invalid"
            mapped_ids: list[int] = []
            for side in ("LONG", "SHORT"):
                mapped = legacy_core.get(side)
                if mapped is None:
                    continue
                if isinstance(mapped, bool):
                    return "legacy_core_mapping_invalid"
                try:
                    mapped_id = int(mapped)
                except (TypeError, ValueError, OverflowError):
                    return "legacy_core_mapping_invalid"
                # A closed transitional core ID remains as historical identity;
                # its absence activates the virtual core. Only a live opposite-
                # side mapping is inconsistent.
                if mapped_id <= 0 or (mapped_id in seen_ids and mapped_id not in ids_by_side[side]):
                    return "legacy_core_mapping_invalid"
                mapped_ids.append(mapped_id)
            if len(mapped_ids) != len(set(mapped_ids)):
                return "legacy_core_mapping_invalid"
            pending_action = target.get("pending_post_close_action")
            if pending_action is not None:
                if not isinstance(pending_action, dict) or pending_action.get("new_wave") not in {-1, 0, 1}:
                    return "pending_post_close_action_invalid"
                if pending_action.get("m5_bar") not in (None, "") and parse_ts(pending_action.get("m5_bar")) is None:
                    return "pending_post_close_action_invalid"
            close_defer = target.get("close_defer")
            if close_defer is not None:
                if not isinstance(close_defer, dict) or not str(close_defer.get("reason") or ""):
                    return "close_defer_invalid"
                for field in ("armed_at_utc", "first_wide_quote_utc", "last_evaluated_quote_utc", "next_retry_utc"):
                    if close_defer.get(field) not in (None, "") and parse_ts(close_defer.get(field)) is None:
                        return "close_defer_invalid"
                stable_count = close_defer.get("stable_quote_count", 0)
                if isinstance(stable_count, bool) or not isinstance(stable_count, int) or stable_count < 0:
                    return "close_defer_invalid"
            for field in (
                "episode_start_quote_utc", "last_productive_close_utc", "last_processed_m5_bar",
                "last_quote_utc", "skip_seed_quote_utc", "entry_retry_until_utc",
                "pending_close_m5_bar", "pending_close_requested_at_utc", "close_retry_after_utc",
            ):
                if target.get(field) not in (None, "") and parse_ts(target.get(field)) is None:
                    return f"invalid_timestamp:{field}"
        return None

    def _load_state(self) -> dict[str, Any]:
        default = self._default_state()
        if not os.path.exists(STATE_FILE):
            self._state_identity_status = "missing"
            return default
        raw: bytes | None = None
        try:
            with open(STATE_FILE, "rb") as handle:
                raw = handle.read()
            state = strict_json_load_bytes(raw)
        except Exception:
            logging.exception("S25 state load failed; creating fail-closed state")
            state = {}
        try:
            state_version = int(state.get("version", 0)) if isinstance(state, dict) else 0
            current_identity = (
                state.get("bot") == "bot25"
                and state.get("strategy_id") == self.params["strategy_id"]
                and state_version == STATE_VERSION
            )
        except (TypeError, ValueError):
            state_version = 0
            current_identity = False
        compatible_spec = next(
            (
                (version, strategy_id, strategy_key)
                for version, strategy_id, strategy_key in LEGACY_STATE_SPECS
                if isinstance(state, dict)
                and state.get("bot") == "bot25"
                and state.get("strategy_id") == strategy_id
                and state_version == version
                and isinstance(state.get("strategies"), dict)
                and set(state["strategies"]) == {strategy_key}
                and isinstance(state["strategies"].get(strategy_key), dict)
            ),
            None,
        )
        if compatible_spec is not None and len(self.params.get("strategies", [])) == 1:
            source_version, source_strategy_id, source_strategy_key = compatible_spec
            target_key = self.params["strategies"][0]["id"]
            state["version"] = STATE_VERSION
            state["strategy_id"] = self.params["strategy_id"]
            state["strategies"] = {target_key: state["strategies"][source_strategy_key]}
            target = state["strategies"][target_key]
            for key, value in self._default_strategy_state().items():
                target.setdefault(key, value)
            for position in target.get("positions") or []:
                if isinstance(position, dict):
                    position.setdefault("close_submission_started_utc", None)
            shape_error = self._current_state_shape_error(state)
            if shape_error:
                self._state_identity_status = "foreign_or_invalid"
                logging.critical("S25 compatible legacy state cannot form a valid V24 state: %s", shape_error)
                default_state = default["strategies"][self.params["strategies"][0]["id"]]
                default_state["sync_block_new_entries"] = True
                default_state["sync_block_reason"] = f"legacy_state_shape_invalid:{shape_error}"
                default_state["sync_block_recoverable"] = False
                return default
            self._state_identity_status = "compatible_legacy_to_v24_pending"
            self._retired_state_sha256 = hashlib.sha256(raw or b"").hexdigest()
            self._compatible_source_identity = {
                "version": source_version,
                "strategy_id": source_strategy_id,
                "strategy_key": source_strategy_key,
            }
            logging.warning(
                "S25 legacy bot25 state staged for broker-owned inventory proof: version=%s strategy=%s",
                source_version,
                source_strategy_id,
            )
            return state
        if not current_identity:
            retired_strategy_id = str(state.get("strategy_id") or "").strip() if isinstance(state, dict) else ""
            if (
                raw is not None
                and isinstance(state, dict)
                and state.get("bot") == "bot25"
                and retired_strategy_id
                and retired_strategy_id != self.params["strategy_id"]
                and state_version > 0
                and isinstance(state.get("strategies"), dict)
            ):
                self._state_identity_status = "retired_bot25"
                self._retired_state_sha256 = hashlib.sha256(raw).hexdigest()
                logging.warning("S25 retired bot25 state detected; flat-inventory preflight required")
            else:
                self._state_identity_status = "foreign_or_invalid"
                logging.critical("S25 foreign or invalid state identity detected; automatic adoption refused")
            default_state = default["strategies"][self.params["strategies"][0]["id"]]
            default_state["sync_block_new_entries"] = True
            default_state["sync_block_reason"] = "state_identity_mismatch"
            default_state["sync_block_recoverable"] = False
            return default
        shape_error = self._current_state_shape_error(state)
        if shape_error:
            self._state_identity_status = "foreign_or_invalid"
            logging.critical("S25 current V24 state shape invalid: %s", shape_error)
            default_state = default["strategies"][self.params["strategies"][0]["id"]]
            default_state["sync_block_new_entries"] = True
            default_state["sync_block_reason"] = f"state_shape_invalid:{shape_error}"
            default_state["sync_block_recoverable"] = False
            return default
        self._state_identity_status = "current"
        for strategy in self.params["strategies"]:
            state.setdefault("strategies", {}).setdefault(strategy["id"], {})
            target = state["strategies"][strategy["id"]]
            for key, value in self._default_strategy_state().items():
                target.setdefault(key, value)
            for position in target.get("positions") or []:
                if isinstance(position, dict):
                    position.setdefault("close_submission_started_utc", None)
        return state

    def _save_state(self) -> None:
        if getattr(self, "_close_transaction_active", False):
            return
        self.state["last_saved_utc"] = dt_text(utc_now())
        atomic_write_json(STATE_FILE, self.state)

    @contextmanager
    def _close_state_transaction(self):
        """Commit reconciliation once; retain exact durable commit on late error.

        Used for confirmed closes and pending-OPEN recovery; never wraps a broker
        mutation. The historical name is retained for existing audit references.
        """
        if getattr(self, "_close_transaction_active", False):
            raise RuntimeError("nested close transaction")
        before = copy.deepcopy(self.state)
        strategy_refs = dict(self.state["strategies"])
        attempted = None
        self._close_transaction_active = True
        try:
            yield
            self.state["last_saved_utc"] = dt_text(utc_now())
            attempted = copy.deepcopy(self.state)
            atomic_write_json(STATE_FILE, attempted)
        except BaseException:
            committed = False
            if attempted is not None:
                try:
                    with open(STATE_FILE, "rb") as handle:
                        committed = strict_json_load_bytes(handle.read()) == attempted
                except (OSError, ValueError, TypeError):
                    pass
            restored = attempted if committed else before
            # Keep references held by _run_strategy valid after rollback.
            for key, reference in strategy_refs.items():
                reference.clear()
                reference.update(copy.deepcopy(restored["strategies"][key]))
            self.state.clear()
            self.state.update(copy.deepcopy(restored))
            self.state["strategies"] = strategy_refs
            raise
        finally:
            self._close_transaction_active = False

    def _retire_loaded_state_if_flat(self, strategy: dict[str, Any], quote_time: pd.Timestamp) -> bool:
        """Replace a retired bot25 state only after a clean, CAS-protected flat check."""
        if self._state_identity_status != "retired_bot25" or not self._retired_state_sha256:
            return False
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        positions = self.executor.get_positions(symbol, int(strategy["magic"]))
        orders = self.executor.get_orders(symbol, int(strategy["magic"]))
        if positions is None or orders is None:
            logging.critical("S25 retired-state inventory query failed; state preserved")
            return False
        if positions or orders:
            logging.critical(
                "S25 retired-state inventory is not flat; state preserved positions=%d orders=%d",
                len(positions), len(orders),
            )
            return False
        try:
            with open(STATE_FILE, "rb") as handle:
                current_raw = handle.read()
        except Exception:
            logging.exception("S25 retired-state CAS read failed; state preserved")
            return False
        if hashlib.sha256(current_raw).hexdigest() != self._retired_state_sha256:
            logging.critical("S25 retired state changed during preflight; state preserved")
            return False
        try:
            old_dir = os.path.join(os.path.dirname(STATE_FILE), "old")
            os.makedirs(old_dir, exist_ok=True)
            stamp = utc_now().strftime("%Y%m%dT%H%M%S%fZ")
            archive_name = f"s25_bot_state_retired_{stamp}_{self._retired_state_sha256[:12]}.json"
            archive_path = os.path.join(old_dir, archive_name)
            os.replace(STATE_FILE, archive_path)
        except Exception:
            logging.exception("S25 retired-state archive failed; state preserved")
            return False
        self.state = self._default_state()
        try:
            self._save_state()
        except Exception:
            logging.exception("S25 new V24 state creation failed; restoring retired state")
            if not os.path.exists(STATE_FILE) and os.path.exists(archive_path):
                os.replace(archive_path, STATE_FILE)
            return False
        self._state_identity_status = "current"
        digest_prefix = self._retired_state_sha256[:12]
        self._retired_state_sha256 = None
        logging.warning("S25 retired bot25 state archived after confirmed flat inventory: %s", archive_name)
        self._trade_row(
            "startup_state_retired", strategy, quote_time_utc=dt_text(quote_time),
            opportunity_id=self._opportunity_id(None, quote_time, "startup_state_retired"),
            reason="retired_bot25_state_archived_after_flat_owned_inventory",
            note=json.dumps({"archive": archive_name, "sha256_prefix": digest_prefix}, sort_keys=True),
        )
        return True

    def _commit_compatible_state_upgrade(self, strategy: dict[str, Any], quote_time: pd.Timestamp) -> bool:
        """Persist a proven legacy -> V24 state upgrade with unchanged-file CAS."""
        if self._state_identity_status != "compatible_legacy_to_v24_pending" or not self._retired_state_sha256:
            return False
        try:
            with open(STATE_FILE, "rb") as handle:
                current_raw = handle.read()
        except Exception:
            logging.exception("S25 compatible-state CAS read failed; old state preserved")
            return False
        if hashlib.sha256(current_raw).hexdigest() != self._retired_state_sha256:
            logging.critical("S25 legacy state changed during V24 preflight; upgrade refused")
            return False
        digest_prefix = self._retired_state_sha256[:12]
        source = dict(self._compatible_source_identity or {})
        try:
            self._save_state()
        except Exception:
            logging.exception("S25 compatible V24 state save failed; old state preserved")
            return False
        self._state_identity_status = "current"
        self._retired_state_sha256 = None
        self._compatible_source_identity = None
        state = self._st(strategy)
        migrated_positions = len(state.get("positions") or [])
        self._trade_row(
            "startup_state_migrated", strategy, quote_time_utc=dt_text(quote_time),
            opportunity_id=self._opportunity_id(None, quote_time, "startup_state_migrated"),
            reason=(
                "nonflat_legacy_owned_inventory_upgraded_to_v24"
                if migrated_positions else "flat_legacy_state_upgraded_to_v24"
            ),
            note=json.dumps(
                {
                    "source_version": source.get("version"),
                    "source_strategy_id": source.get("strategy_id"),
                    "migrated_positions": migrated_positions,
                    "sha256_prefix": digest_prefix,
                },
                sort_keys=True,
            ),
        )
        return True

    def _compatible_state_inventory_proven_and_staged(self, strategy: dict[str, Any]) -> bool:
        """Prove an exact reservation-free legacy inventory and stage transitional cores."""
        state = self._st(strategy)
        positions_state = state.get("positions")
        if not isinstance(positions_state, list):
            logging.critical("S25 V24 migration requires a list-shaped legacy position state")
            return False
        unresolved_fields = (
            "pending_open",
            "pending_close_reason",
            "pending_close_m5_bar",
            "pending_close_requested_at_utc",
            "pending_post_close_action",
            "pending_productive_close",
            "close_defer",
        )
        if any(state.get(field) not in (None, "", False) for field in unresolved_fields):
            logging.critical("S25 V24 migration refused with a pending legacy lifecycle action")
            return False
        if state.get("sync_block_new_entries") or state.get("sync_block_reason"):
            logging.critical("S25 V24 migration refused with a retained legacy sync block")
            return False
        if any(not isinstance(position, dict) or position.get("close_requested") for position in positions_state):
            logging.critical("S25 V24 migration refused with malformed or close-requested legacy positions")
            return False
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        positions = self.executor.get_positions(symbol, int(strategy["magic"]))
        orders = self.executor.get_orders(symbol, int(strategy["magic"]))
        if positions is None or orders is None:
            logging.critical("S25 V24 migration requires available broker positions and orders")
            return False
        unexpected = [record for record in [*positions, *orders] if not self._owned_position(strategy, record)]
        if unexpected:
            logging.critical("S25 V24 migration found same-magic inventory outside the bot25 namespace")
            return False
        if orders:
            logging.critical("S25 V24 migration refused while bot25 pending orders remain")
            return False
        if any(not self._state_ownership_proven(strategy, position) for position in positions_state):
            logging.critical("S25 V24 migration cannot prove stored position ownership")
            return False
        try:
            state_by_id = {
                int(position.get("position_identifier") or position.get("ticket") or 0): position
                for position in positions_state
            }
            live_by_id = {
                int(getattr(position, "identifier", 0) or getattr(position, "ticket", 0)): position
                for position in positions
            }
            if (
                0 in state_by_id
                or 0 in live_by_id
                or len(state_by_id) != len(positions_state)
                or len(live_by_id) != len(positions)
                or set(state_by_id) != set(live_by_id)
            ):
                logging.critical("S25 V24 migration state/broker position identity sets do not match")
                return False
            if any(
                not self._state_matches_live(strategy, state_by_id[position_id], live_by_id[position_id])
                for position_id in state_by_id
            ):
                logging.critical("S25 V24 migration state/broker side, lot, or ownership does not match")
                return False
            core_ids: dict[str, int | None] = {"LONG": None, "SHORT": None}
            for side in ("LONG", "SHORT"):
                side_positions = [position for position in positions_state if position.get("side") == side]
                if side_positions:
                    core = min(side_positions, key=lambda row: float(row["entry_price"])) if side == "LONG" else max(
                        side_positions, key=lambda row: float(row["entry_price"])
                    )
                    core_ids[side] = int(core.get("position_identifier") or core.get("ticket"))
            state["legacy_physical_core_position_ids"] = core_ids
            if positions_state:
                self._ensure_episode_identity(strategy)
                if state.get("episode_start_quote_utc") is None:
                    opened = [parse_ts(position.get("entry_time_utc")) for position in positions_state]
                    opened = [value for value in opened if value is not None]
                    if not opened:
                        logging.critical("S25 V24 migration requires broker-derived entry time for nonflat inventory")
                        return False
                    state["episode_start_quote_utc"] = dt_text(min(opened))
            long_count, short_count = self._logical_position_counts(strategy)
            max_side = int(strategy.get("max_positions_per_side", 6))
            ratio = int(strategy.get("max_active_to_opposite_ratio", 3))
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            logging.exception("S25 V24 migration found malformed legacy position values")
            return False
        if long_count > max_side or short_count > max_side:
            logging.critical("S25 V24 migration would exceed the logical side cap")
            return False
        if long_count and short_count and (long_count > ratio * short_count or short_count > ratio * long_count):
            logging.critical("S25 V24 migration would exceed the logical side ratio")
            return False
        return True

    def _st(self, strategy: dict[str, Any]) -> dict[str, Any]:
        return self.state["strategies"][strategy["id"]]

    def _trade_row(self, event: str, strategy: dict[str, Any], **kwargs: Any) -> None:
        state = self._st(strategy)
        now = utc_now()
        long_count, short_count = self._position_counts(strategy)
        logical_long, logical_short = self._logical_position_counts(strategy)
        virtual_active = state.get("episode_start_quote_utc") is not None
        virtual_long, virtual_short = self._virtual_core_flags(strategy)
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
            "logical_long_positions": logical_long,
            "logical_short_positions": logical_short,
            "virtual_core_long": virtual_long,
            "virtual_core_short": virtual_short,
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
            if isinstance(state.get("pending_open"), dict):
                # Any interrupted/unsafe inventory observation breaks the
                # consecutive clean-confirmation proof, even if its reason is
                # superseded by a stronger existing block below.
                state["pending_open"]["flat_confirmation_count"] = 0
            if recoverable and state.get("sync_block_new_entries") and not state.get("sync_block_recoverable"):
                retained = (str(previous or ""), str(reason))
                if self._last_retained_block_warning != retained:
                    logging.warning(
                        "S25 retained non-recoverable block: existing=%s ignored_transient=%s",
                        previous,
                        reason,
                    )
                    self._last_retained_block_warning = retained
                return
            self._last_retained_block_warning = None
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
        self._last_retained_block_warning = None

    @staticmethod
    def _side_from_record(record: Any) -> str:
        return "LONG" if int(getattr(record, "type", -1)) == ORDER_TYPE_BUY else "SHORT"

    def _owned_position(self, strategy: dict[str, Any], record: Any) -> bool:
        try:
            return (
                int(getattr(record, "ticket", 0) or 0) > 0
                and int(getattr(record, "identifier", 0) or getattr(record, "ticket", 0) or 0) > 0
                and int(getattr(record, "type", -1)) in {ORDER_TYPE_BUY, ORDER_TYPE_SELL}
                and math.isclose(float(getattr(record, "volume", 0.0)), float(strategy.get("lot", 0.01)), rel_tol=0.0, abs_tol=1e-9)
                and str(getattr(record, "symbol", "")) == str(self.params.get("mt5_symbol", self.params["symbol"]))
                and int(getattr(record, "magic", -1)) == int(strategy["magic"])
                and S25_COMMENT_RE.fullmatch(str(getattr(record, "comment", "") or "")) is not None
            )
        except (TypeError, ValueError, OverflowError):
            return False

    def _state_ownership_proven(self, strategy: dict[str, Any], position: dict[str, Any]) -> bool:
        try:
            return (
                not bool(position.get("shadow", False))
                and int(position.get("ticket") or 0) > 0
                and int(position.get("position_identifier") or position.get("ticket") or 0) > 0
                and str(position.get("owner_symbol") or "") == str(self.params.get("mt5_symbol", self.params["symbol"]))
                and int(position.get("owner_magic") or -1) == int(strategy["magic"])
                and S25_COMMENT_RE.fullmatch(str(position.get("owner_comment") or "")) is not None
                and position.get("side") in {"LONG", "SHORT"}
                and math.isclose(float(position.get("lot") or 0.0), float(strategy.get("lot", 0.01)), rel_tol=0.0, abs_tol=1e-9)
            )
        except (TypeError, ValueError, OverflowError):
            return False

    def _state_matches_live(self, strategy: dict[str, Any], state_position: dict[str, Any], live_position: Any) -> bool:
        state_id = int(state_position.get("position_identifier") or state_position.get("ticket") or 0)
        live_id = int(getattr(live_position, "identifier", 0) or getattr(live_position, "ticket", 0))
        try:
            live_open_time = int(getattr(live_position, "open_time", 0) or 0)
            return (
                int(state_position.get("ticket") or 0) == int(getattr(live_position, "ticket", 0) or 0)
                and state_id == live_id
                and int(state_position.get("open_time_epoch") or 0) == live_open_time
                and self._state_ownership_proven(strategy, state_position)
                and str(state_position.get("owner_comment") or "") == str(getattr(live_position, "comment", "") or "")
                and state_position.get("side") == self._side_from_record(live_position)
                and math.isclose(
                    float(state_position.get("lot") or 0.0),
                    float(getattr(live_position, "volume", 0.0)),
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                and self._owned_position(strategy, live_position)
            )
        except (TypeError, ValueError, OverflowError):
            return False

    @staticmethod
    def _broker_open_timestamp(live_position: Any) -> tuple[pd.Timestamp, int]:
        open_time = int(getattr(live_position, "open_time", 0) or 0)
        open_time_msc = int(getattr(live_position, "open_time_msc", 0) or 0)
        if open_time_msc > 0:
            if open_time <= 0 or open_time_msc // 1000 != open_time:
                raise ValueError("broker position open time fields disagree")
            return pd.Timestamp(open_time_msc, unit="ms", tz="UTC"), open_time
        if open_time <= 0:
            raise ValueError("broker position open time unavailable")
        return pd.Timestamp(open_time, unit="s", tz="UTC"), open_time

    def _state_position_from_live(self, strategy: dict[str, Any], live_position: Any) -> dict[str, Any]:
        position_id = int(getattr(live_position, "identifier", 0) or live_position.ticket)
        open_timestamp, open_time = self._broker_open_timestamp(live_position)
        return {
            "ticket": int(live_position.ticket), "position_identifier": position_id,
            "side": self._side_from_record(live_position), "lot": float(live_position.volume),
            "entry_price": float(live_position.open_price),
            "entry_time_utc": dt_text(open_timestamp),
            "open_time_epoch": open_time,
            "owner_symbol": str(live_position.symbol), "owner_magic": int(live_position.magic),
            "owner_comment": str(getattr(live_position, "comment", "") or ""),
            "shadow": False, "close_requested": False,
            "close_submission_started_utc": None,
        }

    @staticmethod
    def _open_error_is_definitive_no_fill(error: str) -> bool:
        exact = {
            "INVALID_OPEN_REQUEST", "OPEN_POLICY_GUARD", "ERR|BAD_OPEN_GUARD",
            "ERR|OPEN_POLICY_GUARD", "ERR|OPEN_INVENTORY_GUARD",
            "ERR|SYMBOL_ADMISSION_GUARD", "ERR|MARGIN_ADMISSION_GUARD",
            "ERR|ACCOUNT_IDENTITY_GUARD", "ERR|ACCOUNT_MODE_GUARD",
            "ERR|TRADE_PERMISSION_GUARD", "ERR|REQUEST_EXPIRED",
            "ERR|COMMAND_BUSY", "ERR|LOCK_TIMEOUT", "ERR|WRITE_FAILED",
            "ERR|RESPONSE_BUSY", "ERR|INVALID_TIMEOUT", "ERR|INVALID_COMMAND",
        }
        if error in exact:
            return True
        return any(
            error.startswith(f"ERR|{retcode}|ORDER=0|DEAL=0|")
            for retcode in (10018, 10026, 10027)
        )

    @staticmethod
    def _close_result_intrinsically_no_fill(result: Any) -> bool:
        return str(getattr(result, "status", "")) in {
            "INVALID_REQUEST", "ACCOUNT_IDENTITY_GUARD", "ACCOUNT_MODE_GUARD",
            "TRADE_PERMISSION_GUARD", "POSITION_OWNERSHIP_GUARD",
            "CLOSE_POLICY_GUARD", "IPC_NOT_PUBLISHED",
        }

    def _close_result_definitive_no_fill(
        self, strategy: dict[str, Any], state_position: dict[str, Any], result: Any,
    ) -> bool:
        """Return true only when a retry cannot duplicate a prior close fill."""
        if self._close_result_intrinsically_no_fill(result):
            return True
        if int(getattr(result, "retcode", 0) or 0) not in {10018, 10026, 10027}:
            return False
        ticket = int(state_position.get("ticket") or 0)
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        live_position = self.executor.get_position(ticket)
        orders = self.executor.get_orders(symbol, int(strategy["magic"]))
        return (
            live_position is not None and live_position is not False
            and orders == []
            and self._state_matches_live(strategy, state_position, live_position)
        )

    def _submit_reserved_close(
        self, strategy: dict[str, Any], position: dict[str, Any], quote_time: pd.Timestamp,
    ) -> tuple[str, Any]:
        """Submit one durable close intent once; ambiguous submissions are never replayed."""
        if position.get("close_submission_started_utc"):
            return "awaiting_confirmation", None
        ticket = int(position.get("ticket") or 0)
        live_position = self.executor.get_position(ticket)
        if live_position is None or live_position is False or not self._state_matches_live(strategy, position, live_position):
            self._set_sync_block(
                strategy, "state_ticket_unowned_or_foreign", {"ticket": ticket}, recoverable=False,
            )
            self._save_state()
            return "blocked", None
        position["close_submission_started_utc"] = dt_text(quote_time)
        self._save_state()
        result = self.executor.close_position(
            ticket, int(self.params.get("deviation_points", 50)),
            expected_login=int(MT5_LOGIN), expected_server=str(MT5_SERVER),
            expected_symbol=str(position.get("owner_symbol") or ""),
            expected_magic=int(position.get("owner_magic") or -1),
            expected_comment=str(position.get("owner_comment") or ""),
            expected_identifier=int(position.get("position_identifier") or ticket),
            expected_type=ORDER_TYPE_BUY if position.get("side") == "LONG" else ORDER_TYPE_SELL,
            expected_volume=float(position.get("lot") or 0.0),
        )
        if result:
            self._st(strategy)["trade_permission_reject_count"] = 0
            return "submitted", result
        if self._close_result_definitive_no_fill(strategy, position, result):
            position["close_submission_started_utc"] = None
            self._save_state()
            return "retryable", result
        self._set_sync_block(
            strategy, "close_submission_unresolved",
            {"ticket": ticket, "status": str(getattr(result, "status", "FAILED")),
             "response": str(getattr(result, "raw_response", "") or "")[:200]},
            recoverable=False,
        )
        self._save_state()
        return "blocked", result

    def _has_real_state_positions(self, strategy: dict[str, Any]) -> bool:
        return any(
            isinstance(position, dict) and not bool(position.get("shadow", False))
            for position in self._st(strategy).get("positions") or []
        )

    def _shadow_inventory_matches_read_only(
        self,
        strategy: dict[str, Any],
        positions: list[Any] | None = None,
        orders: list[Any] | None = None,
    ) -> bool:
        """Verify retained real inventory in shadow mode without mutating state."""
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        if positions is None:
            positions = self.executor.get_positions(symbol, int(strategy["magic"]))
        if orders is None:
            orders = self.executor.get_orders(symbol, int(strategy["magic"]))
        if positions is None or orders is None:
            logging.critical("S25 shadow canary inventory unavailable; state left unchanged")
            return False
        unexpected = [record for record in [*positions, *orders] if not self._owned_position(strategy, record)]
        if unexpected or orders:
            logging.critical("S25 shadow canary found unexpected same-magic inventory; state left unchanged")
            return False
        state_positions = [
            position for position in self._st(strategy).get("positions") or []
            if isinstance(position, dict) and not bool(position.get("shadow", False))
        ]
        try:
            state_by_id = {
                int(position.get("position_identifier") or position.get("ticket") or 0): position
                for position in state_positions
            }
            live_by_id = {
                int(getattr(position, "identifier", 0) or getattr(position, "ticket", 0)): position
                for position in positions
            }
            exact_ids = (
                0 not in state_by_id
                and 0 not in live_by_id
                and len(state_by_id) == len(state_positions)
                and len(live_by_id) == len(positions)
                and set(state_by_id) == set(live_by_id)
            )
            exact_values = exact_ids and all(
                self._state_matches_live(strategy, state_by_id[position_id], live_by_id[position_id])
                for position_id in state_by_id
            )
        except (AttributeError, TypeError, ValueError, OverflowError):
            logging.exception("S25 shadow canary found malformed inventory; state left unchanged")
            return False
        if not exact_values:
            logging.critical("S25 shadow canary state/broker inventory mismatch; state left unchanged")
            return False
        return True

    def _reconcile_pending_open(self, strategy: dict[str, Any], positions: list[Any], *, orders_available: bool = True) -> bool:
        state = self._st(strategy)
        pending = state.get("pending_open")
        if not pending:
            return True
        if not orders_available:
            pending["flat_confirmation_count"] = 0
            self._save_state()
            return False
        known = {int(value) for value in pending.get("known_position_ids", [])}
        candidates = [
            position for position in positions
            if int(getattr(position, "identifier", 0) or position.ticket) not in known
            and self._side_from_record(position) == pending.get("side")
            and abs(float(position.volume) - float(pending.get("lot", 0))) <= 1e-9
            and str(getattr(position, "comment", "") or "") == str(pending.get("comment", ""))
        ]
        unknown_ids = {int(getattr(position, "identifier", 0) or position.ticket) for position in positions} - known
        if len(candidates) == 1 and len(unknown_ids) == 1:
            with self._close_state_transaction():
                self._ensure_episode_identity(strategy)
                recovered_position = self._state_position_from_live(strategy, candidates[0])
                state["positions"].append(recovered_position)
                state["pending_open"] = None
                if state.get("sync_block_reason") in {None, "ambiguous_open_result", "pending_open_reconciliation_ambiguous"}:
                    self._set_sync_block(strategy, None)
                recovery_quote = parse_ts(pending.get("quote_time_utc"))
                if recovery_quote is None:
                    recovery_quote = pd.Timestamp(utc_now())
                execution_time = parse_ts(recovered_position.get("entry_time_utc"))
                if execution_time is None:
                    raise ValueError("recovered broker position open time unavailable")
                recovery_causal = self._causal_fields(pending.get("signal_bar_time"), recovery_quote)
                recovery_causal["executable_at"] = dt_text(execution_time)
                self._trade_row(
                    "entry_recovered_after_restart", strategy, quote_time_utc=dt_text(execution_time),
                    opportunity_id=pending.get("opportunity_id"), ticket=int(candidates[0].ticket),
                    position_identifier=int(getattr(candidates[0], "identifier", 0) or candidates[0].ticket),
                    side=pending.get("side"), lot=pending.get("lot"), price=float(candidates[0].open_price),
                    price_basis="broker_position_reconciliation", order_comment=pending.get("comment"),
                    reason=pending.get("reason"), **recovery_causal,
                )
                self._save_state()
                return True
        if not candidates and not unknown_ids:
            with self._close_state_transaction():
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
        if int(strategy.get("virtual_core_positions_per_side", -1)) != 1:
            return "unexpected_virtual_core_count"
        if int(strategy.get("physical_seed_orders", -1)) != 0:
            return "physical_seed_orders_must_be_zero"
        return None

    def _configuration_contract_error(self) -> str | None:
        """Pin every strategy-semantic value represented by the V24 identity/hash."""
        if bool(self.params.get("live_trading_enabled", False)) == bool(self.params.get("shadow_forward_enabled", False)):
            return "exactly_one_live_or_shadow_mode_required"
        exact = {
            "enabled": True,
            "require_hedging_account": True,
            "bot_number": "25", "bot_suffix": "s25",
            "strategy_id": "bot25_v24_xauusd_virtual_bilateral_core_v001",
            "candidate_id": "combo_014_v001_v001_virtual_core_v001",
            "candidate_params_hash": V24_CANDIDATE_HASH,
            "parent_candidate_params_hash": V24_CANDIDATE_SPEC["parent_hash"],
            "expected_bridge_name": "BotBridge_s25",
            "expected_bridge_version": "2026-09-04-s25-v24-atomic-v8",
            "real_trading_activation_env": "BOT25_ENABLE_REAL_TRADING",
            "real_trading_activation_value": "V24_VIRTUAL_CORE_LIVE_ACK",
            "expected_magic": EXPECTED_S25_MAGIC,
            "symbol": "XAUUSD", "mt5_symbol": "XAUUSD",
            "backtest_profit_currency": "USD", "broker_timezone": "UTC",
            "m5_timeframe": 5, "m5_bars": 260, "drop_latest_m5_bar": True,
            "price_digits": 3, "deviation_points": 50,
            "enforce_broker_quote_freshness": True,
        }
        for key, expected in exact.items():
            if self.params.get(key) != expected:
                return f"config_mismatch:{key}"
        numeric = {
            "default_lot": 0.01, "contract_size": 100.0, "point_size": 0.001,
            "productive_close_threshold_usd": 0.10,
            "productive_close_drought_minutes": 120.0, "episode_minutes": 720.0,
            "live_release_profit_buffer_price": 0.030,
            "shadow_adverse_slippage_price": 0.030,
            "max_signal_delay_minutes": 7.0, "max_entry_spread_points": 300.0,
            "feed_gap_minutes": 5.0,
            "max_broker_quote_age_seconds": 15.0,
            "max_broker_quote_future_seconds": 2.0,
            "time_close_spread_limit_points": 300.0,
            "time_close_stable_quotes": 3.0,
            "time_close_wide_timeout_minutes": 30.0,
            "trade_permission_retry_seconds": 30.0,
            "close_retry_seconds": 15.0,
            "time_close_market_closed_retry_seconds": 60.0,
            "trade_permission_alert_threshold": 3.0,
        }
        for key, expected in numeric.items():
            try:
                observed = float(self.params.get(key))
            except (TypeError, ValueError, OverflowError):
                return f"config_mismatch:{key}"
            if not math.isfinite(observed) or not math.isclose(observed, expected, rel_tol=0.0, abs_tol=1e-12):
                return f"config_mismatch:{key}"
        strategy = self.params["strategies"][0]
        strategy_exact = {
            "enabled": True,
            "id": "v24_virtual_bilateral_book",
            "spec_id": "man_231_drought_minority_virtual_core_v24_v001",
            "magic": EXPECTED_S25_MAGIC, "comment_prefix": "s25_m231",
            "atr_period": 14, "ema_period": 200, "pivot_radius": 2,
            "virtual_core_positions_per_side": 1, "physical_seed_orders": 0,
            "max_positions_per_side": 6, "max_active_to_opposite_ratio": 3,
        }
        for key, expected in strategy_exact.items():
            if strategy.get(key) != expected:
                return f"strategy_config_mismatch:{key}"
        for key, expected in {"lot": 0.01, "pivot_break_atr_buffer": 0.10, "frontier_add_atr": 0.50}.items():
            try:
                observed = float(strategy.get(key))
            except (TypeError, ValueError, OverflowError):
                return f"strategy_config_mismatch:{key}"
            if not math.isfinite(observed) or not math.isclose(observed, expected, rel_tol=0.0, abs_tol=1e-12):
                return f"strategy_config_mismatch:{key}"
        encoded_spec = json.dumps(V24_CANDIDATE_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if hashlib.sha256(encoded_spec).hexdigest() != V24_CANDIDATE_HASH:
            return "embedded_candidate_hash_mismatch"
        expected_safety = {
            "hist_timestamps_are_utc": True,
            "stale_signal_guard": True,
            "preflight_clean_sync": True,
            "periodic_clean_sync": True,
            "clear_recoverable_sync_block": True,
            "save_state_after_clear": True,
            "broker_sl_residual_clear": False,
            "audit_log": True,
        }
        if self.params.get("safety") != expected_safety:
            return "config_mismatch:safety"
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

    def _quote_clock_error(self, quote_time: pd.Timestamp, strategy: dict[str, Any] | None = None) -> str | None:
        if _SELF_TEST_HISTORICAL_QUOTES:
            return None
        if not bool(self.params.get("enforce_broker_quote_freshness", True)):
            return None
        now = pd.Timestamp(utc_now())
        age_seconds = (now - quote_time).total_seconds()
        if age_seconds > float(self.params.get("max_broker_quote_age_seconds", 15)):
            return f"broker_quote_stale:{age_seconds:.3f}"
        if age_seconds < -float(self.params.get("max_broker_quote_future_seconds", 2)):
            return f"broker_quote_from_future:{age_seconds:.3f}"
        if strategy is not None:
            previous = parse_ts(self._st(strategy).get("last_quote_utc"))
            if previous is not None and quote_time < previous:
                return f"broker_quote_regressed:{dt_text(previous)}:{dt_text(quote_time)}"
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
        contract_error = self._configuration_contract_error()
        if contract_error:
            logging.critical("S25 frozen configuration contract invalid: %s", contract_error)
            return False
        strategy = self.params["strategies"][0]
        if self._state_identity_status == "foreign_or_invalid":
            logging.critical("Archive retired bot25 state before starting V24")
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
        quote_time = pd.Timestamp(int(info.quote_time_msc), unit="ms", tz="UTC")
        quote_clock_error = self._quote_clock_error(quote_time)
        if quote_clock_error:
            logging.critical("S25 broker quote clock failed preflight: %s", quote_clock_error)
            return False
        if self._state_identity_status == "retired_bot25":
            if not self._retire_loaded_state_if_flat(strategy, quote_time):
                return False
        if self._state_identity_status == "compatible_legacy_to_v24_pending":
            if not self._compatible_state_inventory_proven_and_staged(strategy):
                return False
            if not self._commit_compatible_state_upgrade(strategy, quote_time):
                return False
        if not self._sync_strategy(strategy):
            return False
        state = self._st(strategy)
        if state["positions"]:
            self._ensure_episode_identity(strategy)
            if state.get("episode_start_quote_utc") is None:
                recovered_times = [parse_ts(position.get("entry_time_utc")) for position in state["positions"]]
                recovered_times = [value for value in recovered_times if value is not None]
                state["episode_start_quote_utc"] = dt_text(min(recovered_times) if recovered_times else quote_time)
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
            quote_time_utc=dt_text(quote_time),
            opportunity_id=self._opportunity_id(None, quote_time, "startup"),
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
        if not self.live_enabled and self._has_real_state_positions(strategy):
            return self._shadow_inventory_matches_read_only(strategy, positions, orders)
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
        if positions and not self._has_real_state_positions(strategy) and not state.get("pending_open"):
            self._set_sync_block(
                strategy,
                "live_positions_without_real_state",
                {"tickets": [int(position.ticket) for position in positions]},
                recoverable=False,
            )
            self._save_state()
            return False
        if not self.live_enabled and not self._has_real_state_positions(strategy):
            return True
        if self.live_enabled and any(bool(position.get("shadow", False)) for position in state.get("positions") or []):
            self._set_sync_block(strategy, "shadow_positions_present_in_live_mode", recoverable=False)
            self._save_state()
            return False
        state_positions = list(state.get("positions") or [])
        if state_positions and not state.get("current_episode_id"):
            self._ensure_episode_identity(strategy)
        if not state_positions and positions and not state.get("pending_open"):
            self._set_sync_block(strategy, "live_positions_without_state", {"tickets": [int(position.ticket) for position in positions]}, recoverable=False)
            self._save_state()
            return False
        try:
            live_ids = [int(getattr(position, "identifier", 0) or 0) for position in positions]
            state_ids_before = [
                int(position.get("position_identifier") or position.get("ticket") or 0)
                for position in state_positions if isinstance(position, dict)
            ]
        except (TypeError, ValueError, OverflowError):
            live_ids, state_ids_before = [], []
        if (
            len(state_ids_before) != len(state_positions) or 0 in state_ids_before
            or len(state_ids_before) != len(set(state_ids_before))
            or 0 in live_ids or len(live_ids) != len(set(live_ids))
        ):
            self._set_sync_block(
                strategy, "duplicate_or_invalid_position_identity",
                {"state_ids": state_ids_before, "live_ids": live_ids}, recoverable=False,
            )
            self._save_state()
            return False
        live_by_id = {live_id: position for live_id, position in zip(live_ids, positions)}
        if not self._reconcile_pending_open(strategy, positions, orders_available=orders_available):
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
            absence = self.executor.confirm_position_absent(int(state_position.get("ticket") or 0))
            if absence is not True:
                self._set_sync_block(
                    strategy, "position_absence_not_confirmed", {"ticket": position_id, "absence": absence}, recoverable=True,
                )
                self._save_state()
                return False
            opened_at = max(0, int(state_position.get("open_time_epoch") or 0) - 60)
            deal = self.executor.get_position_close_deal(position_id, opened_at)
            if deal is False:
                deal = self.executor.get_position_close_deal(position_id, max(0, opened_at - 86400))
            if deal is None or deal is False:
                self._set_sync_block(strategy, "close_deal_not_confirmed", {"ticket": position_id}, recoverable=True)
                self._save_state()
                return False
            deal_time = int(getattr(deal, "deal_time", 0) or 0)
            open_time = int(state_position.get("open_time_epoch") or 0)
            if (
                int(deal.position_id) != position_id or str(deal.symbol) != symbol
                or not self._state_ownership_proven(strategy, state_position)
                or not math.isclose(float(getattr(deal, "exit_volume", 0.0)), float(state_position.get("lot") or 0.0), rel_tol=0.0, abs_tol=1e-9)
                or deal_time < open_time
            ):
                self._set_sync_block(strategy, "close_deal_ownership_mismatch", {"ticket": position_id}, recoverable=False)
                self._save_state()
                return False
            confirmed_closes.append((state_position, deal))
        if len(remaining) != len(state_positions):
            with self._close_state_transaction():
                state["positions"] = remaining
                pending_signal = state.get("pending_close_m5_bar")
                requested_at = parse_ts(state.get("pending_close_requested_at_utc"))
                decision_at = requested_at if requested_at is not None else pd.Timestamp(utc_now())
                pending_productive = state.get("pending_productive_close")
                for state_position, deal in sorted(confirmed_closes, key=lambda item: int(item[1].deal_time)):
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
                    if pending_productive is not None:
                        position_id = int(state_position.get("position_identifier") or state_position.get("ticket") or 0)
                        target_ids = {int(value) for value in pending_productive.get("position_ids", [])}
                        confirmed_ids = {int(value) for value in pending_productive.get("confirmed_ids", [])}
                        if position_id in target_ids and position_id not in confirmed_ids:
                            confirmed_ids.add(position_id)
                            pending_productive["confirmed_ids"] = sorted(confirmed_ids)
                            pending_productive["strategy_profit_usd"] = float(pending_productive.get("strategy_profit_usd", 0.0)) + float(getattr(deal, "profit", deal.net_profit))
                            pending_productive["last_deal_utc"] = dt_text(deal_time)
                if not any(position.get("close_requested") for position in remaining):
                    if pending_productive is not None:
                        target_ids = {int(value) for value in pending_productive.get("position_ids", [])}
                        confirmed_ids = {int(value) for value in pending_productive.get("confirmed_ids", [])}
                        strategy_profit = float(pending_productive.get("strategy_profit_usd", 0.0))
                        threshold = float(self.params.get("productive_close_threshold_usd", 0.10))
                        if target_ids and confirmed_ids == target_ids and strategy_profit + 1e-12 >= threshold:
                            productive_at = parse_ts(pending_productive.get("last_deal_utc"))
                            if productive_at is None:
                                productive_at = decision_at
                            state["last_productive_close_utc"] = dt_text(productive_at)
                            self._trade_row(
                                "productive_close_confirmed", strategy,
                                ticket_set=",".join(str(value) for value in sorted(target_ids)),
                                quote_time_utc=dt_text(productive_at), gross_profit=strategy_profit,
                                profit_basis="broker_gross_price_pnl_for_v23_clock",
                                reason=str(pending_productive.get("reason") or "native_productive_close"),
                                note=f"threshold_usd={threshold:.3f};tickets={len(target_ids)}",
                            )
                        state["pending_productive_close"] = None
                    state["pending_close_reason"] = None
                    state["pending_close_m5_bar"] = None
                    state["pending_close_requested_at_utc"] = None
                    state["close_retry_after_utc"] = None
                if (
                    (
                        state.get("sync_block_reason") in CLOSE_RECONCILIATION_RESOLVED_REASONS
                        or (
                            state.get("sync_block_reason") == "close_submission_unresolved"
                            and any(position.get("close_submission_started_utc") for position, _deal in confirmed_closes)
                        )
                    )
                    and not any(position.get("close_submission_started_utc") for position in remaining)
                ):
                    self._set_sync_block(strategy, None)
                    if not orders_available:
                        self._set_sync_block(strategy, "orders_unavailable", recoverable=True)
                self._save_state()
        recoverable_reason = str(state.get("sync_block_reason") or "")
        if (
            orders_available and state.get("sync_block_new_entries") and state.get("sync_block_recoverable")
            and (recoverable_reason in FULL_SYNC_RECOVERABLE_REASONS or recoverable_reason.startswith("broker_quote_"))
        ):
            self._set_sync_block(strategy, None)
            self._save_state()
        return True

    def _position_counts(self, strategy: dict[str, Any]) -> tuple[int, int]:
        positions = self._st(strategy)["positions"]
        return sum(position["side"] == "LONG" for position in positions), sum(position["side"] == "SHORT" for position in positions)

    def _logical_position_counts(self, strategy: dict[str, Any]) -> tuple[int, int]:
        """Count broker tickets plus any side whose core is already virtual."""
        long_count, short_count = self._position_counts(strategy)
        virtual_long, virtual_short = self._virtual_core_flags(strategy)
        return long_count + virtual_long, short_count + virtual_short

    def _virtual_core_flags(self, strategy: dict[str, Any]) -> tuple[int, int]:
        """Return virtual sides, suppressing overlap with a migrated physical core."""
        state = self._st(strategy)
        if state.get("episode_start_quote_utc") is None:
            return 0, 0
        legacy = state.get("legacy_physical_core_position_ids")
        if not isinstance(legacy, dict):
            legacy = {}
        live_ids_by_side: dict[str, set[int]] = {"LONG": set(), "SHORT": set()}
        for position in state.get("positions") or []:
            if not isinstance(position, dict) or position.get("side") not in live_ids_by_side:
                continue
            try:
                position_id = int(position.get("position_identifier") or position.get("ticket") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if position_id > 0:
                live_ids_by_side[str(position["side"])].add(position_id)
        flags: list[int] = []
        for side in ("LONG", "SHORT"):
            try:
                legacy_id = int(legacy.get(side) or 0)
            except (TypeError, ValueError, OverflowError):
                legacy_id = 0
            flags.append(0 if legacy_id > 0 and legacy_id in live_ids_by_side[side] else 1)
        return flags[0], flags[1]

    def _ensure_episode_identity(self, strategy: dict[str, Any]) -> str:
        state = self._st(strategy)
        if not state.get("current_episode_id"):
            state["episode_sequence"] = int(state.get("episode_sequence", 0)) + 1
            state["current_episode_id"] = f"s25_v24_e{int(state['episode_sequence']):06d}"
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

    def _entry_block_reason(self, strategy: dict[str, Any], info: Any, quote_time: pd.Timestamp) -> str | None:
        state = self._st(strategy)
        retry = parse_ts(state.get("entry_retry_until_utc"))
        if state.get("sync_block_new_entries"):
            return f"sync_block:{state.get('sync_block_reason') or 'unknown'}"
        if state.get("pending_open"):
            return "pending_open"
        if retry is not None and quote_time < retry:
            return "entry_retry_cooldown"
        if self._spread_points(info) > float(self.params.get("max_entry_spread_points", 300.0)):
            return "spread"
        contract_error = self._validate_symbol_contract(strategy, info)
        if contract_error:
            return f"symbol_contract:{contract_error}"
        return None

    def _entry_allowed(self, strategy: dict[str, Any], info: Any, quote_time: pd.Timestamp) -> bool:
        return self._entry_block_reason(strategy, info, quote_time) is None

    def _v23_drought_minority_blocked(
        self, strategy: dict[str, Any], side: str, quote_time: pd.Timestamp,
        long_count: int, short_count: int,
    ) -> bool:
        state = self._st(strategy)
        productive_at = parse_ts(state.get("last_productive_close_utc"))
        if productive_at is None:
            return False
        drought_minutes = float(self.params.get("productive_close_drought_minutes", 120.0))
        if quote_time - productive_at <= pd.Timedelta(minutes=drought_minutes):
            return False
        return long_count < short_count if side == "LONG" else short_count < long_count

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
            # Reservation logging and history/IPC work may outlive quote admission.
            # This branch has not called OPEN: clearing its reservation is safe.
            submission_clock_error = self._quote_clock_error(quote_time, strategy)
            if submission_clock_error:
                state["pending_open"] = None
                self._set_sync_block(strategy, submission_clock_error, recoverable=True)
                self._save_state()
                return False
            ticket = self.executor.open_position(
                symbol, ORDER_TYPE_BUY if side == "LONG" else ORDER_TYPE_SELL, lot, 0.0, 0.0,
                deviation=int(self.params.get("deviation_points", 50)), magic=int(strategy["magic"]),
                comment=comment, digits=digits, expected_login=int(MT5_LOGIN),
                expected_server=str(MT5_SERVER), expected_owned_positions=len(known),
            )
            error = str(getattr(self.executor, "last_order_error", None) or "")
            positions = self.executor.get_positions(symbol, int(strategy["magic"]))
            orders = self.executor.get_orders(symbol, int(strategy["magic"]))
            if positions is None or orders is None:
                self._set_sync_block(strategy, "inventory_unavailable_after_open", {"ticket": ticket or 0}, recoverable=False)
                self._save_state()
                return False
            live_tickets = [int(position.ticket) for position in positions]
            live_ids = [int(getattr(position, "identifier", 0) or 0) for position in positions]
            if len(live_tickets) != len(set(live_tickets)) or 0 in live_ids or len(live_ids) != len(set(live_ids)) or orders:
                self._set_sync_block(
                    strategy, "ambiguous_inventory_after_open",
                    {"ticket": ticket or 0, "live_tickets": live_tickets, "live_ids": live_ids, "order_count": len(orders)},
                    recoverable=False,
                )
                self._save_state()
                return False
            live_by_id = {int(position.identifier): position for position in positions}
            if (
                any(not self._owned_position(strategy, position) for position in positions)
                or any(
                    int(saved.get("position_identifier") or saved.get("ticket") or 0) not in live_by_id
                    or not self._state_matches_live(
                        strategy, saved,
                        live_by_id[int(saved.get("position_identifier") or saved.get("ticket") or 0)],
                    )
                    for saved in state["positions"]
                )
            ):
                self._set_sync_block(strategy, "post_open_inventory_ownership_mismatch", recoverable=False)
                self._save_state()
                return False
            new_owned = [
                position for position in positions
                if self._owned_position(strategy, position)
                and int(getattr(position, "identifier", 0) or position.ticket) not in known
            ]
            if ticket is not None:
                matches = [
                    position for position in new_owned
                    if int(position.ticket) == ticket
                    and self._side_from_record(position) == side
                    and str(getattr(position, "comment", "") or "") == comment
                    and math.isclose(float(position.volume), lot, rel_tol=0.0, abs_tol=1e-9)
                ]
                returned_identifier = int(getattr(self.executor, "last_open_identifier", 0) or 0)
                if len(matches) == 1 and int(getattr(matches[0], "identifier", 0) or 0) == returned_identifier and len(new_owned) == 1:
                    confirmed = matches[0]
            if confirmed is None:
                if not new_owned and self._open_error_is_definitive_no_fill(error):
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
        execution_time = quote_time
        if confirmed is not None:
            confirmed_state_position = self._state_position_from_live(strategy, confirmed)
            state["positions"].append(confirmed_state_position)
            parsed_execution_time = parse_ts(confirmed_state_position.get("entry_time_utc"))
            if parsed_execution_time is None:
                raise ValueError("confirmed broker position open time unavailable")
            execution_time = parsed_execution_time
        else:
            state["positions"].append({
                "ticket": int(ticket or 0), "position_identifier": position_id, "side": side, "lot": lot,
                "entry_price": entry_price, "entry_time_utc": dt_text(quote_time),
                "open_time_epoch": int(quote_time.timestamp()), "owner_symbol": symbol,
                "owner_magic": int(strategy["magic"]), "owner_comment": comment,
                "shadow": True, "close_requested": False,
                "close_submission_started_utc": None,
            })
        logged_ticket = int(getattr(confirmed, "ticket", 0) or ticket or 0)
        price_basis = "broker_confirmed_open" if confirmed is not None else "shadow_adverse_cost_proxy"
        causal["executable_at"] = dt_text(execution_time)
        self._trade_row(
            "entry", strategy, quote_time_utc=dt_text(execution_time), opportunity_id=opportunity_id,
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
        *, post_close_action: dict[str, Any] | None = None,
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
        productive_candidate = reason in PRODUCTIVE_CLOSE_REASONS
        if self.live_enabled:
            for position in selected:
                ticket = int(position["ticket"])
                live_position = self.executor.get_position(ticket)
                if live_position is None or live_position is False or not self._state_matches_live(strategy, position, live_position):
                    self._set_sync_block(strategy, "state_ticket_unowned_or_foreign", {"ticket": ticket}, recoverable=False)
                    self._save_state()
                    return "blocked"
            state["pending_close_reason"] = reason
            if post_close_action is not None:
                state["pending_post_close_action"] = copy.deepcopy(post_close_action)
            state["pending_close_m5_bar"] = m5_bar
            state["pending_close_requested_at_utc"] = dt_text(quote_time)
            state["pending_productive_close"] = (
                {
                    "reason": reason,
                    "position_ids": sorted(int(position.get("position_identifier") or position.get("ticket") or 0) for position in selected),
                    "confirmed_ids": [],
                    "strategy_profit_usd": 0.0,
                    "last_deal_utc": None,
                }
                if productive_candidate else None
            )
            for position in state["positions"]:
                if position_key(position) in selected_keys:
                    position["close_requested"] = True
                    position.setdefault("close_submission_started_utc", None)
            self._save_state()
            self._trade_row(
                "close_reserved", strategy, quote_time_utc=dt_text(quote_time),
                opportunity_id=opportunity_id, ticket_set=ticket_set, profit=mtm_profit,
                profit_basis="executable_mtm_before_close", reason=reason,
                spread_points=self._spread_points(info), **close_causal,
            )
            for position in selected:
                outcome, result = self._submit_reserved_close(strategy, position, quote_time)
                if outcome in {"submitted", "awaiting_confirmation"}:
                    continue
                status = str(getattr(result, "status", "FAILED"))
                if outcome == "retryable":
                    delay_key = "time_close_market_closed_retry_seconds" if (
                        status in {"MARKET_CLOSED", "TRADE_PERMISSION_GUARD"}
                        or int(getattr(result, "retcode", 0) or 0) in {10018, 10026, 10027}
                    ) else "close_retry_seconds"
                    state["close_retry_after_utc"] = dt_text(
                        quote_time + pd.Timedelta(seconds=float(self.params.get(delay_key, 60 if delay_key.startswith("time_close") else 15)))
                    )
                    if int(getattr(result, "retcode", 0) or 0) in {10026, 10027} or status == "TRADE_PERMISSION_GUARD":
                        state["trade_permission_reject_count"] = int(state.get("trade_permission_reject_count", 0)) + 1
                        if state["trade_permission_reject_count"] >= int(self.params.get("trade_permission_alert_threshold", 3)):
                            self._manual_alert(strategy, "repeated_trade_permission_reject", {"status": status, "count": state["trade_permission_reject_count"]})
                    continue
                return "blocked"
            if parse_ts(state.get("close_retry_after_utc")) is None or parse_ts(state.get("close_retry_after_utc")) <= quote_time:
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
        closed_profit = sum(float(row["profit"]) for row in closed_rows)
        for row in closed_rows:
            self._trade_row(
                "close", strategy, quote_time_utc=dt_text(quote_time),
                opportunity_id=opportunity_id, ticket_set=str(row["ticket"]),
                price_basis="shadow_adverse_cost_proxy", gross_profit=row["profit"],
                commission=0.0, swap=0.0, fee=0.0,
                profit_basis="shadow_cost_proxy_account_currency", reason=reason,
                spread_points=self._spread_points(info), **row, **close_causal,
            )
        threshold = float(self.params.get("productive_close_threshold_usd", 0.10))
        if productive_candidate and closed_rows and closed_profit + 1e-12 >= threshold:
            state["last_productive_close_utc"] = dt_text(quote_time)
            self._trade_row(
                "productive_close_confirmed", strategy, quote_time_utc=dt_text(quote_time),
                profit=closed_profit, profit_basis="shadow_cost_proxy_account_currency",
                reason=reason, note=f"threshold_usd={threshold:.3f};tickets={len(closed_rows)}",
            )
        self._save_state()
        return "completed"

    def _reset_episode(self, strategy: dict[str, Any], quote_time: pd.Timestamp) -> None:
        state = self._st(strategy)
        state["episode_start_quote_utc"] = None
        state["active_wave"] = 0
        state["last_long_frontier"] = None
        state["last_short_frontier"] = None
        state["last_productive_close_utc"] = None
        state["pending_productive_close"] = None
        state["drought_blocked_adds"] = 0
        state["pending_post_close_action"] = None
        state["pending_close_reason"] = None
        state["pending_close_m5_bar"] = None
        state["pending_close_requested_at_utc"] = None
        state["close_retry_after_utc"] = None
        state["close_defer"] = None
        state["legacy_physical_core_position_ids"] = {"LONG": None, "SHORT": None}
        state["current_episode_id"] = None
        state["skip_seed_quote_utc"] = dt_text(quote_time)
        self._save_state()

    def _ensure_virtual_bilateral_core(self, strategy: dict[str, Any], info: Any, quote_time: pd.Timestamp) -> bool:
        state = self._st(strategy)
        if state.get("skip_seed_quote_utc") == dt_text(quote_time):
            return False
        if state.get("episode_start_quote_utc") is None:
            if self._entry_block_reason(strategy, info, quote_time) is not None:
                return False
            episode_id = self._ensure_episode_identity(strategy)
            mid = 0.5 * (float(info.bid) + float(info.ask))
            state["episode_start_quote_utc"] = dt_text(quote_time)
            state["last_long_frontier"] = mid
            state["last_short_frontier"] = mid
            state["active_wave"] = 0
            self._trade_row(
                "episode_start", strategy, quote_time_utc=dt_text(quote_time),
                opportunity_id=f"v24_episode_{episode_id}", reason="virtual_bilateral_core_established",
                decision_time=dt_text(quote_time), executable_at=dt_text(quote_time),
                note="broker_seed_orders=0;virtual_long=1;virtual_short=1",
            )
            self._save_state()
        return state.get("episode_start_quote_utc") is not None

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
        if not self._ensure_virtual_bilateral_core(strategy, info, quote_time):
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
        submitted = 0
        for position in pending:
            if position.get("close_submission_started_utc"):
                continue
            ticket = int(position["ticket"])
            outcome, result = self._submit_reserved_close(strategy, position, quote_time)
            if outcome == "submitted":
                submitted += 1
                continue
            if outcome == "awaiting_confirmation":
                continue
            status = str(getattr(result, "status", "FAILED"))
            if outcome == "retryable":
                state["close_retry_after_utc"] = dt_text(quote_time + pd.Timedelta(seconds=float(self.params.get("time_close_market_closed_retry_seconds", 60))))
                self._trade_row(
                    "DEFER", strategy, quote_time_utc=dt_text(quote_time), reason=f"retryable_close:{status}",
                    opportunity_id=opportunity_id, ticket=ticket, ticket_set=ticket_set,
                    **causal,
                )
                self._save_state()
                return True
            return True
        state["close_retry_after_utc"] = dt_text(quote_time + pd.Timedelta(seconds=float(self.params.get("close_retry_seconds", 15))))
        if submitted:
            self._trade_row(
                "close_retry_requested", strategy, quote_time_utc=dt_text(quote_time),
                opportunity_id=opportunity_id, ticket_set=ticket_set,
                reason=state.get("pending_close_reason") or "pending_close",
                note=f"submitted={submitted};pending={len(pending)}", **causal,
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
        selected = select_profitable_real_positions(
            state["positions"], side, float(info.bid), float(info.ask), close_buffer,
        )
        if not selected:
            state["active_wave"] = int(new_wave)
            self._save_state()
            return False
        result = self._close_positions(
            strategy, selected, reason, info, quote_time, m5_bar,
            post_close_action={"new_wave": int(new_wave), "reason": reason, "m5_bar": m5_bar},
        )
        if result == "completed":
            self._ensure_virtual_bilateral_core(strategy, info, quote_time)
            state["active_wave"] = int(new_wave)
            self._save_state()
            return False
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
        atr = float(row.get("atr14", math.nan))
        ema = float(row.get("ema200", math.nan))
        if math.isfinite(atr) and math.isfinite(ema):
            state["last_atr"] = atr
            state["last_ema"] = ema
        available_at = bar_time + pd.Timedelta(minutes=5)
        if quote_time < available_at:
            self._trade_row("m5_not_evaluated", strategy, quote_time_utc=dt_text(quote_time), signal_bar_time=bar_key, reason="future_or_unavailable_completed_bar")
            # The diagnostic above is provisional, not a final decision receipt.
            # Reserving its receipt here would hide the later eligible decision.
            self._save_state()
            return
        # A pre-boundary quote postpones this bar; it must not consume it.
        # Keep the existing once-only semantics after availability is proven.
        state["last_processed_m5_bar"] = bar_key
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
        if not self._ensure_virtual_bilateral_core(strategy, info, quote_time):
            self._m5_receipt(strategy, bar_time, quote_time, reason="signal" if int(row.get("break_dir", 0)) else "no_signal", note="action=entry_blocked;virtual_core_not_started")
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
        if active == 0:
            self._m5_receipt(strategy, bar_time, quote_time, reason="signal" if new_break else "no_signal", note=f"action=no_add;break_dir={new_break};active_wave={active}")
            self._save_state()
            return
        entry_block_reason = self._entry_block_reason(strategy, info, quote_time)
        long_count, short_count = self._logical_position_counts(strategy)
        mid = 0.5 * (float(info.bid) + float(info.ask))
        step = float(strategy.get("frontier_add_atr", 0.50)) * atr
        max_side = int(strategy.get("max_positions_per_side", 6))
        ratio = int(strategy.get("max_active_to_opposite_ratio", 3))
        action = "no_add"
        opportunity_id = self._opportunity_id(bar_key, quote_time, "m5")
        side = "LONG" if active == 1 else "SHORT"
        side_count = long_count if side == "LONG" else short_count
        opposite_count = short_count if side == "LONG" else long_count
        frontier_key = "last_long_frontier" if side == "LONG" else "last_short_frontier"
        frontier = float(state.get(frontier_key) or mid)
        frontier_reached = mid >= frontier + step if side == "LONG" else mid <= frontier - step
        if frontier_reached:
            capacity_allowed = side_count < max_side
            ratio_allowed = side_count < ratio * opposite_count
            v23_blocked = self._v23_drought_minority_blocked(strategy, side, quote_time, long_count, short_count)
            execution_allowed = entry_block_reason is None
            self._register_frontier_observation(
                strategy, row, info, quote_time,
                opportunity_id=opportunity_id, side=side, frontier=frontier, atr=atr,
                capacity_allowed=capacity_allowed, ratio_allowed=ratio_allowed,
                v23_allowed=not v23_blocked, execution_allowed=execution_allowed,
            )
            route_reason = ""
            if not execution_allowed:
                action = "blocked_execution_gate"
                route_reason = str(entry_block_reason or "entry_gate")
            elif not capacity_allowed:
                action = "blocked_capacity"
                route_reason = "max_positions_per_side"
            elif not ratio_allowed:
                action = "blocked_ratio"
                route_reason = "max_active_to_opposite_ratio"
            elif v23_blocked:
                state["drought_blocked_adds"] = int(state.get("drought_blocked_adds", 0)) + 1
                action = "blocked_v23_drought_minority"
                route_reason = "v23_drought_minority_add_pause"
                self._trade_row(
                    "entry_blocked", strategy, quote_time_utc=dt_text(quote_time),
                    opportunity_id=opportunity_id, side=side,
                    reason=route_reason,
                    signal_bar_time=bar_key, spread_points=self._spread_points(info),
                    atr14=atr, ema200=ema,
                    note=f"long={long_count};short={short_count};blocked_total={state['drought_blocked_adds']}",
                )
            elif self._open_position(
                strategy, side, info, quote_time,
                "long_frontier_add" if side == "LONG" else "short_frontier_add",
                bar_key, opportunity_id,
            ):
                state[frontier_key] = mid
                action = "entry_long" if side == "LONG" else "entry_short"
                route_reason = "frontier_add_confirmed"
                self._save_state()
            else:
                action = "entry_long_failed" if side == "LONG" else "entry_short_failed"
                route_reason = "open_not_confirmed"
            self._record_frontier_route(
                opportunity_id,
                status="consumed" if action in {"entry_long", "entry_short"} else "unconsumed",
                reason=route_reason,
                quote_time=quote_time,
            )
        self._m5_receipt(
            strategy, bar_time, quote_time, reason="signal" if new_break else "no_signal",
            side="LONG" if active == 1 else "SHORT",
            note=f"action={action};break_dir={new_break};active_wave={active}",
        )
        self._save_state()

    def _run_strategy(self, strategy: dict[str, Any], bars: pd.DataFrame | None, info: Any, quote_time: pd.Timestamp) -> None:
        state = self._st(strategy)
        previous_quote = parse_ts(state.get("last_quote_utc"))
        shadow_inventory_hold = not self.live_enabled and self._has_real_state_positions(strategy)
        if not self._sync_strategy(strategy):
            if shadow_inventory_hold:
                logging.critical("S25 shadow canary halted after read-only inventory mismatch")
                return
            state["last_quote_utc"] = dt_text(quote_time)
            self._save_state()
            return
        if shadow_inventory_hold:
            self._trade_row(
                "m5_not_evaluated",
                strategy,
                quote_time_utc=dt_text(quote_time),
                reason="legacy_inventory_shadow_canary_hold",
                note="read_only_owned_sync;entries_and_exits_disabled_until_explicit_live_activation",
            )
            return
        if any(position.get("close_requested") for position in state["positions"]):
            self._retry_pending_close_requests(strategy, quote_time)
            state["last_quote_utc"] = dt_text(quote_time)
            self._save_state()
            return
        episode_active = state.get("episode_start_quote_utc") is not None
        if episode_active and previous_quote is not None and quote_time - previous_quote > pd.Timedelta(minutes=float(self.params.get("feed_gap_minutes", 5))):
            self._arm_full_close(strategy, "feed_gap", quote_time)
        episode_start = parse_ts(state.get("episode_start_quote_utc"))
        if episode_start is not None and quote_time - episode_start >= pd.Timedelta(minutes=float(self.params.get("episode_minutes", 720))):
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
        quote_clock_error = self._quote_clock_error(quote_time, strategy)
        if quote_clock_error:
            # Read-only ownership/deal reconciliation does not require a fresh
            # executable quote. Never progress entries or quote-timed exits here.
            self._sync_strategy(strategy)
            self._set_sync_block(strategy, quote_clock_error, recoverable=True)
            self._save_state()
            return
        self._observe_quote(info, quote_time)
        bars = self._get_m5()
        self._run_strategy(strategy, bars, info, quote_time)
        now = time.time()
        if now - self._last_status_log >= float(self.params.get("status_log_interval_seconds", 300)):
            long_count, short_count = self._position_counts(strategy)
            logical_long, logical_short = self._logical_position_counts(strategy)
            logging.info("S25 V24 status live=%s shadow=%s real_long=%d real_short=%d logical_long=%d logical_short=%d wave=%s drought_blocks=%d sync_block=%s", self.live_enabled, self.shadow_enabled, long_count, short_count, logical_long, logical_short, self._st(strategy).get("active_wave"), int(self._st(strategy).get("drought_blocked_adds", 0)), self._st(strategy).get("sync_block_reason"))
            self._last_status_log = now


# Compatibility aliases for older test/evidence imports; runtime identity is V24.
S25V23Runner = S25V24Runner
S25Man231Runner = S25V24Runner


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
        self.last_open_identifier = None
        self.last_open_deal = None
        self.last_open_price = None
        self.next_ticket = 1000
        self.next_deal = 12000
        self.deals: dict[int, Any] = {}
        self.info = SimpleNamespace(bid=4020.0, ask=4020.18, point=0.001, volume_min=0.01, volume_max=100.0, volume_step=0.01, digits=3, stops_level=0, quote_time_msc=int(pd.Timestamp(quote_time).timestamp() * 1000))

    def get_bridge_capabilities(self) -> dict[str, Any]:
        return {"name": "BotBridge_s25", "version": "2026-09-04-s25-v24-atomic-v8", "commands": set(REQUIRED_SHARED_ACCOUNT_COMMANDS)}

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
        self.last_open_identifier = int(record.identifier)
        self.last_open_deal = self.next_deal
        self.last_open_price = side_price
        return self.next_ticket

    def close_position(self, ticket: int, *_: Any, **__: Any) -> Any:
        position = self.get_position(ticket)
        if position is False:
            return CloseResult(False, status="MISSING_UNCONFIRMED")
        self.positions = [row for row in self.positions if int(row.ticket) != int(ticket)]
        self.next_deal += 1
        self.deals[int(position.identifier)] = SimpleNamespace(deal=self.next_deal, position_id=int(position.identifier), symbol=position.symbol, magic=position.magic, reason="EXPERT", price=self.info.bid if position.type == ORDER_TYPE_BUY else self.info.ask, profit=0.25, commission=-0.02, swap=-0.01, fee=0.0, net_profit=0.22, deal_time=int(self.info.quote_time_msc / 1000), exit_volume=float(position.volume))
        return CloseResult(True, status="CONFIRMED")


def load_params(path: str = PARAMS_FILE) -> dict[str, Any]:
    with open(path, "rb") as handle:
        params = strict_json_load_bytes(handle.read())
    if not isinstance(params, dict):
        raise ValueError("S25 params root must be an object")
    return params


def self_test() -> None:
    configured = load_params()
    assert type(configured["live_trading_enabled"]) is bool
    assert type(configured["shadow_forward_enabled"]) is bool
    assert configured["live_trading_enabled"] != configured["shadow_forward_enabled"]
    encoded_spec = json.dumps(V24_CANDIDATE_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(encoded_spec).hexdigest() == V24_CANDIDATE_HASH == configured["candidate_params_hash"]
    assert configured["strategies"][0]["physical_seed_orders"] == 0
    assert configured["strategies"][0]["virtual_core_positions_per_side"] == 1
    params = json.loads(json.dumps(configured))
    params["live_trading_enabled"] = False
    params["shadow_forward_enabled"] = True
    strategy = params["strategies"][0]

    retired_state = {
        "version": STATE_VERSION - 1,
        "bot": "bot25",
        "strategy_id": "retired_bot25_strategy",
        "last_saved_utc": "2026-08-26T00:00:00+00:00",
        "strategies": {},
    }
    atomic_write_json(STATE_FILE, retired_state)
    retired_runner = S25Man231Runner(params)
    retired_runner._suppress_manual_alerts = True
    retired_runner.dm = FakeDM()
    retired_runner.executor = FakeExecutor()
    assert retired_runner._state_identity_status == "retired_bot25"
    assert retired_runner.connect_and_preflight(), "flat retired bot25 state must migrate after preflight"
    with open(STATE_FILE, "r", encoding="utf-8") as handle:
        migrated_state = json.load(handle)
    assert migrated_state["bot"] == "bot25" and migrated_state["strategy_id"] == params["strategy_id"]
    assert int(migrated_state["version"]) == STATE_VERSION
    retired_dir = os.path.join(os.path.dirname(STATE_FILE), "old")
    archived_after_flat = [name for name in os.listdir(retired_dir) if name.startswith("s25_bot_state_retired_")]
    assert len(archived_after_flat) == 1
    with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as handle:
        retired_rows = list(csv.DictReader(handle))
    assert any(row["event"] == "startup_state_retired" for row in retired_rows)

    exact_old_strategy_state = retired_runner._default_strategy_state()
    exact_old_state = {
        "version": PREVIOUS_STATE_VERSION,
        "bot": "bot25",
        "strategy_id": PREVIOUS_STRATEGY_ID,
        "last_saved_utc": "2026-08-27T00:00:00+00:00",
        "strategies": {PREVIOUS_STRATEGY_KEY: exact_old_strategy_state},
    }
    atomic_write_json(STATE_FILE, exact_old_state)
    compatible_runner = S25V23Runner(params)
    compatible_runner._suppress_manual_alerts = True
    compatible_runner.dm = FakeDM()
    compatible_runner.executor = FakeExecutor()
    assert compatible_runner._state_identity_status == "compatible_legacy_to_v24_pending"
    assert compatible_runner.connect_and_preflight(), "exact flat V23 state must upgrade in place"
    assert compatible_runner.state["version"] == STATE_VERSION
    assert compatible_runner.state["strategy_id"] == params["strategy_id"]
    assert set(compatible_runner.state["strategies"]) == {strategy["id"]}
    assert compatible_runner._st(strategy)["last_productive_close_utc"] is None
    with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as handle:
        compatible_rows = list(csv.DictReader(handle))
    assert any(row["event"] == "startup_state_migrated" for row in compatible_rows)

    atomic_write_json(STATE_FILE, retired_state)
    occupied = SimpleNamespace(
        ticket=901, identifier=901, symbol=params.get("mt5_symbol", params["symbol"]),
        magic=int(strategy["magic"]), comment="retired_s25_position", type=ORDER_TYPE_BUY,
    )
    occupied_runner = S25Man231Runner(params)
    occupied_runner._suppress_manual_alerts = True
    occupied_runner.dm = FakeDM()
    occupied_runner.executor = FakeExecutor(positions=[occupied])
    assert not occupied_runner.connect_and_preflight(), "retired state with scoped inventory must stay blocked"
    assert os.path.exists(STATE_FILE)
    assert len([name for name in os.listdir(retired_dir) if name.startswith("s25_bot_state_retired_")]) == 1

    class UnavailableOrdersExecutor(FakeExecutor):
        def get_orders(self, *_: Any, **__: Any) -> None:
            return None

    atomic_write_json(STATE_FILE, retired_state)
    unavailable_runner = S25Man231Runner(params)
    unavailable_runner._suppress_manual_alerts = True
    unavailable_runner.dm = FakeDM()
    unavailable_runner.executor = UnavailableOrdersExecutor()
    assert not unavailable_runner.connect_and_preflight(), "unknown order inventory must stay blocked"
    assert os.path.exists(STATE_FILE)

    atomic_write_json(STATE_FILE, retired_state)
    changed_runner = S25Man231Runner(params)
    changed_runner._suppress_manual_alerts = True
    changed_runner.dm = FakeDM()
    changed_runner.executor = FakeExecutor()
    changed_state = dict(retired_state)
    changed_state["last_saved_utc"] = "2026-08-26T00:00:01+00:00"
    atomic_write_json(STATE_FILE, changed_state)
    assert not changed_runner.connect_and_preflight(), "changed retired state must fail CAS and stay blocked"
    assert os.path.exists(STATE_FILE)

    atomic_write_json(STATE_FILE, migrated_state)
    os.remove(TRADE_LOG_FILE)
    runner = S25Man231Runner(params)
    runner.state = runner._default_state()
    runner._save_state = lambda: None
    runner._suppress_manual_alerts = True
    runner.dm = FakeDM()
    runner.executor = FakeExecutor()
    assert runner._ownership_namespace_error() is None
    assert runner.connect_and_preflight()
    runner.run_once()
    long_count, short_count = runner._position_counts(strategy)
    assert (long_count, short_count) == (0, 0), "virtual startup must not create broker seed positions"
    assert runner._logical_position_counts(strategy) == (1, 1)
    state = runner._st(strategy)
    assert state["episode_start_quote_utc"] is not None
    assert state["current_episode_id"] == "s25_v24_e000001"
    assert state["last_decision_receipt_m5_bar"] is not None
    with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as handle:
        startup_rows = list(csv.DictReader(handle))
    startup_events = [row["event"] for row in startup_rows]
    assert startup_events.count("startup_recovery") == 1
    assert startup_events.count("entry") == 0 and startup_events.count("episode_start") == 1
    assert startup_events.count("m5_decision") == 1
    episode_row = next(row for row in startup_rows if row["event"] == "episode_start")
    assert episode_row["reason"] == "virtual_bilateral_core_established"
    assert episode_row["long_positions"] == "0" and episode_row["short_positions"] == "0"
    assert episode_row["logical_long_positions"] == "1" and episode_row["logical_short_positions"] == "1"
    assert episode_row["virtual_core_long"] == "1" and episode_row["virtual_core_short"] == "1"
    decision = next(row for row in startup_rows if row["event"] == "m5_decision")
    assert decision["reason"] in {"signal", "no_signal"}
    assert parse_ts(decision["available_time"]) <= parse_ts(decision["decision_time"]) <= parse_ts(decision["executable_at"])

    block_runner = S25V23Runner(params)
    block_runner.state = block_runner._default_state()
    block_runner._save_state = lambda: None
    block_runner._suppress_manual_alerts = True
    block_state = block_runner._st(strategy)
    block_state["current_episode_id"] = "s25_m231_e000901"
    block_state["episode_start_quote_utc"] = "2026-08-26T12:00:00+00:00"
    block_state["active_wave"] = 1
    block_state["last_long_frontier"] = 4019.0
    block_state["last_short_frontier"] = 4020.0
    block_state["last_productive_close_utc"] = "2026-08-26T22:24:00+00:00"
    block_state["positions"] = [
        {"ticket": -901, "position_identifier": -901, "side": "LONG", "lot": 0.01, "entry_price": 4019.0, "owner_comment": "s25_m231_L0901", "shadow": True},
        {"ticket": -902, "position_identifier": -902, "side": "SHORT", "lot": 0.01, "entry_price": 4021.0, "owner_comment": "s25_m231_S0902", "shadow": True},
        {"ticket": -903, "position_identifier": -903, "side": "SHORT", "lot": 0.01, "entry_price": 4022.0, "owner_comment": "s25_m231_S0903", "shadow": True},
    ]
    block_quote = pd.Timestamp("2026-08-27T00:25:00Z")
    assert block_runner._v23_drought_minority_blocked(strategy, "LONG", block_quote, 1, 2)
    assert not block_runner._v23_drought_minority_blocked(strategy, "SHORT", block_quote, 1, 2)
    assert not block_runner._v23_drought_minority_blocked(strategy, "LONG", pd.Timestamp("2026-08-27T00:24:00Z"), 1, 2), "exactly 120 minutes is not a drought"
    block_row = pd.Series({"atr14": 1.0, "ema200": 4000.0, "break_dir": 0}, name=pd.Timestamp("2026-08-27T00:20:00Z"))
    block_runner._process_m5_event(strategy, block_row, FakeExecutor().info, block_quote)
    assert block_runner._position_counts(strategy) == (1, 2)
    assert block_state["drought_blocked_adds"] == 1 and block_state["last_long_frontier"] == 4019.0
    observer_rows = list(csv.DictReader(open(
        os.path.join(os.path.dirname(TRADE_LOG_FILE), "s25_shadow_opportunities.csv"),
        "r", newline="", encoding="utf-8",
    )))
    block_observation = next(row for row in observer_rows if row["opportunity_id"] == "m231_m5_20260827T002000Z" and row["event"] == "route_update")
    assert block_observation["route_status"] == "unconsumed"
    assert block_observation["route_reason"] == "v23_drought_minority_add_pause"
    tag_rows = list(csv.DictReader(open(
        os.path.join(os.path.dirname(TRADE_LOG_FILE), "s25_shadow_state_tags.csv"),
        "r", newline="", encoding="utf-8",
    )))
    assert any(row["opportunity_id"] == "m231_m5_20260827T002000Z" for row in tag_rows)

    class BrokenObserver:
        def register_opportunity(self, *_: Any, **__: Any) -> None:
            raise OSError("observer-write-failure")

        def record_route(self, *_: Any, **__: Any) -> None:
            raise OSError("observer-route-failure")

        def observe_quote(self, *_: Any, **__: Any) -> None:
            raise OSError("observer-quote-failure")

    class BrokenTagger:
        def record(self, *_: Any, **__: Any) -> None:
            raise OSError("tagger-write-failure")

    passive_failure = S25V23Runner(params)
    passive_failure.state = passive_failure._default_state()
    passive_failure._save_state = lambda: None
    passive_failure._suppress_manual_alerts = True
    passive_failure.shadow_observer = BrokenObserver()
    passive_failure.shadow_state_tagger = BrokenTagger()
    passive_state = passive_failure._st(strategy)
    passive_state["current_episode_id"] = "s25_m231_e000902"
    passive_state["episode_start_quote_utc"] = "2026-08-27T00:00:00+00:00"
    passive_state["active_wave"] = 1
    passive_state["last_long_frontier"] = 4019.0
    passive_state["last_short_frontier"] = 4020.0
    passive_state["positions"] = [
        {"ticket": -911, "position_identifier": -911, "side": "LONG", "lot": 0.01, "entry_price": 4019.0, "owner_comment": "s25_m231_L0911", "shadow": True},
        {"ticket": -912, "position_identifier": -912, "side": "SHORT", "lot": 0.01, "entry_price": 4021.0, "owner_comment": "s25_m231_S0912", "shadow": True},
    ]
    passive_failure._observe_quote(FakeExecutor().info, block_quote)
    passive_failure._process_m5_event(
        strategy,
        pd.Series({"Open": 4019.0, "High": 4021.0, "Low": 4018.0, "Close": 4020.0, "Volume": 10, "atr14": 1.0, "ema200": 4000.0, "break_dir": 0}, name=pd.Timestamp("2026-08-27T00:20:00Z")),
        FakeExecutor().info,
        block_quote,
    )
    assert passive_failure._position_counts(strategy) == (2, 1), "passive observer failure must not alter the entry path"
    with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as handle:
        v23_rows = list(csv.DictReader(handle))
    assert any(row["event"] == "entry_blocked" and row["reason"] == "v23_drought_minority_add_pause" for row in v23_rows)

    future_runner = S25Man231Runner(params)
    future_runner.state = future_runner._default_state()
    future_runner._save_state = lambda: None
    future_runner._suppress_manual_alerts = True
    future_row = pd.Series({"atr14": 1.0, "ema200": 4020.0, "break_dir": 0}, name=pd.Timestamp("2026-08-27T00:25:00Z"))
    future_runner._process_m5_event(strategy, future_row, FakeExecutor().info, pd.Timestamp("2026-08-27T00:25:00Z"))
    assert future_runner._st(strategy)["positions"] == []
    with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as handle:
        future_rows = list(csv.DictReader(handle))
    future_decision = next(row for row in future_rows if row["event"] == "m5_not_evaluated" and row["reason"] == "future_or_unavailable_completed_bar")
    assert not future_decision["executable_at"]
    assert future_runner._st(strategy).get("last_processed_m5_bar") != dt_text(future_row.name)
    assert future_runner._st(strategy).get("last_decision_receipt_m5_bar") != dt_text(future_row.name)

    legacy_path = os.path.join(os.path.dirname(TRADE_LOG_FILE), "legacy_s25_trades.csv")
    with open(legacy_path, "w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerow(["old", "schema"])
    append_csv(legacy_path, {"event": "schema_restarted"}, TRADE_FIELDS)
    with open(legacy_path, "r", newline="", encoding="utf-8") as handle:
        assert next(csv.reader(handle)) == TRADE_FIELDS
    with open(legacy_path, "r", newline="", encoding="utf-8") as handle:
        rollover_rows = list(csv.DictReader(handle))
    assert [row["event"] for row in rollover_rows] == ["schema_rollover", "schema_restarted"]
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
    selected = select_profitable_real_positions(positions, "LONG", 4011.0, 4011.2)
    assert [position["ticket"] for position in selected] == [3, 2, 1], "V24 virtual core must allow every profitable real LONG to close LIFO"
    selected = select_profitable_real_positions(short_positions, "SHORT", 3998.8, 3999.0)
    assert [position["ticket"] for position in selected] == [6, 5, 4], "V24 virtual core must allow every profitable real SHORT to close LIFO"

    release_all = S25V24Runner(params)
    release_all.state = release_all._default_state()
    release_all._save_state = lambda: None
    release_all._suppress_manual_alerts = True
    release_state = release_all._st(strategy)
    release_state["current_episode_id"] = "s25_v24_e000778"
    release_state["episode_start_quote_utc"] = "2026-08-27T00:00:00+00:00"
    release_state["active_wave"] = 1
    release_state["positions"] = [
        {"ticket": -31, "position_identifier": -31, "side": "LONG", "lot": 0.01, "entry_price": 4010.0, "owner_comment": "s25_m231_L0031"},
        {"ticket": -32, "position_identifier": -32, "side": "LONG", "lot": 0.01, "entry_price": 4015.0, "owner_comment": "s25_m231_L0032"},
        {"ticket": -33, "position_identifier": -33, "side": "LONG", "lot": 0.01, "entry_price": 4018.0, "owner_comment": "s25_m231_L0033"},
        {"ticket": -34, "position_identifier": -34, "side": "SHORT", "lot": 0.01, "entry_price": 4030.0, "owner_comment": "s25_m231_S0034"},
    ]
    release_info = SimpleNamespace(bid=4020.0, ask=4020.18, point=0.001)
    assert not release_all._release_active_side(
        strategy, -1, "opposite_pivot_break", release_info,
        pd.Timestamp("2026-08-27T00:25:00Z"), "2026-08-27T00:20:00+00:00",
    )
    assert release_all._position_counts(strategy) == (0, 1)
    assert release_all._logical_position_counts(strategy) == (1, 2)
    assert release_state["active_wave"] == -1

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
    assert shadow_close._close_positions(strategy, [shadow_state["positions"][1]], "opposite_pivot_break", shadow_info, pd.Timestamp("2026-08-27T00:25:00Z"), "2026-08-27T00:20:00Z") == "completed"
    with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as handle:
        shadow_rows = list(csv.DictReader(handle))
    shadow_logged = next(row for row in reversed(shadow_rows) if row["event"] == "close" and row["ticket"] == "-12")
    assert shadow_logged["long_positions"] == "1" and shadow_logged["short_positions"] == "1"
    assert shadow_logged["price_basis"] == "shadow_adverse_cost_proxy" and shadow_logged["profit_basis"] == "shadow_cost_proxy_account_currency"
    assert abs(float(shadow_logged["profit"]) - float(shadow_logged["gross_profit"])) < 1e-12
    assert shadow_state["last_productive_close_utc"] == "2026-08-27T00:25:00+00:00"
    assert any(row["event"] == "productive_close_confirmed" for row in shadow_rows)

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

    virtual_expiry = S25V24Runner(params)
    virtual_expiry.state = virtual_expiry._default_state()
    virtual_expiry._save_state = lambda: None
    virtual_expiry._suppress_manual_alerts = True
    virtual_expiry.executor = FakeExecutor()
    virtual_state = virtual_expiry._st(strategy)
    virtual_state["current_episode_id"] = "s25_v24_e000777"
    virtual_state["episode_start_quote_utc"] = "2026-08-26T12:25:00+00:00"
    virtual_state["last_quote_utc"] = "2026-08-27T00:24:59+00:00"
    virtual_expiry._run_strategy(strategy, None, shadow_info, pd.Timestamp("2026-08-27T00:25:01Z"))
    assert virtual_state["positions"] == [] and virtual_state["current_episode_id"] is None
    assert virtual_state["episode_start_quote_utc"] is None, "virtual-only episode must expire without a broker close"

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

    retained_runner = S25Man231Runner(params)
    retained_runner.state = retained_runner._default_state()
    retained_runner._save_state = lambda: None
    retained_runner._suppress_manual_alerts = True
    retained_runner._set_sync_block(strategy, "ambiguous_open_result", {"ticket": 123}, recoverable=False)
    retained_runner._set_sync_block(strategy, "positions_unavailable", recoverable=True)
    retained_state = retained_runner._st(strategy)
    assert retained_state["sync_block_reason"] == "ambiguous_open_result"
    assert retained_state["sync_block_recoverable"] is False

    matching_state_position = {
        "ticket": 321, "position_identifier": 4321, "side": "LONG", "lot": 0.01,
        "owner_symbol": "XAUUSD", "owner_magic": EXPECTED_S25_MAGIC,
        "owner_comment": "s25_m231_L0321",
    }
    matching_live_position = SimpleNamespace(
        ticket=321, identifier=4321, symbol="XAUUSD", type=ORDER_TYPE_BUY,
        volume=0.01, magic=EXPECTED_S25_MAGIC, comment="s25_m231_L0321",
    )
    assert retained_runner._state_matches_live(strategy, matching_state_position, matching_live_position)
    mismatched_volume = SimpleNamespace(**{**vars(matching_live_position), "volume": 0.02})
    assert not retained_runner._state_matches_live(strategy, matching_state_position, mismatched_volume)

    live_params = json.loads(json.dumps(params))
    live_params["live_trading_enabled"] = True
    live_params["shadow_forward_enabled"] = False
    old_gate = os.environ.pop(str(params["real_trading_activation_env"]), None)
    try:
        gated = S25Man231Runner(live_params)
        assert gated.activation_error and not gated.live_enabled, "legacy params alone must not enable real orders"
        os.environ[str(params["real_trading_activation_env"])] = str(params["real_trading_activation_value"])

        live_old_position = SimpleNamespace(
            ticket=490, identifier=5490, symbol="XAUUSD", type=ORDER_TYPE_BUY,
            volume=0.01, open_price=4020.18, sl=0.0, tp=0.0, profit=0.0,
            magic=EXPECTED_S25_MAGIC,
            open_time=int(pd.Timestamp("2026-08-27T00:00:00Z").timestamp()),
            comment="s25_m231_L0490",
        )
        live_old_strategy_state = compatible_runner._default_strategy_state()
        live_old_strategy_state["positions"] = [{
            "ticket": 490, "position_identifier": 5490, "side": "LONG", "lot": 0.01,
            "entry_price": 4020.18, "entry_time_utc": "2026-08-27T00:00:00+00:00",
            "open_time_epoch": int(pd.Timestamp("2026-08-27T00:00:00Z").timestamp()),
            "owner_symbol": "XAUUSD", "owner_magic": EXPECTED_S25_MAGIC,
            "owner_comment": "s25_m231_L0490", "shadow": False, "close_requested": False,
        }]
        live_old_strategy_state["current_episode_id"] = "s25_m231_e000490"
        live_old_strategy_state["episode_start_quote_utc"] = "2026-08-27T00:00:00+00:00"
        atomic_write_json(STATE_FILE, {
            "version": PREVIOUS_STATE_VERSION, "bot": "bot25",
            "strategy_id": PREVIOUS_STRATEGY_ID, "last_saved_utc": "2026-08-27T00:20:00+00:00",
            "strategies": {PREVIOUS_STRATEGY_KEY: live_old_strategy_state},
        })
        live_upgrade = S25V23Runner(live_params)
        live_upgrade._suppress_manual_alerts = True
        live_upgrade.dm = FakeDM()
        live_upgrade.executor = FakeExecutor(positions=[live_old_position])
        assert live_upgrade._state_identity_status == "compatible_legacy_to_v24_pending"
        assert live_upgrade.connect_and_preflight(), "exact owned V23 inventory must migrate without new orders"
        assert live_upgrade._state_identity_status == "current"
        assert len(live_upgrade.executor.positions) == 1
        assert live_upgrade._logical_position_counts(strategy) == (1, 1)
        assert live_upgrade._virtual_core_flags(strategy) == (0, 1)

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
                self.last_open_identifier = 1777
                self.last_open_deal = 2777
                self.last_open_price = record.open_price
                return 777

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
                self.last_order_error = "ERR|10026|ORDER=0|DEAL=0|LAST=0"
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
                return CloseResult(False, status="MARKET_CLOSED", retcode=10018)

        market_position = SimpleNamespace(ticket=579, identifier=5579, symbol="XAUUSD", type=ORDER_TYPE_BUY, volume=0.01, open_price=4010.0, sl=0.0, tp=0.0, profit=0.0, magic=EXPECTED_S25_MAGIC, open_time=int(pd.Timestamp("2026-08-27T00:00:00Z").timestamp()), comment="s25_m231_L0579")
        market_closed = S25Man231Runner(live_params)
        market_closed.state = market_closed._default_state()
        market_closed._save_state = lambda: None
        market_closed._suppress_manual_alerts = True
        market_closed.executor = MarketClosedExecutor(positions=[market_position])
        market_state = market_closed._st(strategy)
        market_closed._ensure_episode_identity(strategy)
        market_state["positions"] = [market_closed._state_position_from_live(strategy, market_position)]
        assert market_closed._close_positions(strategy, list(market_state["positions"]), "market_closed_test", market_closed.executor.info, pd.Timestamp("2026-08-27T00:25:00Z"), None) == "requested"
        assert market_state["positions"][0]["close_requested"] and market_state["close_defer"] is None
        assert market_state["positions"][0]["close_submission_started_utc"] is None
        with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as handle:
            market_rows = list(csv.DictReader(handle))
        assert any(row["event"] == "close_requested" and row["reason"] == "market_closed_test" for row in market_rows)

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
        assert live_close._close_positions(strategy, list(live_close_state["positions"]), "opposite_pivot_break", live_close.executor.info, pd.Timestamp("2026-08-27T00:25:00Z"), "2026-08-27T00:20:00Z") == "requested"
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
        assert live_close_state["last_productive_close_utc"] is not None
        productive_row = next(row for row in reversed(live_close_rows) if row["event"] == "productive_close_confirmed")
        assert productive_row["gross_profit"] and not productive_row["profit"]
        assert productive_row["profit_basis"] == "broker_gross_price_pnl_for_v23_clock"

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
            {"ticket": 701, "position_identifier": 7701, "side": "LONG", "lot": 0.01, "entry_price": 4010.0, "entry_time_utc": "2026-08-27T00:00:00+00:00", "open_time_epoch": int(pd.Timestamp("2026-08-27T00:00:00Z").timestamp()), "owner_symbol": "XAUUSD", "owner_magic": EXPECTED_S25_MAGIC, "owner_comment": "s25_m231_L0701", "shadow": False, "close_requested": True, "close_submission_started_utc": "2026-08-27T00:25:00+00:00"},
            {"ticket": 702, "position_identifier": 7702, "side": "SHORT", "lot": 0.01, "entry_price": 4030.0, "entry_time_utc": "2026-08-27T00:00:00+00:00", "open_time_epoch": int(pd.Timestamp("2026-08-27T00:00:00Z").timestamp()), "owner_symbol": "XAUUSD", "owner_magic": EXPECTED_S25_MAGIC, "owner_comment": "s25_m231_S0702", "shadow": False, "close_requested": True, "close_submission_started_utc": "2026-08-27T00:25:00+00:00"},
        ]
        two_state["pending_close_reason"] = "two_phase_test"
        two_state["pending_close_m5_bar"] = "2026-08-27T00:20:00+00:00"
        two_state["pending_close_requested_at_utc"] = "2026-08-27T00:25:00+00:00"
        def fake_deal(deal_id: int, position_id: int, price: float) -> Any:
            return SimpleNamespace(deal=deal_id, position_id=position_id, symbol="XAUUSD", magic=EXPECTED_S25_MAGIC, reason="EXPERT", price=price, profit=1.25, commission=-0.05, swap=-0.02, fee=0.0, net_profit=1.18, deal_time=int(pd.Timestamp("2026-08-27T00:25:01Z").timestamp()), exit_volume=0.01)
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
    global TRADE_LOG_FILE, STATE_FILE, _SELF_TEST_HISTORICAL_QUOTES
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
        _SELF_TEST_HISTORICAL_QUOTES = True
        try:
            with tempfile.TemporaryDirectory(prefix="s25-self-test-") as temp_dir:
                TRADE_LOG_FILE = os.path.join(temp_dir, "s25_trades.csv")
                STATE_FILE = os.path.join(temp_dir, "s25_bot_state.json")
                self_test()
        finally:
            _SELF_TEST_HISTORICAL_QUOTES = False
            TRADE_LOG_FILE = original_trade_log
            STATE_FILE = original_state_file
        print("s25 V24 virtual-core self-test ok")
        return 0
    runner_lock = acquire_runner_singleton_lock()
    if runner_lock is None:
        logging.critical("Another bot25 runner already owns the state/order namespace; refusing to start")
        return 1
    try:
        runner = S25V24Runner(params)
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
