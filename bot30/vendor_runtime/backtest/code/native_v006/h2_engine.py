#!/usr/bin/env python3
"""Streaming H2 diagnostic replay engine for ordered bid/ask quote events.

Observed demo deals are deliberately outside this module.  The only inputs are
ordered quote events and the frozen H1/H2 assumptions in ReplayConfig.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator, Protocol


EPOCH_DATE = date(1970, 1, 1)
PRICE_SCALE = 1_000_000  # integer micro-price units; point 0.001 = 1,000 units
VOLUME_STEP = Decimal("0.01")


class QuoteFault(RuntimeError):
    """A source quote cannot be used without inventing or repairing a side."""


def raw_utc_epoch_ms(raw_date: str, raw_time: str) -> int:
    """Parse the raw label as provisional UTC using integer arithmetic only."""
    date_parts = raw_date.split(".")
    time_parts = raw_time.split(":")
    if len(date_parts) != 3 or len(time_parts) != 3:
        raise QuoteFault(f"unsupported raw timestamp: {raw_date} {raw_time}")
    year, month, day = (int(value) for value in date_parts)
    second_parts = time_parts[2].split(".")
    if len(second_parts) == 1:
        whole_second, fraction = int(second_parts[0]), "0"
    elif len(second_parts) == 2:
        whole_second, fraction = int(second_parts[0]), second_parts[1]
    else:
        raise QuoteFault(f"unsupported raw timestamp fraction: {raw_time}")
    if len(fraction) > 3 or (fraction and not fraction.isdigit()):
        raise QuoteFault(f"tick precision exceeds the frozen millisecond clock: {raw_time}")
    millis = int(fraction.ljust(3, "0")) if fraction else 0
    hour, minute = int(time_parts[0]), int(time_parts[1])
    day_count = (date(year, month, day) - EPOCH_DATE).days
    whole_seconds = day_count * 86_400 + hour * 3_600 + minute * 60 + whole_second
    return whole_seconds * 1_000 + millis


def format_assumed_utc_ms(epoch_ms: int) -> str:
    """Format a provisional-UTC simulator clock without float timestamps."""
    days, rem_ms = divmod(epoch_ms, 86_400_000)
    d = EPOCH_DATE.fromordinal(EPOCH_DATE.toordinal() + days)
    whole_seconds, millis = divmod(rem_ms, 1_000)
    hours, rem = divmod(whole_seconds, 3_600)
    minutes, seconds = divmod(rem, 60)
    return f"{d:%Y-%m-%d}T{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}Z"


def parse_price_micro(raw: str, *, field_name: str) -> int:
    if raw is None or raw == "":
        raise QuoteFault(f"missing {field_name}")
    value = Decimal(raw) * PRICE_SCALE
    integral = value.to_integral_value()
    if value != integral:
        raise QuoteFault(f"{field_name} precision exceeds 1e-6 price units: {raw}")
    return int(integral)


def fmt_price_micro(value: int | None) -> str:
    if value is None:
        return ""
    sign = "-" if value < 0 else ""
    whole, frac = divmod(abs(value), PRICE_SCALE)
    return f"{sign}{whole}.{frac:06d}"


@dataclass(frozen=True)
class QuoteEvent:
    context_row_index: int
    source_row_sequence: int
    raw_date: str
    raw_time: str
    raw_time_label: str
    assumed_utc_epoch_ms: int
    raw_bid: str
    raw_ask: str
    raw_last: str
    raw_volume: str
    raw_flags: str
    flags: int
    bid_micro: int
    ask_micro: int
    carried_sides: tuple[str, ...] = ()


def read_ordered_quotes(
    context_path: Path,
    rowmap_path: Path,
    *,
    expected_rows: int,
) -> Iterator[QuoteEvent]:
    """Zip the byte-preserved context and row map; never sort or skip rows."""
    prior_bid: int | None = None
    prior_ask: int | None = None
    previous_time_ms: int | None = None
    previous_global_sequence: int | None = None
    context_count = 0
    rowmap_count = 0
    with context_path.open("r", encoding="utf-8-sig", newline="") as cstream, rowmap_path.open(
        "r", encoding="utf-8-sig", newline=""
    ) as mstream:
        context_reader = csv.DictReader(cstream, delimiter="\t")
        rowmap_reader = csv.DictReader(mstream, delimiter="\t")
        if context_reader.fieldnames != ["<DATE>", "<TIME>", "<BID>", "<ASK>", "<LAST>", "<VOLUME>", "<FLAGS>"]:
            raise QuoteFault(f"unexpected raw context schema: {context_reader.fieldnames}")
        if rowmap_reader.fieldnames != ["context_row_index", "source_row_sequence", "raw_event_time_label"]:
            raise QuoteFault(f"unexpected row map schema: {rowmap_reader.fieldnames}")
        context_iter = iter(context_reader)
        rowmap_iter = iter(rowmap_reader)
        while True:
            try:
                raw = next(context_iter)
            except StopIteration:
                raw = None
            try:
                mapping = next(rowmap_iter)
            except StopIteration:
                mapping = None
            if raw is None and mapping is None:
                break
            if raw is None or mapping is None:
                raise QuoteFault("context and row map row counts differ")
            context_count += 1
            rowmap_count += 1
            context_index = int(mapping["context_row_index"])
            source_sequence = int(mapping["source_row_sequence"])
            if context_index != context_count:
                raise QuoteFault(f"context row index is discontinuous at {context_count}")
            if previous_global_sequence is not None and source_sequence != previous_global_sequence + 1:
                raise QuoteFault(f"global source row mapping is discontinuous at {source_sequence}")
            previous_global_sequence = source_sequence
            raw_time_label = f'{raw["<DATE>"]} {raw["<TIME>"]}'
            if mapping["raw_event_time_label"] != raw_time_label:
                raise QuoteFault(f"row map raw timestamp differs at context row {context_count}")
            epoch_ms = raw_utc_epoch_ms(raw["<DATE>"], raw["<TIME>"])
            if previous_time_ms is not None and epoch_ms < previous_time_ms:
                raise QuoteFault(f"source-order timestamp reversal at row {source_sequence}")
            previous_time_ms = epoch_ms
            raw_flags = raw["<FLAGS>"]
            try:
                flags = int(raw_flags)
            except (TypeError, ValueError) as exc:
                raise QuoteFault(f"invalid raw FLAGS at source row {source_sequence}") from exc
            if flags < 0:
                raise QuoteFault(f"negative FLAGS at source row {source_sequence}")
            bid_raw, ask_raw = raw["<BID>"], raw["<ASK>"]
            bid_update = bool(flags & 0x2)
            ask_update = bool(flags & 0x4)
            carried: list[str] = []
            if not bid_raw and not ask_raw:
                raise QuoteFault(f"both quote sides missing at source row {source_sequence}")
            if bid_raw:
                bid = parse_price_micro(bid_raw, field_name="BID")
            elif bid_update:
                raise QuoteFault(f"BID update bit set with missing BID at source row {source_sequence}")
            elif not ask_update:
                raise QuoteFault(f"BID omission is not paired with an ASK update at source row {source_sequence}")
            elif prior_bid is None:
                raise QuoteFault(f"leading missing BID at source row {source_sequence}")
            else:
                bid = prior_bid
                carried.append("BID")
            if ask_raw:
                ask = parse_price_micro(ask_raw, field_name="ASK")
            elif ask_update:
                raise QuoteFault(f"ASK update bit set with missing ASK at source row {source_sequence}")
            elif not bid_update:
                raise QuoteFault(f"ASK omission is not paired with a BID update at source row {source_sequence}")
            elif prior_ask is None:
                raise QuoteFault(f"leading missing ASK at source row {source_sequence}")
            else:
                ask = prior_ask
                carried.append("ASK")
            if bid <= 0 or ask <= 0:
                raise QuoteFault(f"nonpositive effective quote at source row {source_sequence}")
            if bid > ask:
                raise QuoteFault(f"crossed effective quote at source row {source_sequence}")
            prior_bid, prior_ask = bid, ask
            yield QuoteEvent(
                context_index,
                source_sequence,
                raw["<DATE>"],
                raw["<TIME>"],
                raw_time_label,
                epoch_ms,
                bid_raw,
                ask_raw,
                raw["<LAST>"],
                raw["<VOLUME>"],
                raw_flags,
                flags,
                bid,
                ask,
                tuple(carried),
            )
    if context_count != expected_rows or rowmap_count != expected_rows:
        raise QuoteFault(f"expected {expected_rows} context/map rows, got {context_count}/{rowmap_count}")


@dataclass(frozen=True)
class ReplayConfig:
    activation_raw_date: str = "2026.10.08"
    activation_raw_time: str = "13:29:24.000"
    activation_boundary_label: str = "parent-reported direct observation; diagnostic only"
    initial_lot_steps: int = 1
    p2_lot_steps: int = 3
    donchian_period: int = 80
    atr_period: int = 14
    p2_start_positions_strictly_greater_than: int = 2
    edge_atr_fraction: Decimal = Decimal("0.3")
    p1_breakout_spacing_points: int = 400
    p2_spacing_points: int = 400
    hedge_stop_points: int = 300
    market_latency_ms: int = 100
    point_micro: int = 1_000
    assumed_contract_size: int = 100
    account_currency_basis: str = "USD parent-reported; local snapshot lacks currency field"
    p2_recovery_target_currency_micros: int = 500_000
    basket_minimum_settlement_currency_micros: int = 500_000
    basket_profit_per_position_currency_micros: int = 500_000
    external_fee_per_lot_currency_micros: int = 11_000_000
    max_positions: int = 500
    time_pause_start_hour_utc_assumed: int = 21
    time_pause_end_hour_utc_assumed: int = 2
    shock_atr_short: int = 3
    shock_atr_long: int = 30
    shock_atr_ratio: Decimal = Decimal("2")
    shock_latest_tr_ratio: Decimal = Decimal("2.5")
    shock_resume_ratio: Decimal = Decimal("1.3")
    shock_resume_minutes: int = 60
    shock_resume_normal_bars: int = 3
    settlement_cooldown_ms: int = 15 * 60 * 1_000
    entry_slippage_micro: int = 0
    modeled_commission_currency_micros: int = 0
    start_state_assumption: str = "flat inventory, no pending order, shock unlocked; unverified bootstrap"
    p2_policy_id: str = "H2: close all open P2 plus current worst-negative P1"
    close_order_sort: str = "own creation sequence ascending"
    close_by_model: str = "not implemented; no CloseBy PASS"

    @property
    def activation_epoch_ms(self) -> int:
        return raw_utc_epoch_ms(self.activation_raw_date, self.activation_raw_time)


@dataclass
class Bar:
    minute_index: int
    open_micro: int
    high_micro: int
    low_micro: int
    close_micro: int
    first_source_row_sequence: int
    first_raw_time: str


@dataclass
class Position:
    position_id: str
    role: str
    side: str
    volume_steps: int
    entry_price_micro: int
    entry_price_raw: str
    entry_source_row_sequence: int
    entry_raw_time: str
    entry_epoch_ms_assumed_utc: int
    creation_sequence: int
    is_p1: bool
    best_favorable_micro: int
    stop_price_micro: int | None = None
    close_pending_order_id: str | None = None
    be_armed: bool = False
    trailing_armed: bool = False
    broker_order_id: str = ""
    strategy_parent_id: str = ""
    entry_filled_volume_steps_total: int = 0


@dataclass(frozen=True)
class StopEligibility:
    """Synthetic typed placement/ACK gate for an assumed resting STOP."""

    eligibility_basis: str
    order_id: str
    broker_order_id: str
    strategy_parent_id: str
    source_position_id: str
    side: str
    trigger_price_micro: int
    volume_steps: int
    placement_source_row_sequence: int
    placement_epoch_ms_assumed_utc_provisional: int
    ack_source_row_sequence_assumed: int
    ack_epoch_ms_assumed_utc_provisional: int
    trigger_eligible_after_source_row_sequence: int
    trigger_eligible_after_epoch_ms_assumed_utc_provisional: int


@dataclass
class PendingStop:
    side: str
    trigger_price_micro: int
    volume_steps: int
    active_after_source_row_sequence: int
    source_position_id: str
    order_id: str
    broker_order_id: str
    strategy_parent_id: str
    created_source_row_sequence: int
    created_raw_time: str
    created_epoch_ms_assumed_utc_provisional: int
    active_after_epoch_ms_assumed_utc_provisional: int
    eligibility: StopEligibility | None


class Sink(Protocol):
    def emit(self, kind: str, row: dict[str, Any]) -> None: ...


class MemorySink:
    """Bounded fixture sink; only synthetic fixture outputs are kept in memory."""

    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}

    def emit(self, kind: str, row: dict[str, Any]) -> None:
        self.rows.setdefault(kind, []).append(dict(row))


class CsvSink:
    """Streaming CSV evidence sink with fail-closed per-kind schema checks."""

    FILES = {
        "order": "orders.csv",
        "fill": "fills.csv",
        "state": "state_events.csv",
        "branch": "branch_decisions.csv",
        "order_event": "order_events.csv",
        "quote_carry": "quote_carries.csv",
    }

    def __init__(self, output_dir: Path, *, flush_every: int = 4_096) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.flush_every = flush_every
        self._writers: dict[str, csv.DictWriter] = {}
        self._streams: dict[str, Any] = {}
        self.schemas: dict[str, tuple[str, ...]] = {}
        self.row_counts: Counter[str] = Counter()
        self.total_rows = 0

    def emit(self, kind: str, row: dict[str, Any]) -> None:
        if kind not in self.FILES:
            raise RuntimeError(f"unexpected engine output kind: {kind}")
        schema = tuple(row.keys())
        if kind not in self._writers:
            path = self.output_dir / self.FILES[kind]
            stream = path.open("x", encoding="utf-8", newline="")
            writer = csv.DictWriter(stream, fieldnames=list(schema), lineterminator="\n", extrasaction="raise")
            writer.writeheader()
            self._streams[kind] = stream
            self._writers[kind] = writer
            self.schemas[kind] = schema
        elif self.schemas[kind] != schema:
            raise RuntimeError(f"output schema drift for {kind}: {schema} != {self.schemas[kind]}")
        self._writers[kind].writerow(row)
        self.row_counts[kind] += 1
        self.total_rows += 1
        if self.total_rows % self.flush_every == 0:
            for stream in self._streams.values():
                stream.flush()

    def close(self) -> None:
        for stream in self._streams.values():
            stream.flush()
            stream.close()
        self._streams.clear()

    def __enter__(self) -> "CsvSink":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


class ExecutionPlan:
    """Synthetic-only order outcomes; no wall-clock timeout is inferred."""

    def __init__(self, outcomes: dict[tuple[str, str], list[tuple[str, int | None]]] | None = None) -> None:
        self.outcomes = {key: list(values) for key, values in (outcomes or {}).items()}

    def next(self, reason: str, position_role: str) -> tuple[str, int | None]:
        queue = self.outcomes.get((reason, position_role))
        return queue.pop(0) if queue else ("fill", None)


class ReplayEngine:
    def __init__(
        self,
        sink: Sink,
        *,
        config: ReplayConfig | None = None,
        execution_plan: ExecutionPlan | None = None,
    ) -> None:
        self.config = config or ReplayConfig()
        self.sink = sink
        self.execution_plan = execution_plan or ExecutionPlan()
        self.completed_bars: deque[Bar] = deque(maxlen=self.config.donchian_period)
        self.true_ranges_micro: deque[int] = deque(maxlen=self.config.shock_atr_long)
        self.current_bar: Bar | None = None
        self.previous_completed_close_micro: int | None = None
        self.inventory: dict[str, Position] = {}
        self.pending_orders: deque[dict[str, Any]] = deque()
        self.pending_stop: PendingStop | None = None
        self.stop_metadata_by_order_id: dict[str, StopEligibility] = {}
        self.close_batch: dict[str, Any] | None = None
        self.single_close_retries: dict[str, int] = {}
        self.execution_degraded = False
        self.started = False
        self.activation_source_row_sequence: int | None = None
        self.start_raw_time: str | None = None
        self.last_epoch_ms: int | None = None
        self.last_source_row_sequence: int | None = None
        self.last_completed_bar_event_ms: int | None = None
        self.shock_locked: bool | None = None
        self.shock_lock_started_ms: int | None = None
        self.shock_normal_bars = 0
        self.last_full_settlement_ms: int | None = None
        self.p1_anchor_by_side: dict[str, int] = {}
        self.last_p1_fill_by_side: dict[str, int] = {}
        self.last_p2_fill_by_side: dict[str, int] = {}
        self.position_sequence = 0
        self.order_sequence = 0
        self.broker_order_sequence = 0
        self.strategy_parent_sequence = 0
        self.fill_sequence = 0
        self.state_sequence = 0
        self.batch_sequence = 0
        self.last_features: dict[str, Any] | None = None
        self.reason_counts: Counter[str] = Counter()
        self.metrics: Counter[str] = Counter()
        self.context_rows = 0
        self.source_equal_timestamp_pairs = 0
        self.max_gap_ms = 0
        self.long_gap_gt_60s = 0
        self._last_minute_decision: int | None = None

    def _state_name(self) -> str:
        if self.execution_degraded:
            return "EXECUTION_DEGRADED"
        if self.close_batch is not None:
            return "CLOSE_PENDING"
        if not self.inventory:
            if any(item["command_type"] == "entry" for item in self.pending_orders):
                return "ENTRY_PENDING"
            return "FLAT"
        if any(p.role == "p2" for p in self.inventory.values()):
            return "P2_ACTIVE"
        if any(p.role in {"hedge", "p1_breakout"} for p in self.inventory.values()):
            return "HEDGED_OR_P1"
        return "INITIAL"

    def _position_counts(self) -> tuple[int, int, int, int]:
        p1 = sum(1 for p in self.inventory.values() if p.is_p1 and p.volume_steps > 0)
        p2 = sum(1 for p in self.inventory.values() if p.role == "p2" and p.volume_steps > 0)
        volume = sum(p.volume_steps for p in self.inventory.values())
        return len(self.inventory), p1, p2, volume

    def _reserved_position_slots(self) -> int:
        # Capacity is a set of future/open position identities, not a sum of
        # volume and order counts. A partial RETURN order bound to an existing
        # position therefore consumes no additional slot; after that position
        # closes, the same active order reserves its identity for reopening.
        identities = {("position", position_id) for position_id in self.inventory}
        for item in self.pending_orders:
            if item["command_type"] != "entry":
                continue
            bound_position_id = item.get("bound_position_id") or ""
            if bound_position_id and self._entry_command_identity_valid(item):
                identities.add(("position", bound_position_id))
            else:
                identities.add(("future_order", item["broker_order_id"]))
        if self.pending_stop is not None:
            identities.add(("future_order", self.pending_stop.broker_order_id))
        return len(identities)

    def _state_snapshot(self) -> dict[str, Any]:
        total, p1, p2, volume = self._position_counts()
        return {
            "state": self._state_name(),
            "open_positions": total,
            "open_p1_positions": p1,
            "open_p2_positions": p2,
            "open_volume_steps": volume,
            "reserved_position_slots": self._reserved_position_slots(),
            "max_positions": self.config.max_positions,
            "pending_market_orders": len(self.pending_orders),
            "pending_stop": self.pending_stop.order_id if self.pending_stop else "",
            "close_batch_id": self.close_batch["batch_id"] if self.close_batch else "",
            "close_batch_targets_remaining": len(self.close_batch["remaining_targets"]) if self.close_batch else 0,
            "execution_degraded": self.execution_degraded,
        }

    def diagnostic_state_snapshot(self) -> dict[str, Any]:
        """Return deterministic causal state for fixture prefix comparisons."""
        return {
            "context_rows": self.context_rows,
            "last_epoch_ms": self.last_epoch_ms,
            "last_source_row_sequence": self.last_source_row_sequence,
            "current_bar": vars(self.current_bar).copy() if self.current_bar else None,
            "completed_bars": [vars(bar).copy() for bar in self.completed_bars],
            "previous_completed_close_micro": self.previous_completed_close_micro,
            "true_ranges_micro": list(self.true_ranges_micro),
            "inventory": [vars(pos).copy() for pos in sorted(self.inventory.values(), key=lambda p: p.creation_sequence)],
            "pending_orders": [dict(order) for order in self.pending_orders],
            "pending_stop": vars(self.pending_stop).copy() if self.pending_stop else None,
            "close_batch": {
                **self.close_batch,
                "partial_fill_counts": dict(self.close_batch["partial_fill_counts"]),
            } if self.close_batch else None,
            "single_close_retries": dict(self.single_close_retries),
            "execution_degraded": self.execution_degraded,
            "started": self.started,
            "activation_source_row_sequence": self.activation_source_row_sequence,
            "start_raw_time": self.start_raw_time,
            "shock_locked": self.shock_locked,
            "shock_lock_started_ms": self.shock_lock_started_ms,
            "shock_normal_bars": self.shock_normal_bars,
            "last_full_settlement_ms": self.last_full_settlement_ms,
            "p1_anchor_by_side": dict(self.p1_anchor_by_side),
            "last_p1_fill_by_side": dict(self.last_p1_fill_by_side),
            "last_p2_fill_by_side": dict(self.last_p2_fill_by_side),
            "position_sequence": self.position_sequence,
            "order_sequence": self.order_sequence,
            "fill_sequence": self.fill_sequence,
            "state_sequence": self.state_sequence,
            "batch_sequence": self.batch_sequence,
            "last_features": dict(self.last_features) if self.last_features else None,
            "last_minute_decision": self._last_minute_decision,
        }

    def _emit_state(self, event: str, tick: QuoteEvent, reason: str, details: dict[str, Any] | None = None) -> None:
        self.state_sequence += 1
        self.sink.emit("state", {
            "state_event_sequence": self.state_sequence,
            "source_row_sequence": tick.source_row_sequence,
            "raw_time": tick.raw_time_label,
            "assumed_utc_epoch_ms_provisional": tick.assumed_utc_epoch_ms,
            "event": event,
            "reason": reason,
            **self._state_snapshot(),
            "details_json": json.dumps(details or {}, ensure_ascii=False, sort_keys=True, default=str),
            "event_time": "",
            "release_time": "",
            "ingested_time": "",
            "available_time": "",
            "cutoff_time": "",
        })

    def _emit_branch(self, tick: QuoteEvent, branch: str, decision: str, reason: str, features: dict[str, Any] | None = None, extra: dict[str, Any] | None = None) -> None:
        self.reason_counts[f"{branch}:{reason}"] += 1
        total, p1, p2, _ = self._position_counts()
        features = features or {}
        self.sink.emit("branch", {
            "source_row_sequence": tick.source_row_sequence,
            "raw_time": tick.raw_time_label,
            "assumed_utc_epoch_ms_provisional": tick.assumed_utc_epoch_ms,
            "branch": branch,
            "decision": decision,
            "reason": reason,
            "bid_raw": tick.raw_bid,
            "ask_raw": tick.raw_ask,
            "bid_effective": fmt_price_micro(tick.bid_micro),
            "ask_effective": fmt_price_micro(tick.ask_micro),
            "donchian_upper": fmt_price_micro(features.get("donchian_upper_micro")),
            "donchian_lower": fmt_price_micro(features.get("donchian_lower_micro")),
            "atr14_price": str(features.get("atr14_micro", "")),
            "atr3_price": str(features.get("atr3_micro", "")),
            "atr30_price": str(features.get("atr30_micro", "")),
            "latest_tr_price": str(features.get("latest_tr_micro", "")),
            "p1_open_count": p1,
            "p2_open_count": p2,
            "open_position_count": total,
            "shock_locked": self.shock_locked,
            "details_json": json.dumps(extra or {}, ensure_ascii=False, sort_keys=True, default=str),
            "event_time": "",
            "release_time": "",
            "ingested_time": "",
            "available_time": "",
            "cutoff_time": "",
        })

    def _update_bar(self, tick: QuoteEvent) -> bool:
        minute_index = tick.assumed_utc_epoch_ms // 60_000
        if self.current_bar is None:
            self.current_bar = Bar(minute_index, tick.bid_micro, tick.bid_micro, tick.bid_micro, tick.bid_micro, tick.source_row_sequence, tick.raw_time_label)
            return False
        if minute_index < self.current_bar.minute_index:
            raise QuoteFault("minute index reversed despite source timestamp validation")
        if minute_index == self.current_bar.minute_index:
            self.current_bar.high_micro = max(self.current_bar.high_micro, tick.bid_micro)
            self.current_bar.low_micro = min(self.current_bar.low_micro, tick.bid_micro)
            self.current_bar.close_micro = tick.bid_micro
            return False
        finished = self.current_bar
        if self.previous_completed_close_micro is None:
            tr_micro = finished.high_micro - finished.low_micro
        else:
            tr_micro = max(
                finished.high_micro - finished.low_micro,
                abs(finished.high_micro - self.previous_completed_close_micro),
                abs(finished.low_micro - self.previous_completed_close_micro),
            )
        self.true_ranges_micro.append(tr_micro)
        self.completed_bars.append(finished)
        self.previous_completed_close_micro = finished.close_micro
        self.last_completed_bar_event_ms = tick.assumed_utc_epoch_ms
        self.current_bar = Bar(minute_index, tick.bid_micro, tick.bid_micro, tick.bid_micro, tick.bid_micro, tick.source_row_sequence, tick.raw_time_label)
        return True

    def _features(self) -> dict[str, Any] | None:
        if len(self.completed_bars) < self.config.donchian_period or len(self.true_ranges_micro) < self.config.shock_atr_long:
            return None
        donchian = list(self.completed_bars)
        bars80 = donchian[-self.config.donchian_period:]
        tr = list(self.true_ranges_micro)
        atr14 = Decimal(sum(tr[-self.config.atr_period:])) / Decimal(self.config.atr_period)
        atr3 = Decimal(sum(tr[-self.config.shock_atr_short:])) / Decimal(self.config.shock_atr_short)
        atr30 = Decimal(sum(tr[-self.config.shock_atr_long:])) / Decimal(self.config.shock_atr_long)
        return {
            "donchian_upper_micro": max(bar.high_micro for bar in bars80),
            "donchian_lower_micro": min(bar.low_micro for bar in bars80),
            "atr14_micro": atr14,
            "atr3_micro": atr3,
            "atr30_micro": atr30,
            "latest_tr_micro": tr[-1],
            "atr_short_ratio": atr3 / atr30 if atr30 else Decimal(0),
            "latest_tr_ratio": Decimal(tr[-1]) / atr30 if atr30 else Decimal(0),
            "completed_bar_count": len(self.completed_bars),
            "tr_count": len(tr),
            "last_completed_bar_minute_index": bars80[-1].minute_index,
            "last_completed_bar_high_micro": bars80[-1].high_micro,
            "last_completed_bar_low_micro": bars80[-1].low_micro,
        }

    def _on_completed_bar(self, tick: QuoteEvent, features: dict[str, Any] | None) -> None:
        if not self.started or features is None:
            return
        ratio = features["atr_short_ratio"]
        latest_ratio = features["latest_tr_ratio"]
        is_shock = ratio >= self.config.shock_atr_ratio or latest_ratio >= self.config.shock_latest_tr_ratio
        if is_shock:
            self.shock_locked = True
            self.shock_lock_started_ms = tick.assumed_utc_epoch_ms
            self.shock_normal_bars = 0
            self.metrics["shock_lock_bars"] += 1
            self._emit_state("shock_lock", tick, "ATR3_ATR30_OR_LATEST_TR_THRESHOLD", {
                "atr3_atr30_ratio": str(ratio),
                "latest_tr_atr30_ratio": str(latest_ratio),
            })
            return
        if self.shock_locked:
            if ratio <= self.config.shock_resume_ratio and latest_ratio < self.config.shock_latest_tr_ratio:
                self.shock_normal_bars += 1
            else:
                self.shock_normal_bars = 0
            elapsed = tick.assumed_utc_epoch_ms - (self.shock_lock_started_ms or tick.assumed_utc_epoch_ms)
            if elapsed >= self.config.shock_resume_minutes * 60_000 and self.shock_normal_bars >= self.config.shock_resume_normal_bars:
                self.shock_locked = False
                self.shock_lock_started_ms = None
                self.shock_normal_bars = 0
                self.metrics["shock_unlocks"] += 1
                self._emit_state("shock_unlock", tick, "60MIN_AND_3_NORMAL_COMPLETED_BARS", {
                    "elapsed_ms_assumed_utc": elapsed,
                })

    def _inside_initial_pause(self, tick: QuoteEvent) -> bool:
        ms_per_day = 86_400_000
        hour = (tick.assumed_utc_epoch_ms % ms_per_day) // 3_600_000
        return hour >= self.config.time_pause_start_hour_utc_assumed or hour < self.config.time_pause_end_hour_utc_assumed

    def _cooldown_active(self, tick: QuoteEvent) -> bool:
        return self.last_full_settlement_ms is not None and (
            tick.assumed_utc_epoch_ms - self.last_full_settlement_ms < self.config.settlement_cooldown_ms
        )

    def _side_exit_price(self, pos: Position, tick: QuoteEvent) -> int:
        return tick.bid_micro if pos.side == "BUY" else tick.ask_micro

    def _mark_currency_micro(self, pos: Position, tick: QuoteEvent) -> int:
        exit_price = self._side_exit_price(pos, tick)
        difference = exit_price - pos.entry_price_micro if pos.side == "BUY" else pos.entry_price_micro - exit_price
        # With assumed contract 100 and 0.01 lots per step, price-micro * steps
        # is gross currency-micro. External fee is applied by the caller.
        return difference * pos.volume_steps

    def _external_fee_currency_micro(self, volume_steps: int) -> int:
        # Static H1 hypothesis: external fee is USD/1.00 lot and each step is
        # 0.01 lot. Its real timing/basis is not recovered from the EX5.
        return self.config.external_fee_per_lot_currency_micros * volume_steps // 100

    def _new_order(
        self,
        tick: QuoteEvent,
        *,
        command_type: str,
        side: str,
        role: str,
        volume_steps: int,
        reason: str,
        strategy_parent_id: str = "",
        target_position_id: str = "",
        retry_count: int = 0,
        parent_order_id: str = "",
        batch_id: str = "",
        stop_trigger_micro: int | None = None,
    ) -> dict[str, Any]:
        self.order_sequence += 1
        order_id = f"SIM-O{self.order_sequence:07d}"
        self.broker_order_sequence += 1
        broker_order_id = f"SIM-BO{self.broker_order_sequence:07d}"
        if not strategy_parent_id:
            self.strategy_parent_sequence += 1
            strategy_parent_id = f"SIM-SP{self.strategy_parent_sequence:07d}"
        is_resting_stop = command_type == "pending_stop"
        market_eligible_epoch = None if is_resting_stop else tick.assumed_utc_epoch_ms + self.config.market_latency_ms
        command = {
            "order_id": order_id,
            "broker_order_id": broker_order_id,
            "strategy_parent_id": strategy_parent_id,
            "command_type": command_type,
            "side": side,
            "role": role,
            "volume_steps": volume_steps,
            "initial_volume_steps": volume_steps,
            "filled_volume_steps_total": 0,
            "bound_position_id": "",
            "position_opening_snapshot": None,
            "reason": reason,
            "target_position_id": target_position_id,
            "retry_count": retry_count,
            "parent_order_id": parent_order_id,
            "batch_id": batch_id,
            "decision_source_row_sequence": tick.source_row_sequence,
            "decision_raw_time": tick.raw_time_label,
            "decision_epoch_ms_assumed_utc_provisional": tick.assumed_utc_epoch_ms,
            "simulated_eligibility_basis": "resting_stop_cross_after_ack" if is_resting_stop else "market_latency_after_command",
            "simulated_eligible_after_epoch_ms_assumed_utc": market_eligible_epoch,
            "simulated_eligible_after_raw_utc_assumption": format_assumed_utc_ms(market_eligible_epoch) if market_eligible_epoch is not None else None,
            "stop_placement_source_row_sequence": tick.source_row_sequence if is_resting_stop else None,
            "stop_placement_epoch_ms_assumed_utc_provisional": tick.assumed_utc_epoch_ms if is_resting_stop else None,
            "stop_ack_source_row_sequence_assumed": tick.source_row_sequence if is_resting_stop else None,
            "stop_ack_epoch_ms_assumed_utc_provisional": tick.assumed_utc_epoch_ms if is_resting_stop else None,
            "stop_trigger_eligible_after_source_row_sequence": tick.source_row_sequence if is_resting_stop else None,
            "stop_trigger_eligible_after_epoch_ms_assumed_utc_provisional": tick.assumed_utc_epoch_ms if is_resting_stop else None,
            "stop_trigger_micro": stop_trigger_micro,
            "status": "queued",
        }
        self.sink.emit("order", command)
        self.sink.emit("order_event", {
            "order_id": order_id,
            "broker_order_id": broker_order_id,
            "strategy_parent_id": strategy_parent_id,
            "event": "command_queued",
            "source_row_sequence": tick.source_row_sequence,
            "raw_time": tick.raw_time_label,
            "reason": reason,
            "requested_volume_steps": volume_steps,
            "filled_volume_steps": 0,
            "residual_volume_steps": volume_steps,
            "details_json": json.dumps({"parent_order_id": parent_order_id, "retry_count": retry_count, "batch_id": batch_id}),
        })
        return command

    def _queue_market_order(self, tick: QuoteEvent, command: dict[str, Any]) -> bool:
        if command["command_type"] == "entry":
            if not self._entry_command_identity_valid(command):
                self.execution_degraded = True
                command["status"] = "blocked_identity_mismatch"
                self.sink.emit("order_event", {
                    "order_id": command["order_id"],
                    "broker_order_id": command["broker_order_id"],
                    "strategy_parent_id": command["strategy_parent_id"],
                    "event": "entry_identity_blocked",
                    "source_row_sequence": tick.source_row_sequence,
                    "raw_time": tick.raw_time_label,
                    "reason": "entry_command_position_identity_mismatch",
                    "requested_volume_steps": command["initial_volume_steps"],
                    "filled_volume_steps": command.get("filled_volume_steps_total", 0),
                    "residual_volume_steps": command["volume_steps"],
                    "details_json": "{}",
                })
                self._emit_state("execution_degraded", tick, "entry_command_position_identity_mismatch", {
                    "order_id": command["order_id"],
                    "broker_order_id": command["broker_order_id"],
                    "strategy_parent_id": command["strategy_parent_id"],
                })
                return False
            bound_id = command.get("bound_position_id") or ""
            already_reserved = bool(bound_id and bound_id in self.inventory)
            if bound_id and not already_reserved:
                already_reserved = any(
                    item["command_type"] == "entry" and item.get("bound_position_id") == bound_id
                    for item in self.pending_orders
                )
            if not bound_id:
                already_reserved = any(
                    item["command_type"] == "entry" and item["broker_order_id"] == command["broker_order_id"]
                    for item in self.pending_orders
                )
            projected_slots = self._reserved_position_slots() + (0 if already_reserved else 1)
            if projected_slots > self.config.max_positions:
                command["status"] = "blocked_position_cap"
                self.sink.emit("order_event", {
                    "order_id": command["order_id"],
                    "broker_order_id": command["broker_order_id"],
                    "strategy_parent_id": command["strategy_parent_id"],
                    "event": "entry_reservation_blocked",
                    "source_row_sequence": tick.source_row_sequence,
                    "raw_time": tick.raw_time_label,
                    "reason": "open_plus_future_position_identity_cap",
                    "requested_volume_steps": command["volume_steps"],
                    "filled_volume_steps": command.get("filled_volume_steps_total", 0),
                    "residual_volume_steps": command["volume_steps"],
                    "details_json": json.dumps({
                        "open_positions": len(self.inventory),
                        "reserved_position_slots": self._reserved_position_slots(),
                        "projected_reserved_position_slots": projected_slots,
                        "max_positions": self.config.max_positions,
                    }),
                })
                self._emit_state("entry_order_blocked_position_cap", tick, "open_plus_future_position_identity_cap", {
                    "order_id": command["order_id"],
                    "broker_order_id": command["broker_order_id"],
                    "strategy_parent_id": command["strategy_parent_id"],
                    "projected_reserved_position_slots": projected_slots,
                    "max_positions": self.config.max_positions,
                })
                return False
        self.pending_orders.append(command)
        if command["command_type"] == "exit" and command["target_position_id"] in self.inventory:
            self.inventory[command["target_position_id"]].close_pending_order_id = command["order_id"]
        self._emit_state("command_queued", tick, command["reason"], {
            "order_id": command["order_id"],
            "broker_order_id": command["broker_order_id"],
            "strategy_parent_id": command["strategy_parent_id"],
        })
        return True

    def _position_role(self, role: str) -> str:
        return role.lower()

    def _create_position(self, command: dict[str, Any], tick: QuoteEvent, fill_price_micro: int, raw_fill_price: str, *, volume_steps: int | None = None) -> Position:
        snapshot = command.get("position_opening_snapshot")
        bound_position_id = command.get("bound_position_id") or ""
        if bound_position_id and snapshot:
            position_id = bound_position_id
            creation_sequence = int(snapshot["creation_sequence"])
            self.position_sequence = max(self.position_sequence, creation_sequence)
            entry_source_row_sequence = int(snapshot["entry_source_row_sequence"])
            entry_raw_time = str(snapshot["entry_raw_time"])
            entry_epoch_ms_assumed_utc = int(snapshot["entry_epoch_ms_assumed_utc"])
            best_favorable_micro = fill_price_micro
        else:
            self.position_sequence += 1
            position_id = f"SIM-P{self.position_sequence:07d}"
            creation_sequence = self.position_sequence
            entry_source_row_sequence = tick.source_row_sequence
            entry_raw_time = tick.raw_time_label
            entry_epoch_ms_assumed_utc = tick.assumed_utc_epoch_ms
            best_favorable_micro = fill_price_micro
        role = command["role"]
        is_p1 = role in {"initial", "hedge", "p1_breakout"}
        pos = Position(
            position_id=position_id,
            role=role,
            side=command["side"],
            volume_steps=command["volume_steps"] if volume_steps is None else volume_steps,
            entry_price_micro=fill_price_micro,
            entry_price_raw=raw_fill_price,
            entry_source_row_sequence=entry_source_row_sequence,
            entry_raw_time=entry_raw_time,
            entry_epoch_ms_assumed_utc=entry_epoch_ms_assumed_utc,
            creation_sequence=creation_sequence,
            is_p1=is_p1,
            best_favorable_micro=best_favorable_micro,
            broker_order_id=command.get("broker_order_id", ""),
            strategy_parent_id=command.get("strategy_parent_id", ""),
            entry_filled_volume_steps_total=command.get("filled_volume_steps_total", 0),
        )
        self.inventory[position_id] = pos
        if is_p1:
            self.p1_anchor_by_side.setdefault(pos.side, fill_price_micro)
            self.last_p1_fill_by_side[pos.side] = fill_price_micro
        if role == "p2":
            self.last_p2_fill_by_side[pos.side] = fill_price_micro
        return pos

    def _record_fill(
        self,
        command: dict[str, Any],
        tick: QuoteEvent,
        *,
        fill_steps: int,
        fill_price_micro: int,
        fill_price_raw: str,
        position: Position | None,
        position_volume_before: int,
        position_volume_after: int,
        fill_kind: str,
    ) -> None:
        self.fill_sequence += 1
        self.sink.emit("fill", {
            "fill_sequence": self.fill_sequence,
            "order_id": command["order_id"],
            "broker_order_id": command.get("broker_order_id", ""),
            "strategy_parent_id": command.get("strategy_parent_id", ""),
            "source_row_sequence": tick.source_row_sequence,
            "raw_time": tick.raw_time_label,
            "assumed_utc_epoch_ms_provisional": tick.assumed_utc_epoch_ms,
            "event_time": "",
            "release_time": "",
            "ingested_time": "",
            "available_time": "",
            "cutoff_time": "",
            "fill_kind": fill_kind,
            "side": command["side"],
            "position_id": position.position_id if position else "",
            "position_role": command["role"],
            "volume_steps": fill_steps,
            "volume_assumed": format(VOLUME_STEP * fill_steps, "f"),
            "fill_price_raw": fill_price_raw,
            "fill_price_micro": fill_price_micro,
            "position_volume_before_steps": position_volume_before,
            "position_volume_after_steps": position_volume_after,
            "reason": command["reason"],
            "simulated_eligibility_basis": command.get("simulated_eligibility_basis", "market_latency_after_command"),
            "stop_placement_source_row_sequence": command.get("stop_placement_source_row_sequence"),
            "stop_placement_epoch_ms_assumed_utc_provisional": command.get("stop_placement_epoch_ms_assumed_utc_provisional"),
            "stop_ack_source_row_sequence_assumed": command.get("stop_ack_source_row_sequence_assumed"),
            "stop_ack_epoch_ms_assumed_utc_provisional": command.get("stop_ack_epoch_ms_assumed_utc_provisional"),
            "stop_trigger_eligible_after_source_row_sequence": command.get("stop_trigger_eligible_after_source_row_sequence"),
            "stop_trigger_eligible_after_epoch_ms_assumed_utc_provisional": command.get("stop_trigger_eligible_after_epoch_ms_assumed_utc_provisional"),
            "price_model": "stop_trigger_price_assumed" if fill_kind == "resting_stop" else "first_strictly_later_quote_after_100ms_assumed",
        })

    def _make_hedge_stop(self, initial: Position, tick: QuoteEvent) -> None:
        if self.pending_stop is not None and self.pending_stop.source_position_id == initial.position_id:
            return
        if self._reserved_position_slots() >= self.config.max_positions:
            self._emit_state("hedge_stop_not_armed_position_cap", tick, "max_positions_reserved_slot_cap", {
                "open_positions": len(self.inventory),
                "reserved_position_slots": self._reserved_position_slots(),
                "max_positions": self.config.max_positions,
            })
            return
        side = "SELL" if initial.side == "BUY" else "BUY"
        distance = self.config.hedge_stop_points * self.config.point_micro
        trigger = initial.entry_price_micro - distance if side == "SELL" else initial.entry_price_micro + distance
        command = self._new_order(
            tick,
            command_type="pending_stop",
            side=side,
            role="hedge",
            volume_steps=initial.volume_steps,
            reason="opposite_hedge_stop_300_points_from_confirmed_initial_fill",
            target_position_id=initial.position_id,
            stop_trigger_micro=trigger,
        )
        # Placement and ACK are synthetic assumptions on the confirmed-entry
        # quote. The STOP can cross only after both its ACK row and ACK epoch.
        eligibility = StopEligibility(
            eligibility_basis="resting_stop_cross_after_ack",
            order_id=command["order_id"],
            broker_order_id=command["broker_order_id"],
            strategy_parent_id=command["strategy_parent_id"],
            source_position_id=initial.position_id,
            side=side,
            trigger_price_micro=trigger,
            volume_steps=initial.volume_steps,
            placement_source_row_sequence=tick.source_row_sequence,
            placement_epoch_ms_assumed_utc_provisional=tick.assumed_utc_epoch_ms,
            ack_source_row_sequence_assumed=tick.source_row_sequence,
            ack_epoch_ms_assumed_utc_provisional=tick.assumed_utc_epoch_ms,
            trigger_eligible_after_source_row_sequence=tick.source_row_sequence,
            trigger_eligible_after_epoch_ms_assumed_utc_provisional=tick.assumed_utc_epoch_ms,
        )
        self.stop_metadata_by_order_id[command["order_id"]] = eligibility
        self.pending_stop = PendingStop(
            side, trigger, initial.volume_steps, tick.source_row_sequence,
            initial.position_id, command["order_id"], command["broker_order_id"],
            command["strategy_parent_id"], tick.source_row_sequence, tick.raw_time_label,
            tick.assumed_utc_epoch_ms, tick.assumed_utc_epoch_ms, eligibility,
        )
        self.sink.emit("order_event", {
            "order_id": command["order_id"],
            "broker_order_id": command["broker_order_id"],
            "strategy_parent_id": command["strategy_parent_id"],
            "event": "ack_assumed",
            "source_row_sequence": tick.source_row_sequence,
            "raw_time": tick.raw_time_label,
            "reason": "ack_before_next_source_row_eligibility",
            "requested_volume_steps": initial.volume_steps,
            "filled_volume_steps": 0,
            "residual_volume_steps": initial.volume_steps,
            "details_json": json.dumps({
                "trigger_price_micro": trigger,
                "active_after_source_row_sequence": tick.source_row_sequence,
                "ack_epoch_ms_assumed_utc_provisional": tick.assumed_utc_epoch_ms,
            }, sort_keys=True),
        })
        self._emit_state("hedge_stop_ack_assumed", tick, "pending_order_ack_precedes_eligibility", {
            "order_id": command["order_id"],
            "trigger_price_micro": trigger,
            "active_after_source_row_sequence": tick.source_row_sequence,
        })

    def _process_pending_stop(self, tick: QuoteEvent) -> None:
        pending = self.pending_stop
        if pending is None:
            return
        eligibility = self.stop_metadata_by_order_id.get(pending.order_id)
        required_ints = (
            eligibility.placement_source_row_sequence,
            eligibility.placement_epoch_ms_assumed_utc_provisional,
            eligibility.ack_source_row_sequence_assumed,
            eligibility.ack_epoch_ms_assumed_utc_provisional,
            eligibility.trigger_eligible_after_source_row_sequence,
            eligibility.trigger_eligible_after_epoch_ms_assumed_utc_provisional,
            eligibility.trigger_price_micro,
            eligibility.volume_steps,
        ) if isinstance(eligibility, StopEligibility) else ()
        metadata_valid = (
            isinstance(eligibility, StopEligibility)
            and eligibility.eligibility_basis == "resting_stop_cross_after_ack"
            and all(type(value) is int for value in required_ints)
            and eligibility.side in {"BUY", "SELL"}
            and eligibility.volume_steps > 0
            and eligibility.trigger_price_micro > 0
            and all(isinstance(value, str) and value for value in (
                eligibility.order_id,
                eligibility.broker_order_id,
                eligibility.strategy_parent_id,
                eligibility.source_position_id,
            ))
            and all((eligibility.placement_source_row_sequence <= eligibility.ack_source_row_sequence_assumed,
                     eligibility.placement_epoch_ms_assumed_utc_provisional <= eligibility.ack_epoch_ms_assumed_utc_provisional,
                     eligibility.ack_source_row_sequence_assumed == eligibility.trigger_eligible_after_source_row_sequence,
                     eligibility.ack_epoch_ms_assumed_utc_provisional == eligibility.trigger_eligible_after_epoch_ms_assumed_utc_provisional))
            and eligibility.order_id == pending.order_id
            and eligibility.broker_order_id == pending.broker_order_id
            and eligibility.strategy_parent_id == pending.strategy_parent_id
            and eligibility.source_position_id == pending.source_position_id
            and eligibility.side == pending.side
            and eligibility.trigger_price_micro == pending.trigger_price_micro
            and eligibility.volume_steps == pending.volume_steps
            and eligibility.placement_source_row_sequence == pending.created_source_row_sequence
            and eligibility.placement_epoch_ms_assumed_utc_provisional == pending.created_epoch_ms_assumed_utc_provisional
            and eligibility.ack_source_row_sequence_assumed == pending.active_after_source_row_sequence
            and eligibility.ack_epoch_ms_assumed_utc_provisional == pending.active_after_epoch_ms_assumed_utc_provisional
            and pending.eligibility == eligibility
        )
        if not metadata_valid:
            self.execution_degraded = True
            self._emit_state("execution_degraded", tick, "stop_fill_typed_ack_metadata_missing_or_mismatched", {
                "order_id": pending.order_id,
                "broker_order_id": pending.broker_order_id,
                "position_id": pending.source_position_id,
                "fail_closed": True,
            })
            return
        if (tick.source_row_sequence <= eligibility.trigger_eligible_after_source_row_sequence
                or tick.assumed_utc_epoch_ms <= eligibility.trigger_eligible_after_epoch_ms_assumed_utc_provisional):
            return
        crossed = tick.ask_micro >= pending.trigger_price_micro if pending.side == "BUY" else tick.bid_micro <= pending.trigger_price_micro
        if not crossed:
            return
        self.pending_stop = None
        self.stop_metadata_by_order_id.pop(pending.order_id, None)
        if self._reserved_position_slots() >= self.config.max_positions:
            self.execution_degraded = True
            self._emit_state("hedge_stop_fill_blocked_position_limit", tick, "position_cap_reconciliation_failure", {
                "open_positions": len(self.inventory),
                "reserved_position_slots": self._reserved_position_slots(),
                "max_positions": self.config.max_positions,
                "order_id": pending.order_id,
            })
            return
        command = {
            "order_id": pending.order_id,
            "broker_order_id": pending.broker_order_id,
            "strategy_parent_id": pending.strategy_parent_id,
            "command_type": "pending_stop_fill",
            "side": pending.side,
            "role": "hedge",
            "volume_steps": pending.volume_steps,
            "reason": "active_opposite_stop_triggered",
            "target_position_id": "",
            "retry_count": 0,
            "parent_order_id": "",
            "batch_id": "",
            "filled_volume_steps_total": pending.volume_steps,
            "bound_position_id": "",
            "position_opening_snapshot": None,
            "simulated_eligibility_basis": eligibility.eligibility_basis,
            "simulated_eligible_after_epoch_ms_assumed_utc": None,
            "simulated_eligible_after_raw_utc_assumption": None,
            "stop_placement_source_row_sequence": eligibility.placement_source_row_sequence,
            "stop_placement_epoch_ms_assumed_utc_provisional": eligibility.placement_epoch_ms_assumed_utc_provisional,
            "stop_ack_source_row_sequence_assumed": eligibility.ack_source_row_sequence_assumed,
            "stop_ack_epoch_ms_assumed_utc_provisional": eligibility.ack_epoch_ms_assumed_utc_provisional,
            "stop_trigger_eligible_after_source_row_sequence": eligibility.trigger_eligible_after_source_row_sequence,
            "stop_trigger_eligible_after_epoch_ms_assumed_utc_provisional": eligibility.trigger_eligible_after_epoch_ms_assumed_utc_provisional,
        }
        fill = pending.trigger_price_micro
        pos = self._create_position(command, tick, fill, fmt_price_micro(fill))
        pos.entry_filled_volume_steps_total = pending.volume_steps
        self._record_fill(command, tick, fill_steps=pending.volume_steps, fill_price_micro=fill, fill_price_raw=fmt_price_micro(fill), position=pos, position_volume_before=0, position_volume_after=pos.volume_steps, fill_kind="resting_stop")
        self.sink.emit("order_event", {
            "order_id": pending.order_id,
            "broker_order_id": pending.broker_order_id,
            "strategy_parent_id": pending.strategy_parent_id,
            "event": "filled",
            "source_row_sequence": tick.source_row_sequence,
            "raw_time": tick.raw_time_label,
            "reason": "resting_stop_trigger_quote_assumed",
            "requested_volume_steps": pending.volume_steps,
            "filled_volume_steps": pending.volume_steps,
            "residual_volume_steps": 0,
            "details_json": json.dumps({"fill_price_micro": fill, "source_position_id": pending.source_position_id}),
        })
        self._emit_state("entry_fill", tick, "hedge_stop_triggered", {"position_id": pos.position_id, "entry_price_micro": fill})
        self.metrics["hedge_stop_fills"] += 1

    def _cancel_pending_stop(self, tick: QuoteEvent, reason: str) -> None:
        if self.pending_stop is None:
            return
        order_id = self.pending_stop.order_id
        self.sink.emit("order_event", {
            "order_id": order_id,
            "broker_order_id": self.pending_stop.broker_order_id,
            "strategy_parent_id": self.pending_stop.strategy_parent_id,
            "event": "cancel_assumed",
            "source_row_sequence": tick.source_row_sequence,
            "raw_time": tick.raw_time_label,
            "reason": reason,
            "requested_volume_steps": self.pending_stop.volume_steps,
            "filled_volume_steps": 0,
            "residual_volume_steps": self.pending_stop.volume_steps,
            "details_json": "{}",
        })
        self.stop_metadata_by_order_id.pop(order_id, None)
        self.pending_stop = None
        self._emit_state("hedge_stop_cancelled", tick, reason, {"order_id": order_id})

    def _entry_command_identity_valid(self, command: dict[str, Any]) -> bool:
        """Fail closed unless residual volume, broker order, and bound position agree."""
        try:
            initial = int(command["initial_volume_steps"])
            remaining = int(command["volume_steps"])
            filled = int(command.get("filled_volume_steps_total", 0))
            broker_order_id = str(command["broker_order_id"])
            strategy_parent_id = str(command["strategy_parent_id"])
        except (KeyError, TypeError, ValueError):
            return False
        if not broker_order_id or not strategy_parent_id:
            return False
        if initial <= 0 or remaining < 0 or filled < 0 or initial != filled + remaining:
            return False

        bound_position_id = str(command.get("bound_position_id") or "")
        snapshot = command.get("position_opening_snapshot")
        same_broker_pending = [
            item for item in self.pending_orders
            if item.get("command_type") == "entry"
            and item.get("broker_order_id") == broker_order_id
            and item.get("order_id") != command.get("order_id")
        ]
        if same_broker_pending:
            return False
        same_broker_open = [
            position for position in self.inventory.values()
            if position.broker_order_id == broker_order_id
        ]
        if filled == 0:
            return not bound_position_id and snapshot is None and not same_broker_open
        if not bound_position_id or not isinstance(snapshot, dict):
            return False
        if (
            snapshot.get("position_id") != bound_position_id
            or snapshot.get("broker_order_id") != broker_order_id
            or snapshot.get("strategy_parent_id") != strategy_parent_id
            or snapshot.get("side") != command.get("side")
            or snapshot.get("role") != command.get("role")
        ):
            return False
        if len(same_broker_open) > 1:
            return False
        if same_broker_open and same_broker_open[0].position_id != bound_position_id:
            return False
        bound_open = self.inventory.get(bound_position_id)
        if bound_open is not None and (
            bound_open.broker_order_id != broker_order_id
            or bound_open.strategy_parent_id != strategy_parent_id
            or bound_open.side != command.get("side")
            or bound_open.role != command.get("role")
            or bound_open.entry_filled_volume_steps_total != filled
        ):
            return False
        return True

    def _entry_terminal_reconciled(self, command: dict[str, Any]) -> bool:
        if not self._entry_command_identity_valid(command):
            return False
        if int(command.get("filled_volume_steps_total", 0)) == 0:
            return not bool(command.get("bound_position_id"))
        bound_position_id = command.get("bound_position_id") or ""
        matches = [
            position for position in self.inventory.values()
            if position.position_id == bound_position_id
        ] if bound_position_id else []
        filled = int(command["filled_volume_steps_total"])
        return len(matches) <= 1 and all(
            position.broker_order_id == command["broker_order_id"]
            and position.strategy_parent_id == command["strategy_parent_id"]
            and position.side == command["side"]
            and position.role == command["role"]
            and position.entry_filled_volume_steps_total == filled
            for position in matches
        )

    def _retry_or_degrade(
        self,
        command: dict[str, Any],
        tick: QuoteEvent,
        *,
        position: Position | None,
        terminal_kind: str = "reject",
    ) -> None:
        if command["command_type"] == "exit":
            current = self.inventory.get(command["target_position_id"])
            reconciled = current is not None and current.volume_steps == command["volume_steps"]
        else:
            reconciled = self._entry_terminal_reconciled(command)
        reconciliation_event = (
            "reconciliation_after_reject"
            if terminal_kind == "reject"
            else "reconciliation_after_terminal_unfilled"
        )
        self.sink.emit("order_event", {
            "order_id": command["order_id"],
            "broker_order_id": command["broker_order_id"],
            "strategy_parent_id": command["strategy_parent_id"],
            "event": reconciliation_event,
            "source_row_sequence": tick.source_row_sequence,
            "raw_time": tick.raw_time_label,
            "reason": "terminal_unfilled_and_existing_deals_reconciled" if reconciled else "reconciliation_mismatch_fail_closed",
            "requested_volume_steps": command["volume_steps"],
            "filled_volume_steps": command.get("filled_volume_steps_total", 0),
            "residual_volume_steps": command["volume_steps"],
            "details_json": json.dumps({
                "reconciled": reconciled,
                "position_id": command.get("bound_position_id") or command["target_position_id"],
                "filled_volume_steps_total": command.get("filled_volume_steps_total", 0),
                "initial_volume_steps": command.get("initial_volume_steps", command["volume_steps"]),
                "residual_volume_steps": command["volume_steps"],
                "terminal_kind": terminal_kind,
            }),
        })
        if not reconciled:
            self.execution_degraded = True
            self._emit_state("execution_degraded", tick, "reconciliation_mismatch", {"order_id": command["order_id"]})
            return
        if command["retry_count"] >= 1:
            self.execution_degraded = True
            self._emit_state("execution_degraded", tick, "bounded_retry_exhausted", {"order_id": command["order_id"]})
            return
        retry = self._new_order(
            tick,
            command_type=command["command_type"],
            side=command["side"],
            role=command["role"],
            volume_steps=command["volume_steps"],
            reason=command["reason"],
            strategy_parent_id=command["strategy_parent_id"],
            target_position_id=command["target_position_id"],
            retry_count=command["retry_count"] + 1,
            parent_order_id=command["order_id"],
            batch_id=command["batch_id"],
        )
        if command["command_type"] == "exit" and retry["target_position_id"] in self.inventory:
            self.inventory[retry["target_position_id"]].close_pending_order_id = retry["order_id"]
        if command["command_type"] == "entry":
            queued = self._queue_market_order(tick, retry)
            if not queued:
                self.execution_degraded = True
                self._emit_state("execution_degraded", tick, "bounded_entry_retry_blocked_by_position_cap", {
                    "parent_order_id": command["order_id"],
                    "retry_order_id": retry["order_id"],
                    "broker_order_id": retry["broker_order_id"],
                    "strategy_parent_id": retry["strategy_parent_id"],
                })
                return
        else:
            self.pending_orders.append(retry)
        if self.close_batch is not None:
            self.close_batch["active_order_id"] = retry["order_id"]
        self._emit_state("bounded_retry_queued", tick, "single_retry_after_internal_state_reconciliation", {"parent_order_id": command["order_id"], "retry_order_id": retry["order_id"]})

    def _issue_next_batch_close(self, tick: QuoteEvent) -> None:
        if self.close_batch is None or self.close_batch["active_order_id"]:
            return
        while self.close_batch["remaining_targets"]:
            position_id = self.close_batch["remaining_targets"][0]
            pos = self.inventory.get(position_id)
            if pos is None or pos.volume_steps <= 0:
                self.close_batch["remaining_targets"].pop(0)
                continue
            command = self._new_order(
                tick,
                command_type="exit",
                side="SELL" if pos.side == "BUY" else "BUY",
                role=pos.role,
                volume_steps=pos.volume_steps,
                reason=self.close_batch["reason"],
                target_position_id=position_id,
                batch_id=self.close_batch["batch_id"],
            )
            self.close_batch["active_order_id"] = command["order_id"]
            self._queue_market_order(tick, command)
            return
        batch = self.close_batch
        if not batch["remaining_targets"]:
            self.close_batch = None
            active_entry_order = any(item["command_type"] == "entry" for item in self.pending_orders)
            if not self.inventory and not active_entry_order and batch["kind"].startswith("basket_"):
                self.last_full_settlement_ms = tick.assumed_utc_epoch_ms
                self.metrics["basket_full_settlements"] += 1
                self._finalize_flat(tick, batch["reason"])
            self._emit_state("close_batch_complete", tick, batch["reason"], {"batch_id": batch["batch_id"], "remaining_positions": len(self.inventory)})

    def _begin_close_batch(self, tick: QuoteEvent, *, kind: str, reason: str, position_ids: list[str], details: dict[str, Any]) -> None:
        if self.close_batch is not None:
            return
        targets = [pid for pid in position_ids if pid in self.inventory]
        targets.sort(key=lambda pid: self.inventory[pid].creation_sequence)
        self.batch_sequence += 1
        batch_id = f"SIM-B{self.batch_sequence:05d}"
        self.close_batch = {
            "batch_id": batch_id,
            "kind": kind,
            "reason": reason,
            "remaining_targets": targets,
            "active_order_id": "",
            "partial_fill_counts": Counter(),
        }
        self._cancel_pending_stop(tick, "global_close_batch_cancels_untriggered_hedge_stop")
        self._emit_state("close_batch_started", tick, reason, {"batch_id": batch_id, "target_position_ids": targets, **details})
        self._issue_next_batch_close(tick)

    def _begin_single_close(self, tick: QuoteEvent, pos: Position, reason: str) -> None:
        if self.close_batch is not None or pos.close_pending_order_id:
            return
        command = self._new_order(
            tick,
            command_type="exit",
            side="SELL" if pos.side == "BUY" else "BUY",
            role=pos.role,
            volume_steps=pos.volume_steps,
            reason=reason,
            target_position_id=pos.position_id,
        )
        self._queue_market_order(tick, command)
        self.metrics[f"single_close_requested:{reason}"] += 1

    def _finalize_flat(self, tick: QuoteEvent, reason: str) -> None:
        active_entries = [item for item in self.pending_orders if item["command_type"] == "entry"]
        if active_entries:
            self._emit_state("book_flat_blocked_active_entry_order", tick, "position_identity_may_reopen", {
                "active_broker_order_ids": [item["broker_order_id"] for item in active_entries],
                "reserved_position_slots": self._reserved_position_slots(),
                "max_positions": self.config.max_positions,
            })
            return
        self.p1_anchor_by_side.clear()
        self.last_p1_fill_by_side.clear()
        self.last_p2_fill_by_side.clear()
        self._cancel_pending_stop(tick, "book_flat_after_confirmed_full_settlement")
        self._emit_state("book_flat", tick, reason, {
            "last_full_settlement_ms_assumed_utc": self.last_full_settlement_ms,
            "cooldown_ms": self.config.settlement_cooldown_ms,
        })

    def _defer_unresolved_order_outcome(
        self,
        command: dict[str, Any],
        tick: QuoteEvent,
        *,
        event: str,
        reason: str,
    ) -> None:
        self.pending_orders.append(command)
        command["status"] = "active_unresolved"
        self.sink.emit("order_event", {
            "order_id": command["order_id"],
            "broker_order_id": command["broker_order_id"],
            "strategy_parent_id": command["strategy_parent_id"],
            "event": event,
            "source_row_sequence": tick.source_row_sequence,
            "raw_time": tick.raw_time_label,
            "reason": reason,
            "requested_volume_steps": command["volume_steps"],
            "filled_volume_steps": 0,
            "residual_volume_steps": command["volume_steps"],
            "details_json": json.dumps({
                "reservation_retained": True,
                "retry_forbidden_until_terminal_confirmation": True,
                "filled_volume_steps_total": command.get("filled_volume_steps_total", 0),
                "bound_position_id": command.get("bound_position_id", ""),
            }),
        })
        self._emit_state("market_order_outcome_unresolved", tick, reason, {
            "order_id": command["order_id"],
            "broker_order_id": command["broker_order_id"],
            "strategy_parent_id": command["strategy_parent_id"],
            "reservation_retained": True,
            "retry_forbidden": True,
            "open_positions": len(self.inventory),
            "reserved_position_slots": self._reserved_position_slots(),
        })

    def _terminal_unfilled_order(
        self,
        command: dict[str, Any],
        tick: QuoteEvent,
        *,
        outcome: str,
    ) -> None:
        confirmed = outcome in {"cancel_confirmed", "timeout_confirmed_unfilled"}
        if not confirmed:
            raise RuntimeError(f"nonterminal order outcome sent to terminal handler: {outcome}")
        event = "cancel_confirmed_unfilled" if outcome == "cancel_confirmed" else "timeout_confirmed_unfilled"
        self.sink.emit("order_event", {
            "order_id": command["order_id"],
            "broker_order_id": command["broker_order_id"],
            "strategy_parent_id": command["strategy_parent_id"],
            "event": event,
            "source_row_sequence": tick.source_row_sequence,
            "raw_time": tick.raw_time_label,
            "reason": "synthetic_confirmed_terminal_unfilled_residual",
            "requested_volume_steps": command["volume_steps"],
            "filled_volume_steps": 0,
            "residual_volume_steps": command["volume_steps"],
            "details_json": json.dumps({
                "confirmed_terminal_unfilled": True,
                "filled_volume_steps_total": command.get("filled_volume_steps_total", 0),
                "bound_position_id": command.get("bound_position_id", ""),
                "timer_seconds_assumed": None,
            }),
        })
        self._retry_or_degrade(command, tick, position=None, terminal_kind=outcome)

    def _process_command_fill(self, command: dict[str, Any], tick: QuoteEvent) -> None:
        if command.get("status") == "active_identity_mismatch":
            self.pending_orders.append(command)
            return
        if command["command_type"] == "entry" and not self._entry_command_identity_valid(command):
            command["status"] = "active_identity_mismatch"
            self.pending_orders.append(command)
            self.execution_degraded = True
            self.sink.emit("order_event", {
                "order_id": command["order_id"],
                "broker_order_id": command["broker_order_id"],
                "strategy_parent_id": command["strategy_parent_id"],
                "event": "entry_identity_quarantined",
                "source_row_sequence": tick.source_row_sequence,
                "raw_time": tick.raw_time_label,
                "reason": "entry_command_position_identity_mismatch_reservation_retained",
                "requested_volume_steps": command.get("initial_volume_steps", command["volume_steps"]),
                "filled_volume_steps": command.get("filled_volume_steps_total", 0),
                "residual_volume_steps": command["volume_steps"],
                "details_json": json.dumps({
                    "reservation_retained": True,
                    "retry_forbidden": True,
                    "bound_position_id": command.get("bound_position_id", ""),
                }),
            })
            self._emit_state("execution_degraded", tick, "entry_command_position_identity_mismatch", {
                "order_id": command["order_id"],
                "broker_order_id": command["broker_order_id"],
                "strategy_parent_id": command["strategy_parent_id"],
                "reserved_position_slots": self._reserved_position_slots(),
            })
            return
        outcome, planned_steps = self.execution_plan.next(command["reason"], command["role"])
        if outcome == "cancel_requested":
            self._defer_unresolved_order_outcome(
                command, tick, event="cancel_requested", reason="cancel_request_is_not_terminal_confirmation",
            )
            return
        if outcome == "timeout_unconfirmed":
            self._defer_unresolved_order_outcome(
                command, tick, event="timeout_unconfirmed", reason="timeout_without_terminal_order_state",
            )
            return
        if outcome in {"cancel_confirmed", "timeout_confirmed_unfilled"}:
            self._terminal_unfilled_order(command, tick, outcome=outcome)
            return
        if outcome == "reject":
            self.sink.emit("order_event", {
                "order_id": command["order_id"],
                "broker_order_id": command["broker_order_id"],
                "strategy_parent_id": command["strategy_parent_id"],
                "event": "rejected",
                "source_row_sequence": tick.source_row_sequence,
                "raw_time": tick.raw_time_label,
                "reason": "synthetic_or_engine_execution_reject",
                "requested_volume_steps": command["volume_steps"],
                "filled_volume_steps": 0,
                "residual_volume_steps": command["volume_steps"],
                "details_json": "{}",
            })
            self._retry_or_degrade(command, tick, position=None)
            return
        request_before_fill = int(command["volume_steps"])
        fill_steps = command["volume_steps"] if planned_steps is None else min(command["volume_steps"], planned_steps)
        if fill_steps <= 0:
            self.execution_degraded = True
            command["status"] = "active_nonpositive_fill_unresolved"
            self.pending_orders.append(command)
            self.sink.emit("order_event", {
                "order_id": command["order_id"],
                "broker_order_id": command["broker_order_id"],
                "strategy_parent_id": command["strategy_parent_id"],
                "event": "nonpositive_fill_unresolved",
                "source_row_sequence": tick.source_row_sequence,
                "raw_time": tick.raw_time_label,
                "reason": "zero_fill_is_not_terminal_confirmation",
                "requested_volume_steps": command["initial_volume_steps"],
                "filled_volume_steps": command.get("filled_volume_steps_total", 0),
                "residual_volume_steps": command["volume_steps"],
                "details_json": json.dumps({"reservation_retained": True, "retry_forbidden": True}),
            })
            self._emit_state("execution_degraded", tick, "nonpositive_simulated_fill", {"order_id": command["order_id"]})
            return
        if command["command_type"] == "entry":
            bound_position_id = command.get("bound_position_id") or ""
            target_is_open = bool(bound_position_id and bound_position_id in self.inventory)
            projected_slots = self._reserved_position_slots() + (0 if target_is_open else 1)
            if projected_slots > self.config.max_positions:
                self.execution_degraded = True
                self._emit_state("entry_fill_blocked_position_limit", tick, "reserved_position_slot_invariant_failed", {
                    "open_positions": len(self.inventory),
                    "reserved_position_slots": self._reserved_position_slots(),
                    "projected_reserved_position_slots": projected_slots,
                    "max_positions": self.config.max_positions,
                    "order_id": command["order_id"],
                })
                self.pending_orders.append(command)
                return
            price = tick.ask_micro if command["side"] == "BUY" else tick.bid_micro
            price += self.config.entry_slippage_micro if command["side"] == "BUY" else -self.config.entry_slippage_micro
            raw_price = fmt_price_micro(price)
            pos = self.inventory.get(bound_position_id) if bound_position_id else None
            existing_open_position = pos is not None
            if not existing_open_position:
                entry_command = dict(command)
                entry_command["volume_steps"] = fill_steps
                pos = self._create_position(entry_command, tick, price, raw_price, volume_steps=fill_steps)
                if not bound_position_id:
                    command["bound_position_id"] = pos.position_id
                    command["position_opening_snapshot"] = {
                        "position_id": pos.position_id,
                        "creation_sequence": pos.creation_sequence,
                        "entry_source_row_sequence": pos.entry_source_row_sequence,
                        "entry_raw_time": pos.entry_raw_time,
                        "entry_epoch_ms_assumed_utc": pos.entry_epoch_ms_assumed_utc,
                        "broker_order_id": command["broker_order_id"],
                        "strategy_parent_id": command["strategy_parent_id"],
                        "side": command["side"],
                        "role": command["role"],
                    }
                before = 0
                pos.entry_filled_volume_steps_total += fill_steps
            else:
                before = pos.volume_steps
                before_price = pos.entry_price_micro
                total_volume = before + fill_steps
                weighted_numerator = before_price * before + price * fill_steps
                weighted_price = (weighted_numerator + total_volume // 2) // total_volume
                pos.entry_price_micro = weighted_price
                pos.entry_price_raw = fmt_price_micro(weighted_price)
                pos.volume_steps = before + fill_steps
                pos.entry_filled_volume_steps_total += fill_steps
            # A still-active remainder may reopen its original position ID
            # after the partial position was fully closed. _create_position
            # restores the original ID/opening metadata in that case.
            command["filled_volume_steps_total"] = int(command.get("filled_volume_steps_total", 0)) + fill_steps
            pos.entry_filled_volume_steps_total = command["filled_volume_steps_total"]
            if pos.is_p1:
                self.p1_anchor_by_side.setdefault(pos.side, price)
                self.last_p1_fill_by_side[pos.side] = price
            if pos.role == "p2":
                self.last_p2_fill_by_side[pos.side] = price
            position_after = pos.volume_steps
            self._record_fill(command, tick, fill_steps=fill_steps, fill_price_micro=price, fill_price_raw=raw_price, position=pos, position_volume_before=before, position_volume_after=position_after, fill_kind="entry")
            command["volume_steps"] = request_before_fill - fill_steps
            command["status"] = "partially_filled_active" if command["volume_steps"] else "filled"
            self.sink.emit("order_event", {
                "order_id": command["order_id"],
                "broker_order_id": command["broker_order_id"],
                "strategy_parent_id": command["strategy_parent_id"],
                "event": "filled" if command["volume_steps"] == 0 else "partial_fill",
                "source_row_sequence": tick.source_row_sequence,
                "raw_time": tick.raw_time_label,
                "reason": "entry_confirmed_by_simulator_quote",
                "requested_volume_steps": request_before_fill,
                "filled_volume_steps": fill_steps,
                "residual_volume_steps": command["volume_steps"],
                "details_json": json.dumps({
                    "position_id": pos.position_id,
                    "entry_price_micro_deal": price,
                    "entry_price_micro_weighted": pos.entry_price_micro,
                    "position_volume_before_steps": before,
                    "position_volume_after_steps": position_after,
                    "filled_volume_steps_total": command["filled_volume_steps_total"],
                }),
            })
            self._emit_state("entry_fill", tick, command["reason"], {
                "position_id": pos.position_id,
                "broker_order_id": command["broker_order_id"],
                "strategy_parent_id": command["strategy_parent_id"],
                "role": pos.role,
                "entry_price_micro_deal": price,
                "entry_price_micro_weighted": pos.entry_price_micro,
                "position_volume_steps": position_after,
            })
            self.metrics[f"entry_fills:{pos.role}"] += 1
            if pos.role == "initial" and not existing_open_position:
                self._make_hedge_stop(pos, tick)
            if command["volume_steps"] > 0:
                self.pending_orders.append(command)
            return

        position_id = command["target_position_id"]
        pos = self.inventory.get(position_id)
        if pos is None:
            self.execution_degraded = True
            self._emit_state("execution_degraded", tick, "close_target_missing_at_fill", {"order_id": command["order_id"], "position_id": position_id})
            return
        before = pos.volume_steps
        if fill_steps > before:
            self.execution_degraded = True
            self._emit_state("execution_degraded", tick, "close_overfill_blocked", {"order_id": command["order_id"], "position_id": position_id})
            return
        price = self._side_exit_price(pos, tick)
        self._record_fill(command, tick, fill_steps=fill_steps, fill_price_micro=price, fill_price_raw=fmt_price_micro(price), position=pos, position_volume_before=before, position_volume_after=before - fill_steps, fill_kind="exit")
        pos.volume_steps -= fill_steps
        pos.close_pending_order_id = None
        self.sink.emit("order_event", {
            "order_id": command["order_id"],
            "broker_order_id": command["broker_order_id"],
            "strategy_parent_id": command["strategy_parent_id"],
            "event": "filled" if fill_steps == command["volume_steps"] else "partial_fill",
            "source_row_sequence": tick.source_row_sequence,
            "raw_time": tick.raw_time_label,
            "reason": "exit_confirmed_by_simulator_quote",
            "requested_volume_steps": command["volume_steps"],
            "filled_volume_steps": fill_steps,
            "residual_volume_steps": before - fill_steps,
            "details_json": json.dumps({"position_id": position_id, "exit_price_micro": price}),
        })
        if pos.volume_steps == 0:
            del self.inventory[position_id]
            if self.close_batch is None and pos.role == "initial":
                self._cancel_pending_stop(tick, "initial_ticket_closed_before_hedge_stop_trigger")
            if self.close_batch is not None and self.close_batch["remaining_targets"] and self.close_batch["remaining_targets"][0] == position_id:
                self.close_batch["remaining_targets"].pop(0)
                self.close_batch["active_order_id"] = ""
        self._emit_state("exit_fill", tick, command["reason"], {
            "position_id": position_id,
            "volume_before_steps": before,
            "volume_after_steps": before - fill_steps,
            "exit_price_micro": price,
        })
        self.metrics[f"exit_fills:{command['reason']}"] += 1
        if self.close_batch is not None:
            if pos.volume_steps > 0:
                self.close_batch["partial_fill_counts"][position_id] += 1
                if self.close_batch["partial_fill_counts"][position_id] > 8:
                    self.execution_degraded = True
                    self._emit_state("execution_degraded", tick, "partial_fill_cap_exceeded", {"position_id": position_id})
                    return
                self.close_batch["active_order_id"] = ""
            self._issue_next_batch_close(tick)
        elif pos.volume_steps > 0:
            count = self.single_close_retries.get(position_id, 0) + 1
            self.single_close_retries[position_id] = count
            if count > 8:
                self.execution_degraded = True
                self._emit_state("execution_degraded", tick, "partial_fill_cap_exceeded", {"position_id": position_id})
                return
            self._begin_single_close(tick, pos, command["reason"])
        elif not self.inventory:
            # Individual stops and P2 exits explicitly do not start the 15min
            # full-settlement cooldown in this hypothesis.
            self._finalize_flat(tick, command["reason"])

    def _process_due_market_orders(self, tick: QuoteEvent) -> None:
        if not self.pending_orders:
            return
        count_at_start = len(self.pending_orders)
        for _ in range(count_at_start):
            command = self.pending_orders.popleft()
            eligible_epoch = command.get("simulated_eligible_after_epoch_ms_assumed_utc")
            eligible_raw = command.get("simulated_eligible_after_raw_utc_assumption")
            if (command.get("simulated_eligibility_basis") != "market_latency_after_command"
                    or type(eligible_epoch) is not int
                    or eligible_raw != format_assumed_utc_ms(eligible_epoch)):
                if command.get("status") != "blocked_unknown_market_eligibility_metadata":
                    command["status"] = "blocked_unknown_market_eligibility_metadata"
                    self.execution_degraded = True
                    self._emit_state("execution_degraded", tick, "market_fill_typed_eligibility_metadata_missing_or_mismatched", {
                        "order_id": command.get("order_id"),
                        "broker_order_id": command.get("broker_order_id"),
                        "fail_closed": True,
                    })
                self.pending_orders.append(command)
                continue
            if tick.assumed_utc_epoch_ms <= eligible_epoch:
                self.pending_orders.append(command)
                continue
            self._process_command_fill(command, tick)

    def _manage_initial_protection(self, tick: QuoteEvent) -> None:
        if self.close_batch is not None:
            return
        for pos in list(self.inventory.values()):
            if pos.role != "initial" or pos.close_pending_order_id:
                continue
            favorable = tick.bid_micro - pos.entry_price_micro if pos.side == "BUY" else pos.entry_price_micro - tick.ask_micro
            current_favorable_quote = tick.bid_micro if pos.side == "BUY" else tick.ask_micro
            if pos.side == "BUY":
                pos.best_favorable_micro = max(pos.best_favorable_micro, current_favorable_quote)
            else:
                pos.best_favorable_micro = min(pos.best_favorable_micro, current_favorable_quote)
            if favorable >= 1_000 * self.config.point_micro:
                self._begin_single_close(tick, pos, "initial_take_profit_1000_points")
                continue
            if favorable >= 70 * self.config.point_micro:
                be_stop = pos.entry_price_micro + 30 * self.config.point_micro if pos.side == "BUY" else pos.entry_price_micro - 30 * self.config.point_micro
                improves = pos.stop_price_micro is None or (be_stop > pos.stop_price_micro if pos.side == "BUY" else be_stop < pos.stop_price_micro)
                if improves:
                    pos.stop_price_micro = be_stop
                    pos.be_armed = True
                    self._emit_state("breakeven_stop_armed", tick, "70_POINT_START_30_POINT_OFFSET", {"position_id": pos.position_id, "stop_price_micro": be_stop})
            if favorable >= 150 * self.config.point_micro:
                trailing_stop = pos.best_favorable_micro - 50 * self.config.point_micro if pos.side == "BUY" else pos.best_favorable_micro + 50 * self.config.point_micro
                improves_by_step = pos.stop_price_micro is None or (
                    trailing_stop - pos.stop_price_micro >= 10 * self.config.point_micro
                    if pos.side == "BUY" else pos.stop_price_micro - trailing_stop >= 10 * self.config.point_micro
                )
                if improves_by_step:
                    pos.stop_price_micro = trailing_stop
                    pos.trailing_armed = True
                    self._emit_state("trailing_stop_armed", tick, "150_START_50_DISTANCE_10_STEP", {"position_id": pos.position_id, "stop_price_micro": trailing_stop})
            if pos.stop_price_micro is not None:
                stop_hit = tick.bid_micro <= pos.stop_price_micro if pos.side == "BUY" else tick.ask_micro >= pos.stop_price_micro
                if stop_hit:
                    reason = "initial_trailing_stop" if pos.trailing_armed else "initial_breakeven_stop"
                    self._begin_single_close(tick, pos, reason)

    def _p2_recovery_metrics(self, tick: QuoteEvent) -> dict[str, Any]:
        p2_positions = [p for p in self.inventory.values() if p.role == "p2"]
        p1_positions = [p for p in self.inventory.values() if p.is_p1]
        gross_p2 = sum(self._mark_currency_micro(p, tick) for p in p2_positions)
        p2_volume_steps = sum(p.volume_steps for p in p2_positions)
        p2_fee = self._external_fee_currency_micro(p2_volume_steps)
        net_p2 = gross_p2 - p2_fee
        p1_marks = [(self._mark_currency_micro(p, tick), p) for p in p1_positions]
        negative = [(mark, p) for mark, p in p1_marks if mark < 0]
        worst = min(negative, key=lambda item: (item[0], item[1].creation_sequence)) if negative else None
        worst_mark = worst[0] if worst else 0
        worst_fee = self._external_fee_currency_micro(worst[1].volume_steps) if worst else 0
        selected_steps = p2_volume_steps + (worst[1].volume_steps if worst else 0)
        score = net_p2 + min(0, worst_mark) - worst_fee
        return {
            "p2_positions": p2_positions,
            "gross_p2_currency_micro": gross_p2,
            "net_p2_currency_micro": net_p2,
            "p2_fee_currency_micro": p2_fee,
            "worst_p1": worst[1] if worst else None,
            "worst_p1_gross_currency_micro": worst_mark,
            "worst_p1_fee_currency_micro": worst_fee,
            "selected_volume_steps": selected_steps,
            "recovery_score_net_currency_micro": score,
        }

    def _evaluate_recovery_and_basket(self, tick: QuoteEvent, features: dict[str, Any] | None) -> None:
        if not self.inventory or self.close_batch is not None or any(p.close_pending_order_id for p in self.inventory.values()):
            return
        recovery = self._p2_recovery_metrics(tick)
        recovery_trigger = bool(recovery["p2_positions"]) and recovery["recovery_score_net_currency_micro"] >= self.config.p2_recovery_target_currency_micros
        gross_book_mark = sum(self._mark_currency_micro(p, tick) for p in self.inventory.values())
        total, _, _, volume_steps = self._position_counts()
        book_fee = self._external_fee_currency_micro(volume_steps)
        basket_net = gross_book_mark - book_fee
        basket_target = max(
            self.config.basket_minimum_settlement_currency_micros,
            self.config.basket_profit_per_position_currency_micros * total,
        )
        basket_take = basket_net >= basket_target
        if recovery_trigger and basket_take:
            self.metrics["exit_priority_competition"] += 1
            self._emit_branch(tick, "exit_priority", "H2_P2_RECOVERY_SELECTED", "simulator_priority_assumption_collision_unresolved", features, {
                "basket_take": basket_take,
                "basket_gross_currency_micro": gross_book_mark,
                "basket_fee_currency_micro": book_fee,
                "basket_net_currency_micro": basket_net,
                "recovery_score_net_currency_micro": recovery["recovery_score_net_currency_micro"],
            })
        if recovery_trigger:
            targets = [p.position_id for p in recovery["p2_positions"]]
            if recovery["worst_p1"] is not None:
                targets.append(recovery["worst_p1"].position_id)
            self._emit_branch(tick, "p2_recovery", "close_selected_inventory", "H2_net_p2_plus_worst_negative_p1_at_least_0_5", features, {
                "p2_position_ids": [p.position_id for p in recovery["p2_positions"]],
                "worst_p1_position_id": recovery["worst_p1"].position_id if recovery["worst_p1"] else "",
                "gross_p2_currency_micro": recovery["gross_p2_currency_micro"],
                "net_p2_currency_micro": recovery["net_p2_currency_micro"],
                "p2_fee_currency_micro": recovery["p2_fee_currency_micro"],
                "worst_p1_gross_currency_micro": recovery["worst_p1_gross_currency_micro"],
                "worst_p1_fee_currency_micro": recovery["worst_p1_fee_currency_micro"],
                "selected_volume_steps": recovery["selected_volume_steps"],
                "recovery_score_net_currency_micro": recovery["recovery_score_net_currency_micro"],
                "target_currency_micro": self.config.p2_recovery_target_currency_micros,
            })
            self._begin_close_batch(tick, kind="h2_p2_recovery", reason="H2_P2_RECOVERY", position_ids=targets, details={
                "net_p2_currency_micro": recovery["net_p2_currency_micro"],
                "worst_p1_position_id": recovery["worst_p1"].position_id if recovery["worst_p1"] else "",
                "recovery_score_net_currency_micro": recovery["recovery_score_net_currency_micro"],
            })
            return
        if basket_take:
            reason = "whole_book_net_take_profit_hypothesis"
            self._emit_branch(tick, "basket_exit", "close_all", reason, features, {
                "basket_gross_currency_micro": gross_book_mark,
                "basket_fee_currency_micro": book_fee,
                "basket_net_currency_micro": basket_net,
                "basket_target_currency_micro": basket_target,
                "open_positions": total,
                "open_volume_steps": volume_steps,
            })
            self._begin_close_batch(tick, kind="basket_take_profit", reason=reason, position_ids=list(self.inventory), details={
                "basket_gross_currency_micro": gross_book_mark,
                "basket_fee_currency_micro": book_fee,
                "basket_net_currency_micro": basket_net,
                "basket_target_currency_micro": basket_target,
            })

    def _initial_signal(self, tick: QuoteEvent, features: dict[str, Any]) -> tuple[str | None, str, dict[str, Any]]:
        if self._inside_initial_pause(tick):
            return None, "raw_utc_assumed_initial_pause", {}
        if self.shock_locked:
            return None, "shock_lock_initial_only", {}
        upper = Decimal(features["donchian_upper_micro"])
        lower = Decimal(features["donchian_lower_micro"])
        atr14 = features["atr14_micro"]
        sell_edge = Decimal(tick.bid_micro) >= upper - self.config.edge_atr_fraction * atr14
        buy_edge = Decimal(tick.bid_micro) <= lower + self.config.edge_atr_fraction * atr14
        details = {"sell_edge": sell_edge, "buy_edge": buy_edge}
        if sell_edge and buy_edge:
            return None, "both_edges_true_ambiguous", details
        if sell_edge:
            return "SELL", "donchian_upper_edge", details
        if buy_edge:
            return "BUY", "donchian_lower_edge", details
        return None, "central_channel_no_order", details

    def _breakout_side(self, tick: QuoteEvent, features: dict[str, Any]) -> tuple[str | None, str]:
        last_high = features["last_completed_bar_high_micro"]
        last_low = features["last_completed_bar_low_micro"]
        if tick.bid_micro > last_high:
            return "BUY", "strict_break_above_last_completed_high"
        if tick.bid_micro < last_low:
            return "SELL", "strict_break_below_last_completed_low"
        return None, "no_strict_breakout"

    def _p1_spacing_ok(self, side: str, tick: QuoteEvent) -> bool:
        previous = self.last_p1_fill_by_side.get(side)
        if previous is None:
            return False
        difference = tick.bid_micro - previous if side == "BUY" else previous - tick.bid_micro
        return difference >= self.config.p1_breakout_spacing_points * self.config.point_micro

    def _p2_spacing_ok(self, side: str, tick: QuoteEvent) -> tuple[bool, str]:
        anchor = self.p1_anchor_by_side.get(side)
        if anchor is None:
            return False, "no_same_side_p1_anchor"
        reference = self.last_p2_fill_by_side.get(side, anchor)
        difference = tick.bid_micro - reference if side == "BUY" else reference - tick.bid_micro
        return difference >= self.config.p2_spacing_points * self.config.point_micro, "same_side_anchor_or_prior_p2_400_point_spacing"

    def _has_pending_entry(self, role: str, side: str) -> bool:
        return any(item["command_type"] == "entry" and item["role"] == role and item["side"] == side for item in self.pending_orders)

    def _evaluate_entries(self, tick: QuoteEvent, features: dict[str, Any] | None, new_minute: bool) -> None:
        if features is None:
            if new_minute:
                self._emit_branch(tick, "features", "no_order", "insufficient_m1_warmup")
            return
        self.last_features = features
        if self.execution_degraded:
            if new_minute:
                self._emit_branch(tick, "admission", "no_order", "execution_degraded_fail_closed", features)
            return
        if self.close_batch is not None or any(p.close_pending_order_id for p in self.inventory.values()):
            if new_minute:
                self._emit_branch(tick, "admission", "no_order", "close_batch_or_ticket_close_pending", features)
            return
        if self.pending_orders:
            if new_minute:
                self._emit_branch(tick, "admission", "no_order", "market_command_pending_duplicate_suppression", features)
            return
        if not self.inventory:
            if self._cooldown_active(tick):
                if new_minute:
                    self._emit_branch(tick, "initial", "no_order", "15_min_full_settlement_cooldown", features)
                return
            if self._reserved_position_slots() >= self.config.max_positions:
                if new_minute:
                    self._emit_branch(tick, "admission", "no_order", "max_positions_reserved_slot_cap", features, {
                        "reserved_position_slots": self._reserved_position_slots(),
                        "max_positions": self.config.max_positions,
                    })
                return
            side, reason, details = self._initial_signal(tick, features)
            if side is None:
                if new_minute or reason == "both_edges_true_ambiguous":
                    self._emit_branch(tick, "initial", "no_order", reason, features, details)
                return
            self._emit_branch(tick, "initial", "submit_market_entry", reason, features, details)
            command = self._new_order(tick, command_type="entry", side=side, role="initial", volume_steps=self.config.initial_lot_steps, reason="initial_" + reason)
            self._queue_market_order(tick, command)
            return

        breakout_side, breakout_reason = self._breakout_side(tick, features)
        p1_candidate = breakout_side is not None and self._p1_spacing_ok(breakout_side, tick)
        p1_reason = breakout_reason if p1_candidate else (
            "same_side_p1_spacing_below_400_points" if breakout_side is not None else "no_strict_breakout"
        )
        _, open_p1, _, _ = self._position_counts()
        p2_side = breakout_side
        p2_spacing, p2_spacing_reason = self._p2_spacing_ok(p2_side, tick) if p2_side else (False, "no_breakout_side")
        p2_candidate = p2_side is not None and open_p1 > self.config.p2_start_positions_strictly_greater_than and p2_spacing
        if p2_side and open_p1 <= self.config.p2_start_positions_strictly_greater_than and p1_candidate:
            p2_gate_reason = "P1_open_count_must_be_strictly_greater_than_2"
        else:
            p2_gate_reason = p2_spacing_reason
        if (p1_candidate or p2_candidate) and self._reserved_position_slots() >= self.config.max_positions:
            self._emit_branch(tick, "admission", "no_order", "max_positions_reserved_slot_cap", features, {
                "reserved_position_slots": self._reserved_position_slots(),
                "max_positions": self.config.max_positions,
                "p1_candidate": p1_candidate,
                "p2_candidate": p2_candidate,
            })
            return
        if p1_candidate and p2_candidate:
            self._emit_branch(tick, "add_priority", "no_order", "simultaneous_P1_P2_branch_priority_unresolved", features, {
                "side": breakout_side,
                "p1_reason": p1_reason,
                "p2_reason": p2_gate_reason,
                "open_p1_count": open_p1,
            })
            return
        if p2_candidate and not self._has_pending_entry("p2", p2_side):
            self._emit_branch(tick, "p2_add", "submit_market_entry", "P1_count_gt_2_and_400_point_same_side_spacing", features, {
                "side": p2_side,
                "open_p1_count": open_p1,
                "spacing_reason": p2_gate_reason,
            })
            command = self._new_order(tick, command_type="entry", side=p2_side, role="p2", volume_steps=self.config.p2_lot_steps, reason="p2_same_side_spacing")
            self._queue_market_order(tick, command)
            return
        if p1_candidate and not self._has_pending_entry("p1_breakout", breakout_side):
            self._emit_branch(tick, "p1_add", "submit_market_entry", p1_reason, features, {
                "side": breakout_side,
                "spacing_points": self.config.p1_breakout_spacing_points,
                "open_p1_count": open_p1,
            })
            command = self._new_order(tick, command_type="entry", side=breakout_side, role="p1_breakout", volume_steps=self.config.initial_lot_steps, reason="p1_strict_breakout_spacing")
            self._queue_market_order(tick, command)
            return
        if new_minute and breakout_side is not None:
            self._emit_branch(tick, "add_gate", "no_order", p2_gate_reason if open_p1 <= 2 else p1_reason, features, {
                "side": breakout_side,
                "open_p1_count": open_p1,
                "p1_spacing_ok": p1_candidate,
                "p2_spacing_ok": p2_spacing,
            })

    def process(self, tick: QuoteEvent) -> None:
        if self.last_source_row_sequence is not None and tick.source_row_sequence <= self.last_source_row_sequence:
            raise QuoteFault("source row sequence did not increase")
        if self.last_epoch_ms is not None:
            if tick.assumed_utc_epoch_ms < self.last_epoch_ms:
                raise QuoteFault("source timestamp reversed; input is never sorted")
            gap = tick.assumed_utc_epoch_ms - self.last_epoch_ms
            if gap == 0:
                self.source_equal_timestamp_pairs += 1
            self.max_gap_ms = max(self.max_gap_ms, gap)
            if gap > 60_000:
                self.long_gap_gt_60s += 1
        self.last_epoch_ms = tick.assumed_utc_epoch_ms
        self.last_source_row_sequence = tick.source_row_sequence
        self.context_rows += 1
        if tick.carried_sides:
            self.metrics["carried_quote_sides"] += len(tick.carried_sides)
            for side in tick.carried_sides:
                self.sink.emit("quote_carry", {
                    "source_row_sequence": tick.source_row_sequence,
                    "context_row_index": tick.context_row_index,
                    "raw_time": tick.raw_time_label,
                    "raw_bid": tick.raw_bid,
                    "raw_ask": tick.raw_ask,
                    "raw_flags": tick.raw_flags,
                    "carried_side": side,
                    "effective_bid": fmt_price_micro(tick.bid_micro),
                    "effective_ask": fmt_price_micro(tick.ask_micro),
                    "carry_policy": "prior raw quote field; missing-side update bit must be unset",
                    "event_time": "",
                    "release_time": "",
                    "ingested_time": "",
                    "available_time": "",
                    "cutoff_time": "",
                })

        new_minute = self._update_bar(tick)
        if not self.started and tick.assumed_utc_epoch_ms >= self.config.activation_epoch_ms:
            self.started = True
            self.activation_source_row_sequence = tick.source_row_sequence
            self.start_raw_time = tick.raw_time_label
            self.shock_locked = False  # explicitly unverified diagnostic bootstrap
            self.shock_lock_started_ms = None
            self.shock_normal_bars = 0
            self._emit_state("diagnostic_activation", tick, self.config.activation_boundary_label, {
                "bootstrap": self.config.start_state_assumption,
                "shock_lock_assumed_unlocked": True,
                "source_timezone": None,
                "assumed_timezone": "UTC provisional",
            })
        features = self._features()
        if not self.started:
            return
        if new_minute:
            self._on_completed_bar(tick, features)
        if new_minute and features is not None:
            self._emit_branch(tick, "feature_snapshot", "evaluated", "new_completed_m1_boundary", features, {
                "completed_bars": features["completed_bar_count"],
                "shock_atr_short_ratio": str(features["atr_short_ratio"]),
                "shock_latest_tr_ratio": str(features["latest_tr_ratio"]),
            })
        self._process_due_market_orders(tick)
        self._process_pending_stop(tick)
        self._manage_initial_protection(tick)
        self._evaluate_recovery_and_basket(tick, features)
        self._evaluate_entries(tick, features, new_minute)

    def summary(self) -> dict[str, Any]:
        total, p1, p2, volume = self._position_counts()
        return {
            "context_rows_processed": self.context_rows,
            "state_final": self._state_name(),
            "activation_source_row_sequence": self.activation_source_row_sequence,
            "activation_raw_time": self.start_raw_time,
            "activation_epoch_ms_assumed_utc_provisional": self.config.activation_epoch_ms,
            "source_equal_timestamp_pairs_seen": self.source_equal_timestamp_pairs,
            "max_gap_ms_seen": self.max_gap_ms,
            "gaps_gt_60s_seen": self.long_gap_gt_60s,
            "open_positions_final": total,
            "open_p1_positions_final": p1,
            "open_p2_positions_final": p2,
            "open_volume_steps_final": volume,
            "reserved_position_slots_final": self._reserved_position_slots(),
            "max_positions": self.config.max_positions,
            "pending_market_orders_final": len(self.pending_orders),
            "active_entry_broker_order_ids_final": [
                item["broker_order_id"] for item in self.pending_orders
                if item["command_type"] == "entry"
            ],
            "capacity_invariant_open_plus_reserved_le_max_final": self._reserved_position_slots() <= self.config.max_positions,
            "pending_stop_final": self.pending_stop.order_id if self.pending_stop else None,
            "close_batch_final": self.close_batch,
            "execution_degraded_final": self.execution_degraded,
            "last_full_settlement_ms_assumed_utc": self.last_full_settlement_ms,
            "reason_counts": dict(self.reason_counts),
            "metrics": dict(self.metrics),
            "known_omissions": [
                "wick reversal branch omitted as unresolved",
                "P1 range filter is enabled in static inputs but its predicate is unresolved and omitted",
                "STOP rejection/cancel/market fallback branch unresolved; no silent fallback",
                "CloseBy is enabled in static inputs but matching/residual handling is not implemented; no CloseBy PASS",
                "P3 entry is statically disabled, while global take-profit is enabled; branch semantics remain unresolved",
                "P1/P2 dynamic spacing and P2 lot scaling are statically disabled and not activated",
                "real external-fee basis/timing, swaps and realized P2 profit treatment are unknown; H1 11-per-lot fee is an explicit diagnostic proxy",
                "spread filter is statically disabled; max-spread and wick-spread values do not imply a generic filter",
                "EA runtime activation, bootstrap pending/inventory/shock state are unverified",
                "trigger-time activation/data availability clocks are unknown; UTC is provisional",
            ],
        }
