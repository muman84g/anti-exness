from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


UTC = timezone.utc


def _utc_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _timestamp_text(value: datetime) -> str:
    return _utc_datetime(value).isoformat()


@dataclass(frozen=True)
class ExitDecision:
    reason: str | None
    policy_state: dict[str, Any]


def build_policy_state(entry_time: str | datetime, vol30_bps: float, config: dict[str, Any]) -> dict[str, Any]:
    entered_at = _utc_datetime(entry_time)
    lower = float(config["inner_vol_lower_bps_exclusive"])
    upper = float(config["inner_vol_upper_bps_inclusive"])
    inner_regime = lower < float(vol30_bps) <= upper
    branch = dict(config["inner_branch"] if inner_regime else config["outer_branch"])
    return {
        "policy_id": str(config["id"]),
        "reason_prefix": str(config.get("reason_prefix", "c4566")),
        "mode": str(branch["mode"]),
        "entry_vol30_bps": float(vol30_bps),
        "floor_bps": float(config["floor_bps"]),
        "grace_until_utc": _timestamp_text(entered_at + timedelta(minutes=float(branch["grace_min"]))),
        "grace_started": False,
        "required_seconds": float(branch["required_min"]) * 60.0,
        "accumulated_seconds": 0.0,
        "accumulated_milliseconds": 0,
        "last_observation_time_utc": _timestamp_text(entered_at),
        "last_observation_above_floor": False,
    }


def evaluate_policy(
    policy_state: dict[str, Any],
    *,
    now: str | datetime,
    current_profit_bps: float,
    max_observation_gap_seconds: float,
) -> ExitDecision:
    updated = dict(policy_state)
    observed_at = _utc_datetime(now)
    last_observed_at = _utc_datetime(updated["last_observation_time_utc"])
    if observed_at < last_observed_at:
        return ExitDecision(reason=None, policy_state=updated)
    if observed_at == last_observed_at:
        if str(updated["mode"]) == "continuous" and current_profit_bps < float(updated["floor_bps"]):
            updated["accumulated_milliseconds"] = 0
            updated["accumulated_seconds"] = 0.0
        updated["last_observation_above_floor"] = current_profit_bps >= float(updated["floor_bps"])
        return ExitDecision(reason=None, policy_state=updated)
    grace_until = _utc_datetime(updated["grace_until_utc"])
    elapsed_milliseconds = max(0, int(round((observed_at - last_observed_at).total_seconds() * 1000.0)))
    elapsed_seconds = elapsed_milliseconds / 1000.0
    reliable_gap = elapsed_seconds <= float(max_observation_gap_seconds)
    grace_started = bool(updated.get("grace_started", last_observed_at >= grace_until))
    accumulated_milliseconds = int(updated.get("accumulated_milliseconds", round(float(updated.get("accumulated_seconds", 0.0)) * 1000.0)))
    previous_above = bool(updated.get("last_observation_above_floor", False))
    if grace_started and reliable_gap and previous_above:
        accumulated_milliseconds += elapsed_milliseconds
    elif not grace_started and observed_at >= grace_until:
        grace_started = True
    mode = str(updated["mode"])
    required_milliseconds = int(round(float(updated["required_seconds"]) * 1000.0))
    reason = None
    if accumulated_milliseconds >= required_milliseconds:
        reason = f"{updated.get('reason_prefix', 'c4566')}_{mode}_positive_time"
    elif mode == "continuous" and (not reliable_gap or current_profit_bps < float(updated["floor_bps"])):
        accumulated_milliseconds = 0
    updated["accumulated_milliseconds"] = accumulated_milliseconds
    updated["accumulated_seconds"] = accumulated_milliseconds / 1000.0
    updated["grace_started"] = grace_started
    updated["last_observation_time_utc"] = _timestamp_text(observed_at)
    updated["last_observation_above_floor"] = current_profit_bps >= float(updated["floor_bps"])
    return ExitDecision(reason=reason, policy_state=updated)
