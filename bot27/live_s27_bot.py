from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from types import SimpleNamespace
from typing import Any

import pandas as pd

os.environ.setdefault("BOT_SUFFIX", "s27")

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
from live_safety import (
    LiveSafetyOptions,
    clean_sync_block_if_flat,
    clear_recoverable_sync_block_after_clean_sync,
    lot_contract_error,
    stale_signal_decision,
)
try:
    from live_config import MIN_LOT_OVERRIDES, MT5_LOGIN, MT5_SERVER
except ImportError:
    MIN_LOT_OVERRIDES: dict[str, float] = {}
    MT5_LOGIN = 0
    MT5_SERVER = ""
from protocol_v2_strategy import latest_signal
try:
    from live_manual_alerts import notify_manual_action_required
except ImportError:
    def notify_manual_action_required(**_kwargs: Any) -> bool:
        return False
try:
    from protocol_v2_strategy import short_overlay_signals
except ImportError:
    short_overlay_signals = None
try:
    from shadow_opportunity_observer import ShadowOpportunityObserver
except ImportError:
    ShadowOpportunityObserver = None  # type: ignore[assignment,misc]
try:
    from shadow_state_tagger import ShadowStateTagger
except ImportError:
    ShadowStateTagger = None  # type: ignore[assignment,misc]


UTC = timezone.utc
BOT_SUFFIX = os.environ.get("BOT_SUFFIX", "s27")
EXPECTED_MAGIC = int(os.environ.get("EXPECTED_MAGIC", "200027"))
TIME_CLOSE_RETRYABLE_RETCODES = {"10018"}
REPEATABLE_DIAGNOSTIC_REASONS = {"target_m1_unavailable", "context_m1_unavailable"}
PARAMS_FILE = os.path.join(SCRIPT_DIR, f"{BOT_SUFFIX}_params.json")
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
STATE_DIR = os.path.join(SCRIPT_DIR, "state")
LOG_FILE = os.path.join(LOG_DIR, f"{BOT_SUFFIX}_bot.log")
TRADE_LOG_FILE = os.path.join(LOG_DIR, f"{BOT_SUFFIX}_trades.csv")
STATE_FILE = os.path.join(STATE_DIR, f"{BOT_SUFFIX}_bot_state.json")
FLAT_AUTO_CLEAR_SYNC_REASONS = {
    "open_success_position_not_confirmed",
    "live_time_close_failed",
    "live_time_close_unconfirmed",
}
BASE_TRADE_FIELDS = [
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
S27_AUDIT_TRADE_FIELDS = [
    "timestamp_utc", "event", "strategy_id", "lane_id", "signal_type",
    "symbol", "mt5_symbol", "opportunity_id", "ticket", "position_identifier",
    "deal_id", "side", "lot", "price", "profit_bps", "profit_usd", "reason",
    "signal_bar_time", "entry_time_utc", "exit_time_utc", "context_reference_time",
    "context_stale_seconds", "live", "repeat_count", "repeat_window_seconds", "note",
]
TRADE_FIELDS = S27_AUDIT_TRADE_FIELDS if BOT_SUFFIX == "s27" else BASE_TRADE_FIELDS
_CSV_SCHEMAS_VALIDATED: set[str] = set()


def validate_csv_schema(path: str, fields: list[str]) -> None:
    if not os.path.exists(path) or os.path.getsize(path) == 0 or path in _CSV_SCHEMAS_VALIDATED:
        return
    with open(path, "r", newline="", encoding="utf-8") as existing_handle:
        observed_fields = next(csv.reader(existing_handle), [])
    if observed_fields != fields:
        raise RuntimeError(
            f"CSV schema mismatch for {path}; archive/reset the old trades CSV before starting {BOT_SUFFIX}"
        )
    _CSV_SCHEMAS_VALIDATED.add(path)


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


def append_csv(
    path: str,
    row: dict[str, Any],
    fields: list[str],
    *,
    validate_schema: bool = False,
) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    if validate_schema:
        validate_csv_schema(path, fields)
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
            if validate_schema:
                _CSV_SCHEMAS_VALIDATED.add(path)
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
        self.max_positions = int(self.strategy.get("max_positions", 1))
        self.lane_parameters = {
            int(row["lane_id"]): dict(row)
            for row in self.strategy.get("lane_parameters") or [
                {
                    "lane_id": lane_id,
                    "role": "baseline",
                    "threshold": float(self.strategy["threshold"]),
                    "ret25_floor_bps": 0.0,
                    "exit_floor_bps": float((self.strategy.get("exit_policy") or {}).get("floor_bps", 0.0)),
                    "exit_required_min": int(((self.strategy.get("exit_policy") or {}).get("inner_branch") or {}).get("required_min", 0)),
                }
                for lane_id in range(1, self.max_positions + 1)
            ]
        }
        self.live_enabled = bool(params.get("live_trading_enabled", False))
        self.shadow_enabled = bool(params.get("shadow_forward_enabled", True))
        self.durable_open_reservation_enabled = bool(params.get("durable_open_reservation_enabled", False))
        self.strict_trade_csv_schema = bool(params.get("strict_trade_csv_schema", False))
        self.safety = safety_options(params)
        self.dm = MT5DataManager(self.safety)
        self.executor = MT5Executor()
        self.state = self._load_state()
        self.last_status_log = 0.0
        self._diagnostic_repeats: dict[str, dict[str, Any]] = {}
        self.shadow_observer: Any = None
        self.shadow_state_tagger: Any = None
        self._shadow_error_signatures: dict[str, str] = {}
        try:
            if ShadowOpportunityObserver is not None:
                self.shadow_observer = ShadowOpportunityObserver(
                    params.get("shadow_opportunity_observer", {}),
                    log_dir=LOG_DIR,
                    state_dir=STATE_DIR,
                    symbol=str(params["mt5_symbol"]),
                    contract_size=float(params.get("contract_size", 1.0)),
                    lot=float(self.strategy.get("lot", 0.0)),
                )
        except Exception as exc:
            logging.error("%s passive shadow observer disabled: %s", BOT_SUFFIX.upper(), exc)
        try:
            if ShadowStateTagger is not None:
                self.shadow_state_tagger = ShadowStateTagger(
                    params.get("shadow_state_tagger", {}),
                    log_dir=LOG_DIR,
                    symbol=str(params["mt5_symbol"]),
                )
        except Exception as exc:
            logging.error("%s passive state tagger disabled: %s", BOT_SUFFIX.upper(), exc)

    def _now(self) -> pd.Timestamp:
        return utc_now()

    def _default_state(self) -> dict[str, Any]:
        return {
            "schema_version": 4,
            "bot_suffix": BOT_SUFFIX,
            "strategy_id": self.strategy["id"],
            "positions": [],
            "last_evaluated_bar": None,
            "last_signal_bar": None,
            "last_close_time_utc": None,
            "last_close_by_side": {"LONG": None, "SHORT": None},
            "sync_block_new_entries": False,
            "sync_block_reason": None,
            "sync_block_details": None,
            "sync_block_recoverable": False,
            "flat_clear_confirmation_count": 0,
            "flat_clear_confirmation_reason": None,
            "shadow_ticket_seq": -EXPECTED_MAGIC * 1000,
            "pending_open_action": None,
            "manual_alert_last_signature": None,
            "manual_alert_last_reason": None,
            "manual_alert_last_at_utc": None,
        }

    def _load_state(self) -> dict[str, Any]:
        baseline = self._default_state()
        if not os.path.exists(STATE_FILE):
            return baseline
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            compatible_ids = {str(value) for value in self.strategy.get("state_compatible_strategy_ids", [])}
            if loaded.get("bot_suffix") != BOT_SUFFIX or (
                loaded.get("strategy_id") != self.strategy["id"] and str(loaded.get("strategy_id")) not in compatible_ids
            ):
                raise ValueError("state identity mismatch")
            loaded_schema = int(loaded.get("schema_version") or 1)
            legacy_position = loaded.pop("position", None)
            if "positions" not in loaded:
                loaded["positions"] = [legacy_position] if legacy_position is not None else []
            if not isinstance(loaded.get("positions"), list):
                raise ValueError("state positions must be a list")
            if loaded_schema < 3:
                available_lanes = iter(range(1, self.max_positions + 1))
                for position in loaded["positions"]:
                    if position.get("lane_id") is None:
                        position["lane_id"] = next(available_lanes)
            baseline.update(loaded)
            baseline["schema_version"] = 4
            baseline["strategy_id"] = self.strategy["id"]
            baseline.setdefault("last_close_by_side", {"LONG": baseline.get("last_close_time_utc"), "SHORT": None})
            return baseline
        except Exception as exc:
            baseline["sync_block_new_entries"] = True
            baseline["sync_block_reason"] = "state_load_failed"
            baseline["sync_block_details"] = {"error": str(exc)}
            return baseline

    def _save_state(self) -> None:
        atomic_write_json(STATE_FILE, self.state)

    def _trade_row(self, event: str, **kwargs: Any) -> None:
        now = self._now()
        row = {
            "timestamp_utc": timestamp_text(now),
            "event": event,
            "strategy_id": self.strategy["id"],
            "symbol": self.params["symbol"],
            "mt5_symbol": self.params["mt5_symbol"],
            "live": self.live_enabled,
            **kwargs,
        }
        reason = str(row.get("reason") or "")
        coalesce = (
            bool(self.params.get("diagnostic_repeat_summary_seconds"))
            and event == "entry_skip"
            and reason in REPEATABLE_DIAGNOSTIC_REASONS
        )
        signature = f"{event}|{reason}|{str(row.get('note') or '')}"
        if not coalesce:
            self._flush_diagnostic_repeat(now)
            self._append_trade_row(row)
            return
        active = self._diagnostic_repeats.get("global")
        if active is None or active["signature"] != signature:
            self._flush_diagnostic_repeat(now)
            row["repeat_count"] = 1
            row["repeat_window_seconds"] = 0
            self._append_trade_row(row)
            self._diagnostic_repeats["global"] = {
                "signature": signature,
                "first": now,
                "last": now,
                "suppressed": 0,
                "row": dict(row),
            }
            return
        active["last"] = now
        active["suppressed"] = int(active.get("suppressed", 0)) + 1
        if (now - active["first"]).total_seconds() >= float(self.params.get("diagnostic_repeat_summary_seconds", 300.0)):
            self._flush_diagnostic_repeat(now, keep_signature=True)

    def _append_trade_row(self, row: dict[str, Any]) -> None:
        append_csv(
            TRADE_LOG_FILE,
            row,
            TRADE_FIELDS,
            validate_schema=self.strict_trade_csv_schema,
        )

    def _flush_diagnostic_repeat(self, now: pd.Timestamp | None = None, *, keep_signature: bool = False) -> None:
        active = self._diagnostic_repeats.get("global")
        if active is None:
            return
        at = now if now is not None else self._now()
        suppressed = int(active.get("suppressed", 0))
        if suppressed > 0:
            row = dict(active["row"])
            row.update(
                {
                    "timestamp_utc": timestamp_text(at),
                    "event": "diagnostic_repeat_summary",
                    "repeat_count": suppressed,
                    "repeat_window_seconds": round(max(0.0, (active["last"] - active["first"]).total_seconds()), 3),
                    "note": f"source_event=entry_skip;source_reason={row.get('reason', '')}",
                }
            )
            self._append_trade_row(row)
        if keep_signature:
            active["first"] = at
            active["last"] = at
            active["suppressed"] = 0
        else:
            self._diagnostic_repeats.pop("global", None)

    def _clear_pending_open(self) -> None:
        self.state["pending_open_action"] = None

    def _passive_call(self, component: str, method: str, **kwargs: Any) -> Any:
        target = self.shadow_observer if component == "observer" else self.shadow_state_tagger
        if target is None or not bool(getattr(target, "enabled", False)):
            return None
        try:
            result = getattr(target, method)(**kwargs)
            self._shadow_error_signatures.pop(component, None)
            return result
        except Exception as exc:
            signature = f"{method}:{type(exc).__name__}:{exc}"
            if self._shadow_error_signatures.get(component) != signature:
                logging.error(
                    "%s passive %s failure ignored by trading path: %s",
                    BOT_SUFFIX.upper(),
                    component,
                    signature,
                )
                self._shadow_error_signatures[component] = signature
            return None

    def _shadow_context(self, info: Any, bars: pd.DataFrame | None = None) -> dict[str, Any]:
        positions = list(self.state.get("positions") or [])
        occupied = {int(position.get("lane_id") or 0) for position in positions}
        long_positions = sum(str(position.get("side")) == "LONG" for position in positions)
        short_positions = sum(str(position.get("side")) == "SHORT" for position in positions)
        point = float(self.params.get("point_size", 0.0) or 0.0)
        context = {
            "spread_points": (float(info.ask) - float(info.bid)) / point if point > 0 else "",
            "portfolio_positions": len(positions),
            "long_positions": long_positions,
            "short_positions": short_positions,
            "lane_positions": {str(lane_id): int(lane_id in occupied) for lane_id in self.lane_parameters},
            "lane_pending": {str(lane_id): False for lane_id in self.lane_parameters},
            "lane_readiness": {
                str(lane_id): {
                    "ready": lane_id not in occupied,
                    "reason": "available" if lane_id not in occupied else "lane_position_occupied",
                    "consumed": False,
                }
                for lane_id in self.lane_parameters
            },
        }
        if bars is not None and not bars.empty:
            current = bars.iloc[-1]
            closes = bars["Close"].astype(float)
            context["ret10"] = float(closes.iloc[-1] - closes.iloc[-11]) if len(closes) > 10 else ""
            if "Volume" in bars and len(bars) >= 31:
                prior_volume = bars["Volume"].iloc[-31:-1].astype(float)
                mean_volume = float(prior_volume.mean())
                context["vol_ratio"] = float(current.get("Volume", 0.0)) / mean_volume if mean_volume > 0 else ""
            context["atr30"] = current.get("atr30", "")
        return context

    def _notify_manual_block(self, reason: str, details: dict[str, Any]) -> None:
        encoded = json.dumps({"reason": reason, "details": details}, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
        signature = hashlib.sha256(encoded).hexdigest()
        if self.state.get("manual_alert_last_signature") == signature:
            return
        self.state["manual_alert_last_signature"] = signature
        self.state["manual_alert_last_reason"] = reason
        self.state["manual_alert_last_at_utc"] = timestamp_text(self._now())
        notify_manual_action_required(
            bot_id=f"bot{self.params.get('bot_number', 27)}",
            symbol=str(self.params["mt5_symbol"]),
            title="reconciliation_required",
            reason=f"{reason}; details={json.dumps(details, ensure_ascii=True, sort_keys=True, default=str)}",
            action=f"Inspect {BOT_SUFFIX}-owned MT5 positions and state before clearing the entry block.",
            key=f"{BOT_SUFFIX}:reconciliation:{reason}",
        )

    def _set_sync_block(
        self,
        reason: str,
        details: dict[str, Any] | None = None,
        *,
        recoverable: bool = False,
    ) -> None:
        if recoverable and self.state.get("sync_block_new_entries") and not self.state.get("sync_block_recoverable"):
            logging.warning("%s retained non-recoverable block: %s", BOT_SUFFIX.upper(), self.state.get("sync_block_reason"))
            return
        self.state["sync_block_new_entries"] = True
        self.state["sync_block_reason"] = reason
        self.state["sync_block_details"] = details or {}
        self.state["sync_block_recoverable"] = bool(recoverable)
        self.state["flat_clear_confirmation_count"] = 0
        self.state["flat_clear_confirmation_reason"] = None
        self._trade_row("ERROR", reason=reason, note=json.dumps(details or {}, ensure_ascii=False, sort_keys=True))
        if not recoverable:
            self._notify_manual_block(reason, details or {})

    def _ownership_namespace_error(self) -> str | None:
        prefix = str(self.strategy.get("comment_prefix") or "")
        if self.magic != EXPECTED_MAGIC or int(self.params.get("expected_magic", 0)) != EXPECTED_MAGIC:
            return f"invalid_magic={self.magic} expected={EXPECTED_MAGIC}"
        if not prefix.startswith(f"{BOT_SUFFIX}_") or len(prefix) > 20:
            return f"invalid_comment_prefix={prefix}"
        if str(self.params.get("bot_suffix")) != BOT_SUFFIX:
            return f"invalid_bot_suffix={self.params.get('bot_suffix')}"
        if self.max_positions < 1 or self.max_positions > 8:
            return f"invalid_max_positions={self.max_positions} expected=1..8"
        if sorted(self.lane_parameters) != list(range(1, self.max_positions + 1)):
            return f"invalid_lane_ids={sorted(self.lane_parameters)} expected={list(range(1, self.max_positions + 1))}"
        for lane_id, lane in self.lane_parameters.items():
            signal_type = str(lane.get("signal_type", "long"))
            side = str(lane.get("side", "LONG"))
            exit_floor = float(lane.get("exit_floor_bps", float("nan")))
            exit_required = int(lane.get("exit_required_min", 0))
            hold_min = int(lane.get("hold_min", self.strategy.get("hold_min", 0)))
            lot = float(lane.get("lot", self.strategy.get("lot", float("nan"))))
            valid = side in {"LONG", "SHORT"} and signal_type in {"long", "activity", "vsa"}
            valid = valid and -1000.0 < exit_floor < 1000.0 and exit_required >= 1 and hold_min >= 1 and lot > 0.0
            if signal_type == "long":
                threshold = float(lane.get("threshold", float("nan")))
                ret_floor = float(lane.get("ret25_floor_bps", float("nan")))
                valid = valid and side == "LONG" and 0.0 < threshold < 2.0 and -1000.0 < ret_floor < 1000.0
            else:
                valid = valid and side == "SHORT"
            if not valid:
                return f"invalid_lane_parameters=lane{lane_id}"
        lane1 = self.lane_parameters[1]
        base_exit_policy = self.strategy.get("exit_policy") or {}
        if (
            float(lane1["threshold"]) != float(self.strategy["threshold"])
            or float(lane1["ret25_floor_bps"]) != 0.0
            or float(lane1["exit_floor_bps"]) != float(base_exit_policy.get("floor_bps", float("nan")))
            or int(lane1["exit_required_min"]) != int((base_exit_policy.get("inner_branch") or {}).get("required_min", 0))
        ):
            return "lane1_baseline_contract_drift"
        return None

    @staticmethod
    def _account_identity_error(account: dict[str, Any]) -> str | None:
        observed_login = account.get("login")
        observed_server = str(account.get("server") or "")
        if observed_login is None or not observed_server:
            return f"account_identity_unavailable; recompile and attach the current BotBridge_{BOT_SUFFIX}"
        if int(observed_login) != int(MT5_LOGIN) or observed_server.casefold() != str(MT5_SERVER).casefold():
            return (
                f"observed_login={int(observed_login)} observed_server={observed_server} "
                f"expected_login={int(MT5_LOGIN)} expected_server={MT5_SERVER}"
            )
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
        if self.strict_trade_csv_schema:
            try:
                validate_csv_schema(TRADE_LOG_FILE, TRADE_FIELDS)
            except RuntimeError as exc:
                logging.critical("%s trade CSV preflight failed: %s", BOT_SUFFIX.upper(), exc)
                return False
        namespace_error = self._ownership_namespace_error()
        if namespace_error:
            logging.critical("%s ownership namespace invalid: %s", BOT_SUFFIX.upper(), namespace_error)
            return False
        if not self.shadow_enabled and not self.live_enabled:
            logging.critical("%s neither shadow nor live mode is enabled", BOT_SUFFIX.upper())
            return False
        if not self.dm.connect():
            logging.critical("%s bridge connection failed", BOT_SUFFIX.upper())
            return False
        capabilities = self.executor.get_bridge_capabilities()
        if capabilities is None:
            logging.critical("%s bridge capability query failed", BOT_SUFFIX.upper())
            return False
        expected_bridge = str(self.params["expected_bridge_name"])
        if str(capabilities.get("name") or "") != expected_bridge:
            logging.critical("%s wrong bridge: got=%s expected=%s", BOT_SUFFIX.upper(), capabilities.get("name"), expected_bridge)
            return False
        missing = REQUIRED_SHARED_ACCOUNT_COMMANDS - {
            str(command).upper() for command in capabilities.get("commands", set())
        }
        if missing:
            logging.critical("%s bridge missing commands: %s", BOT_SUFFIX.upper(), sorted(missing))
            return False
        if self.live_enabled:
            account = self.executor.get_account_info()
            if account is None:
                logging.critical("%s account metadata unavailable", BOT_SUFFIX.upper())
                return False
            if bool(self.params.get("require_account_identity", False)):
                account_identity_error = self._account_identity_error(account)
                if account_identity_error is not None:
                    logging.critical("%s account identity mismatch: %s", BOT_SUFFIX.upper(), account_identity_error)
                    self._notify_manual_block("account_identity_mismatch", {"error": account_identity_error})
                    self._save_state()
                    return False
            if bool(self.params.get("require_hedging_account", True)) and int(account.get("margin_mode", -1)) != HEDGING_MARGIN_MODE:
                logging.critical("%s live mode requires hedging account", BOT_SUFFIX.upper())
                return False
            if bool(self.params.get("require_trade_permissions", False)) and not all(
                bool(account.get(field))
                for field in ("account_trade_allowed", "account_trade_expert", "terminal_trade_allowed", "mql_trade_allowed")
            ):
                logging.critical("%s live trading permissions are not all enabled", BOT_SUFFIX.upper())
                self._notify_manual_block(
                    "live_trade_permissions_disabled",
                    {
                        field: bool(account.get(field))
                        for field in ("account_trade_allowed", "account_trade_expert", "terminal_trade_allowed", "mql_trade_allowed")
                    },
                )
                self._save_state()
                return False
        return self._sync_positions()

    def _sync_positions(self) -> bool:
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
        state_positions = list(self.state.get("positions") or [])
        pending_open = self.state.get("pending_open_action")
        if self.durable_open_reservation_enabled and pending_open:
            self._set_sync_block(
                "unresolved_open_action",
                {
                    "reservation": dict(pending_open) if isinstance(pending_open, dict) else str(pending_open),
                    "live_tickets": [int(position.ticket) for position in positions],
                },
            )
            self._save_state()
            return False
        if len(state_positions) > self.max_positions or len(positions) > self.max_positions:
            self._set_sync_block(
                "position_limit_exceeded",
                {"state_count": len(state_positions), "live_count": len(positions), "max_positions": self.max_positions},
            )
            self._save_state()
            return False
        state_lane_ids = [int(position.get("lane_id") or 0) for position in state_positions]
        if (
            any(lane_id not in self.lane_parameters for lane_id in state_lane_ids)
            or len(state_lane_ids) != len(set(state_lane_ids))
        ):
            self._set_sync_block("state_lane_ownership_invalid", {"lane_ids": state_lane_ids})
            self._save_state()
            return False
        live_by_id: dict[int, Any] = {}
        for live_position in positions:
            identifier = int(getattr(live_position, "identifier", 0) or live_position.ticket)
            if identifier in live_by_id:
                self._set_sync_block("duplicate_live_position_identifier", {"position_identifier": identifier})
                self._save_state()
                return False
            live_by_id[identifier] = live_position
        retained: list[dict[str, Any]] = []
        matched_ids: set[int] = set()
        for state_position in state_positions:
            position_id = int(state_position.get("position_identifier") or state_position.get("ticket") or 0)
            live_position = live_by_id.get(position_id)
            if live_position is not None:
                expected_side = str(state_position.get("side"))
                actual_side = "LONG" if int(live_position.type) == ORDER_TYPE_BUY else "SHORT"
                if not self._owned_position(live_position) or expected_side != actual_side:
                    self._set_sync_block("state_position_ownership_mismatch", {"ticket": int(live_position.ticket)})
                    self._save_state()
                    return False
                retained.append(state_position)
                matched_ids.add(position_id)
                continue
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
            side = str(state_position.get("side", "LONG"))
            profit_bps = ((close_price / entry_price - 1.0) if side == "LONG" else (entry_price / close_price - 1.0)) * 10000.0
            self._trade_row(
                "position_close_deal",
                lane_id=state_position.get("lane_id"),
                signal_type=state_position.get("signal_type", ""),
                opportunity_id=state_position.get("opportunity_id", ""),
                ticket=position_id,
                position_identifier=position_id,
                deal_id=int(close_deal.deal),
                side=side,
                lot=state_position.get("lot"),
                price=close_price,
                profit_bps=profit_bps,
                profit_usd=float(close_deal.net_profit),
                reason=state_position.get("pending_close_reason") or "broker_or_external_close_confirmed",
                signal_bar_time=state_position.get("signal_bar_time"),
                entry_time_utc=state_position.get("entry_time_utc"),
                exit_time_utc=timestamp_text(
                    pd.Timestamp(int(close_deal.deal_time), unit="s", tz="UTC")
                    if int(getattr(close_deal, "deal_time", 0) or 0) > 0
                    else self._now()
                ),
                note=f"deal={int(close_deal.deal)} net_profit={float(close_deal.net_profit)}",
            )
            self.state["last_close_time_utc"] = timestamp_text(self._now())
            self.state.setdefault("last_close_by_side", {})[side] = timestamp_text(self._now())
        unmatched = [position for identifier, position in live_by_id.items() if identifier not in matched_ids]
        if unmatched:
            self._set_sync_block("live_position_without_state", {"tickets": [int(position.ticket) for position in unmatched]})
            self._save_state()
            return False
        self.state["positions"] = retained
        if orders_available and not orders:
            clear_recoverable_sync_block_after_clean_sync(
                symbol_key=self.strategy["id"],
                state=self.state,
                save_state=self._save_state,
                options=self.safety,
                audit=lambda _symbol, event, reason: self._trade_row(event, reason=reason),
            )
        self._save_state()
        return True

    def _sync_position(self) -> bool:
        """Compatibility alias for existing diagnostics and operator tooling."""
        return self._sync_positions()

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

    def _lane_exit_policy_config(self, lane_id: int) -> dict[str, Any] | None:
        configured = self.strategy.get("exit_policy")
        if not configured:
            return None
        policy = json.loads(json.dumps(configured))
        lane = self.lane_parameters[lane_id]
        policy["floor_bps"] = float(lane["exit_floor_bps"])
        required_min = int(lane["exit_required_min"])
        policy["inner_branch"]["required_min"] = required_min
        policy["outer_branch"]["required_min"] = required_min
        return policy

    def _open_entry(
        self,
        info: Any,
        signal: dict[str, Any],
        *,
        lane_id: int = 1,
        opportunity: dict[str, Any] | None = None,
    ) -> None:
        if lane_id not in self.lane_parameters:
            self._set_sync_block("invalid_entry_lane", {"lane_id": lane_id})
            self._save_state()
            return
        if len(self.state.get("positions") or []) >= self.max_positions:
            self._trade_row("entry_skip", reason="position_limit_reached", signal_bar_time=timestamp_text(signal.get("bar_time")))
            return
        if any(int(position.get("lane_id") or 0) == lane_id for position in self.state.get("positions") or []):
            self._trade_row("entry_skip", reason="lane_position_occupied", signal_bar_time=timestamp_text(signal.get("bar_time")), note=f"lane_id={lane_id}")
            return
        signal_bar = utc_timestamp(signal["bar_time"])
        if signal_bar is None:
            self._set_sync_block("signal_bar_time_invalid", recoverable=True)
            self._save_state()
            return
        entry_due = signal_bar + pd.Timedelta(minutes=1)
        if self.strategy.get("exit_policy") and not int(getattr(info, "tick_time_msc", 0) or 0):
            self._set_sync_block("tick_time_unavailable_for_c4566_entry", recoverable=True)
            self._save_state()
            return
        lane = self.lane_parameters[lane_id]
        side = str(lane.get("side", signal.get("side", "LONG")))
        order_type = ORDER_TYPE_BUY if side == "LONG" else ORDER_TYPE_SELL
        lot = float(lane.get("lot", self.strategy["lot"]))
        entry_price = float(info.ask if side == "LONG" else info.bid)
        entry_event_time: pd.Timestamp | None = None
        ticket: int | None = None
        confirmed = None
        if self.live_enabled:
            before = self.executor.get_positions(str(self.params["mt5_symbol"]), self.magic)
            if before is None:
                self._set_sync_block("positions_unavailable_before_open", recoverable=True)
                self._save_state()
                return
            if len(before) >= self.max_positions:
                self._trade_row("entry_skip", reason="position_limit_reached", signal_bar_time=timestamp_text(signal_bar))
                return
            known_ids = {int(getattr(position, "identifier", 0) or position.ticket) for position in before}
            if self.durable_open_reservation_enabled:
                reservation = {
                    "reservation_id": (
                        f"{self.params['mt5_symbol']}|{timestamp_text(signal_bar)}|"
                        f"{str(signal.get('signal_type', 'long'))}|lane{lane_id}|{side}"
                    ),
                    "reserved_at_utc": timestamp_text(self._now()),
                    "signal_bar_time": timestamp_text(signal_bar),
                    "signal_type": str(signal.get("signal_type", "long")),
                    "lane_id": lane_id,
                    "side": side,
                    "lot": lot,
                    "known_position_ids": sorted(known_ids),
                }
                self.state["pending_open_action"] = reservation
                self._save_state()
                try:
                    self._trade_row(
                        "open_reserved",
                        lane_id=lane_id,
                        signal_type=str(signal.get("signal_type", "long")),
                        opportunity_id=str((opportunity or {}).get("opportunity_id") or reservation["reservation_id"]),
                        side=side,
                        lot=lot,
                        reason="durable_open_reservation",
                        signal_bar_time=timestamp_text(signal_bar),
                        note=json.dumps(reservation, ensure_ascii=False, sort_keys=True),
                    )
                except Exception:
                    # No order has been submitted yet, so this reservation can
                    # be safely removed if the audit row itself cannot be written.
                    self._clear_pending_open()
                    self._save_state()
                    raise
            ticket = self.executor.open_position(
                str(self.params["mt5_symbol"]),
                order_type,
                lot,
                0.0,
                0.0,
                deviation=int(self.params.get("deviation_points", 50)),
                magic=self.magic,
                comment=f"{self.strategy['comment_prefix']}_l{lane_id}",
                digits=int(self.params["price_digits"]),
            )
            error = str(getattr(self.executor, "last_order_error", None) or "")
            after = self.executor.get_positions(str(self.params["mt5_symbol"]), self.magic)
            if after is None:
                self._set_sync_block("positions_unavailable_after_open", {"ticket": int(ticket or 0), "error": error}, recoverable=True)
                self._save_state()
                return
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
                return
            if int(getattr(confirmed, "type", -1)) != order_type:
                self._set_sync_block("open_confirmed_side_mismatch", {"ticket": int(confirmed.ticket), "expected_side": side})
                self._save_state()
                return
            entry_price = float(confirmed.open_price)
            confirmed_open_time = int(getattr(confirmed, "open_time", 0) or 0)
            if confirmed_open_time > 0:
                entry_event_time = pd.Timestamp(confirmed_open_time, unit="s", tz="UTC")
            else:
                entry_event_time = self._now()
        else:
            ticket = self._next_shadow_ticket()
            entry_event_time = self._now()
        if entry_event_time is None:
            raise RuntimeError("entry event time was not established")
        hold_clock = str(self.strategy.get("hold_clock", "actual_entry_time"))
        if hold_clock == "decision_time":
            close_base = entry_due
        elif hold_clock == "actual_entry_time":
            close_base = entry_event_time
        else:
            raise ValueError(f"unsupported hold_clock={hold_clock}")
        close_due = close_base + pd.Timedelta(minutes=int(lane.get("hold_min", self.strategy["hold_min"])))
        exit_policy_config = self._lane_exit_policy_config(lane_id)
        exit_policy_state = None
        if exit_policy_config:
            from c4566_exit_policy import build_policy_state

            entry_vol30_bps = float(signal["vol30_bps"])
            exit_policy_state = build_policy_state(entry_event_time.to_pydatetime(), entry_vol30_bps, exit_policy_config)
        position = {
            "lane_id": lane_id,
            "ticket": ticket,
            "position_identifier": int(getattr(confirmed, "identifier", 0) or ticket or 0),
            "side": side,
            "lot": lot,
            "entry_price": entry_price,
            "entry_due_utc": timestamp_text(entry_due),
            "entry_time_utc": timestamp_text(entry_event_time),
            "exit_due_utc": timestamp_text(close_due),
            "signal_bar_time": timestamp_text(signal_bar),
            "signal_type": str(signal.get("signal_type", "long")),
            "opportunity_id": str((opportunity or {}).get("opportunity_id") or ""),
            "open_time_epoch": int(getattr(confirmed, "open_time", 0) or 0),
            "owner_symbol": str(self.params["mt5_symbol"]),
            "owner_magic": self.magic,
            "owner_comment": str(getattr(confirmed, "comment", "") or self.strategy["comment_prefix"]),
            "shadow": not self.live_enabled,
            "pending_close_reason": None,
            "close_requested": False,
            "exit_policy_state": exit_policy_state,
        }
        if self.live_enabled and self.durable_open_reservation_enabled:
            self._clear_pending_open()
        self.state.setdefault("positions", []).append(position)
        self.state["last_signal_bar"] = timestamp_text(signal_bar)
        self._trade_row(
            "entry",
            lane_id=lane_id,
            signal_type=str(signal.get("signal_type", "long")),
            opportunity_id=str((opportunity or {}).get("opportunity_id") or ""),
            ticket=ticket,
            position_identifier=position["position_identifier"],
            side=side,
            lot=lot,
            price=entry_price,
            reason="protocol_v2_signal",
            signal_bar_time=timestamp_text(signal_bar),
            entry_time_utc=timestamp_text(entry_event_time),
            context_reference_time=timestamp_text(signal.get("context_reference_time")),
            context_stale_seconds=signal.get("context_stale_seconds", ""),
            note=json.dumps(
                {
                    **{
                        key: value
                        for key, value in signal.items()
                        if key not in {"bar_time", "context_reference_time", "context_reference_bar_start"}
                    },
                    "lane_id": lane_id,
                    "lane_role": self.lane_parameters[lane_id].get("role", ""),
                    "lane_signal_type": lane.get("signal_type", "long"),
                    "lane_threshold": lane.get("threshold", ""),
                    "lane_ret25_floor_bps": lane.get("ret25_floor_bps", ""),
                    "lane_exit_floor_bps": float(self.lane_parameters[lane_id]["exit_floor_bps"]),
                    "lane_exit_required_min": int(self.lane_parameters[lane_id]["exit_required_min"]),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        self._save_state()

    def _time_close_spread_action(
        self,
        position: dict[str, Any],
        info: Any,
        exit_due: pd.Timestamp,
        *,
        require_due: bool = True,
    ) -> str:
        if not bool(self.params.get("time_close_spread_guard_enabled", True)):
            return "close"
        tick_time_msc = int(getattr(info, "tick_time_msc", 0) or 0)
        quote_time = pd.Timestamp(tick_time_msc, unit="ms", tz="UTC") if tick_time_msc > 0 else None
        last_quote_time = utc_timestamp(position.get("time_close_last_quote_utc"))
        if quote_time is None or (require_due and quote_time < exit_due) or (last_quote_time is not None and quote_time <= last_quote_time):
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

    def _handle_time_exit(self, info: Any, position: dict[str, Any] | None = None) -> bool:
        if position is None:
            positions = list(self.state.get("positions") or [])
            position = positions[0] if positions else None
        if position is None:
            return False
        if bool(position.get("close_requested")):
            return True
        exit_due = utc_timestamp(position.get("exit_due_utc"))
        if exit_due is None:
            self._set_sync_block("exit_due_invalid", {"ticket": int(position.get("ticket") or 0)})
            self._save_state()
            return True
        wall_now = self._now()
        observation_now = None
        tick_time_msc = int(getattr(info, "tick_time_msc", 0) or 0)
        if tick_time_msc > 0:
            observation_now = pd.Timestamp(tick_time_msc, unit="ms", tz="UTC")
        policy_reason = None
        exit_policy_config = self.strategy.get("exit_policy")
        policy_state = position.get("exit_policy_state") if exit_policy_config else None
        side = str(position.get("side", "LONG"))
        close_price = float(info.bid if side == "LONG" else info.ask)
        entry_price = float(position["entry_price"])
        profit_bps = ((close_price / entry_price - 1.0) if side == "LONG" else (entry_price / close_price - 1.0)) * 10000.0
        if policy_state is not None and observation_now is not None:
            from c4566_exit_policy import evaluate_policy

            decision = evaluate_policy(
                policy_state,
                now=observation_now.to_pydatetime(),
                current_profit_bps=profit_bps,
                max_observation_gap_seconds=float(exit_policy_config["max_observation_gap_seconds"]),
            )
            position["exit_policy_state"] = decision.policy_state
            policy_reason = decision.reason
            self._save_state()
        exit_clock_now = observation_now if policy_state is not None and observation_now is not None else wall_now
        if policy_reason is None and exit_clock_now < exit_due:
            return False
        retry_after = utc_timestamp(position.get("time_close_retry_after_utc"))
        if retry_after is not None and wall_now < retry_after:
            return True
        early_policy_exit = policy_reason is not None and exit_clock_now < exit_due
        if self._time_close_spread_action(position, info, exit_due, require_due=not early_policy_exit) == "defer":
            self._save_state()
            return True
        ticket = int(position.get("ticket") or 0)
        hold_reason = policy_reason or f"fixed_hold_{int(self.strategy['hold_min'])}m"
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
                        side=side,
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
                lane_id=position.get("lane_id"),
                signal_type=position.get("signal_type", ""),
                opportunity_id=position.get("opportunity_id", ""),
                ticket=ticket,
                position_identifier=position.get("position_identifier"),
                side=side,
                lot=position.get("lot"),
                price=close_price,
                profit_bps=profit_bps,
                reason=hold_reason,
                signal_bar_time=position.get("signal_bar_time"),
                entry_time_utc=position.get("entry_time_utc"),
            )
            self._save_state()
            return True
        self._trade_row(
            "position_close",
            lane_id=position.get("lane_id"),
            signal_type=position.get("signal_type", ""),
            opportunity_id=position.get("opportunity_id", ""),
            ticket=ticket,
            position_identifier=position.get("position_identifier"),
            side=side,
            lot=position.get("lot"),
            price=close_price,
            profit_bps=profit_bps,
            reason=hold_reason,
            signal_bar_time=position.get("signal_bar_time"),
            entry_time_utc=position.get("entry_time_utc"),
            exit_time_utc=timestamp_text(self._now()),
        )
        position_id = int(position.get("position_identifier") or ticket)
        self.state["positions"] = [
            row
            for row in self.state.get("positions") or []
            if int(row.get("position_identifier") or row.get("ticket") or 0) != position_id
        ]
        self.state["last_close_time_utc"] = timestamp_text(self._now())
        self.state.setdefault("last_close_by_side", {})[side] = timestamp_text(self._now())
        self._save_state()
        return True

    def _handle_time_exits(self, info: Any) -> bool:
        positions = sorted(
            list(self.state.get("positions") or []),
            key=lambda row: timestamp_text(row.get("exit_due_utc")),
        )
        for position in positions:
            if self._handle_time_exit(info, position):
                return True
        return False

    def _lane_signal_eligible(self, lane_id: int, signal: dict[str, Any], info: Any | None = None) -> bool:
        lane = self.lane_parameters[lane_id]
        signal_type = str(signal.get("signal_type", "long"))
        if str(lane.get("signal_type", "long")) != signal_type or not bool(signal.get("eligible")):
            return False
        if signal_type == "long":
            try:
                ret25_bps = float(signal["ret25"]) * 10000.0
                feature = float(signal["absret_std_ratio30_120"])
            except (KeyError, TypeError, ValueError):
                return False
            if not (
                math.isfinite(ret25_bps)
                and math.isfinite(feature)
                and ret25_bps > float(lane["ret25_floor_bps"])
                and feature <= float(lane["threshold"])
            ):
                return False
            spread_cap = lane.get("entry_spread_bps_max")
            if spread_cap is not None:
                if info is None or float(info.bid) <= 0.0:
                    return False
                spread_bps = (float(info.ask) / float(info.bid) - 1.0) * 10000.0
                if spread_bps > float(spread_cap):
                    return False
        return True

    def _route_signal_lane(self, signal: dict[str, Any], info: Any | None = None) -> tuple[int | None, list[int]]:
        occupied = {int(position.get("lane_id") or 0) for position in self.state.get("positions") or []}
        eligible = [lane_id for lane_id in sorted(self.lane_parameters) if self._lane_signal_eligible(lane_id, signal, info)]
        for lane_id in eligible:
            if lane_id not in occupied:
                return lane_id, eligible
        return None, eligible

    def run_once(self) -> None:
        info = self.executor.get_symbol_info(str(self.params["mt5_symbol"]))
        if info is None:
            self._set_sync_block("symbol_info_failed", recoverable=True)
            self._save_state()
            return
        self._passive_call("observer", "observe_quote", at=self._now(), bid=float(info.bid), ask=float(info.ask))
        if not self._sync_positions():
            return
        if self._handle_time_exits(info):
            return
        if self.state.get("sync_block_new_entries") or len(self.state.get("positions") or []) >= self.max_positions:
            return
        mt5_symbol = str(self.params["mt5_symbol"])
        for lane_id, lane in self.lane_parameters.items():
            lot_error = lot_contract_error(
                float(lane.get("lot", self.strategy["lot"])),
                float(info.volume_min), float(info.volume_max), float(info.volume_step),
                float(MIN_LOT_OVERRIDES.get(mt5_symbol, 0.0)),
            )
            if lot_error:
                self._set_sync_block("invalid_lot_contract", {"symbol": mt5_symbol, "lane_id": lane_id, "error": lot_error}, recoverable=False)
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
        long_signal = latest_signal(target_bars, context_bars, self.strategy)
        long_signal["signal_type"] = "long"
        signals = [long_signal]
        if self.strategy.get("short_overlays"):
            if short_overlay_signals is None:
                self._set_sync_block("short_overlay_strategy_unavailable", recoverable=False)
                self._save_state()
                return
            overlays = short_overlay_signals(target_bars, self.strategy)
            signals.extend([overlays["activity"], overlays["vsa"]])
        signal_bar = utc_timestamp(long_signal.get("bar_time"))
        if signal_bar is None:
            self._set_sync_block("signal_bar_time_invalid", recoverable=True)
            self._save_state()
            return
        signal_bar_text = timestamp_text(signal_bar)
        if self.state.get("last_evaluated_bar") == signal_bar_text:
            return
        self.state["last_evaluated_bar"] = signal_bar_text
        decision_time = self._now()
        shadow_context = self._shadow_context(info, target_bars)
        opportunities: dict[str, dict[str, Any]] = {}
        for signal in signals:
            if not bool(signal.get("eligible")):
                continue
            signal_type = str(signal.get("signal_type", "long"))
            signal_side = str(signal.get("side") or ("LONG" if signal_type == "long" else "SHORT")).upper()
            signal_lanes = [
                lane for lane in self.lane_parameters.values()
                if str(lane.get("signal_type", "long")) == signal_type
            ]
            signal_lot = float(signal_lanes[0].get("lot", self.strategy["lot"])) if signal_lanes else float(self.strategy["lot"])
            opportunity = {
                "opportunity_id": f"{mt5_symbol}|{signal_bar_text}|{signal_type}|{signal_side}",
                "side": signal_side,
                "raw_side": signal_side,
                "effective_side": signal_side,
                "lot": signal_lot,
                "entry_policy": {
                    "policy_id": "s27_signal_lane_policy_v1",
                    "action": "unchanged",
                    "reason": "raw_signal_confirmed",
                },
                "event_time": signal_bar_text,
                "release_time": timestamp_text(signal_bar + pd.Timedelta(minutes=1)),
                "decision_time": timestamp_text(decision_time),
            }
            opportunities[signal_type] = opportunity
            self._passive_call(
                "observer",
                "register_opportunity",
                opportunity=opportunity,
                at=decision_time,
                bid=float(info.bid),
                ask=float(info.ask),
                context=shadow_context,
            )
            self._passive_call(
                "tagger",
                "tag_opportunity",
                opportunity=opportunity,
                at=decision_time,
                bars=target_bars,
                bid=float(info.bid),
                ask=float(info.ask),
                context=shadow_context,
            )
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
            for opportunity in opportunities.values():
                self._passive_call(
                    "observer",
                    "record_route",
                    opportunity_id=opportunity["opportunity_id"],
                    at=decision_time,
                    status="stale_rejected",
                    consumed_lane_id=None,
                    reason="stale_signal_skip",
                )
            self._save_state()
            return
        for signal in signals:
            signal_type = str(signal.get("signal_type", "long"))
            opportunity = opportunities.get(signal_type)
            lane_id, eligible_lanes = self._route_signal_lane(signal, info)
            if lane_id is None:
                reason = "eligible_lanes_occupied" if eligible_lanes else "no_signal"
                self._trade_row(
                    "decision_receipt",
                    signal_type=signal_type,
                    opportunity_id=str((opportunity or {}).get("opportunity_id") or ""),
                    reason=reason,
                    signal_bar_time=signal_bar_text,
                    note=f"signal_type={signal_type} eligible_lanes={eligible_lanes}",
                )
                if opportunity is not None:
                    self._passive_call(
                        "observer",
                        "record_route",
                        opportunity_id=opportunity["opportunity_id"],
                        at=decision_time,
                        status="unconsumed",
                        consumed_lane_id=None,
                        reason=reason,
                    )
                continue
            side = str(self.lane_parameters[lane_id].get("side", signal.get("side", "LONG")))
            last_close = utc_timestamp((self.state.get("last_close_by_side") or {}).get(side))
            entry_due = signal_bar + pd.Timedelta(minutes=1)
            if last_close is not None and entry_due <= last_close:
                self._trade_row("entry_skip", lane_id=lane_id, signal_type=signal_type, opportunity_id=str((opportunity or {}).get("opportunity_id") or ""), reason="same_direction_reentry_after_close_skip", signal_bar_time=signal_bar_text, note=f"side={side}")
                if opportunity is not None:
                    self._passive_call("observer", "record_route", opportunity_id=opportunity["opportunity_id"], at=decision_time, status="unconsumed", consumed_lane_id=None, reason="same_direction_reentry_after_close_skip")
                continue
            self._trade_row("decision_receipt", lane_id=lane_id, signal_type=signal_type, opportunity_id=str((opportunity or {}).get("opportunity_id") or ""), reason="signal", signal_bar_time=signal_bar_text, note=f"signal_type={signal_type} lane_id={lane_id}")
            self._open_entry(info, signal, lane_id=lane_id, opportunity=opportunity)
            if opportunity is not None:
                entered = any(
                    str(position.get("opportunity_id") or "") == opportunity["opportunity_id"]
                    for position in self.state.get("positions") or []
                )
                route_reason = "entry_confirmed" if entered else str(self.state.get("sync_block_reason") or "entry_not_confirmed")
                self._passive_call(
                    "observer",
                    "record_route",
                    opportunity_id=opportunity["opportunity_id"],
                    at=decision_time,
                    status="consumed" if entered else "execution_uncertain",
                    consumed_lane_id=lane_id if entered else None,
                    reason=route_reason,
                )
        self._save_state()

    def log_status(self) -> None:
        now = time.time()
        if now - self.last_status_log < float(self.params.get("status_log_interval_seconds", 60)):
            return
        logging.info(
            "%s status live=%s shadow=%s active=%s positions=%s/%s lanes=%s blocked=%s reason=%s",
            BOT_SUFFIX.upper(),
            self.live_enabled,
            self.shadow_enabled,
            bool(self.state.get("positions")),
            len(self.state.get("positions") or []),
            self.max_positions,
            sorted(int(position.get("lane_id") or 0) for position in self.state.get("positions") or []),
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
        self.tick_time_msc = 1787306465000
        self.on_open: Any = None

    def get_symbol_info(self, _symbol: str) -> Any:
        return SimpleNamespace(
            bid=28646.45,
            ask=28648.97,
            tick_time_msc=self.tick_time_msc,
            volume_min=0.05,
            volume_max=500.0,
            volume_step=0.01,
        )

    def get_bridge_capabilities(self) -> dict[str, Any]:
        return {"name": f"BotBridge_{BOT_SUFFIX}", "commands": set(REQUIRED_SHARED_ACCOUNT_COMMANDS)}

    def get_account_info(self) -> dict[str, Any]:
        return {
            "margin_mode": HEDGING_MARGIN_MODE,
            "margin_mode_name": "RETAIL_HEDGING",
            "account_trade_allowed": True,
            "account_trade_expert": True,
            "terminal_trade_allowed": True,
            "mql_trade_allowed": True,
            "login": MT5_LOGIN,
            "server": MT5_SERVER,
        }

    def get_positions(self, _symbol: str, _magic: int) -> list[Any]:
        return list(self.positions)

    def get_orders(self, _symbol: str, _magic: int) -> list[Any] | None:
        return list(self.orders) if self.orders_available else None

    def confirm_position_absent(self, _ticket: int) -> bool:
        return True

    def get_position_close_deal(self, position_id: int, opened_at_epoch: int) -> Any:
        self.close_deal_calls.append((position_id, opened_at_epoch))
        return self.close_deal_results.pop(0)

    def open_position(
        self,
        symbol: str,
        order_type: int,
        lot: float,
        _sl: float,
        _tp: float,
        **kwargs: Any,
    ) -> int:
        if self.on_open is not None:
            self.on_open()
        ticket = 910000 + len(self.positions)
        self.positions.append(
            SimpleNamespace(
                symbol=symbol,
                magic=int(kwargs["magic"]),
                comment=str(kwargs["comment"]),
                ticket=ticket,
                identifier=ticket,
                type=order_type,
                volume=lot,
                open_price=28648.97 if order_type == ORDER_TYPE_BUY else 28646.45,
                open_time=1787306465,
            )
        )
        return ticket


def load_params(path: str = PARAMS_FILE) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def self_test() -> None:
    params = load_params()
    assert params["live_trading_enabled"] is True
    assert params["shadow_forward_enabled"] is False
    if BOT_SUFFIX == "s27":
        assert params["durable_open_reservation_enabled"] is True
        assert params["strict_trade_csv_schema"] is True
        assert params["require_account_identity"] is True
        assert params["require_trade_permissions"] is True
        configured_account_identity = int(MT5_LOGIN) > 0 and bool(MT5_SERVER)
        if configured_account_identity:
            matching_account = {"login": MT5_LOGIN, "server": MT5_SERVER}
            assert ProtocolV2FixedHoldRunner._account_identity_error(matching_account) is None
            assert ProtocolV2FixedHoldRunner._account_identity_error({"login": int(MT5_LOGIN) + 1, "server": MT5_SERVER}) is not None
        assert ProtocolV2FixedHoldRunner._account_identity_error({"login": None, "server": ""}) is not None
        import live_executor as executor_module

        test_account_login = int(MT5_LOGIN) if configured_account_identity else 123456789
        test_account_server = str(MT5_SERVER) if configured_account_identity else "Test-MT5"
        original_account_send = executor_module.ea_bridge.send_command
        executor_module.ea_bridge.send_command = lambda *_args, **_kwargs: (
            f"OK|2|RETAIL_HEDGING|1|1|1|1|{test_account_login}|{test_account_server}"
        )
        try:
            parsed_account = executor_module.MT5Executor().get_account_info()
        finally:
            executor_module.ea_bridge.send_command = original_account_send
        assert parsed_account is not None
        assert parsed_account["login"] == test_account_login
        assert parsed_account["server"] == test_account_server
        with tempfile.TemporaryDirectory() as temporary_dir:
            mismatched = os.path.join(temporary_dir, "trades.csv")
            with open(mismatched, "w", newline="", encoding="utf-8") as handle:
                csv.writer(handle).writerow(["old", "schema"])
            try:
                append_csv(mismatched, {}, TRADE_FIELDS, validate_schema=True)
            except RuntimeError as exc:
                assert "CSV schema mismatch" in str(exc)
            else:
                raise AssertionError("strict trade CSV schema must reject an incompatible header")
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
    runner._now = lambda: pd.Timestamp("2026-08-21 10:01:05", tz="UTC")
    runner._open_entry(
        runner.executor.get_symbol_info("USTEC"),
        {"bar_time": signal_bar, "side": "LONG", "eligible": True, "vol30_bps": 2.0},
    )
    position = runner.state["positions"][0]
    shadow_position = json.loads(json.dumps(position))
    runner._open_entry(
        runner.executor.get_symbol_info("USTEC"),
        {"bar_time": signal_bar + pd.Timedelta(minutes=1), "side": "LONG", "eligible": True, "vol30_bps": 2.0},
        lane_id=2,
    )
    assert len(runner.state["positions"]) == 2
    second_shadow_position = json.loads(json.dumps(runner.state["positions"][1]))
    assert float(position["exit_policy_state"]["floor_bps"]) == 4.0
    assert float(second_shadow_position["exit_policy_state"]["floor_bps"]) == 2.0
    assert int(position["exit_policy_state"]["required_seconds"]) == 1200
    assert int(second_shadow_position["exit_policy_state"]["required_seconds"]) == 1200

    if BOT_SUFFIX == "s27":
        diagnostic_runner = ProtocolV2FixedHoldRunner(test_params)
        diagnostic_rows: list[dict[str, Any]] = []
        diagnostic_runner._append_trade_row = lambda row: diagnostic_rows.append(dict(row))
        diagnostic_runner._now = lambda: pd.Timestamp("2026-08-21 10:01:05", tz="UTC")
        diagnostic_runner._trade_row("entry_skip", reason="target_m1_unavailable")
        diagnostic_runner._trade_row("entry_skip", reason="target_m1_unavailable")
        diagnostic_runner._trade_row("entry_skip", reason="target_m1_unavailable")
        diagnostic_runner._trade_row("decision_receipt", reason="no_signal")
        assert [row["event"] for row in diagnostic_rows] == ["entry_skip", "diagnostic_repeat_summary", "decision_receipt"]
        assert diagnostic_rows[1]["repeat_count"] == 2

        live_runner = ProtocolV2FixedHoldRunner(params)
        live_runner.state = live_runner._default_state()
        live_runner.executor = FakeExecutor()
        live_events: list[tuple[str, dict[str, Any]]] = []
        saved_states: list[dict[str, Any]] = []
        live_runner._trade_row = lambda event, **kwargs: live_events.append((event, kwargs))
        live_runner._save_state = lambda: saved_states.append(json.loads(json.dumps(live_runner.state)))
        live_runner._now = lambda: pd.Timestamp("2026-08-21 10:01:05", tz="UTC")
        live_runner.executor.on_open = lambda: (
            live_runner.state.get("pending_open_action")
            or (_ for _ in ()).throw(AssertionError("order submitted without durable reservation"))
        )
        live_runner._open_entry(
            live_runner.executor.get_symbol_info("USTEC"),
            {
                "bar_time": signal_bar,
                "side": "LONG",
                "signal_type": "long",
                "eligible": True,
                "vol30_bps": 2.0,
            },
        )
        assert any(snapshot.get("pending_open_action") for snapshot in saved_states)
        assert live_runner.state["pending_open_action"] is None
        assert len(live_runner.state["positions"]) == 1
        assert [event for event, _kwargs in live_events][:2] == ["open_reserved", "entry"]
        live_runner.state = live_runner._default_state()
        live_runner.state["pending_open_action"] = {"reservation_id": "restart-test"}
        live_runner.executor.positions = []
        assert not live_runner._sync_position()
        assert live_runner.state["sync_block_reason"] == "unresolved_open_action"
        if configured_account_identity:
            preflight_runner = ProtocolV2FixedHoldRunner(params)
            preflight_runner.state = preflight_runner._default_state()
            preflight_runner.dm = SimpleNamespace(connect=lambda: True)
            preflight_runner.executor = FakeExecutor()
            preflight_runner._trade_row = lambda *_args, **_kwargs: None
            preflight_runner._save_state = lambda: None
            assert preflight_runner.connect_and_preflight()
            wrong_account_executor = FakeExecutor()
            wrong_account = wrong_account_executor.get_account_info()
            wrong_account_executor.get_account_info = lambda: {
                **wrong_account,
                "login": int(MT5_LOGIN) + 1,
            }
            preflight_runner.executor = wrong_account_executor
            assert not preflight_runner.connect_and_preflight()
    from c4566_exit_policy import evaluate_policy

    lane1_policy = json.loads(json.dumps(position["exit_policy_state"]))
    lane2_policy = json.loads(json.dumps(second_shadow_position["exit_policy_state"]))
    lane1_reason = None
    lane2_reason = None
    for seconds in range(10, 1230, 10):
        observed_at = pd.Timestamp(position["entry_time_utc"]) + pd.Timedelta(seconds=seconds)
        lane1_decision = evaluate_policy(lane1_policy, now=observed_at.to_pydatetime(), current_profit_bps=3.0, max_observation_gap_seconds=15.0)
        lane2_decision = evaluate_policy(lane2_policy, now=observed_at.to_pydatetime(), current_profit_bps=3.0, max_observation_gap_seconds=15.0)
        lane1_policy = lane1_decision.policy_state
        lane2_policy = lane2_decision.policy_state
        lane1_reason = lane1_reason or lane1_decision.reason
        lane2_reason = lane2_reason or lane2_decision.reason
    assert lane1_reason is None
    assert lane2_reason == "c4560_continuous_positive_time"
    runner._open_entry(
        runner.executor.get_symbol_info("USTEC"),
        {"bar_time": signal_bar + pd.Timedelta(minutes=2), "side": "LONG", "eligible": True, "vol30_bps": 2.0},
    )
    assert len(runner.state["positions"]) == 2
    assert any(event == "entry_skip" and kwargs.get("reason") == "lane_position_occupied" for event, kwargs in events)
    runner.state["positions"] = [position]
    baseline_signal = {"signal_type": "long", "eligible": True, "ret25": 0.001, "absret_std_ratio30_120": 0.66}
    ineligible_signal = {"signal_type": "long", "eligible": True, "ret25": 0.001, "absret_std_ratio30_120": 0.68}
    runner.state["positions"] = []
    assert runner._route_signal_lane(baseline_signal, runner.executor.get_symbol_info("USTEC"))[0] == 1
    assert runner._route_signal_lane(ineligible_signal, runner.executor.get_symbol_info("USTEC"))[0] is None
    runner.state["positions"] = [position]
    assert runner._route_signal_lane(baseline_signal, runner.executor.get_symbol_info("USTEC"))[0] == 2
    runner.state["positions"] = []
    short_signal = {"signal_type": "activity", "side": "SHORT", "eligible": True}
    assert runner._route_signal_lane(short_signal, runner.executor.get_symbol_info("USTEC"))[0] == 4
    runner._open_entry(
        runner.executor.get_symbol_info("USTEC"),
        {"bar_time": signal_bar + pd.Timedelta(minutes=3), "signal_type": "activity", "side": "SHORT", "eligible": True, "vol30_bps": 2.0},
        lane_id=4,
    )
    short_position = runner.state["positions"][0]
    assert short_position["side"] == "SHORT" and float(short_position["lot"]) == 0.15
    assert pd.Timestamp(short_position["exit_due_utc"]) == pd.Timestamp(short_position["entry_time_utc"]) + pd.Timedelta(minutes=60)
    assert float(short_position["exit_policy_state"]["floor_bps"]) == 4.0
    assert int(short_position["exit_policy_state"]["required_seconds"]) == 600
    short_due = pd.Timestamp(short_position["exit_due_utc"])
    runner._now = lambda: short_due + pd.Timedelta(seconds=1)
    runner.executor.tick_time_msc = int((short_due + pd.Timedelta(seconds=1)).timestamp() * 1000)
    assert runner._handle_time_exit(runner.executor.get_symbol_info("USTEC"), short_position)
    assert runner.state["positions"] == []
    assert any(event == "position_close" and kwargs.get("side") == "SHORT" for event, kwargs in events)
    runner._now = lambda: pd.Timestamp("2026-08-21 10:01:05", tz="UTC")
    runner.state["positions"] = [position]
    has_exit_policy = bool(test_params["strategy"].get("exit_policy"))
    assert position["entry_time_utc"] == "2026-08-21T10:01:05+00:00"
    close_base = signal_bar + pd.Timedelta(minutes=1) if runner.strategy.get("hold_clock") == "decision_time" else pd.Timestamp(position["entry_time_utc"])
    expected_exit_due = close_base + pd.Timedelta(minutes=int(runner.strategy["hold_min"]))
    assert position["exit_due_utc"] == timestamp_text(expected_exit_due)
    if has_exit_policy:
        assert position["exit_policy_state"] is not None
    else:
        assert position["exit_policy_state"] is None
        position["exit_policy_state"] = {"mode": "continuous", "accumulated_milliseconds": 1200000}
    runner._now = lambda: pd.Timestamp("2026-08-21 10:31:01", tz="UTC")
    assert not runner._handle_time_exit(
        SimpleNamespace(bid=float(position["entry_price"]) * 1.0005, ask=float(position["entry_price"]) * 1.0006)
    )
    assert runner.state["positions"][0] is position
    runner._now = lambda: expected_exit_due + pd.Timedelta(seconds=1)
    runner.executor.tick_time_msc = int((expected_exit_due + pd.Timedelta(seconds=1)).timestamp() * 1000)
    assert runner._handle_time_exit(runner.executor.get_symbol_info("USTEC"))
    assert runner.state["positions"] == []
    assert any(event == "position_close" for event, _kwargs in events)
    waiting_events = len(events)
    runner.state["positions"] = [{"close_requested": True}]
    assert runner._handle_time_exit(runner.executor.get_symbol_info("USTEC"))
    assert len(events) == waiting_events
    runner.state["positions"] = []
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
    retry_runner.state["positions"] = [retry_position]
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
    assert retry_runner.state["positions"][0] is retry_position
    assert utc_timestamp(retry_position["time_close_retry_after_utc"]) == expected_exit_due + pd.Timedelta(seconds=61)
    assert close_failure_retcode("ERR|10018") == "10018"
    runner._now = lambda: pd.Timestamp("2026-08-21 10:01:05", tz="UTC")
    runner._open_entry(
        SimpleNamespace(bid=100.0, ask=101.0, tick_time_msc=None),
        {"bar_time": signal_bar, "side": "LONG", "eligible": True},
    )
    if has_exit_policy:
        assert runner.state["positions"] == []
        assert runner.state["sync_block_reason"] == "tick_time_unavailable_for_c4566_entry"
        runner.state = runner._default_state()
    else:
        assert runner.state["positions"]
        assert runner.state["positions"][0]["exit_policy_state"] is None
        runner.state["positions"] = []
    foreign = SimpleNamespace(symbol=params["mt5_symbol"], magic=EXPECTED_MAGIC + 1, comment=params["strategy"]["comment_prefix"], ticket=1)
    assert not runner._owned_position(foreign)
    wrong_comment = SimpleNamespace(symbol=params["mt5_symbol"], magic=EXPECTED_MAGIC, comment="foreign", ticket=2)
    assert not runner._owned_position(wrong_comment)
    runner.live_enabled = True
    runner.state["positions"] = [shadow_position, second_shadow_position]
    runner.executor.positions = [
        SimpleNamespace(
            symbol=params["mt5_symbol"],
            magic=EXPECTED_MAGIC,
            comment=params["strategy"]["comment_prefix"],
            ticket=shadow_position["ticket"],
            identifier=shadow_position["position_identifier"],
            type=ORDER_TYPE_BUY,
        ),
        SimpleNamespace(
            symbol=params["mt5_symbol"],
            magic=EXPECTED_MAGIC,
            comment=params["strategy"]["comment_prefix"],
            ticket=second_shadow_position["ticket"],
            identifier=second_shadow_position["position_identifier"],
            type=ORDER_TYPE_BUY,
        ),
    ]
    runner.executor.orders_available = False
    assert runner._sync_position(), "pending-order inventory failure must not suppress an exactly-owned exit"
    assert runner.state["sync_block_reason"] == "orders_unavailable"
    runner.executor.orders_available = True
    assert runner._sync_position()
    assert len(runner.state["positions"]) == 2
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
    runner.state["positions"] = []
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
    if arguments.self_test:
        self_test()
        print(f"{BOT_SUFFIX} self-test ok")
        return 0
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
