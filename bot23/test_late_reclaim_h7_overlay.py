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


if __name__ == "__main__":
    unittest.main()
