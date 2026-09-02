"""No-order tests for the bot24 causal forward state tagger."""

from __future__ import annotations

import ast
import csv
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from shadow_state_tagger import ShadowStateTagger


UTC = timezone.utc


def bars_with_low_rejection() -> pd.DataFrame:
    index = pd.date_range("2026-08-28 12:21:00", periods=40, freq="1min", tz="UTC")
    frame = pd.DataFrame(
        {
            "Open": [100.0] * 40,
            "High": [101.0] * 40,
            "Low": [99.0] * 40,
            "Close": [100.0] * 40,
            "Volume": [50.0] * 40,
            "atr30": [2.0] * 40,
        },
        index=index,
    )
    frame.iloc[-1, frame.columns.get_loc("Open")] = 99.0
    frame.iloc[-1, frame.columns.get_loc("High")] = 101.5
    frame.iloc[-1, frame.columns.get_loc("Low")] = 98.0
    frame.iloc[-1, frame.columns.get_loc("Close")] = 100.5
    frame.iloc[-1, frame.columns.get_loc("Volume")] = 100.0
    return frame


def opportunity() -> dict:
    return {
        "opportunity_id": "XAUUSD|2026-08-28T13:00:00+00:00|SHORT",
        "side": "LONG",
        "raw_side": "SHORT",
        "effective_side": "LONG",
        "event_time": "2026-08-28T13:00:00+00:00",
        "release_time": "2026-08-28T13:01:00+00:00",
        "decision_time": "2026-08-28T13:01:02+00:00",
    }


class ShadowStateTaggerTests(unittest.TestCase):
    def make(self, root: str) -> ShadowStateTagger:
        return ShadowStateTagger(
            {"enabled": True, "csv": "state_tags.csv"},
            log_dir=Path(root) / "logs",
            symbol="XAUUSD",
        )

    def test_csv_path_cannot_escape_the_configured_log_directory(self):
        with tempfile.TemporaryDirectory() as root:
            for value in ("../outside.csv", str(Path(root) / "outside.csv"), "tags.csv:stream"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    ShadowStateTagger(
                        {"enabled": True, "csv": value},
                        log_dir=Path(root) / "logs",
                        symbol="XAUUSD",
                    )

    def test_malformed_existing_csv_rows_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            log_dir = Path(root) / "logs"
            log_dir.mkdir()
            path = log_dir / "state_tags.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(__import__("shadow_state_tagger").TAG_FIELDS)
                writer.writerow([""] * len(__import__("shadow_state_tagger").TAG_FIELDS) + ["extra"])
            with self.assertRaises(RuntimeError):
                self.make(root)

    def test_runtime_csv_corruption_is_rejected_before_next_append(self):
        with tempfile.TemporaryDirectory() as root:
            tagger = self.make(root)
            kwargs = {
                "at": datetime(2026, 8, 28, 13, 1, 2, tzinfo=UTC),
                "bars": bars_with_low_rejection(), "bid": 100.45, "ask": 100.55,
            }
            self.assertTrue(tagger.tag_opportunity(opportunity(), **kwargs))
            with tagger.path.open("a", newline="", encoding="utf-8") as handle:
                csv.writer(handle).writerow([""] * (len(__import__("shadow_state_tagger").TAG_FIELDS) + 1))
            before = tagger.path.read_bytes()
            later = opportunity()
            later["opportunity_id"] += "|later"

            with self.assertRaises(RuntimeError):
                tagger.tag_opportunity(later, **kwargs)
            self.assertEqual(tagger.path.read_bytes(), before)

    def test_causal_low_rejection_tag_and_inventory_are_written(self):
        with tempfile.TemporaryDirectory() as root:
            tagger = self.make(root)
            wrote = tagger.tag_opportunity(
                opportunity(),
                at=datetime(2026, 8, 28, 13, 1, 2, tzinfo=UTC),
                bars=bars_with_low_rejection(),
                bid=100.45,
                ask=100.55,
                context={
                    "long_positions": 3,
                    "short_positions": 1,
                    "lane_positions": {"1": 2, "2": 1, "3": 1, "4": 0},
                    "lane_pending": {"1": False, "2": True, "3": False, "4": False},
                },
            )
            self.assertTrue(wrote)
            with tagger.path.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["raw_side"], "SHORT")
            self.assertEqual(row["effective_side"], "LONG")
            self.assertEqual(row["swept_low"], "1")
            self.assertEqual(row["rejected_low"], "1")
            self.assertEqual(row["rejection_alignment"], "aligned_low_rejection_long")
            self.assertAlmostEqual(float(row["prior20_low"]), 99.0)
            self.assertAlmostEqual(float(row["prior20_high"]), 101.0)
            self.assertAlmostEqual(float(row["activity_ratio_prior30"]), 2.0)
            self.assertAlmostEqual(float(row["ret30"]), 0.5)
            self.assertEqual(row["portfolio_positions"], "4")
            self.assertEqual(row["side_imbalance"], "2")

    def test_nonfinite_or_inverted_historical_ranges_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            for column, value in (("High", float("nan")), ("Low", float("inf"))):
                bars = bars_with_low_rejection()
                bars.iloc[-2, bars.columns.get_loc(column)] = value
                with self.subTest(column=column), self.assertRaises(ValueError):
                    self.make(root).tag_opportunity(
                        opportunity(),
                        at=datetime(2026, 8, 28, 13, 1, 2, tzinfo=UTC),
                        bars=bars,
                        bid=100.45,
                        ask=100.55,
                    )

            bars = bars_with_low_rejection()
            bars.iloc[-2, bars.columns.get_loc("High")] = 98.0
            bars.iloc[-2, bars.columns.get_loc("Low")] = 99.0
            with self.assertRaises(ValueError):
                self.make(root).tag_opportunity(
                    opportunity(),
                    at=datetime(2026, 8, 28, 13, 1, 2, tzinfo=UTC),
                    bars=bars,
                    bid=100.45,
                    ask=100.55,
                )

    def test_duplicate_is_suppressed_in_process_and_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            kwargs = {
                "at": datetime(2026, 8, 28, 13, 1, 2, tzinfo=UTC),
                "bars": bars_with_low_rejection(), "bid": 100.45, "ask": 100.55,
            }
            first = self.make(root)
            self.assertTrue(first.tag_opportunity(opportunity(), **kwargs))
            self.assertFalse(first.tag_opportunity(opportunity(), **kwargs))
            second = self.make(root)
            self.assertFalse(second.tag_opportunity(opportunity(), **kwargs))
            with first.path.open(newline="", encoding="utf-8") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 1)

    def test_module_has_no_order_or_bridge_dependency(self):
        path = Path(__file__).with_name("shadow_state_tagger.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertTrue(imported.isdisjoint({"live_executor", "ea_bridge", "live_data_fetcher", "live_config"}))
        self.assertTrue(attributes.isdisjoint({"open_position", "close_position", "send_command", "modify_position", "cancel_order"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
