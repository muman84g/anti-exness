import sys
import unittest
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from research_entries import (  # noqa: E402
    EVALUATORS,
    LOOKBACKS,
    SESSION_TIMEZONE,
    declared_session_window,
    load_session_calendar,
    validate_calendar_coverage,
)
from live_s23_bot import research_opportunity_clock_fields  # noqa: E402


class ResearchEntryFormulaContractTests(unittest.TestCase):
    def test_direction_and_lookback_contract_is_explicit(self):
        expected = {
            "nwv_checkpoint_restart": ("LONG", 67),
            "alternation_double_break": ("LONG", 16),
            "curvature_fade_short": ("LONG", 68),
            "speed_reversal_long": ("SHORT", 68),
            "nwv_base_centroid_migration": ("SHORT", 45),
            "ir_original_priority_union": ("LONG", 99),
        }
        self.assertEqual(set(expected) - {"ir_original_priority_union"}, set(EVALUATORS))
        for signal_id, (side, lookback) in expected.items():
            if signal_id == "ir_original_priority_union":
                self.assertEqual(side, "LONG")
                self.assertEqual(LOOKBACKS[signal_id], lookback)
                continue
            self.assertEqual(EVALUATORS[signal_id][0], side)
            self.assertEqual(LOOKBACKS[signal_id], lookback)

    def test_calendar_hash_timezone_and_coverage_are_fail_closed(self):
        calendar = load_session_calendar()
        self.assertEqual(calendar["timezone"], SESSION_TIMEZONE)
        validate_calendar_coverage(calendar, pd.Timestamp("2026-09-21T00:00:00Z"))
        self.assertIsNone(
            declared_session_window(pd.Timestamp("2026-09-26T00:00:00Z"), calendar)
        )

    def test_five_clock_uses_receipt_and_separate_cutoff(self):
        clocks = research_opportunity_clock_fields(
            "2026-09-21T13:00:00Z",
            "2026-09-21T13:01:02Z",
            "2026-09-21T13:01:05Z",
        )
        self.assertEqual(clocks["event_time"], "2026-09-21T13:00:00+00:00")
        self.assertEqual(clocks["release_time"], "2026-09-21T13:01:00+00:00")
        self.assertEqual(clocks["ingested_time"], "2026-09-21T13:01:02+00:00")
        self.assertEqual(clocks["available_time"], "2026-09-21T13:01:02+00:00")
        self.assertEqual(clocks["cutoff_time"], "2026-09-21T13:01:05+00:00")
        self.assertLessEqual(clocks["available_time"], clocks["cutoff_time"])
        self.assertNotEqual(clocks["release_time"], clocks["cutoff_time"])

    def test_five_clock_rejects_future_available_data(self):
        with self.assertRaisesRegex(ValueError, "clock order invalid"):
            research_opportunity_clock_fields(
                "2026-09-21T13:00:00Z",
                "2026-09-21T13:01:10Z",
                "2026-09-21T13:01:05Z",
            )


if __name__ == "__main__":
    unittest.main()
