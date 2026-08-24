from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd

from protocol_v2_strategy import cycle520_latest


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    source = next(root.rglob("STALE_FC_DIAGNOSTIC_R1_LEDGER.csv.gz")).parent
    audit = load_module(source / "audit_cross_symbol_provenance.py", "pv2c520_audit")
    data_dir = root / "backtest" / "leakcheck_data"
    target, _ = audit.build_m1_close(data_dir / "USTEC_tick_leakcheck.csv")
    context, _ = audit.build_m1_close(data_dir / "USOIL_tick_leakcheck.csv")
    target = target.set_index("bar_start")
    context = context.set_index("bar_start")
    strategy = json.loads((Path(__file__).parent / "s26_params.json").read_text(encoding="utf-8"))["strategy"]
    assert strategy["candidate"] == "PV2C520_C4535_CONT1_WINDOW60_H75_FORWARD_ONLY"
    assert strategy["hold_min"] == 75
    assert strategy["entry_confirmation"] == {
        "type": "continuation_confirmation",
        "reference": "first_valid_ask_at_signal_decision",
        "continuation_bps": 1.0,
        "window_seconds": 60,
        "pending_lane": "single_pending_or_position",
    }
    kept = pd.read_csv(source / "STALE_FC_DIAGNOSTIC_R1_LEDGER.csv.gz")
    rejected = pd.read_csv(source / "STALE_FC_DIAGNOSTIC_R1_REJECTED_SIGNALS.csv")
    samples = pd.concat([kept.iloc[[0, -1]], rejected], ignore_index=True)
    for row in samples.itertuples():
        signal_time = pd.Timestamp(row.signal_bar_start)
        result = cycle520_latest(target.loc[:signal_time], context.loc[:signal_time], strategy)
        assert abs(result["ret25"] - float(row.feature_ret25)) < 1e-9
        assert abs(result["lead5_corr_z"] - float(row.feature_lead5_corr_z)) < 1e-9
        assert abs(result["context_stale_seconds"] - float(row.context_stale_seconds)) < 1e-9
        assert result["eligible"] == bool(row.context_stale_pass)
    print(f"bot26 porting parity ok: samples={len(samples)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
