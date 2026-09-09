from __future__ import annotations

import unittest
from types import SimpleNamespace

import pandas as pd

from utc1330_hl_overlay import EXPECTED_CONFIG, apply_policy, config_error
from live_s24_bot import S24NoAdverseRunner


def bars(evaluation_open: float = 101.0, low_after: float = 100.8) -> pd.DataFrame:
    index = pd.date_range("2026-09-10 12:30", "2026-09-10 13:40", freq="1min", tz="UTC")
    frame = pd.DataFrame({"Open": 100.0, "High": 101.2, "Low": 100.8, "Close": 101.0}, index=index)
    frame.loc[pd.Timestamp("2026-09-10 13:20", tz="UTC"), "Open"] = evaluation_open
    frame.loc[pd.Timestamp("2026-09-10 13:30", tz="UTC"), "Open"] = 101.0
    frame.loc[pd.Timestamp("2026-09-10 13:36", tz="UTC"), "Low"] = low_after
    return frame


class Utc1330HlOverlayTests(unittest.TestCase):
    def test_config_is_frozen(self):
        self.assertIsNone(config_error(dict(EXPECTED_CONFIG)))
        bad = dict(EXPECTED_CONFIG)
        bad["qualification_rise_ratio"] = 0.003
        self.assertEqual(config_error(bad), "qualification_rise_ratio")

    def test_nonqualified_long_is_unchanged(self):
        side, detail = apply_policy(bars(100.1), "2026-09-10 13:31Z", "LONG", 100.5, dict(EXPECTED_CONFIG))
        self.assertEqual(side, "LONG")
        self.assertEqual(detail["reason"], "rise_threshold_not_met")

    def test_each_long_signal_in_window_is_inverted(self):
        for minute in range(30, 36):
            side, detail = apply_policy(bars(), f"2026-09-10 13:{minute:02d}Z", "LONG", 101.0, dict(EXPECTED_CONFIG))
            self.assertEqual(side, "SHORT")
            self.assertEqual(detail["action"], "invert_short")

    def test_long_is_blocked_then_rearmed(self):
        side, detail = apply_policy(bars(low_after=100.9), "2026-09-10 13:36Z", "LONG", 100.9, dict(EXPECTED_CONFIG))
        self.assertIsNone(side)
        self.assertEqual(detail["reason"], "long_rearm_not_reached")
        side, detail = apply_policy(bars(low_after=100.7), "2026-09-10 13:36Z", "LONG", 100.7, dict(EXPECTED_CONFIG))
        self.assertEqual(side, "LONG")
        self.assertEqual(detail["reason"], "long_rearmed")

    def test_short_is_never_changed(self):
        side, detail = apply_policy(bars(), "2026-09-10 13:31Z", "SHORT", 101.0, dict(EXPECTED_CONFIG))
        self.assertEqual(side, "SHORT")
        self.assertEqual(detail["reason"], "not_long")

    def test_missing_inputs_fail_long_closed(self):
        frame = bars().drop(pd.Timestamp("2026-09-10 12:30", tz="UTC"))
        side, detail = apply_policy(frame, "2026-09-10 13:31Z", "LONG", 101.0, dict(EXPECTED_CONFIG))
        self.assertIsNone(side)
        self.assertEqual(detail["reason"], "qualification_inputs_unavailable")

    def test_runner_integration_uses_frozen_config(self):
        runner = object.__new__(S24NoAdverseRunner)
        runner.params = {"utc1330_hl": dict(EXPECTED_CONFIG)}
        side, detail = runner._apply_utc1330_hl_policy(
            bars(), "2026-09-10 13:31Z", "LONG", SimpleNamespace(bid=101.0)
        )
        self.assertEqual(side, "SHORT")
        self.assertEqual(detail["policy_id"], EXPECTED_CONFIG["policy_id"])


if __name__ == "__main__":
    unittest.main()
