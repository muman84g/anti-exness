from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import raw_tick_shadow_collector as raw_module

from raw_tick_shadow_collector import (
    Cursor,
    RawTickShadowCollector,
    acquire_collector_lock,
    collector_lock_path,
    load_collector_config,
    load_csv_recovery_evidence,
    load_cursor_from_csv,
    load_state_cursor,
    load_state_recovery_evidence,
    parse_page,
    reconcile_recovery_evidence,
    reconcile_recovery_cursors,
    release_collector_lock,
)


class FakeBridge:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.commands: list[str] = []

    def send_command(self, cmd_str: str, timeout: float = 10) -> str:
        self.commands.append(cmd_str)
        return self.responses.pop(0)


class RawTickShadowCollectorTests(unittest.TestCase):
    def test_collector_rejects_self_corrupting_identity_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = {
                "bridge": FakeBridge([]),
                "symbol": "XAUUSD",
                "csv_path": root / "ticks.csv",
                "state_path": root / "state.json",
                "recipe_version": "test_v1",
            }
            cases = (
                {"symbol": " XAUUSD"},
                {"symbol": 1},
                {"recipe_version": "test_v1\n"},
                {"recipe_version": 1},
                {"run_id": " run1"},
                {"run_id": "run,1"},
                {"run_id": 1},
            )
            for changed in cases:
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    RawTickShadowCollector(**{**base, **changed})

    def test_config_rejects_duplicate_nonfinite_coercible_or_escaping_values(self):
        base = {
            "mt5_symbol": "XAUUSD",
            "raw_tick_shadow_collector": {
                "enabled": False,
                "recipe_version": "s23_raw_tick_shadow_v1",
                "page_rows": 1000,
                "lookback_seconds_on_first_run": 300,
                "max_catchup_seconds_per_run": 3600,
                "csv": "ticks.csv",
                "state_file": "ticks.json",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "params.json"
            path.write_text(json.dumps(base), encoding="utf-8")
            _params, cfg = load_collector_config(path)
            self.assertFalse(cfg["enabled"])
            malformed = (
                '{"mt5_symbol":"XAUUSD","mt5_symbol":"XAUUSD","raw_tick_shadow_collector":{}}',
                json.dumps({**base, "raw_tick_shadow_collector": {**base["raw_tick_shadow_collector"], "enabled": "false"}}),
                json.dumps({**base, "raw_tick_shadow_collector": {**base["raw_tick_shadow_collector"], "page_rows": True}}),
                json.dumps({**base, "raw_tick_shadow_collector": {**base["raw_tick_shadow_collector"], "csv": "../ticks.csv"}}),
                json.dumps(base).replace('1000', 'NaN', 1),
            )
            for payload in malformed:
                with self.subTest(payload=payload):
                    path.write_text(payload, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_collector_config(path)

    def test_cursor_rejects_noncanonical_or_nonpositive_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ticks.csv"
            for values in (("0", "0", "1"), ("1000", "-1", "1"), ("1000", "0", " 1")):
                with self.subTest(values=values):
                    with path.open("w", encoding="utf-8", newline="") as handle:
                        writer = csv.DictWriter(handle, fieldnames=[
                            "recipe_version", "run_id", "batch_id", "source_sequence",
                            "event_time", "release_time", "ingested_time", "available_time",
                            "cutoff_time", "recorded_at", "broker_time_msc", "bid", "ask",
                            "last", "volume", "flags", "next_from_msc", "next_skip_at_from_msc",
                        ])
                        writer.writeheader()
                        writer.writerow({
                            "source_sequence": values[2], "next_from_msc": values[0],
                            "next_skip_at_from_msc": values[1],
                        })
                    with self.assertRaises(ValueError):
                        load_cursor_from_csv(path)

    def test_cursor_rejects_extended_or_inconsistent_last_evidence_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ticks.csv"
            valid = {
                "recipe_version": "s23_raw_tick_shadow_v1",
                "source_sequence": "1",
                "broker_time_msc": "1000",
                "next_from_msc": "1000",
                "next_skip_at_from_msc": "1",
            }
            cases = (
                ({**valid, "broker_time_msc": "999"}, False),
                ({**valid, "recipe_version": "other"}, False),
                (valid, True),
            )
            for row, add_extra in cases:
                with self.subTest(row=row, add_extra=add_extra):
                    with path.open("w", encoding="utf-8", newline="") as handle:
                        writer = csv.DictWriter(handle, fieldnames=[
                            "recipe_version", "run_id", "batch_id", "source_sequence",
                            "event_time", "release_time", "ingested_time", "available_time",
                            "cutoff_time", "recorded_at", "broker_time_msc", "bid", "ask",
                            "last", "volume", "flags", "next_from_msc", "next_skip_at_from_msc",
                        ])
                        writer.writeheader()
                        writer.writerow(row)
                        if add_extra:
                            handle.seek(0, 2)
                    if add_extra:
                        lines = path.read_text(encoding="utf-8").splitlines()
                        lines[-1] += ",unexpected"
                        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_cursor_from_csv(
                            path,
                            expected_recipe_version="s23_raw_tick_shadow_v1",
                        )
    def test_parse_rejects_unordered_or_crossed_quotes(self):
        with self.assertRaises(ValueError):
            parse_page("OK|META,2,0,1000,1|1001,1,2,0,0,6|1000,1,2,0,0,6")
        with self.assertRaises(ValueError):
            parse_page("OK|META,1,0,1000,1|1000,2,1,0,0,6")

    def test_parse_rejects_nonfinite_or_inconsistent_tick_evidence(self):
        malformed = (
            "OK|META,1,0,1000,1|1000,1,nan,0,0,6",
            "OK|META,1,0,1000,1|1000,1,2,0,-1,6",
            "OK|META,1,2,1000,1|1000,1,2,0,0,6",
            "OK|META,1,0,999,1|1000,1,2,0,0,6",
            "OK|META,1,0,1000,0|1000,1,2,0,0,6",
            "OK|META,1,0,1000,1| 1000,1,2,0,0,6",
            "OK|META,0,1,1000,0",
            "OK|META,2001,0,1000,0",
            "OK|META,1,0,1000,1|1000,1,2,-1,0,6",
            "OK|META,1,0,1000,1|1000,1,2,0,0,4294967296",
            "OK|META,0,0,999,0",
        )
        for response in malformed:
            with self.subTest(response=response), self.assertRaises(ValueError):
                parse_page(response)

    def test_collector_rejects_ticks_outside_requested_window(self):
        responses = (
            "OK|META,1,0,999,1|999,1,2,0,0,6",
            "OK|META,1,0,2001,1|2001,1,2,0,0,6",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for response in responses:
                with self.subTest(response=response):
                    collector = RawTickShadowCollector(
                        bridge=FakeBridge([response]), symbol="XAUUSD",
                        csv_path=root / "ticks.csv", state_path=root / "state.json",
                        recipe_version="test_v1", page_rows=1, run_id="run1",
                    )
                    with self.assertRaises(ValueError):
                        collector.collect_until(Cursor(1000, 0, 0), 2000, max_pages=1)

    def test_append_only_cursor_survives_restart_and_same_millisecond(self):
        page1 = "OK|META,3,1,1001,1|1000,1.0,1.2,0,0,6|1000,1.1,1.3,0,0,6|1001,1.2,1.4,0,0,6"
        page2 = "OK|META,2,0,1002,1|1001,1.3,1.5,0,0,6|1002,1.4,1.6,0,0,6"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path, state_path = root / "ticks.csv", root / "state.json"
            first = RawTickShadowCollector(bridge=FakeBridge([page1]), symbol="XAUUSD", csv_path=csv_path,
                                           state_path=state_path, recipe_version="test_v1", page_rows=3, run_id="run1")
            cursor, count = first.collect_until(Cursor(1000, 0, 0), 2000, max_pages=1)
            self.assertEqual((count, cursor.from_msc, cursor.skip_at_from_msc), (3, 1001, 1))
            restored = load_cursor_from_csv(csv_path)
            self.assertEqual(restored, cursor)
            bridge = FakeBridge([page2])
            second = RawTickShadowCollector(bridge=bridge, symbol="XAUUSD", csv_path=csv_path,
                                            state_path=state_path, recipe_version="test_v1", page_rows=3, run_id="run2")
            cursor, count = second.collect_until(restored, 2000, max_pages=1)
            self.assertEqual(count, 2)
            self.assertIn("TICKS|XAUUSD|1001|2000|3|1", bridge.commands)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([int(row["broker_time_msc"]) for row in rows], [1000, 1000, 1001, 1001, 1002])
            self.assertEqual([int(row["source_sequence"]) for row in rows], [1, 2, 3, 4, 5])
            self.assertEqual(int(rows[-1]["next_skip_at_from_msc"]), 1)
            for row in rows:
                self.assertLessEqual(row["event_time"], row["release_time"])
                self.assertLessEqual(row["release_time"], row["ingested_time"])
                self.assertLessEqual(row["ingested_time"], row["available_time"])

    def test_cursor_validates_every_historical_row_not_only_the_tail(self):
        page = "OK|META,3,0,1002,1|1000,1.0,1.2,0,0,6|1001,1.1,1.3,0,0,6|1002,1.2,1.4,0,0,6"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "ticks.csv"
            collector = RawTickShadowCollector(
                bridge=FakeBridge([page]), symbol="XAUUSD", csv_path=csv_path,
                state_path=root / "state.json", recipe_version="test_v1",
                page_rows=3, run_id="run1",
            )
            cursor, _count = collector.collect_until(Cursor(1000, 0, 0), 2000, max_pages=1)
            self.assertEqual(load_cursor_from_csv(csv_path), cursor)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.reader(handle))
            rows[2][3] = "99"
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerows(rows)
            with self.assertRaisesRegex(ValueError, "continuity"):
                load_cursor_from_csv(csv_path)

    def test_cursor_rejects_missing_first_same_millisecond_tick(self):
        page = "OK|META,1,0,1000,1|1000,1.0,1.2,0,0,6"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "ticks.csv"
            collector = RawTickShadowCollector(
                bridge=FakeBridge([page]), symbol="XAUUSD", csv_path=csv_path,
                state_path=root / "state.json", recipe_version="test_v1",
                page_rows=1, run_id="run1",
            )
            collector.collect_until(Cursor(1000, 0, 0), 2000, max_pages=1)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.reader(handle))
            rows[1][17] = "2"
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerows(rows)
            with self.assertRaisesRegex(ValueError, "sequence/skip 1"):
                load_cursor_from_csv(csv_path)

    def test_cursor_rejects_out_of_domain_last_and_flags(self):
        page = "OK|META,1,0,1000,1|1000,1.0,1.2,0,0,6"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "ticks.csv"
            collector = RawTickShadowCollector(
                bridge=FakeBridge([page]), symbol="XAUUSD", csv_path=csv_path,
                state_path=root / "state.json", recipe_version="test_v1",
                page_rows=1, run_id="run1",
            )
            collector.collect_until(Cursor(1000, 0, 0), 2000, max_pages=1)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                original = list(csv.reader(handle))
            for column, value in ((13, "-1"), (15, "4294967296")):
                with self.subTest(column=column, value=value):
                    rows = [list(row) for row in original]
                    rows[1][column] = value
                    with csv_path.open("w", encoding="utf-8", newline="") as handle:
                        csv.writer(handle).writerows(rows)
                    with self.assertRaisesRegex(ValueError, "evidence"):
                        load_cursor_from_csv(csv_path)

    def test_cursor_rejects_broken_receipt_time_causality(self):
        page = "OK|META,1,0,1000,1|1000,1.0,1.2,0,0,6"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "ticks.csv"
            collector = RawTickShadowCollector(
                bridge=FakeBridge([page]), symbol="XAUUSD", csv_path=csv_path,
                state_path=root / "state.json", recipe_version="test_v1",
                page_rows=1, run_id="run1",
            )
            collector.collect_until(Cursor(1000, 0, 0), 2000, max_pages=1)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.reader(handle))
            rows[1][6] = "1970-01-01T00:00:00.500+00:00"
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerows(rows)
            with self.assertRaisesRegex(ValueError, "evidence"):
                load_cursor_from_csv(csv_path)

    def test_cursor_rejects_available_time_reversal_inside_batch(self):
        page = "OK|META,2,0,1001,1|1000,1.0,1.2,0,0,6|1001,1.1,1.3,0,0,6"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "ticks.csv"
            collector = RawTickShadowCollector(
                bridge=FakeBridge([page]), symbol="XAUUSD", csv_path=csv_path,
                state_path=root / "state.json", recipe_version="test_v1",
                page_rows=2, run_id="run1",
            )
            collector.collect_until(Cursor(1000, 0, 0), 2000, max_pages=1)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["available_time"] = "2099-01-02T00:00:00+00:00"
            rows[0]["recorded_at"] = rows[0]["available_time"]
            rows[1]["available_time"] = "2099-01-01T00:00:00+00:00"
            rows[1]["recorded_at"] = rows[1]["available_time"]
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(ValueError, "available time reversed"):
                load_cursor_from_csv(csv_path)

    def test_state_evidence_detects_missing_truncated_or_conflicting_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps({
                "schema_version": "s23_raw_tick_collector_state_v1",
                "recipe_version": "test_v1",
                "run_id": "run1",
                "from_msc": 1001,
                "skip_at_from_msc": 1,
                "source_sequence": 2,
                "last_available_time": "2026-08-31T00:00:00+00:00",
            }), encoding="utf-8")
            state_cursor = load_state_cursor(
                state_path, expected_recipe_version="test_v1",
            )
            self.assertEqual(state_cursor, Cursor(1001, 1, 2))
            with self.assertRaisesRegex(RuntimeError, "missing or empty"):
                reconcile_recovery_cursors(None, state_cursor)
            with self.assertRaisesRegex(RuntimeError, "behind persisted state"):
                reconcile_recovery_cursors(Cursor(1000, 1, 1), state_cursor)
            with self.assertRaisesRegex(RuntimeError, "conflicts"):
                reconcile_recovery_cursors(Cursor(999, 1, 2), state_cursor)
            reconcile_recovery_cursors(Cursor(1002, 1, 3), state_cursor)

    def test_state_evidence_rejects_corrupt_schema_and_non_utc_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            valid = {
                "schema_version": "s23_raw_tick_collector_state_v1",
                "recipe_version": "test_v1",
                "run_id": "run1",
                "from_msc": 1000,
                "skip_at_from_msc": 1,
                "source_sequence": 1,
                "last_available_time": "2026-08-31T00:00:00+00:00",
            }
            for payload in (
                {**valid, "unexpected": True},
                {**valid, "source_sequence": True},
                {**valid, "last_available_time": "2026-08-31T09:00:00+09:00"},
            ):
                with self.subTest(payload=payload):
                    state_path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_state_cursor(state_path, expected_recipe_version="test_v1")

    def test_equal_sequence_requires_exact_state_tail_identity(self):
        page = "OK|META,1,0,1000,1|1000,1.0,1.2,0,0,6"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path, state_path = root / "ticks.csv", root / "state.json"
            collector = RawTickShadowCollector(
                bridge=FakeBridge([page]), symbol="XAUUSD", csv_path=csv_path,
                state_path=state_path, recipe_version="test_v1",
                page_rows=1, run_id="run1",
            )
            collector.collect_until(Cursor(1000, 0, 0), 2000, max_pages=1)
            csv_evidence = load_csv_recovery_evidence(
                csv_path, expected_recipe_version="test_v1",
            )
            state_evidence = load_state_recovery_evidence(
                state_path, expected_recipe_version="test_v1",
            )
            reconcile_recovery_evidence(csv_evidence, state_evidence)
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            for key, value in (
                ("run_id", "other-run"),
                ("last_available_time", "2099-01-01T00:00:00+00:00"),
            ):
                with self.subTest(key=key):
                    changed = {**payload, key: value}
                    state_path.write_text(json.dumps(changed), encoding="utf-8")
                    conflicting = load_state_recovery_evidence(
                        state_path, expected_recipe_version="test_v1",
                    )
                    with self.assertRaisesRegex(RuntimeError, "tail identity conflicts"):
                        reconcile_recovery_evidence(csv_evidence, conflicting)

    def test_stale_state_must_match_its_exact_csv_checkpoint(self):
        page = "OK|META,2,0,1001,1|1000,1.0,1.2,0,0,6|1001,1.1,1.3,0,0,6"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path, state_path = root / "ticks.csv", root / "state.json"
            collector = RawTickShadowCollector(
                bridge=FakeBridge([page]), symbol="XAUUSD", csv_path=csv_path,
                state_path=state_path, recipe_version="test_v1",
                page_rows=2, run_id="run1",
            )
            collector.collect_until(Cursor(1000, 0, 0), 2000, max_pages=1)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                first = next(csv.DictReader(handle))
            valid = raw_module.RecoveryEvidence(
                Cursor(1000, 1, 1),
                "run1",
                datetime.fromisoformat(first["available_time"]),
            )
            evidence = load_csv_recovery_evidence(
                csv_path, expected_recipe_version="test_v1", checkpoint=valid,
            )
            reconcile_recovery_evidence(evidence, valid)
            conflicts = (
                raw_module.RecoveryEvidence(Cursor(999, 1, 1), valid.run_id, valid.last_available_time),
                raw_module.RecoveryEvidence(valid.cursor, "other-run", valid.last_available_time),
                raw_module.RecoveryEvidence(valid.cursor, valid.run_id, datetime(2099, 1, 1, tzinfo=timezone.utc)),
            )
            for checkpoint in conflicts:
                with self.subTest(checkpoint=checkpoint):
                    with self.assertRaisesRegex(RuntimeError, "checkpoint conflicts"):
                        load_csv_recovery_evidence(
                            csv_path,
                            expected_recipe_version="test_v1",
                            checkpoint=checkpoint,
                        )

    def test_available_time_cannot_reverse_across_batches_or_clock_rollback(self):
        page1 = "OK|META,1,0,1000,1|1000,1.0,1.2,0,0,6"
        page2 = "OK|META,1,0,1001,1|1001,1.1,1.3,0,0,6"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "ticks.csv"
            collector = RawTickShadowCollector(
                bridge=FakeBridge([page1, page2]), symbol="XAUUSD",
                csv_path=csv_path, state_path=root / "state.json",
                recipe_version="test_v1", page_rows=1, run_id="run1",
            )
            first_time = datetime(2026, 8, 31, tzinfo=timezone.utc)
            rolled_back = datetime(1960, 1, 1, tzinfo=timezone.utc)
            with mock.patch.object(
                raw_module, "_utc_now",
                side_effect=[first_time, first_time, rolled_back, rolled_back],
            ):
                cursor, _ = collector.collect_until(Cursor(1000, 0, 0), 2000, max_pages=1)
                collector.collect_until(cursor, 2000, max_pages=1)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertLessEqual(rows[0]["available_time"], rows[1]["available_time"])
            rows[0]["available_time"] = "2099-01-02T00:00:00+00:00"
            rows[0]["recorded_at"] = rows[0]["available_time"]
            rows[1]["ingested_time"] = "2099-01-01T00:00:00+00:00"
            rows[1]["available_time"] = rows[1]["ingested_time"]
            rows[1]["recorded_at"] = rows[1]["available_time"]
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(ValueError, "available time reversed"):
                load_cursor_from_csv(csv_path)

    def test_clock_rollback_cannot_create_noncausal_receipt_times(self):
        page = "OK|META,1,0,1000,1|1000,1.0,1.2,0,0,6"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "ticks.csv"
            collector = RawTickShadowCollector(
                bridge=FakeBridge([page]), symbol="XAUUSD", csv_path=csv_path,
                state_path=root / "state.json", recipe_version="test_v1",
                page_rows=1, run_id="run1",
            )
            rolled_back = datetime(1960, 1, 1, tzinfo=timezone.utc)
            with mock.patch.object(raw_module, "_utc_now", side_effect=[rolled_back, rolled_back]):
                collector.collect_until(Cursor(1000, 0, 0), 2000, max_pages=1)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                row = next(csv.DictReader(handle))
            cutoff = datetime.fromisoformat(row["cutoff_time"])
            self.assertEqual(datetime.fromisoformat(row["ingested_time"]), cutoff)
            self.assertEqual(datetime.fromisoformat(row["available_time"]), cutoff)
            self.assertEqual(load_cursor_from_csv(csv_path), Cursor(1000, 1, 1))

    def test_batch_identity_is_unique_and_continuous_within_run(self):
        page1 = "OK|META,1,0,1000,1|1000,1.0,1.2,0,0,6"
        page2 = "OK|META,1,0,1001,1|1001,1.1,1.3,0,0,6"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "ticks.csv"
            collector = RawTickShadowCollector(
                bridge=FakeBridge([page1, page2]), symbol="XAUUSD",
                csv_path=csv_path, state_path=root / "state.json",
                recipe_version="test_v1", page_rows=1, run_id="run1",
            )
            cursor, _count = collector.collect_until(Cursor(1000, 0, 0), 2000, max_pages=1)
            cursor, _count = collector.collect_until(cursor, 2000, max_pages=1)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["batch_id"] for row in rows], ["run1:1", "run1:2"])
            self.assertEqual(load_cursor_from_csv(csv_path), cursor)
            rows[1]["batch_id"] = "run1:1"
            rows[1]["ingested_time"] = "2099-01-01T00:00:00+00:00"
            rows[1]["available_time"] = "2099-01-01T00:00:00+00:00"
            rows[1]["recorded_at"] = "2099-01-01T00:00:00+00:00"
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(ValueError, "batch evidence mismatch"):
                load_cursor_from_csv(csv_path)

    def test_csv_identity_selects_the_single_writer_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "same.csv"
            self.assertEqual(
                collector_lock_path(csv_path),
                collector_lock_path(root / "same.csv"),
            )
            self.assertNotEqual(
                collector_lock_path(csv_path),
                collector_lock_path(root / "other.csv"),
            )
            self.assertEqual(collector_lock_path(csv_path).name, "same.csv.lock")

    def test_append_fails_closed_if_existing_csv_is_modified(self):
        first_page = "OK|META,1,0,1000,1|1000,1.0,1.2,0,0,6"
        second_page = "OK|META,1,0,1001,1|1001,1.1,1.3,0,0,6"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "ticks.csv"
            collector = RawTickShadowCollector(
                bridge=FakeBridge([first_page, second_page]), symbol="XAUUSD",
                csv_path=csv_path, state_path=root / "state.json",
                recipe_version="test_v1", page_rows=1, run_id="run1",
            )
            cursor, _count = collector.collect_until(Cursor(1000, 0, 0), 2000, max_pages=1)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.reader(handle))
            rows[1][10] = "999"
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerows(rows)
            with self.assertRaisesRegex(RuntimeError, "changed before append"):
                collector.collect_until(cursor, 2000, max_pages=1)

    def test_running_collector_does_not_rescan_full_history_each_page(self):
        seed_page = "OK|META,1,0,1000,1|1000,1.0,1.2,0,0,6"
        page1 = "OK|META,1,1,1000,1|1000,1.1,1.3,0,0,6"
        page2 = "OK|META,1,0,1001,1|1001,1.2,1.4,0,0,6"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path, state_path = root / "ticks.csv", root / "state.json"
            seed = RawTickShadowCollector(
                bridge=FakeBridge([seed_page]), symbol="XAUUSD", csv_path=csv_path,
                state_path=state_path, recipe_version="test_v1", page_rows=1,
                run_id="seed",
            )
            cursor, _count = seed.collect_until(Cursor(1000, 0, 0), 2000, max_pages=1)
            collector = RawTickShadowCollector(
                bridge=FakeBridge([page1, page2]), symbol="XAUUSD", csv_path=csv_path,
                state_path=state_path, recipe_version="test_v1", page_rows=1,
                run_id="run2",
            )
            with mock.patch.object(
                raw_module,
                "_load_cursor_from_handle",
                wraps=raw_module._load_cursor_from_handle,
            ) as validator:
                cursor, count = collector.collect_until(cursor, 2000, max_pages=2)
            self.assertEqual(count, 2)
            self.assertEqual(cursor.source_sequence, 3)
            self.assertEqual(validator.call_count, 1)

    def test_collector_lock_allows_only_one_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "collector.lock"
            first = acquire_collector_lock(lock_path)
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(acquire_collector_lock(lock_path))
            finally:
                release_collector_lock(first)
            second = acquire_collector_lock(lock_path)
            self.assertIsNotNone(second)
            release_collector_lock(second)

    def test_module_uses_read_only_command_only(self):
        source = Path(__file__).with_name("raw_tick_shadow_collector.py").read_text(encoding="utf-8")
        for forbidden in ("OPEN|", "CLOSE|", "MODIFY|", "CANCEL|", "PENDING|"):
            self.assertNotIn(forbidden, source)

    def test_bridge_exposes_bounded_read_only_tick_command(self):
        bridge_source = Path(__file__).with_name("BotBridge_s23.mq5").read_text(encoding="utf-8")
        self.assertIn('if(op == "TICKS" && n == 6)', bridge_source)
        self.assertIn("ParseCanonicalUnsignedLong(parts[2], raw_from_msc)", bridge_source)
        self.assertIn("ParseCanonicalUnsignedLong(parts[5], raw_skip_at_from)", bridge_source)
        self.assertIn("CopyTicks(symbol, ticks, COPY_TICKS_INFO", bridge_source)
        self.assertIn("max_rows > 2000", bridge_source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
