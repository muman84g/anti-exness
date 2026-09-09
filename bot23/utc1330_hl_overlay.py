"""Causal UTC13:30 Long inversion and delayed-rearm overlay."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd


POLICY_ID = "utc1230_rise020_signal_invert_1330_1335_long_rearm020_v001"
POLICY_PARAMS_HASH = "85831ad64224fad5e64a7959f4aa5f5d1ec512ece45c38bb49fdb931e89a4706"
EXPECTED_CONFIG = {
    "enabled": True,
    "policy_id": POLICY_ID,
    "params_hash": POLICY_PARAMS_HASH,
    "reference_minute_utc": 750,
    "evaluation_minute_utc": 800,
    "qualification_rise_ratio": 0.002,
    "anchor_minute_utc": 810,
    "inversion_end_minute_utc": 816,
    "long_rearm_drop_ratio": 0.002,
}


def config_error(config: Any) -> str | None:
    if not isinstance(config, dict):
        return "not_object"
    if set(config) != set(EXPECTED_CONFIG):
        return "schema"
    for key, expected in EXPECTED_CONFIG.items():
        observed = config.get(key)
        if isinstance(expected, bool):
            if not isinstance(observed, bool) or observed is not expected:
                return key
        elif isinstance(expected, int):
            if isinstance(observed, bool) or not isinstance(observed, int) or observed != expected:
                return key
        elif isinstance(expected, float):
            if isinstance(observed, bool) or not isinstance(observed, (int, float)) or not math.isclose(float(observed), expected, rel_tol=0.0, abs_tol=1e-12):
                return key
        elif observed != expected:
            return key
    return None


def apply_policy(
    bars: pd.DataFrame,
    signal_time: Any,
    side: str | None,
    current_bid: float | None,
    config: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    """Apply only information available after a completed M1 signal bar."""
    effective = str(side or "").upper() or None
    details: dict[str, Any] = {
        "policy_id": str(config.get("policy_id") or ""),
        "raw_side": effective,
        "effective_side": effective,
        "action": "unchanged",
        "reason": "not_long",
        "qualified": False,
        "reference_open": None,
        "evaluation_open": None,
        "rise_ratio": None,
        "anchor_open": None,
        "rearm_level": None,
        "rearmed": False,
    }
    if config_error(config) is not None:
        details.update({"effective_side": None, "action": "blocked", "reason": "invalid_config"})
        return None, details
    if not config["enabled"] or effective != "LONG":
        details["reason"] = "disabled" if not config["enabled"] else "not_long"
        return effective, details
    try:
        stamp = pd.Timestamp(signal_time)
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    except Exception:
        details.update({"effective_side": None, "action": "blocked", "reason": "signal_time_invalid"})
        return None, details
    minute = stamp.hour * 60 + stamp.minute
    if minute < int(config["evaluation_minute_utc"]):
        details["reason"] = "before_evaluation"
        return effective, details

    frame = bars.copy()
    if frame.index.tz is None:
        frame.index = frame.index.tz_localize("UTC")
    else:
        frame.index = frame.index.tz_convert("UTC")
    day = stamp.floor("D")

    def open_at(minute_utc: int) -> float | None:
        target = day + pd.Timedelta(minutes=minute_utc)
        matches = frame.loc[frame.index == target, "Open"] if "Open" in frame else pd.Series(dtype=float)
        if len(matches) != 1:
            return None
        try:
            value = float(matches.iloc[0])
        except (TypeError, ValueError, OverflowError):
            return None
        return value if math.isfinite(value) and value > 0.0 else None

    reference = open_at(int(config["reference_minute_utc"]))
    evaluation = open_at(int(config["evaluation_minute_utc"]))
    details.update({"reference_open": reference, "evaluation_open": evaluation})
    if reference is None or evaluation is None:
        details.update({"effective_side": None, "action": "blocked", "reason": "qualification_inputs_unavailable"})
        return None, details
    rise = evaluation / reference - 1.0
    qualified = rise >= float(config["qualification_rise_ratio"])
    details.update({"rise_ratio": rise, "qualified": qualified})
    if not qualified:
        details["reason"] = "rise_threshold_not_met"
        return effective, details
    if minute < int(config["anchor_minute_utc"]):
        details["reason"] = "qualified_before_anchor"
        return effective, details

    anchor = open_at(int(config["anchor_minute_utc"]))
    if anchor is None:
        details.update({"effective_side": None, "action": "blocked", "reason": "anchor_unavailable"})
        return None, details
    rearm_level = anchor * (1.0 - float(config["long_rearm_drop_ratio"]))
    start = day + pd.Timedelta(minutes=int(config["anchor_minute_utc"]))
    lows = frame.loc[(frame.index >= start) & (frame.index <= stamp), "Low"] if "Low" in frame else pd.Series(dtype=float)
    rearmed = bool(len(lows) and pd.to_numeric(lows, errors="coerce").le(rearm_level).any())
    if current_bid is not None:
        try:
            quote = float(current_bid)
            rearmed = rearmed or (math.isfinite(quote) and quote <= rearm_level)
        except (TypeError, ValueError, OverflowError):
            pass
    details.update({"anchor_open": anchor, "rearm_level": rearm_level, "rearmed": rearmed})
    if minute < int(config["inversion_end_minute_utc"]):
        details.update({"effective_side": "SHORT", "action": "invert_short", "reason": "qualified_inversion_window"})
        return "SHORT", details
    if not rearmed:
        details.update({"effective_side": None, "action": "blocked", "reason": "long_rearm_not_reached"})
        return None, details
    details["reason"] = "long_rearmed"
    return "LONG", details


def policy_note(details: dict[str, Any]) -> str:
    keys = ("policy_id", "raw_side", "effective_side", "action", "reason", "qualified", "reference_open", "evaluation_open", "rise_ratio", "anchor_open", "rearm_level", "rearmed")
    return ";".join(f"hl_{key}={details.get(key)}" for key in keys)
