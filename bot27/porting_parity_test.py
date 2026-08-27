from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from protocol_v2_strategy import cycle859_latest, latest_signal, short_overlay_signals
import live_executor as executor_module
import live_s27_bot as runner_module


def resolve_source(root: Path) -> Path:
    source = root / "backtest" / "bot関連backtest" / "0_bot27実装PV2C859_OOS_001"
    if not source.is_dir():
        raise RuntimeError(f"PV2C859 source resolution failed: {source}")
    return source


def build_bid_m1(raw_path: Path) -> pd.DataFrame:
    pieces = []
    prior_bid: float | None = None
    for chunk in pd.read_csv(
        raw_path,
        sep="\t",
        usecols=["<DATE>", "<TIME>", "<BID>", "<ASK>", "<FLAGS>"],
        dtype={"<DATE>": "string", "<TIME>": "string", "<BID>": "float64", "<ASK>": "float64", "<FLAGS>": "int64"},
        chunksize=500_000,
    ):
        bid = chunk["<BID>"].copy()
        ask = chunk["<ASK>"]
        flags = chunk["<FLAGS>"].astype(np.int64)
        bid_missing = bid.isna()
        ask_missing = ask.isna()
        valid_ask_only = bid_missing & ~ask_missing & ((flags & 4) != 0) & ((flags & 2) == 0)
        invalid = bid_missing & ~valid_ask_only
        if invalid.any():
            raise RuntimeError(f"invalid missing Bid rows: {int(invalid.sum())}")
        if prior_bid is not None:
            bid.iloc[0] = prior_bid if pd.isna(bid.iloc[0]) else bid.iloc[0]
        bid = bid.ffill()
        if bid.isna().any():
            raise RuntimeError("leading Bid cannot be reconstructed")
        prior_bid = float(bid.iloc[-1])
        event_time = pd.to_datetime(
            chunk["<DATE>"] + " " + chunk["<TIME>"],
            format="%Y.%m.%d %H:%M:%S.%f",
            utc=True,
        )
        minute = event_time.dt.floor("min")
        pieces.append(pd.DataFrame({"bar_start": minute, "Close": bid}).groupby("bar_start", sort=True).last())
    bars = pd.concat(pieces).groupby(level=0, sort=True).last()
    return bars


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    source = resolve_source(root)
    params = json.loads((Path(__file__).parent / "s27_params.json").read_text(encoding="utf-8"))
    strategy = params["strategy"]
    assert params["require_midpoint_close"] is False
    assert strategy["strategy_type"] == "PV2C859"
    assert strategy["candidate"] == "OLD_BEST_ACTIVITY_075_VSA_075_FORWARD_WINNER"
    assert strategy["max_positions"] == 5
    assert [row["signal_type"] for row in strategy["lane_parameters"]] == ["long", "long", "long", "activity", "vsa"]
    assert strategy["hold_min"] == 90
    exit_policy = strategy.get("exit_policy")
    assert exit_policy is not None
    assert exit_policy["id"] == "PV2C859_L3_ACTIVITY_VSA_FORWARD_R1"
    assert exit_policy["reason_prefix"] == "c4560"
    assert exit_policy["floor_bps"] == 4.0
    assert exit_policy["inner_branch"] == {"mode": "continuous", "grace_min": 0, "required_min": 20}
    assert exit_policy["outer_branch"] == {"mode": "continuous", "grace_min": 0, "required_min": 20}
    assert exit_policy["max_observation_gap_seconds"] == 15
    bars = build_bid_m1(root / "backtest" / "leakcheck_data" / "USTEC_tick_leakcheck.csv")
    ledger = pd.read_csv(source / "CORRECTED_DIAGNOSTIC_R1_LEDGER.csv.gz")
    eligible = ledger.loc[ledger.feature_value <= float(strategy["threshold"])]
    samples = eligible.iloc[[0, len(eligible) // 2, -1]]
    for row in samples.itertuples(index=False):
        signal_time = pd.Timestamp(row.signal_bar_start)
        result = cycle859_latest(bars.loc[:signal_time], strategy)
        assert abs(result["ret25"] - float(row.feature_ret25)) < 1e-12
        assert abs(result["absret_std_ratio30_120"] - float(row.feature_value)) < 1e-12
        expected_vol30 = np.log(bars.loc[:signal_time, "Close"]).diff().mul(10000.0).rolling(30, min_periods=30).std().iloc[-1]
        assert abs(result["vol30_bps"] - float(expected_vol30)) < 1e-12
        assert result["eligible"]
    overlay_bars = bars.iloc[-130:].copy()
    overlay_bars["Open"] = overlay_bars["Close"]
    overlay_bars["High"] = overlay_bars["Close"]
    overlay_bars["Low"] = overlay_bars["Close"]
    overlay_bars["Volume"] = 100
    overlay_bars["MidClose"] = overlay_bars["Close"]
    overlay_result = short_overlay_signals(overlay_bars, strategy)
    assert set(overlay_result) == {"activity", "vsa"}
    assert overlay_result["activity"]["side"] == "SHORT"
    assert overlay_result["vsa"]["side"] == "SHORT"
    synthetic_index = pd.date_range("2026-08-01", periods=80, freq="min", tz="UTC")
    activity_bars = pd.DataFrame({"Open": 100.0, "High": 100.01, "Low": 99.99, "Close": 100.0, "MidClose": 100.0, "Volume": 100}, index=synthetic_index)
    activity_bars.iloc[-2, activity_bars.columns.get_loc("Volume")] = 200
    activity_bars.iloc[-1, activity_bars.columns.get_loc("Open")] = 100.0
    activity_bars.iloc[-1, activity_bars.columns.get_loc("High")] = 100.0
    activity_bars.iloc[-1, activity_bars.columns.get_loc("Low")] = 99.97
    activity_bars.iloc[-1, activity_bars.columns.get_loc("Close")] = 99.98
    activity_bars.iloc[-1, activity_bars.columns.get_loc("MidClose")] = 99.98
    assert short_overlay_signals(activity_bars, strategy)["activity"]["eligible"]
    vsa_bars = pd.DataFrame({"Open": 100.0, "High": 100.01, "Low": 99.99, "Close": 100.0, "MidClose": 100.0, "Volume": 100}, index=synthetic_index)
    vsa_bars.iloc[-2, vsa_bars.columns.get_loc("Close")] = 101.0
    vsa_bars.iloc[-2, vsa_bars.columns.get_loc("MidClose")] = 101.0
    vsa_bars.iloc[-1] = [101.0, 102.0, 100.0, 100.5, 100.5, 200]
    assert short_overlay_signals(vsa_bars, strategy)["vsa"]["eligible"]
    original_state_file = runner_module.STATE_FILE
    with tempfile.TemporaryDirectory() as temporary:
        runner_module.STATE_FILE = str(Path(temporary) / "state.json")
        legacy = runner_module.ProtocolV2FixedHoldRunner(params)._default_state()
        legacy["strategy_id"] = "PV2C859_C4560_GAP15_FORWARD_R1"
        legacy["positions"] = [{"lane_id": 1, "ticket": 123, "side": "LONG"}]
        Path(runner_module.STATE_FILE).write_text(json.dumps(legacy), encoding="utf-8")
        migrated = runner_module.ProtocolV2FixedHoldRunner(params).state
        assert migrated["strategy_id"] == strategy["id"]
        assert migrated["positions"] == legacy["positions"]
    runner_module.STATE_FILE = original_state_file
    original_send = executor_module.ea_bridge.send_command
    executor_module.ea_bridge.send_command = lambda *_args, **_kwargs: "OK|101|100|1000|0.01|0.01|100|0.01|1|0.01|1|2|0|1787306465000"
    try:
        symbol_info = executor_module.MT5Executor().get_symbol_info("USTEC")
    finally:
        executor_module.ea_bridge.send_command = original_send
    assert symbol_info is not None and symbol_info.tick_time_msc == 1787306465000
    try:
        latest_signal(bars.iloc[-130:], None, {**strategy, "strategy_type": "PV2C421"})
    except ValueError:
        pass
    else:
        raise AssertionError("retired PV2C421 strategy type must be rejected")
    print(f"bot27 PV2C859 porting parity ok: samples={len(samples)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
