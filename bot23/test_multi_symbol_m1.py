import unittest
from unittest.mock import Mock

import pandas as pd

from multi_symbol_m1 import (
    fetch_completed_m1_snapshot,
    jst1113_usd_accel_pre_session_short,
    valid_history_symbol_map,
)


def bars(last="2026-09-10 03:00:00+00:00", rising=True):
    index = pd.date_range(end=last, periods=140, freq="min", tz="UTC")
    values = [100.0 + (i if rising else -i) * 0.01 for i in range(140)]
    return pd.DataFrame({"Open": values, "High": values, "Low": values, "Close": values, "Volume": 1}, index=index)


class MultiSymbolM1Tests(unittest.TestCase):
    def test_snapshot_is_available_at_bar_end_and_allows_any_configured_symbols(self):
        dm = Mock()
        dm.get_historical_data.side_effect = lambda *_a, **_k: bars()
        snap, reason = fetch_completed_m1_snapshot(
            dm, {"EURUSD": "EURUSD", "GBPUSD": "GBPUSD", "AUDUSD": "AUDUSD", "USDJPY": "USDJPY"},
            decision_time="2026-09-10 03:01:30+00:00",
        )
        self.assertEqual(reason, "ok")
        self.assertIsNotNone(snap)
        self.assertEqual(dm.get_historical_data.call_count, 4)

    def test_snapshot_uses_last_available_bar_and_fails_closed_when_stale(self):
        dm = Mock(); dm.get_historical_data.return_value = bars()
        snap, reason = fetch_completed_m1_snapshot(dm, {"EURUSD": "EURUSD"}, decision_time="2026-09-10 03:00+00:00")
        self.assertEqual(reason, "ok")
        self.assertEqual(snap.bars["EURUSD"].index[-1], pd.Timestamp("2026-09-10 02:59:00+00:00"))
        self.assertIn("stale", fetch_completed_m1_snapshot(dm, {"EURUSD": "EURUSD"}, decision_time="2026-09-10 03:04:00+00:00")[1])

    def test_snapshot_trims_bars_released_after_decision_clock(self):
        dm = Mock(); dm.get_historical_data.return_value = bars(last="2026-09-10 03:02:00+00:00")
        snap, reason = fetch_completed_m1_snapshot(
            dm, {"EURUSD": "EURUSD"}, decision_time="2026-09-10 03:01:00+00:00",
        )
        self.assertEqual(reason, "ok")
        self.assertEqual(snap.bars["EURUSD"].index[-1], pd.Timestamp("2026-09-10 03:00:00+00:00"))

    def test_symbol_map_rejects_aliasing_and_command_delimiters(self):
        self.assertFalse(valid_history_symbol_map({"EURUSD": "EURUSD", "GBPUSD": "EURUSD"}))
        self.assertFalse(valid_history_symbol_map({"EURUSD": "EURUSD|1|140"}))
        dm = Mock()
        snap, reason = fetch_completed_m1_snapshot(
            dm,
            {"EURUSD": "EURUSD", "GBPUSD": "eurusd"},
            decision_time="2026-09-10 03:01:00+00:00",
        )
        self.assertIsNone(snap)
        self.assertEqual(reason, "invalid_multi_symbol_contract")
        dm.get_historical_data.assert_not_called()

    def test_literal_signal_is_short_only_and_causal(self):
        dm = Mock()
        mapping = {"EURUSD": "EURUSD", "GBPUSD": "GBPUSD", "AUDUSD": "AUDUSD", "USDJPY": "USDJPY"}
        dm.get_historical_data.side_effect = [bars(rising=False), bars(rising=False), bars(rising=False), bars(rising=True)]
        snap, _ = fetch_completed_m1_snapshot(dm, mapping, decision_time="2026-09-10 03:01:30+00:00")
        ok, reason = jst1113_usd_accel_pre_session_short(bars(rising=True), snap)
        self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main()
