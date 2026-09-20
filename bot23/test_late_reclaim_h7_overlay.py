from __future__ import annotations

import unittest
import json
import os
import tempfile
from unittest.mock import patch

import pandas as pd

from late_reclaim_h7_overlay import MinuteFeature, minute_feature, signal
from raw_tick_shadow_collector import Tick
import live_s23_bot
from live_s23_bot import S23HorizontalInventoryRunner
from test_s23_regressions import make_runner


class LateReclaimH7OverlayTests(unittest.TestCase):
    def test_first45_and_last15_use_boundary_sides(self):
        minute = pd.Timestamp("2026-01-01T00:00:00Z")
        start = int(minute.timestamp() * 1000)
        ticks = [
            Tick(start, 100.0, 100.1, 0.0, 0.0, 1),
            Tick(start + 44_999, 98.0, 98.1, 0.0, 0.0, 1),
            Tick(start + 45_000, 98.5, 98.6, 0.0, 0.0, 1),
            Tick(start + 59_999, 99.5, 99.6, 0.0, 0.0, 1),
        ]
        row = minute_feature(minute, ticks)
        self.assertEqual(row.first45_move, -2.0)
        self.assertEqual(row.last15_move, 1.0)

    def test_frozen_thresholds_emit_long_opportunity(self):
        start = int(pd.Timestamp("2026-01-01T00:00:00Z").timestamp() * 1000)
        rows = []
        close = 100.0
        for index in range(62):
            close += 0.1 if index % 2 else -0.1
            rows.append(MinuteFeature(start + index * 60_000, close, close + 1, close - 1,
                                      close, 0.0, 0.0))
        current = rows[-1]
        rows[-1] = MinuteFeature(current.minute_msc, current.open_bid, current.high_bid,
                                  98.0, 98.1, -1.0, 0.6)
        result = signal(rows)
        self.assertIsNotNone(result)
        self.assertEqual(result["opportunity_id"], "late-reclaim-h7:2026-01-01T01:01:00+00:00")
        self.assertEqual(result["source"], "late_reclaim_h7_frozen_v1")
        self.assertEqual(result["available_time"], result["release_time"])
        self.assertIn("first45_move", result)
        self.assertIn("last15_move", result)

    def test_gap_fails_closed(self):
        start = int(pd.Timestamp("2026-01-01T00:00:00Z").timestamp() * 1000)
        rows = [MinuteFeature(start + i * 60_000, 1, 2, 0, 1, -1, 1) for i in range(62)]
        rows[30] = MinuteFeature(rows[30].minute_msc + 1, 1, 2, 0, 1, -1, 1)
        self.assertIsNone(signal(rows))

    def test_pre_h7_state_migration_preserves_existing_lanes(self):
        seed, _strategy, _state = make_runner(live=True)
        legacy = seed._default_state()
        legacy["routing"].pop("h7_policy_id")
        legacy["routing"].pop("h7_params_hash")
        legacy["routing"].pop("h7_last_condition")
        legacy["routing"].pop("h7_last_signal_minute")
        legacy["routing"].pop("h7_last_evaluated_bar")
        legacy["strategies"].pop(seed._h7_strategies()[0]["id"])
        preserved = seed.params["strategies"][0]["id"]
        legacy["strategies"][preserved]["cooldown_until_bar"] = 12345
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(legacy, handle)
            path = handle.name
        try:
            with patch.object(live_s23_bot, "STATE_FILE", path):
                migrated = S23HorizontalInventoryRunner(json.loads(json.dumps(seed.params)))
            self.assertTrue(migrated._h7_state_migrated)
            self.assertEqual(migrated.state["strategies"][preserved]["cooldown_until_bar"], 12345)
            self.assertEqual(migrated._st(migrated._h7_strategies()[0])["basket"], [])
        finally:
            os.unlink(path)

    def test_identityless_existing_h7_lane_fails_closed_as_invalid_state(self):
        seed, _strategy, _state = make_runner(live=True)
        state = seed._default_state()
        state["routing"].pop("h7_policy_id")
        state["routing"].pop("h7_params_hash")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(state, handle)
            path = handle.name
        try:
            with patch.object(live_s23_bot, "STATE_FILE", path):
                loaded = S23HorizontalInventoryRunner(json.loads(json.dumps(seed.params)))
            lane = loaded._st(loaded._h7_strategies()[0])
            self.assertEqual(lane["sync_block_reason"], "state_identity_mismatch")
            self.assertFalse(lane["sync_block_recoverable"])
        finally:
            os.unlink(path)

    def test_foreign_h7_policy_blocks_only_h7_lane_without_rewrite(self):
        seed, _strategy, _state = make_runner(live=True)
        state = seed._default_state()
        state["routing"]["h7_policy_id"] = "foreign-h7"
        state["routing"]["h7_params_hash"] = "f" * 64
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(state, handle)
            path = handle.name
        try:
            with patch.object(live_s23_bot, "STATE_FILE", path):
                loaded = S23HorizontalInventoryRunner(json.loads(json.dumps(seed.params)))
            lane = loaded._st(loaded._h7_strategies()[0])
            self.assertEqual(lane["sync_block_reason"], "h7_policy_identity_mismatch")
            self.assertIsNone(loaded._st(loaded.params["strategies"][0])["sync_block_reason"])
            self.assertEqual(loaded.state["routing"]["h7_policy_id"], "foreign-h7")
        finally:
            os.unlink(path)

    def test_h7_full_lane_contract_is_preflight_frozen(self):
        for key, bad in (("comment_prefix", "wrong"), ("lot", 0.02), ("hold_minutes", 8),
                         ("max_positions", 2), ("cooldown", 1), ("signal_id", "wrong")):
            runner, _strategy, _state = make_runner(live=True)
            runner.params["h7_strategies"][0][key] = bad
            self.assertIn("invalid_h7_lane_contract", runner._ownership_namespace_error())

    def test_disabled_h7_preserves_namespace_and_blocks_only_new_entries(self):
        runner, _strategy, _state = make_runner(live=True)
        runner.params["h7_enabled"] = False
        h7 = runner._h7_strategies()[0]
        self.assertIsNone(runner._ownership_namespace_error())
        self.assertIn(h7["id"], runner.state["strategies"])
        self.assertEqual(runner._st(h7)["lane_id"], 24)
        with patch.object(runner, "_open_entry") as open_entry:
            runner._process_h7_entries(
                pd.Series({"Close": 2000.0}, name=pd.Timestamp("2026-01-01T00:00:00Z")),
                object(),
                pd.Timestamp("2026-01-01T00:00:01Z"),
                {24: True},
            )
        open_entry.assert_not_called()

    def test_disabled_h7_still_monitors_owned_position_for_close(self):
        runner, _strategy, _state = make_runner(live=True)
        runner.params["h7_enabled"] = False
        strat = runner._h7_strategies()[0]
        runner._st(strat)["basket"] = [{"ticket": 1}]
        with patch.object(runner, "_sync_strategy", return_value=True) as sync, patch.object(
            runner, "_monitor_fixed_hold_position", return_value=False
        ) as monitor:
            readiness = runner._process_h7_exits(object(), pd.Timestamp("2026-01-01T00:00:00Z"))
        sync.assert_called_once_with(strat)
        monitor.assert_called_once()
        self.assertFalse(monitor.call_args.kwargs["defer_for_spread"])
        self.assertFalse(readiness[24])

    def test_h7_fixed_hold_never_defers_for_spread(self):
        runner, _strategy, _state = make_runner(live=True)
        with patch.object(runner, "_sync_strategy", return_value=True), patch.object(
            runner, "_monitor_fixed_hold_position", return_value=False
        ) as monitor:
            runner._process_h7_exits(object(), pd.Timestamp("2026-01-01T00:00:00Z"))
        self.assertFalse(monitor.call_args.kwargs["defer_for_spread"])

    def test_disabled_h7_still_reconciles_unresolved_lane_state(self):
        for state_key, value in (("pending_open_opportunity_id", "h7:pending"),
                                 ("pending_close_reason", "late_reclaim_h7_fixed_hold"),
                                 ("sync_block_new_entries", True)):
            runner, _strategy, _state = make_runner(live=True)
            runner.params["h7_enabled"] = False
            strat = runner._h7_strategies()[0]
            runner._st(strat)[state_key] = value
            with patch.object(runner, "_sync_strategy", return_value=True) as sync, patch.object(
                runner, "_monitor_fixed_hold_position", return_value=False
            ):
                readiness = runner._process_h7_exits(object(), pd.Timestamp("2026-01-01T00:00:00Z"))
            sync.assert_called_once_with(strat)
            self.assertFalse(readiness[24])


if __name__ == "__main__":
    unittest.main()
