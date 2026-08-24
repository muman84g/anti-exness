from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from c4566_exit_policy import build_policy_state, evaluate_policy


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    run = root / "backtest" / "bot関連backtest" / "0_bot27実装PV2C859_OOS_001" / "runs" / "20260824_bot27_cyclecanonical_v1"
    sys.path.insert(0, str(run))
    import run_transfer_cycles as core

    params = json.loads((Path(__file__).parent / "s27_params.json").read_text(encoding="utf-8"))
    config = params["strategy"]["exit_policy"]
    ledger = pd.read_csv(run / "tick_gap_corrected_c4566" / "c4566_v01_opportunity_ledger.csv.gz")
    accepted = ledger[ledger.accepted.astype(str).str.lower().eq("true")].copy()
    accepted["entry_tick_time"] = pd.to_datetime(accepted.entry_tick_time, utc=True, format="mixed")
    accepted["exit_tick_time"] = pd.to_datetime(accepted.exit_tick_time, utc=True, format="mixed")
    ticks = pd.read_pickle(core.CACHE)
    times = ticks.event_time.to_numpy(dtype="datetime64[ns]").astype(np.int64)
    bid = ticks.bid.to_numpy(float)
    ask = ticks.ask.to_numpy(float)
    mismatches: list[dict[str, object]] = []
    for trade in accepted.itertuples(index=False):
        entry_ns = trade.entry_tick_time.value
        entry_index = int(np.searchsorted(times, entry_ns, side="left"))
        final_index = int(np.searchsorted(times, entry_ns + 90 * core.MINUTE_NS, side="left"))
        state = build_policy_state(trade.entry_tick_time.to_pydatetime(), float(trade.vol30_bps), config)
        predicted_index = final_index
        predicted_reason = "hard_hold"
        for tick_index in range(entry_index + 1, final_index + 1):
            profit_bps = (bid[tick_index] / ask[entry_index] - 1.0) * 10000.0
            decision = evaluate_policy(
                state,
                now=pd.Timestamp(times[tick_index], tz="UTC").to_pydatetime(),
                current_profit_bps=float(profit_bps),
                max_observation_gap_seconds=float(config["max_observation_gap_seconds"]),
            )
            state = decision.policy_state
            if decision.reason is not None:
                predicted_index = tick_index
                predicted_reason = decision.reason
                break
        expected_reason = "hard_hold" if trade.exit_reason == "hard_hold" else (
            "c4566_continuous_positive_time" if trade.exit_reason == "mid_vol_continuous_dwell_gap15" else "c4566_cumulative_positive_time"
        )
        predicted_time = pd.Timestamp(times[predicted_index], tz="UTC")
        if predicted_time != trade.exit_tick_time or predicted_reason != expected_reason:
            mismatches.append({
                "signal_opportunity_id": trade.signal_opportunity_id,
                "expected_time": trade.exit_tick_time.isoformat(),
                "predicted_time": predicted_time.isoformat(),
                "expected_reason": expected_reason,
                "predicted_reason": predicted_reason,
            })
            if len(mismatches) >= 10:
                break
    if mismatches:
        raise AssertionError(json.dumps(mismatches, ensure_ascii=False, indent=2))
    print(f"bot27 C4566 exit parity ok: trades={len(accepted)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
