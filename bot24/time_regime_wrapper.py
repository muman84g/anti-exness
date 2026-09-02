"""Pure entry-time routing for symbol-specific strategy adapters.

This module deliberately does not own positions, exits, orders, account state,
or persistence.  A caller may use an ``EVALUATED`` result to consider new
entries.  Every other result is fail-closed for new entries only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


UTC = timezone.utc
MATCHED = "matched"
NO_ACTIVE_REGIME = "no_active_regime"
AMBIGUOUS_ACTIVE_REGIMES = "ambiguous_active_regimes"
CLASSIFIER_ERROR = "classifier_error"
UNKNOWN_CLASSIFIER_REGIME = "unknown_classifier_regime"
EVALUATED = "evaluated"
BLOCKED = "blocked"


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("routing time must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _parse_hhmm(value: str) -> time:
    try:
        hour_text, minute_text = str(value).split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid local time: {value!r}") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"invalid local time: {value!r}")
    return time(hour, minute)


@dataclass(frozen=True)
class RegimeSpec:
    """One half-open local-time window used only for new-entry routing.

    For a cross-midnight window, ``weekdays`` identifies the weekday on which
    the window starts.  Monday 22:00-02:00 therefore also covers Tuesday 01:00.
    """

    id: str
    timezone_name: str
    start_local: str
    end_local: str
    strategy_ids: tuple[str, ...]
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.id or not self.id.strip():
            raise ValueError("regime id is required")
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {self.timezone_name}") from exc
        start = _parse_hhmm(self.start_local)
        end = _parse_hhmm(self.end_local)
        if start == end:
            raise ValueError("start_local and end_local must differ")
        if not self.strategy_ids or any(not str(item).strip() for item in self.strategy_ids):
            raise ValueError("at least one non-empty strategy id is required")
        if len(set(self.strategy_ids)) != len(self.strategy_ids):
            raise ValueError("strategy ids must be unique within a regime")
        if not self.weekdays or any(day not in range(7) for day in self.weekdays):
            raise ValueError("weekdays must contain integers from 0 through 6")
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("weekdays must be unique")

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def contains(self, at_utc: datetime) -> bool:
        if not self.enabled:
            return False
        local = _aware_utc(at_utc).astimezone(self.zone)
        current = local.timetz().replace(tzinfo=None)
        start = _parse_hhmm(self.start_local)
        end = _parse_hhmm(self.end_local)
        if start < end:
            return local.weekday() in self.weekdays and start <= current < end
        if current >= start:
            anchor_weekday = local.weekday()
        elif current < end:
            anchor_weekday = (local - timedelta(days=1)).weekday()
        else:
            return False
        return anchor_weekday in self.weekdays

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "end_local": self.end_local,
            "id": self.id,
            "start_local": self.start_local,
            "strategy_ids": list(self.strategy_ids),
            "timezone": self.timezone_name,
            "weekdays": list(self.weekdays),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "RegimeSpec":
        return cls(
            id=str(raw.get("id") or ""),
            timezone_name=str(raw.get("timezone") or ""),
            start_local=str(raw.get("start_local") or ""),
            end_local=str(raw.get("end_local") or ""),
            strategy_ids=tuple(str(item) for item in (raw.get("strategy_ids") or ())),
            weekdays=tuple(int(item) for item in raw.get("weekdays", (0, 1, 2, 3, 4))),
            enabled=bool(raw.get("enabled", True)),
        )


@dataclass(frozen=True)
class RouteDecision:
    at_utc: datetime
    status: str
    regime_id: str | None
    strategy_ids: tuple[str, ...]
    matched_regime_ids: tuple[str, ...]
    topology_hash: str
    reason: str


class TimeRegimeRouter:
    """Resolve exactly one active entry regime, or fail closed."""

    def __init__(self, regimes: Sequence[RegimeSpec]):
        self.regimes = tuple(regimes)
        if not self.regimes:
            raise ValueError("at least one regime is required")
        ids = [item.id for item in self.regimes]
        if len(set(ids)) != len(ids):
            raise ValueError("regime ids must be unique")
        canonical = [item.canonical_dict() for item in sorted(self.regimes, key=lambda item: item.id)]
        payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.topology_hash = hashlib.sha256(payload).hexdigest()

    @classmethod
    def from_mappings(cls, raw_regimes: Sequence[Mapping[str, Any]]) -> "TimeRegimeRouter":
        return cls([RegimeSpec.from_mapping(item) for item in raw_regimes])

    def route(self, at_utc: datetime) -> RouteDecision:
        instant = _aware_utc(at_utc)
        matches = tuple(item for item in self.regimes if item.contains(instant))
        match_ids = tuple(item.id for item in matches)
        if not matches:
            return RouteDecision(
                instant, NO_ACTIVE_REGIME, None, (), (), self.topology_hash,
                "no configured entry regime is active",
            )
        if len(matches) != 1:
            return RouteDecision(
                instant, AMBIGUOUS_ACTIVE_REGIMES, None, (), match_ids,
                self.topology_hash, "multiple entry regimes are active",
            )
        selected = matches[0]
        return RouteDecision(
            instant, MATCHED, selected.id, selected.strategy_ids, match_ids,
            self.topology_hash, "one entry regime matched",
        )


class ClassifierRegimeRouter:
    """Adapt an audited external clock/classifier to the common route contract.

    This is the preferred path for bot23 because its boundaries intentionally
    combine fixed UTC, London DST, and New York DST clocks. ``classifier_id``
    identifies the caller-pinned implementation; its source hash must be frozen
    alongside ``topology_hash`` by the adoption evidence.
    """

    def __init__(
        self,
        classifier_id: str,
        classifier: Callable[[datetime], Any],
        strategy_ids_by_regime: Mapping[str, Sequence[str]],
    ):
        if not classifier_id or not classifier_id.strip():
            raise ValueError("classifier_id is required")
        if not callable(classifier):
            raise ValueError("classifier must be callable")
        normalized: dict[str, tuple[str, ...]] = {}
        for regime_id, raw_ids in strategy_ids_by_regime.items():
            key = str(regime_id)
            strategy_ids = tuple(str(item) for item in raw_ids)
            if not key or not strategy_ids or any(not item for item in strategy_ids):
                raise ValueError("classifier mapping requires non-empty regime and strategy ids")
            if len(set(strategy_ids)) != len(strategy_ids):
                raise ValueError("strategy ids must be unique within a classified regime")
            normalized[key] = strategy_ids
        if not normalized:
            raise ValueError("at least one classified regime mapping is required")
        self.classifier_id = classifier_id
        self.classifier = classifier
        self.strategy_ids_by_regime = normalized
        payload = json.dumps(
            {"classifier_id": classifier_id, "strategy_ids_by_regime": normalized},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.topology_hash = hashlib.sha256(payload).hexdigest()

    def route(self, at_utc: datetime) -> RouteDecision:
        instant = _aware_utc(at_utc)
        try:
            raw = self.classifier(instant)
        except Exception as exc:
            return RouteDecision(
                instant, CLASSIFIER_ERROR, None, (), (), self.topology_hash,
                f"entry classifier failed: {type(exc).__name__}",
            )
        if raw is None:
            return RouteDecision(
                instant, NO_ACTIVE_REGIME, None, (), (), self.topology_hash,
                "entry classifier returned no active regime",
            )
        regime_id = raw if isinstance(raw, str) else getattr(raw, "id", None)
        if not isinstance(regime_id, str) or regime_id not in self.strategy_ids_by_regime:
            label = regime_id if isinstance(regime_id, str) else type(raw).__name__
            return RouteDecision(
                instant, UNKNOWN_CLASSIFIER_REGIME, None, (), (), self.topology_hash,
                f"entry classifier returned an unmapped regime: {label}",
            )
        return RouteDecision(
            instant,
            MATCHED,
            regime_id,
            self.strategy_ids_by_regime[regime_id],
            (regime_id,),
            self.topology_hash,
            "one classified entry regime matched",
        )


StrategyAdapter = Callable[[Mapping[str, Any]], Any]


@dataclass(frozen=True)
class StrategyOutcome:
    strategy_id: str
    value: Any


@dataclass(frozen=True)
class WrapperEvaluation:
    status: str
    decision: RouteDecision
    outcomes: tuple[StrategyOutcome, ...]
    reason: str


class TimeRegimeStrategyWrapper:
    """Call pure strategy adapters only after an unambiguous route decision.

    Adapters must be side-effect-free.  The caller may submit orders only when
    ``status == EVALUATED``.  Adapter exceptions discard every candidate output
    from that evaluation and block new entries for the active regime.
    """

    def __init__(self, router: Any, adapters: Mapping[str, StrategyAdapter]):
        if not callable(getattr(router, "route", None)) or not getattr(router, "topology_hash", None):
            raise ValueError("router must provide route() and topology_hash")
        self.router = router
        self.adapters = dict(adapters)
        if any(not key or not callable(value) for key, value in self.adapters.items()):
            raise ValueError("adapter registry must map non-empty strategy ids to callables")

    def evaluate(self, at_utc: datetime, context: Mapping[str, Any]) -> WrapperEvaluation:
        decision = self.router.route(at_utc)
        if decision.status != MATCHED:
            return WrapperEvaluation(BLOCKED, decision, (), decision.reason)
        missing = tuple(item for item in decision.strategy_ids if item not in self.adapters)
        if missing:
            return WrapperEvaluation(
                BLOCKED, decision, (), f"missing strategy adapters: {','.join(missing)}",
            )
        outcomes: list[StrategyOutcome] = []
        for strategy_id in decision.strategy_ids:
            try:
                value = self.adapters[strategy_id](context)
            except Exception as exc:
                return WrapperEvaluation(
                    BLOCKED,
                    decision,
                    (),
                    f"strategy adapter failed: {strategy_id}:{type(exc).__name__}",
                )
            outcomes.append(StrategyOutcome(strategy_id, value))
        return WrapperEvaluation(EVALUATED, decision, tuple(outcomes), "active regime evaluated")
