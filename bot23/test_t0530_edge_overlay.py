# -*- coding: utf-8 -*-

import unittest
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import pandas as pd

import live_executor
import live_s23_bot

from t0530_edge_overlay import (
    POLICY_PARAMS_HASH,
    in_release_session,
    latest_signal,
    signal_series,
)


_CANONICAL_STATE_FILE = Path(live_s23_bot.STATE_FILE).resolve()
_MODULE_RUNTIME_DIR = None
_MODULE_STATE_PATCH = None


def setUpModule():
    global _MODULE_RUNTIME_DIR, _MODULE_STATE_PATCH
    _MODULE_RUNTIME_DIR = tempfile.TemporaryDirectory(prefix="bot23-t0530-")
    state_path = Path(_MODULE_RUNTIME_DIR.name) / "s23_bot_state.json"
    _MODULE_STATE_PATCH = patch.object(live_s23_bot, "STATE_FILE", str(state_path))
    _MODULE_STATE_PATCH.start()


def tearDownModule():
    global _MODULE_RUNTIME_DIR, _MODULE_STATE_PATCH
    if _MODULE_STATE_PATCH is not None:
        _MODULE_STATE_PATCH.stop()
        _MODULE_STATE_PATCH = None
    if _MODULE_RUNTIME_DIR is not None:
        _MODULE_RUNTIME_DIR.cleanup()
        _MODULE_RUNTIME_DIR = None


def bars_ending(at: str, closes: list[float]) -> pd.DataFrame:
    index = pd.date_range(end=pd.Timestamp(at), periods=len(closes), freq="min", tz="UTC")
    values = pd.Series(closes, index=index, dtype=float)
    return pd.DataFrame({"High": values + 0.1, "Low": values - 0.1, "Close": values})


class T0530EdgeOverlayTests(unittest.TestCase):
    def test_no_order_module_uses_only_isolated_state_file(self):
        self.assertNotEqual(Path(live_s23_bot.STATE_FILE).resolve(), _CANONICAL_STATE_FILE)

    def test_policy_hash_is_frozen(self):
        self.assertEqual(
            POLICY_PARAMS_HASH,
            "27d51f6243e74a56e2ad10428f1a1f46e58f2f89a31bd96db4fb7025301d6163",
        )

    def test_summer_and_standard_release_windows_are_dst_aware(self):
        self.assertTrue(in_release_session("2026-07-01T09:30:00Z"))
        self.assertFalse(in_release_session("2026-07-01T10:30:00Z"))
        self.assertTrue(in_release_session("2026-01-07T10:30:00Z"))
        self.assertFalse(in_release_session("2026-01-07T09:30:00Z"))
        self.assertFalse(in_release_session("2026-07-01T10:00:00Z"))

    def test_upper_break_fades_short_at_release_window_start(self):
        closes = [100.0] * 16 + [101.0]
        bars = bars_ending("2026-07-01T09:29:00Z", closes)
        self.assertEqual(latest_signal(bars), "SHORT")

    def test_lower_break_fades_long(self):
        closes = [100.0] * 16 + [99.0]
        bars = bars_ending("2026-07-01T09:29:00Z", closes)
        self.assertEqual(latest_signal(bars), "LONG")

    def test_continuing_break_is_not_a_second_onset(self):
        closes = [100.0] * 16 + [101.0, 102.0]
        bars = bars_ending("2026-07-01T09:30:00Z", closes)
        sides = signal_series(bars)
        self.assertEqual(sides.iloc[-2], "SHORT")
        self.assertTrue(pd.isna(sides.iloc[-1]))
        self.assertIsNone(latest_signal(bars))

    def test_side_flip_is_a_new_onset(self):
        closes = [100.0] * 15 + [101.0, 98.0]
        bars = bars_ending("2026-07-01T09:30:00Z", closes)
        sides = signal_series(bars)
        self.assertEqual(sides.iloc[-2], "SHORT")
        self.assertEqual(sides.iloc[-1], "LONG")

    def test_event_outside_release_window_is_rejected(self):
        closes = [100.0] * 16 + [101.0]
        bars = bars_ending("2026-07-01T09:59:00Z", closes)
        self.assertIsNone(latest_signal(bars))

    def test_break_continuing_from_before_window_is_onset_at_window_boundary(self):
        closes = [100.0] * 15 + [101.0, 102.0]
        bars = bars_ending("2026-07-01T09:29:00Z", closes)
        self.assertEqual(latest_signal(bars), "SHORT")

    def test_non_monotonic_or_missing_history_fails_closed(self):
        bars = bars_ending("2026-07-01T09:29:00Z", [100.0] * 16 + [101.0])
        with self.assertRaises(ValueError):
            latest_signal(bars.iloc[::-1])
        with self.assertRaises(ValueError):
            latest_signal(bars.drop(columns=["High"]))
        gap_bars = bars_ending("2026-07-01T09:29:00Z", [100.0] * 17 + [101.0])
        with self.assertRaises(ValueError):
            latest_signal(gap_bars.drop(index=gap_bars.index[-2]))


class T0530EdgeBotIntegrationTests(unittest.TestCase):
    def make_runner(self):
        params = json.loads(json.dumps(live_s23_bot.load_params()))
        for key in (
            "shadow_opportunity_observer", "shadow_state_tagger",
            "midday_shadow_opportunity_observer", "midday_shadow_state_tagger",
            "pre_eu30_shadow_opportunity_observer", "pre_eu30_shadow_state_tagger",
        ):
            params[key]["enabled"] = False
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            runner = live_s23_bot.S23HorizontalInventoryRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        return runner

    def arm_retry(self, runner, lane, *, event="2026-07-01T09:29:00+00:00"):
        event_time = pd.Timestamp(event)
        release = event_time + pd.Timedelta(minutes=1)
        side = "SHORT"
        opportunity = {
            "opportunity_id": f"XAUUSD|{event_time.isoformat()}|t0530_edge_break_fade|{side}",
            "source": "t0530_edge_break_fade", "side": side,
            "raw_side": side, "effective_side": side,
            "event_time": event_time.isoformat(), "release_time": release.isoformat(),
            "available_time": release.isoformat(), "decision_time": release.isoformat(),
            "executable_at": release.isoformat(),
        }
        runner._st(lane)["t0530_edge_retry_opportunity"] = {
            "opportunity": opportunity,
            "expires_utc": (release + pd.Timedelta(minutes=5)).isoformat(),
            "note": "t0530_edge_w15_onset_hold_15m",
        }
        runner.state["routing"]["t0530_edge_last_evaluated_bar"] = event_time.isoformat()
        return opportunity

    def test_config_is_enabled_and_owns_four_new_namespaces(self):
        runner = self.make_runner()
        self.assertEqual(
            runner.params["candidate_id"],
            live_s23_bot.EXPECTED_CANDIDATE_ID,
        )
        self.assertTrue(runner.params["t0530_edge_enabled"])
        lanes = runner._t0530_edge_strategies()
        self.assertEqual([row["lane_id"] for row in lanes], [18, 19, 20, 21])
        self.assertEqual([row["magic"] for row in lanes], [230040, 230041, 230042, 230043])
        self.assertEqual(len(runner._all_strategies()), 22)
        live_s23_bot.validate_boolean_config(runner.params)
        live_s23_bot.validate_strategy_topology_config(runner.params)
        live_s23_bot.validate_execution_numeric_config(runner.params)
        self.assertEqual(live_executor.S23_OPEN_POLICY[230043], "s23_ed_l4")

    def test_one_signal_submits_only_first_available_private_lane(self):
        runner = self.make_runner()
        runner.params["t0530_edge_enabled"] = True
        lanes = runner._t0530_edge_strategies()
        bars = bars_ending("2026-07-01T09:29:00Z", [100.0] * 16 + [101.0])
        bars["Open"] = bars["Close"]
        bars["AskOpen"] = bars["Close"] + 0.03
        row = bars.iloc[-1]
        info = type("Info", (), {"bid": 101.0, "ask": 101.03, "quote_time_msc": 1782898201000})()
        calls = []

        def open_once(strat, side, _row, _info, **kwargs):
            calls.append((strat["id"], side, kwargs["opportunity"]))
            runner._st(strat)["basket"].append({"opportunity_id": kwargs["opportunity"]["opportunity_id"]})
            return True

        runner._open_entry = open_once
        runner._process_t0530_edge_entries(
            bars, row, info, pd.Timestamp("2026-07-01T09:30:01Z"),
            {int(lane["lane_id"]): True for lane in lanes},
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "ny0530_edge_lane_1")
        self.assertEqual(calls[0][1], "SHORT")
        self.assertEqual(calls[0][2]["source"], "t0530_edge_break_fade")
        runner._process_t0530_edge_entries(
            bars, row, info, pd.Timestamp("2026-07-01T09:30:02Z"),
            {int(lane["lane_id"]): True for lane in lanes},
        )
        self.assertEqual(len(calls), 1)

    def test_group_capacity_four_blocks_a_fifth_open(self):
        runner = self.make_runner()
        runner.params["t0530_edge_enabled"] = True
        lanes = runner._t0530_edge_strategies()
        for lane in lanes:
            runner._st(lane)["basket"] = [{"opportunity_id": f"owned-{lane['lane_id']}"}]
        bars = bars_ending("2026-07-01T09:29:00Z", [100.0] * 16 + [101.0])
        bars["Open"] = bars["Close"]
        bars["AskOpen"] = bars["Close"] + 0.03
        info = type("Info", (), {"bid": 101.0, "ask": 101.03})()
        called = []
        runner._open_entry = lambda *args, **kwargs: called.append((args, kwargs))
        runner._process_t0530_edge_entries(
            bars, bars.iloc[-1], info, pd.Timestamp("2026-07-01T09:30:01Z"),
            {int(lane["lane_id"]): True for lane in lanes},
        )
        self.assertEqual(called, [])

    def test_disabled_group_still_monitors_owned_position(self):
        runner = self.make_runner()
        lane = runner._t0530_edge_strategies()[0]
        runner._st(lane)["basket"] = [{"ticket": 1}]
        synced = []
        monitored = []
        runner._sync_strategy = lambda strat: synced.append(strat["id"]) or True
        runner._monitor_t0530_edge_position = lambda strat, info, at: monitored.append(strat["id"]) or False
        info = type("Info", (), {"bid": 100.0, "ask": 100.03})()
        runner._process_t0530_edge_exits(info, pd.Timestamp("2026-07-01T12:00:00Z"))
        self.assertEqual(synced, [lane["id"]])
        self.assertEqual(monitored, [lane["id"]])

    def test_existing_overlay_entry_order_is_preserved(self):
        source = Path(live_s23_bot.__file__).read_text(encoding="utf-8")
        run_once = source[source.index("    def run_once(self)"):]
        self.assertLess(
            run_once.index("self._process_session_vwap_entries(info, poll_time"),
            run_once.index("self._process_t0530_edge_entries("),
        )

    def test_pre_edge_state_migration_preserves_every_existing_lane(self):
        seed = self.make_runner()
        legacy = seed._default_state()
        legacy["routing"].pop("t0530_edge_policy_id")
        legacy["routing"].pop("t0530_edge_params_hash")
        legacy["routing"].pop("t0530_edge_last_evaluated_bar")
        new_ids = {row["id"] for row in seed._t0530_edge_strategies()}
        for strategy_id in new_ids:
            legacy["strategies"].pop(strategy_id)
        for lane_state in legacy["strategies"].values():
            lane_state.pop("t0530_edge_retry_opportunity")
        legacy["strategies"]["za_horizontal_lane_1"]["daily_realized_pnl_usd"] = -12.34
        legacy["strategies"]["ny0530_session_vwap_lane_5"]["basket_sequence"] = 77
        params = json.loads(json.dumps(seed.params))
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "s23_bot_state.json"
            state_path.write_text(json.dumps(legacy), encoding="utf-8")
            with patch.object(live_s23_bot, "STATE_FILE", str(state_path)):
                migrated = live_s23_bot.S23HorizontalInventoryRunner(params)
        self.assertEqual(migrated._st(params["strategies"][0])["daily_realized_pnl_usd"], -12.34)
        self.assertEqual(migrated.state["strategies"]["ny0530_session_vwap_lane_5"]["basket_sequence"], 77)
        self.assertTrue(migrated._t0530_edge_state_migrated)
        for lane in migrated._t0530_edge_strategies():
            self.assertEqual(migrated._st(lane)["basket"], [])
            self.assertEqual(migrated._st(lane)["basket_sequence"], 0)

    def test_corrupt_group_receipt_blocks_all_private_lanes(self):
        runner = self.make_runner()
        runner.params["t0530_edge_enabled"] = True
        runner.state["routing"]["t0530_edge_last_evaluated_bar"] = 1782898140
        bars = bars_ending("2026-07-01T09:29:00Z", [100.0] * 16 + [101.0])
        bars["Open"] = bars["Close"]
        bars["AskOpen"] = bars["Close"] + 0.03
        info = type("Info", (), {"bid": 101.0, "ask": 101.03})()
        runner._process_t0530_edge_entries(
            bars, bars.iloc[-1], info, pd.Timestamp("2026-07-01T09:30:01Z"),
            {lane_id: True for lane_id in (18, 19, 20, 21)},
        )
        self.assertTrue(all(
            runner._st(lane)["sync_block_reason"] == "t0530_edge_decision_receipt_state_invalid"
            for lane in runner._t0530_edge_strategies()
        ))

    def test_crash_before_open_reservation_retries_without_duplication_fallthrough(self):
        runner = self.make_runner()
        lane = runner._t0530_edge_strategies()[0]
        opportunity = self.arm_retry(runner, lane)
        calls = []
        runner._attempt_t0530_edge_open = lambda *args, **kwargs: calls.append(args[1]) or False
        row = pd.Series({"Close": 101.0}, name=pd.Timestamp(opportunity["event_time"]))
        info = type("Info", (), {"bid": 101.0, "ask": 101.03})()
        consumed = runner._process_t0530_edge_retries(
            info, pd.Timestamp("2026-07-01T09:30:01Z"), {18: True}, row,
        )
        self.assertTrue(consumed)
        self.assertEqual(calls, [opportunity])

    def test_retry_never_submits_before_release(self):
        runner = self.make_runner()
        lane = runner._t0530_edge_strategies()[0]
        opportunity = self.arm_retry(runner, lane)
        calls = []
        runner._attempt_t0530_edge_open = lambda *args, **kwargs: calls.append(args) or False
        row = pd.Series({"Close": 101.0}, name=pd.Timestamp(opportunity["event_time"]))
        info = type("Info", (), {"bid": 101.0, "ask": 101.03})()
        self.assertTrue(runner._process_t0530_edge_retries(
            info, pd.Timestamp("2026-07-01T09:29:59Z"), {18: True}, row,
        ))
        self.assertEqual(calls, [])

    def test_numeric_retry_clock_is_rejected_fail_closed(self):
        runner = self.make_runner()
        lane = runner._t0530_edge_strategies()[0]
        opportunity = self.arm_retry(runner, lane)
        runner._st(lane)["open_retry_after_utc"] = 1782898201
        row = pd.Series({"Close": 101.0}, name=pd.Timestamp(opportunity["event_time"]))
        info = type("Info", (), {"bid": 101.0, "ask": 101.03})()
        runner._process_t0530_edge_retries(
            info, pd.Timestamp("2026-07-01T09:30:01Z"), {18: True}, row,
        )
        state = runner._st(lane)
        self.assertIsNone(state["t0530_edge_retry_opportunity"])
        self.assertEqual(state["sync_block_reason"], "t0530_edge_retry_clock_invalid")


if __name__ == "__main__":
    unittest.main()
