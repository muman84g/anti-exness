"""Parity and fail-closed tests for bot24 entry-time routing."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import unittest

import pandas as pd

import live_s24_bot as s24
from time_regime_wrapper import MATCHED, NO_ACTIVE_REGIME


UTC = timezone.utc


def build_runner(params: dict | None = None) -> s24.S24NoAdverseRunner:
    runner = object.__new__(s24.S24NoAdverseRunner)
    runner.params = copy.deepcopy(params or s24.load_params())
    runner.entry_router, runner.entry_wrapper = runner._build_entry_wrapper()
    return runner


class S24TimeRegimeWrapperTests(unittest.TestCase):
    def test_current_utc_session_matches_legacy_gate_for_full_year(self) -> None:
        runner = build_runner()
        strategy = runner.params["strategies"][0]
        at = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2027, 1, 1, tzinfo=UTC)
        while at < end:
            decision = runner.entry_router.route(at)
            expected = s24.in_session(
                pd.Timestamp(at),
                int(strategy["session_start_utc"]),
                int(strategy["session_end_utc"]),
            )
            self.assertEqual(decision.status == MATCHED, expected, at.isoformat())
            at += timedelta(minutes=10)

    def test_signal_adapter_preserves_current_decision_inside_session(self) -> None:
        runner = build_runner()
        strategy = runner.params["strategies"][0]
        bars = s24.add_features(s24.FakeDM().get_historical_data(), float(runner.params["point_size"]))
        row = bars.iloc[-1].copy()
        row["spread_points"] = 30.0
        row.name = pd.Timestamp("2026-08-31T13:30:00Z")
        self.assertEqual(
            runner._signal_decision(row, strategy),
            runner._strategy_signal_decision(row, strategy),
        )

    def test_outside_session_blocks_only_entry_evaluation(self) -> None:
        runner = build_runner()
        strategy = runner.params["strategies"][0]
        row = pd.Series(name=pd.Timestamp("2026-08-31T12:59:00Z"), dtype=float)
        self.assertEqual(runner.entry_router.route(row.name.to_pydatetime()).status, NO_ACTIVE_REGIME)
        self.assertEqual(runner._signal_decision(row, strategy), (None, "outside_session"))

    def test_routing_strategy_identity_mismatch_fails_at_startup(self) -> None:
        params = copy.deepcopy(s24.load_params())
        params["entry_time_routing"]["regimes"][0]["strategy_ids"] = ["foreign_strategy"]
        runner = object.__new__(s24.S24NoAdverseRunner)
        runner.params = params
        with self.assertRaisesRegex(RuntimeError, "entry routing identity mismatch"):
            runner._build_entry_wrapper()


if __name__ == "__main__":
    unittest.main()
