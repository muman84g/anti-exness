from __future__ import annotations

import copy
import logging
import math
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import numpy as np

from live_config import MT5_LOGIN, MT5_SERVER
from live_executor import ORDER_TYPE_BUY, ORDER_TYPE_SELL
from v206_range_strategy import latest_v206_signal, target_from_actual_fill


UTC = timezone.utc
STATE_VERSION = 2
FLAT_CONFIRMATIONS = 3
QUOTE_FUTURE_TOLERANCE_SECONDS = 5
CLOSE_BLOCK_REASONS = {
    "v206_timeout_close_unconfirmed",
    "v206_close_submission_unresolved",
    "v206_close_deal_not_confirmed",
    "v206_close_deal_invalid",
}
RECOVERABLE_BLOCK_REASONS = {
    "v206_namespace_migration_pending",
    "v206_inventory_unavailable",
    "v206_orders_unavailable",
    "v206_orders_unavailable_after_confirmed_close",
    "v206_quote_clock_invalid",
    "v206_server_protection_repair_failed",
}
POLICY_ID = "man_237_v206/path_monotonic_center_approach"
RESEARCH_SHA256 = "3c1f2701835e202b9c0c7ddb478c29b72ebf34581d68986be13c46329a384996"
EXIT_SHA256 = "6276b5ed3be24c5afaf757a0d4597eef37f7dfbbdaee360a6598d27fa1d5a1db"
NONRECOVERABLE_OPEN_GUARD_REASONS = {
    "BAD_OPEN_R1_GUARD",
    "OPEN_R1_POLICY_GUARD",
    "OPEN_R1_INVENTORY_GUARD",
    "OPEN_R1_INVENTORY_QUERY",
    "OPEN_R1_ORDER_QUERY",
    "SYMBOL_ADMISSION_GUARD",
    "MARGIN_ADMISSION_GUARD",
    "ACCOUNT_IDENTITY_GUARD",
    "ACCOUNT_MODE_GUARD",
    "BAD_OPEN_TYPE",
    "INVALID_OPEN_R1_REQUEST",
}


def default_v206_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "policy_id": POLICY_ID,
        "research_sha256": RESEARCH_SHA256,
        "exit_sha256": EXIT_SHA256,
        "migration_pending": True,
        "migration_flat_confirmations": 0,
        "basket": [],
        "pending_signal": None,
        "pending_open": None,
        "pending_close": None,
        "close_retry_after_utc": None,
        "close_permission_reject_count": 0,
        "entry_permission_reject_count": 0,
        "time_close_defer_started_utc": None,
        "time_close_last_quote_msc": None,
        "time_close_stable_count": 0,
        "time_close_wide_seen": False,
        "last_evaluated_bar": None,
        "last_decision": None,
        "last_history_unavailable_minute": None,
        "last_history_fetch_minute": None,
        "last_quote_time_utc": None,
        "cooldown_until_utc": None,
        "last_closed_at_utc": None,
        "last_closed_side": None,
        "last_closed_reason": None,
        "last_closed_signal_bar": None,
        "last_consumed_signal_bar": None,
        "blocked_reason": "v206_namespace_migration_pending",
        "blocked_details": {},
        "manual_alert_last_signature": None,
        "quarantined_state_snapshot": None,
    }


def _utc(value: Any) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    try:
        stamp = pd.Timestamp(value)
    except Exception:
        return None
    if pd.isna(stamp):
        return None
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _text(value: Any) -> str:
    stamp = _utc(value)
    return "" if stamp is None else stamp.isoformat()


def _finite_positive(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(number) and number > 0.0


def _finite_nonnegative(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(number) and number >= 0.0


class V206LiveLane:
    """One-position v206 lifecycle isolated from every pre-existing strategy lane."""

    def __init__(self, runner: Any):
        self.runner = runner
        self.params = runner.params
        self.cfg = dict(self.params.get("v206_strategy") or {})
        self.symbol = str(self.params.get("mt5_symbol", self.params.get("symbol", "")))
        error = self.config_error()
        if error is not None:
            raise ValueError(f"invalid v206 configuration: {error}")

    def config_error(self) -> str | None:
        expected = {
            "enabled": True,
            "id": "range_rotation_v206_lane_1",
            "spec_id": "man_237_v206",
            "signal_id": "path_monotonic_center_approach",
            "lot": 0.01,
            "max_positions": 1,
            "cooldown": 5,
            "timeout_minutes": 30,
            "stop_atr": 0.5,
            "target_r": 1.0,
        }
        if not isinstance(self.params.get("v206_enabled"), bool):
            return "v206_enabled_not_boolean"
        if self.symbol != "XAUUSD" or int(self.params.get("v206_m1_bars", 0)) != 1000:
            return "symbol_or_history_contract"
        for key, value in expected.items():
            observed = self.cfg.get(key)
            if isinstance(value, float):
                if isinstance(observed, bool) or not isinstance(observed, (int, float)) or not math.isclose(float(observed), value, rel_tol=0.0, abs_tol=1e-12):
                    return f"{key}={observed!r}"
            elif observed != value:
                return f"{key}={observed!r}"
        magic = int(self.cfg.get("magic", 0) or 0)
        comment = str(self.cfg.get("comment_prefix", ""))
        if (magic, comment) not in {(230044, "s23_v206"), (240206, "s24_v206")}:
            return f"ownership_namespace={(magic, comment)!r}"
        expected_lane = 22 if magic == 230044 else 206
        if int(self.cfg.get("lane_id", 0) or 0) != expected_lane:
            return f"lane_id={self.cfg.get('lane_id')!r}"
        return None

    @property
    def enabled(self) -> bool:
        # A disabled entry switch must not make broker-owned inventory invisible.
        # The lane always performs read-only reconciliation when its namespace is configured.
        return bool(self.cfg)

    @staticmethod
    def _normalize_time_close_state(raw: dict[str, Any]) -> bool:
        """Repair close-defer auxiliaries without discarding broker-owned inventory."""
        defaults = {
            "time_close_defer_started_utc": None,
            "time_close_last_quote_msc": None,
            "time_close_stable_count": 0,
            "time_close_wide_seen": False,
        }
        changed = False
        for key, value in defaults.items():
            if key not in raw:
                raw[key] = value
                changed = True
        started = _utc(raw.get("time_close_defer_started_utc"))
        last_msc = raw.get("time_close_last_quote_msc")
        count = raw.get("time_close_stable_count")
        wide = raw.get("time_close_wide_seen")
        valid = (
            (raw.get("time_close_defer_started_utc") is None or started is not None)
            and (last_msc is None or (isinstance(last_msc, int) and not isinstance(last_msc, bool) and last_msc > 0))
            and isinstance(count, int) and not isinstance(count, bool) and count >= 0
            and isinstance(wide, bool)
            and ((wide and started is not None) or (not wide and started is None and count == 0))
        )
        if not valid:
            for key, value in defaults.items():
                if raw.get(key) != value:
                    raw[key] = value
                    changed = True
        return changed

    def _state_shape_error(self, raw: Any) -> str | None:
        if not isinstance(raw, dict):
            return "not_object"
        if (
            raw.get("version") != STATE_VERSION
            or raw.get("policy_id") != POLICY_ID
            or raw.get("research_sha256") != RESEARCH_SHA256
            or raw.get("exit_sha256") != EXIT_SHA256
        ):
            return "identity_mismatch"
        if not isinstance(raw.get("migration_pending"), bool):
            return "migration_pending_invalid"
        if raw.get("blocked_reason") is not None and not isinstance(raw.get("blocked_reason"), str):
            return "blocked_reason_invalid"
        if raw.get("manual_alert_last_signature") is not None and not isinstance(raw.get("manual_alert_last_signature"), str):
            return "manual_alert_last_signature_invalid"
        if raw.get("quarantined_state_snapshot") is not None and not isinstance(raw.get("quarantined_state_snapshot"), dict):
            return "quarantined_state_snapshot_invalid"
        if not isinstance(raw.get("basket"), list) or len(raw["basket"]) > 1:
            return "basket_shape"
        if not isinstance(raw.get("blocked_details", {}), dict):
            return "blocked_details_shape"
        for key in ("migration_flat_confirmations", "entry_permission_reject_count"):
            value = raw.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > FLAT_CONFIRMATIONS:
                return f"{key}_invalid"
        for key in ("last_evaluated_bar", "last_history_unavailable_minute", "last_history_fetch_minute", "last_quote_time_utc", "cooldown_until_utc", "close_retry_after_utc", "time_close_defer_started_utc", "last_closed_at_utc", "last_closed_signal_bar", "last_consumed_signal_bar"):
            if raw.get(key) not in (None, "") and (
                not isinstance(raw.get(key), str) or _utc(raw.get(key)) is None
            ):
                return f"{key}_invalid"
        close_at = _utc(raw.get("last_closed_at_utc"))
        close_side = raw.get("last_closed_side")
        close_reason = raw.get("last_closed_reason")
        close_identity_present = any(
            value not in (None, "")
            for value in (raw.get("last_closed_at_utc"), close_side, close_reason)
        )
        if close_identity_present and (
            close_at is None
            or not isinstance(close_side, str)
            or close_side not in {"LONG", "SHORT"}
            or not isinstance(close_reason, str)
            or not close_reason
        ):
            return "last_closed_identity_invalid"
        if not close_identity_present and any(
            raw.get(key) not in (None, "")
            for key in ("last_closed_signal_bar", "last_consumed_signal_bar")
        ):
            return "last_closed_identity_invalid"
        basket = raw["basket"]
        if basket:
            row = basket[0]
            if not isinstance(row, dict):
                return "basket_row_shape"
            if (
                isinstance(row.get("ticket"), bool)
                or not isinstance(row.get("ticket"), int)
                or int(row.get("ticket") or 0) <= 0
                or isinstance(row.get("position_identifier"), bool)
                or not isinstance(row.get("position_identifier"), int)
                or int(row.get("position_identifier") or 0) <= 0
                or not isinstance(row.get("side"), str)
                or row.get("side") not in {"LONG", "SHORT"}
                or isinstance(row.get("lot"), bool)
                or not isinstance(row.get("lot"), (int, float))
                or not math.isfinite(float(row.get("lot")))
                or not math.isclose(float(row.get("lot") or 0.0), float(self.cfg["lot"]), rel_tol=0.0, abs_tol=1e-12)
                or not all(_finite_positive(row.get(key)) for key in ("entry_price", "fixed_stop"))
                or (row.get("target") is not None and not _finite_nonnegative(row.get("target")))
                or isinstance(row.get("open_time_epoch"), bool)
                or not isinstance(row.get("open_time_epoch"), int)
                or int(row.get("open_time_epoch") or 0) <= 0
                or not isinstance(row.get("entry_time_utc"), str)
                or _utc(row.get("entry_time_utc")) is None
                or not isinstance(row.get("signal_bar_time"), str)
                or _utc(row.get("signal_bar_time")) is None
                or not isinstance(row.get("timeout_at_utc"), str)
                or _utc(row.get("timeout_at_utc")) is None
                or row.get("owner_symbol") != self.symbol
                or isinstance(row.get("owner_magic"), bool)
                or not isinstance(row.get("owner_magic"), int)
                or int(row.get("owner_magic") or 0) != int(self.cfg["magic"])
                or row.get("owner_comment") != str(self.cfg["comment_prefix"])
            ):
                return "basket_row_invalid"
            entry_time = _utc(row.get("entry_time_utc"))
            timeout = _utc(row.get("timeout_at_utc"))
            if entry_time is None or timeout != entry_time + pd.Timedelta(minutes=int(self.cfg["timeout_minutes"])):
                return "basket_timeout_not_fill_based"
        for key in ("pending_signal", "pending_open", "pending_close", "last_decision"):
            if raw.get(key) is not None and not isinstance(raw.get(key), dict):
                return f"{key}_shape"
        if raw.get("pending_signal") is not None and self._pending_signal_error(raw["pending_signal"]) is not None:
            return "pending_signal_invalid"
        if raw.get("pending_open") is not None and self._pending_open_error(raw["pending_open"]) is not None:
            return "pending_open_invalid"
        if sum(bool(value) for value in (basket, raw.get("pending_signal"), raw.get("pending_open"))) > 1:
            return "open_lifecycle_container_conflict"
        pending_close = raw.get("pending_close")
        if pending_close is not None and (
            isinstance(pending_close.get("ticket"), bool)
            or not isinstance(pending_close.get("ticket"), int)
            or int(pending_close.get("ticket") or 0) <= 0
            or isinstance(pending_close.get("position_identifier"), bool)
            or not isinstance(pending_close.get("position_identifier"), int)
            or int(pending_close.get("position_identifier") or 0) <= 0
            or isinstance(pending_close.get("lot"), bool)
            or not isinstance(pending_close.get("lot"), (int, float))
            or not math.isfinite(float(pending_close.get("lot")))
            or not math.isclose(float(pending_close.get("lot") or 0.0), float(self.cfg["lot"]), rel_tol=0.0, abs_tol=1e-12)
            or not isinstance(pending_close.get("started_utc"), str)
            or _utc(pending_close.get("started_utc")) is None
            or pending_close.get("reason") != "timeout_30m"
        ):
            return "pending_close_invalid"
        if pending_close is not None:
            if not basket:
                return "pending_close_without_basket"
            basket_row = basket[0]
            if (
                int(pending_close.get("ticket") or 0) != int(basket_row.get("ticket") or 0)
                or int(pending_close.get("position_identifier") or 0) != int(basket_row.get("position_identifier") or 0)
                or not math.isclose(
                    float(pending_close.get("lot") or 0.0),
                    float(basket_row.get("lot") or 0.0),
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            ):
                return "pending_close_basket_identity_mismatch"
            has_confirmed = "confirmed_response" in pending_close or "deal" in pending_close
            confirmed = pending_close.get("confirmed_response")
            deal = pending_close.get("deal")
            if has_confirmed and (
                confirmed is not True
                or isinstance(deal, bool)
                or not isinstance(deal, int)
                or deal <= 0
            ):
                return "pending_close_receipt_invalid"
        decision = raw.get("last_decision")
        if decision is not None and (
            not isinstance(decision.get("signal_bar_time"), str)
            or _utc(decision.get("signal_bar_time")) is None
            or not isinstance(decision.get("outcome"), str)
            or not decision.get("outcome")
        ):
            return "last_decision_invalid"
        reject_count = raw.get("close_permission_reject_count", 0)
        if isinstance(reject_count, bool) or not isinstance(reject_count, int) or reject_count < 0:
            return "close_permission_reject_count_invalid"
        last_quote_msc = raw.get("time_close_last_quote_msc")
        stable_count = raw.get("time_close_stable_count", 0)
        wide_seen = raw.get("time_close_wide_seen", False)
        defer_started = _utc(raw.get("time_close_defer_started_utc"))
        if (
            (last_quote_msc is not None and (isinstance(last_quote_msc, bool) or not isinstance(last_quote_msc, int) or last_quote_msc <= 0))
            or isinstance(stable_count, bool) or not isinstance(stable_count, int) or stable_count < 0
            or not isinstance(wide_seen, bool)
            or (wide_seen and defer_started is None)
            or (not wide_seen and (defer_started is not None or stable_count != 0))
        ):
            return "time_close_spread_state_invalid"
        return None

    @property
    def state(self) -> dict[str, Any]:
        raw = self.runner.state.setdefault("v206", default_v206_state())
        default = default_v206_state()
        normalized = isinstance(raw, dict) and self._normalize_time_close_state(raw)
        if isinstance(raw, dict) and "entry_permission_reject_count" not in raw:
            raw["entry_permission_reject_count"] = 0
            normalized = True
        try:
            error = self._state_shape_error(raw)
        except (TypeError, ValueError, OverflowError):
            error = "value_parse_error"
        if error is not None:
            rejected = copy.deepcopy(raw)
            raw = default
            raw["blocked_reason"] = "v206_state_identity_mismatch"
            raw["blocked_details"] = {"reason": error, "quarantined": True}
            raw["quarantined_state_snapshot"] = rejected
            self.runner.state["v206"] = raw
            logging.error("v206 state rejected and reset to migration gate: %s", error)
            self.runner._save_state()
            if hasattr(self.runner, "_notify_reconciliation_required"):
                try:
                    self.runner._notify_reconciliation_required(self.cfg, raw["blocked_reason"], raw["blocked_details"])
                except Exception:
                    logging.exception("v206 invalid-state alert failed")
        else:
            for key, value in default.items():
                raw.setdefault(key, value)
            if normalized:
                self.runner._save_state()
        return raw

    def _save(self) -> None:
        self.runner._save_state()

    def _log(self, event: str, **kwargs: Any) -> None:
        st = self.state
        pending = st.get("pending_open") or st.get("pending_signal") or {}
        if isinstance(pending, dict) and pending.get("opportunity_id"):
            kwargs.setdefault("opportunity_id", str(pending["opportunity_id"]))
        basket = st.get("basket") or []
        if isinstance(basket, list) and len(basket) == 1 and isinstance(basket[0], dict):
            row = basket[0]
            kwargs.setdefault("ticket", row.get("ticket", ""))
            kwargs.setdefault("position_identifier", row.get("position_identifier", ""))
            kwargs.setdefault("entry_price", row.get("entry_price", ""))
        try:
            self.runner._trade_row(event, self.cfg, **kwargs)
        except Exception:
            logging.exception("v206 audit row failed")

    def _block(self, reason: str, *, replace_resolved: bool = False, **details: Any) -> None:
        st = self.state
        previous_reason = st.get("blocked_reason")
        if (
            not replace_resolved
            and previous_reason is not None
            and previous_reason not in RECOVERABLE_BLOCK_REASONS
            and reason in RECOVERABLE_BLOCK_REASONS
        ):
            logging.warning("v206 retained non-recoverable block: %s", previous_reason)
            return
        changed = st.get("blocked_reason") != reason or st.get("blocked_details") != details
        st["blocked_reason"] = reason
        st["blocked_details"] = details
        if changed:
            logging.error("v206 entry block: %s %s", reason, details)
            self._save()
            if reason not in RECOVERABLE_BLOCK_REASONS and hasattr(self.runner, "_notify_reconciliation_required"):
                try:
                    self.runner._notify_reconciliation_required(self.cfg, reason, details)
                    self._save()
                except Exception:
                    logging.exception("v206 manual-action alert failed")

    def _clear_block(self, allowed: set[str] | None = None) -> None:
        st = self.state
        reason = st.get("blocked_reason")
        if reason is not None and (allowed is None or reason in allowed):
            st["blocked_reason"] = None
            st["blocked_details"] = {}
            st["manual_alert_last_signature"] = None
            self._save()

    def _reset_time_close_state(self) -> None:
        st = self.state
        st["time_close_defer_started_utc"] = None
        st["time_close_last_quote_msc"] = None
        st["time_close_stable_count"] = 0
        st["time_close_wide_seen"] = False

    def _time_close_ready(self, info: Any, quote_time: pd.Timestamp) -> bool:
        st = self.state
        quote_msc = int(quote_time.timestamp() * 1000)
        last_msc = st.get("time_close_last_quote_msc")
        if last_msc is not None and quote_msc <= int(last_msc):
            return False
        st["time_close_last_quote_msc"] = quote_msc
        point = float(self.params.get("point_size", 0.001))
        bid = float(getattr(info, "bid", float("nan")))
        ask = float(getattr(info, "ask", float("nan")))
        cap = float(self.params.get("time_close_max_spread_points", 300.0))
        spread = (ask - bid) / point if point > 0.0 else math.inf
        if not all(math.isfinite(value) for value in (bid, ask, point, cap, spread)) or bid <= 0.0 or ask < bid or point <= 0.0 or cap <= 0.0:
            self._save()
            return False
        wide_seen = bool(st.get("time_close_wide_seen"))
        started = _utc(st.get("time_close_defer_started_utc"))
        if not wide_seen and spread > cap:
            st["time_close_wide_seen"] = True
            st["time_close_defer_started_utc"] = quote_time.isoformat()
            st["time_close_stable_count"] = 0
            self._log("v206_close_deferred", reason="spread_wide", note=f"spread_points={spread:.3f};cap={cap:.3f}")
            self._save()
            return False
        if wide_seen:
            if started is None:
                self._reset_time_close_state()
                self._save()
                return False
            force_after = started + pd.Timedelta(minutes=float(self.params.get("time_close_force_after_minutes", 30.0)))
            if quote_time < force_after:
                count = int(st.get("time_close_stable_count", 0)) + 1 if spread <= cap else 0
                st["time_close_stable_count"] = count
                self._save()
                if count < int(self.params.get("time_close_stable_quotes", 3)):
                    return False
        return True

    def _owned(self, record: Any) -> bool:
        return (
            str(getattr(record, "symbol", "")) == self.symbol
            and int(getattr(record, "magic", -1)) == int(self.cfg.get("magic", 0))
            and str(getattr(record, "comment", "") or "") == str(self.cfg.get("comment_prefix", ""))
        )

    def _inventory(self) -> tuple[list[Any] | None, list[Any] | None]:
        magic = int(self.cfg.get("magic", 0))
        return (
            self.runner.executor.get_positions(self.symbol, magic),
            self.runner.executor.get_orders(self.symbol, magic),
        )

    def _pending_open_error(self, pending: Any) -> str | None:
        if not isinstance(pending, dict):
            return "not_object"
        side = str(pending.get("side") or "")
        signal_bar = _utc(pending.get("signal_bar_time"))
        due = _utc(pending.get("entry_due_utc"))
        expiry = _utc(pending.get("entry_expiry_utc"))
        started = _utc(pending.get("started_utc"))
        flat_confirmations = pending.get("flat_confirmations")
        expected_id = f"v206:{signal_bar.isoformat()}:{side}" if signal_bar is not None else ""
        if (
            not isinstance(pending.get("side"), str)
            or side not in {"LONG", "SHORT"}
            or not all(isinstance(pending.get(key), str) for key in ("signal_bar_time", "entry_due_utc", "entry_expiry_utc", "started_utc"))
            or signal_bar is None or due is None or expiry is None or started is None
            or due != signal_bar + pd.Timedelta(minutes=1)
            or expiry != due + pd.Timedelta(minutes=float(self.params.get("max_signal_delay_minutes", 2)))
            or started < due or started > expiry
            or str(pending.get("opportunity_id") or "") != expected_id
            or isinstance(flat_confirmations, bool)
            or not isinstance(flat_confirmations, int)
            or flat_confirmations < 0
            or flat_confirmations >= FLAT_CONFIRMATIONS
            or isinstance(pending.get("lot"), bool)
            or not isinstance(pending.get("lot"), (int, float))
            or not math.isfinite(float(pending.get("lot")))
            or not math.isclose(float(pending.get("lot") or 0.0), float(self.cfg["lot"]), rel_tol=0.0, abs_tol=1e-12)
            or not _finite_positive(pending.get("fixed_stop"))
            or pending.get("owner_symbol") != self.symbol
            or isinstance(pending.get("owner_magic"), bool)
            or not isinstance(pending.get("owner_magic"), int)
            or int(pending.get("owner_magic") or 0) != int(self.cfg["magic"])
            or pending.get("owner_comment") != str(self.cfg["comment_prefix"])
        ):
            return "identity_or_clock_invalid"
        return None

    def _adopt(self, position: Any, pending: dict[str, Any]) -> None:
        try:
            pending_error = self._pending_open_error(pending)
        except (TypeError, ValueError, OverflowError):
            pending_error = "value_parse_error"
        if pending_error is not None:
            self._block("v206_pending_open_state_invalid", cause=pending_error)
            return
        position_type = int(getattr(position, "type", -1))
        if position_type not in {ORDER_TYPE_BUY, ORDER_TYPE_SELL}:
            self._block("v206_pending_fill_type_invalid", observed=position_type)
            return
        side = "LONG" if position_type == ORDER_TYPE_BUY else "SHORT"
        expected_side = str(pending.get("side") or "")
        if side != expected_side:
            self._block("v206_pending_fill_side_mismatch", expected=expected_side, observed=side)
            return
        open_time = int(getattr(position, "open_time", 0) or 0)
        started = _utc(pending["started_utc"])
        expiry = _utc(pending["entry_expiry_utc"])
        live_time = pd.Timestamp(open_time, unit="s", tz="UTC") if open_time > 0 else None
        if (
            live_time is None or started is None or expiry is None
            or live_time < started - pd.Timedelta(seconds=5)
            or live_time > expiry + pd.Timedelta(seconds=30)
        ):
            self._block("v206_broker_open_time_unavailable", ticket=int(position.ticket))
            return
        ticket = int(position.ticket)
        identifier = int(getattr(position, "identifier", 0) or ticket)
        lot = float(getattr(position, "volume", 0.0))
        entry_price = float(getattr(position, "open_price", 0.0))
        live_stop = float(getattr(position, "sl", 0.0) or 0.0)
        fixed_stop = float(pending["fixed_stop"])
        tolerance = 0.5 * float(self.params.get("point_size", 0.001))
        if (
            ticket <= 0 or identifier <= 0
            or not math.isfinite(lot)
            or not math.isclose(lot, float(self.cfg["lot"]), rel_tol=0.0, abs_tol=1e-12)
            or not math.isfinite(entry_price) or entry_price <= 0.0
            or not math.isfinite(live_stop)
        ):
            self._block("v206_pending_fill_identity_invalid", ticket=ticket, identifier=identifier)
            return
        self.state["basket"] = [{
            "ticket": ticket,
            "position_identifier": identifier,
            "side": side,
            "lot": lot,
            "entry_price": entry_price,
            "entry_time_utc": datetime.fromtimestamp(open_time, UTC).isoformat(),
            "open_time_epoch": open_time,
            "owner_symbol": self.symbol,
            "owner_magic": int(self.cfg["magic"]),
            "owner_comment": str(self.cfg["comment_prefix"]),
            "signal_bar_time": str(pending["signal_bar_time"]),
            "timeout_at_utc": (live_time + pd.Timedelta(minutes=int(self.cfg["timeout_minutes"]))).isoformat(),
            "fixed_stop": fixed_stop,
            "target": float(getattr(position, "tp", 0.0) or 0.0),
        }]
        self.state["pending_open"] = None
        self._save()
        if not math.isclose(live_stop, fixed_stop, rel_tol=0.0, abs_tol=tolerance):
            self._block("v206_fixed_stop_missing_or_changed", ticket=ticket, expected_stop=fixed_stop, observed_stop=live_stop)
            return
        self._repair_if_needed(position)

    def _repair_if_needed(self, position: Any) -> bool:
        basket = list(self.state.get("basket") or [])
        if len(basket) != 1:
            return False
        row = basket[0]
        side = str(row["side"])
        if (
            int(getattr(position, "ticket", 0) or 0) != int(row.get("ticket") or 0)
            or int(getattr(position, "identifier", 0) or 0) != int(row.get("position_identifier") or 0)
            or not self._owned(position)
            or not math.isclose(float(getattr(position, "volume", 0.0)), float(row.get("lot", 0.0)), rel_tol=0.0, abs_tol=1e-12)
            or not _finite_positive(getattr(position, "open_price", 0.0))
        ):
            self._block("v206_state_live_identity_mismatch", ticket=int(getattr(position, "ticket", 0) or 0))
            return False
        stop = float(row.get("fixed_stop") or 0.0)
        live_stop = float(getattr(position, "sl", 0.0) or 0.0)
        tolerance = 0.5 * float(self.params.get("point_size", 0.001))
        if not math.isclose(live_stop, stop, rel_tol=0.0, abs_tol=tolerance):
            self._block(
                "v206_fixed_stop_missing_or_changed",
                ticket=int(position.ticket), expected_stop=stop, observed_stop=live_stop,
            )
            return False
        try:
            expected_target = target_from_actual_fill(side, float(position.open_price), stop)
        except ValueError:
            self._block("v206_invalid_live_risk_geometry", ticket=int(position.ticket))
            return False
        if (
            math.isclose(live_stop, stop, rel_tol=0.0, abs_tol=tolerance)
            and math.isclose(float(getattr(position, "tp", 0.0)), expected_target, rel_tol=0.0, abs_tol=tolerance)
        ):
            observed_target = float(position.tp)
            changed = not math.isclose(float(row.get("target", 0.0) or 0.0), observed_target, rel_tol=0.0, abs_tol=tolerance)
            row["fixed_stop"] = stop
            row["target"] = observed_target
            prior_block = self.state.get("blocked_reason")
            self._clear_block({"v206_server_protection_repair_failed"})
            if changed and prior_block != "v206_server_protection_repair_failed":
                self._save()
            return True
        result = self.runner.executor.repair_r1_position(
            ticket=int(position.ticket), expected_login=int(MT5_LOGIN), expected_server=str(MT5_SERVER),
            expected_symbol=self.symbol, expected_magic=int(self.cfg["magic"]),
            expected_comment=str(self.cfg["comment_prefix"]),
            expected_identifier=int(getattr(position, "identifier", 0) or position.ticket),
        )
        if not result.ok:
            self._block("v206_server_protection_repair_failed", ticket=int(position.ticket), status=result.status, raw=result.raw_response)
            return False
        row["fixed_stop"] = float(result.stop)
        row["target"] = float(result.target)
        self._clear_block({"v206_server_protection_repair_failed"})
        self._save()
        self._log("v206_protection_repaired", ticket=int(position.ticket), side=side, price=float(result.fill), note=result.raw_response)
        return True

    def _sync(self, quote_time: pd.Timestamp, info: Any, *, time_actions_allowed: bool = True) -> bool:
        st = self.state
        positions = self.runner.executor.get_positions(self.symbol, int(self.cfg["magic"]))
        if positions is None:
            self._block("v206_inventory_unavailable")
            return False
        if any(not self._owned(record) for record in positions):
            self._block("v206_namespace_contains_foreign_inventory")
            return False
        position_keys = [
            (int(getattr(row, "ticket", 0) or 0), int(getattr(row, "identifier", 0) or 0))
            for row in positions
        ]
        if any(ticket <= 0 or identifier <= 0 for ticket, identifier in position_keys) or len(position_keys) != len(set(position_keys)):
            self._block("v206_live_position_identity_invalid")
            return False
        if len(positions) > 1:
            self._block("v206_multiple_owned_positions", tickets=[int(row.ticket) for row in positions])
            return False
        orders = self.runner.executor.get_orders(self.symbol, int(self.cfg["magic"]))
        orders_available = orders is not None
        if orders_available:
            if any(not self._owned(record) for record in orders):
                self._block("v206_namespace_contains_foreign_inventory")
                return False
            order_tickets = [int(getattr(row, "ticket", 0) or 0) for row in orders]
            if any(ticket <= 0 for ticket in order_tickets) or len(order_tickets) != len(set(order_tickets)):
                self._block("v206_live_order_identity_invalid")
                return False
            if orders:
                self._block("v206_unexpected_pending_order", tickets=order_tickets)
                return False
        if st.get("migration_pending"):
            if not orders_available:
                self._block("v206_orders_unavailable")
                return False
            if positions:
                if st.get("blocked_reason") == "v206_state_identity_mismatch":
                    details = dict(st.get("blocked_details") or {})
                    if "reason" in details:
                        details["state_error"] = details.pop("reason")
                    details["live_tickets"] = [int(row.ticket) for row in positions]
                    self._block("v206_state_identity_mismatch", **details)
                else:
                    self._block("v206_migration_inventory_not_flat", tickets=[int(row.ticket) for row in positions])
                return False
            quarantined = st.get("quarantined_state_snapshot")
            if isinstance(quarantined, dict) and (
                bool(quarantined.get("basket"))
                or quarantined.get("pending_open") is not None
                or quarantined.get("pending_close") is not None
            ):
                details = dict(st.get("blocked_details") or {})
                if "reason" in details:
                    details["state_error"] = details.pop("reason")
                details["active_lifecycle_quarantined"] = True
                self._block("v206_state_identity_mismatch", **details)
                return False
            st["migration_flat_confirmations"] = int(st.get("migration_flat_confirmations", 0)) + 1
            if st["migration_flat_confirmations"] < FLAT_CONFIRMATIONS:
                self._save()
                return False
            st["migration_pending"] = False
            st["migration_flat_confirmations"] = FLAT_CONFIRMATIONS
            self._clear_block()
            self._save()
        basket = list(st.get("basket") or [])
        pending = st.get("pending_open")
        if not basket and pending:
            try:
                pending_error = self._pending_open_error(pending)
            except (TypeError, ValueError, OverflowError):
                pending_error = "value_parse_error"
            if pending_error is not None:
                self._block("v206_pending_open_state_invalid", cause=pending_error)
                return False
            if not orders_available:
                self._block("v206_orders_unavailable")
                return False
            if positions:
                self._adopt(positions[0], pending)
                return bool(self.state.get("basket")) and not bool(self.state.get("blocked_reason"))
            pending["flat_confirmations"] = int(pending.get("flat_confirmations", 0)) + 1
            if pending["flat_confirmations"] >= FLAT_CONFIRMATIONS:
                self._log("v206_open_resolved_no_fill", reason="three_confirmed_flat_inventory_polls", signal_bar_time=pending.get("signal_bar_time"))
                st["pending_open"] = None
                self._clear_block()
            self._save()
            return False
        if not basket:
            if positions:
                self._block("v206_live_position_without_durable_state", tickets=[int(row.ticket) for row in positions])
                return False
            if not orders_available:
                self._block("v206_orders_unavailable")
                return False
            self._clear_block(RECOVERABLE_BLOCK_REASONS)
            return True
        if len(basket) != 1:
            self._block("v206_invalid_basket_state")
            return False
        state_pos = basket[0]
        identifier = int(state_pos.get("position_identifier") or state_pos.get("ticket") or 0)
        if positions:
            live = positions[0]
            live_identifier = int(getattr(live, "identifier", 0) or live.ticket)
            if (
                int(getattr(live, "ticket", 0) or 0) != int(state_pos.get("ticket") or 0)
                or live_identifier != identifier
                or not self._owned(live)
                or not math.isclose(float(getattr(live, "volume", 0.0)), float(state_pos.get("lot", 0.0)), rel_tol=0.0, abs_tol=1e-12)
            ):
                self._block("v206_state_live_identity_mismatch", state_identifier=identifier, live_identifier=live_identifier)
                return False
            try:
                broker_open_epoch = int(getattr(live, "open_time", 0) or 0)
                broker_entry_price = float(getattr(live, "open_price", 0.0) or 0.0)
            except (TypeError, ValueError, OverflowError):
                broker_open_epoch = 0
                broker_entry_price = 0.0
            if broker_open_epoch <= 0 or not math.isfinite(broker_entry_price) or broker_entry_price <= 0.0:
                self._block("v206_broker_fill_identity_unavailable", ticket=int(getattr(live, "ticket", 0) or 0))
                return False
            broker_entry_time = pd.Timestamp(broker_open_epoch, unit="s", tz="UTC")
            broker_timeout = broker_entry_time + pd.Timedelta(minutes=int(self.cfg["timeout_minutes"]))
            if (
                int(state_pos.get("open_time_epoch") or 0) != broker_open_epoch
                or _utc(state_pos.get("entry_time_utc")) != broker_entry_time
                or _utc(state_pos.get("timeout_at_utc")) != broker_timeout
                or not math.isclose(float(state_pos.get("entry_price") or 0.0), broker_entry_price, rel_tol=0.0, abs_tol=1e-12)
            ):
                previous_open_epoch = state_pos.get("open_time_epoch")
                previous_entry_price = state_pos.get("entry_price")
                state_pos["open_time_epoch"] = broker_open_epoch
                state_pos["entry_time_utc"] = broker_entry_time.isoformat()
                state_pos["timeout_at_utc"] = broker_timeout.isoformat()
                state_pos["entry_price"] = broker_entry_price
                self._log(
                    "v206_lifecycle_recovered",
                    ticket=int(live.ticket),
                    reason="confirmed_broker_fill_identity_restored",
                    note=(
                        f"previous_open_epoch={previous_open_epoch};broker_open_epoch={broker_open_epoch};"
                        f"previous_entry_price={previous_entry_price};broker_entry_price={broker_entry_price}"
                    ),
                )
                self._save()
            if not self._repair_if_needed(live):
                return False
            # An atomic guard is definitive no-fill, so pending_close is
            # cleared at the rejection site.  Keep reconciliation active but
            # do not recreate the same unsafe close while its durable block
            # remains unresolved.
            if st.get("blocked_reason") == "v206_atomic_close_guard_rejected":
                return False
            if (
                st.get("pending_close") is not None
                and st.get("blocked_reason") == "v206_market_closed_close_inventory_unconfirmed"
            ):
                started = _utc((st.get("pending_close") or {}).get("started_utc"))
                st["pending_close"] = None
                if started is not None:
                    st["close_retry_after_utc"] = (
                        started + pd.Timedelta(seconds=float(self.params.get("time_close_market_closed_retry_seconds", 60.0)))
                    ).isoformat()
                self._clear_block({"v206_market_closed_close_inventory_unconfirmed"})
                self._save()
            timeout = _utc(state_pos.get("timeout_at_utc"))
            if timeout is None:
                self._block("v206_timeout_state_invalid", ticket=int(live.ticket))
                return False
            if time_actions_allowed and quote_time >= timeout:
                if st.get("pending_close"):
                    if st.get("blocked_reason") not in (None, ""):
                        return False
                    self._block("v206_close_submission_unresolved", ticket=int(live.ticket))
                    return False
                retry_after = _utc(st.get("close_retry_after_utc"))
                if retry_after is not None and quote_time < retry_after:
                    return False
                if not self._time_close_ready(info, quote_time):
                    return False
                st["pending_close"] = {
                    "reason": "timeout_30m",
                    "started_utc": quote_time.isoformat(),
                    "ticket": int(live.ticket),
                    "position_identifier": live_identifier,
                    "lot": float(state_pos["lot"]),
                }
                self._save()
                result = self.runner.executor.close_r1_position(
                    ticket=int(live.ticket), deviation=int(self.params.get("deviation_points", 50)),
                    expected_login=int(MT5_LOGIN), expected_server=str(MT5_SERVER), expected_symbol=self.symbol,
                    expected_magic=int(self.cfg["magic"]), expected_comment=str(self.cfg["comment_prefix"]),
                    expected_identifier=live_identifier,
                )
                if not result.success:
                    if result.status == "MARKET_CLOSED":
                        self._reset_time_close_state()
                        live_after = self.runner.executor.get_position(int(live.ticket))
                        if (
                            live_after is None or live_after is False
                            or int(getattr(live_after, "ticket", 0) or 0) != int(live.ticket)
                            or int(getattr(live_after, "identifier", 0) or 0) != live_identifier
                            or not self._owned(live_after)
                            or not math.isclose(float(getattr(live_after, "volume", 0.0)), float(state_pos["lot"]), rel_tol=0.0, abs_tol=1e-12)
                        ):
                            self._block("v206_market_closed_close_inventory_unconfirmed", ticket=int(live.ticket))
                            return False
                        st["pending_close"] = None
                        st["close_retry_after_utc"] = (
                            quote_time + pd.Timedelta(seconds=float(self.params.get("time_close_market_closed_retry_seconds", 60.0)))
                        ).isoformat()
                        self._log("v206_close_deferred", ticket=int(live.ticket), reason=result.status, note=result.raw_response)
                        self._save()
                        return False
                    if result.status == "TRADE_PERMISSION_GUARD":
                        st["pending_close"] = None
                        st["close_retry_after_utc"] = (quote_time + pd.Timedelta(seconds=60)).isoformat()
                        st["close_permission_reject_count"] = int(st.get("close_permission_reject_count", 0)) + 1
                        if st["close_permission_reject_count"] >= 3:
                            self._block("v206_trade_permission_rejected_repeatedly", count=st["close_permission_reject_count"])
                            return False
                        self._log("v206_close_deferred", ticket=int(live.ticket), reason=result.status, note=result.raw_response)
                        self._save()
                        return False
                    if result.status in {"ACCOUNT_MODE_GUARD", "ACCOUNT_IDENTITY_GUARD", "POSITION_OWNERSHIP_GUARD", "CLOSE_R1_POLICY_GUARD", "INVALID_REQUEST"}:
                        st["pending_close"] = None
                        self._save()
                        self._block("v206_atomic_close_guard_rejected", ticket=int(live.ticket), status=result.status)
                        return False
                    self._block("v206_timeout_close_unconfirmed", ticket=int(live.ticket), status=result.status, raw=result.raw_response)
                    return False
                if int(result.ticket) != int(live.ticket) or not math.isclose(float(result.lot), float(state_pos["lot"]), rel_tol=0.0, abs_tol=1e-12):
                    self._block("v206_close_response_identity_invalid", expected_ticket=int(live.ticket), observed_ticket=int(result.ticket))
                    return False
                st["pending_close"]["confirmed_response"] = True
                st["pending_close"]["deal"] = int(result.deal)
                st["close_permission_reject_count"] = 0
                st["close_retry_after_utc"] = None
                self._save()
                self._log("v206_close_submitted", ticket=int(live.ticket), side=state_pos.get("side"), lot=result.lot,
                          position_identifier=live_identifier, deal_id=int(result.deal),
                          entry_price=float(state_pos.get("entry_price") or 0.0), exit_price=result.close_price,
                          price=result.close_price, profit=result.profit, reason="timeout_30m", signal_bar_time=state_pos.get("signal_bar_time"), note=result.raw_response)
                return False
            return True
        direct_absence = self.runner.executor.confirm_position_absent(int(state_pos.get("ticket") or 0))
        if direct_absence is not True:
            self._block("v206_position_absence_unconfirmed", ticket=int(state_pos.get("ticket") or 0))
            return False
        opened_epoch = int(state_pos.get("open_time_epoch") or 0)
        if opened_epoch <= 0:
            self._block("v206_open_time_state_invalid", ticket=int(state_pos.get("ticket") or 0))
            return False
        opened = max(1, opened_epoch - 60)
        deal = self.runner.executor.get_position_close_deal(identifier, opened)
        if deal is False:
            # The persisted open-time window can be too narrow after broker or
            # manual close history arrives late.  Retry once with the bridge's
            # bounded default history before declaring that no deal exists.
            deal = self.runner.executor.get_position_close_deal(identifier, 0)
        if deal is None:
            self._block("v206_close_deal_query_unavailable", ticket=identifier)
            return False
        if deal is False:
            self._block("v206_close_deal_not_confirmed", ticket=identifier)
            return False
        exit_volume = float(getattr(deal, "exit_volume", 0.0) or 0.0)
        if (
            int(deal.position_id) != identifier
            or str(deal.symbol) != self.symbol
            or int(getattr(deal, "deal", 0) or 0) <= 0
            or int(getattr(deal, "deal_time", 0) or 0) < opened_epoch
            or not _finite_positive(getattr(deal, "price", 0.0))
            or not math.isfinite(float(getattr(deal, "net_profit", float("nan"))))
            or not math.isfinite(exit_volume)
            or not math.isclose(exit_volume, float(state_pos["lot"]), rel_tol=0.0, abs_tol=1e-9)
        ):
            self._block("v206_close_deal_invalid", ticket=identifier)
            return False
        reason = str((st.get("pending_close") or {}).get("reason") or "server_sl_tp_or_external_close")
        self._log("v206_close_confirmed", ticket=int(state_pos.get("ticket") or 0), position_identifier=identifier,
                  deal_id=int(deal.deal), side=state_pos.get("side"), lot=state_pos.get("lot"),
                  entry_price=float(state_pos.get("entry_price") or 0.0), exit_price=float(deal.price),
                  price=float(deal.price), profit=float(deal.net_profit), reason=reason,
                  signal_bar_time=state_pos.get("signal_bar_time"), note=f"deal={int(deal.deal)}")
        st["basket"] = []
        st["pending_close"] = None
        st["close_retry_after_utc"] = None
        st["close_permission_reject_count"] = 0
        self._reset_time_close_state()
        close_time = pd.Timestamp(int(deal.deal_time), unit="s", tz="UTC")
        st["last_closed_at_utc"] = close_time.isoformat()
        st["last_closed_side"] = state_pos.get("side")
        st["last_closed_reason"] = reason
        st["last_closed_signal_bar"] = state_pos.get("signal_bar_time")
        evaluated_bar = _utc(st.get("last_evaluated_bar"))
        st["last_consumed_signal_bar"] = (
            evaluated_bar.isoformat()
            if evaluated_bar is not None and evaluated_bar + pd.Timedelta(minutes=1) <= close_time
            else None
        )
        st["cooldown_until_utc"] = (close_time + pd.Timedelta(minutes=int(self.cfg.get("cooldown", 5)))).isoformat()
        if orders_available:
            self._clear_block()
        else:
            self._block("v206_orders_unavailable_after_confirmed_close", replace_resolved=True)
        self._save()
        return bool(orders_available)

    def reconcile_without_quote(self, cause: str = "symbol_info_unavailable") -> None:
        """Reconcile exact ownership without advancing quote-time lifecycle."""
        if not self.enabled:
            return
        prior = _utc(self.state.get("last_quote_time_utc"))
        quote_time = prior if prior is not None else pd.Timestamp(0, unit="s", tz="UTC")
        self._sync(quote_time, None, time_actions_allowed=False)
        self._block("v206_quote_clock_invalid", cause=cause)

    def _history(self) -> pd.DataFrame | None:
        requested = int(self.params.get("v206_m1_bars", 1000))
        drop_latest = bool(self.params.get("drop_latest_m1_bar", True))
        fetch_rows = requested + 1 if drop_latest else requested
        bars = self.runner.dm.get_historical_data(
            self.symbol, int(self.params.get("m1_timeframe", 1)), fetch_rows,
            str(self.params.get("broker_timezone", "UTC")), drop_latest=drop_latest,
        )
        if bars is None or len(bars) != requested:
            return None
        if not isinstance(bars.index, pd.DatetimeIndex) or bars.index.has_duplicates or not bars.index.is_monotonic_increasing:
            return None
        required = {"Open", "High", "Low", "Close"}
        if not required.issubset(bars.columns):
            return None
        prices = bars[["Open", "High", "Low", "Close"]].astype(float)
        values = prices.to_numpy()
        if (
            not bool(np.isfinite(values).all())
            or not bool((values > 0.0).all())
            or not bool((prices["High"] >= prices[["Open", "Low", "Close"]].max(axis=1)).all())
            or not bool((prices["Low"] <= prices[["Open", "High", "Close"]].min(axis=1)).all())
        ):
            return None
        return bars

    def _host_now(self) -> pd.Timestamp:
        hook = getattr(self.runner, "_v206_host_now", None)
        if callable(hook):
            observed = _utc(hook())
            if observed is not None:
                return observed
        return pd.Timestamp.now(tz="UTC")

    def _quote_clock_error(self, quote_time: pd.Timestamp) -> str | None:
        host_now = self._host_now()
        if quote_time > host_now + pd.Timedelta(seconds=QUOTE_FUTURE_TOLERANCE_SECONDS):
            return "future_quote"
        tolerance = pd.Timedelta(minutes=float(self.params.get("max_signal_delay_minutes", 2)))
        if host_now - quote_time > tolerance:
            return "stale_quote"
        previous = _utc(self.state.get("last_quote_time_utc"))
        if previous is not None and quote_time < previous:
            return "nonmonotonic_quote"
        return None

    def _broker_contract_error(self, info: Any) -> str | None:
        try:
            lot = float(self.cfg["lot"])
            volume_min = float(info.volume_min)
            volume_max = float(info.volume_max)
            volume_step = float(info.volume_step)
            point = float(info.point)
            digits = int(info.digits)
            values = (lot, volume_min, volume_max, volume_step, point)
            if not all(math.isfinite(value) for value in values):
                return "nonfinite"
            if volume_min <= 0.0 or volume_max < volume_min or volume_step <= 0.0:
                return "volume_contract_invalid"
            if lot < volume_min - 1e-12 or lot > volume_max + 1e-12:
                return "lot_out_of_range"
            if not math.isclose((lot - volume_min) / volume_step, round((lot - volume_min) / volume_step), rel_tol=0.0, abs_tol=1e-9):
                return "lot_off_step"
            if digits != int(self.params.get("price_digits", 3)):
                return "digits_mismatch"
            if not math.isclose(point, float(self.params.get("point_size", 0.001)), rel_tol=0.0, abs_tol=1e-12):
                return "point_mismatch"
        except (AttributeError, TypeError, ValueError, OverflowError):
            return "metadata_unavailable"
        return None

    def _pending_signal_error(self, pending: Any) -> str | None:
        if not isinstance(pending, dict):
            return "not_object"
        side = str(pending.get("side") or "")
        signal_bar = _utc(pending.get("signal_bar_time"))
        due = _utc(pending.get("entry_due_utc"))
        expiry = _utc(pending.get("entry_expiry_utc"))
        retry_after = _utc(pending.get("retry_after_utc"))
        expected_id = f"v206:{signal_bar.isoformat()}:{side}" if signal_bar is not None else ""
        if (
            not isinstance(pending.get("side"), str)
            or side not in {"LONG", "SHORT"}
            or not all(isinstance(pending.get(key), str) for key in ("signal_bar_time", "entry_due_utc", "entry_expiry_utc"))
            or signal_bar is None or due is None or expiry is None
            or due != signal_bar + pd.Timedelta(minutes=1)
            or expiry != due + pd.Timedelta(minutes=float(self.params.get("max_signal_delay_minutes", 2)))
            or str(pending.get("opportunity_id") or "") != expected_id
            or not _finite_positive(pending.get("fixed_stop"))
            or (
                pending.get("retry_after_utc") is not None
                and (retry_after is None or due is None or expiry is None or retry_after < due or retry_after > expiry)
            )
        ):
            return "identity_or_clock_invalid"
        return None

    def _attempt_pending_signal(self, info: Any, quote_time: pd.Timestamp) -> None:
        st = self.state
        pending_signal = st.get("pending_signal")
        try:
            error = self._pending_signal_error(pending_signal)
        except (TypeError, ValueError, OverflowError):
            error = "value_parse_error"
        if error is not None:
            self._block("v206_pending_signal_state_invalid", cause=error)
            return
        expiry = _utc(pending_signal["entry_expiry_utc"])
        due = _utc(pending_signal["entry_due_utc"])
        if expiry is None or due is None:
            self._block("v206_pending_signal_state_invalid", cause="clock_missing")
            return
        if quote_time < due:
            return
        retry_after = _utc(pending_signal.get("retry_after_utc"))
        if retry_after is not None and quote_time < retry_after:
            return
        last_close = _utc(st.get("last_closed_at_utc"))
        if (
            last_close is not None
            and st.get("last_closed_side") == pending_signal.get("side")
            and due <= last_close
        ):
            signal_bar = _utc(pending_signal.get("signal_bar_time"))
            st["last_consumed_signal_bar"] = (
                signal_bar.isoformat() if signal_bar is not None else st.get("last_consumed_signal_bar")
            )
            st["last_decision"] = {
                "signal_bar_time": pending_signal["signal_bar_time"],
                "outcome": "known_same_direction_signal_after_close",
            }
            self._log(
                "v206_entry_skip",
                reason="known_same_direction_signal_after_close",
                signal_bar_time=pending_signal["signal_bar_time"],
            )
            st["pending_signal"] = None
            self._save()
            return
        if quote_time > expiry:
            self._log("v206_entry_skip", reason="signal_expired", signal_bar_time=pending_signal["signal_bar_time"])
            st["last_decision"] = {"signal_bar_time": pending_signal["signal_bar_time"], "outcome": "signal_expired"}
            st["pending_signal"] = None
            self._save()
            return
        cooldown = _utc(st.get("cooldown_until_utc"))
        if cooldown is not None and quote_time < cooldown:
            self._log("v206_entry_deferred", reason="cooldown", signal_bar_time=pending_signal["signal_bar_time"])
            return
        point = float(self.params.get("point_size", 0.001))
        bid = float(info.bid)
        ask = float(info.ask)
        if not all(math.isfinite(value) and value > 0.0 for value in (point, bid, ask)) or ask < bid:
            self._block("v206_quote_values_invalid")
            return
        spread = (ask - bid) / point
        if spread > float(self.params.get("max_entry_spread_points", 300.0)):
            self._log("v206_entry_deferred", reason="spread_guard", signal_bar_time=pending_signal["signal_bar_time"])
            return
        entry_quote = ask if pending_signal["side"] == "LONG" else bid
        fixed_stop = float(pending_signal["fixed_stop"])
        if (pending_signal["side"] == "LONG" and fixed_stop >= entry_quote) or (pending_signal["side"] == "SHORT" and fixed_stop <= entry_quote):
            self._log("v206_entry_skip", reason="invalid_stop_at_execution_quote", signal_bar_time=pending_signal["signal_bar_time"])
            st["pending_signal"] = None
            self._save()
            return
        if not self.runner.live_enabled:
            self._log("v206_shadow_signal", side=pending_signal["side"], price=entry_quote, signal_bar_time=pending_signal["signal_bar_time"], note="no_order_submitted")
            st["last_decision"] = {"signal_bar_time": pending_signal["signal_bar_time"], "outcome": "shadow_signal"}
            st["pending_signal"] = None
            self._save()
            return
        # Re-read the quote and symbol contract at the actual submission boundary.
        # The signal-evaluation quote may already be stale by the time history and
        # inventory checks have completed.
        fresh_info = self.runner.executor.get_symbol_info(self.symbol)
        if fresh_info is None:
            self._block("v206_pre_open_symbol_info_unavailable")
            return
        try:
            fresh_quote_time = pd.Timestamp(int(fresh_info.quote_time_msc), unit="ms", tz="UTC")
            fresh_quote_error = self._quote_clock_error(fresh_quote_time)
            point = float(self.params.get("point_size", 0.001))
            bid = float(fresh_info.bid)
            ask = float(fresh_info.ask)
        except (AttributeError, TypeError, ValueError, OverflowError):
            self._block("v206_pre_open_quote_invalid")
            return
        if fresh_quote_error is not None:
            self._block("v206_pre_open_quote_clock_invalid", cause=fresh_quote_error)
            return
        if fresh_quote_time < due or fresh_quote_time > expiry:
            self._log("v206_entry_skip", reason="signal_expired_at_submit_boundary", signal_bar_time=pending_signal["signal_bar_time"])
            st["last_decision"] = {"signal_bar_time": pending_signal["signal_bar_time"], "outcome": "signal_expired_at_submit_boundary"}
            st["pending_signal"] = None
            self._save()
            return
        if not all(math.isfinite(value) and value > 0.0 for value in (point, bid, ask)) or ask < bid:
            self._block("v206_pre_open_quote_invalid")
            return
        spread = (ask - bid) / point
        if spread > float(self.params.get("max_entry_spread_points", 300.0)):
            self._log("v206_entry_deferred", reason="spread_guard_at_submit_boundary", signal_bar_time=pending_signal["signal_bar_time"])
            return
        entry_quote = ask if pending_signal["side"] == "LONG" else bid
        if (pending_signal["side"] == "LONG" and fixed_stop >= entry_quote) or (pending_signal["side"] == "SHORT" and fixed_stop <= entry_quote):
            self._log("v206_entry_skip", reason="invalid_stop_at_submit_boundary", signal_bar_time=pending_signal["signal_bar_time"])
            st["pending_signal"] = None
            self._save()
            return
        info = fresh_info
        quote_time = fresh_quote_time
        self.state["last_quote_time_utc"] = quote_time.isoformat()
        contract_error = self._broker_contract_error(info)
        if contract_error is not None:
            self._block("v206_broker_entry_contract_invalid", cause=contract_error)
            return
        pending = {
            **pending_signal,
            "started_utc": quote_time.isoformat(),
            "flat_confirmations": 0,
            "lot": float(self.cfg["lot"]),
            "owner_symbol": self.symbol,
            "owner_magic": int(self.cfg["magic"]),
            "owner_comment": str(self.cfg["comment_prefix"]),
        }
        st["pending_signal"] = None
        st["pending_open"] = pending
        self._save()
        result = self.runner.executor.open_r1_position(
            self.symbol, ORDER_TYPE_BUY if pending["side"] == "LONG" else ORDER_TYPE_SELL,
            float(self.cfg["lot"]), fixed_stop, deviation=int(self.params.get("deviation_points", 50)),
            magic=int(self.cfg["magic"]), comment=str(self.cfg["comment_prefix"]), digits=int(self.params.get("price_digits", 3)),
            expected_login=int(MT5_LOGIN), expected_server=str(MT5_SERVER), expected_owned_positions=0,
        )
        positions, orders = self._inventory()
        if positions is None or orders is None:
            self._block("v206_post_open_inventory_unavailable", status=result.status)
            return
        if any(not self._owned(record) for record in positions + orders):
            self._block("v206_namespace_contains_foreign_inventory")
            return
        position_tickets = [int(getattr(row, "ticket", 0) or 0) for row in positions]
        position_ids = [int(getattr(row, "identifier", 0) or 0) for row in positions]
        order_tickets = [int(getattr(row, "ticket", 0) or 0) for row in orders]
        if (
            any(value <= 0 for value in position_tickets + position_ids + order_tickets)
            or len(position_tickets) != len(set(position_tickets))
            or len(position_ids) != len(set(position_ids))
            or len(order_tickets) != len(set(order_tickets))
        ):
            self._block("v206_post_open_inventory_identity_invalid", positions=len(positions), orders=len(orders))
            return
        if result.status == "NO_FILL":
            if positions or orders:
                self._block("v206_definitive_no_fill_with_inventory", positions=len(positions), orders=len(orders))
                return
            st["pending_open"] = None
            retry_reason = (
                "market_closed" if result.reason == "RETCODE_10018" else
                "trade_permission" if result.reason in {"RETCODE_10026", "RETCODE_10027", "TRADE_PERMISSION_GUARD"} else
                None
            )
            if result.reason in NONRECOVERABLE_OPEN_GUARD_REASONS:
                st["pending_signal"] = None
                st["entry_permission_reject_count"] = 0
                self._block(
                    "v206_open_atomic_guard_rejected",
                    guard_reason=result.reason,
                    opportunity_id=pending["opportunity_id"],
                )
                self._save()
                return
            expiry = _utc(pending.get("entry_expiry_utc"))
            retry_at = quote_time + pd.Timedelta(seconds=60)
            if retry_reason == "trade_permission":
                st["entry_permission_reject_count"] = int(st.get("entry_permission_reject_count", 0)) + 1
            elif retry_reason is not None:
                st["entry_permission_reject_count"] = 0
            if retry_reason == "trade_permission" and st["entry_permission_reject_count"] >= 3:
                st["pending_signal"] = None
                self._block("v206_entry_trade_permission_rejected_repeatedly", count=st["entry_permission_reject_count"])
                self._save()
                return
            if retry_reason is not None and expiry is not None and retry_at <= expiry:
                st["pending_signal"] = {
                    "opportunity_id": pending["opportunity_id"],
                    "side": pending["side"],
                    "signal_bar_time": pending["signal_bar_time"],
                    "entry_due_utc": pending["entry_due_utc"],
                    "entry_expiry_utc": pending["entry_expiry_utc"],
                    "fixed_stop": pending["fixed_stop"],
                    "retry_after_utc": retry_at.isoformat(),
                }
                st["last_decision"] = {"signal_bar_time": pending["signal_bar_time"], "outcome": "entry_deferred", "reason": retry_reason}
                self._log("v206_entry_deferred", side=pending["side"], reason=retry_reason, signal_bar_time=pending["signal_bar_time"], note=result.raw_response)
            else:
                st["entry_permission_reject_count"] = 0
                st["last_decision"] = {"signal_bar_time": pending["signal_bar_time"], "outcome": "entry_rejected", "reason": result.reason}
                self._log("v206_entry_rejected", side=pending["side"], reason=result.reason, signal_bar_time=pending["signal_bar_time"], note=result.raw_response)
            self._save()
            return
        if result.status in {"CONFIRMED", "REPAIR_REQUIRED"}:
            if len(positions) != 1 or orders:
                self._block("v206_post_open_inventory_delta_invalid", positions=len(positions), orders=len(orders))
                return
            matching = [row for row in positions if int(row.ticket) == int(result.ticket) and int(getattr(row, "identifier", 0) or 0) == int(result.identifier)]
            if len(matching) != 1:
                self._block("v206_post_open_returned_position_missing", ticket=int(result.ticket))
                return
            self._adopt(matching[0], pending)
            if self.state.get("basket") and not self.state.get("blocked_reason"):
                st["entry_permission_reject_count"] = 0
            if result.status == "CONFIRMED" and not self.state.get("blocked_reason"):
                st["last_decision"] = {"signal_bar_time": pending["signal_bar_time"], "outcome": "entry_confirmed"}
                self._log("v206_entry_confirmed", opportunity_id=pending["opportunity_id"], ticket=result.ticket,
                          position_identifier=result.identifier, deal_id=result.deal, side=pending["side"],
                          lot=float(self.cfg["lot"]), entry_price=result.fill, price=result.fill,
                          signal_bar_time=pending["signal_bar_time"], executable_at=datetime.fromtimestamp(result.open_time, UTC).isoformat(),
                          note=result.raw_response)
                self._save()
            return
        self._block("v206_open_response_ambiguous", raw=result.raw_response, opportunity_id=pending["opportunity_id"])

    def run_once(self, info: Any) -> None:
        if not self.enabled:
            return
        quote_msc = getattr(info, "quote_time_msc", None)
        if quote_msc is None:
            self.reconcile_without_quote("missing_quote_time")
            return
        try:
            quote_time = pd.Timestamp(int(quote_msc), unit="ms", tz="UTC")
        except Exception:
            self.reconcile_without_quote("invalid_quote_time")
            return
        quote_error = self._quote_clock_error(quote_time)
        if quote_error is None:
            self.state["last_quote_time_utc"] = quote_time.isoformat()
        if not self._sync(quote_time, info, time_actions_allowed=quote_error is None):
            return
        if quote_error is None:
            self._clear_block({"v206_quote_clock_invalid"})
        st = self.state
        if st.get("basket") or st.get("pending_open") or st.get("blocked_reason"):
            return
        if not (
            bool(self.params.get("enabled", True))
            and bool(self.params.get("v206_enabled", False))
            and bool(self.cfg.get("enabled", True))
        ):
            return
        if quote_error is not None:
            self._block("v206_quote_clock_invalid", cause=quote_error)
            return
        if st.get("pending_signal"):
            self._attempt_pending_signal(info, quote_time)
            return
        quote_minute = quote_time.floor("min").isoformat()
        if st.get("last_history_fetch_minute") == quote_minute:
            return
        st["last_history_fetch_minute"] = quote_minute
        self._save()
        bars = self._history()
        if bars is None or bars.empty:
            if st.get("last_history_unavailable_minute") != quote_minute:
                st["last_history_unavailable_minute"] = quote_minute
                self._log("v206_entry_skip", reason="m1_bars_unavailable")
                signal_bar = quote_time.floor("min") - pd.Timedelta(minutes=1)
                st["last_decision"] = {
                    "signal_bar_time": signal_bar.isoformat(),
                    "outcome": "not_evaluated_data_unavailable",
                    "reason": "m1_bars_unavailable",
                    "side": None,
                }
                self._log(
                    "v206_strategy_decision",
                    reason="m1_bars_unavailable",
                    signal_bar_time=signal_bar.isoformat(),
                    note="outcome=not_evaluated_data_unavailable",
                )
                self._save()
            return
        st["last_history_unavailable_minute"] = None
        signal_bar = _utc(bars.index[-1])
        if signal_bar is None:
            self._block("v206_signal_bar_invalid")
            return
        signal_bar_text = signal_bar.isoformat()
        if st.get("last_evaluated_bar") == signal_bar_text:
            return
        signal = latest_v206_signal(bars)
        st["last_evaluated_bar"] = signal_bar_text
        if signal is None:
            st["last_decision"] = {
                "signal_bar_time": signal_bar_text,
                "outcome": "no_signal",
                "reason": "signal_conditions_not_met",
                "side": None,
            }
            self._save()
            return
        due = signal.signal_bar_time + pd.Timedelta(minutes=1)
        latest = due + pd.Timedelta(minutes=float(self.params.get("max_signal_delay_minutes", 2)))
        if quote_time < due or quote_time > latest:
            self._log("v206_entry_skip", reason="stale_or_not_yet_executable", signal_bar_time=signal_bar_text)
            st["last_decision"] = {"signal_bar_time": signal_bar_text, "outcome": "signal_outside_window"}
            self._save()
            return
        st["pending_signal"] = {
            "opportunity_id": f"v206:{signal_bar_text}:{signal.side}",
            "side": signal.side,
            "signal_bar_time": signal_bar_text,
            "entry_due_utc": due.isoformat(),
            "entry_expiry_utc": latest.isoformat(),
            "fixed_stop": float(signal.stop),
        }
        st["last_decision"] = {"signal_bar_time": signal_bar_text, "outcome": "signal_pending_execution"}
        self._save()
        self._attempt_pending_signal(info, quote_time)
