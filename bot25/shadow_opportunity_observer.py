# -*- coding: utf-8 -*-
"""Execution-independent frontier opportunity and markout observer for bot25."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from passive_evidence_io import (
    append_durable_csv,
    atomic_write_json,
    csv_rows,
    dt_text,
    load_json,
    utc_datetime,
)

UTC = timezone.utc
OBSERVER_VERSION = "s25_v23_shadow_opportunity_observer_v1"
STATE_VERSION = 1

OPPORTUNITY_FIELDS = [
    "timestamp_utc", "event", "observer_version", "opportunity_id", "symbol",
    "opportunity_type", "side", "signal_bar_time", "registered_at",
    "entry_bid", "entry_ask", "spread_price", "spread_points", "lot",
    "contract_size", "episode_id", "active_wave", "atr14", "ema200",
    "ema_distance_atr", "frontier", "frontier_distance_atr",
    "long_positions", "short_positions", "side_imbalance",
    "episode_age_minutes", "minutes_since_productive_close",
    "inventory_mtm_usd", "core_positions", "satellite_positions",
    "capacity_allowed", "ratio_allowed", "v23_allowed", "execution_allowed",
    "route_status", "route_reason", "note",
]

MARKOUT_FIELDS = [
    "timestamp_utc", "event", "observer_version", "opportunity_id", "symbol",
    "opportunity_type", "side", "horizon_minutes", "registered_at", "due_at",
    "observed_at", "observation_delay_seconds", "entry_bid", "entry_ask",
    "exit_bid", "exit_ask", "pnl_price", "pnl_usd", "mfe_price", "mae_price",
    "mfe_usd", "mae_usd", "quote_samples", "route_status", "route_reason", "note",
]


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _pnl_price(side: str, entry_bid: float, entry_ask: float, bid: float, ask: float) -> float:
    if side == "LONG":
        return bid - entry_ask
    if side == "SHORT":
        return entry_bid - ask
    raise ValueError(f"unsupported side: {side}")


class S25ShadowOpportunityObserver:
    """Persist counterfactual add evidence without interacting with execution."""

    def __init__(self, cfg: dict[str, Any], *, log_dir: str, state_dir: str):
        self.enabled = bool(cfg.get("enabled", False))
        self.horizons = tuple(sorted({int(value) for value in cfg.get("horizons_minutes", [1, 5, 15, 30, 60, 120])}))
        if not self.horizons or any(value <= 0 for value in self.horizons):
            raise ValueError("observer horizons must be positive")
        self.retention_days = max(1, int(cfg.get("completed_id_retention_days", 14)))
        self.opportunity_path = Path(log_dir) / str(cfg.get("opportunity_csv", "s25_shadow_opportunities.csv"))
        self.markout_path = Path(log_dir) / str(cfg.get("markout_csv", "s25_shadow_markouts.csv"))
        self.state_path = Path(state_dir) / str(cfg.get("state_file", "s25_shadow_observer_state.json"))
        raw = load_json(self.state_path, {"version": STATE_VERSION, "pending": {}, "completed": {}})
        if int(raw.get("version", 0)) != STATE_VERSION:
            raise ValueError("unsupported s25 shadow observer state version")
        pending = raw.get("pending", {})
        completed = raw.get("completed", {})
        if not isinstance(pending, dict) or not isinstance(completed, dict):
            raise ValueError("invalid s25 shadow observer state")
        self.pending: dict[str, dict[str, Any]] = pending
        self.completed: dict[str, str] = {str(key): str(value) for key, value in completed.items()}
        opportunity_rows = csv_rows(self.opportunity_path, OPPORTUNITY_FIELDS)
        self.registration_ids = {
            str(row.get("opportunity_id") or "")
            for row in opportunity_rows
            if row.get("event") == "registered" and row.get("opportunity_id")
        }
        self.route_keys = {
            (str(row.get("opportunity_id") or ""), str(row.get("route_status") or ""), str(row.get("route_reason") or ""))
            for row in opportunity_rows
            if row.get("event") == "route_update" and row.get("opportunity_id")
        }
        self.markout_keys = {
            (str(row.get("opportunity_id") or ""), int(row.get("horizon_minutes") or 0))
            for row in csv_rows(self.markout_path, MARKOUT_FIELDS)
            if row.get("event") == "markout" and row.get("opportunity_id")
        }
        self._reconcile_csv_identity()
        self._save()

    def _reconcile_csv_identity(self) -> None:
        for opportunity_id in list(self.pending):
            item = self.pending[opportunity_id]
            if opportunity_id not in self.registration_ids:
                append_durable_csv(
                    self.opportunity_path,
                    self._opportunity_row(item, event="registered", at=item["registered_at"]),
                    OPPORTUNITY_FIELDS,
                )
                self.registration_ids.add(opportunity_id)
            route_status = str(item.get("route_status") or "registered")
            route_reason = str(item.get("route_reason") or "")
            route_key = (opportunity_id, route_status, route_reason)
            if route_status != "registered" and route_key not in self.route_keys:
                append_durable_csv(
                    self.opportunity_path,
                    self._opportunity_row(item, event="route_update", at=item.get("route_updated_at") or item["registered_at"]),
                    OPPORTUNITY_FIELDS,
                )
                self.route_keys.add(route_key)
            emitted = {int(value) for value in item.get("emitted_horizons", [])}
            emitted.update(h for oid, h in self.markout_keys if oid == opportunity_id)
            item["emitted_horizons"] = sorted(emitted)
            if all(horizon in emitted for horizon in self.horizons):
                self.completed[opportunity_id] = str(item.get("last_observed_at") or item["registered_at"])
                self.pending.pop(opportunity_id, None)

    def _save(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
        retained: dict[str, str] = {}
        for key, value in self.completed.items():
            try:
                if utc_datetime(value) >= cutoff:
                    retained[key] = value
            except (TypeError, ValueError):
                continue
        self.completed = retained
        atomic_write_json(
            self.state_path,
            {"version": STATE_VERSION, "observer_version": OBSERVER_VERSION, "pending": self.pending, "completed": self.completed},
        )

    @staticmethod
    def _opportunity_row(item: dict[str, Any], *, event: str, at: Any) -> dict[str, Any]:
        row = {field: item.get(field, "") for field in OPPORTUNITY_FIELDS}
        row.update({
            "timestamp_utc": dt_text(at),
            "event": event,
            "observer_version": OBSERVER_VERSION,
            "route_status": item.get("route_status", "registered"),
            "route_reason": item.get("route_reason", ""),
        })
        return row

    def register_opportunity(self, payload: dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        opportunity_id = str(payload.get("opportunity_id") or "")
        side = str(payload.get("side") or "")
        if not opportunity_id or side not in {"LONG", "SHORT"}:
            raise ValueError("opportunity_id and LONG/SHORT side are required")
        if opportunity_id in self.pending or opportunity_id in self.completed or opportunity_id in self.registration_ids:
            return False
        registered_at = dt_text(payload.get("registered_at") or datetime.now(UTC))
        entry_bid = _finite(payload.get("entry_bid"))
        entry_ask = _finite(payload.get("entry_ask"))
        initial = _pnl_price(side, entry_bid, entry_ask, entry_bid, entry_ask)
        item = {field: payload.get(field, "") for field in OPPORTUNITY_FIELDS}
        item.update({
            "opportunity_id": opportunity_id,
            "side": side,
            "registered_at": registered_at,
            "entry_bid": entry_bid,
            "entry_ask": entry_ask,
            "route_status": "registered",
            "route_reason": "",
            "emitted_horizons": [],
            "mfe_price": initial,
            "mae_price": initial,
            "quote_samples": 1,
            "last_observed_at": registered_at,
        })
        self.pending[opportunity_id] = item
        self._save()
        append_durable_csv(self.opportunity_path, self._opportunity_row(item, event="registered", at=registered_at), OPPORTUNITY_FIELDS)
        self.registration_ids.add(opportunity_id)
        return True

    def record_route(self, opportunity_id: str, *, status: str, reason: str, at: Any) -> bool:
        if not self.enabled:
            return False
        item = self.pending.get(str(opportunity_id))
        if item is None:
            return False
        item["route_status"] = str(status)
        item["route_reason"] = str(reason)
        item["route_updated_at"] = dt_text(at)
        self._save()
        key = (str(opportunity_id), str(status), str(reason))
        if key not in self.route_keys:
            append_durable_csv(self.opportunity_path, self._opportunity_row(item, event="route_update", at=at), OPPORTUNITY_FIELDS)
            self.route_keys.add(key)
        return True

    def observe_quote(self, *, at: Any, bid: float, ask: float) -> int:
        if not self.enabled or not self.pending:
            return 0
        observed_at = utc_datetime(at)
        bid_value = _finite(bid)
        ask_value = _finite(ask)
        emitted_count = 0
        completed_ids: list[str] = []
        for opportunity_id, item in list(self.pending.items()):
            side = str(item["side"])
            pnl_price = _pnl_price(side, _finite(item["entry_bid"]), _finite(item["entry_ask"]), bid_value, ask_value)
            item["mfe_price"] = max(_finite(item.get("mfe_price"), pnl_price), pnl_price)
            item["mae_price"] = min(_finite(item.get("mae_price"), pnl_price), pnl_price)
            item["quote_samples"] = int(item.get("quote_samples", 0)) + 1
            item["last_observed_at"] = dt_text(observed_at)
            registered = utc_datetime(item["registered_at"])
            emitted = {int(value) for value in item.get("emitted_horizons", [])}
            lot = _finite(item.get("lot"), 0.01)
            contract_size = _finite(item.get("contract_size"), 100.0)
            for horizon in self.horizons:
                if horizon in emitted:
                    continue
                due = registered + timedelta(minutes=horizon)
                if observed_at < due:
                    continue
                key = (opportunity_id, horizon)
                if key not in self.markout_keys:
                    row = {
                        "timestamp_utc": dt_text(observed_at),
                        "event": "markout",
                        "observer_version": OBSERVER_VERSION,
                        "opportunity_id": opportunity_id,
                        "symbol": item.get("symbol", ""),
                        "opportunity_type": item.get("opportunity_type", ""),
                        "side": side,
                        "horizon_minutes": horizon,
                        "registered_at": item["registered_at"],
                        "due_at": dt_text(due),
                        "observed_at": dt_text(observed_at),
                        "observation_delay_seconds": round(max(0.0, (observed_at - due).total_seconds()), 3),
                        "entry_bid": item["entry_bid"],
                        "entry_ask": item["entry_ask"],
                        "exit_bid": bid_value,
                        "exit_ask": ask_value,
                        "pnl_price": pnl_price,
                        "pnl_usd": pnl_price * contract_size * lot,
                        "mfe_price": item["mfe_price"],
                        "mae_price": item["mae_price"],
                        "mfe_usd": _finite(item["mfe_price"]) * contract_size * lot,
                        "mae_usd": _finite(item["mae_price"]) * contract_size * lot,
                        "quote_samples": item["quote_samples"],
                        "route_status": item.get("route_status", ""),
                        "route_reason": item.get("route_reason", ""),
                        "note": "",
                    }
                    append_durable_csv(self.markout_path, row, MARKOUT_FIELDS)
                    self.markout_keys.add(key)
                    emitted_count += 1
                emitted.add(horizon)
            item["emitted_horizons"] = sorted(emitted)
            if all(horizon in emitted for horizon in self.horizons):
                completed_ids.append(opportunity_id)
        for opportunity_id in completed_ids:
            item = self.pending.pop(opportunity_id)
            self.completed[opportunity_id] = str(item.get("last_observed_at") or item["registered_at"])
        self._save()
        return emitted_count


__all__ = [
    "MARKOUT_FIELDS",
    "OBSERVER_VERSION",
    "OPPORTUNITY_FIELDS",
    "S25ShadowOpportunityObserver",
]
