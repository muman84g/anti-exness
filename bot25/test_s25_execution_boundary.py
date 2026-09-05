# -*- coding: utf-8 -*-
"""Fail-closed regression tests for the bot25 broker execution boundary."""

from __future__ import annotations

import copy
import csv
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd

import live_executor
import ea_bridge
import live_data_fetcher
import live_s25_bot as s25


class ExecutionBoundaryTests(unittest.TestCase):
    def test_strict_json_rejects_duplicate_and_nonfinite_values(self):
        with self.assertRaises(ValueError):
            s25.strict_json_load_bytes(b'{"version":7,"version":7}')
        with self.assertRaises(ValueError):
            s25.strict_json_load_bytes(b'{"value":NaN}')

    def test_runner_singleton_lock_rejects_second_process_namespace_owner(self):
        with tempfile.TemporaryDirectory(prefix="s25-runner-lock-") as temp:
            path = str(Path(temp) / "state" / "s25_runner.lock")
            first = s25.acquire_runner_singleton_lock(path)
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(s25.acquire_runner_singleton_lock(path))
            finally:
                first.close()
            third = s25.acquire_runner_singleton_lock(path)
            self.assertIsNotNone(third)
            third.close()

    def test_frozen_strategy_config_tamper_fails_preflight_contract(self):
        params = copy.deepcopy(s25.load_params())
        params["strategies"][0]["frontier_add_atr"] = 0.49
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        runner = s25.S25V24Runner(params)
        runner._suppress_manual_alerts = True
        self.assertEqual(runner._configuration_contract_error(), "strategy_config_mismatch:frontier_add_atr")

        params = copy.deepcopy(s25.load_params())
        params["max_signal_delay_minutes"] = 8
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        runner = s25.S25V24Runner(params)
        self.assertEqual(runner._configuration_contract_error(), "config_mismatch:max_signal_delay_minutes")

        params = copy.deepcopy(s25.load_params())
        params["poll_interval_seconds"] = 10
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        runner = s25.S25V24Runner(params)
        self.assertEqual(runner._configuration_contract_error(), "config_mismatch:poll_interval_seconds")

    def test_frozen_config_rejects_boolean_numeric_aliases_and_nonboolean_modes(self):
        mutations = (
            ("mode", "live_trading_enabled", 1, "live_shadow_mode_boolean_required"),
            ("top", "poll_interval_seconds", True, "config_mismatch:poll_interval_seconds"),
            ("strategy", "virtual_core_positions_per_side", True, "strategy_config_mismatch:virtual_core_positions_per_side"),
            ("strategy", "physical_seed_orders", False, "strategy_config_mismatch:physical_seed_orders"),
            ("strategy", "lot", False, "strategy_config_mismatch:lot"),
        )
        for scope, key, value, expected in mutations:
            with self.subTest(scope=scope, key=key, value=value):
                params = copy.deepcopy(s25.load_params())
                params["shadow_opportunity_observer"]["enabled"] = False
                params["shadow_state_tagger"]["enabled"] = False
                if scope == "strategy":
                    params["strategies"][0][key] = value
                else:
                    params[key] = value
                runner = s25.S25V24Runner(params)
                self.assertEqual(runner._configuration_contract_error(), expected)

    def test_current_state_rejects_sync_block_lifecycle_drift(self):
        params = copy.deepcopy(s25.load_params())
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        runner = s25.S25V24Runner(params)
        strategy = runner.params["strategies"][0]
        state = runner._st(strategy)
        mutations = (
            {"sync_block_new_entries": False, "sync_block_reason": "ambiguous_open_result"},
            {"sync_block_new_entries": True, "sync_block_reason": None},
            {
                "sync_block_new_entries": True,
                "sync_block_reason": "positions_unavailable",
                "sync_block_recoverable": True,
                "flat_clear_confirmation_count": 1,
                "flat_clear_confirmation_reason": "orders_unavailable",
            },
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                candidate = copy.deepcopy(runner.state)
                candidate["strategies"][strategy["id"]].update(mutation)
                self.assertEqual(
                    runner._current_state_shape_error(candidate),
                    "sync_block_lifecycle_invalid",
                )

        runner._suppress_manual_alerts = True
        runner._set_sync_block(strategy, "positions_unavailable", recoverable=True)
        state["flat_clear_confirmation_count"] = 1
        state["flat_clear_confirmation_reason"] = "positions_unavailable"
        runner._set_sync_block(strategy, "positions_unavailable", recoverable=False)
        self.assertEqual(state["flat_clear_confirmation_count"], 0)
        self.assertIsNone(state["flat_clear_confirmation_reason"])
        self.assertIsNone(runner._current_state_shape_error(runner.state))

    def test_current_state_rejects_invalid_top_level_save_timestamp(self):
        params = copy.deepcopy(s25.load_params())
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        runner = s25.S25V24Runner(params)
        candidate = copy.deepcopy(runner.state)
        candidate["last_saved_utc"] = "not-a-timestamp"
        self.assertEqual(runner._current_state_shape_error(candidate), "last_saved_utc_invalid")

    def test_current_state_rejects_boolean_direction_values(self):
        params = copy.deepcopy(s25.load_params())
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        runner = s25.S25V24Runner(params)
        strategy_id = runner.params["strategies"][0]["id"]
        active_wave = copy.deepcopy(runner.state)
        active_wave["strategies"][strategy_id]["active_wave"] = True
        self.assertEqual(runner._current_state_shape_error(active_wave), "active_wave_invalid")

        pending_wave = copy.deepcopy(runner.state)
        pending_wave["strategies"][strategy_id]["pending_post_close_action"] = {
            "new_wave": True,
            "reason": "invalid_test_reason",
            "m5_bar": "2026-08-27T00:20:00+00:00",
        }
        self.assertEqual(
            runner._current_state_shape_error(pending_wave),
            "pending_post_close_action_invalid",
        )

        numeric_state = copy.deepcopy(runner.state)
        numeric_state["strategies"][strategy_id]["last_atr"] = True
        self.assertEqual(
            runner._current_state_shape_error(numeric_state),
            "invalid_numeric_state:last_atr",
        )

        position_state = copy.deepcopy(runner.state)
        strategy = runner.params["strategies"][0]
        live = SimpleNamespace(
            ticket=101, identifier=1001, symbol="XAUUSD", type=s25.ORDER_TYPE_BUY,
            volume=0.01, open_price=4000.0, magic=s25.EXPECTED_S25_MAGIC,
            open_time=1787788800, open_time_msc=1787788800000,
            comment="s25_m231_L0101",
        )
        position = runner._state_position_from_live(strategy, live)
        position["entry_price"] = True
        position_state["strategies"][strategy_id]["positions"] = [position]
        self.assertEqual(
            runner._current_state_shape_error(position_state),
            "position_numeric_field_invalid",
        )

    def test_current_state_requires_complete_episode_inventory_identity(self):
        params = copy.deepcopy(s25.load_params())
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        runner = s25.S25V24Runner(params)
        strategy = runner.params["strategies"][0]
        strategy_id = strategy["id"]

        orphaned = copy.deepcopy(runner.state)
        live = SimpleNamespace(
            ticket=111, identifier=1111, symbol="XAUUSD", type=s25.ORDER_TYPE_BUY,
            volume=0.01, open_price=4000.0, magic=s25.EXPECTED_S25_MAGIC,
            open_time=1787788800, open_time_msc=1787788800000,
            comment="s25_m231_L0111",
        )
        orphaned["strategies"][strategy_id]["positions"] = [
            runner._state_position_from_live(strategy, live)
        ]
        self.assertEqual(
            runner._current_state_shape_error(orphaned),
            "position_without_episode_identity",
        )

        incomplete = copy.deepcopy(runner.state)
        current = incomplete["strategies"][strategy_id]
        current["episode_sequence"] = 1
        current["current_episode_id"] = "s25_v24_e000001"
        current["episode_start_quote_utc"] = "2026-08-27T00:00:00+00:00"
        current["last_long_frontier"] = 4000.0
        self.assertEqual(
            runner._current_state_shape_error(incomplete),
            "episode_frontier_identity_incomplete",
        )

        current["last_short_frontier"] = 4000.0
        self.assertIsNone(runner._current_state_shape_error(incomplete))

    def test_current_state_rejects_missing_durable_safety_fields(self):
        params = copy.deepcopy(s25.load_params())
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        runner = s25.S25V24Runner(params)
        strategy = runner.params["strategies"][0]
        strategy_id = strategy["id"]

        missing_block = copy.deepcopy(runner.state)
        del missing_block["strategies"][strategy_id]["sync_block_new_entries"]
        self.assertEqual(
            runner._current_state_shape_error(missing_block),
            "strategy_state_fields_missing",
        )

        missing_submit_marker = copy.deepcopy(runner.state)
        current = missing_submit_marker["strategies"][strategy_id]
        current["episode_sequence"] = 1
        current["current_episode_id"] = "s25_v24_e000001"
        current["episode_start_quote_utc"] = "2026-08-27T00:00:00+00:00"
        current["last_long_frontier"] = 4000.0
        current["last_short_frontier"] = 4000.0
        live = SimpleNamespace(
            ticket=112, identifier=1112, symbol="XAUUSD", type=s25.ORDER_TYPE_BUY,
            volume=0.01, open_price=4000.0, magic=s25.EXPECTED_S25_MAGIC,
            open_time=1787788800, open_time_msc=1787788800000,
            comment="s25_m231_L0112",
        )
        position = runner._state_position_from_live(strategy, live)
        del position["close_submission_started_utc"]
        current["positions"] = [position]
        self.assertEqual(
            runner._current_state_shape_error(missing_submit_marker),
            "position_fields_missing",
        )

        missing_pending_counter = copy.deepcopy(runner.state)
        pending_state = missing_pending_counter["strategies"][strategy_id]
        pending_state["episode_sequence"] = 1
        pending_state["current_episode_id"] = "s25_v24_e000001"
        pending_state["episode_start_quote_utc"] = "2026-08-27T00:00:00+00:00"
        pending_state["last_long_frontier"] = 4000.0
        pending_state["last_short_frontier"] = 4000.0
        pending_state["active_wave"] = 1
        pending_state["last_processed_m5_bar"] = "2026-08-27T00:20:00+00:00"
        pending_state["pending_open"] = {
            "side": "LONG", "lot": 0.01, "comment": "s25_m231_L0113",
            "reason": "long_frontier_add",
            "opportunity_id": "m231_m5_20260827T002000Z",
            "quote_time_utc": "2026-08-27T00:25:00+00:00",
            "decision_time": "2026-08-27T00:25:00+00:00",
            "signal_bar_time": "2026-08-27T00:20:00+00:00",
            "known_position_ids": [],
        }
        self.assertEqual(
            runner._current_state_shape_error(missing_pending_counter),
            "pending_open_fields_missing",
        )

        complete = runner._default_state()
        for field in tuple(complete):
            with self.subTest(missing_top_level=field):
                candidate = copy.deepcopy(complete)
                del candidate[field]
                self.assertIsNotNone(runner._current_state_shape_error(candidate))
        complete_strategy = complete["strategies"][strategy_id]
        for field in tuple(complete_strategy):
            with self.subTest(missing_strategy_field=field):
                candidate = copy.deepcopy(complete)
                del candidate["strategies"][strategy_id][field]
                self.assertIsNotNone(runner._current_state_shape_error(candidate))

    def test_incomplete_current_state_load_fails_closed_without_overwrite(self):
        params = copy.deepcopy(s25.load_params())
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        strategy_id = params["strategies"][0]["id"]
        with tempfile.TemporaryDirectory(prefix="s25-incomplete-current-") as temp:
            state_path = str(Path(temp) / "state.json")
            with mock.patch.object(s25, "STATE_FILE", state_path):
                seed = s25.S25V24Runner(params)
                candidate = copy.deepcopy(seed.state)
                del candidate["strategies"][strategy_id]["sync_block_new_entries"]
                s25.atomic_write_json(state_path, candidate)
                before = Path(state_path).read_bytes()
                loaded = s25.S25V24Runner(params)
                self.assertEqual(loaded._state_identity_status, "foreign_or_invalid")
                self.assertTrue(loaded._st(params["strategies"][0])["sync_block_new_entries"])
                self.assertEqual(Path(state_path).read_bytes(), before)

    def test_current_state_validates_pending_wave_handoff_identity(self):
        with tempfile.TemporaryDirectory(prefix="s25-wave-state-") as temp:
            runner, strategy, _executor, position = self._runner_with_position(temp)
            state = runner._st(strategy)
            state["active_wave"] = 1
            state["last_processed_m5_bar"] = "2026-08-27T00:20:00+00:00"
            position["close_requested"] = True
            position["close_submission_started_utc"] = "2026-08-27T00:25:00+00:00"
            state["pending_close_reason"] = "opposite_pivot_break"
            state["pending_close_m5_bar"] = "2026-08-27T00:20:00+00:00"
            state["pending_close_requested_at_utc"] = "2026-08-27T00:25:00+00:00"
            state["pending_post_close_action"] = {
                "new_wave": -1,
                "reason": "opposite_pivot_break",
                "m5_bar": "2026-08-27T00:20:00+00:00",
            }
            self.assertIsNone(runner._current_state_shape_error(runner.state))

            drift = copy.deepcopy(runner.state)
            drift["strategies"][strategy["id"]]["pending_post_close_action"]["m5_bar"] = "2026-08-27T00:15:00+00:00"
            self.assertEqual(
                runner._current_state_shape_error(drift),
                "pending_post_close_action_lifecycle_invalid",
            )

            completed = copy.deepcopy(runner.state)
            completed_state = completed["strategies"][strategy["id"]]
            completed_state["positions"] = []
            completed_state["pending_close_reason"] = None
            completed_state["pending_close_m5_bar"] = None
            completed_state["pending_close_requested_at_utc"] = None
            self.assertIsNone(runner._current_state_shape_error(completed))

    def test_broker_quote_clock_rejects_stale_future_and_regression(self):
        params = copy.deepcopy(s25.load_params())
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        runner = s25.S25V24Runner(params)
        strategy = params["strategies"][0]
        now = pd.Timestamp("2026-09-04T00:00:20Z").to_pydatetime()
        with mock.patch.object(s25, "utc_now", return_value=now):
            self.assertTrue(runner._quote_clock_error(pd.Timestamp("2026-09-04T00:00:00Z")).startswith("broker_quote_stale"))
            self.assertTrue(runner._quote_clock_error(pd.Timestamp("2026-09-04T00:00:23Z")).startswith("broker_quote_from_future"))
            runner._st(strategy)["last_quote_utc"] = "2026-09-04T00:00:19+00:00"
            self.assertTrue(runner._quote_clock_error(pd.Timestamp("2026-09-04T00:00:18Z"), strategy).startswith("broker_quote_regressed"))

    def test_mql_bridge_contract_is_bot25_scoped_and_request_correlated(self):
        source = (Path(__file__).with_name("BotBridge_s25.mq5")).read_text(encoding="utf-8-sig")
        self.assertIn('#define BRIDGE_VERSION "2026-09-05-s25-v24-atomic-v10"', source)
        self.assertIn('IsCanonicalS25CommentForType(comment, order_type)', source)
        self.assertIn('IsCanonicalS25CommentForType(expected_comment, expected_type)', source)
        self.assertIn('(int)PositionGetInteger(POSITION_TYPE) == order_type', source)
        self.assertIn('MathAbs(PositionGetDouble(POSITION_VOLUME) - volume)', source)
        self.assertIn('input string InpClaimFile = "claim_s25.txt";', source)
        self.assertIn('"RES|RID|" + request_id', source)
        self.assertIn('magic == 200025', source)
        self.assertIn('expected_owned_positions', source)
        self.assertIn('expected_identifier', source)
        self.assertIn('OwnedOrdersFlat(symbol, magic)', source)
        self.assertIn('ERR|MUTATION_RESULT_UNRESOLVED', source)
        self.assertIn('StringFind(command, "CLOSE|") == 0', source)
        self.assertIn('entry != DEAL_ENTRY_IN && entry != DEAL_ENTRY_OUT', source)
        advertised = source.split('#define BRIDGE_COMMANDS "', 1)[1].split('"', 1)[0]
        for disabled in ("PENDING", "MODIFY", "CANCEL", "HISTPAGE", "TICKS"):
            self.assertNotIn(disabled, advertised.split(","))
        for removed_call in ("trade.BuyStop", "trade.SellStop", "trade.PositionModify", "trade.OrderDelete"):
            self.assertNotIn(removed_call, source)

    def test_file_ipc_request_and_response_envelope_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="s25-ipc-") as temp:
            bridge = ea_bridge.EABridgeServer(bot_suffix="s25", files_dir=temp)

            def responder():
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and not os.path.exists(bridge.cmd_file):
                    time.sleep(0.01)
                with open(bridge.cmd_file, "r", encoding="utf-8") as handle:
                    request = handle.read()
                parts = request.split("|", 3)
                self.assertEqual(parts[0], "REQ")
                os.remove(bridge.cmd_file)
                with open(bridge.res_file, "w", encoding="utf-8") as handle:
                    handle.write(f"RES|RID|{parts[1]}|OK|Alive|ENDRES")

            worker = threading.Thread(target=responder)
            worker.start()
            self.assertEqual(bridge.send_command("ECHO|", timeout=2), "OK|Alive")
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())

    def test_executor_open_and_close_are_atomic_and_strict(self):
        executor = live_executor.MT5Executor()
        responses = iter((
            "OK|101|5101|701|4020.1800000000|1788480000000|10009",
            "OK|101|0.01|4020.1800000000|4021.0000000000|0.82|702|10009",
        ))
        commands: list[str] = []

        def send(command, **_kwargs):
            commands.append(command)
            return next(responses)

        with mock.patch.object(live_executor.ea_bridge, "send_command", side_effect=send):
            ticket = executor.open_position(
                "XAUUSD", s25.ORDER_TYPE_BUY, 0.01, 0.0, 0.0,
                deviation=50, magic=s25.EXPECTED_S25_MAGIC, comment="s25_m231_L0001",
                digits=3, expected_login=123, expected_server="server", expected_owned_positions=0,
            )
            result = executor.close_position(
                101, 50, expected_login=123, expected_server="server", expected_symbol="XAUUSD",
                expected_magic=s25.EXPECTED_S25_MAGIC, expected_comment="s25_m231_L0001",
                expected_identifier=5101, expected_type=s25.ORDER_TYPE_BUY, expected_volume=0.01,
            )
        self.assertEqual(ticket, 101)
        self.assertTrue(result)
        self.assertEqual(executor.last_open_identifier, 5101)
        self.assertEqual(executor.last_open_time_msc, 1788480000000)
        self.assertEqual(len(commands[0].split("|")), 12)
        self.assertEqual(len(commands[1].split("|")), 11)

        with mock.patch.object(
            live_executor.ea_bridge,
            "send_command",
            return_value="OK|101|0.02|4020.1800000000|4021.0000000000|0.82|702|10009",
        ):
            mismatched = executor.close_position(
                101, 50, expected_login=123, expected_server="server", expected_symbol="XAUUSD",
                expected_magic=s25.EXPECTED_S25_MAGIC, expected_comment="s25_m231_L0001",
                expected_identifier=5101, expected_type=s25.ORDER_TYPE_BUY, expected_volume=0.01,
            )
        self.assertFalse(mismatched)
        self.assertEqual(mismatched.status, "MALFORMED_OK")

    def test_executor_rejects_comment_side_mismatch_and_boolean_numeric_inputs(self):
        executor = live_executor.MT5Executor()
        with mock.patch.object(live_executor.ea_bridge, "send_command") as send:
            self.assertIsNone(executor.open_position(
                "XAUUSD", s25.ORDER_TYPE_SELL, 0.01, 0.0, 0.0,
                deviation=50, magic=s25.EXPECTED_S25_MAGIC,
                comment="s25_m231_L0001", digits=3, expected_login=123,
                expected_server="server", expected_owned_positions=0,
            ))
            self.assertEqual(executor.last_order_error, "OPEN_POLICY_GUARD")
            self.assertIsNone(executor.open_position(
                "XAUUSD", True, 0.01, 0.0, 0.0,
                deviation=50, magic=s25.EXPECTED_S25_MAGIC,
                comment="s25_m231_S0001", digits=3, expected_login=123,
                expected_server="server", expected_owned_positions=0,
            ))
            close = executor.close_position(
                101, 50, expected_login=123, expected_server="server",
                expected_symbol="XAUUSD", expected_magic=s25.EXPECTED_S25_MAGIC,
                expected_comment="s25_m231_S0001", expected_identifier=5101,
                expected_type=s25.ORDER_TYPE_BUY, expected_volume=0.01,
            )
            boolean_close = executor.close_position(
                True, 50, expected_login=123, expected_server="server",
                expected_symbol="XAUUSD", expected_magic=s25.EXPECTED_S25_MAGIC,
                expected_comment="s25_m231_L0001", expected_identifier=5101,
                expected_type=s25.ORDER_TYPE_BUY, expected_volume=0.01,
            )
        self.assertFalse(close)
        self.assertEqual(close.status, "INVALID_REQUEST")
        self.assertFalse(boolean_close)
        self.assertEqual(boolean_close.status, "INVALID_REQUEST")
        send.assert_not_called()

    def test_record_protocol_rejects_missing_sentinel_and_bad_count(self):
        executor = live_executor.MT5Executor()
        record = "101,XAUUSD,0,0.01,4020.0,0,0,0,200025,1788480000,1788480000000,5101,s25_m231_L0001"
        with mock.patch.object(live_executor.ea_bridge, "send_command", return_value=f"OK|{record}"):
            self.assertIsNone(executor.get_positions("XAUUSD", s25.EXPECTED_S25_MAGIC))
        with mock.patch.object(live_executor.ea_bridge, "send_command", return_value=f"OK|{record}|END,2"):
            self.assertIsNone(executor.get_positions("XAUUSD", s25.EXPECTED_S25_MAGIC))

    def test_history_protocol_requires_complete_ordered_rows(self):
        manager = live_data_fetcher.MT5DataManager()
        good = (
            "OK|2026.09.04 00:00,4000.0,4001.0,3999.0,4000.5,10,1788470400|"
            "2026.09.04 00:05,4000.5,4002.0,4000.0,4001.0,11,1788470700|END,2"
        )
        with mock.patch.object(live_data_fetcher.ea_bridge, "send_command", return_value=good):
            bars = manager.get_historical_data("XAUUSD", 5, 2)
        self.assertIsNotNone(bars)
        self.assertEqual(len(bars), 2)
        with mock.patch.object(live_data_fetcher.ea_bridge, "send_command", return_value=good.rsplit("|", 1)[0]):
            self.assertIsNone(manager.get_historical_data("XAUUSD", 5, 2))
        duplicate = good.replace("1788470700", "1788470400")
        with mock.patch.object(live_data_fetcher.ea_bridge, "send_command", return_value=duplicate):
            self.assertIsNone(manager.get_historical_data("XAUUSD", 5, 2))

    def test_shadow_mode_refuses_broker_positions_missing_from_real_state(self):
        with tempfile.TemporaryDirectory(prefix="s25-shadow-inventory-") as temp:
            params = copy.deepcopy(s25.load_params())
            params["shadow_opportunity_observer"]["enabled"] = False
            params["shadow_state_tagger"]["enabled"] = False
            strategy = params["strategies"][0]
            with (
                mock.patch.object(s25, "STATE_FILE", str(Path(temp) / "state" / "s25_bot_state.json")),
                mock.patch.object(s25, "TRADE_LOG_FILE", str(Path(temp) / "logs" / "s25_trades.csv")),
            ):
                runner = s25.S25V24Runner(params)
                executor = s25.FakeExecutor()
                executor.open_position(
                    "XAUUSD", s25.ORDER_TYPE_BUY, 0.01, magic=s25.EXPECTED_S25_MAGIC,
                    comment="s25_m231_L0001",
                )
                runner.executor = executor
                self.assertFalse(runner._sync_strategy(strategy))
                state = runner._st(strategy)
                self.assertEqual(state["sync_block_reason"], "live_positions_without_real_state")
                self.assertTrue(state["sync_block_new_entries"])

    def test_live_mode_refuses_synthetic_shadow_positions(self):
        with tempfile.TemporaryDirectory(prefix="s25-live-shadow-state-") as temp:
            runner, strategy, executor, position = self._runner_with_position(temp)
            position["shadow"] = True
            executor.get_positions = mock.Mock(
                side_effect=AssertionError("broker query must not precede local state rejection")
            )
            executor.get_orders = mock.Mock(
                side_effect=AssertionError("broker query must not precede local state rejection")
            )
            self.assertFalse(runner._sync_strategy(strategy))
            self.assertEqual(runner._st(strategy)["sync_block_reason"], "shadow_positions_present_in_live_mode")
            executor.get_positions.assert_not_called()
            executor.get_orders.assert_not_called()

    def test_run_strategy_preserves_canonical_state_on_live_shadow_inventory(self):
        with tempfile.TemporaryDirectory(prefix="s25-live-shadow-run-loop-") as temp:
            runner, strategy, executor, position = self._runner_with_position(temp)
            state_path = Path(s25.STATE_FILE)
            before = state_path.read_bytes()
            executor.positions = []
            position["shadow"] = True
            quote_time = pd.Timestamp(executor.info.quote_time_msc, unit="ms", tz="UTC")

            runner._run_strategy(strategy, None, executor.info, quote_time)

            self.assertEqual(
                runner._st(strategy)["sync_block_reason"],
                "shadow_positions_present_in_live_mode",
            )
            self.assertEqual(state_path.read_bytes(), before)

    def test_run_once_failure_branches_preserve_canonical_live_state(self):
        for failure in ("symbol_info", "quote_clock"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(
                prefix=f"s25-live-shadow-{failure}-"
            ) as temp:
                runner, strategy, executor, position = self._runner_with_position(temp)
                state_path = Path(s25.STATE_FILE)
                before = state_path.read_bytes()
                position["shadow"] = True
                if failure == "symbol_info":
                    executor.get_symbol_info = mock.Mock(return_value=None)
                else:
                    executor.positions = []
                    executor.info.quote_time_msc = int(
                        pd.Timestamp("2000-01-01T00:00:00Z").timestamp() * 1000
                    )

                runner.run_once()

                self.assertTrue(runner._st(strategy)["sync_block_new_entries"])
                self.assertEqual(state_path.read_bytes(), before)

    def test_real_position_state_uses_broker_open_time_only(self):
        with tempfile.TemporaryDirectory(prefix="s25-broker-open-time-") as temp:
            runner, strategy, executor, position = self._runner_with_position(temp)
            record = executor.positions[0]
            record.open_time_msc = record.open_time * 1000 + 321
            state_position = runner._state_position_from_live(strategy, record)
            self.assertEqual(state_position["open_time_epoch"], record.open_time)
            self.assertEqual(pd.Timestamp(state_position["entry_time_utc"]), pd.Timestamp(record.open_time_msc, unit="ms", tz="UTC"))
            state_position["open_time_epoch"] -= 1
            self.assertFalse(runner._state_matches_live(strategy, state_position, record))

            state_position = runner._state_position_from_live(strategy, record)
            state_position["entry_price"] += 0.001
            self.assertFalse(runner._state_matches_live(strategy, state_position, record))

    def test_comment_side_must_match_broker_and_state_direction(self):
        with tempfile.TemporaryDirectory(prefix="s25-comment-side-") as temp:
            runner, strategy, executor, _position = self._runner_with_position(temp)
            live = copy.copy(executor.positions[0])
            live.comment = "s25_m231_S0001"
            self.assertFalse(runner._owned_position(strategy, live))

            state_position = runner._state_position_from_live(strategy, executor.positions[0])
            state_position["owner_comment"] = "s25_m231_S0001"
            candidate = copy.deepcopy(runner.state)
            candidate["strategies"][strategy["id"]]["positions"] = [state_position]
            self.assertEqual(
                runner._current_state_shape_error(candidate),
                "position_identity_or_ownership_invalid",
            )

            pending = copy.deepcopy(runner.state)
            current = pending["strategies"][strategy["id"]]
            current["pending_open"] = {
                "side": "LONG", "lot": 0.01, "comment": "s25_m231_S0002",
                "reason": "long_frontier_add",
                "quote_time_utc": "2026-08-27T00:25:00+00:00",
                "decision_time": "2026-08-27T00:25:00+00:00",
                "signal_bar_time": "2026-08-27T00:20:00+00:00",
                "known_position_ids": [int(_position["position_identifier"])],
                "flat_confirmation_count": 0,
                "opportunity_id": "m231_m5_20260827T002000Z",
            }
            self.assertEqual(
                runner._current_state_shape_error(pending),
                "pending_open_values_invalid",
            )

    def test_current_state_rejects_entry_timestamp_epoch_drift(self):
        with tempfile.TemporaryDirectory(prefix="s25-entry-clock-state-") as temp:
            runner, strategy, _executor, position = self._runner_with_position(temp)
            position["entry_time_utc"] = "2026-08-27T00:26:00+00:00"
            self.assertEqual(
                runner._current_state_shape_error(runner.state),
                "position_identity_or_ownership_invalid",
            )

    def _runner_with_position(self, temp: str):
        params = copy.deepcopy(s25.load_params())
        params["live_trading_enabled"] = True
        params["shadow_forward_enabled"] = False
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        strategy = params["strategies"][0]
        gate = {params["real_trading_activation_env"]: params["real_trading_activation_value"]}
        state_path = str(Path(temp) / "state" / "s25_bot_state.json")
        trade_path = str(Path(temp) / "logs" / "s25_trades.csv")
        patches = (
            mock.patch.object(s25, "STATE_FILE", state_path),
            mock.patch.object(s25, "TRADE_LOG_FILE", trade_path),
            mock.patch.dict(os.environ, gate),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        runner = s25.S25V24Runner(params)
        runner._suppress_manual_alerts = True
        executor = s25.FakeExecutor()
        executor.open_position(
            "XAUUSD", s25.ORDER_TYPE_BUY, 0.01, magic=s25.EXPECTED_S25_MAGIC,
            comment="s25_m231_L0001",
        )
        record = executor.positions[0]
        runner.executor = executor
        state = runner._st(strategy)
        state["episode_sequence"] = 1
        state["current_episode_id"] = "s25_m231_e000001"
        state["episode_start_quote_utc"] = "2026-09-04T00:00:00+00:00"
        state["last_long_frontier"] = 4020.09
        state["last_short_frontier"] = 4020.09
        state["positions"] = [runner._state_position_from_live(strategy, record)]
        runner._save_state()
        return runner, strategy, executor, state["positions"][0]

    def _arm_valid_pending_open(
        self, runner, strategy, position, *, quote="2026-09-04T00:25:00Z",
        flat_confirmation_count=0,
    ):
        state = runner._st(strategy)
        quote_ts = pd.Timestamp(quote)
        signal_ts = quote_ts - pd.Timedelta(minutes=5)
        state["last_processed_m5_bar"] = s25.dt_text(signal_ts)
        state["l05_activation_m5_bar"] = s25.dt_text(signal_ts)
        state["active_wave"] = 1
        state["pending_open"] = {
            "side": "LONG", "lot": 0.01, "comment": "s25_m231_L0002",
            "reason": "long_frontier_add",
            "known_position_ids": [position["position_identifier"]],
            "quote_time_utc": s25.dt_text(quote_ts),
            "decision_time": s25.dt_text(quote_ts),
            "signal_bar_time": s25.dt_text(signal_ts),
            "flat_confirmation_count": flat_confirmation_count,
            "opportunity_id": runner._opportunity_id(
                s25.dt_text(signal_ts), quote_ts, "m5",
            ),
        }

    def test_ambiguous_close_is_never_replayed(self):
        with tempfile.TemporaryDirectory(prefix="s25-close-ambiguous-") as temp:
            runner, strategy, executor, position = self._runner_with_position(temp)
            calls = 0

            def ambiguous(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                return s25.CloseResult(False, status="MALFORMED_OK", raw_response="OK|truncated")

            executor.close_position = ambiguous
            quote = pd.Timestamp("2026-09-04T00:10:00Z")
            result = runner._close_positions(strategy, [position], "feed_gap", executor.info, quote, None)
            self.assertEqual(result, "blocked")
            self.assertEqual(calls, 1)
            self.assertIsNotNone(position["close_submission_started_utc"])
            runner._retry_pending_close_requests(strategy, quote + pd.Timedelta(minutes=1))
            self.assertEqual(calls, 1)

    def test_untrusted_close_reason_never_reaches_executor(self):
        with tempfile.TemporaryDirectory(prefix="s25-close-reason-guard-") as temp:
            runner, strategy, executor, position = self._runner_with_position(temp)
            quote = pd.Timestamp(executor.info.quote_time_msc, unit="ms", tz="UTC")
            calls = 0
            original_close = executor.close_position

            def counted_close(*args, **kwargs):
                nonlocal calls
                calls += 1
                return original_close(*args, **kwargs)

            executor.close_position = counted_close
            self.assertEqual(
                runner._close_positions(
                    strategy, [position], "arbitrary_manual_close",
                    executor.info, quote, None,
                ),
                "blocked",
            )
            self.assertEqual(calls, 0)
            self.assertEqual(len(executor.positions), 1)
            self.assertEqual(
                runner._st(strategy)["sync_block_reason"],
                "unsupported_or_noncausal_close_intent",
            )

    def test_wave_handoff_survives_crash_after_close_submission(self):
        with tempfile.TemporaryDirectory(prefix="s25-wave-crash-") as temp:
            runner, strategy, executor, position = self._runner_with_position(temp)
            state = runner._st(strategy)
            state["active_wave"] = 1
            executor.info.bid = position["entry_price"] + 2.0
            executor.info.ask = executor.info.bid + 0.18
            close = executor.close_position
            def crash_after_close(*args, **kwargs):
                close(*args, **kwargs)
                raise OSError("simulated process failure after broker close")
            quote = pd.Timestamp(executor.info.quote_time_msc, unit="ms", tz="UTC")
            signal_bar = quote.floor("5min") - pd.Timedelta(minutes=5)
            state["last_processed_m5_bar"] = s25.dt_text(signal_bar)
            with mock.patch.object(executor, "close_position", side_effect=crash_after_close), self.assertRaises(OSError):
                runner._release_active_side(
                    strategy, -1, "opposite_pivot_break", executor.info, quote,
                    s25.dt_text(signal_bar),
                )
            restarted = s25.S25V24Runner(runner.params)
            restarted.executor = executor
            restarted._suppress_manual_alerts = True
            action = restarted._st(strategy)["pending_post_close_action"]
            self.assertIsNotNone(action)
            self.assertEqual(action["new_wave"], -1)
            self.assertTrue(restarted._sync_strategy(strategy))
            self.assertFalse(restarted._apply_pending_post_close(strategy, executor.info, quote))
            self.assertEqual(restarted._st(strategy)["active_wave"], -1)
            self.assertEqual(executor.positions, [])

    def test_rejected_close_ownership_does_not_arm_wave_handoff(self):
        with tempfile.TemporaryDirectory(prefix="s25-wave-owner-") as temp:
            runner, strategy, executor, position = self._runner_with_position(temp)
            state = runner._st(strategy)
            state["active_wave"] = 1
            executor.info.bid = position["entry_price"] + 2.0
            executor.info.ask = executor.info.bid + 0.18
            executor.positions[0].magic = 999999
            quote = pd.Timestamp(executor.info.quote_time_msc, unit="ms", tz="UTC")
            signal_bar = quote.floor("5min") - pd.Timedelta(minutes=5)
            state["last_processed_m5_bar"] = s25.dt_text(signal_bar)
            with mock.patch.object(executor, "close_position") as close:
                self.assertTrue(runner._release_active_side(
                    strategy, -1, "opposite_pivot_break", executor.info, quote,
                    s25.dt_text(signal_bar),
                ))
            close.assert_not_called()
            self.assertIsNone(state["pending_post_close_action"])
            self.assertEqual(state["active_wave"], 1)
    def test_post_open_checks_foreign_rows_known_drift_and_response_identity(self):
        for anomaly in ("foreign_comment", "known_volume", "response_price", "response_time"):
            with self.subTest(anomaly=anomaly), tempfile.TemporaryDirectory(prefix="s25-open-inventory-") as temp:
                runner, strategy, executor, _position = self._runner_with_position(temp)
                original = executor.open_position
                def altered(*args, **kwargs):
                    ticket = original(*args, **kwargs)
                    if anomaly == "known_volume":
                        executor.positions[0].volume = 0.02
                    elif anomaly == "foreign_comment":
                        foreign = copy.copy(executor.positions[-1])
                        foreign.ticket += 10000
                        foreign.identifier += 10000
                        foreign.comment = "foreign_manual"
                        executor.positions.append(foreign)
                    elif anomaly == "response_price":
                        executor.last_open_price += 0.001
                    else:
                        executor.last_open_time_msc += 1
                    return ticket
                quote = pd.Timestamp(executor.info.quote_time_msc, unit="ms", tz="UTC")
                signal_bar = quote.floor("5min") - pd.Timedelta(minutes=5)
                runner._st(strategy)["last_processed_m5_bar"] = s25.dt_text(signal_bar)
                runner._st(strategy)["active_wave"] = 1
                with mock.patch.object(runner, "_quote_clock_error", return_value=None), mock.patch.object(executor, "open_position", side_effect=altered):
                    self.assertFalse(runner._open_position(
                        strategy, "LONG", executor.info, quote,
                        "long_frontier_add", s25.dt_text(signal_bar),
                        runner._opportunity_id(s25.dt_text(signal_bar), quote, "m5"),
                    ))
                self.assertTrue(runner._st(strategy)["sync_block_new_entries"])
                self.assertIsNotNone(runner._st(strategy)["pending_open"])
                self.assertEqual(len(runner._st(strategy)["positions"]), 1)

    def test_pending_adoption_requires_no_unexplained_extra_position(self):
        with tempfile.TemporaryDirectory(prefix="s25-recovery-extra-") as temp:
            runner, strategy, executor, position = self._runner_with_position(temp)
            state = runner._st(strategy)
            self._arm_valid_pending_open(
                runner, strategy, position, quote="2026-08-27T00:25:00Z",
            )
            executor.open_position("XAUUSD", s25.ORDER_TYPE_BUY, 0.01, magic=s25.EXPECTED_S25_MAGIC, comment="s25_m231_L0002")
            executor.open_position("XAUUSD", s25.ORDER_TYPE_SELL, 0.01, magic=s25.EXPECTED_S25_MAGIC, comment="s25_m231_S9999")
            self.assertFalse(runner._sync_strategy(strategy))
            self.assertIsNotNone(state["pending_open"])
            self.assertEqual(len(state["positions"]), 1)

    def test_pending_adoption_rejects_matching_position_outside_reservation_window(self):
        for opened_at in ("2026-08-27T00:15:00Z", "2026-08-27T00:26:01Z"):
            with self.subTest(opened_at=opened_at), tempfile.TemporaryDirectory(prefix="s25-recovery-time-window-") as temp:
                runner, strategy, executor, position = self._runner_with_position(temp)
                state = runner._st(strategy)
                state["pending_open"] = {
                    "side": "LONG", "lot": 0.01, "comment": "s25_m231_L0002",
                    "known_position_ids": [position["position_identifier"]],
                    "quote_time_utc": "2026-08-27T00:25:00Z",
                    "decision_time": "2026-08-27T00:25:00Z",
                    "signal_bar_time": "2026-08-27T00:20:00Z",
                    "flat_confirmation_count": 0,
                    "opportunity_id": "m231_m5_20260827T002000Z",
                    "reason": "long_frontier_add",
                }
                state["last_processed_m5_bar"] = "2026-08-27T00:20:00+00:00"
                state["active_wave"] = 1
                executor.open_position(
                    "XAUUSD", s25.ORDER_TYPE_BUY, 0.01,
                    magic=s25.EXPECTED_S25_MAGIC, comment="s25_m231_L0002",
                )
                executor.positions[-1].open_time = int(pd.Timestamp(opened_at).timestamp())
                executor.positions[-1].open_time_msc = executor.positions[-1].open_time * 1000
                self.assertFalse(runner._sync_strategy(strategy))
                self.assertEqual(len(state["positions"]), 1)
                self.assertIsNotNone(state["pending_open"])

    def test_pending_open_requires_consecutive_complete_clean_queries(self):
        for failed_query in ("get_positions", "get_orders"):
            with self.subTest(query=failed_query), tempfile.TemporaryDirectory(prefix="s25-pending-clean-") as temp:
                runner, strategy, executor, position = self._runner_with_position(temp)
                state = runner._st(strategy)
                self._arm_valid_pending_open(
                    runner, strategy, position, quote="2026-08-27T00:25:00Z",
                )
                self.assertFalse(runner._sync_strategy(strategy))
                self.assertEqual(state["pending_open"]["flat_confirmation_count"], 1)
                with mock.patch.object(executor, failed_query, return_value=None):
                    runner._sync_strategy(strategy)
                self.assertIsNotNone(state["pending_open"])
                self.assertEqual(state["pending_open"]["flat_confirmation_count"], 0)
                self.assertFalse(runner._sync_strategy(strategy))
                self.assertIsNotNone(state["pending_open"])
                self.assertTrue(runner._sync_strategy(strategy))
                self.assertIsNone(state["pending_open"])

    def test_current_state_rejects_malformed_pending_open_recovery_identity(self):
        params = copy.deepcopy(s25.load_params())
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        runner = s25.S25V24Runner(params)
        strategy = runner.params["strategies"][0]
        state = runner._st(strategy)
        state["pending_open"] = {
            "side": "LONG",
            "lot": 0.01,
            "comment": "s25_m231_L0001",
            "reason": "long_frontier_add",
            "quote_time_utc": "2026-08-27T00:25:00+00:00",
            "decision_time": "2026-08-27T00:25:00+00:00",
            "signal_bar_time": "2026-08-27T00:20:00+00:00",
            "known_position_ids": [-1],
            "flat_confirmation_count": -1,
            "opportunity_id": "m231_m5_20260827T002000Z",
        }
        self.assertEqual(
            runner._current_state_shape_error(runner.state),
            "pending_open_values_invalid",
        )

    def test_current_state_accepts_exact_pending_open_identity_and_rejects_each_drift(self):
        params = copy.deepcopy(s25.load_params())
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        runner = s25.S25V24Runner(params)
        strategy = runner.params["strategies"][0]
        state = runner._st(strategy)
        state["episode_sequence"] = 1
        state["current_episode_id"] = "s25_v24_e000001"
        state["episode_start_quote_utc"] = "2026-08-27T00:00:00+00:00"
        state["last_long_frontier"] = 4000.0
        state["last_short_frontier"] = 4000.0
        state["active_wave"] = 1
        state["last_processed_m5_bar"] = "2026-08-27T00:20:00+00:00"
        valid_pending = {
            "side": "LONG",
            "lot": 0.01,
            "comment": "s25_m231_L0001",
            "reason": "long_frontier_add",
            "quote_time_utc": "2026-08-27T00:25:00+00:00",
            "decision_time": "2026-08-27T00:25:00+00:00",
            "signal_bar_time": "2026-08-27T00:20:00+00:00",
            "known_position_ids": [],
            "flat_confirmation_count": 0,
            "opportunity_id": "m231_m5_20260827T002000Z",
        }
        state["pending_open"] = copy.deepcopy(valid_pending)
        self.assertIsNone(runner._current_state_shape_error(runner.state))
        mutations = {
            "decision_time": {"decision_time": "2026-08-27T00:26:00+00:00"},
            "reason": {"reason": "short_frontier_add"},
            "opportunity_id": {"opportunity_id": "m231_m5_wrong"},
            "signal_too_old": {"signal_bar_time": "2026-08-27T00:10:00+00:00", "opportunity_id": "m231_m5_20260827T001000Z"},
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                state["pending_open"] = {**copy.deepcopy(valid_pending), **mutation}
                self.assertEqual(
                    runner._current_state_shape_error(runner.state),
                    "pending_open_causal_identity_invalid",
                )

        causal_mutations = {
            "episode": {"current_episode_id": None, "episode_start_quote_utc": None},
            "wave": {"active_wave": -1},
            "processed_bar": {"last_processed_m5_bar": "2026-08-27T00:15:00+00:00"},
        }
        for name, mutation in causal_mutations.items():
            with self.subTest(causal=name):
                candidate = copy.deepcopy(runner.state)
                candidate["strategies"][strategy["id"]].update(mutation)
                self.assertEqual(
                    runner._current_state_shape_error(candidate),
                    "pending_open_causal_identity_invalid",
                )

    def test_current_state_rejects_open_and_close_lifecycle_conflict(self):
        params = copy.deepcopy(s25.load_params())
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        runner = s25.S25V24Runner(params)
        strategy = runner.params["strategies"][0]
        state = runner._st(strategy)
        state.update({
            "episode_sequence": 1,
            "current_episode_id": "s25_v24_e000001",
            "episode_start_quote_utc": "2026-08-27T00:00:00+00:00",
            "last_long_frontier": 4000.0,
            "last_short_frontier": 4000.0,
            "active_wave": 1,
            "last_processed_m5_bar": "2026-08-27T00:20:00+00:00",
            "pending_open": {
                "side": "LONG", "lot": 0.01, "comment": "s25_m231_L0001",
                "reason": "long_frontier_add",
                "quote_time_utc": "2026-08-27T00:25:00+00:00",
                "decision_time": "2026-08-27T00:25:00+00:00",
                "signal_bar_time": "2026-08-27T00:20:00+00:00",
                "known_position_ids": [], "flat_confirmation_count": 0,
                "opportunity_id": "m231_m5_20260827T002000Z",
            },
            "close_defer": {
                "reason": "episode_12h", "armed_at_utc": "2026-08-27T00:25:00+00:00",
                "first_wide_quote_utc": None, "last_evaluated_quote_utc": None,
                "stable_quote_count": 0, "next_retry_utc": None,
            },
        })
        self.assertEqual(
            runner._current_state_shape_error(runner.state),
            "open_close_lifecycle_conflict",
        )

    def test_current_state_rejects_malformed_pending_close_accounting(self):
        params = copy.deepcopy(s25.load_params())
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        runner = s25.S25V24Runner(params)
        strategy = runner.params["strategies"][0]
        state = runner._st(strategy)
        state["pending_productive_close"] = {
            "reason": "opposite_pivot_break",
            "position_ids": [101],
            "confirmed_ids": [202],
            "strategy_profit_usd": float("nan"),
            "last_deal_utc": "not-a-time",
        }
        self.assertEqual(
            runner._current_state_shape_error(runner.state),
            "pending_productive_close_invalid",
        )

    def test_current_state_rejects_orphaned_pending_close_lifecycle(self):
        params = copy.deepcopy(s25.load_params())
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        runner = s25.S25V24Runner(params)
        strategy = runner.params["strategies"][0]
        state = runner._st(strategy)
        state["pending_close_reason"] = "loss_policy_L05"
        state["pending_close_requested_at_utc"] = "2026-08-27T00:25:00+00:00"
        self.assertEqual(
            runner._current_state_shape_error(runner.state),
            "pending_close_lifecycle_invalid",
        )

    def test_current_state_rejects_untrusted_or_noncausal_pending_close(self):
        with tempfile.TemporaryDirectory(prefix="s25-close-state-identity-") as temp:
            runner, strategy, _executor, position = self._runner_with_position(temp)
            state = runner._st(strategy)
            position["close_requested"] = True
            state["pending_close_reason"] = "arbitrary_manual_close"
            state["pending_close_requested_at_utc"] = "2026-09-04T00:25:00+00:00"
            self.assertEqual(
                runner._current_state_shape_error(runner.state),
                "pending_close_reason_invalid",
            )

            state["pending_close_reason"] = "loss_policy_L05"
            self.assertEqual(
                runner._current_state_shape_error(runner.state),
                "pending_close_m5_identity_invalid",
            )

            state["last_processed_m5_bar"] = "2026-09-04T00:20:00+00:00"
            state["pending_close_m5_bar"] = "2026-09-04T00:20:00+00:00"
            self.assertIsNone(runner._current_state_shape_error(runner.state))

            position["close_submission_started_utc"] = "2026-09-04T00:24:59+00:00"
            self.assertEqual(
                runner._current_state_shape_error(runner.state),
                "pending_close_time_order_invalid",
            )
            position["close_submission_started_utc"] = None
            state["close_retry_after_utc"] = "2026-09-04T00:24:59+00:00"
            self.assertEqual(
                runner._current_state_shape_error(runner.state),
                "pending_close_time_order_invalid",
            )
            state["close_retry_after_utc"] = None

            state["pending_close_requested_at_utc"] = "2026-09-04T00:40:00+00:00"
            self.assertEqual(
                runner._current_state_shape_error(runner.state),
                "pending_close_m5_identity_invalid",
            )

    def test_current_state_rejects_untrusted_or_noncausal_full_close_defer(self):
        with tempfile.TemporaryDirectory(prefix="s25-close-defer-identity-") as temp:
            runner, strategy, _executor, _position = self._runner_with_position(temp)
            state = runner._st(strategy)
            valid_defer = {
                "reason": "episode_12h",
                "armed_at_utc": "2026-09-04T00:20:00+00:00",
                "first_wide_quote_utc": "2026-09-04T00:21:00+00:00",
                "last_evaluated_quote_utc": "2026-09-04T00:22:00+00:00",
                "stable_quote_count": 1,
                "next_retry_utc": None,
            }
            state["close_defer"] = copy.deepcopy(valid_defer)
            self.assertIsNone(runner._current_state_shape_error(runner.state))

            state["close_defer"]["reason"] = "arbitrary_manual_close"
            self.assertEqual(
                runner._current_state_shape_error(runner.state),
                "close_defer_reason_invalid",
            )

            state["close_defer"] = copy.deepcopy(valid_defer)
            state["close_defer"]["first_wide_quote_utc"] = None
            self.assertEqual(
                runner._current_state_shape_error(runner.state),
                "close_defer_lifecycle_invalid",
            )

            state["close_defer"] = copy.deepcopy(valid_defer)
            state["close_defer"]["last_evaluated_quote_utc"] = "2026-09-04T00:20:30+00:00"
            self.assertEqual(
                runner._current_state_shape_error(runner.state),
                "close_defer_lifecycle_invalid",
            )

    def test_invalid_runtime_state_is_never_persisted(self):
        with tempfile.TemporaryDirectory(prefix="s25-invalid-save-guard-") as temp:
            runner, strategy, _executor, _position = self._runner_with_position(temp)
            state_path = Path(s25.STATE_FILE)
            before = state_path.read_bytes()
            runner._st(strategy)["pending_close_reason"] = "arbitrary_manual_close"
            with self.assertRaisesRegex(RuntimeError, "refusing invalid current state save"):
                runner._save_state()
            self.assertEqual(state_path.read_bytes(), before)

    def test_current_state_rejects_unknown_schema_fields(self):
        with tempfile.TemporaryDirectory(prefix="s25-unknown-state-field-") as temp:
            runner, strategy, _executor, position = self._runner_with_position(temp)
            mutations = (
                (runner.state, "retired_top_level", "top_level_state_fields_unknown"),
                (runner._st(strategy), "retired_strategy_field", "strategy_state_fields_unknown"),
                (position, "retired_position_field", "position_fields_unknown"),
            )
            for target, field, expected in mutations:
                with self.subTest(field=field):
                    target[field] = "stale"
                    self.assertEqual(runner._current_state_shape_error(runner.state), expected)
                    target.pop(field)

    def test_current_state_rejects_logical_position_limit_drift(self):
        with tempfile.TemporaryDirectory(prefix="s25-state-position-limit-") as temp:
            runner, strategy, _executor, position = self._runner_with_position(temp)
            state = runner._st(strategy)
            for sequence in range(2, 7):
                extra = copy.deepcopy(position)
                extra.update({
                    "ticket": 1000 + sequence,
                    "position_identifier": 2000 + sequence,
                    "owner_comment": f"s25_m231_L{sequence:04d}",
                    "shadow": False,
                })
                state["positions"].append(extra)
            self.assertEqual(
                runner._current_state_shape_error(runner.state),
                "logical_position_cap_exceeded",
            )

    def test_live_state_validator_rejects_shadow_inventory(self):
        with tempfile.TemporaryDirectory(prefix="s25-live-shadow-state-") as temp:
            runner, strategy, _executor, position = self._runner_with_position(temp)
            position["ticket"] = -1
            position["position_identifier"] = -1
            position["shadow"] = True
            self.assertTrue(runner.live_enabled)
            self.assertEqual(
                runner._current_state_shape_error(runner.state),
                "shadow_position_in_live_state",
            )
            with self.assertRaisesRegex(RuntimeError, "shadow_position_in_live_state"):
                runner._save_state()

    def test_invalid_transaction_state_rolls_back_without_persisting(self):
        with tempfile.TemporaryDirectory(prefix="s25-invalid-transaction-guard-") as temp:
            runner, strategy, _executor, _position = self._runner_with_position(temp)
            state_path = Path(s25.STATE_FILE)
            before = state_path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "refusing invalid transactional state save"):
                with runner._close_state_transaction():
                    runner._st(strategy)["pending_close_reason"] = "arbitrary_manual_close"
            self.assertEqual(state_path.read_bytes(), before)
            self.assertIsNone(runner._st(strategy)["pending_close_reason"])

    def test_current_state_rejects_productive_targets_without_matching_close_requests(self):
        params = copy.deepcopy(s25.load_params())
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        runner = s25.S25V24Runner(params)
        strategy = runner.params["strategies"][0]
        state = runner._st(strategy)
        live = SimpleNamespace(
            ticket=101, identifier=1001, symbol="XAUUSD", type=s25.ORDER_TYPE_BUY,
            volume=0.01, open_price=4000.0, magic=s25.EXPECTED_S25_MAGIC,
            open_time=1787788800, open_time_msc=1787788800000,
            comment="s25_m231_L0101",
        )
        state["positions"] = [runner._state_position_from_live(strategy, live)]
        state["episode_sequence"] = 1
        state["current_episode_id"] = "s25_v24_e000001"
        state["episode_start_quote_utc"] = "2026-08-27T00:00:00+00:00"
        state["last_long_frontier"] = 4000.0
        state["last_short_frontier"] = 4000.0
        state["pending_close_reason"] = "opposite_pivot_break"
        state["pending_close_requested_at_utc"] = "2026-08-27T00:25:00+00:00"
        state["pending_productive_close"] = {
            "reason": "opposite_pivot_break",
            "position_ids": [1001],
            "confirmed_ids": [],
            "strategy_profit_usd": 0.0,
            "last_deal_utc": None,
        }
        self.assertEqual(
            runner._current_state_shape_error(runner.state),
            "pending_productive_close_lifecycle_invalid",
        )

    def test_current_state_rejects_episode_sequence_identity_drift(self):
        params = copy.deepcopy(s25.load_params())
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        runner = s25.S25V24Runner(params)
        strategy = runner.params["strategies"][0]
        state = runner._st(strategy)
        state["episode_sequence"] = 2
        state["current_episode_id"] = "s25_v24_e000003"
        state["episode_start_quote_utc"] = "2026-08-27T00:25:00+00:00"
        self.assertEqual(
            runner._current_state_shape_error(runner.state),
            "episode_sequence_identity_mismatch",
        )

    def test_open_skips_an_existing_comment_when_sequence_lags(self):
        params = copy.deepcopy(s25.load_params())
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        runner = s25.S25V24Runner(params)
        strategy = runner.params["strategies"][0]
        state = runner._st(strategy)
        live = SimpleNamespace(
            ticket=101, identifier=1001, symbol="XAUUSD", type=s25.ORDER_TYPE_BUY,
            volume=0.01, open_price=4000.0, magic=s25.EXPECTED_S25_MAGIC,
            open_time=1787788800, open_time_msc=1787788800000,
            comment="s25_m231_L0002",
        )
        existing = runner._state_position_from_live(strategy, live)
        existing["shadow"] = True
        existing["ticket"] = -2
        existing["position_identifier"] = -2
        state["positions"] = [existing]
        state["shadow_sequence"] = 1
        state["episode_sequence"] = 1
        state["current_episode_id"] = "s25_v24_e000001"
        state["episode_start_quote_utc"] = "2026-08-27T00:00:00+00:00"
        runner._save_state = mock.Mock()
        runner._trade_row = mock.Mock()
        self.assertTrue(
            runner._open_position(
                strategy, "LONG",
                SimpleNamespace(bid=4000.0, ask=4000.1, point=0.001),
                pd.Timestamp("2026-08-27T00:25:00Z"), "long_frontier_add",
                "2026-08-27T00:20:00Z", "comment_collision_test",
            )
        )
        self.assertEqual(state["positions"][-1]["owner_comment"], "s25_m231_L0003")

    def test_pending_recovery_transaction_is_retryable_and_deduplicated(self):
        for recovery in ("adopt", "flat"):
            for fault in ("ledger", "before_commit", "after_commit"):
                with self.subTest(recovery=recovery, fault=fault), tempfile.TemporaryDirectory(prefix="s25-recovery-commit-") as temp:
                    runner, strategy, executor, position = self._runner_with_position(temp)
                    state = runner._st(strategy)
                    self._arm_valid_pending_open(
                        runner, strategy, position,
                        quote="2026-08-27T00:25:00Z", flat_confirmation_count=1,
                    )
                    if recovery == "adopt":
                        executor.open_position("XAUUSD", s25.ORDER_TYPE_BUY, 0.01, magic=s25.EXPECTED_S25_MAGIC, comment="s25_m231_L0002")
                    runner._save_state()
                    before = copy.deepcopy(runner.state)
                    actual_write = s25.atomic_write_json
                    def fail_write(path, payload):
                        if fault == "after_commit":
                            actual_write(path, payload)
                        raise OSError("injected recovery commit failure")
                    patcher = mock.patch.object(s25, "append_csv", side_effect=OSError("recovery ledger failure")) if fault == "ledger" else mock.patch.object(s25, "atomic_write_json", side_effect=fail_write)
                    with patcher, self.assertRaises(OSError):
                        runner._sync_strategy(strategy)
                    self.assertIs(runner._st(strategy), state)
                    if fault != "after_commit":
                        self.assertEqual(runner.state, before)
                    self.assertTrue(runner._sync_strategy(strategy))
                    self.assertIsNone(state["pending_open"])
                    self.assertEqual(len(state["positions"]), 2 if recovery == "adopt" else 1)
                    event = "entry_recovered_after_restart" if recovery == "adopt" else "ambiguous_open_resolved_flat"
                    with Path(s25.TRADE_LOG_FILE).open(newline="", encoding="utf-8") as handle:
                        rows = list(csv.DictReader(handle))
                    self.assertEqual(sum(row["event"] == event for row in rows), 1)
                    restarted = s25.S25V24Runner(runner.params)
                    self.assertIsNone(restarted._st(strategy)["pending_open"])
                    self.assertEqual(len(restarted._st(strategy)["positions"]), 2 if recovery == "adopt" else 1)

    def test_quote_expiring_during_reservation_never_submits_open(self):
        with tempfile.TemporaryDirectory(prefix="s25-submit-clock-") as temp:
            runner, strategy, executor, _position = self._runner_with_position(temp)
            quote = pd.Timestamp(executor.info.quote_time_msc, unit="ms", tz="UTC")
            signal_bar = quote.floor("5min") - pd.Timedelta(minutes=5)
            runner._st(strategy)["last_processed_m5_bar"] = s25.dt_text(signal_bar)
            runner._st(strategy)["active_wave"] = 1
            fresh = quote.to_pydatetime()
            expired = (quote + pd.Timedelta(seconds=60)).to_pydatetime()
            with mock.patch.object(s25, "utc_now", return_value=fresh):
                self.assertIsNone(runner._quote_clock_error(quote, strategy))
            original_row = runner._trade_row
            def reserve(event, *args, **kwargs):
                original_row(event, *args, **kwargs)
                if event == "open_reserved":
                    clock.return_value = expired
            with mock.patch.object(s25, "utc_now", return_value=fresh) as clock, mock.patch.object(runner, "_trade_row", side_effect=reserve), mock.patch.object(executor, "open_position") as submit:
                self.assertFalse(runner._open_position(
                    strategy, "LONG", executor.info, quote, "long_frontier_add",
                    s25.dt_text(signal_bar),
                    runner._opportunity_id(s25.dt_text(signal_bar), quote, "m5"),
                ))
            submit.assert_not_called()
            self.assertIsNone(runner._st(strategy)["pending_open"])
            self.assertTrue(runner._st(strategy)["sync_block_reason"].startswith("broker_quote_stale"))

    def test_open_confirmation_requires_reserved_side_and_comment(self):
        for mismatch in ("side", "comment"):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory(prefix="s25-open-owner-") as temp:
                runner, strategy, executor, _position = self._runner_with_position(temp)
                original = executor.open_position
                def altered(*args, **kwargs):
                    ticket = original(*args, **kwargs)
                    record = executor.positions[-1]
                    if mismatch == "side":
                        record.type = s25.ORDER_TYPE_SELL
                    else:
                        record.comment = "s25_m231_L9999"
                    return ticket
                quote = pd.Timestamp(executor.info.quote_time_msc, unit="ms", tz="UTC")
                signal_bar = quote.floor("5min") - pd.Timedelta(minutes=5)
                runner._st(strategy)["last_processed_m5_bar"] = s25.dt_text(signal_bar)
                runner._st(strategy)["active_wave"] = 1
                with mock.patch.object(runner, "_quote_clock_error", return_value=None), mock.patch.object(executor, "open_position", side_effect=altered):
                    self.assertFalse(runner._open_position(
                        strategy, "LONG", executor.info, quote, "long_frontier_add",
                        s25.dt_text(signal_bar),
                        runner._opportunity_id(s25.dt_text(signal_bar), quote, "m5"),
                    ))
                self.assertEqual(len(runner._st(strategy)["positions"]), 1)
                self.assertIsNotNone(runner._st(strategy)["pending_open"])

    def test_close_transaction_failure_and_late_commit_recovery(self):
        for fault in ("ledger", "before_commit", "after_commit"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(prefix="s25-close-commit-") as temp:
                runner, strategy, executor, position = self._runner_with_position(temp)
                executor.close_position(position["ticket"])
                state_ref = runner._st(strategy)
                before = copy.deepcopy(runner.state)
                real_write = s25.atomic_write_json
                def write(path, payload):
                    if fault == "after_commit":
                        real_write(path, payload)
                    raise OSError("injected state write failure")
                patcher = mock.patch.object(s25, "append_csv", side_effect=OSError("ledger failure")) if fault == "ledger" else mock.patch.object(s25, "atomic_write_json", side_effect=write)
                with patcher, self.assertRaises(OSError):
                    runner._sync_strategy(strategy)
                self.assertIs(state_ref, runner._st(strategy))
                self.assertEqual(len(state_ref["positions"]), 0 if fault == "after_commit" else 1)
                if fault != "after_commit":
                    self.assertEqual(runner.state, before)
                self.assertTrue(runner._sync_strategy(strategy))
                self.assertEqual(state_ref["positions"], [])
                with Path(s25.TRADE_LOG_FILE).open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(sum(row["event"] == "position_close_confirmed" for row in rows), 1)
                restarted = s25.S25V24Runner(runner.params)
                self.assertEqual(restarted._st(strategy)["positions"], [])

    def test_close_ledger_conflicting_ownership_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="s25-ledger-owner-") as temp:
            path = str(Path(temp) / "trades.csv")
            row = {"event": "position_close_confirmed", "deal_id": 42, "ticket": 11, "strategy_id": "s25"}
            s25.append_csv(path, row, s25.TRADE_FIELDS)
            s25.append_csv(path, row, s25.TRADE_FIELDS)
            with self.assertRaises(RuntimeError):
                s25.append_csv(path, dict(row, ticket=12), s25.TRADE_FIELDS)

    def test_stale_quote_still_reconciles_confirmed_external_close(self):
        with tempfile.TemporaryDirectory(prefix="s25-stale-sync-") as temp:
            runner, strategy, executor, position = self._runner_with_position(temp)
            executor.close_position(position["ticket"])
            with mock.patch.object(runner, "_quote_clock_error", return_value="broker_quote_stale"), mock.patch.object(runner, "_run_strategy") as run:
                runner.run_once()
            self.assertEqual(runner._st(strategy)["positions"], [])
            self.assertEqual(runner._st(strategy)["sync_block_reason"], "broker_quote_stale")
            run.assert_not_called()

    def test_manual_close_deal_magic_zero_reconciles_by_owned_position_identity(self):
        with tempfile.TemporaryDirectory(prefix="s25-manual-close-sync-") as temp:
            runner, strategy, executor, position = self._runner_with_position(temp)
            position_id = int(position["position_identifier"])
            executor.close_position(position["ticket"])
            executor.deals[position_id].magic = 0
            executor.deals[position_id].reason = "CLIENT"

            self.assertTrue(runner._sync_strategy(strategy))

            self.assertEqual(runner._st(strategy)["positions"], [])
            with Path(s25.TRADE_LOG_FILE).open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            confirmed = [row for row in rows if row["event"] == "position_close_confirmed"]
            self.assertEqual(len(confirmed), 1)
            self.assertEqual(confirmed[0]["broker_reason"], "CLIENT")

    def test_productive_close_row_deduplicates_after_state_failure(self):
        with tempfile.TemporaryDirectory(prefix="s25-productive-retry-") as temp:
            runner, strategy, executor, position = self._runner_with_position(temp)
            state = runner._st(strategy)
            position["close_requested"] = True
            state["last_processed_m5_bar"] = "2026-09-04T00:20:00+00:00"
            state["pending_close_reason"] = "opposite_pivot_break"
            state["pending_close_m5_bar"] = "2026-09-04T00:20:00+00:00"
            state["pending_close_requested_at_utc"] = "2026-09-04T00:25:00+00:00"
            state["pending_productive_close"] = {
                "position_ids": [position["position_identifier"]],
                "confirmed_ids": [], "strategy_profit_usd": 0.0,
                "reason": "opposite_pivot_break", "last_deal_utc": None,
            }
            executor.close_position(position["ticket"])
            with mock.patch.object(s25, "atomic_write_json", side_effect=OSError("before replace")), self.assertRaises(OSError):
                runner._sync_strategy(strategy)
            self.assertTrue(runner._sync_strategy(strategy))
            with Path(s25.TRADE_LOG_FILE).open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(sum(row["event"] == "productive_close_confirmed" for row in rows), 1)

    def test_resolved_close_keeps_orders_visibility_block(self):
        with tempfile.TemporaryDirectory(prefix="s25-close-orders-") as temp:
            runner, strategy, executor, position = self._runner_with_position(temp)
            position["close_requested"] = True
            position["close_submission_started_utc"] = "2026-08-27T00:25:00Z"
            runner._set_sync_block(strategy, "close_submission_unresolved", recoverable=False)
            executor.close_position(position["ticket"])
            with mock.patch.object(executor, "get_orders", return_value=None):
                self.assertTrue(runner._sync_strategy(strategy))
            state = runner._st(strategy)
            self.assertEqual(state["positions"], [])
            self.assertEqual(state["sync_block_reason"], "orders_unavailable")
            self.assertTrue(state["sync_block_new_entries"])
            self.assertTrue(runner._sync_strategy(strategy))
            self.assertFalse(state["sync_block_new_entries"])

    def test_partial_confirmation_preserves_other_unresolved_close(self):
        with tempfile.TemporaryDirectory(prefix="s25-close-partial-") as temp:
            runner, strategy, executor, first = self._runner_with_position(temp)
            executor.open_position("XAUUSD", s25.ORDER_TYPE_SELL, 0.01, magic=s25.EXPECTED_S25_MAGIC, comment="s25_m231_S0002")
            second = runner._state_position_from_live(strategy, executor.positions[-1])
            second["close_requested"] = True
            second["close_submission_started_utc"] = "2026-08-27T00:25:00Z"
            runner._st(strategy)["positions"].append(second)
            runner._st(strategy)["pending_close_reason"] = "feed_gap"
            runner._st(strategy)["pending_close_requested_at_utc"] = "2026-08-27T00:25:00Z"
            runner._set_sync_block(strategy, "close_submission_unresolved", recoverable=False)
            executor.close_position(first["ticket"])
            self.assertTrue(runner._sync_strategy(strategy))
            self.assertEqual(runner._st(strategy)["sync_block_reason"], "close_submission_unresolved")
            self.assertEqual(len(runner._st(strategy)["positions"]), 1)

    def test_market_closed_retry_stays_scoped_to_selected_ticket(self):
        with tempfile.TemporaryDirectory(prefix="s25-close-market-") as temp:
            runner, strategy, executor, position = self._runner_with_position(temp)
            calls = 0

            def market_closed(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                return s25.CloseResult(False, status="MARKET_CLOSED", retcode=10018)

            executor.close_position = market_closed
            quote = pd.Timestamp("2026-09-04T00:10:00Z")
            self.assertEqual(runner._close_positions(strategy, [position], "feed_gap", executor.info, quote, None), "requested")
            self.assertIsNone(position["close_submission_started_utc"])
            self.assertIsNone(runner._st(strategy)["close_defer"])
            runner._retry_pending_close_requests(strategy, quote + pd.Timedelta(minutes=2))
            self.assertEqual(calls, 2)

    def test_replaced_trade_csv_is_revalidated(self):
        with tempfile.TemporaryDirectory(prefix="s25-csv-") as temp:
            path = str(Path(temp) / "logs" / "events.csv")
            fields = ["event", "deal_id"]
            s25._CSV_SCHEMAS_VALIDATED.discard(path)
            s25._CSV_EVENT_KEYS.pop(path, None)
            s25._CSV_FILE_IDENTITIES.pop(path, None)
            s25.append_csv(path, {"event": "close", "deal_id": "1"}, fields)
            with open(path, "w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(fields)
                writer.writerow(["broken"])
            with self.assertRaises(RuntimeError):
                s25.append_csv(path, {"event": "close", "deal_id": "2"}, fields)


if __name__ == "__main__":
    unittest.main()
