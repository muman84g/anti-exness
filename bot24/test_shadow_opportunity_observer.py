"""No-order tests for the passive bot24 shadow opportunity observer."""

from __future__ import annotations

import ast
import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shadow_opportunity_observer import ShadowOpportunityObserver


UTC = timezone.utc


def config(*, horizons: list[int] | None = None) -> dict:
    return {
        "enabled": True,
        "horizons_minutes": horizons or [1, 5, 15, 30, 60],
        "completed_id_retention_days": 14,
        "opportunity_csv": "opportunities.csv",
        "markout_csv": "markouts.csv",
        "state_file": "observer_state.json",
    }


def opportunity(*, side: str = "LONG", effective_side: str | None = None, suffix: str = "1") -> dict:
    effective = side if effective_side is None else effective_side
    return {
        "opportunity_id": f"op-{suffix}",
        "side": effective or side,
        "raw_side": side,
        "effective_side": effective,
        "entry_policy": {"policy_id": "s24_runner_shadow_v1", "action": "shadow_route", "reason": "test"},
        "event_time": "2026-08-26T13:00:00+00:00",
        "release_time": "2026-08-26T13:01:00+00:00",
        "decision_time": "2026-08-26T13:01:02+00:00",
    }


class ShadowOpportunityObserverTests(unittest.TestCase):
    def make(self, root: str, *, horizons: list[int] | None = None) -> ShadowOpportunityObserver:
        return ShadowOpportunityObserver(
            config(horizons=horizons),
            log_dir=Path(root) / "logs",
            state_dir=Path(root) / "state",
            symbol="XAUUSD",
            contract_size=100.0,
            lot=0.01,
        )

    def test_artifact_paths_cannot_escape_the_configured_directories(self):
        with tempfile.TemporaryDirectory() as root:
            for key, value in (
                ("opportunity_csv", "../outside.csv"),
                ("markout_csv", str(Path(root) / "outside.csv")),
                ("state_file", "state.json:stream"),
            ):
                cfg = config()
                cfg[key] = value
                with self.subTest(key=key), self.assertRaises(ValueError):
                    ShadowOpportunityObserver(
                        cfg,
                        log_dir=Path(root) / "logs",
                        state_dir=Path(root) / "state",
                        symbol="XAUUSD",
                        contract_size=100.0,
                        lot=0.01,
                    )

    def test_numeric_configuration_must_be_strict_positive_and_finite(self):
        with tempfile.TemporaryDirectory() as root:
            base = dict(
                log_dir=Path(root) / "logs",
                state_dir=Path(root) / "state",
                symbol="XAUUSD",
                contract_size=100.0,
                lot=0.01,
            )
            invalid_cases = (
                ({"horizons_minutes": [True]}, {}),
                ({"horizons_minutes": [1.5]}, {}),
                ({"horizons_minutes": "1"}, {}),
                ({"completed_id_retention_days": True}, {}),
                ({"completed_id_retention_days": 1.5}, {}),
                ({}, {"contract_size": float("nan")}),
                ({}, {"lot": 0.0}),
            )
            for config_update, argument_update in invalid_cases:
                cfg = config()
                cfg.update(config_update)
                arguments = dict(base)
                arguments.update(argument_update)
                with self.subTest(config_update=config_update, argument_update=argument_update), self.assertRaises(ValueError):
                    ShadowOpportunityObserver(cfg, **arguments)

    def test_malformed_state_is_rejected_before_evidence_reconciliation(self):
        with tempfile.TemporaryDirectory() as root:
            observer = self.make(root, horizons=[1])
            start = datetime(2026, 8, 26, 13, 1, 2, tzinfo=UTC)
            observer.register_opportunity(opportunity(), at=start, bid=100.0, ask=100.2)
            state = json.loads(observer.state_path.read_text(encoding="utf-8"))
            state["completed"]["bad"] = "not-a-time"
            observer.state_path.write_text(json.dumps(state), encoding="utf-8")
            observer.opportunity_path.unlink()

            with self.assertRaises(RuntimeError):
                self.make(root, horizons=[1])
            self.assertFalse(observer.opportunity_path.exists())
            self.assertFalse(observer.markout_path.exists())

    def test_boolean_numeric_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            observer = self.make(root, horizons=[1])
            start = datetime(2026, 8, 26, 13, 1, 2, tzinfo=UTC)
            observer.register_opportunity(opportunity(), at=start, bid=100.0, ask=100.2)
            state = json.loads(observer.state_path.read_text(encoding="utf-8"))
            state["pending"]["op-1"]["entry_bid"] = True
            observer.state_path.write_text(json.dumps(state), encoding="utf-8")

            with self.assertRaises(RuntimeError):
                self.make(root, horizons=[1])

    def test_malformed_existing_csv_rows_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            log_dir = Path(root) / "logs"
            log_dir.mkdir()
            path = log_dir / "opportunities.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(__import__("shadow_opportunity_observer").OPPORTUNITY_FIELDS)
                writer.writerow([""] * len(__import__("shadow_opportunity_observer").OPPORTUNITY_FIELDS) + ["extra"])
            with self.assertRaises(RuntimeError):
                self.make(root)

    def test_runtime_csv_corruption_is_rejected_before_next_append(self):
        with tempfile.TemporaryDirectory() as root:
            observer = self.make(root)
            start = datetime(2026, 8, 26, 13, 1, 2, tzinfo=UTC)
            observer.register_opportunity(opportunity(), at=start, bid=100.0, ask=100.2)
            with observer.opportunity_path.open("a", newline="", encoding="utf-8") as handle:
                csv.writer(handle).writerow([""] * (len(__import__("shadow_opportunity_observer").OPPORTUNITY_FIELDS) + 1))
            before = observer.opportunity_path.read_bytes()

            with self.assertRaises(RuntimeError):
                observer.register_opportunity(opportunity(suffix="2"), at=start, bid=100.0, ask=100.2)
            self.assertEqual(observer.opportunity_path.read_bytes(), before)

    def test_long_markout_uses_entry_ask_future_bid_and_tracks_executable_extrema(self):
        with tempfile.TemporaryDirectory() as root:
            observer = self.make(root, horizons=[1])
            start = datetime(2026, 8, 26, 13, 1, 2, tzinfo=UTC)
            self.assertTrue(observer.register_opportunity(opportunity(), at=start, bid=100.0, ask=100.2, context={"atr30": 2.0}))
            self.assertTrue(observer.record_route("op-1", at=start, status="consumed", consumed_lane_id=2, reason="entry_attempted"))
            self.assertEqual(observer.observe_quote(at=start + timedelta(seconds=30), bid=101.0, ask=101.2), 0)
            self.assertEqual(observer.observe_quote(at=start + timedelta(seconds=61), bid=99.5, ask=99.7), 1)
            with observer.markout_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertAlmostEqual(float(row["pnl_price"]), -0.7)
            self.assertAlmostEqual(float(row["mfe_price"]), 0.8)
            self.assertAlmostEqual(float(row["mae_price"]), -0.7)
            self.assertAlmostEqual(float(row["pnl_usd"]), -0.7)
            self.assertEqual(row["route_status"], "consumed")
            self.assertEqual(row["consumed_lane_id"], "2")

    def test_short_markout_uses_entry_bid_future_ask(self):
        with tempfile.TemporaryDirectory() as root:
            observer = self.make(root, horizons=[1])
            start = datetime(2026, 8, 26, 13, 1, 2, tzinfo=UTC)
            observer.register_opportunity(opportunity(side="SHORT", suffix="short"), at=start, bid=100.0, ask=100.2)
            observer.observe_quote(at=start + timedelta(seconds=61), bid=98.8, ask=99.0)
            with observer.markout_path.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
            self.assertAlmostEqual(float(row["pnl_price"]), 1.0)
            self.assertAlmostEqual(float(row["mfe_price"]), 1.0)

    def test_policy_blocked_opportunity_uses_raw_side_as_explicit_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            observer = self.make(root, horizons=[1])
            start = datetime(2026, 8, 26, 13, 1, 2, tzinfo=UTC)
            observer.register_opportunity(
                opportunity(side="SHORT", effective_side="", suffix="blocked"),
                at=start,
                bid=100.0,
                ask=100.2,
            )
            item = observer.state["pending"]["op-blocked"]
            self.assertEqual(item["markout_side"], "SHORT")
            self.assertEqual(item["markout_basis"], "raw_fallback_policy_blocked")

    def test_restart_preserves_pending_and_deduplicates_registration_route_and_markout(self):
        with tempfile.TemporaryDirectory() as root:
            start = datetime(2026, 8, 26, 13, 1, 2, tzinfo=UTC)
            first = self.make(root, horizons=[1])
            first.register_opportunity(opportunity(), at=start, bid=100.0, ask=100.2)
            first.record_route("op-1", at=start, status="unconsumed", reason="all_lanes_noop")
            second = self.make(root, horizons=[1])
            self.assertFalse(second.register_opportunity(opportunity(), at=start, bid=100.0, ask=100.2))
            self.assertEqual(second.observe_quote(at=start + timedelta(seconds=61), bid=101.0, ask=101.2), 1)
            third = self.make(root, horizons=[1])
            self.assertFalse(third.register_opportunity(opportunity(), at=start, bid=100.0, ask=100.2))
            self.assertEqual(third.observe_quote(at=start + timedelta(seconds=122), bid=102.0, ask=102.2), 0)
            with second.opportunity_path.open(newline="", encoding="utf-8") as handle:
                opportunity_rows = list(csv.DictReader(handle))
            with second.markout_path.open(newline="", encoding="utf-8") as handle:
                markout_rows = list(csv.DictReader(handle))
            self.assertEqual([row["event"] for row in opportunity_rows], ["registered", "route_update"])
            self.assertEqual(len(markout_rows), 1)
            state = json.loads(second.state_path.read_text(encoding="utf-8"))
            self.assertNotIn("op-1", state["pending"])
            self.assertIn("op-1", state["completed"])

    def test_same_or_older_quote_timestamp_does_not_advance_extrema_or_samples(self):
        with tempfile.TemporaryDirectory() as root:
            observer = self.make(root, horizons=[1])
            start = datetime(2026, 8, 26, 13, 1, 2, tzinfo=UTC)
            observer.register_opportunity(opportunity(), at=start, bid=100.0, ask=100.2)
            observer.observe_quote(at=start, bid=105.0, ask=105.2)
            item = observer.state["pending"]["op-1"]
            self.assertEqual(item["quote_samples"], 1)
            self.assertAlmostEqual(float(item["mfe_price"]), -0.2)

    def test_module_has_no_order_or_bridge_dependency(self):
        path = Path(__file__).with_name("shadow_opportunity_observer.py")
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
