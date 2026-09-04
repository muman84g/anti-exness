"""Shared-account and restart safety regressions ported from bot23."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd

import live_s24_bot as s24
from live_safety import clean_sync_block_if_flat
from v206_execution import R1CloseResult
from v206_live_lane import default_v206_state


def params_copy() -> dict:
    params = json.loads(json.dumps(s24.load_params()))
    params["_suppress_manual_alerts"] = True
    params["runner_shadow"]["enabled"] = False
    params["runner_shadow"]["opportunity_observer"]["enabled"] = False
    params["runner_shadow"]["state_tagger"]["enabled"] = False
    return params


class RecordingExecutor(s24.FakeExecutor):
    def __init__(self, *, positions=None, orders=None):
        super().__init__(positions=positions, orders=orders)
        self.open_calls = 0
        self.close_calls = 0

    def open_position(self, *args, **kwargs):
        self.open_calls += 1
        return super().open_position(*args, **kwargs)

    def close_position(self, *args, **kwargs):
        self.close_calls += 1
        return super().close_position(*args, **kwargs)


def live_position(*, volume: float = 0.01) -> SimpleNamespace:
    return SimpleNamespace(
        ticket=7001,
        identifier=7001,
        symbol="XAUUSD",
        magic=s24.EXPECTED_S24_MAGIC,
        comment="s24_no_adverse",
        type=s24.ORDER_TYPE_BUY,
        volume=volume,
        open_price=2064.0,
        open_time=1767272400,
    )


def persisted_position() -> dict:
    return {
        "ticket": 7001,
        "position_identifier": 7001,
        "side": "LONG",
        "lot": 0.01,
        "entry_price": 2064.0,
        "entry_time_utc": "2026-01-01T13:00:00+00:00",
        "open_time_epoch": 1767272400,
        "owner_symbol": "XAUUSD",
        "owner_magic": s24.EXPECTED_S24_MAGIC,
        "owner_comment": "s24_no_adverse",
        "signal_bar_time": "2026-01-01T12:59:00+00:00",
        "shadow": False,
    }


class S24SafetyRegressionTests(unittest.TestCase):
    def setUp(self):
        self._state_directory = tempfile.TemporaryDirectory(prefix="s24-test-state-")
        self._original_state_file = s24.STATE_FILE
        s24.STATE_FILE = str(Path(self._state_directory.name) / "s24_bot_state.json")

    def tearDown(self):
        s24.STATE_FILE = self._original_state_file
        self._state_directory.cleanup()

    def test_self_test_never_reads_or_writes_configured_live_state_file(self):
        params = params_copy()
        seed_runner = object.__new__(s24.S24NoAdverseRunner)
        seed_runner.params = params
        seeded = seed_runner._default_state()
        seeded["strategies"][params["strategies"][0]["id"]].pop("entry_permission_reject_count")
        with tempfile.TemporaryDirectory() as root:
            sentinel = Path(root) / "configured-live-state.json"
            sentinel.write_text(json.dumps(seeded, sort_keys=True), encoding="utf-8")
            before = sentinel.read_bytes()
            original_state_file = s24.STATE_FILE
            s24.STATE_FILE = str(sentinel)
            try:
                s24.self_test()
            finally:
                s24.STATE_FILE = original_state_file

            self.assertEqual(sentinel.read_bytes(), before)

    def test_runner_singleton_lock_is_exclusive_and_os_released(self):
        with tempfile.TemporaryDirectory() as root:
            lock_path = Path(root) / "s24_runner.lock"
            first = s24.acquire_runner_singleton_lock(str(lock_path))
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(s24.acquire_runner_singleton_lock(str(lock_path)))
            finally:
                first.close()
            second = s24.acquire_runner_singleton_lock(str(lock_path))
            self.assertIsNotNone(second)
            second.close()

    def test_main_acquires_runner_lock_before_runtime_construction(self):
        source = Path(s24.__file__).read_text(encoding="utf-8")
        main_block = source.split("def main()", 1)[1]
        self.assertLess(
            main_block.index("acquire_runner_singleton_lock()"),
            main_block.index("S24NoAdverseRunner(params)"),
        )

    def test_core_close_identity_is_complete_and_blocks_known_same_side_signal(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        state = runner._st(strategy)
        state["basket"] = [persisted_position()]
        state["last_evaluated_bar"] = "2026-01-01T13:01:00+00:00"
        runner._save_state = lambda: None
        rows = []
        runner._trade_row = lambda event, *_args, **kwargs: rows.append((event, kwargs))

        runner._clear_basket_state(
            strategy,
            "basket_target",
            "2026-01-01T13:01:00+00:00",
            close_side="LONG",
            close_time="2026-01-01T13:02:00+00:00",
        )

        self.assertEqual(state["last_closed_side"], "LONG")
        self.assertEqual(state["last_closed_at_utc"], "2026-01-01T13:02:00+00:00")
        self.assertEqual(state["last_consumed_signal_bar"], "2026-01-01T13:01:00+00:00")
        self.assertEqual(state["last_closed_entry_signal_bars"], ["2026-01-01T12:59:00+00:00"])
        row = pd.Series(
            {"Open": 2064.0, "Close": 2064.0, "AskOpen": 2064.1},
            name=pd.Timestamp("2026-01-01T13:01:00Z"),
        )
        runner._open_entry(strategy, "LONG", row, SimpleNamespace(bid=2064.0, ask=2064.1))

        self.assertEqual(state["basket"], [])
        self.assertTrue(any(event == "entry_skip" and values.get("reason") == "known_same_direction_signal_after_close" for event, values in rows))

    def test_core_close_identity_partial_state_fails_closed(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        state = runner._default_state()["strategies"][params["strategies"][0]["id"]]
        state["last_closed_at_utc"] = "2026-01-01T13:02:00+00:00"
        state["last_closed_reason"] = "basket_target"
        state["last_closed_signal_bar"] = "2026-01-01T13:01:00+00:00"

        self.assertEqual(
            runner._core_state_shape_error(params["strategies"][0], state),
            "last_closed_identity_invalid",
        )

    def test_core_close_identity_does_not_block_known_opposite_side_signal(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.live_enabled = False
        runner.shadow_enabled = True
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        state = runner._st(strategy)
        state["last_closed_at_utc"] = "2026-01-01T13:02:00+00:00"
        state["last_closed_side"] = "LONG"
        state["last_closed_reason"] = "basket_stop"
        state["last_closed_signal_bar"] = "2026-01-01T13:01:00+00:00"
        state["last_consumed_signal_bar"] = "2026-01-01T13:01:00+00:00"
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        row = pd.Series(
            {"Open": 2064.0, "Close": 2064.0, "AskOpen": 2064.1},
            name=pd.Timestamp("2026-01-01T13:01:00Z"),
        )

        runner._open_entry(strategy, "SHORT", row, SimpleNamespace(bid=2064.0, ask=2064.1), note="reverse_after_stop")

        self.assertEqual(len(state["basket"]), 1)
        self.assertEqual(state["basket"][0]["side"], "SHORT")

    def test_v206_close_identity_blocks_known_same_side_pending_signal(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.live_enabled = False
        runner.state = runner._default_state()
        runner.state["v206"] = default_v206_state()
        lane = runner.v206_lane
        state = lane.state
        state["migration_pending"] = False
        state["blocked_reason"] = None
        state["last_closed_at_utc"] = "2026-01-01T13:02:00+00:00"
        state["last_closed_side"] = "LONG"
        state["last_closed_reason"] = "timeout_30m"
        state["last_consumed_signal_bar"] = "2026-01-01T13:01:00+00:00"
        state["pending_signal"] = {
            "opportunity_id": "v206:2026-01-01T13:01:00+00:00:LONG",
            "side": "LONG",
            "signal_bar_time": "2026-01-01T13:01:00+00:00",
            "entry_due_utc": "2026-01-01T13:02:00+00:00",
            "entry_expiry_utc": "2026-01-01T13:04:00+00:00",
            "fixed_stop": 2059.0,
        }
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None

        lane._attempt_pending_signal(SimpleNamespace(bid=2064.0, ask=2064.1), pd.Timestamp("2026-01-01T13:03:00Z"))

        self.assertIsNone(state["pending_signal"])
        self.assertEqual(state["last_decision"]["outcome"], "known_same_direction_signal_after_close")

    def test_v206_partial_close_identity_fails_closed(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        raw = default_v206_state()
        raw["last_closed_at_utc"] = "2026-01-01T13:02:00+00:00"
        raw["last_closed_reason"] = "timeout_30m"

        self.assertEqual(runner.v206_lane._state_shape_error(raw), "last_closed_identity_invalid")

    def test_v206_state_rejects_pending_close_without_basket(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        raw = default_v206_state()
        raw["pending_close"] = {
            "ticket": 8206, "position_identifier": 9206, "lot": 0.01,
            "started_utc": "2026-01-01T13:30:00+00:00", "reason": "timeout_30m",
        }

        self.assertEqual(runner.v206_lane._state_shape_error(raw), "pending_close_without_basket")

    def test_v206_state_rejects_pending_close_basket_identity_mismatch(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        raw = default_v206_state()
        raw["basket"] = [{
            "ticket": 8206, "position_identifier": 9206, "side": "LONG", "lot": 0.01,
            "entry_price": 2064.0, "fixed_stop": 2059.0, "open_time_epoch": 1767272400,
            "entry_time_utc": "2026-01-01T13:00:00+00:00",
            "signal_bar_time": "2026-01-01T12:59:00+00:00",
            "timeout_at_utc": "2026-01-01T13:30:00+00:00",
            "owner_symbol": "XAUUSD", "owner_magic": 240206, "owner_comment": "s24_v206",
        }]
        raw["pending_close"] = {
            "ticket": 8207, "position_identifier": 9207, "lot": 0.01,
            "started_utc": "2026-01-01T13:30:00+00:00", "reason": "timeout_30m",
        }

        self.assertEqual(runner.v206_lane._state_shape_error(raw), "pending_close_basket_identity_mismatch")

    def test_v206_state_rejects_malformed_confirmed_close_receipt(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        basket = {
            "ticket": 8206, "position_identifier": 9206, "side": "LONG", "lot": 0.01,
            "entry_price": 2064.0, "fixed_stop": 2059.0, "open_time_epoch": 1767272400,
            "entry_time_utc": "2026-01-01T13:00:00+00:00",
            "signal_bar_time": "2026-01-01T12:59:00+00:00",
            "timeout_at_utc": "2026-01-01T13:30:00+00:00",
            "owner_symbol": "XAUUSD", "owner_magic": 240206, "owner_comment": "s24_v206",
        }
        base_close = {
            "ticket": 8206, "position_identifier": 9206, "lot": 0.01,
            "started_utc": "2026-01-01T13:30:00+00:00", "reason": "timeout_30m",
        }
        cases = (
            {"confirmed_response": "true", "deal": 12001},
            {"confirmed_response": True},
            {"deal": 12001},
            {"confirmed_response": True, "deal": 0},
        )
        for receipt in cases:
            with self.subTest(receipt=receipt):
                raw = default_v206_state()
                raw["basket"] = [dict(basket)]
                raw["pending_close"] = dict(base_close, **receipt)
                self.assertEqual(runner.v206_lane._state_shape_error(raw), "pending_close_receipt_invalid")

    def test_v206_state_rejects_overlapping_open_lifecycle_containers(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        raw = default_v206_state()
        raw["basket"] = [{
            "ticket": 8206, "position_identifier": 9206, "side": "LONG", "lot": 0.01,
            "entry_price": 2064.0, "fixed_stop": 2059.0, "open_time_epoch": 1767272400,
            "entry_time_utc": "2026-01-01T13:00:00+00:00",
            "signal_bar_time": "2026-01-01T12:59:00+00:00",
            "timeout_at_utc": "2026-01-01T13:30:00+00:00",
            "owner_symbol": "XAUUSD", "owner_magic": 240206, "owner_comment": "s24_v206",
        }]
        raw["pending_open"] = {
            "opportunity_id": "v206:2026-01-01T12:59:00+00:00:LONG",
            "side": "LONG", "signal_bar_time": "2026-01-01T12:59:00+00:00",
            "entry_due_utc": "2026-01-01T13:00:00+00:00",
            "entry_expiry_utc": "2026-01-01T13:02:00+00:00",
            "fixed_stop": 2059.0, "started_utc": "2026-01-01T13:00:00+00:00",
            "flat_confirmations": 0, "lot": 0.01, "owner_symbol": "XAUUSD",
            "owner_magic": 240206, "owner_comment": "s24_v206",
        }

        self.assertEqual(runner.v206_lane._state_shape_error(raw), "open_lifecycle_container_conflict")

    def test_v206_state_rejects_malformed_pending_open_flat_confirmation_count(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        raw = default_v206_state()
        raw["pending_open"] = {
            "opportunity_id": "v206:2026-01-01T12:59:00+00:00:LONG",
            "side": "LONG", "signal_bar_time": "2026-01-01T12:59:00+00:00",
            "entry_due_utc": "2026-01-01T13:00:00+00:00",
            "entry_expiry_utc": "2026-01-01T13:02:00+00:00",
            "fixed_stop": 2059.0, "started_utc": "2026-01-01T13:00:00+00:00",
            "flat_confirmations": "broken", "lot": 0.01, "owner_symbol": "XAUUSD",
            "owner_magic": 240206, "owner_comment": "s24_v206",
        }

        self.assertEqual(runner.v206_lane._state_shape_error(raw), "pending_open_invalid")

    def test_v206_state_rejects_malformed_root_control_fields(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        cases = (
            ("migration_pending", "false", "migration_pending_invalid"),
            ("blocked_reason", 7, "blocked_reason_invalid"),
            ("manual_alert_last_signature", 7, "manual_alert_last_signature_invalid"),
            ("quarantined_state_snapshot", "broken", "quarantined_state_snapshot_invalid"),
        )
        for key, value, expected in cases:
            with self.subTest(key=key):
                raw = default_v206_state()
                raw[key] = value
                self.assertEqual(runner.v206_lane._state_shape_error(raw), expected)

    def test_core_shadow_open_persists_entry_signal_bar_identity(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.live_enabled = False
        runner.shadow_enabled = True
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        signal_bar = pd.Timestamp("2026-01-01T13:01:00Z")
        row = pd.Series(
            {"Open": 2064.0, "Close": 2064.0, "AskOpen": 2064.1},
            name=signal_bar,
        )

        runner._open_entry(strategy, "LONG", row, SimpleNamespace(bid=2064.0, ask=2064.1))

        self.assertEqual(runner._st(strategy)["basket"][0]["signal_bar_time"], signal_bar.isoformat())

    def test_confirmed_core_open_is_persisted_before_mandatory_trade_csv(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        params["shadow_forward_enabled"] = False
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        signal_bar = pd.Timestamp.now(tz="UTC").floor("min") - pd.Timedelta(minutes=1)
        row = pd.Series(
            {"Open": 2064.0, "Close": 2064.0, "AskOpen": 2064.03},
            name=signal_bar,
        )

        class ConfirmedExecutor(RecordingExecutor):
            def open_position(self, *_args, **kwargs):
                self.open_calls += 1
                opened_at = int((signal_bar + pd.Timedelta(minutes=1)).timestamp())
                position = SimpleNamespace(
                    ticket=9401,
                    identifier=19401,
                    symbol="XAUUSD",
                    magic=s24.EXPECTED_S24_MAGIC,
                    comment=str(kwargs["comment"]),
                    type=s24.ORDER_TYPE_BUY,
                    volume=0.01,
                    open_price=2064.03,
                    open_time=opened_at,
                )
                self.positions = [position]
                self.last_open_identifier = position.identifier
                self.last_open_deal = 29401
                self.last_open_price = position.open_price
                self.last_open_time = position.open_time
                return position.ticket

        runner.executor = ConfirmedExecutor(positions=[], orders=[])
        saved_states = []
        runner._save_state = lambda: saved_states.append(json.loads(json.dumps(runner.state)))

        def fail_entry_csv(event, *_args, **_kwargs):
            if event == "entry":
                raise OSError("simulated mandatory trade CSV failure")

        runner._trade_row = fail_entry_csv
        with self.assertRaisesRegex(OSError, "mandatory trade CSV failure"):
            runner._open_entry(strategy, "LONG", row, runner.executor.get_symbol_info("XAUUSD"))

        self.assertTrue(saved_states)
        persisted = saved_states[-1]["strategies"][strategy["id"]]
        self.assertEqual(len(persisted["basket"]), 1)
        self.assertEqual(persisted["basket"][0]["ticket"], 9401)
        self.assertIsNone(persisted["pending_open_opportunity_id"])

    def test_confirmed_core_close_persists_identity_before_mandatory_trade_csv(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        state = runner._st(strategy)
        state["basket"] = [persisted_position()]
        state["last_evaluated_bar"] = "2026-01-01T13:01:00+00:00"
        state["pending_close_reason"] = "basket_target"
        state["pending_close_signal_bar"] = "2026-01-01T13:01:00+00:00"
        runner.executor = RecordingExecutor(positions=[], orders=[])
        saved_states = []
        runner._save_state = lambda: saved_states.append(json.loads(json.dumps(runner.state)))

        def fail_close_csv(event, *_args, **_kwargs):
            if event == "position_close_deal":
                raise OSError("simulated mandatory trade CSV failure")

        runner._trade_row = fail_close_csv
        with self.assertRaisesRegex(OSError, "mandatory trade CSV failure"):
            runner._sync_strategy(strategy)

        self.assertTrue(saved_states)
        persisted = saved_states[-1]["strategies"][strategy["id"]]
        self.assertEqual(persisted["basket"], [])
        self.assertEqual(persisted["last_closed_side"], "LONG")
        self.assertEqual(
            persisted["last_closed_entry_signal_bars"],
            ["2026-01-01T12:59:00+00:00"],
        )

    def test_core_history_outage_records_one_durable_not_evaluated_receipt(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        runner.executor = RecordingExecutor(positions=[], orders=[])
        runner._save_state = lambda: None
        rows = []
        runner._trade_row = lambda event, *_args, **kwargs: rows.append((event, kwargs))
        quote_time = pd.Timestamp.now(tz="UTC").floor("min")
        info = runner.executor.get_symbol_info()
        info.quote_time_msc = int(quote_time.timestamp() * 1000)

        runner._manage_core_without_history(info)
        runner._manage_core_without_history(info)

        expected_bar = (quote_time - pd.Timedelta(minutes=1)).isoformat()
        self.assertEqual(runner._st(strategy)["last_decision"], {
            "signal_bar_time": expected_bar,
            "outcome": "not_evaluated_data_unavailable",
            "reason": "m1_bars_unavailable",
            "side": None,
        })
        receipts = [row for row in rows if row[0] == "strategy_decision"]
        self.assertEqual(len(receipts), 1)

    def test_v206_history_outage_records_one_durable_not_evaluated_receipt(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner.executor = RecordingExecutor(positions=[], orders=[])
        runner._save_state = lambda: None
        rows = []
        runner._trade_row = lambda event, *_args, **kwargs: rows.append((event, kwargs))
        lane = runner.v206_lane
        lane.state["migration_pending"] = False
        lane.state["migration_flat_confirmations"] = 3
        lane.state["blocked_reason"] = None
        lane._history = lambda: None
        quote_time = pd.Timestamp.now(tz="UTC").floor("min")
        info = SimpleNamespace(
            bid=2000.0,
            ask=2000.2,
            quote_time_msc=int(quote_time.timestamp() * 1000),
        )

        lane.run_once(info)
        lane.run_once(info)

        expected_bar = (quote_time - pd.Timedelta(minutes=1)).isoformat()
        self.assertEqual(lane.state["last_decision"], {
            "signal_bar_time": expected_bar,
            "outcome": "not_evaluated_data_unavailable",
            "reason": "m1_bars_unavailable",
            "side": None,
        })
        receipts = [row for row in rows if row[0] == "v206_strategy_decision"]
        self.assertEqual(len(receipts), 1)

    def test_runtime_missing_quote_clock_reconciles_but_does_not_advance_strategy(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        runner.executor = RecordingExecutor(positions=[], orders=[])
        info = runner.executor.get_symbol_info()
        info.quote_time_msc = None
        runner.executor.get_symbol_info = lambda *_args, **_kwargs: info
        runner._get_m1 = lambda: self.fail("history must not advance on a legacy-shaped INFO response")
        runner._save_state = lambda: None
        v206_reconciliations = []
        runner.v206_lane.reconcile_without_quote = lambda _cause: v206_reconciliations.append(True)

        runner.run_once()

        self.assertEqual(v206_reconciliations, [True])
        state = runner._st(strategy)
        self.assertEqual(state["sync_block_reason"], "runtime_quote_clock_invalid")
        self.assertIsNone(state["last_evaluated_bar"])

    def test_v206_direct_missing_quote_clock_uses_quote_less_reconciliation(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        lane = runner.v206_lane
        observed = []
        lane.reconcile_without_quote = lambda cause="": observed.append(cause)

        lane.run_once(SimpleNamespace(quote_time_msc=None))

        self.assertEqual(observed, ["missing_quote_time"])

    def test_exact_core_sync_restores_broker_entry_price_open_time_and_last_add(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        state = runner._st(strategy)
        persisted = persisted_position()
        persisted["entry_price"] = 1999.0
        persisted["entry_time_utc"] = "2026-01-01T12:55:00+00:00"
        persisted["open_time_epoch"] = 1767272100
        state["basket"] = [persisted]
        state["last_add_price"] = 1999.0
        runner.executor = RecordingExecutor(positions=[live_position()], orders=[])
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None

        self.assertTrue(runner._sync_strategy(strategy))

        restored = state["basket"][0]
        self.assertEqual(restored["entry_price"], 2064.0)
        self.assertEqual(restored["open_time_epoch"], 1767272400)
        self.assertEqual(restored["entry_time_utc"], "2026-01-01T13:00:00+00:00")
        self.assertEqual(state["last_add_price"], 2064.0)

    def test_core_confirmed_external_close_persists_broker_close_identity(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        state = runner._st(strategy)
        state["basket"] = [persisted_position()]
        state["last_evaluated_bar"] = "2026-01-01T13:01:00+00:00"
        runner.executor = RecordingExecutor(positions=[], orders=[])
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None

        self.assertTrue(runner._sync_strategy(strategy))

        self.assertEqual(state["basket"], [])
        self.assertEqual(state["last_closed_at_utc"], "2026-01-01T13:02:00+00:00")
        self.assertEqual(state["last_closed_side"], "LONG")
        self.assertEqual(state["last_closed_reason"], "broker_or_external_close_confirmed")
        self.assertEqual(state["last_consumed_signal_bar"], "2026-01-01T13:01:00+00:00")

    def test_exact_v206_sync_restores_broker_fill_identity_and_timeout(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner.state["v206"] = default_v206_state()
        runner.state["v206"]["migration_pending"] = False
        lane = runner.v206_lane
        opened = 1767272400
        live = SimpleNamespace(
            ticket=8206, identifier=8206, symbol="XAUUSD", magic=240206,
            comment="s24_v206", type=s24.ORDER_TYPE_BUY, volume=0.01,
            open_price=2064.0, open_time=opened, sl=2059.0, tp=2069.0,
        )
        lane.state["basket"] = [{
            "ticket": 8206, "position_identifier": 8206, "side": "LONG", "lot": 0.01,
            "entry_price": 1999.0, "entry_time_utc": "2026-01-01T12:55:00+00:00",
            "open_time_epoch": 1767272100, "owner_symbol": "XAUUSD", "owner_magic": 240206,
            "owner_comment": "s24_v206", "signal_bar_time": "2026-01-01T12:59:00+00:00",
            "timeout_at_utc": "2026-01-01T13:25:00+00:00", "fixed_stop": 2059.0, "target": 2069.0,
        }]
        runner.executor = RecordingExecutor(positions=[live], orders=[])
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None

        self.assertTrue(lane._sync(pd.Timestamp("2026-01-01T13:10:00Z"), SimpleNamespace(bid=2064.0, ask=2064.1)))

        restored = lane.state["basket"][0]
        self.assertEqual(restored["entry_price"], 2064.0)
        self.assertEqual(restored["open_time_epoch"], opened)
        self.assertEqual(restored["entry_time_utc"], "2026-01-01T13:00:00+00:00")
        self.assertEqual(restored["timeout_at_utc"], "2026-01-01T13:30:00+00:00")

    def test_invalid_active_core_peak_is_normalized_for_conservative_rebuild(self):
        state = {"basket": [persisted_position()], "basket_peak_pnl_usd": "broken"}
        self.assertTrue(s24.S24NoAdverseRunner._normalize_core_peak_container(state))
        self.assertIsNone(state["basket_peak_pnl_usd"])

    def test_core_persists_one_no_signal_decision_receipt_per_bar(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        runner.executor = RecordingExecutor(positions=[], orders=[])
        runner._save_state = lambda: None
        rows = []
        runner._trade_row = lambda event, *_args, **kwargs: rows.append((event, kwargs))
        runner._signal_decision = lambda _row, _strategy: (None, "impulse_not_met")
        bar_time = pd.Timestamp.now(tz="UTC").floor("min")
        bars = pd.DataFrame(
            [
                {"Open": 2064.0, "High": 2064.1, "Low": 2063.9, "Close": 2064.0, "AskOpen": 2064.1},
                {"Open": 2064.0, "High": 2064.1, "Low": 2063.9, "Close": 2064.0, "AskOpen": 2064.1},
            ],
            index=[bar_time - pd.Timedelta(minutes=1), bar_time],
        )
        info = runner.executor.get_symbol_info()
        info.quote_time_msc = int(bar_time.timestamp() * 1000)

        runner._run_strategy(strategy, bars, info)
        runner._run_strategy(strategy, bars, info)

        state = runner._st(strategy)
        self.assertEqual(state["last_decision"], {
            "signal_bar_time": bar_time.isoformat(), "outcome": "no_signal", "reason": "impulse_not_met", "side": None,
        })
        receipts = [row for row in rows if row[0] == "strategy_decision"]
        self.assertEqual(len(receipts), 1)

    def test_live_disabled_preflight_still_requires_account_and_quote_identity(self):
        params = params_copy()
        params["live_trading_enabled"] = False
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner.dm = s24.FakeDM()

        class WrongAccountExecutor(RecordingExecutor):
            def get_account_info(self):
                result = dict(super().get_account_info())
                result["login"] = int(result["login"]) + 1
                return result

        wrong_account = WrongAccountExecutor()
        runner.executor = wrong_account
        self.assertFalse(runner.connect_and_preflight())

        class MissingQuoteClockExecutor(RecordingExecutor):
            def get_symbol_info(self, *_args, **_kwargs):
                result = super().get_symbol_info()
                result.quote_time_msc = None
                return result

        missing_quote_clock = MissingQuoteClockExecutor()
        runner.executor = missing_quote_clock
        self.assertFalse(runner.connect_and_preflight())

        runner.executor = RecordingExecutor()
        self.assertTrue(runner.connect_and_preflight())

    def test_live_switch_off_preserves_and_reconciles_broker_owned_core_basket(self):
        params = params_copy()
        params["live_trading_enabled"] = False
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        state = runner._st(strategy)
        state["basket"] = [persisted_position()]
        runner.executor = RecordingExecutor(positions=[live_position()], orders=[])
        runner._save_state = lambda: None
        rows = []
        runner._trade_row = lambda event, *_args, **kw: rows.append((event, kw))

        self.assertTrue(runner._sync_strategy(strategy))
        row = pd.Series({"Open": 2065.0, "Close": 2065.0}, name=pd.Timestamp("2026-01-01T13:10:00Z"))
        runner._close_basket(strategy, "basket_target", row, 1.0)

        self.assertEqual(state["basket"], [persisted_position()])
        self.assertEqual(runner.executor.close_calls, 0)
        self.assertEqual(state["pending_close_reason"], "basket_target")
        self.assertEqual(state["sync_block_reason"], "live_disabled_with_owned_inventory")
        self.assertTrue(state["sync_block_recoverable"])
        self.assertTrue(any(event == "basket_close_deferred" for event, _ in rows))

        runner.live_enabled = True
        self.assertTrue(runner._sync_strategy(strategy))
        self.assertFalse(state["sync_block_new_entries"])
        self.assertIsNone(state["sync_block_reason"])
        self.assertEqual(state["pending_close_reason"], "basket_target")

    def test_mode_transition_cannot_mix_shadow_and_broker_positions(self):
        params = params_copy()
        params["live_trading_enabled"] = False
        params["shadow_forward_enabled"] = True
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        state = runner._st(strategy)
        state["basket"] = [persisted_position()]
        runner._save_state = lambda: None
        rows = []
        runner._trade_row = lambda event, *_args, **kw: rows.append((event, kw))
        signal_time = pd.Timestamp("2026-01-01T13:10:00Z")
        row = pd.Series({"Open": 2065.0, "Close": 2065.0, "AskOpen": 2065.03}, name=signal_time)

        runner._open_entry(strategy, "LONG", row, SimpleNamespace(bid=2065.0, ask=2065.03))

        self.assertEqual(state["basket"], [persisted_position()])
        self.assertTrue(any(event == "entry_skip" and event_row.get("reason") == "execution_mode_transition_with_open_basket" for event, event_row in rows))

    def test_persisted_shadow_core_basket_has_valid_restart_identity(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        strategy = params["strategies"][0]
        state = runner._default_state()["strategies"][strategy["id"]]
        state["basket"] = [{
            "ticket": None,
            "position_identifier": 0,
            "side": "LONG",
            "lot": 0.01,
            "entry_price": 2064.0,
            "entry_time_utc": "2026-01-01T13:00:00+00:00",
            "open_time_epoch": 0,
            "owner_symbol": "XAUUSD",
            "owner_magic": s24.EXPECTED_S24_MAGIC,
            "owner_comment": "s24_no_adverse",
            "signal_bar_time": "2026-01-01T12:59:00+00:00",
            "close_submission_started_utc": None,
            "close_requested": False,
            "shadow": True,
        }]
        state["last_add_price"] = 2064.0

        self.assertIsNone(runner._core_state_shape_error(strategy, state))

    def test_malformed_shadow_runner_row_is_isolated_from_live_core_state(self):
        params = params_copy()
        strategy_id = params["strategies"][0]["id"]
        seed_runner = s24.S24NoAdverseRunner(params)
        seed = seed_runner._default_state()
        seed["strategies"][strategy_id]["basket"] = [persisted_position()]
        seed["strategies"][strategy_id]["shadow_runner"]["basket"] = ["corrupt"]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps(seed), encoding="utf-8")
            previous = s24.STATE_FILE
            s24.STATE_FILE = str(path)
            try:
                runner = s24.S24NoAdverseRunner(params)
            finally:
                s24.STATE_FILE = previous

        self.assertFalse(runner._fatal_state_identity_mismatch)
        self.assertEqual(runner.state["strategies"][strategy_id]["basket"], [persisted_position()])
        self.assertEqual(runner.state["strategies"][strategy_id]["shadow_runner"]["basket"], [])
        quarantined = runner.state["quarantined_shadow_runner_states"][strategy_id]
        self.assertEqual(quarantined["basket"], ["corrupt"])

    def test_invalid_quarantine_container_fails_closed_without_startup_exception(self):
        params = params_copy()
        seed_runner = s24.S24NoAdverseRunner(params)
        seed = seed_runner._default_state()
        seed["quarantined_strategy_states"] = ["corrupt"]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps(seed), encoding="utf-8")
            previous = s24.STATE_FILE
            s24.STATE_FILE = str(path)
            try:
                runner = s24.S24NoAdverseRunner(params)
            finally:
                s24.STATE_FILE = previous

        self.assertTrue(runner._fatal_state_identity_mismatch)
        self.assertEqual(runner.state["quarantined_strategy_states"], {})
        strategy = params["strategies"][0]
        self.assertEqual(runner._st(strategy)["sync_block_reason"], "state_identity_mismatch")

    def test_nonlist_invalid_core_basket_is_quarantined_without_shape_check_crash(self):
        params = params_copy()
        strategy_id = params["strategies"][0]["id"]
        seed_runner = s24.S24NoAdverseRunner(params)
        seed = seed_runner._default_state()
        seed["strategies"][strategy_id]["basket"] = {"ticket": 7001}

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps(seed), encoding="utf-8")
            previous = s24.STATE_FILE
            s24.STATE_FILE = str(path)
            try:
                runner = s24.S24NoAdverseRunner(params)
            finally:
                s24.STATE_FILE = previous

        self.assertTrue(runner._fatal_state_identity_mismatch)
        self.assertEqual(runner.state["quarantined_strategy_states"][strategy_id]["basket"], {"ticket": 7001})
        self.assertTrue(runner._st(params["strategies"][0])["sync_block_details"]["active_lifecycle_quarantined"])

    def test_malformed_core_time_close_aux_state_preserves_owned_basket(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        core = runner._default_state()["strategies"][params["strategies"][0]["id"]]
        core["basket"] = [persisted_position()]
        core["time_close_wide_seen"] = True
        core["time_close_defer_started_utc"] = "broken"
        core["time_close_last_quote_msc"] = -1
        core["time_close_stable_count"] = "three"

        self.assertTrue(runner._normalize_time_close_spread_container(core))
        self.assertEqual(core["basket"], [persisted_position()])
        self.assertIsNone(core["time_close_defer_started_utc"])
        self.assertIsNone(core["time_close_last_quote_msc"])
        self.assertEqual(core["time_close_stable_count"], 0)
        self.assertFalse(core["time_close_wide_seen"])

    def test_malformed_v206_time_close_aux_state_preserves_owned_basket(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        raw = default_v206_state()
        raw["basket"] = [{
            "ticket": 8206, "position_identifier": 9206, "side": "LONG", "lot": 0.01,
            "entry_price": 2064.0, "fixed_stop": 2059.0, "open_time_epoch": 1767272400,
            "entry_time_utc": "2026-01-01T13:00:00+00:00",
            "signal_bar_time": "2026-01-01T12:59:00+00:00",
            "timeout_at_utc": "2026-01-01T13:30:00+00:00",
            "owner_symbol": "XAUUSD", "owner_magic": 240206, "owner_comment": "s24_v206",
        }]
        raw["time_close_wide_seen"] = True
        raw["time_close_defer_started_utc"] = "broken"
        raw["time_close_stable_count"] = -4
        runner.state["v206"] = raw

        observed = runner.v206_lane.state
        self.assertEqual(len(observed["basket"]), 1)
        self.assertEqual(observed["basket"][0]["ticket"], 8206)
        self.assertIsNone(observed["time_close_defer_started_utc"])
        self.assertEqual(observed["time_close_stable_count"], 0)
        self.assertFalse(observed["time_close_wide_seen"])

    def test_definitive_no_fill_never_adopts_concurrent_inventory(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        signal_time = pd.Timestamp.now(tz="UTC").floor("min") - pd.Timedelta(minutes=1)
        row = pd.Series({"Open": 2064.0, "Close": 2064.0, "AskOpen": 2064.03}, name=signal_time)

        class ConcurrentExecutor(RecordingExecutor):
            def __init__(self):
                super().__init__(positions=[], orders=[])
                self.position_reads = 0
            def get_positions(self, *_args, **_kwargs):
                self.position_reads += 1
                return [] if self.position_reads == 1 else list(self.positions)
            def open_position(self, *_args, **kwargs):
                self.open_calls += 1
                comment = kwargs["comment"]
                self.positions = [SimpleNamespace(
                    ticket=9001, identifier=9901, symbol="XAUUSD", magic=s24.EXPECTED_S24_MAGIC,
                    comment=comment, type=s24.ORDER_TYPE_BUY, volume=0.01,
                    open_price=2064.03, open_time=int((signal_time + pd.Timedelta(minutes=1)).timestamp()),
                )]
                self.last_order_error = "ERR|TRADE_PERMISSION_GUARD"
                return None

        executor = ConcurrentExecutor()
        runner.executor = executor
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        runner._open_entry(strategy, "LONG", row, executor.get_symbol_info("XAUUSD"))
        self.assertEqual(runner._st(strategy)["basket"], [])
        self.assertEqual(runner._st(strategy)["sync_block_reason"], "unresolved_open_action")

    def test_core_exact_no_fill_open_retcodes_clear_reservation_after_confirmed_flat(self):
        signal_time = pd.Timestamp.now(tz="UTC").floor("min") - pd.Timedelta(minutes=1)
        row = pd.Series({"Open": 2064.0, "Close": 2064.0, "AskOpen": 2064.03}, name=signal_time)
        for retcode in (10018, 10026, 10027):
            with self.subTest(retcode=retcode):
                params = params_copy()
                params["live_trading_enabled"] = True
                runner = s24.S24NoAdverseRunner(params)
                runner.state = runner._default_state()
                strategy = params["strategies"][0]

                class ExactNoFillExecutor(RecordingExecutor):
                    def open_position(self, *_args, **_kwargs):
                        self.open_calls += 1
                        self.last_order_error = f"ERR|{retcode}|ORDER=0|DEAL=0|LAST=0"
                        return None

                executor = ExactNoFillExecutor(positions=[], orders=[])
                runner.executor = executor
                runner._save_state = lambda: None
                rows = []
                runner._trade_row = lambda event, _strategy, **kwargs: rows.append((event, kwargs))

                runner._open_entry(strategy, "LONG", row, executor.get_symbol_info("XAUUSD"))

                state = runner._st(strategy)
                self.assertIsNone(state["pending_open_opportunity_id"])
                self.assertIsNone(state["pending_open_started_utc"])
                self.assertIsNone(state["sync_block_reason"])
                self.assertEqual(state["entry_retry_signal_bar"], signal_time.isoformat())
                self.assertIsNotNone(state["entry_retry_after_utc"])
                if retcode == 10018:
                    self.assertEqual(state["entry_permission_reject_count"], 0)
                else:
                    self.assertEqual(state["entry_permission_reject_count"], 1)
                self.assertTrue(any(event == "entry_deferred" and values.get("reason") in {"market_closed", "trade_permission"} for event, values in rows))

    def test_core_exact_no_fill_is_persisted_before_mandatory_trade_csv(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        signal_time = pd.Timestamp.now(tz="UTC").floor("min") - pd.Timedelta(minutes=1)
        row = pd.Series({"Open": 2064.0, "Close": 2064.0, "AskOpen": 2064.03}, name=signal_time)

        class ExactNoFillExecutor(RecordingExecutor):
            def open_position(self, *_args, **_kwargs):
                self.open_calls += 1
                self.last_order_error = "ERR|10018|ORDER=0|DEAL=0|LAST=0"
                return None

        runner.executor = ExactNoFillExecutor(positions=[], orders=[])
        saved_states = []
        runner._save_state = lambda: saved_states.append(json.loads(json.dumps(runner.state)))

        def fail_deferred_csv(event, *_args, **_kwargs):
            if event == "entry_deferred":
                raise OSError("simulated mandatory trade CSV failure")

        runner._trade_row = fail_deferred_csv
        with self.assertRaisesRegex(OSError, "mandatory trade CSV failure"):
            runner._open_entry(strategy, "LONG", row, runner.executor.get_symbol_info("XAUUSD"))

        persisted = saved_states[-1]["strategies"][strategy["id"]]
        self.assertIsNone(persisted["pending_open_opportunity_id"])
        self.assertIsNone(persisted["pending_open_started_utc"])
        self.assertEqual(persisted["entry_retry_reason"], "market_closed")
        self.assertIsNotNone(persisted["entry_retry_after_utc"])

    def test_core_open_retry_does_not_resubmit_on_the_same_broker_quote(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        quote_time = pd.Timestamp.now(tz="UTC").floor("s")
        signal_time = quote_time.floor("min") - pd.Timedelta(minutes=1)
        row = pd.Series({"Open": 2064.0, "Close": 2064.0, "AskOpen": 2064.03}, name=signal_time)

        class ClosedExecutor(RecordingExecutor):
            def get_symbol_info(self, *_args, **_kwargs):
                info = super().get_symbol_info("XAUUSD")
                info.quote_time_msc = int(quote_time.timestamp() * 1000)
                return info
            def open_position(self, *_args, **_kwargs):
                self.open_calls += 1
                self.last_order_error = "ERR|10018|ORDER=0|DEAL=0|LAST=0"
                return None

        executor = ClosedExecutor(positions=[], orders=[])
        runner.executor = executor
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        info = executor.get_symbol_info("XAUUSD")

        runner._open_entry(strategy, "LONG", row, info)
        runner._open_entry(strategy, "LONG", row, info)

        self.assertEqual(executor.open_calls, 1)

    def test_core_open_permission_retry_escalates_after_three_fresh_quotes(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        due = pd.Timestamp.now(tz="UTC").floor("min") - pd.Timedelta(minutes=2)
        signal_time = due - pd.Timedelta(minutes=1)
        row = pd.Series({"Open": 2064.0, "Close": 2064.0, "AskOpen": 2064.03}, name=signal_time)

        class PermissionExecutor(RecordingExecutor):
            current_quote = due
            def get_symbol_info(self, *_args, **_kwargs):
                info = super().get_symbol_info("XAUUSD")
                info.quote_time_msc = int(self.current_quote.timestamp() * 1000)
                return info
            def open_position(self, *_args, **_kwargs):
                self.open_calls += 1
                self.last_order_error = "ERR|10026|ORDER=0|DEAL=0|LAST=0"
                return None

        executor = PermissionExecutor(positions=[], orders=[])
        runner.executor = executor
        runner._validated_core_quote_time = lambda _strategy, _info: (executor.current_quote, None)
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        for offset in (0, 60, 120):
            executor.current_quote = due + pd.Timedelta(seconds=offset)
            runner._open_entry(strategy, "LONG", row, executor.get_symbol_info("XAUUSD"))

        state = runner._st(strategy)
        self.assertEqual(executor.open_calls, 3)
        self.assertEqual(state["entry_permission_reject_count"], 3)
        self.assertEqual(state["sync_block_reason"], "core_entry_trade_permission_rejected_repeatedly")
        self.assertIsNone(state["entry_retry_after_utc"])

    def test_core_execution_bearing_open_reject_remains_unresolved(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        signal_time = pd.Timestamp.now(tz="UTC").floor("min") - pd.Timedelta(minutes=1)
        row = pd.Series({"Open": 2064.0, "Close": 2064.0, "AskOpen": 2064.03}, name=signal_time)

        class ExecutionBearingRejectExecutor(RecordingExecutor):
            def open_position(self, *_args, **_kwargs):
                self.open_calls += 1
                self.last_order_error = "ERR|10018|ORDER=7|DEAL=9|LAST=0"
                return None

        executor = ExecutionBearingRejectExecutor(positions=[], orders=[])
        runner.executor = executor
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None

        runner._open_entry(strategy, "LONG", row, executor.get_symbol_info("XAUUSD"))

        self.assertEqual(runner._st(strategy)["sync_block_reason"], "unresolved_open_action")
        self.assertIsNotNone(runner._st(strategy)["pending_open_opportunity_id"])

    def test_pre_open_rejects_duplicate_live_ticket_before_broker_command(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        second_state = persisted_position()
        second_state["ticket"] = 7002
        second_state["position_identifier"] = 7002
        runner._st(strategy)["basket"] = [persisted_position(), second_state]
        first_live = live_position()
        second_live = live_position()
        second_live.identifier = 7002
        executor = RecordingExecutor(positions=[first_live, second_live], orders=[])
        runner.executor = executor
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        signal_time = pd.Timestamp.now(tz="UTC").floor("min") - pd.Timedelta(minutes=1)
        row = pd.Series({"Open": 2064.0, "Close": 2064.0, "AskOpen": 2064.03}, name=signal_time)

        runner._open_entry(strategy, "LONG", row, executor.get_symbol_info("XAUUSD"))

        self.assertEqual(executor.open_calls, 0)
        self.assertEqual(runner._st(strategy)["sync_block_reason"], "pre_open_inventory_identity_invalid")

    def test_post_open_rejects_duplicate_live_ticket_instead_of_adopting_fill(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        runner._st(strategy)["basket"] = [persisted_position()]
        signal_time = pd.Timestamp.now(tz="UTC").floor("min") - pd.Timedelta(minutes=1)

        class DuplicateAfterOpenExecutor(RecordingExecutor):
            def open_position(self, *_args, **kwargs):
                self.open_calls += 1
                new_position = live_position()
                new_position.ticket = 7001
                new_position.identifier = 7999
                new_position.comment = kwargs["comment"]
                new_position.open_time = int((signal_time + pd.Timedelta(minutes=1)).timestamp())
                self.positions.append(new_position)
                self.last_open_identifier = 7999
                return 7001

        executor = DuplicateAfterOpenExecutor(positions=[live_position()], orders=[])
        runner.executor = executor
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        row = pd.Series({"Open": 2064.0, "Close": 2064.0, "AskOpen": 2064.03}, name=signal_time)

        runner._open_entry(strategy, "LONG", row, executor.get_symbol_info("XAUUSD"))

        self.assertEqual(executor.open_calls, 1)
        self.assertEqual(len(runner._st(strategy)["basket"]), 1)
        self.assertEqual(runner._st(strategy)["sync_block_reason"], "post_open_inventory_identity_invalid")

    def test_confirmed_or_unresolved_close_is_not_submitted_twice(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        state = runner._st(strategy)
        state["basket"] = [persisted_position()]
        state["basket"][0]["close_requested"] = True
        state["basket"][0]["close_submission_started_utc"] = None
        executor = RecordingExecutor(positions=[live_position()], orders=[])
        runner.executor = executor
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        row = pd.Series({"Open": 2064.0, "Close": 2064.0}, name=pd.Timestamp.now(tz="UTC").floor("min"))
        runner._close_basket(strategy, "basket_target", row, 20.0)
        self.assertEqual(executor.close_calls, 0)

        state["basket"][0]["close_requested"] = False
        state["basket"][0]["close_submission_started_utc"] = None
        close_attempts = []
        def malformed_close(*_args, **_kwargs):
            close_attempts.append(True)
            return s24.CloseResult(False, "MALFORMED_OK", raw_response="OK|truncated")
        executor.close_position = malformed_close
        runner._close_basket(strategy, "basket_target", row, 20.0)
        marker = state["basket"][0]["close_submission_started_utc"]
        self.assertIsNotNone(marker)
        self.assertEqual(len(close_attempts), 1)
        runner._close_basket(strategy, "basket_target", row, 20.0)
        self.assertEqual(len(close_attempts), 1)
        self.assertEqual(state["basket"][0]["close_submission_started_utc"], marker)

    def test_failed_manual_alert_is_retried_until_delivered(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._suppress_manual_alerts = False
        strategy = params["strategies"][0]
        calls = []
        original = s24.notify_manual_action_required
        def fake_notify(**kwargs):
            calls.append(kwargs)
            return len(calls) >= 2
        s24.notify_manual_action_required = fake_notify
        try:
            runner._notify_reconciliation_required(strategy, "test_failure", {"ticket": 7})
            self.assertIsNone(runner._st(strategy).get("manual_alert_last_signature"))
            runner._notify_reconciliation_required(strategy, "test_failure", {"ticket": 7})
            runner._notify_reconciliation_required(strategy, "test_failure", {"ticket": 7})
        finally:
            s24.notify_manual_action_required = original
        self.assertEqual(len(calls), 2)
        self.assertIsNotNone(runner._st(strategy).get("manual_alert_last_signature"))

    def test_corrupt_state_is_nonrecoverable_fail_closed(self):
        params = params_copy()
        original = s24.STATE_FILE
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            s24.STATE_FILE = str(path)
            try:
                runner = s24.S24NoAdverseRunner(params)
            finally:
                s24.STATE_FILE = original
        state = runner._st(params["strategies"][0])
        self.assertTrue(state["sync_block_new_entries"])
        self.assertEqual(state["sync_block_reason"], "state_identity_mismatch")
        self.assertFalse(state["sync_block_recoverable"])
        self.assertIn("load_error", state["sync_block_details"]["observed"])

    def test_state_live_volume_mismatch_is_rejected(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        strategy = params["strategies"][0]
        self.assertFalse(runner._state_matches_live(strategy, persisted_position(), live_position(volume=0.02)))

    def test_state_live_ticket_mismatch_is_rejected_even_when_identifier_matches(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        strategy = params["strategies"][0]
        live = live_position()
        live.ticket = 7999

        self.assertFalse(runner._state_matches_live(strategy, persisted_position(), live))

    def test_core_comment_namespace_requires_exact_or_colon_delimited_prefix(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]

        exact = live_position()
        delimited = live_position()
        delimited.comment = "s24_no_adverse:abc123def0"
        empty_suffix = live_position()
        empty_suffix.comment = "s24_no_adverse:"
        non_hash_suffix = live_position()
        non_hash_suffix.comment = "s24_no_adverse:owner"
        nested_suffix = live_position()
        nested_suffix.comment = "s24_no_adverse:abc123:foreign"
        foreign = live_position()
        foreign.comment = "s24_no_adverse_foreign"

        self.assertTrue(runner._owned_position(strategy, exact))
        self.assertTrue(runner._owned_position(strategy, delimited))
        self.assertFalse(runner._owned_position(strategy, empty_suffix))
        self.assertFalse(runner._owned_position(strategy, non_hash_suffix))
        self.assertFalse(runner._owned_position(strategy, nested_suffix))
        self.assertFalse(runner._owned_position(strategy, foreign))

        foreign_state = persisted_position()
        foreign_state["owner_comment"] = foreign.comment
        self.assertFalse(runner._state_ownership_proven(strategy, foreign_state))

        runner.executor = RecordingExecutor(positions=[foreign], orders=[])
        runner._save_state = lambda: None
        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(runner._st(strategy)["sync_block_reason"], "same_magic_unexpected_position_or_order")

    def test_unresolved_add_blocks_entry_but_preserves_confirmed_basket_exit(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        params["shadow_forward_enabled"] = False
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        state = runner._st(strategy)
        state["basket"] = [persisted_position()]
        state["pending_open_opportunity_id"] = "pending-add-2"
        state["pending_open_started_utc"] = "2026-01-01T13:10:00+00:00"
        state["last_exit_evaluated_bar"] = "2026-01-01T13:09:00+00:00"
        executor = RecordingExecutor(positions=[live_position()], orders=[])
        runner.executor = executor
        runner._save_state = lambda: None
        rows: list[tuple[str, dict]] = []
        runner._trade_row = lambda event, _strategy, **kwargs: rows.append((event, kwargs))

        bars = s24.add_features(s24.FakeDM().get_historical_data(), float(params["point_size"]))
        bars["spread_points"] = 30.0
        runner._run_strategy(strategy, bars, type("Info", (), {"bid": 2081.0, "ask": 2081.03})())

        self.assertEqual(executor.open_calls, 0)
        self.assertEqual(executor.close_calls, 1)
        self.assertEqual(state["sync_block_reason"], "unresolved_open_action")
        self.assertEqual(state["pending_open_opportunity_id"], "pending-add-2")
        self.assertTrue(any(event == "basket_close_requested" for event, _row in rows))

    def test_entry_regime_end_does_not_disable_existing_basket_exit(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        params["shadow_forward_enabled"] = False
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        state = runner._st(strategy)
        state["basket"] = [persisted_position()]
        executor = RecordingExecutor(positions=[live_position()], orders=[])
        runner.executor = executor
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None

        bars = s24.add_features(s24.FakeDM().get_historical_data(), float(params["point_size"]))
        bars["spread_points"] = 30.0
        index = list(bars.index)
        index[-1] = pd.Timestamp("2026-01-01T18:00:00Z")
        bars.index = pd.DatetimeIndex(index)
        runner._run_strategy(strategy, bars, type("Info", (), {"bid": 2081.0, "ask": 2081.03})())

        self.assertEqual(runner.entry_router.route(datetime(2026, 1, 1, 18, 0, tzinfo=s24.UTC)).status, "no_active_regime")
        self.assertEqual(executor.close_calls, 1)

    def test_trade_csv_schema_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "trades.csv"
            path.write_text("wrong,header\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                s24.validate_csv_schema(str(path), s24.TRADE_FIELDS)

    def test_trade_csv_malformed_data_rows_are_rejected_before_append(self):
        valid_row = [""] * len(s24.TRADE_FIELDS)
        for malformed in (valid_row[:-1], valid_row + ["extra"]):
            with self.subTest(columns=len(malformed)), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "trades.csv"
                with path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(s24.TRADE_FIELDS)
                    writer.writerow(malformed)
                before = path.read_bytes()

                with self.assertRaisesRegex(RuntimeError, "data row width mismatch"):
                    s24.validate_csv_schema(str(path), s24.TRADE_FIELDS)
                with self.assertRaisesRegex(RuntimeError, "data row width mismatch"):
                    s24.append_csv(str(path), {}, s24.TRADE_FIELDS)

                self.assertEqual(path.read_bytes(), before)

    def test_trade_csv_semantically_invalid_full_width_rows_are_rejected(self):
        def trade_row(**overrides):
            row = {
                "timestamp_utc": "2026-01-01T00:00:00+00:00",
                "event": "entry",
                "strategy_id": "visual_no_adverse_c_target16",
                "lane_id": "1",
                "magic": "200024",
                "symbol": "XAUUSD",
                "mt5_symbol": "XAUUSD",
                "side": "LONG",
                "lot": "0.01",
                "entry_price": "2000",
                "live": "True",
            }
            row.update(overrides)
            return [row.get(name, "") for name in s24.TRADE_FIELDS]

        invalid_rows = [
            list(s24.TRADE_FIELDS),
            trade_row(timestamp_utc="not-a-timestamp"),
            trade_row(event=""),
            trade_row(strategy_id=""),
            trade_row(symbol=""),
            trade_row(live="maybe"),
        ]
        for invalid_row in invalid_rows:
            with self.subTest(row=invalid_row), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "trades.csv"
                with path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(s24.TRADE_FIELDS)
                    writer.writerow(invalid_row)
                before = path.read_bytes()

                with self.assertRaisesRegex(RuntimeError, "CSV execution row invalid"):
                    s24.validate_csv_schema(str(path), s24.TRADE_FIELDS)
                with self.assertRaisesRegex(RuntimeError, "CSV execution row invalid"):
                    s24.append_csv(str(path), {}, s24.TRADE_FIELDS)

                self.assertEqual(path.read_bytes(), before)

    def test_existing_empty_trade_csv_is_not_silently_reinitialized(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "trades.csv"
            path.touch()

            with self.assertRaisesRegex(RuntimeError, "existing file is empty"):
                s24.validate_csv_schema(str(path), s24.TRADE_FIELDS)
            with self.assertRaisesRegex(RuntimeError, "existing file is empty"):
                s24.append_csv(str(path), {}, s24.TRADE_FIELDS)

            self.assertEqual(path.read_bytes(), b"")

            new_path = Path(root) / "new-trades.csv"
            s24.append_csv(
                str(new_path),
                {
                    "timestamp_utc": "2026-01-01T00:00:00+00:00",
                    "event": "entry_skip",
                    "strategy_id": "visual_no_adverse_c_target16",
                    "lane_id": 1,
                    "magic": 200024,
                    "symbol": "XAUUSD",
                    "mt5_symbol": "XAUUSD",
                    "live": False,
                },
                s24.TRADE_FIELDS,
            )
            s24.validate_csv_schema(str(new_path), s24.TRADE_FIELDS)
            with new_path.open("r", newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle, strict=True))
            self.assertEqual(rows[0], s24.TRADE_FIELDS)
            self.assertEqual(len(rows[1]), len(s24.TRADE_FIELDS))

    def test_validated_trade_csv_runtime_loss_or_replacement_fails_closed(self):
        valid_row = [""] * len(s24.TRADE_FIELDS)
        valid_values = {
            "timestamp_utc": "2026-01-01T00:00:00+00:00",
            "event": "entry_skip",
            "strategy_id": "visual_no_adverse_c_target16",
            "lane_id": "1",
            "magic": "200024",
            "symbol": "XAUUSD",
            "mt5_symbol": "XAUUSD",
            "live": "False",
        }
        for name, value in valid_values.items():
            valid_row[s24.TRADE_FIELDS.index(name)] = value
        for failure_mode in ("deleted", "replaced"):
            with self.subTest(failure_mode=failure_mode), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "trades.csv"
                with path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(s24.TRADE_FIELDS)
                    writer.writerow(valid_row)
                s24.validate_csv_schema(str(path), s24.TRADE_FIELDS)

                if failure_mode == "deleted":
                    path.unlink()
                    before = None
                else:
                    path.write_text("wrong,header\n", encoding="utf-8")
                    before = path.read_bytes()

                with self.assertRaises(RuntimeError):
                    s24.append_csv(str(path), {}, s24.TRADE_FIELDS)

                if before is None:
                    self.assertFalse(path.exists())
                else:
                    self.assertEqual(path.read_bytes(), before)

    def test_execution_csv_append_requires_file_and_new_path_durability_sync(self):
        row = {
            "timestamp_utc": "2026-01-01T00:00:00+00:00",
            "event": "entry_skip",
            "strategy_id": "visual_no_adverse_c_target16",
            "lane_id": 1,
            "magic": 200024,
            "symbol": "XAUUSD",
            "mt5_symbol": "XAUUSD",
            "live": False,
        }
        with tempfile.TemporaryDirectory() as root:
            existing_path = Path(root) / "existing.csv"
            with existing_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=s24.TRADE_FIELDS)
                writer.writeheader()
                writer.writerow(row)

            with mock.patch.object(s24.os, "fsync", side_effect=OSError("simulated durability failure")):
                with self.assertRaisesRegex(OSError, "durability failure"):
                    s24.append_csv(str(existing_path), row, s24.TRADE_FIELDS)

            new_path = Path(root) / "new.csv"
            with mock.patch.object(s24, "_fsync_parent_directory") as sync_parent:
                s24.append_csv(str(new_path), row, s24.TRADE_FIELDS)
            sync_parent.assert_called_once_with(str(new_path))

    def test_execution_csv_absent_path_creation_race_fails_closed(self):
        row = {
            "timestamp_utc": "2026-01-01T00:00:00+00:00",
            "event": "entry_skip",
            "strategy_id": "visual_no_adverse_c_target16",
            "lane_id": 1,
            "magic": 200024,
            "symbol": "XAUUSD",
            "mt5_symbol": "XAUUSD",
            "live": False,
        }
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "trades.csv"
            foreign_bytes = b"foreign,unvalidated\n"
            original_exists = s24.os.path.exists

            def raced_exists(candidate):
                if s24.os.path.normcase(s24.os.path.abspath(candidate)) == s24.os.path.normcase(str(path.resolve())):
                    path.write_bytes(foreign_bytes)
                    return False
                return original_exists(candidate)

            with mock.patch.object(s24.os.path, "exists", side_effect=raced_exists):
                with self.assertRaisesRegex(RuntimeError, "created concurrently"):
                    s24.append_csv(str(path), row, s24.TRADE_FIELDS)

            self.assertEqual(path.read_bytes(), foreign_bytes)

    def test_market_closed_keeps_close_marker_without_exact_requery(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        params["shadow_forward_enabled"] = False
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        state = runner._st(strategy)
        state["basket"] = [persisted_position()]

        class MarketClosedExecutor(RecordingExecutor):
            def __init__(self):
                super().__init__(positions=[live_position()], orders=[])
                self.ticket_reads = 0
            def get_position(self, ticket):
                self.ticket_reads += 1
                return self.positions[0] if self.ticket_reads == 1 else None
            def close_position(self, *_args, **_kwargs):
                self.close_calls += 1
                return s24.CloseResult(False, "MARKET_CLOSED", retcode=10018)

        runner.executor = MarketClosedExecutor()
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        now = pd.Timestamp.now(tz="UTC").floor("s")
        runner._close_basket(strategy, "basket_target", pd.Series(name=now, dtype=float), 1.0)

        self.assertIsNotNone(state["basket"][0]["close_submission_started_utc"])
        self.assertEqual(state["sync_block_reason"], "market_closed_close_inventory_unconfirmed")
        self.assertFalse(state["sync_block_recoverable"])

    def test_market_closed_clears_marker_only_after_exact_position_requery(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        params["shadow_forward_enabled"] = False
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        state = runner._st(strategy)
        state["basket"] = [persisted_position()]

        class MarketClosedExecutor(RecordingExecutor):
            def close_position(self, *_args, **_kwargs):
                self.close_calls += 1
                return s24.CloseResult(False, "MARKET_CLOSED", retcode=10018)

        runner.executor = MarketClosedExecutor(positions=[live_position()], orders=[])
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        now = pd.Timestamp.now(tz="UTC").floor("s")
        runner._close_basket(strategy, "basket_target", pd.Series(name=now, dtype=float), 1.0)

        self.assertIsNone(state["basket"][0]["close_submission_started_utc"])
        self.assertIsNotNone(state["close_retry_after_utc"])
        self.assertIsNone(state["sync_block_reason"])

    def test_retryable_core_close_no_fill_is_persisted_before_mandatory_trade_csv(self):
        for status in ("MARKET_CLOSED", "TRADE_PERMISSION_GUARD"):
            with self.subTest(status=status):
                params = params_copy()
                params["live_trading_enabled"] = True
                params["shadow_forward_enabled"] = False
                runner = s24.S24NoAdverseRunner(params)
                runner.state = runner._default_state()
                strategy = params["strategies"][0]
                runner._st(strategy)["basket"] = [persisted_position()]

                class RetryableCloseExecutor(RecordingExecutor):
                    def close_position(self, *_args, **_kwargs):
                        self.close_calls += 1
                        return s24.CloseResult(False, status)

                runner.executor = RetryableCloseExecutor(positions=[live_position()], orders=[])
                saved_states = []
                runner._save_state = lambda: saved_states.append(json.loads(json.dumps(runner.state)))

                def fail_deferred_csv(event, *_args, **_kwargs):
                    if event == "basket_close_deferred":
                        raise OSError("simulated mandatory trade CSV failure")

                runner._trade_row = fail_deferred_csv
                now = pd.Timestamp.now(tz="UTC").floor("s")
                with self.assertRaisesRegex(OSError, "mandatory trade CSV failure"):
                    runner._close_basket(strategy, "basket_target", pd.Series(name=now, dtype=float), 1.0)

                persisted = saved_states[-1]["strategies"][strategy["id"]]
                self.assertIsNone(persisted["basket"][0]["close_submission_started_utc"])
                self.assertIsNotNone(persisted["close_retry_after_utc"])

    def test_atomic_core_close_guard_clears_no_fill_marker_and_is_not_resubmitted(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        params["shadow_forward_enabled"] = False
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        state = runner._st(strategy)
        state["basket"] = [persisted_position()]

        class GuardExecutor(RecordingExecutor):
            def close_position(self, *_args, **_kwargs):
                self.close_calls += 1
                return s24.CloseResult(False, "ACCOUNT_IDENTITY_GUARD")

        runner.executor = GuardExecutor(positions=[live_position()], orders=[])
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        now = pd.Timestamp.now(tz="UTC").floor("s")
        row = pd.Series(name=now, dtype=float)
        runner._close_basket(strategy, "basket_target", row, 1.0)
        runner._close_basket(strategy, "basket_target", row, 1.0)

        self.assertEqual(runner.executor.close_calls, 1)
        self.assertIsNone(state["basket"][0]["close_submission_started_utc"])
        self.assertEqual(state["sync_block_reason"], "atomic_close_guard_rejected")

    def test_confirmed_close_replaces_resolved_ambiguity_when_orders_unavailable(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        params["shadow_forward_enabled"] = False
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        state = runner._st(strategy)
        closed = persisted_position()
        closed["close_submission_started_utc"] = "2026-01-01T13:30:00+00:00"
        state["basket"] = [closed]
        state["sync_block_new_entries"] = True
        state["sync_block_reason"] = "live_time_close_unconfirmed"
        state["sync_block_recoverable"] = False

        class ConfirmedCloseExecutor(RecordingExecutor):
            def get_orders(self, *_args, **_kwargs):
                return None
            def confirm_position_absent(self, _ticket):
                return True

        runner.executor = ConfirmedCloseExecutor(positions=[], orders=[])
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(state["basket"], [])
        self.assertEqual(state["sync_block_reason"], "orders_unavailable_after_confirmed_close")
        self.assertTrue(state["sync_block_recoverable"])

    def test_time_close_requires_fresh_stable_quotes_after_wide_spread(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        start = pd.Timestamp("2026-01-01T13:00:00Z")
        wide = SimpleNamespace(bid=2000.0, ask=2000.301)
        narrow = SimpleNamespace(bid=2000.0, ask=2000.200)

        self.assertFalse(runner._time_close_spread_ready(strategy, "max_hold", start, wide, "bar"))
        self.assertFalse(runner._time_close_spread_ready(strategy, "max_hold", start + pd.Timedelta(seconds=1), narrow, "bar"))
        self.assertFalse(runner._time_close_spread_ready(strategy, "max_hold", start + pd.Timedelta(seconds=2), narrow, "bar"))
        self.assertFalse(runner._time_close_spread_ready(strategy, "max_hold", start + pd.Timedelta(seconds=2), narrow, "bar"), "duplicate quote must not count")
        self.assertTrue(runner._time_close_spread_ready(strategy, "max_hold", start + pd.Timedelta(seconds=3), narrow, "bar"))

        runner._reset_time_close_spread_state(strategy)
        self.assertFalse(runner._time_close_spread_ready(strategy, "max_hold", start, wide, "bar"))
        self.assertTrue(runner._time_close_spread_ready(strategy, "max_hold", start + pd.Timedelta(minutes=30), wide, "bar"), "forced release must remain reachable")

    def test_v206_time_close_uses_the_same_fresh_quote_and_force_contract(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        lane = runner.v206_lane
        st = lane.state
        st["migration_pending"] = False
        st["migration_flat_confirmations"] = 3
        st["blocked_reason"] = None
        st["blocked_details"] = {}
        start = pd.Timestamp("2026-01-01T13:00:00Z")
        wide = SimpleNamespace(bid=2000.0, ask=2000.301)
        narrow = SimpleNamespace(bid=2000.0, ask=2000.200)

        self.assertFalse(lane._time_close_ready(wide, start))
        self.assertFalse(lane._time_close_ready(narrow, start + pd.Timedelta(seconds=1)))
        self.assertFalse(lane._time_close_ready(narrow, start + pd.Timedelta(seconds=2)))
        self.assertFalse(lane._time_close_ready(narrow, start + pd.Timedelta(seconds=2)))
        self.assertTrue(lane._time_close_ready(narrow, start + pd.Timedelta(seconds=3)))
        lane._reset_time_close_state()
        self.assertFalse(lane._time_close_ready(wide, start + pd.Timedelta(minutes=1)))
        self.assertTrue(lane._time_close_ready(wide, start + pd.Timedelta(minutes=31)))

    def test_time_close_accepts_first_narrow_quote_without_artificial_delay(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        quote_time = pd.Timestamp("2026-01-01T13:00:00Z")
        narrow = SimpleNamespace(bid=2000.0, ask=2000.200)

        self.assertTrue(runner._time_close_spread_ready(strategy, "max_hold", quote_time, narrow, "bar"))

    def test_time_close_force_releases_after_wide_spread_timeout(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        start = pd.Timestamp("2026-01-01T13:00:00Z")
        wide = SimpleNamespace(bid=2000.0, ask=2000.301)

        self.assertFalse(runner._time_close_spread_ready(strategy, "max_hold", start, wide, "bar"))
        self.assertTrue(
            runner._time_close_spread_ready(
                strategy,
                "max_hold",
                start + pd.Timedelta(minutes=float(params["time_close_force_after_minutes"])),
                wide,
                "bar",
            )
        )

    def test_malformed_strategy_container_isolated_and_blocked(self):
        params = params_copy()
        seed = s24.S24NoAdverseRunner(params)._default_state()
        strategy_id = params["strategies"][0]["id"]
        seed["strategies"][strategy_id] = "corrupt"
        original = s24.STATE_FILE
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            path.write_text(json.dumps(seed), encoding="utf-8")
            s24.STATE_FILE = str(path)
            try:
                runner = s24.S24NoAdverseRunner(params)
            finally:
                s24.STATE_FILE = original
        state = runner._st(params["strategies"][0])
        self.assertEqual(state["sync_block_reason"], "state_container_invalid")
        self.assertFalse(state["sync_block_recoverable"])
        self.assertEqual(runner.state["quarantined_strategy_states"][strategy_id], "corrupt")
        self.assertFalse(runner._fatal_state_identity_mismatch)

    def test_v10_core_state_load_adds_only_empty_closed_signal_evidence_container(self):
        params = params_copy()
        seed = s24.S24NoAdverseRunner(params)._default_state()
        strategy_id = params["strategies"][0]["id"]
        seed["strategies"][strategy_id].pop("last_closed_entry_signal_bars")
        original = s24.STATE_FILE
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            path.write_text(json.dumps(seed), encoding="utf-8")
            s24.STATE_FILE = str(path)
            try:
                runner = s24.S24NoAdverseRunner(params)
            finally:
                s24.STATE_FILE = original

        self.assertFalse(runner._fatal_state_identity_mismatch)
        self.assertEqual(runner._st(params["strategies"][0])["last_closed_entry_signal_bars"], [])

    def test_corrupt_closed_signal_evidence_container_fails_shape_without_exception(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        strategy = params["strategies"][0]
        state = runner._default_state()["strategies"][strategy["id"]]
        state["last_closed_entry_signal_bars"] = [{"bad": "shape"}]

        self.assertEqual(
            runner._core_state_shape_error(strategy, state),
            "last_closed_entry_signal_bars_invalid",
        )

    def test_current_core_state_rejects_position_missing_signal_identity(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        strategy = params["strategies"][0]
        state = runner._default_state()["strategies"][strategy["id"]]
        position = persisted_position()
        position.pop("signal_bar_time")
        state["basket"] = [position]

        self.assertEqual(
            runner._core_state_shape_error(strategy, state),
            "basket_row_invalid",
        )

    def test_current_core_state_rejects_mixed_side_basket(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        strategy = params["strategies"][0]
        state = runner._default_state()["strategies"][strategy["id"]]
        second = persisted_position()
        second["ticket"] = 7002
        second["position_identifier"] = 7002
        second["side"] = "SHORT"
        state["basket"] = [persisted_position(), second]

        self.assertEqual(runner._core_state_shape_error(strategy, state), "basket_side_mixed")

    def test_core_state_rejects_close_receipt_without_persisted_intent(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        strategy = params["strategies"][0]
        state = runner._default_state()["strategies"][strategy["id"]]
        position = persisted_position()
        position["close_requested"] = True
        state["basket"] = [position]

        self.assertEqual(runner._core_state_shape_error(strategy, state), "close_receipt_without_intent")

    def test_core_state_rejects_pending_close_without_basket(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        strategy = params["strategies"][0]
        state = runner._default_state()["strategies"][strategy["id"]]
        state["pending_close_reason"] = "basket_target"
        state["pending_close_signal_bar"] = "2026-01-01T13:00:00+00:00"

        self.assertEqual(runner._core_state_shape_error(strategy, state), "pending_close_without_basket")

    def test_v14_state_migrates_open_retry_defaults_without_quarantine(self):
        params = params_copy()
        seed = s24.S24NoAdverseRunner(params)._default_state()
        strategy_id = params["strategies"][0]["id"]
        active = seed["strategies"][strategy_id]
        for key in (
            "entry_retry_after_utc", "entry_retry_signal_bar", "entry_retry_reason",
            "entry_permission_reject_count",
        ):
            active.pop(key)
        seed["v206"].pop("entry_permission_reject_count")
        original = s24.STATE_FILE
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            path.write_text(json.dumps(seed), encoding="utf-8")
            s24.STATE_FILE = str(path)
            try:
                runner = s24.S24NoAdverseRunner(params)
            finally:
                s24.STATE_FILE = original

        migrated = runner._st(params["strategies"][0])
        self.assertFalse(runner._fatal_state_identity_mismatch)
        self.assertIsNone(migrated["entry_retry_after_utc"])
        self.assertIsNone(migrated["entry_retry_signal_bar"])
        self.assertIsNone(migrated["entry_retry_reason"])
        self.assertEqual(migrated["entry_permission_reject_count"], 0)
        self.assertEqual(runner.v206_lane.state["entry_permission_reject_count"], 0)

    def test_open_retry_state_rejects_partial_or_out_of_window_identity(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        strategy = params["strategies"][0]
        core = runner._default_state()["strategies"][strategy["id"]]
        core["entry_retry_after_utc"] = "2026-01-01T13:01:00+00:00"
        self.assertEqual(runner._core_state_shape_error(strategy, core), "entry_retry_identity_invalid")

        raw = default_v206_state()
        raw["pending_signal"] = {
            "opportunity_id": "v206:2026-01-01T13:00:00+00:00:LONG",
            "side": "LONG", "signal_bar_time": "2026-01-01T13:00:00+00:00",
            "entry_due_utc": "2026-01-01T13:01:00+00:00",
            "entry_expiry_utc": "2026-01-01T13:03:00+00:00",
            "fixed_stop": 2059.0,
            "retry_after_utc": "2026-01-01T13:04:00+00:00",
        }
        self.assertEqual(runner.v206_lane._state_shape_error(raw), "pending_signal_invalid")

    def test_legacy_active_core_state_migrates_without_losing_inventory_and_blocks_adds(self):
        params = params_copy()
        seed = s24.S24NoAdverseRunner(params)._default_state()
        strategy = params["strategies"][0]
        active = seed["strategies"][strategy["id"]]
        active.pop("position_signal_identity_required")
        position = persisted_position()
        position.pop("signal_bar_time")
        active["basket"] = [position]
        original = s24.STATE_FILE
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            path.write_text(json.dumps(seed), encoding="utf-8")
            s24.STATE_FILE = str(path)
            try:
                runner = s24.S24NoAdverseRunner(params)
            finally:
                s24.STATE_FILE = original

        migrated = runner._st(strategy)
        self.assertFalse(runner._fatal_state_identity_mismatch)
        self.assertEqual(migrated["basket"], [position])
        self.assertFalse(migrated["position_signal_identity_required"])
        self.assertEqual(
            runner._entry_submission_block_reason(strategy),
            "legacy_position_signal_identity_migration_pending",
        )

    def test_malformed_shadow_container_preserves_active_core_basket(self):
        params = params_copy()
        seed = s24.S24NoAdverseRunner(params)._default_state()
        strategy_id = params["strategies"][0]["id"]
        active = seed["strategies"][strategy_id]
        active["basket"] = [persisted_position()]
        active["shadow_runner"] = "corrupt"
        original = s24.STATE_FILE
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            path.write_text(json.dumps(seed), encoding="utf-8")
            s24.STATE_FILE = str(path)
            try:
                runner = s24.S24NoAdverseRunner(params)
            finally:
                s24.STATE_FILE = original
        state = runner._st(params["strategies"][0])
        self.assertFalse(runner._fatal_state_identity_mismatch)
        self.assertEqual(state["basket"], [persisted_position()])
        self.assertEqual(state["shadow_runner"]["basket"], [])
        self.assertEqual(runner.state["quarantined_shadow_runner_states"][strategy_id], "corrupt")

    def test_v206_invalid_aux_state_quarantines_active_lifecycle_snapshot(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        raw = runner.state["v206"]
        raw["basket"] = [{
            "ticket": 8206, "position_identifier": 8206, "side": "LONG", "lot": 0.01,
            "entry_price": 2000.0, "entry_time_utc": "2026-01-01T13:00:00+00:00",
            "open_time_epoch": 1767272400, "owner_symbol": "XAUUSD", "owner_magic": 240206,
            "owner_comment": "s24_v206", "signal_bar_time": "2026-01-01T12:59:00+00:00",
            "timeout_at_utc": "2026-01-01T13:30:00+00:00", "fixed_stop": 1999.5, "target": 2000.5,
        }]
        raw["blocked_details"] = "corrupt"

        state = runner.v206_lane.state

        self.assertEqual(state["blocked_reason"], "v206_state_identity_mismatch")
        self.assertTrue(state["blocked_details"]["quarantined"])
        self.assertEqual(state["quarantined_state_snapshot"]["basket"][0]["ticket"], 8206)

    def test_v206_active_quarantine_cannot_clear_on_flat_confirmations(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        lane = runner.v206_lane
        st = lane.state
        st["migration_pending"] = True
        st["migration_flat_confirmations"] = 2
        st["blocked_reason"] = "v206_state_identity_mismatch"
        st["blocked_details"] = {"reason": "blocked_details_shape", "quarantined": True}
        st["quarantined_state_snapshot"] = {"basket": [{"ticket": 8206}], "pending_open": None, "pending_close": None}
        runner.executor = RecordingExecutor(positions=[], orders=[])

        self.assertFalse(lane._sync(pd.Timestamp.now(tz="UTC"), SimpleNamespace(bid=2000.0, ask=2000.2)))
        self.assertTrue(st["migration_pending"])
        self.assertEqual(st["migration_flat_confirmations"], 2)
        self.assertEqual(st["blocked_reason"], "v206_state_identity_mismatch")
        self.assertTrue(st["blocked_details"]["active_lifecycle_quarantined"])

    def test_invalid_persisted_core_close_identity_is_quarantined_and_fatal(self):
        params = params_copy()
        seed = s24.S24NoAdverseRunner(params)._default_state()
        strategy_id = params["strategies"][0]["id"]
        active = seed["strategies"][strategy_id]
        active["basket"] = [persisted_position()]
        active["pending_close_reason"] = 7
        active["pending_close_signal_bar"] = "2026-01-01T13:01:00+00:00"
        original = s24.STATE_FILE
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            path.write_text(json.dumps(seed), encoding="utf-8")
            s24.STATE_FILE = str(path)
            try:
                runner = s24.S24NoAdverseRunner(params)
            finally:
                s24.STATE_FILE = original

        self.assertTrue(runner._fatal_state_identity_mismatch)
        self.assertEqual(
            runner._st(params["strategies"][0])["sync_block_details"]["cause"],
            "pending_close_reason_invalid",
        )

    def test_symbol_info_failure_reconciles_core_and_v206_without_downgrading_blocks(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        params["shadow_forward_enabled"] = False
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        strategy = params["strategies"][0]
        core = runner._st(strategy)
        core["basket"] = [persisted_position()]
        core["sync_block_new_entries"] = True
        core["sync_block_reason"] = "live_time_close_unconfirmed"
        core["sync_block_recoverable"] = False
        v206_calls = []
        runner.v206_lane.reconcile_without_quote = lambda *_args: v206_calls.append(True)
        runner.executor = RecordingExecutor(positions=[live_position()], orders=[])
        runner.executor.get_symbol_info = lambda *_args: None

        runner.run_once()

        self.assertEqual(v206_calls, [True])
        self.assertEqual(core["sync_block_reason"], "live_time_close_unconfirmed")
        self.assertFalse(core["sync_block_recoverable"])

    def test_symbol_info_failure_still_consumes_confirmed_core_close_deal(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        params["shadow_forward_enabled"] = False
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        strategy = params["strategies"][0]
        core = runner._st(strategy)
        core["basket"] = [persisted_position()]
        runner.v206_lane.reconcile_without_quote = lambda *_args: None
        runner.executor = RecordingExecutor(positions=[], orders=[])
        runner.executor.get_symbol_info = lambda *_args: None

        runner.run_once()

        self.assertEqual(core["basket"], [])
        self.assertEqual(core["sync_block_reason"], "symbol_info_failed")
        self.assertTrue(core["sync_block_recoverable"])

    def test_history_outage_retries_only_persisted_core_close_intent(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        params["shadow_forward_enabled"] = False
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        strategy = params["strategies"][0]
        runner._st(strategy)["basket"] = [persisted_position()]
        runner._st(strategy)["pending_close_reason"] = "basket_target"
        runner.executor = RecordingExecutor(positions=[live_position()], orders=[])
        calls = []
        runner._close_basket = lambda strat, reason, row, pnl: calls.append((strat["id"], reason, row.name, pnl))
        quote_time = pd.Timestamp.now(tz="UTC")
        info = SimpleNamespace(
            bid=2065.0,
            ask=2065.2,
            quote_time_msc=int(quote_time.timestamp() * 1000),
        )

        runner._manage_core_without_history(info)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "basket_target")

    def test_history_outage_does_not_originate_a_new_core_close(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        params["shadow_forward_enabled"] = False
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        strategy = params["strategies"][0]
        runner._st(strategy)["basket"] = [persisted_position()]
        runner.executor = RecordingExecutor(positions=[live_position()], orders=[])
        calls = []
        runner._close_basket = lambda *_args, **_kwargs: calls.append(True)
        quote_time = pd.Timestamp.now(tz="UTC")
        info = SimpleNamespace(
            bid=2100.0,
            ask=2100.2,
            quote_time_msc=int(quote_time.timestamp() * 1000),
        )

        runner._manage_core_without_history(info)

        self.assertEqual(calls, [])

    def test_v206_recoverable_inventory_error_cannot_downgrade_atomic_ambiguity(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        lane = runner.v206_lane
        lane.state["blocked_reason"] = "v206_timeout_close_unconfirmed"
        lane.state["blocked_details"] = {"ticket": 1}
        lane._block("v206_inventory_unavailable")
        self.assertEqual(lane.state["blocked_reason"], "v206_timeout_close_unconfirmed")

    def test_v206_atomic_close_guard_clears_no_fill_receipt_and_does_not_resubmit(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        params["shadow_forward_enabled"] = False
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        lane = runner.v206_lane
        st = lane.state
        st["migration_pending"] = False
        st["migration_flat_confirmations"] = 3
        st["blocked_reason"] = None
        st["blocked_details"] = {}
        st["basket"] = [{
            "ticket": 8206, "position_identifier": 8206, "side": "LONG", "lot": 0.01,
            "entry_price": 2000.0, "entry_time_utc": "2026-01-01T13:00:00+00:00",
            "open_time_epoch": 1767272400, "owner_symbol": "XAUUSD", "owner_magic": 240206,
            "owner_comment": "s24_v206", "signal_bar_time": "2026-01-01T12:59:00+00:00",
            "timeout_at_utc": "2026-01-01T13:30:00+00:00", "fixed_stop": 1999.5, "target": 2000.5,
        }]
        position = SimpleNamespace(
            ticket=8206, identifier=8206, symbol="XAUUSD", magic=240206, comment="s24_v206",
            type=s24.ORDER_TYPE_BUY, volume=0.01, open_price=2000.0, open_time=1767272400,
            sl=1999.5, tp=2000.5,
        )

        class GuardExecutor(RecordingExecutor):
            def __init__(self):
                super().__init__(positions=[position], orders=[])
                self.r1_close_calls = 0
            def close_r1_position(self, **_kwargs):
                self.r1_close_calls += 1
                return R1CloseResult(False, "ACCOUNT_IDENTITY_GUARD", "ERR|ACCOUNT_IDENTITY_GUARD")

        runner.executor = GuardExecutor()
        quote_time = pd.Timestamp("2026-01-01T13:31:00Z")
        info = SimpleNamespace(bid=2000.0, ask=2000.2)
        self.assertFalse(lane._sync(quote_time, info))
        self.assertFalse(lane._sync(quote_time + pd.Timedelta(seconds=1), info))

        self.assertEqual(runner.executor.r1_close_calls, 1)
        self.assertIsNone(st["pending_close"])
        self.assertEqual(st["blocked_reason"], "v206_atomic_close_guard_rejected")

    def test_v206_post_open_extra_inventory_blocks_before_fill_adoption(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        params["shadow_forward_enabled"] = False
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        lane = runner.v206_lane
        st = lane.state
        st["migration_pending"] = False
        st["migration_flat_confirmations"] = 3
        st["blocked_reason"] = None
        st["blocked_details"] = {}
        quote_time = pd.Timestamp.now(tz="UTC").floor("s")
        signal_bar = quote_time.floor("min") - pd.Timedelta(minutes=1)
        st["pending_signal"] = {
            "opportunity_id": f"v206:{signal_bar.isoformat()}:LONG",
            "side": "LONG",
            "signal_bar_time": signal_bar.isoformat(),
            "entry_due_utc": (signal_bar + pd.Timedelta(minutes=1)).isoformat(),
            "entry_expiry_utc": (signal_bar + pd.Timedelta(minutes=3)).isoformat(),
            "fixed_stop": 2059.0,
        }
        returned = SimpleNamespace(
            ticket=8206, identifier=9206, symbol="XAUUSD", magic=240206, comment="s24_v206",
            type=s24.ORDER_TYPE_BUY, volume=0.01, open_price=2064.0,
            open_time=int(quote_time.timestamp()), sl=2059.0, tp=2069.0,
        )
        extra = SimpleNamespace(
            ticket=8207, identifier=9207, symbol="XAUUSD", magic=240206, comment="s24_v206",
            type=s24.ORDER_TYPE_BUY, volume=0.01, open_price=2064.1,
            open_time=int(quote_time.timestamp()), sl=2059.1, tp=2069.1,
        )

        class ExtraInventoryExecutor(RecordingExecutor):
            def open_r1_position(self, *_args, **_kwargs):
                return SimpleNamespace(
                    status="CONFIRMED", ticket=8206, identifier=9206,
                    reason="", raw_response="OK|R1", fill=2064.0,
                )
            def repair_r1_position(self, **_kwargs):
                self.repair_calls = getattr(self, "repair_calls", 0) + 1
                raise AssertionError("repair must not run with extra namespace inventory")

        executor = ExtraInventoryExecutor(positions=[returned, extra], orders=[])
        runner.executor = executor
        info = executor.get_symbol_info("XAUUSD")
        info.quote_time_msc = int(quote_time.timestamp() * 1000)

        lane._attempt_pending_signal(info, quote_time)

        self.assertEqual(st["basket"], [])
        self.assertIsNotNone(st["pending_open"])
        self.assertEqual(st["blocked_reason"], "v206_post_open_inventory_delta_invalid")

    def test_v206_exact_no_fill_rearms_signal_with_broker_quote_retry(self):
        quote_time = pd.Timestamp.now(tz="UTC").floor("s")
        signal_bar = quote_time.floor("min") - pd.Timedelta(minutes=1)
        for reason in ("RETCODE_10018", "RETCODE_10026", "RETCODE_10027", "TRADE_PERMISSION_GUARD"):
            with self.subTest(reason=reason):
                params = params_copy()
                params["live_trading_enabled"] = True
                params["shadow_forward_enabled"] = False
                runner = s24.S24NoAdverseRunner(params)
                runner.state = runner._default_state()
                runner._save_state = lambda: None
                runner._trade_row = lambda *_args, **_kwargs: None
                lane = runner.v206_lane
                st = lane.state
                st["migration_pending"] = False
                st["migration_flat_confirmations"] = 3
                st["blocked_reason"] = None
                st["blocked_details"] = {}
                st["pending_signal"] = {
                    "opportunity_id": f"v206:{signal_bar.isoformat()}:LONG",
                    "side": "LONG", "signal_bar_time": signal_bar.isoformat(),
                    "entry_due_utc": (signal_bar + pd.Timedelta(minutes=1)).isoformat(),
                    "entry_expiry_utc": (signal_bar + pd.Timedelta(minutes=3)).isoformat(),
                    "fixed_stop": 2059.0,
                }

                class NoFillExecutor(RecordingExecutor):
                    def open_r1_position(self, *_args, **_kwargs):
                        return SimpleNamespace(status="NO_FILL", reason=reason, raw_response=reason)

                executor = NoFillExecutor(positions=[], orders=[])
                runner.executor = executor
                info = executor.get_symbol_info("XAUUSD")
                info.quote_time_msc = int(quote_time.timestamp() * 1000)

                lane._attempt_pending_signal(info, quote_time)

                self.assertIsNone(st["pending_open"])
                self.assertIsNotNone(st["pending_signal"])
                self.assertIsNotNone(st["pending_signal"].get("retry_after_utc"))
                expected_count = 0 if reason == "RETCODE_10018" else 1
                self.assertEqual(st["entry_permission_reject_count"], expected_count)

    def test_v206_permission_retry_escalates_after_three_fresh_quotes(self):
        due = pd.Timestamp.now(tz="UTC").floor("min") - pd.Timedelta(minutes=2)
        signal_bar = due - pd.Timedelta(minutes=1)
        params = params_copy()
        params["live_trading_enabled"] = True
        params["shadow_forward_enabled"] = False
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        lane = runner.v206_lane
        st = lane.state
        st["migration_pending"] = False
        st["migration_flat_confirmations"] = 3
        st["blocked_reason"] = None
        st["blocked_details"] = {}
        st["pending_signal"] = {
            "opportunity_id": f"v206:{signal_bar.isoformat()}:LONG",
            "side": "LONG", "signal_bar_time": signal_bar.isoformat(),
            "entry_due_utc": due.isoformat(),
            "entry_expiry_utc": (due + pd.Timedelta(minutes=2)).isoformat(),
            "fixed_stop": 2059.0,
        }

        class PermissionExecutor(RecordingExecutor):
            current_quote = due
            def get_symbol_info(self, *_args, **_kwargs):
                info = super().get_symbol_info("XAUUSD")
                info.quote_time_msc = int(self.current_quote.timestamp() * 1000)
                return info
            def open_r1_position(self, *_args, **_kwargs):
                self.open_calls += 1
                return SimpleNamespace(status="NO_FILL", reason="RETCODE_10026", raw_response="ERR|10026")

        executor = PermissionExecutor(positions=[], orders=[])
        runner.executor = executor
        lane._quote_clock_error = lambda _quote_time: None
        for offset in (0, 60, 120):
            executor.current_quote = due + pd.Timedelta(seconds=offset)
            lane._attempt_pending_signal(executor.get_symbol_info("XAUUSD"), executor.current_quote)

        self.assertEqual(executor.open_calls, 3)
        self.assertEqual(st["entry_permission_reject_count"], 3)
        self.assertEqual(st["blocked_reason"], "v206_entry_trade_permission_rejected_repeatedly")
        self.assertIsNone(st["pending_signal"])

    def test_v206_retries_close_deal_with_bounded_wider_history(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        params["shadow_forward_enabled"] = False
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        lane = runner.v206_lane
        st = lane.state
        st["migration_pending"] = False
        st["migration_flat_confirmations"] = 3
        st["blocked_reason"] = None
        st["blocked_details"] = {}
        st["basket"] = [{
            "ticket": 8206, "position_identifier": 9206, "side": "LONG", "lot": 0.01,
            "entry_price": 2000.0, "entry_time_utc": "2026-01-01T13:00:00+00:00",
            "open_time_epoch": 1767272400, "owner_symbol": "XAUUSD", "owner_magic": 240206,
            "owner_comment": "s24_v206", "signal_bar_time": "2026-01-01T12:59:00+00:00",
            "timeout_at_utc": "2026-01-01T13:30:00+00:00", "fixed_stop": 1999.5, "target": 2000.5,
        }]

        class WiderHistoryExecutor(RecordingExecutor):
            def __init__(self):
                super().__init__(positions=[], orders=[])
                self.close_deal_epochs = []
            def get_position_close_deal(self, position_id, opened_at_epoch):
                self.close_deal_epochs.append(opened_at_epoch)
                if opened_at_epoch > 0:
                    return False
                return SimpleNamespace(
                    deal=99206, position_id=position_id, symbol="XAUUSD", magic=0,
                    reason="DEAL_REASON_CLIENT", price=2001.0, profit=1.2,
                    commission=-0.1, swap=0.0, fee=0.0, deal_time=1767272520,
                    exit_volume=0.01, net_profit=1.1,
                )

        runner.executor = WiderHistoryExecutor()
        quote_time = pd.Timestamp("2026-01-01T13:31:00Z")
        info = SimpleNamespace(bid=2000.0, ask=2000.2)

        self.assertTrue(lane._sync(quote_time, info))
        self.assertEqual(runner.executor.close_deal_epochs, [1767272340, 0])
        self.assertEqual(st["basket"], [])
        self.assertIsNone(st["blocked_reason"])
        self.assertEqual(st["last_closed_at_utc"], "2026-01-01T13:02:00+00:00")
        self.assertEqual(st["last_closed_side"], "LONG")
        self.assertEqual(st["last_closed_reason"], "server_sl_tp_or_external_close")
        self.assertEqual(st["last_closed_signal_bar"], "2026-01-01T12:59:00+00:00")

    def test_state_validators_reject_unhashable_close_reason_without_exception(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        strategy = params["strategies"][0]
        for malformed in ([], {}, ["basket_target"]):
            core = runner._default_state()["strategies"][strategy["id"]]
            core["pending_close_reason"] = malformed
            self.assertEqual(
                runner._core_state_shape_error(strategy, core),
                "pending_close_reason_invalid",
            )

    def test_persisted_state_rejects_numeric_timestamps_and_boolean_financial_identity(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        strategy = params["strategies"][0]

        core = runner._default_state()["strategies"][strategy["id"]]
        core["last_core_quote_time_utc"] = 1
        self.assertEqual(
            runner._core_state_shape_error(strategy, core),
            "last_core_quote_time_utc_invalid",
        )

        core = runner._default_state()["strategies"][strategy["id"]]
        position = persisted_position()
        position["ticket"] = True
        core["basket"] = [position]
        self.assertEqual(runner._core_state_shape_error(strategy, core), "basket_row_invalid")

        raw = default_v206_state()
        raw["last_quote_time_utc"] = 1
        self.assertEqual(runner.v206_lane._state_shape_error(raw), "last_quote_time_utc_invalid")

        raw = default_v206_state()
        raw["pending_signal"] = {
            "opportunity_id": "v206:2026-01-01T13:00:00+00:00:LONG",
            "side": "LONG",
            "signal_bar_time": "2026-01-01T13:00:00+00:00",
            "entry_due_utc": "2026-01-01T13:01:00+00:00",
            "entry_expiry_utc": "2026-01-01T13:03:00+00:00",
            "fixed_stop": True,
        }
        self.assertEqual(runner.v206_lane._state_shape_error(raw), "pending_signal_invalid")

    def test_v206_state_validator_rejects_malformed_nested_financial_fields_without_exception(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        lane = runner.v206_lane
        raw = default_v206_state()
        raw["basket"] = [{
            "ticket": 8206,
            "position_identifier": 9206,
            "side": "LONG",
            "lot": 0.01,
            "entry_price": 2000.0,
            "entry_time_utc": "2026-01-01T13:00:00+00:00",
            "open_time_epoch": 1767272400,
            "owner_symbol": "XAUUSD",
            "owner_magic": 240206,
            "owner_comment": "s24_v206",
            "signal_bar_time": "2026-01-01T12:59:00+00:00",
            "timeout_at_utc": "2026-01-01T13:30:00+00:00",
            "fixed_stop": 1999.5,
            "target": {"corrupt": True},
        }]
        self.assertEqual(lane._state_shape_error(raw), "basket_row_invalid")

        raw = default_v206_state()
        raw["pending_open"] = {
            "opportunity_id": "v206:2026-01-01T13:00:00+00:00:LONG",
            "side": "LONG",
            "signal_bar_time": "2026-01-01T13:00:00+00:00",
            "entry_due_utc": "2026-01-01T13:01:00+00:00",
            "entry_expiry_utc": "2026-01-01T13:03:00+00:00",
            "fixed_stop": 1999.5,
            "started_utc": "2026-01-01T13:01:00+00:00",
            "flat_confirmations": 0,
            "lot": {"corrupt": True},
            "owner_symbol": "XAUUSD",
            "owner_magic": 240206,
            "owner_comment": "s24_v206",
        }
        self.assertEqual(lane._state_shape_error(raw), "pending_open_invalid")

    def test_core_atomic_open_guard_is_durable_nonrecoverable_block(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        strategy = params["strategies"][0]
        signal_time = pd.Timestamp.now(tz="UTC").floor("min") - pd.Timedelta(minutes=1)
        row = pd.Series(
            {"Open": 2064.0, "Close": 2064.0, "AskOpen": 2064.03},
            name=signal_time,
        )

        class GuardExecutor(RecordingExecutor):
            def open_position(self, *_args, **_kwargs):
                self.open_calls += 1
                self.last_order_error = "ERR|ACCOUNT_IDENTITY_GUARD"
                return None

        executor = GuardExecutor(positions=[], orders=[])
        runner.executor = executor
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        runner._open_entry(strategy, "LONG", row, executor.get_symbol_info("XAUUSD"))

        state = runner._st(strategy)
        self.assertIsNone(state["pending_open_opportunity_id"])
        self.assertIsNone(state["entry_retry_after_utc"])
        self.assertEqual(state["sync_block_reason"], "core_open_atomic_guard_rejected")
        self.assertFalse(state["sync_block_recoverable"])

    def test_v206_atomic_open_guard_is_durable_nonrecoverable_block(self):
        params = params_copy()
        params["live_trading_enabled"] = True
        params["shadow_forward_enabled"] = False
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        lane = runner.v206_lane
        state = lane.state
        state["migration_pending"] = False
        state["migration_flat_confirmations"] = 3
        state["blocked_reason"] = None
        state["blocked_details"] = {}
        due = pd.Timestamp.now(tz="UTC").floor("min")
        signal_bar = due - pd.Timedelta(minutes=1)
        state["pending_signal"] = {
            "opportunity_id": f"v206:{signal_bar.isoformat()}:LONG",
            "side": "LONG",
            "signal_bar_time": signal_bar.isoformat(),
            "entry_due_utc": due.isoformat(),
            "entry_expiry_utc": (due + pd.Timedelta(minutes=2)).isoformat(),
            "fixed_stop": 1999.5,
        }

        class GuardExecutor(RecordingExecutor):
            def get_symbol_info(self, *_args, **_kwargs):
                info = super().get_symbol_info("XAUUSD")
                info.quote_time_msc = int(due.timestamp() * 1000)
                return info

            def open_r1_position(self, *_args, **_kwargs):
                self.open_calls += 1
                return SimpleNamespace(
                    status="NO_FILL",
                    reason="ACCOUNT_IDENTITY_GUARD",
                    raw_response="ERR|ACCOUNT_IDENTITY_GUARD",
                )

        runner.executor = GuardExecutor(positions=[], orders=[])
        lane._quote_clock_error = lambda _quote_time: None
        lane._attempt_pending_signal(runner.executor.get_symbol_info("XAUUSD"), due)

        self.assertIsNone(state["pending_open"])
        self.assertIsNone(state["pending_signal"])
        self.assertEqual(state["blocked_reason"], "v206_open_atomic_guard_rejected")

    def test_auxiliary_state_validators_reject_nested_types_without_exception(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        strategy = params["strategies"][0]

        core = runner._default_state()["strategies"][strategy["id"]]
        core["last_decision"] = {
            "signal_bar_time": "2026-01-01T13:00:00+00:00",
            "outcome": [],
            "reason": "corrupt",
            "side": None,
        }
        self.assertEqual(runner._core_state_shape_error(strategy, core), "last_decision_invalid")

        shadow = runner._default_state()["strategies"][strategy["id"]]["shadow_runner"]
        shadow["last_evaluated_bar"] = 1
        self.assertEqual(runner._shadow_runner_state_error(shadow), "last_evaluated_bar_invalid")

        raw = default_v206_state()
        raw["last_closed_at_utc"] = "2026-01-01T13:02:00+00:00"
        raw["last_closed_side"] = []
        raw["last_closed_reason"] = "timeout_30m"
        self.assertEqual(runner.v206_lane._state_shape_error(raw), "last_closed_identity_invalid")

    def test_corrupt_passive_shadow_observer_state_does_not_block_core_startup(self):
        params = params_copy()
        params["runner_shadow"]["opportunity_observer"]["enabled"] = True
        params["runner_shadow"]["state_tagger"]["enabled"] = False
        previous = (s24.LOG_DIR, s24.STATE_DIR, s24.STATE_FILE)
        with tempfile.TemporaryDirectory() as root:
            log_dir = Path(root) / "logs"
            state_dir = Path(root) / "state"
            log_dir.mkdir()
            state_dir.mkdir()
            corrupt = state_dir / "s24_shadow_observer_state.json"
            corrupt.write_text("{broken", encoding="utf-8")
            s24.LOG_DIR = str(log_dir)
            s24.STATE_DIR = str(state_dir)
            s24.STATE_FILE = str(state_dir / "s24_bot_state.json")
            try:
                runner = s24.S24NoAdverseRunner(params)
            finally:
                s24.LOG_DIR, s24.STATE_DIR, s24.STATE_FILE = previous

            self.assertFalse(runner.shadow_observer.enabled)
            self.assertIn("opportunity_observer", runner.passive_shadow_init_errors)
            self.assertEqual(corrupt.read_text(encoding="utf-8"), "{broken")
            self.assertIn(params["strategies"][0]["id"], runner.state["strategies"])

    def test_corrupt_passive_shadow_observer_csv_does_not_block_core_startup(self):
        for csv_name in ("s24_shadow_opportunities.csv", "s24_shadow_markouts.csv"):
            with self.subTest(csv_name=csv_name), tempfile.TemporaryDirectory() as root:
                params = params_copy()
                params["runner_shadow"]["opportunity_observer"]["enabled"] = True
                params["runner_shadow"]["state_tagger"]["enabled"] = False
                log_dir = Path(root) / "logs"
                state_dir = Path(root) / "state"
                log_dir.mkdir()
                state_dir.mkdir()
                corrupt = log_dir / csv_name
                corrupt.write_text("wrong,header\n", encoding="utf-8")
                previous = (s24.LOG_DIR, s24.STATE_DIR, s24.STATE_FILE)
                s24.LOG_DIR = str(log_dir)
                s24.STATE_DIR = str(state_dir)
                s24.STATE_FILE = str(state_dir / "s24_bot_state.json")
                try:
                    runner = s24.S24NoAdverseRunner(params)
                finally:
                    s24.LOG_DIR, s24.STATE_DIR, s24.STATE_FILE = previous

                self.assertFalse(runner.shadow_observer.enabled)
                self.assertIn("opportunity_observer", runner.passive_shadow_init_errors)
                self.assertEqual(corrupt.read_text(encoding="utf-8"), "wrong,header\n")
                self.assertIn(params["strategies"][0]["id"], runner.state["strategies"])

    def test_invalid_passive_shadow_observer_options_do_not_block_core_startup(self):
        params = params_copy()
        params["runner_shadow"]["opportunity_observer"]["enabled"] = True
        params["runner_shadow"]["opportunity_observer"]["horizons_minutes"] = ["invalid"]
        params["runner_shadow"]["state_tagger"]["enabled"] = False

        runner = s24.S24NoAdverseRunner(params)

        self.assertFalse(runner.shadow_observer.enabled)
        self.assertIn("opportunity_observer", runner.passive_shadow_init_errors)
        self.assertIn(params["strategies"][0]["id"], runner.state["strategies"])

    def test_boolean_passive_shadow_lot_is_not_silently_coerced(self):
        params = params_copy()
        params["runner_shadow"]["lot"] = True
        params["runner_shadow"]["opportunity_observer"]["enabled"] = True
        params["runner_shadow"]["state_tagger"]["enabled"] = False

        runner = s24.S24NoAdverseRunner(params)

        self.assertFalse(runner.shadow_observer.enabled)
        self.assertIn("opportunity_observer", runner.passive_shadow_init_errors)
        self.assertIn(params["strategies"][0]["id"], runner.state["strategies"])

    def test_corrupt_passive_shadow_tagger_csv_does_not_block_core_startup(self):
        params = params_copy()
        params["runner_shadow"]["opportunity_observer"]["enabled"] = False
        params["runner_shadow"]["state_tagger"]["enabled"] = True
        previous = (s24.LOG_DIR, s24.STATE_DIR, s24.STATE_FILE)
        with tempfile.TemporaryDirectory() as root:
            log_dir = Path(root) / "logs"
            state_dir = Path(root) / "state"
            log_dir.mkdir()
            state_dir.mkdir()
            corrupt = log_dir / "s24_shadow_state_tags.csv"
            corrupt.write_text("wrong,header\n", encoding="utf-8")
            s24.LOG_DIR = str(log_dir)
            s24.STATE_DIR = str(state_dir)
            s24.STATE_FILE = str(state_dir / "s24_bot_state.json")
            try:
                runner = s24.S24NoAdverseRunner(params)
            finally:
                s24.LOG_DIR, s24.STATE_DIR, s24.STATE_FILE = previous

            self.assertFalse(runner.shadow_state_tagger.enabled)
            self.assertIn("state_tagger", runner.passive_shadow_init_errors)
            self.assertEqual(corrupt.read_text(encoding="utf-8"), "wrong,header\n")
            self.assertIn(params["strategies"][0]["id"], runner.state["strategies"])

    def test_corrupt_passive_shadow_runner_csv_does_not_fail_core_preflight(self):
        params = params_copy()
        params["runner_shadow"]["enabled"] = True
        params["runner_shadow"]["opportunity_observer"]["enabled"] = False
        params["runner_shadow"]["state_tagger"]["enabled"] = False
        previous = (
            s24.LOG_DIR,
            s24.STATE_DIR,
            s24.STATE_FILE,
            s24.TRADE_LOG_FILE,
            s24.SHADOW_RUNNER_LOG_FILE,
        )
        with tempfile.TemporaryDirectory() as root:
            log_dir = Path(root) / "logs"
            state_dir = Path(root) / "state"
            log_dir.mkdir()
            state_dir.mkdir()
            shadow_csv = log_dir / "s24_shadow_runner_trades.csv"
            shadow_csv.write_text("wrong,header\n", encoding="utf-8")
            s24.LOG_DIR = str(log_dir)
            s24.STATE_DIR = str(state_dir)
            s24.STATE_FILE = str(state_dir / "s24_bot_state.json")
            s24.TRADE_LOG_FILE = str(log_dir / "s24_trades.csv")
            s24.SHADOW_RUNNER_LOG_FILE = str(shadow_csv)
            try:
                runner = s24.S24NoAdverseRunner(params)
                runner.dm.connect = lambda: True
                runner.executor = s24.FakeExecutor(positions=[], orders=[])
                self.assertTrue(runner.connect_and_preflight())
            finally:
                (
                    s24.LOG_DIR,
                    s24.STATE_DIR,
                    s24.STATE_FILE,
                    s24.TRADE_LOG_FILE,
                    s24.SHADOW_RUNNER_LOG_FILE,
                ) = previous

            self.assertFalse(runner.passive_shadow_runner_enabled)
            self.assertIn("shadow_runner_csv", runner.passive_shadow_init_errors)
            self.assertEqual(shadow_csv.read_text(encoding="utf-8"), "wrong,header\n")

    def test_passive_shadow_runner_poll_failure_does_not_skip_core_strategy(self):
        params = params_copy()
        params["runner_shadow"]["enabled"] = True
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner.executor = s24.FakeExecutor(positions=[], orders=[])
        runner._runtime_info_clock_error = lambda _info: None
        runner.v206_lane.run_once = lambda _info: None
        runner.shadow_observer.observe_quote = lambda **_kwargs: None
        now = pd.Timestamp.now(tz="UTC").floor("min") - pd.Timedelta(minutes=1)
        runner._get_m1 = lambda: pd.DataFrame(
            [{"Open": 2064.0, "High": 2065.0, "Low": 2063.0, "Close": 2064.5}],
            index=[now],
        )
        runner._run_shadow_runner = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("shadow write failed"))
        core_calls = []
        runner._run_strategy = lambda strat, _bars, _info: core_calls.append(strat["id"])

        runner.run_once()

        self.assertEqual(core_calls, [params["strategies"][0]["id"]])
        self.assertFalse(runner.passive_shadow_runner_enabled)
        self.assertIn("shadow_runner_runtime", runner.passive_shadow_init_errors)

    def test_passive_shadow_observer_poll_failure_disables_only_observer(self):
        params = params_copy()
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner.executor = s24.FakeExecutor(positions=[], orders=[])
        runner._runtime_info_clock_error = lambda _info: None
        runner.v206_lane.run_once = lambda _info: None
        runner.shadow_observer.enabled = True
        runner.shadow_observer.observe_quote = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("observer write failed"))
        now = pd.Timestamp.now(tz="UTC").floor("min") - pd.Timedelta(minutes=1)
        runner._get_m1 = lambda: pd.DataFrame(
            [{"Open": 2064.0, "High": 2065.0, "Low": 2063.0, "Close": 2064.5}],
            index=[now],
        )
        runner.passive_shadow_runner_enabled = False
        core_calls = []
        runner._run_strategy = lambda strat, _bars, _info: core_calls.append(strat["id"])

        runner.run_once()

        self.assertEqual(core_calls, [params["strategies"][0]["id"]])
        self.assertFalse(runner.shadow_observer.enabled)
        self.assertIn("opportunity_observer_runtime", runner.passive_shadow_init_errors)

    def test_passive_shadow_route_failure_disables_only_observer(self):
        runner = s24.S24NoAdverseRunner(params_copy())
        runner.shadow_observer.enabled = True
        runner.shadow_observer.record_route = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("route write failed"))

        runner._shadow_route(
            "opportunity",
            status="rejected",
            reason="test",
            at=s24.utc_now(),
        )

        self.assertFalse(runner.shadow_observer.enabled)
        self.assertIn("opportunity_observer_runtime", runner.passive_shadow_init_errors)

    def test_v206_poll_exception_rolls_back_partial_lane_mutation(self):
        runner = s24.S24NoAdverseRunner(params_copy())
        runner.state = runner._default_state()
        runner.executor = s24.FakeExecutor(positions=[], orders=[])
        runner._save_state = lambda: None
        runner._runtime_info_clock_error = lambda _info: None
        runner.shadow_observer.observe_quote = lambda **_kwargs: None
        runner._get_m1 = lambda: None
        runner._manage_core_without_history = lambda _info: None

        def fail_after_mutation(_info):
            runner.state["v206"]["pending_open_opportunity_id"] = "partial"
            runner.state["v206"]["basket"] = [{"partial": True}]
            raise RuntimeError("v206 partial mutation")

        runner.v206_lane.run_once = fail_after_mutation
        runner.run_once()

        self.assertIsNone(runner.state["v206"].get("pending_open_opportunity_id"))
        self.assertEqual(runner.state["v206"]["basket"], [])
        self.assertEqual(runner.state["v206"]["blocked_reason"], "v206_poll_exception")
        self.assertIsNone(runner.v206_lane._state_shape_error(runner.state["v206"]))

    def test_v206_poll_exception_retains_valid_submission_receipt(self):
        runner = s24.S24NoAdverseRunner(params_copy())
        runner.state = runner._default_state()
        runner.executor = s24.FakeExecutor(positions=[], orders=[])
        runner._save_state = lambda: None
        runner._runtime_info_clock_error = lambda _info: None
        runner.shadow_observer.observe_quote = lambda **_kwargs: None
        runner._get_m1 = lambda: None
        runner._manage_core_without_history = lambda _info: None
        receipt = {
            "opportunity_id": "v206:2026-01-01T13:00:00+00:00:LONG",
            "side": "LONG",
            "signal_bar_time": "2026-01-01T13:00:00+00:00",
            "entry_due_utc": "2026-01-01T13:01:00+00:00",
            "entry_expiry_utc": "2026-01-01T13:03:00+00:00",
            "fixed_stop": 1999.5,
            "started_utc": "2026-01-01T13:01:00+00:00",
            "flat_confirmations": 0,
            "lot": 0.01,
            "owner_symbol": "XAUUSD",
            "owner_magic": 240206,
            "owner_comment": "s24_v206",
        }

        def fail_after_valid_receipt(_info):
            state = runner.state["v206"]
            state["migration_pending"] = False
            state["blocked_reason"] = None
            state["blocked_details"] = {}
            state["pending_open"] = dict(receipt)
            self.assertIsNone(runner.v206_lane._state_shape_error(state))
            raise RuntimeError("after durable submission receipt")

        runner.v206_lane.run_once = fail_after_valid_receipt
        runner.run_once()

        self.assertEqual(runner.state["v206"]["pending_open"], receipt)
        self.assertEqual(runner.state["v206"]["blocked_reason"], "v206_poll_exception")
        self.assertIsNone(runner.v206_lane._state_shape_error(runner.state["v206"]))

    def test_v206_quote_less_exception_rolls_back_partial_lane_mutation(self):
        runner = s24.S24NoAdverseRunner(params_copy())
        runner.state = runner._default_state()
        runner.executor = s24.FakeExecutor(positions=[], orders=[])
        runner.executor.get_symbol_info = lambda _symbol: None
        runner._save_state = lambda: None
        runner._sync_strategy = lambda _strat: True
        runner._set_sync_block = lambda *_args, **_kwargs: None

        def fail_after_mutation(_reason):
            runner.state["v206"]["pending_close_ticket"] = 999
            runner.state["v206"]["basket"] = [{"partial": True}]
            raise RuntimeError("v206 quote-less partial mutation")

        runner.v206_lane.reconcile_without_quote = fail_after_mutation
        runner.run_once()

        self.assertIsNone(runner.state["v206"].get("pending_close_ticket"))
        self.assertEqual(runner.state["v206"]["basket"], [])
        self.assertEqual(
            runner.state["v206"]["blocked_reason"],
            "v206_quote_less_reconciliation_exception",
        )
        self.assertIsNone(runner.v206_lane._state_shape_error(runner.state["v206"]))

    def test_passive_shadow_runner_exception_rolls_back_partial_lane_mutation(self):
        params = params_copy()
        params["runner_shadow"]["enabled"] = True
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner.executor = s24.FakeExecutor(positions=[], orders=[])
        runner._runtime_info_clock_error = lambda _info: None
        runner.v206_lane.run_once = lambda _info: None
        runner.shadow_observer.observe_quote = lambda **_kwargs: None
        runner._save_state = lambda: None
        now = pd.Timestamp.now(tz="UTC").floor("min") - pd.Timedelta(minutes=1)
        runner._get_m1 = lambda: pd.DataFrame(
            [{"Open": 2064.0, "High": 2065.0, "Low": 2063.0, "Close": 2064.5}],
            index=[now],
        )
        original = json.loads(json.dumps(runner._st(params["strategies"][0])["shadow_runner"]))

        def fail_after_mutation(strat, _bars, _info):
            lane = runner._st(strat)["shadow_runner"]
            lane["basket"] = [{"partial": True}]
            lane["last_evaluated_bar"] = "partial"
            raise RuntimeError("shadow partial mutation")

        runner._run_shadow_runner = fail_after_mutation
        runner._run_strategy = lambda _strat, _bars, _info: None
        runner.run_once()

        self.assertEqual(runner._st(params["strategies"][0])["shadow_runner"], original)
        self.assertFalse(runner.passive_shadow_runner_enabled)

    def test_clean_sync_malformed_related_ticket_retains_block_without_exception(self):
        state = {
            "sync_block_new_entries": True,
            "sync_block_reason": "unresolved_open_action",
            "sync_block_recoverable": False,
            "sync_block_details": {"ticket": {"corrupt": True}},
            "flat_clear_confirmation_count": 0,
            "flat_clear_confirmation_reason": None,
        }
        saves = []

        cleared = clean_sync_block_if_flat(
            symbol_key="visual_no_adverse_c_target16",
            state=state,
            positions=[],
            orders=[],
            save_state=lambda: saves.append(True),
            flat_auto_clear_reasons={"unresolved_open_action"},
            confirm_position_absent=lambda _ticket: True,
            required_flat_confirmations=3,
        )

        self.assertFalse(cleared)
        self.assertTrue(state["sync_block_new_entries"])
        self.assertEqual(state["flat_clear_confirmation_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
