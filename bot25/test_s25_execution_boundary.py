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
        self.assertIn('#define BRIDGE_VERSION "2026-09-04-s25-v24-atomic-v8"', source)
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
            executor.positions = []
            position["shadow"] = True
            self.assertFalse(runner._sync_strategy(strategy))
            self.assertEqual(runner._st(strategy)["sync_block_reason"], "shadow_positions_present_in_live_mode")

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
        state["positions"] = [runner._state_position_from_live(strategy, record)]
        runner._save_state()
        return runner, strategy, executor, state["positions"][0]

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
            result = runner._close_positions(strategy, [position], "test", executor.info, quote, None)
            self.assertEqual(result, "blocked")
            self.assertEqual(calls, 1)
            self.assertIsNotNone(position["close_submission_started_utc"])
            runner._retry_pending_close_requests(strategy, quote + pd.Timedelta(minutes=1))
            self.assertEqual(calls, 1)

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
            with mock.patch.object(executor, "close_position", side_effect=crash_after_close), self.assertRaises(OSError):
                runner._release_active_side(strategy, -1, "opposite_pivot_break", executor.info, quote, "2026-08-27T00:20:00Z")
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
            with mock.patch.object(executor, "close_position") as close:
                self.assertTrue(runner._release_active_side(strategy, -1, "opposite_pivot_break", executor.info, quote, "2026-08-27T00:20:00Z"))
            close.assert_not_called()
            self.assertIsNone(state["pending_post_close_action"])
            self.assertEqual(state["active_wave"], 1)
    def test_post_open_checks_foreign_rows_and_known_position_drift(self):
        for anomaly in ("foreign_comment", "known_volume"):
            with self.subTest(anomaly=anomaly), tempfile.TemporaryDirectory(prefix="s25-open-inventory-") as temp:
                runner, strategy, executor, _position = self._runner_with_position(temp)
                original = executor.open_position
                def altered(*args, **kwargs):
                    ticket = original(*args, **kwargs)
                    if anomaly == "known_volume":
                        executor.positions[0].volume = 0.02
                    else:
                        foreign = copy.copy(executor.positions[-1])
                        foreign.ticket += 10000
                        foreign.identifier += 10000
                        foreign.comment = "foreign_manual"
                        executor.positions.append(foreign)
                    return ticket
                quote = pd.Timestamp(executor.info.quote_time_msc, unit="ms", tz="UTC")
                with mock.patch.object(runner, "_quote_clock_error", return_value=None), mock.patch.object(executor, "open_position", side_effect=altered):
                    self.assertFalse(runner._open_position(strategy, "LONG", executor.info, quote, "frontier_add"))
                self.assertTrue(runner._st(strategy)["sync_block_new_entries"])
                self.assertIsNotNone(runner._st(strategy)["pending_open"])
                self.assertEqual(len(runner._st(strategy)["positions"]), 1)

    def test_pending_adoption_requires_no_unexplained_extra_position(self):
        with tempfile.TemporaryDirectory(prefix="s25-recovery-extra-") as temp:
            runner, strategy, executor, position = self._runner_with_position(temp)
            state = runner._st(strategy)
            state["pending_open"] = {"side": "LONG", "lot": 0.01, "comment": "s25_m231_L0002", "known_position_ids": [position["position_identifier"]], "quote_time_utc": "2026-08-27T00:25:00Z", "flat_confirmation_count": 0}
            executor.open_position("XAUUSD", s25.ORDER_TYPE_BUY, 0.01, magic=s25.EXPECTED_S25_MAGIC, comment="s25_m231_L0002")
            executor.open_position("XAUUSD", s25.ORDER_TYPE_SELL, 0.01, magic=s25.EXPECTED_S25_MAGIC, comment="s25_m231_S9999")
            self.assertFalse(runner._sync_strategy(strategy))
            self.assertIsNotNone(state["pending_open"])
            self.assertEqual(len(state["positions"]), 1)
    def test_pending_open_requires_consecutive_complete_clean_queries(self):
        for failed_query in ("get_positions", "get_orders"):
            with self.subTest(query=failed_query), tempfile.TemporaryDirectory(prefix="s25-pending-clean-") as temp:
                runner, strategy, executor, position = self._runner_with_position(temp)
                state = runner._st(strategy)
                state["pending_open"] = {
                    "side": "LONG", "lot": 0.01, "comment": "s25_m231_L0002",
                    "known_position_ids": [position["position_identifier"]],
                    "quote_time_utc": "2026-08-27T00:25:00Z", "flat_confirmation_count": 0,
                }
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

    def test_pending_recovery_transaction_is_retryable_and_deduplicated(self):
        for recovery in ("adopt", "flat"):
            for fault in ("ledger", "before_commit", "after_commit"):
                with self.subTest(recovery=recovery, fault=fault), tempfile.TemporaryDirectory(prefix="s25-recovery-commit-") as temp:
                    runner, strategy, executor, position = self._runner_with_position(temp)
                    state = runner._st(strategy)
                    state["pending_open"] = {
                        "side": "LONG", "lot": 0.01, "comment": "s25_m231_L0002",
                        "known_position_ids": [position["position_identifier"]],
                        "quote_time_utc": "2026-08-27T00:25:00Z", "flat_confirmation_count": 1,
                        "opportunity_id": "test_pending_recovery", "reason": "frontier_add",
                    }
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
                self.assertFalse(runner._open_position(strategy, "LONG", executor.info, quote, "frontier_add"))
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
                with mock.patch.object(runner, "_quote_clock_error", return_value=None), mock.patch.object(executor, "open_position", side_effect=altered):
                    self.assertFalse(runner._open_position(strategy, "LONG", executor.info, quote, "frontier_add"))
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

    def test_productive_close_row_deduplicates_after_state_failure(self):
        with tempfile.TemporaryDirectory(prefix="s25-productive-retry-") as temp:
            runner, strategy, executor, position = self._runner_with_position(temp)
            state = runner._st(strategy)
            state["pending_productive_close"] = {"position_ids": [position["position_identifier"]], "confirmed_ids": [], "strategy_profit_usd": 0.0, "reason": "test"}
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
            self.assertEqual(runner._close_positions(strategy, [position], "partial_release", executor.info, quote, None), "requested")
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
