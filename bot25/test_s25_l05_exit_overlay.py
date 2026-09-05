# -*- coding: utf-8 -*-
"""Causal, migration, and restart regressions for the frozen L05 overlay."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd
import numpy as np

import live_s25_bot as s25


class L05ExitOverlayTests(unittest.TestCase):
    def setUp(self):
        self.params = copy.deepcopy(s25.load_params())
        self.params["live_trading_enabled"] = False
        self.params["shadow_forward_enabled"] = True
        self.params["shadow_opportunity_observer"]["enabled"] = False
        self.params["shadow_state_tagger"]["enabled"] = False
        self.strategy = self.params["strategies"][0]

    def _runner_with_state(self, position):
        runner = object.__new__(s25.S25V24Runner)
        runner.params = self.params
        state = runner._default_strategy_state()
        state["episode_sequence"] = 1
        state["current_episode_id"] = "s25_v24_e000001"
        state["episode_start_quote_utc"] = "2026-09-05T00:00:00+00:00"
        state["last_long_frontier"] = 100.0
        state["last_short_frontier"] = 100.0
        state["positions"] = [position]
        runner.state = {
            "version": s25.STATE_VERSION,
            "bot": "bot25",
            "strategy_id": self.params["strategy_id"],
            "last_saved_utc": None,
            "strategies": {self.strategy["id"]: state},
        }
        return runner, state

    @staticmethod
    def _position(side="LONG", entry=101.0, ticket=-1, entry_bar="2026-09-05T00:00:00+00:00"):
        return {
            "ticket": ticket,
            "position_identifier": ticket,
            "side": side,
            "lot": 0.01,
            "entry_price": entry,
            "entry_time_utc": "2026-09-05T00:00:01+00:00",
            "open_time_epoch": 1788566401,
            "owner_symbol": "XAUUSD",
            "owner_magic": s25.EXPECTED_S25_MAGIC,
            "owner_comment": f"s25_m231_{'L' if side == 'LONG' else 'S'}0001",
            "shadow": True,
            "close_requested": False,
            "close_submission_started_utc": None,
            "l05_entry_m5_bar": entry_bar,
        }

    @staticmethod
    def _row(at, *, close, break_dir=0, low=float("nan"), high=float("nan")):
        return pd.Series(
            {
                "Close": close,
                "atr14": 1.0,
                "ema200": 100.0,
                "break_dir": break_dir,
                "native_pivot_low": low,
                "native_pivot_high": high,
            },
            name=pd.Timestamp(at),
        )

    def test_feature_builder_exports_the_native_level_used_by_a_break(self):
        index = pd.date_range("2026-09-01T00:00:00Z", periods=205, freq="5min")
        bars = pd.DataFrame(
            {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0}, index=index,
        )
        bars.iloc[200, bars.columns.get_loc("High")] = 105.0
        bars.iloc[204, bars.columns.get_loc("Open")] = 105.5
        bars.iloc[204, bars.columns.get_loc("High")] = 106.5
        bars.iloc[204, bars.columns.get_loc("Close")] = 106.0
        featured = s25.add_man231_features(bars)
        self.assertEqual(int(featured.iloc[204]["break_dir"]), 1)
        self.assertAlmostEqual(float(featured.iloc[204]["native_pivot_high"]), 105.0)
        self.assertTrue(np.isnan(float(featured.iloc[204]["native_pivot_low"])))

    def test_long_requires_later_reclaim_then_later_reloss_and_loss(self):
        position = self._position()
        runner, state = self._runner_with_state(position)
        info = SimpleNamespace(bid=99.0, ask=99.1)
        selected, _ = runner._l05_update_and_select(
            self.strategy, self._row("2026-09-05T00:05:00Z", close=99.0, break_dir=-1, low=100.0),
            info, pd.Timestamp("2026-09-05T00:05:00Z"),
        )
        self.assertEqual(selected, [])
        self.assertFalse(state["l05_down_break"]["reclaimed"])
        selected, _ = runner._l05_update_and_select(
            self.strategy, self._row("2026-09-05T00:10:00Z", close=100.0),
            info, pd.Timestamp("2026-09-05T00:10:00Z"),
        )
        self.assertEqual(selected, [])
        self.assertTrue(state["l05_down_break"]["reclaimed"])
        selected, note = runner._l05_update_and_select(
            self.strategy, self._row("2026-09-05T00:15:00Z", close=99.0),
            info, pd.Timestamp("2026-09-05T00:15:00Z"),
        )
        self.assertEqual(selected, [position])
        self.assertIn("selected=1", note)

        position["entry_price"] = 98.0
        selected, _ = runner._l05_update_and_select(
            self.strategy, self._row("2026-09-05T00:20:00Z", close=99.0),
            info, pd.Timestamp("2026-09-05T00:20:00Z"),
        )
        self.assertEqual(selected, [], "a profitable ticket must not be closed by L05")

    def test_short_is_symmetric_and_break_before_entry_is_ineligible(self):
        position = self._position(side="SHORT", entry=99.0, entry_bar="2026-09-05T00:10:00Z")
        runner, state = self._runner_with_state(position)
        info = SimpleNamespace(bid=101.0, ask=101.1)
        runner._l05_update_and_select(
            self.strategy, self._row("2026-09-05T00:05:00Z", close=101.0, break_dir=1, high=100.0),
            info, pd.Timestamp("2026-09-05T00:05:00Z"),
        )
        runner._l05_update_and_select(
            self.strategy, self._row("2026-09-05T00:10:00Z", close=100.0),
            info, pd.Timestamp("2026-09-05T00:10:00Z"),
        )
        selected, _ = runner._l05_update_and_select(
            self.strategy, self._row("2026-09-05T00:15:00Z", close=101.0),
            info, pd.Timestamp("2026-09-05T00:15:00Z"),
        )
        self.assertEqual(selected, [], "a pre-entry break must not arm a later ticket")

        runner._l05_update_and_select(
            self.strategy, self._row("2026-09-05T00:20:00Z", close=101.0, break_dir=1, high=100.0),
            info, pd.Timestamp("2026-09-05T00:20:00Z"),
        )
        runner._l05_update_and_select(
            self.strategy, self._row("2026-09-05T00:25:00Z", close=100.0),
            info, pd.Timestamp("2026-09-05T00:25:00Z"),
        )
        selected, _ = runner._l05_update_and_select(
            self.strategy, self._row("2026-09-05T00:30:00Z", close=101.0),
            info, pd.Timestamp("2026-09-05T00:30:00Z"),
        )
        self.assertEqual(selected, [position])
        self.assertTrue(state["l05_up_break"]["reclaimed"])

    def test_process_routes_l05_through_existing_close_before_native_logic(self):
        position = self._position()
        runner, state = self._runner_with_state(position)
        state["l05_activation_m5_bar"] = "2026-09-05T00:00:00+00:00"
        state["l05_down_break"] = {
            "level": 100.0,
            "break_m5_bar": "2026-09-05T00:05:00+00:00",
            "reclaimed": True,
        }
        state["last_processed_m5_bar"] = "2026-09-05T00:10:00+00:00"
        runner._save_state = mock.Mock()
        runner._trade_row = mock.Mock()
        runner._close_positions = mock.Mock(return_value="requested")
        runner._ensure_virtual_bilateral_core = mock.Mock(return_value=True)
        runner._process_m5_event(
            self.strategy,
            self._row("2026-09-05T00:15:00Z", close=99.0),
            SimpleNamespace(bid=99.0, ask=99.1, point=0.001),
            pd.Timestamp("2026-09-05T00:20:00Z"),
        )
        self.assertEqual(runner._close_positions.call_args.args[2], "loss_policy_L05")
        runner._ensure_virtual_bilateral_core.assert_not_called()

    def test_restart_restores_reclaimed_tracker_and_ticket_eligibility(self):
        position = self._position()
        with tempfile.TemporaryDirectory(prefix="s25-l05-restart-") as temp:
            state_path = str(Path(temp) / "state.json")
            with mock.patch.object(s25, "STATE_FILE", state_path):
                seed, state = self._runner_with_state(position)
                state["l05_activation_m5_bar"] = "2026-09-05T00:00:00+00:00"
                state["l05_down_break"] = {
                    "level": 100.0,
                    "break_m5_bar": "2026-09-05T00:05:00+00:00",
                    "reclaimed": True,
                }
                state["last_processed_m5_bar"] = "2026-09-05T00:10:00+00:00"
                s25.atomic_write_json(state_path, seed.state)
                restarted = s25.S25V24Runner(self.params)
                self.assertEqual(restarted._state_identity_status, "current")
                selected, _ = restarted._l05_update_and_select(
                    self.strategy,
                    self._row("2026-09-05T00:15:00Z", close=99.0),
                    SimpleNamespace(bid=99.0, ask=99.1),
                    pd.Timestamp("2026-09-05T00:15:00Z"),
                )
                self.assertEqual([row["position_identifier"] for row in selected], [-1])

    def test_current_state_rejects_malformed_or_future_l05_tracker(self):
        runner, state = self._runner_with_state(self._position())
        state["l05_activation_m5_bar"] = "2026-09-05T00:00:00+00:00"
        state["last_processed_m5_bar"] = "2026-09-05T00:10:00+00:00"
        state["l05_down_break"] = {
            "level": 100.0,
            "break_m5_bar": "2026-09-05T00:15:00+00:00",
            "reclaimed": False,
        }
        self.assertEqual(runner._current_state_shape_error(runner.state), "l05_down_break_time_order_invalid")

    def test_current_state_rejects_tracker_without_activation_identity(self):
        runner, state = self._runner_with_state(self._position(entry_bar=None))
        state["last_processed_m5_bar"] = "2026-09-05T00:10:00+00:00"
        state["l05_down_break"] = {
            "level": 100.0,
            "break_m5_bar": "2026-09-05T00:05:00+00:00",
            "reclaimed": True,
        }
        self.assertEqual(
            runner._current_state_shape_error(runner.state),
            "l05_activation_identity_incomplete",
        )

    def test_current_state_rejects_ticket_mark_without_activation_or_watermark(self):
        runner, _state = self._runner_with_state(self._position())
        self.assertEqual(
            runner._current_state_shape_error(runner.state),
            "l05_activation_identity_incomplete",
        )

    def test_l05_identity_is_frozen_and_not_a_productive_close(self):
        runner = object.__new__(s25.S25V24Runner)
        runner.params = copy.deepcopy(self.params)
        self.assertIsNone(runner._configuration_contract_error())
        runner.params["strategies"][0]["loss_policy"] = "NONE"
        self.assertEqual(runner._configuration_contract_error(), "strategy_config_mismatch:loss_policy")
        self.assertNotIn("loss_policy_L05", s25.PRODUCTIVE_CLOSE_REASONS)

    def test_v7_nonflat_upgrade_preserves_inventory_and_is_nonretroactive(self):
        params = copy.deepcopy(self.params)
        strategy = params["strategies"][0]
        live = SimpleNamespace(
            ticket=25, identifier=5025, symbol="XAUUSD", type=s25.ORDER_TYPE_BUY,
            volume=0.01, open_price=101.0, sl=0.0, tp=0.0, profit=-2.0,
            magic=s25.EXPECTED_S25_MAGIC, open_time=1788566401,
            comment="s25_m231_L0025",
        )
        with tempfile.TemporaryDirectory(prefix="s25-l05-v7-upgrade-") as temp:
            state_path = str(Path(temp) / "state.json")
            trade_path = str(Path(temp) / "trades.csv")
            with mock.patch.object(s25, "STATE_FILE", state_path), mock.patch.object(s25, "TRADE_LOG_FILE", trade_path):
                seed = s25.S25V24Runner(params)
                old = seed._default_state()
                old["version"] = s25.PREVIOUS_V24_STATE_VERSION
                old_state = old["strategies"][strategy["id"]]
                old_state.pop("l05_activation_m5_bar")
                old_state.pop("l05_down_break")
                old_state.pop("l05_up_break")
                old_state["last_processed_m5_bar"] = "2026-09-05T00:00:00+00:00"
                old_state["episode_sequence"] = 1
                old_state["current_episode_id"] = "s25_v24_e000001"
                old_state["episode_start_quote_utc"] = "2026-09-05T00:00:01+00:00"
                saved = seed._state_position_from_live(strategy, live)
                saved.pop("l05_entry_m5_bar")
                old_state["positions"] = [saved]
                old_state["legacy_physical_core_position_ids"] = {"LONG": None, "SHORT": None}
                s25.atomic_write_json(state_path, old)
                before = hashlib.sha256(Path(state_path).read_bytes()).hexdigest()

                runner = s25.S25V24Runner(params)
                runner.dm = s25.FakeDM()
                runner.executor = s25.FakeExecutor(positions=[live])
                runner.executor.open_position = mock.Mock(wraps=runner.executor.open_position)
                runner.executor.close_position = mock.Mock(wraps=runner.executor.close_position)
                runner._suppress_manual_alerts = True
                self.assertEqual(runner._state_identity_status, "compatible_v24_to_l05_pending")
                with mock.patch.object(s25, "_SELF_TEST_HISTORICAL_QUOTES", True):
                    self.assertTrue(runner.connect_and_preflight())
                runner.executor.open_position.assert_not_called()
                runner.executor.close_position.assert_not_called()
                self.assertEqual(runner._st(strategy)["legacy_physical_core_position_ids"], {"LONG": None, "SHORT": None})
                migrated = json.loads(Path(state_path).read_text(encoding="utf-8"))
                self.assertNotEqual(hashlib.sha256(Path(state_path).read_bytes()).hexdigest(), before)
                self.assertEqual(migrated["version"], s25.STATE_VERSION)
                migrated_position = migrated["strategies"][strategy["id"]]["positions"][0]
                self.assertEqual(migrated_position["position_identifier"], live.identifier)
                self.assertEqual(migrated_position["l05_entry_m5_bar"], "2026-09-05T00:00:00+00:00")
                self.assertIsNone(migrated["strategies"][strategy["id"]]["l05_down_break"])
                self.assertAlmostEqual(migrated["strategies"][strategy["id"]]["last_long_frontier"], 4020.09)
                self.assertAlmostEqual(migrated["strategies"][strategy["id"]]["last_short_frontier"], 4020.09)

    def test_v7_upgrade_with_incomplete_pending_lifecycle_is_rejected_without_overwrite(self):
        params = copy.deepcopy(self.params)
        strategy = params["strategies"][0]
        with tempfile.TemporaryDirectory(prefix="s25-l05-v7-pending-") as temp:
            state_path = str(Path(temp) / "state.json")
            with mock.patch.object(s25, "STATE_FILE", state_path):
                seed = s25.S25V24Runner(params)
                old = seed._default_state()
                old["version"] = s25.PREVIOUS_V24_STATE_VERSION
                state = old["strategies"][strategy["id"]]
                state.pop("l05_activation_m5_bar")
                state.pop("l05_down_break")
                state.pop("l05_up_break")
                state["pending_close_reason"] = "opposite_pivot_break"
                s25.atomic_write_json(state_path, old)
                before = Path(state_path).read_bytes()
                runner = s25.S25V24Runner(params)
                runner.executor = s25.FakeExecutor()
                self.assertEqual(runner._state_identity_status, "foreign_or_invalid")
                self.assertFalse(runner.connect_and_preflight())
                self.assertEqual(Path(state_path).read_bytes(), before)

    def test_v6_upgrade_uses_one_nonretroactive_l05_watermark(self):
        params = copy.deepcopy(self.params)
        strategy = params["strategies"][0]
        with tempfile.TemporaryDirectory(prefix="s25-l05-v6-watermark-") as temp:
            state_path = str(Path(temp) / "state.json")
            with mock.patch.object(s25, "STATE_FILE", state_path):
                seed = s25.S25V24Runner(params)
                old = seed._default_state()
                old["version"] = s25.PREVIOUS_STATE_VERSION
                old["strategy_id"] = s25.PREVIOUS_STRATEGY_ID
                old_state = old["strategies"].pop(strategy["id"])
                old["strategies"] = {s25.PREVIOUS_STRATEGY_KEY: old_state}
                old_state.pop("l05_activation_m5_bar")
                old_state.pop("l05_down_break")
                old_state.pop("l05_up_break")
                old_state["last_processed_m5_bar"] = "2026-09-05T00:10:00+00:00"
                saved = self._position(entry_bar=None)
                saved["shadow"] = False
                saved["ticket"] = 25
                saved["position_identifier"] = 5025
                saved.pop("l05_entry_m5_bar")
                old_state["positions"] = [saved]
                s25.atomic_write_json(state_path, old)

                runner = s25.S25V24Runner(params)
                migrated = runner._st(strategy)
                self.assertEqual(runner._state_identity_status, "compatible_legacy_to_v24_pending")
                self.assertEqual(migrated["l05_activation_m5_bar"], "2026-09-05T00:10:00+00:00")
                self.assertEqual(migrated["positions"][0]["l05_entry_m5_bar"], "2026-09-05T00:10:00+00:00")
                self.assertIsNone(migrated["l05_down_break"])
                self.assertEqual(
                    runner._current_state_shape_error(runner.state),
                    "position_without_episode_identity",
                )


if __name__ == "__main__":
    unittest.main()
