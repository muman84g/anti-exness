from __future__ import annotations

import hashlib
import sys
import types
from pathlib import Path
from typing import Any, Callable

EXPECTED_X1_WRAPPER_SHA256 = "16c0c2b368c9864f7c06338c6db02d6f10368557dc177193d8e2a557d374b475"
EXPECTED_NATIVE_HASHES = {
    "h2_engine": "47da2b8b6cbe3a6fc24426e9a11ca55642096e2cf1ad55744e863cf80b9b7396",
    "lifecycle_market_adapter": "786f6490999379d63ffd3646a65a04b15911abd136923096160c2f5a82bb4c2a",
    "a3_lifecycle_boundary": "cd15541aef8b459a31ef79e905c33e32907d1c60596a20e2b47397eaac83dac8",
}
EXPECTED_CONFIG_ARTIFACT_SHA256 = "c38adcb23a0a402e3a1fe9b2acb38b7fca9717676d6b75990213f5328d40ee5d"
X1_VARIANT_ID = "X1_INITIAL_DECISION_REF_BE_TRAIL"
COUNT_ONLY_VARIANT_ID = "X1_P2_BREAKOUT_COUNT_ONLY_V001"
P2_COUNT_REASON = "P2_OPEN_BREAKOUT_COUNT_NOT_GT_2"
TRACE_LIMIT = 8192


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_exact_x1_wrapper(path: str | Path = r"C:\botter\backtest\output\backtest_30\code\research_entry_close.py") -> tuple[Any, dict[str, str]]:
    """Load only the pinned inactive research wrapper from verified source bytes."""
    if sys.dont_write_bytecode is not True:
        raise RuntimeError("synthetic code load requires sys.dont_write_bytecode=True")
    wrapper_path = Path(path).resolve(strict=True)
    source_bytes = wrapper_path.read_bytes()
    digest = _sha256_bytes(source_bytes)
    if digest != EXPECTED_X1_WRAPPER_SHA256:
        raise RuntimeError("pinned X1 research wrapper hash mismatch")
    name = "_task_p2_count_x1_wrapper_v001"
    previous = sys.modules.get(name)
    if previous is not None:
        if getattr(previous, "__verified_source_sha256__", None) != digest:
            raise RuntimeError("unverified wrapper cache rejected")
        module = previous
    else:
        module = types.ModuleType(name)
        module.__file__ = str(wrapper_path)
        module.__package__ = ""
        sys.modules[name] = module
        try:
            exec(compile(source_bytes, str(wrapper_path), "exec"), module.__dict__)
            module.__verified_source_sha256__ = digest
        except BaseException:
            sys.modules.pop(name, None)
            raise

    native_modules = module.load_native_core()
    native_hashes = {
        key: str(getattr(value, "__verified_source_sha256__", ""))
        for key, value in native_modules.items()
    }
    if native_hashes != EXPECTED_NATIVE_HASHES:
        raise RuntimeError(f"pinned native hash mismatch: {native_hashes}")
    module.__native_modules__ = native_modules
    return module, native_hashes


def open_p1_breakout_count(engine: Any) -> int:
    """Count open, positive-volume breakout positions across both directions."""
    return sum(
        1
        for position in engine.inventory.values()
        if position.role == "p1_breakout" and position.volume_steps > 0
    )


def install_p2_breakout_count_gate(engine: Any, *, trace_limit: int = TRACE_LIMIT) -> Any:
    """AND the P2-only all-side breakout-position count with native spacing."""
    if type(trace_limit) is not int or trace_limit <= 0:
        raise ValueError("trace_limit must be a positive integer")
    if hasattr(engine, "_p2_count_gate_installed"):
        raise RuntimeError("P2 breakout count gate already installed")
    config_threshold = engine.config.p2_start_positions_strictly_greater_than
    if type(config_threshold) is not int or config_threshold != 2:
        raise RuntimeError("frozen P2 open-position threshold must remain strict >2")

    native_spacing: Callable[[str, Any], tuple[bool, str]] = engine._p2_spacing_ok
    engine._p2_count_gate_installed = True
    engine._p2_count_gate_trace = []
    engine._p2_count_gate_trace_dropped = 0
    engine._p2_count_gate_calls = 0

    def counted_spacing(side: str, tick: Any) -> tuple[bool, str]:
        result = native_spacing(side, tick)
        if not isinstance(result, tuple) or len(result) != 2:
            raise TypeError("native P2 spacing return must remain a two-item tuple")
        native_pass, native_reason = result
        if type(native_pass) is not bool or not isinstance(native_reason, str):
            raise TypeError("native P2 spacing return types changed")

        breakout_count = open_p1_breakout_count(engine)
        _, native_p1_count, _, _ = engine._position_counts()
        count_pass = breakout_count > config_threshold
        combined_pass = native_pass and count_pass
        combined_reason = (
            native_reason
            if not native_pass or count_pass
            else P2_COUNT_REASON
        )

        engine._p2_count_gate_calls += 1
        observation = {
            "source_row_sequence": getattr(tick, "source_row_sequence", None),
            "raw_time": getattr(tick, "raw_time_label", None),
            "side": side,
            "bid_micro": getattr(tick, "bid_micro", None),
            "native_p1_open_count": native_p1_count,
            "p2_gate_open_breakout_count": breakout_count,
            "count_threshold": config_threshold,
            "native_spacing_pass": native_pass,
            "native_spacing_reason": native_reason,
            "count_pass": count_pass,
            "combined_p2_pass": combined_pass,
            "combined_reason": combined_reason,
        }
        if len(engine._p2_count_gate_trace) < trace_limit:
            engine._p2_count_gate_trace.append(observation)
        else:
            engine._p2_count_gate_trace_dropped += 1
        return combined_pass, combined_reason

    engine._p2_spacing_ok = counted_spacing
    return engine


def build_x1_count_only_engine(
    sink: Any,
    *,
    wrapper: Any,
    config: Any | None = None,
    trace_limit: int = TRACE_LIMIT,
) -> Any:
    """Build the exact X1 factory result, then add only the P2 count predicate."""
    engine = wrapper.build_engine(
        sink,
        variant_id=X1_VARIANT_ID,
        config=config,
    )
    install_p2_breakout_count_gate(engine, trace_limit=trace_limit)
    engine.research_variant_id = COUNT_ONLY_VARIANT_ID
    return engine
