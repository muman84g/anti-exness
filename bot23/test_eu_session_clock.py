"""Boundary regression tests for bot23's shared EU research clock."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import pandas as pd

from eu_entry_admission_clock import classify_entry_admission, is_eu_summer_time, is_us_summer_time
from position_lifecycle_clock import fixed_hold_due_at
from jst1300_pre_eu30_strategy import in_entry_session as in_pre_eu30_entry_session


UTC = timezone.utc
JST = timezone(timedelta(hours=9))


def legacy_jst_block_id(at_utc: datetime) -> str | None:
    """Frozen pre-migration classifier used only for behavior-parity proof."""
    eu_summer = is_eu_summer_time(at_utc)
    us_summer = is_us_summer_time(at_utc)
    local = at_utc.astimezone(JST)
    minute = local.hour * 60 + local.minute
    pre_eu_end = 15 * 60 + 30 + (0 if eu_summer else 60)
    us_preopen = 20 * 60 + 30 + (0 if us_summer else 60)
    us_late_end = 5 * 60 + 30 + (0 if us_summer else 60)
    if 13 * 60 <= minute < pre_eu_end:
        return "jst1300_pre_eu30"
    if pre_eu_end <= minute < us_preopen:
        return "eu_open_to_us_preopen"
    if minute >= us_preopen or minute < us_late_end:
        return "us_to_eu_late"
    return None


class EntryAdmissionClockTests(unittest.TestCase):
    def assert_block(self, stamp: datetime, expected: str | None) -> None:
        block = classify_entry_admission(stamp)
        self.assertEqual(block.id if block else None, expected)

    def test_summer_boundaries(self):
        self.assert_block(datetime(2026, 8, 28, 6, 29, tzinfo=UTC), "jst1300_pre_eu30")
        self.assert_block(datetime(2026, 8, 28, 6, 30, tzinfo=UTC), "eu_open_to_us_preopen")
        self.assert_block(datetime(2026, 8, 28, 11, 30, tzinfo=UTC), "us_to_eu_late")
        self.assert_block(datetime(2026, 8, 28, 20, 29, tzinfo=UTC), "us_to_eu_late")
        self.assert_block(datetime(2026, 8, 28, 20, 30, tzinfo=UTC), None)
        self.assert_block(datetime(2026, 8, 29, 4, 0, tzinfo=UTC), "jst1300_pre_eu30")

    def test_standard_time_boundaries(self):
        self.assert_block(datetime(2026, 12, 1, 7, 29, tzinfo=UTC), "jst1300_pre_eu30")
        self.assert_block(datetime(2026, 12, 1, 7, 30, tzinfo=UTC), "eu_open_to_us_preopen")
        self.assert_block(datetime(2026, 12, 1, 12, 30, tzinfo=UTC), "us_to_eu_late")
        self.assert_block(datetime(2026, 12, 1, 21, 29, tzinfo=UTC), "us_to_eu_late")
        self.assert_block(datetime(2026, 12, 1, 21, 30, tzinfo=UTC), None)
        self.assert_block(datetime(2026, 12, 2, 4, 0, tzinfo=UTC), "jst1300_pre_eu30")

    def test_reported_boundaries_are_utc(self):
        summer = classify_entry_admission(datetime(2026, 8, 28, 6, 30, tzinfo=UTC))
        standard = classify_entry_admission(datetime(2026, 12, 1, 12, 30, tzinfo=UTC))
        self.assertEqual((summer.start_utc, summer.end_utc), ("06:30", "11:30"))
        self.assertEqual((standard.start_utc, standard.end_utc), ("12:30", "21:30"))
        self.assertFalse(hasattr(summer, "start_jst"))

    def test_utc_classifier_matches_frozen_jst_behavior_every_minute(self):
        # Ordinary summer/standard days, both EU/US mismatch windows, and all
        # four 2026 DST transition dates are compared minute by minute.
        dates = (
            (2026, 8, 28), (2026, 12, 1),
            (2026, 3, 20), (2026, 10, 30),
            (2026, 3, 8), (2026, 3, 29),
            (2026, 10, 25), (2026, 11, 1),
        )
        for year, month, day in dates:
            start = datetime(year, month, day, tzinfo=UTC)
            for offset in range(24 * 60):
                stamp = start + timedelta(minutes=offset)
                block = classify_entry_admission(stamp)
                self.assertEqual(
                    block.id if block else None,
                    legacy_jst_block_id(stamp),
                    stamp.isoformat(),
                )

    def test_pre_eu30_strategy_uses_the_shared_dynamic_boundary(self):
        self.assertTrue(in_pre_eu30_entry_session(datetime(2026, 8, 28, 6, 29, tzinfo=UTC)))
        self.assertFalse(in_pre_eu30_entry_session(datetime(2026, 8, 28, 6, 30, tzinfo=UTC)))
        self.assertTrue(in_pre_eu30_entry_session(datetime(2026, 12, 1, 7, 29, tzinfo=UTC)))
        self.assertFalse(in_pre_eu30_entry_session(datetime(2026, 12, 1, 7, 30, tzinfo=UTC)))

    def test_2026_london_clock_change_instants(self):
        self.assertFalse(is_eu_summer_time(datetime(2026, 3, 29, 0, 59, tzinfo=UTC)))
        self.assertTrue(is_eu_summer_time(datetime(2026, 3, 29, 1, 0, tzinfo=UTC)))
        self.assertTrue(is_eu_summer_time(datetime(2026, 10, 25, 0, 59, tzinfo=UTC)))
        self.assertFalse(is_eu_summer_time(datetime(2026, 10, 25, 1, 0, tzinfo=UTC)))

    def test_2026_new_york_clock_change_instants(self):
        self.assertFalse(is_us_summer_time(datetime(2026, 3, 8, 6, 59, tzinfo=UTC)))
        self.assertTrue(is_us_summer_time(datetime(2026, 3, 8, 7, 0, tzinfo=UTC)))
        self.assertTrue(is_us_summer_time(datetime(2026, 11, 1, 5, 59, tzinfo=UTC)))
        self.assertFalse(is_us_summer_time(datetime(2026, 11, 1, 6, 0, tzinfo=UTC)))

    def test_eu_us_dst_mismatch_uses_each_market_clock(self):
        # On 20 March London is still standard-time while New York is already
        # on daylight time. The EU boundary therefore stays at JST16:30, while
        # the US boundary independently moves to JST20:30.
        eu_wait = datetime(2026, 3, 20, 6, 30, tzinfo=UTC)  # JST15:30
        us_open = datetime(2026, 3, 20, 11, 30, tzinfo=UTC)  # JST20:30
        self.assertFalse(is_eu_summer_time(eu_wait))
        self.assertTrue(is_us_summer_time(eu_wait))
        self.assert_block(eu_wait, "jst1300_pre_eu30")
        self.assert_block(us_open, "us_to_eu_late")

    def test_autumn_eu_us_dst_mismatch_uses_each_market_clock(self):
        # London has returned to standard time while New York remains on DST.
        eu_open = datetime(2026, 10, 30, 7, 30, tzinfo=UTC)  # JST16:30
        us_open = datetime(2026, 10, 30, 11, 30, tzinfo=UTC)  # JST20:30
        self.assertFalse(is_eu_summer_time(eu_open))
        self.assertTrue(is_us_summer_time(eu_open))
        self.assert_block(eu_open, "eu_open_to_us_preopen")
        self.assert_block(us_open, "us_to_eu_late")

    def test_naive_datetime_is_rejected(self):
        with self.assertRaises(ValueError):
            classify_entry_admission(datetime(2026, 8, 28, 6, 30))


class PositionLifecycleClockTests(unittest.TestCase):
    def test_summer_position_deadline_crosses_admission_boundary_unchanged(self):
        entry = pd.Timestamp("2026-08-28 06:25:00+00:00")  # JST 15:25
        due = fixed_hold_due_at([entry], 30)
        self.assertEqual(classify_entry_admission(entry.to_pydatetime()).id, "jst1300_pre_eu30")
        self.assertEqual(classify_entry_admission(due.to_pydatetime()).id, "eu_open_to_us_preopen")
        self.assertEqual(due, pd.Timestamp("2026-08-28 06:55:00+00:00"))

    def test_standard_position_deadline_crosses_admission_boundary_unchanged(self):
        entry = pd.Timestamp("2026-12-01 07:25:00+00:00")  # JST 16:25
        due = fixed_hold_due_at([entry], 30)
        self.assertEqual(classify_entry_admission(entry.to_pydatetime()).id, "jst1300_pre_eu30")
        self.assertEqual(classify_entry_admission(due.to_pydatetime()).id, "eu_open_to_us_preopen")
        self.assertEqual(due, pd.Timestamp("2026-12-01 07:55:00+00:00"))

    def test_deadline_is_elapsed_utc_across_dst_change(self):
        entry = pd.Timestamp("2026-03-29 00:50:00+00:00")
        self.assertEqual(
            fixed_hold_due_at([entry], 30),
            pd.Timestamp("2026-03-29 01:20:00+00:00"),
        )

    def test_deadline_uses_earliest_confirmed_fill_for_a_basket(self):
        entries = [
            pd.Timestamp("2026-08-28 06:28:00+00:00"),
            pd.Timestamp("2026-08-28 06:35:00+00:00"),
        ]
        self.assertEqual(
            fixed_hold_due_at(entries, 30),
            pd.Timestamp("2026-08-28 06:58:00+00:00"),
        )


if __name__ == "__main__":
    unittest.main()
