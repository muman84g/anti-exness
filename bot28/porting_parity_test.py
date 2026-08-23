from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from protocol_v2_strategy import cycle560_latest, latest_signal


def resolve_source(root: Path) -> Path:
    matches = [path for path in (root / "backtest").glob("*/*/*PV2C560_OOS_001*") if path.is_dir()]
    if len(matches) != 1:
        raise RuntimeError(f"PV2C560 source resolution failed: {matches}")
    return matches[0]


def build_midpoint_m1(raw_path: Path) -> pd.DataFrame:
    pieces = []
    prior_bid: float | None = None
    prior_ask: float | None = None
    for chunk in pd.read_csv(
        raw_path,
        sep="\t",
        usecols=["<DATE>", "<TIME>", "<BID>", "<ASK>", "<FLAGS>"],
        dtype={"<DATE>": "string", "<TIME>": "string", "<BID>": "float64", "<ASK>": "float64", "<FLAGS>": "int64"},
        chunksize=500_000,
    ):
        bid = chunk["<BID>"].copy()
        ask = chunk["<ASK>"].copy()
        flags = chunk["<FLAGS>"].astype(np.int64)
        bid_missing = bid.isna()
        ask_missing = ask.isna()
        valid_bid_only = ~bid_missing & ask_missing & ((flags & 2) != 0) & ((flags & 4) == 0)
        valid_ask_only = bid_missing & ~ask_missing & ((flags & 4) != 0) & ((flags & 2) == 0)
        invalid = (bid_missing & ask_missing) | (~bid_missing & ask_missing & ~valid_bid_only) | (bid_missing & ~ask_missing & ~valid_ask_only)
        if invalid.any():
            raise RuntimeError(f"invalid one-sided quote rows: {int(invalid.sum())}")
        if prior_bid is not None and pd.isna(bid.iloc[0]):
            bid.iloc[0] = prior_bid
        if prior_ask is not None and pd.isna(ask.iloc[0]):
            ask.iloc[0] = prior_ask
        bid = bid.ffill()
        ask = ask.ffill()
        if bid.isna().any() or ask.isna().any() or (bid > ask).any():
            raise RuntimeError("executable quote reconstruction failed")
        prior_bid = float(bid.iloc[-1])
        prior_ask = float(ask.iloc[-1])
        event_time = pd.to_datetime(chunk["<DATE>"] + " " + chunk["<TIME>"], format="%Y.%m.%d %H:%M:%S.%f", utc=True)
        frame = pd.DataFrame({"bar_start": event_time.dt.floor("min"), "Close": (bid + ask) / 2.0, "TickVolume": 1})
        pieces.append(frame.groupby("bar_start", sort=True).agg(Close=("Close", "last"), TickVolume=("TickVolume", "sum")))
    return pd.concat(pieces).groupby(level=0, sort=True).agg(Close=("Close", "last"), TickVolume=("TickVolume", "sum"))


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    source = resolve_source(root)
    params = json.loads((Path(__file__).parent / "s28_params.json").read_text(encoding="utf-8"))
    strategy = params["strategy"]
    assert params["require_midpoint_close"] is True
    assert params["max_signal_delay_seconds"] == 45
    assert strategy["strategy_type"] == "PV2C560"
    assert strategy["candidate"] == "PV2C560_DEVQ80_H75_FORWARD_R1"
    assert strategy["hold_min"] == 75
    bars = build_midpoint_m1(root / "backtest" / "leakcheck_data" / "USTEC_tick_leakcheck.csv")
    ledger = pd.read_csv(source / "CORRECTED_DIAGNOSTIC_R1_LEDGER.csv.gz")
    samples = ledger.iloc[[0, len(ledger) // 2, -1]]
    for row in samples.itertuples(index=False):
        signal_time = pd.Timestamp(row.signal_bar_start)
        result = cycle560_latest(bars.loc[:signal_time], strategy)
        assert abs(result["ret25"] - float(row.feature_ret25)) < 1e-12
        assert abs(result["sqret_ac_l1_w60"] - float(row.feature_value)) < 1e-12
        assert result["eligible"]
    try:
        latest_signal(bars.iloc[-65:], None, {**strategy, "strategy_type": "PV2C859"})
    except ValueError:
        pass
    else:
        raise AssertionError("foreign strategy type must be rejected")
    print(f"bot28 PV2C560 porting parity ok: samples={len(samples)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
