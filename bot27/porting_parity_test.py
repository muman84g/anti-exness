from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from protocol_v2_strategy import cycle859_latest, latest_signal
import live_executor as executor_module


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
    assert strategy["candidate"] == "PV2C859_DEVQ825_H90_FORWARD_R1"
    assert strategy["hold_min"] == 90
    assert "exit_policy" not in strategy
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
