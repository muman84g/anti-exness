# -*- coding: utf-8 -*-

from __future__ import annotations

import json
from pathlib import Path
import unittest

import numpy as np
import pandas as pd

from live_data_fetcher import MT5DataManager
from session_vwap_overlay import (
    PagedM1History,
    entry_history_issue,
    in_entry_session,
    latest_signal,
    signal_frame,
)


ROOT = Path(__file__).resolve().parent


def bars(index: pd.DatetimeIndex, close: np.ndarray | None = None) -> pd.DataFrame:
    values = np.asarray(close if close is not None else np.linspace(2000.0, 2001.0, len(index)))
    return pd.DataFrame(
        {
            "Open": values,
            "High": values + 0.2,
            "Low": values - 0.2,
            "Close": values,
            "Volume": np.full(len(index), 10),
        },
        index=index,
    )


class FakePagedDM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get_historical_page(self, symbol, timeframe, start_pos, count, timezone):
        self.calls.append((symbol, timeframe, start_pos, count, timezone))
        return self.responses.pop(0)


class SessionVwapOverlayTests(unittest.TestCase):
    def test_history_parser_rejects_extended_or_normalized_rows_in_all_modes(self):
        manager = MT5DataManager()
        responses = (
            "OK|2026.07.01 09:28,2000,2001,1999,2000.5,10,extra",
            "OK|2026.07.01 09:28,2000,2001,1999,2000.5,10 ",
        )
        for response in responses:
            for strict_rows in (False, True):
                with self.subTest(response=response, strict_rows=strict_rows):
                    self.assertIsNone(
                        manager._parse_history_response(
                            response,
                            "UTC",
                            drop_latest=False,
                            strict_rows=strict_rows,
                        )
                    )

    def test_histpage_parser_rejects_any_malformed_row_without_partial_success(self):
        manager = MT5DataManager()
        response = (
            "OK|2026.07.01 09:28,2000,2001,1999,2000.5,10"
            "|2026.07.01 09:29,broken,2001,1999,2000.5,10"
        )
        self.assertIsNone(
            manager._parse_history_response(
                response,
                "UTC",
                drop_latest=False,
                strict_rows=True,
            )
        )
        legacy = manager._parse_history_response(
            response,
            "UTC",
            drop_latest=False,
            strict_rows=False,
        )
        self.assertIsNone(legacy)

    def test_history_parser_rejects_short_success_against_requested_count(self):
        manager = MT5DataManager()
        response = "OK|2026.07.01 09:28,2000,2001,1999,2000.5,10"
        self.assertIsNone(
            manager._parse_history_response(
                response,
                "UTC",
                drop_latest=False,
                strict_rows=True,
                expected_rows=2,
            )
        )
        complete = manager._parse_history_response(
            response,
            "UTC",
            drop_latest=False,
            strict_rows=True,
            expected_rows=1,
        )
        self.assertIsNotNone(complete)
        self.assertEqual(len(complete), 1)

    def test_histpage_parser_rejects_fractional_volume(self):
        manager = MT5DataManager()
        response = "OK|2026.07.01 09:28,2000,2001,1999,2000.5,10.5"
        self.assertIsNone(
            manager._parse_history_response(
                response,
                "UTC",
                drop_latest=False,
                strict_rows=True,
            )
        )

    def test_histpage_parser_rejects_explicitly_signed_volume(self):
        manager = MT5DataManager()
        response = "OK|2026.07.01 09:28,2000,2001,1999,2000.5,+10"
        self.assertIsNone(
            manager._parse_history_response(
                response,
                "UTC",
                drop_latest=False,
                strict_rows=True,
            )
        )

    def test_histpage_parser_rejects_duplicate_or_reversed_timestamps(self):
        manager = MT5DataManager()
        malformed = (
            "OK|2026.07.01 09:28,2000,2001,1999,2000.5,10"
            "|2026.07.01 09:28,2000,2001,1999,2000.6,11",
            "OK|2026.07.01 09:29,2000,2001,1999,2000.5,10"
            "|2026.07.01 09:28,2000,2001,1999,2000.4,11",
        )
        for response in malformed:
            with self.subTest(response=response):
                self.assertIsNone(
                    manager._parse_history_response(
                        response,
                        "UTC",
                        drop_latest=False,
                        strict_rows=True,
                    )
                )

    def test_histpage_parser_rejects_invalid_timestamp_without_exception(self):
        manager = MT5DataManager()
        response = "OK|not-a-time,2000,2001,1999,2000.5,10"
        self.assertIsNone(
            manager._parse_history_response(
                response,
                "UTC",
                drop_latest=False,
                strict_rows=True,
            )
        )

    def test_history_parser_rejects_noncanonical_but_parseable_timestamp(self):
        manager = MT5DataManager()
        for timestamp in ("2026-07-01T09:28:00Z", "07/01/2026 09:28"):
            with self.subTest(timestamp=timestamp):
                response = f"OK|{timestamp},2000,2001,1999,2000.5,10"
                self.assertIsNone(
                    manager._parse_history_response(
                        response,
                        "UTC",
                        drop_latest=False,
                        strict_rows=True,
                    )
                )

    def test_history_parser_rejects_nonfinite_or_inverted_ohlc_in_all_modes(self):
        manager = MT5DataManager()
        malformed = (
            "OK|2026.07.01 09:28,nan,2001,1999,2000.5,10",
            "OK|2026.07.01 09:28,2000,1999,1998,2000.5,10",
            "OK|2026.07.01 09:28,2000,2001,2000.5,1999,10",
            "OK|2026.07.01 09:28,2000,2001,1999,2000.5,-1",
        )
        for strict_rows in (False, True):
            for response in malformed:
                with self.subTest(strict_rows=strict_rows, response=response):
                    self.assertIsNone(
                        manager._parse_history_response(
                            response,
                            "UTC",
                            drop_latest=False,
                            strict_rows=strict_rows,
                        )
                    )

    def test_new_overlay_is_preinstalled_disabled_with_private_namespace(self):
        params = json.loads((ROOT / "s23_params.json").read_text(encoding="utf-8"))
        self.assertFalse(params["session_vwap_enabled"])
        lanes = params["session_vwap_strategies"]
        self.assertEqual([row["lane_id"] for row in lanes], [13, 14, 15, 16, 17])
        self.assertEqual([row["magic"] for row in lanes], [230035, 230036, 230037, 230038, 230039])
        all_magics = [row["magic"] for key in (
            "strategies", "morning_session_strategies", "midday_session_strategies",
            "pre_eu30_session_strategies", "trend_recovery_strategies", "session_vwap_strategies",
        ) for row in params[key]]
        self.assertEqual(len(all_magics), len(set(all_magics)))

    def test_dst_aware_new_york_session(self):
        self.assertTrue(in_entry_session("2026-07-01T09:30:00Z"))
        self.assertFalse(in_entry_session("2026-07-01T12:30:00Z"))
        self.assertTrue(in_entry_session("2026-01-07T10:30:00Z"))
        self.assertFalse(in_entry_session("2026-01-07T13:30:00Z"))

    def test_session_boundaries_use_completed_bar_available_time(self):
        # Summer NY 05:30 is UTC 09:30.  The M1 event stamped 09:29 is
        # released at 09:30 and must be the first included session bar.
        idx = pd.date_range("2026-07-01T08:59:00Z", "2026-07-01T12:29:00Z", freq="1min")
        frame = signal_frame(bars(idx))
        before = pd.Timestamp("2026-07-01T09:28:00Z")
        first = pd.Timestamp("2026-07-01T09:29:00Z")
        last = pd.Timestamp("2026-07-01T12:28:00Z")
        after = pd.Timestamp("2026-07-01T12:29:00Z")
        self.assertTrue(pd.isna(frame.loc[before, "SessionVWAP"]))
        expected_first = (
            frame.loc[first, "High"] + frame.loc[first, "Low"] + frame.loc[first, "Close"]
        ) / 3.0
        self.assertAlmostEqual(float(frame.loc[first, "SessionVWAP"]), float(expected_first))
        self.assertFalse(pd.isna(frame.loc[last, "SessionVWAP"]))
        self.assertTrue(pd.isna(frame.loc[after, "SessionVWAP"]))

    def test_atr60_warms_after_frozen_minimum_thirty_bars(self):
        idx = pd.date_range("2026-07-01T08:00:00Z", periods=31, freq="1min")
        frame = signal_frame(bars(idx))
        self.assertTrue(pd.isna(frame["ATR60"].iloc[28]))
        self.assertFalse(pd.isna(frame["ATR60"].iloc[29]))

    def test_entry_history_rejects_span_only_sparse_data(self):
        idx = pd.DatetimeIndex(
            [pd.Timestamp("2026-06-10T09:29:00Z"), pd.Timestamp("2026-07-01T09:29:00Z")]
        )
        self.assertEqual(
            entry_history_issue(bars(idx), "2026-07-01T09:30:01Z"),
            "atr_tail_not_contiguous",
        )

    def test_entry_history_rejects_stale_latest_completed_bar(self):
        idx = pd.date_range("2026-06-10T09:00:00Z", "2026-07-01T09:28:00Z", freq="1min")
        self.assertEqual(
            entry_history_issue(bars(idx), "2026-07-01T09:30:01Z"),
            "latest_completed_m1_missing",
        )

    def test_entry_history_rejects_current_session_gap(self):
        idx = pd.date_range("2026-06-10T09:00:00Z", "2026-07-01T10:45:00Z", freq="1min")
        idx = idx.delete(idx.get_loc(pd.Timestamp("2026-07-01T09:35:00Z")))
        self.assertEqual(
            entry_history_issue(bars(idx), "2026-07-01T10:46:01Z"),
            "current_session_not_contiguous",
        )

    def test_entry_history_accepts_complete_active_session_history(self):
        idx = pd.date_range("2026-06-10T09:00:00Z", "2026-07-01T10:00:00Z", freq="1min")
        self.assertIsNone(entry_history_issue(bars(idx), "2026-07-01T10:01:01Z"))

    def test_entry_history_rejects_any_nonpositive_relevant_session_volume(self):
        idx = pd.date_range("2026-06-10T09:00:00Z", "2026-07-01T10:00:00Z", freq="1min")
        sample = bars(idx)
        sample.loc[pd.Timestamp("2026-07-01T09:29:00Z"), "Volume"] = 0
        self.assertEqual(
            entry_history_issue(sample, "2026-07-01T10:01:01Z"),
            "session_volume_nonpositive",
        )
        with self.assertRaisesRegex(ValueError, "session_volume_nonpositive"):
            signal_frame(sample)

    def test_history_failure_retries_without_erasing_cache_or_consuming_page(self):
        idx = pd.date_range("2026-06-01", "2026-06-22", freq="1h", tz="UTC")
        complete = bars(idx)
        dm = FakePagedDM([None, complete])
        cache = PagedM1History(dm, symbol="XAUUSD", page_bars=5000, retry_seconds=(5, 15))
        first = cache.advance("2026-06-22T01:00:00Z", monotonic_now=100)
        self.assertFalse(first.ready)
        self.assertEqual(first.reason, "history_fetch_failed")
        waiting = cache.advance("2026-06-22T01:00:00Z", monotonic_now=104)
        self.assertEqual(waiting.reason, "retry_backoff")
        self.assertEqual(len(dm.calls), 1)
        recovered = cache.advance("2026-06-22T01:00:00Z", monotonic_now=105)
        self.assertTrue(recovered.ready)
        self.assertTrue(recovered.fresh)
        self.assertEqual(len(dm.calls), 2)

    def test_first_paged_snapshot_stays_numeric_and_reaches_signal_calculation(self):
        idx = pd.date_range("2026-06-01", "2026-06-22", freq="1min", tz="UTC")
        dm = FakePagedDM([bars(idx)])
        cache = PagedM1History(dm, symbol="XAUUSD")
        snapshot = cache.advance("2026-06-22T00:01:00Z", monotonic_now=10)
        self.assertTrue(snapshot.ready)
        self.assertTrue(all(pd.api.types.is_float_dtype(dtype) for dtype in snapshot.bars.dtypes))
        side, row = latest_signal(snapshot.bars)
        self.assertIn(side, (None, "LONG", "SHORT"))
        self.assertIsNotNone(row)

    def test_invalid_numeric_page_retries_without_consuming_cursor(self):
        idx = pd.date_range("2026-06-01", periods=2, freq="1min", tz="UTC")
        invalid = bars(idx)
        invalid.loc[idx[-1], "High"] = np.inf
        valid = bars(pd.date_range("2026-06-01", "2026-06-22", freq="1h", tz="UTC"))
        dm = FakePagedDM([invalid, valid])
        cache = PagedM1History(dm, symbol="XAUUSD", retry_seconds=(5,))
        failed = cache.advance("2026-06-22T01:00:00Z", monotonic_now=10)
        self.assertEqual(failed.reason, "history_page_invalid")
        self.assertEqual(cache.next_start_pos, 0)
        recovered = cache.advance("2026-06-22T01:00:00Z", monotonic_now=15)
        self.assertTrue(recovered.ready)
        self.assertEqual([call[2] for call in dm.calls], [0, 0])

    def test_invalid_ohlc_page_is_not_admitted(self):
        idx = pd.date_range("2026-06-01", periods=2, freq="1min", tz="UTC")
        invalid = bars(idx)
        invalid.loc[idx[-1], "High"] = invalid.loc[idx[-1], "Low"] - 1.0
        dm = FakePagedDM([invalid])
        cache = PagedM1History(dm, symbol="XAUUSD", retry_seconds=(5,))
        failed = cache.advance("2026-06-22T01:00:00Z", monotonic_now=10)
        self.assertEqual(failed.reason, "history_page_invalid")
        self.assertTrue(failed.bars.empty)

    def test_ready_cache_refreshes_and_retains_old_history_on_failure(self):
        old_idx = pd.date_range("2026-06-01", "2026-06-22", freq="1h", tz="UTC")
        refresh_idx = pd.date_range("2026-06-22T00:51:00Z", periods=10, freq="1min")
        dm = FakePagedDM([bars(old_idx), None, bars(refresh_idx)])
        cache = PagedM1History(dm, symbol="XAUUSD", retry_seconds=(5,))
        ready = cache.advance("2026-06-22T01:00:00Z", monotonic_now=10)
        self.assertTrue(ready.ready)
        retained_len = len(ready.bars)
        failed = cache.advance("2026-06-22T01:01:00Z", monotonic_now=11)
        self.assertFalse(failed.fresh)
        self.assertEqual(len(failed.bars), retained_len)
        refreshed = cache.advance("2026-06-22T01:01:00Z", monotonic_now=16)
        self.assertTrue(refreshed.fresh)
        self.assertEqual(refreshed.bars.index.max(), pd.Timestamp("2026-06-22T01:00:00Z"))

    def test_completed_bar_revision_is_rejected_without_overwriting_cache(self):
        old_idx = pd.date_range("2026-06-01T00:00:00Z", "2026-06-22T01:00:00Z", freq="1h")
        initial = bars(old_idx)
        overlap = initial.tail(2).copy()
        overlap.loc[overlap.index[-1], ["Open", "High", "Low", "Close"]] += 100.0
        dm = FakePagedDM([initial, overlap])
        cache = PagedM1History(dm, symbol="XAUUSD", retry_seconds=(5,))
        ready = cache.advance("2026-06-22T01:01:00Z", monotonic_now=10)
        self.assertTrue(ready.ready)
        original = ready.bars.loc[overlap.index[-1], "Close"]
        conflict = cache.advance("2026-06-22T01:01:00Z", monotonic_now=11)
        self.assertTrue(conflict.ready)
        self.assertFalse(conflict.fresh)
        self.assertEqual(conflict.reason, "completed_bar_revision_conflict")
        self.assertEqual(conflict.bars.loc[overlap.index[-1], "Close"], original)

    def test_continuity_rebackfill_retains_cache_but_isolates_new_coverage(self):
        idx = pd.date_range("2026-06-01", "2026-06-22", freq="1h", tz="UTC")
        initial = bars(idx)
        repaired_new = bars(pd.date_range("2026-06-18T00:00:00Z", "2026-06-22T01:00:00Z", freq="1min"))
        repaired_old = bars(pd.date_range("2026-05-31T23:00:00Z", "2026-06-17T23:00:00Z", freq="1h"))
        dm = FakePagedDM([initial, repaired_new, repaired_old])
        cache = PagedM1History(dm, symbol="XAUUSD", page_bars=5000, refresh_bars=10)
        first = cache.advance("2026-06-22T01:00:00Z", monotonic_now=10)
        self.assertTrue(first.ready)
        retained = len(first.bars)
        cache.request_rebackfill()
        self.assertFalse(cache.ready)
        self.assertEqual(len(cache.bars), retained)
        partial_snapshot = cache.advance("2026-06-22T01:01:00Z", monotonic_now=11)
        self.assertFalse(partial_snapshot.ready)
        self.assertEqual(partial_snapshot.reason, "backfill_in_progress")
        self.assertEqual(len(partial_snapshot.bars), retained)
        self.assertEqual(dm.calls[-1][2:4], (0, 5000))
        repaired_snapshot = cache.advance("2026-06-22T01:01:00Z", monotonic_now=12)
        self.assertTrue(repaired_snapshot.ready)
        self.assertEqual(dm.calls[-1][2:4], (len(repaired_new), 5000))
        self.assertEqual(repaired_snapshot.bars.index.min(), repaired_old.index.min())

    def test_signal_uses_only_latest_onset_and_fades_extension(self):
        idx = pd.date_range(end="2026-07-01T10:00:00Z", periods=21 * 24 * 60, freq="1min")
        values = 2000.0 + np.sin(np.arange(len(idx)) / 13.0) * 0.1
        values[-1] += 8.0
        side, row = latest_signal(bars(idx, values))
        self.assertEqual(side, "SHORT")
        self.assertTrue(bool(row["Onset"]))
        self.assertGreater(float(row["Z"]), float(row["Q90"]))

    def test_bridge_advertises_bounded_histpage(self):
        source = (ROOT / "BotBridge_s23.mq5").read_text(encoding="utf-8")
        self.assertIn("HISTPAGE", source)
        self.assertIn("bars > 5000", source)
        self.assertIn("start_pos > 200000", source)

    def test_paged_backfill_is_scheduled_before_legacy_m1_fetch(self):
        source = (ROOT / "live_s23_bot.py").read_text(encoding="utf-8")
        run_once = source[source.index("    def run_once(self)"):]
        self.assertLess(
            run_once.index("self._refresh_session_vwap_history(info, quote_time)"),
            run_once.index("bars = self._get_m1()"),
        )


if __name__ == "__main__":
    unittest.main()
