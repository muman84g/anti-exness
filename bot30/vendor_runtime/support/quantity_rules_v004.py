"""Pure conditional P2 quantity diagnostics; no history/tick/runtime dependencies."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import product
from typing import Iterable

STEP_LOT = Decimal("0.01")
MIN_STEPS = 2
MAX_STEPS = 3
FALLBACK_STEPS = 3  # profile .03; explicit approximation, not an EA rule
CANDIDATE_PRODUCT_CAP = 64


@dataclass(frozen=True)
class QuantityDecision:
    rule_id: str
    possible_steps: tuple[int, ...]
    selected_steps: int
    fallback_used: bool
    reason: str

    @property
    def selected_lot(self) -> Decimal:
        return STEP_LOT * self.selected_steps

    @property
    def is_ambiguous(self) -> bool:
        return len(self.possible_steps) != 1


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer step/count")
    return value


def _candidate_values(value: object, name: str) -> tuple[int, ...] | None:
    """Normalize supported scalar/candidate inputs before any sequence handling."""
    if value is None:
        return None
    if type(value) is int:
        return (_positive_int(value, name),)
    if isinstance(value, (tuple, list, set, frozenset, range)):
        raw = tuple(value)
        if not raw:
            raise ValueError(f"{name} candidate range must not be empty")
        normalized = tuple(sorted({_positive_int(item, name) for item in raw}))
        return normalized
    raise ValueError(f"{name} must be an integer, supported candidate range, or None")


def _bounded_result(steps: int) -> int:
    return max(MIN_STEPS, min(MAX_STEPS, steps))


def _decision(rule_id: str, possibilities: Iterable[int], reason: str) -> QuantityDecision:
    values = tuple(sorted(set(_positive_int(v, "possible_steps") for v in possibilities)))
    if not values or any(v not in (MIN_STEPS, MAX_STEPS) for v in values):
        raise ValueError("quantity rules may only return 2 or 3 steps")
    if len(values) == 1:
        return QuantityDecision(rule_id, values, values[0], False, reason)
    return QuantityDecision(rule_id, values, FALLBACK_STEPS, True, reason)


def rule_r1_same_p2_count(same_side_open_p2_count: int | Iterable[int] | None) -> QuantityDecision:
    """R1: 3 steps when same-side open-P2 count is zero, otherwise 2."""
    rule = "R1_SAME_P2_COUNT"
    candidates = _candidate_values(same_side_open_p2_count, "same_side_open_p2_count")
    if candidates is None:
        return _decision(rule, (2, 3), "unknown_input_possible_2_or_3")
    return _decision(rule, (3 if count == 0 else 2 for count in candidates), "finite_candidates_evaluated")


def rule_r2_net_exposure(
    opposite_p1_breakout_steps: int | Iterable[int] | None,
    same_p1_breakout_steps: int | Iterable[int] | None,
    same_p2_remaining_steps: int | Iterable[int] | None,
) -> QuantityDecision:
    """R2: clip(opposite P1 breakout - same P1 breakout - same P2 remainder, 2, 3)."""
    rule = "R2_NET_EXPOSURE_RESEARCHER_SUPPLEMENT"
    factors = (
        _candidate_values(opposite_p1_breakout_steps, "opposite_p1_breakout_steps"),
        _candidate_values(same_p1_breakout_steps, "same_p1_breakout_steps"),
        _candidate_values(same_p2_remaining_steps, "same_p2_remaining_steps"),
    )
    if any(values is None for values in factors):
        return _decision(rule, (2, 3), "unknown_input_possible_2_or_3")
    candidate_count = len(factors[0]) * len(factors[1]) * len(factors[2])
    if candidate_count > CANDIDATE_PRODUCT_CAP:
        return _decision(rule, (2, 3), "candidate_product_cap_64_conservative_possible_2_or_3")
    outcomes = (
        _bounded_result(opposite - same_p1 - same_p2)
        for opposite, same_p1, same_p2 in product(*factors)
    )
    return _decision(rule, outcomes, "finite_candidates_evaluated")


def rule_r3_prior_observed_p2_out(any_strictly_earlier_positive_p2_out_allside: bool | Iterable[bool] | None) -> QuantityDecision:
    """R3: 3 steps absent observed positive P2-out; 2 after it; no reset."""
    rule = "R3_PRIOR_P2_OUT_MEMORY_APPROXIMATION"
    value = any_strictly_earlier_positive_p2_out_allside
    if value is None:
        return _decision(rule, (2, 3), "unknown_input_possible_2_or_3")
    if isinstance(value, (tuple, list, set, frozenset)):
        values = tuple(value)
        if not values or any(type(item) is not bool for item in values):
            raise ValueError("prior P2-out candidates must be nonempty booleans")
        return _decision(rule, (2 if item else 3 for item in values), "finite_candidates_evaluated")
    if type(value) is not bool:
        raise ValueError("prior P2-out flag must be bool, bool candidates, or None")
    return _decision(rule, (2 if value else 3,), "scalar_input_evaluated")


def lot_to_steps(lot: Decimal | str | int) -> int:
    """Convert a positive exact lot amount to assumed 0.01-lot steps."""
    try:
        value = lot if isinstance(lot, Decimal) else Decimal(str(lot))
    except Exception as exc:
        raise ValueError("lot must be a finite decimal amount") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("lot must be finite and positive")
    steps = value / STEP_LOT
    if steps != steps.to_integral_value():
        raise ValueError("lot must be an integer multiple of the assumed 0.01 step")
    return int(steps)


@dataclass(frozen=True)
class OrderProgress:
    requested_steps: int
    filled_steps: int = 0
    remaining_steps: int | None = None
    status: str = "queued"

    def __post_init__(self) -> None:
        requested = _positive_int(self.requested_steps, "requested_steps")
        filled = _positive_int(self.filled_steps, "filled_steps")
        remaining = requested - filled if self.remaining_steps is None else _positive_int(self.remaining_steps, "remaining_steps")
        if requested <= 0 or filled > requested or filled + remaining != requested:
            raise ValueError("requested = filled + remaining conservation failed")
        if self.status not in {"queued", "partial", "filled", "rejected"}:
            raise ValueError("unsupported status")
        if self.status == "filled" and remaining != 0:
            raise ValueError("filled status requires zero remaining")
        if self.status == "partial" and not (0 < filled < requested):
            raise ValueError("partial status requires positive filled and remaining")
        if self.status == "queued" and (filled != 0 or remaining != requested):
            raise ValueError("queued status requires no fills")
        object.__setattr__(self, "remaining_steps", remaining)


def apply_fill(progress: OrderProgress, fill_steps: int) -> OrderProgress:
    fill = _positive_int(fill_steps, "fill_steps")
    if fill == 0 or progress.status not in {"queued", "partial"} or fill > progress.remaining_steps:
        raise ValueError("fill must be positive, eligible, and no greater than remaining")
    total = progress.filled_steps + fill
    remain = progress.remaining_steps - fill
    return OrderProgress(progress.requested_steps, total, remain, "filled" if remain == 0 else "partial")


def reject_order(progress: OrderProgress) -> OrderProgress:
    """Reject without inventing a fill or losing the outstanding remainder."""
    if progress.status not in {"queued", "partial"}:
        raise ValueError("only queued or partial orders may be rejected")
    return OrderProgress(progress.requested_steps, progress.filled_steps, progress.remaining_steps, "rejected")


def retry_remainder(rejected: OrderProgress) -> OrderProgress:
    """Create a fresh local request for the rejected order's remainder; no broker ID/runtime."""
    if rejected.status != "rejected" or rejected.remaining_steps == 0:
        raise ValueError("retry requires a rejected order with positive remainder")
    return OrderProgress(rejected.remaining_steps)
