# -*- coding: utf-8 -*-
"""Passive bot23 forward observer.

This module has no broker, executor, bridge, or order dependency.  It observes
quotes already obtained by the runner and records counterfactual executable
markouts for confirmed ZA opportunities.
"""

from __future__ import annotations

import csv
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


UTC = timezone.utc
OBSERVER_VERSION = "s27_shadow_opportunity_observer_v1"
STATE_VERSION = 1

OPPORTUNITY_FIELDS = [
    "timestamp_utc",
    "event",
    "observer_version",
    "opportunity_id",
    "symbol",
    "raw_side",
    "effective_side",
    "markout_side",
    "markout_basis",
    "entry_policy_id",
    "entry_policy_action",
    "entry_policy_reason",
    "event_time",
    "release_time",
    "decision_time",
    "entry_bid",
    "entry_ask",
    "lot",
    "spread_price",
    "spread_points",
    "atr30",
    "ret10",
    "vol_ratio",
    "portfolio_positions",
    "long_positions",
    "short_positions",
    "lane_positions_json",
    "lane_pending_json",
    "lane_readiness_json",
    "route_status",
    "consumed_lane_id",
    "route_reason",
]

MARKOUT_FIELDS = [
    "timestamp_utc",
    "observer_version",
    "opportunity_id",
    "symbol",
    "raw_side",
    "effective_side",
    "markout_side",
    "markout_basis",
    "horizon_minutes",
    "registered_at",
    "due_at",
    "observed_at",
    "observation_delay_seconds",
    "entry_bid",
    "entry_ask",
    "lot",
    "exit_bid",
    "exit_ask",
    "pnl_price",
    "pnl_usd",
    "mfe_price",
    "mae_price",
    "mfe_usd",
    "mae_usd",
    "quote_samples",
    "route_status",
    "consumed_lane_id",
    "route_reason",
]


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        result = datetime.fromisoformat(text)
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _dt_text(value: Any) -> str:
    return _utc(value).isoformat()


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _validate_header(path: Path, fields: list[str]) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open("r", newline="", encoding="utf-8") as handle:
        observed = next(csv.reader(handle), [])
    if observed != fields:
        raise RuntimeError(f"shadow observer CSV schema mismatch: {path}")


def _append_csv(path: Path, row: dict[str, Any], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    _validate_header(path, fields)
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in fields})


class ShadowOpportunityObserver:
    """Persist compact forward markouts without interacting with execution."""

    def __init__(
        self,
        config: dict[str, Any] | None,
        *,
        log_dir: str | os.PathLike[str],
        state_dir: str | os.PathLike[str],
        symbol: str,
        contract_size: float,
        lot: float,
    ) -> None:
        cfg = dict(config or {})
        self.enabled = bool(cfg.get("enabled", False))
        self.symbol = str(symbol)
        self.contract_size = float(contract_size)
        self.lot = float(lot)
        self.horizons = tuple(sorted({int(value) for value in cfg.get("horizons_minutes", [1, 5, 15, 30, 60])}))
        if not self.horizons or any(value <= 0 for value in self.horizons):
            raise ValueError("shadow observer horizons must be positive")
        self.retention_days = max(1, int(cfg.get("completed_id_retention_days", 14)))
        self.opportunity_path = Path(log_dir) / str(cfg.get("opportunity_csv", "s23_shadow_opportunities.csv"))
        self.markout_path = Path(log_dir) / str(cfg.get("markout_csv", "s23_shadow_markouts.csv"))
        self.state_path = Path(state_dir) / str(cfg.get("state_file", "s23_shadow_observer_state.json"))
        self.state: dict[str, Any] = self._load_state()
        self._registration_ids, self._route_keys = self._read_opportunity_keys()
        self._markout_keys = self._read_markout_keys()
        if self.enabled:
            self._reconcile_evidence_rows()

    def _default_state(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "observer_version": OBSERVER_VERSION,
            "updated_at_utc": None,
            "pending": {},
            "completed": {},
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.enabled or not self.state_path.exists():
            return self._default_state()
        with self.state_path.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
        if int(state.get("version", -1)) != STATE_VERSION:
            raise RuntimeError("shadow observer state version mismatch")
        if str(state.get("observer_version")) != OBSERVER_VERSION:
            raise RuntimeError("shadow observer identity mismatch")
        if not isinstance(state.get("pending"), dict) or not isinstance(state.get("completed"), dict):
            raise RuntimeError("shadow observer state shape mismatch")
        return state

    def _save(self, at: Any) -> None:
        if not self.enabled:
            return
        now = _utc(at)
        cutoff = now.timestamp() - self.retention_days * 86400
        completed = self.state["completed"]
        self.state["completed"] = {
            key: value
            for key, value in completed.items()
            if _utc(value).timestamp() >= cutoff
        }
        self.state["updated_at_utc"] = _dt_text(now)
        _atomic_json(self.state_path, self.state)

    def _read_opportunity_keys(self) -> tuple[set[str], set[tuple[str, str, str]]]:
        registrations: set[str] = set()
        routes: set[tuple[str, str, str]] = set()
        if not self.opportunity_path.exists() or self.opportunity_path.stat().st_size == 0:
            return registrations, routes
        _validate_header(self.opportunity_path, OPPORTUNITY_FIELDS)
        with self.opportunity_path.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                opportunity_id = str(row.get("opportunity_id") or "")
                if row.get("event") == "registered":
                    registrations.add(opportunity_id)
                elif row.get("event") == "route_update":
                    routes.add((opportunity_id, str(row.get("route_status") or ""), str(row.get("consumed_lane_id") or "")))
        return registrations, routes

    def _read_markout_keys(self) -> set[tuple[str, int]]:
        keys: set[tuple[str, int]] = set()
        if not self.markout_path.exists() or self.markout_path.stat().st_size == 0:
            return keys
        _validate_header(self.markout_path, MARKOUT_FIELDS)
        with self.markout_path.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    keys.add((str(row["opportunity_id"]), int(row["horizon_minutes"])))
                except (KeyError, TypeError, ValueError):
                    continue
        return keys

    def _registration_row(self, item: dict[str, Any], *, at: Any) -> dict[str, Any]:
        context = dict(item.get("context") or {})
        policy = dict(item.get("entry_policy") or {})
        return {
            "timestamp_utc": _dt_text(at),
            "event": "registered",
            "observer_version": OBSERVER_VERSION,
            "opportunity_id": item["opportunity_id"],
            "symbol": item["symbol"],
            "raw_side": item["raw_side"],
            "effective_side": item["effective_side"],
            "markout_side": item["markout_side"],
            "markout_basis": item["markout_basis"],
            "entry_policy_id": policy.get("policy_id", ""),
            "entry_policy_action": policy.get("action", ""),
            "entry_policy_reason": policy.get("reason", ""),
            "event_time": item.get("event_time", ""),
            "release_time": item.get("release_time", ""),
            "decision_time": item.get("decision_time", ""),
            "entry_bid": item["entry_bid"],
            "entry_ask": item["entry_ask"],
            "lot": item["lot"],
            "spread_price": item["entry_ask"] - item["entry_bid"],
            "spread_points": context.get("spread_points", ""),
            "atr30": context.get("atr30", ""),
            "ret10": context.get("ret10", ""),
            "vol_ratio": context.get("vol_ratio", ""),
            "portfolio_positions": context.get("portfolio_positions", ""),
            "long_positions": context.get("long_positions", ""),
            "short_positions": context.get("short_positions", ""),
            "lane_positions_json": json.dumps(context.get("lane_positions", {}), sort_keys=True),
            "lane_pending_json": json.dumps(context.get("lane_pending", {}), sort_keys=True),
            "lane_readiness_json": json.dumps(context.get("lane_readiness", {}), sort_keys=True),
            "route_status": item.get("route_status", "registered"),
            "consumed_lane_id": item.get("consumed_lane_id", ""),
            "route_reason": item.get("route_reason", ""),
        }

    def _route_row(self, item: dict[str, Any], *, at: Any) -> dict[str, Any]:
        row = self._registration_row(item, at=at)
        row["event"] = "route_update"
        row["route_status"] = item.get("route_status", "")
        row["consumed_lane_id"] = item.get("consumed_lane_id", "")
        row["route_reason"] = item.get("route_reason", "")
        return row

    def _reconcile_evidence_rows(self) -> None:
        changed = False
        for opportunity_id, item in self.state["pending"].items():
            if opportunity_id not in self._registration_ids:
                _append_csv(self.opportunity_path, self._registration_row(item, at=item["registered_at"]), OPPORTUNITY_FIELDS)
                self._registration_ids.add(opportunity_id)
            status = str(item.get("route_status") or "registered")
            lane = str(item.get("consumed_lane_id") or "")
            route_key = (opportunity_id, status, lane)
            if status != "registered" and route_key not in self._route_keys:
                _append_csv(self.opportunity_path, self._route_row(item, at=item.get("route_updated_at") or item["registered_at"]), OPPORTUNITY_FIELDS)
                self._route_keys.add(route_key)
            emitted = {int(value) for value in item.get("emitted_horizons", [])}
            for horizon in self.horizons:
                if (opportunity_id, horizon) in self._markout_keys and horizon not in emitted:
                    emitted.add(horizon)
                    changed = True
            item["emitted_horizons"] = sorted(emitted)
        if changed:
            self._save(datetime.now(UTC))

    @staticmethod
    def _pnl_price(side: str, entry_bid: float, entry_ask: float, bid: float, ask: float) -> float:
        if side == "LONG":
            return bid - entry_ask
        if side == "SHORT":
            return entry_bid - ask
        raise ValueError(f"invalid markout side: {side}")

    def register_opportunity(
        self,
        opportunity: dict[str, Any],
        *,
        at: Any,
        bid: float,
        ask: float,
        context: dict[str, Any] | None = None,
    ) -> bool:
        if not self.enabled:
            return False
        opportunity_id = str(opportunity.get("opportunity_id") or "")
        if not opportunity_id:
            raise ValueError("shadow opportunity_id is required")
        if opportunity_id in self.state["pending"] or opportunity_id in self.state["completed"]:
            return False
        bid_value = _finite(bid)
        ask_value = _finite(ask)
        if bid_value is None or ask_value is None or bid_value <= 0 or ask_value < bid_value:
            raise ValueError("shadow observer requires a valid executable Bid/Ask")
        raw_side = str(opportunity.get("raw_side") or opportunity.get("side") or "").upper()
        effective_value = (
            opportunity.get("effective_side")
            if "effective_side" in opportunity
            else opportunity.get("side")
        )
        effective_side = str(effective_value or "").upper()
        if effective_side in {"LONG", "SHORT"}:
            markout_side = effective_side
            markout_basis = "effective_side"
        elif raw_side in {"LONG", "SHORT"}:
            markout_side = raw_side
            markout_basis = "raw_fallback_policy_blocked"
        else:
            raise ValueError("shadow observer requires a raw or effective side")
        now = _utc(at)
        initial_pnl = self._pnl_price(markout_side, bid_value, ask_value, bid_value, ask_value)
        opportunity_lot = _finite(opportunity.get("lot"), self.lot)
        if opportunity_lot is None or opportunity_lot <= 0:
            raise ValueError("shadow observer requires a positive lot")
        item = {
            "opportunity_id": opportunity_id,
            "symbol": self.symbol,
            "raw_side": raw_side,
            "effective_side": effective_side if effective_side in {"LONG", "SHORT"} else "",
            "markout_side": markout_side,
            "markout_basis": markout_basis,
            "entry_policy": dict(opportunity.get("entry_policy") or {}),
            "event_time": str(opportunity.get("event_time") or ""),
            "release_time": str(opportunity.get("release_time") or ""),
            "decision_time": str(opportunity.get("decision_time") or _dt_text(now)),
            "registered_at": _dt_text(now),
            "entry_bid": bid_value,
            "entry_ask": ask_value,
            "lot": opportunity_lot,
            "last_bid": bid_value,
            "last_ask": ask_value,
            "last_quote_at": _dt_text(now),
            "mfe_price": initial_pnl,
            "mae_price": initial_pnl,
            "quote_samples": 1,
            "emitted_horizons": [],
            "route_status": "registered",
            "consumed_lane_id": "",
            "route_reason": "",
            "context": dict(context or {}),
        }
        self.state["pending"][opportunity_id] = item
        self._save(now)
        if opportunity_id not in self._registration_ids:
            _append_csv(self.opportunity_path, self._registration_row(item, at=now), OPPORTUNITY_FIELDS)
            self._registration_ids.add(opportunity_id)
        return True

    def record_route(
        self,
        opportunity_id: str,
        *,
        at: Any,
        status: str,
        consumed_lane_id: int | None = None,
        reason: str = "",
    ) -> bool:
        if not self.enabled:
            return False
        item = self.state["pending"].get(str(opportunity_id))
        if item is None:
            return False
        now = _utc(at)
        item["route_status"] = str(status)
        item["consumed_lane_id"] = "" if consumed_lane_id is None else int(consumed_lane_id)
        item["route_reason"] = str(reason)
        item["route_updated_at"] = _dt_text(now)
        self._save(now)
        key = (str(opportunity_id), str(status), str(item["consumed_lane_id"]))
        if key not in self._route_keys:
            _append_csv(self.opportunity_path, self._route_row(item, at=now), OPPORTUNITY_FIELDS)
            self._route_keys.add(key)
        return True

    def observe_quote(self, *, at: Any, bid: float, ask: float) -> int:
        if not self.enabled or not self.state["pending"]:
            return 0
        now = _utc(at)
        bid_value = _finite(bid)
        ask_value = _finite(ask)
        if bid_value is None or ask_value is None or bid_value <= 0 or ask_value < bid_value:
            raise ValueError("shadow observer requires a valid executable Bid/Ask")
        emitted_count = 0
        completed_ids: list[str] = []
        for opportunity_id, item in list(self.state["pending"].items()):
            last_quote_at = _utc(item["last_quote_at"])
            if now <= last_quote_at:
                continue
            pnl_price = self._pnl_price(item["markout_side"], item["entry_bid"], item["entry_ask"], bid_value, ask_value)
            item["last_bid"] = bid_value
            item["last_ask"] = ask_value
            item["last_quote_at"] = _dt_text(now)
            item["mfe_price"] = max(float(item["mfe_price"]), pnl_price)
            item["mae_price"] = min(float(item["mae_price"]), pnl_price)
            item["quote_samples"] = int(item.get("quote_samples", 0)) + 1
            emitted = {int(value) for value in item.get("emitted_horizons", [])}
            registered = _utc(item["registered_at"])
            for horizon in self.horizons:
                due = registered.timestamp() + horizon * 60
                if horizon in emitted or now.timestamp() < due:
                    continue
                key = (opportunity_id, horizon)
                if key not in self._markout_keys:
                    multiplier = self.contract_size * float(item["lot"])
                    row = {
                        "timestamp_utc": _dt_text(now),
                        "observer_version": OBSERVER_VERSION,
                        "opportunity_id": opportunity_id,
                        "symbol": item["symbol"],
                        "raw_side": item["raw_side"],
                        "effective_side": item["effective_side"],
                        "markout_side": item["markout_side"],
                        "markout_basis": item["markout_basis"],
                        "horizon_minutes": horizon,
                        "registered_at": item["registered_at"],
                        "due_at": _dt_text(datetime.fromtimestamp(due, UTC)),
                        "observed_at": _dt_text(now),
                        "observation_delay_seconds": round(max(0.0, now.timestamp() - due), 3),
                        "entry_bid": item["entry_bid"],
                        "entry_ask": item["entry_ask"],
                        "lot": item["lot"],
                        "exit_bid": bid_value,
                        "exit_ask": ask_value,
                        "pnl_price": pnl_price,
                        "pnl_usd": pnl_price * multiplier,
                        "mfe_price": item["mfe_price"],
                        "mae_price": item["mae_price"],
                        "mfe_usd": float(item["mfe_price"]) * multiplier,
                        "mae_usd": float(item["mae_price"]) * multiplier,
                        "quote_samples": item["quote_samples"],
                        "route_status": item.get("route_status", ""),
                        "consumed_lane_id": item.get("consumed_lane_id", ""),
                        "route_reason": item.get("route_reason", ""),
                    }
                    _append_csv(self.markout_path, row, MARKOUT_FIELDS)
                    self._markout_keys.add(key)
                    emitted_count += 1
                emitted.add(horizon)
            item["emitted_horizons"] = sorted(emitted)
            if all(horizon in emitted for horizon in self.horizons):
                completed_ids.append(opportunity_id)
        for opportunity_id in completed_ids:
            self.state["pending"].pop(opportunity_id, None)
            self.state["completed"][opportunity_id] = _dt_text(now)
        self._save(now)
        return emitted_count


__all__ = [
    "MARKOUT_FIELDS",
    "OBSERVER_VERSION",
    "OPPORTUNITY_FIELDS",
    "ShadowOpportunityObserver",
]
