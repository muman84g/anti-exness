"""Regression tests for bot24's passive runner lane."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

import pandas as pd

import live_s24_bot as s24


def test_params() -> dict:
    params = s24.load_params()
    params = json.loads(json.dumps(params))
    params["live_trading_enabled"] = True
    params["_suppress_manual_alerts"] = True
    params["shadow_forward_enabled"] = False
    params["runner_shadow"]["enabled"] = True
    params["runner_shadow"]["execution_mode"] = "shadow"
    params["runner_shadow"]["opportunity_observer"]["enabled"] = False
    params["runner_shadow"]["state_tagger"]["enabled"] = False
    return params


class RecordingExecutor(s24.FakeExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.open_calls = 0
        self.close_calls = 0

    def open_position(self, *args, **kwargs):
        self.open_calls += 1
        return super().open_position(*args, **kwargs)

    def close_position(self, *args, **kwargs):
        self.close_calls += 1
        return super().close_position(*args, **kwargs)


class RunnerShadowTests(unittest.TestCase):
    def make_runner(self) -> tuple[s24.S24NoAdverseRunner, dict, RecordingExecutor, list[tuple[str, dict]]]:
        params = test_params()
        runner = s24.S24NoAdverseRunner(params)
        runner.safety = replace(runner.safety, stale_signal_guard=False)
        runner.state = runner._default_state()
        executor = RecordingExecutor()
        runner.executor = executor
        runner._save_state = lambda: None
        rows: list[tuple[str, dict]] = []
        runner._shadow_runner_row = lambda event, _strat, **kwargs: rows.append((event, kwargs))
        return runner, params, executor, rows

    @staticmethod
    def signal_bars() -> pd.DataFrame:
        params = test_params()
        bars = s24.add_features(s24.FakeDM().get_historical_data(), float(params["point_size"]))
        bars["spread_points"] = 30.0
        bars.iloc[-1, bars.columns.get_loc("Close")] = float(bars.iloc[-1]["roll_high30"]) + 1.0
        return bars

    def test_runner_entry_is_stateful_but_never_calls_broker(self):
        runner, params, executor, rows = self.make_runner()
        strategy = params["strategies"][0]
        bars = self.signal_bars()
        info = type("Info", (), {"bid": 2064.0, "ask": 2064.03})()

        runner._run_shadow_runner(strategy, bars, info)
        state = runner._st(strategy)["shadow_runner"]

        self.assertEqual(len(state["basket"]), 1)
        self.assertEqual(state["basket"][0]["side"], "LONG")
        self.assertEqual(executor.open_calls, 0)
        self.assertEqual(executor.close_calls, 0)
        self.assertTrue(any(event == "runner_entry" for event, _row in rows))

        runner._run_shadow_runner(strategy, bars, info)
        self.assertEqual(len(state["basket"]), 1, "same confirmed bar must not duplicate a runner entry")

    def test_confirmed_m1_target_closes_only_shadow_state(self):
        runner, params, executor, rows = self.make_runner()
        strategy = params["strategies"][0]
        bars = self.signal_bars()
        runner._run_shadow_runner(strategy, bars, type("Info", (), {"bid": 2064.0, "ask": 2064.03})())
        core_before = list(runner._st(strategy)["basket"])

        extra = bars.iloc[[-1]].copy()
        extra.index = pd.DatetimeIndex([bars.index[-1] + pd.Timedelta(minutes=1)])
        next_bars = pd.concat([bars, extra])
        runner._run_shadow_runner(strategy, next_bars, type("Info", (), {"bid": 2100.0, "ask": 2100.03})())

        self.assertEqual(runner._st(strategy)["shadow_runner"]["basket"], [])
        self.assertEqual(runner._st(strategy)["basket"], core_before)
        self.assertEqual(executor.open_calls, 0)
        self.assertEqual(executor.close_calls, 0)
        self.assertTrue(any(event == "runner_basket_close" and row.get("reason") == "basket_target" for event, row in rows))

    def test_signal_reason_funnel_preserves_signal_result(self):
        runner, params, _executor, _rows = self.make_runner()
        strategy = params["strategies"][0]
        bars = self.signal_bars()
        side, reason = runner._signal_decision(bars.iloc[-1], strategy)
        self.assertEqual((side, reason), ("LONG", "long_signal"))

        outside = bars.iloc[-1].copy()
        outside.name = pd.Timestamp("2026-01-01T10:00:00Z")
        side, reason = runner._signal_decision(outside, strategy)
        self.assertIsNone(side)
        self.assertEqual(reason, "outside_session")


if __name__ == "__main__":
    unittest.main(verbosity=2)
