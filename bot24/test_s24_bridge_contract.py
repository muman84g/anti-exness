"""Contract tests for the bot24 MT5 bridge and Python parser."""

from __future__ import annotations

import unittest
import io
import hashlib
import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path

import live_executor
import live_data_fetcher
import live_s24_bot
from ea_bridge import EABridgeServer


class S24BridgeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_send = live_executor.ea_bridge.send_command

    def tearDown(self) -> None:
        live_executor.ea_bridge.send_command = self.original_send

    def test_mql_bridge_exposes_identity_and_quote_timestamp(self):
        text = Path(__file__).with_name("BotBridge_s24.mq5").read_text(encoding="utf-8-sig")
        self.assertIn('#define BRIDGE_NAME "BotBridge_s24"', text)
        self.assertIn('#define BRIDGE_VERSION "2026-09-02-s24-core-atomic-v13"', text)
        self.assertIn('input string InpCommandFile = "cmd_s24.txt";', text)
        self.assertIn('input string InpResponseFile = "res_s24.txt";', text)
        self.assertIn("ACCOUNT_LOGIN", text)
        self.assertIn("ACCOUNT_SERVER", text)
        self.assertIn("tick.time_msc", text)
        self.assertIn('"RES|" + response + "|ENDRES"', text)
        self.assertIn('StringFormat("END,%d", matched)', text)
        self.assertIn('(long)rates[i].time', text)
        self.assertNotIn('TimeToString(rates[i].time', text)
        caps = text.split('#define BRIDGE_COMMANDS "', 1)[1].split('"', 1)[0].split(",")
        for unsupported in ("PENDING", "MODIFY", "CANCEL"):
            self.assertNotIn(unsupported, caps)
            self.assertIn(f'if(op == "{unsupported}")', text)
        self.assertGreaterEqual(text.count('return "ERR|UNSUPPORTED_COMMAND";'), 3)
        self.assertIn("return FileDelete(InpCommandFile);", text)
        self.assertNotIn("void ClearCommand()", text)
        self.assertIn("ParseRequestEnvelope", text)
        self.assertIn('WriteResponse("RID|" + request_id + "|" + HandleCommand(payload))', text)

    def test_mql_zero_argument_commands_reject_extra_fields(self):
        text = Path(__file__).with_name("BotBridge_s24.mq5").read_text(encoding="utf-8-sig")
        self.assertIn("bool IsZeroArgCommand", text)
        for command in ("ECHO", "CAPS", "ACCOUNT"):
            self.assertIn(f'if(op == "{command}" && IsZeroArgCommand(parts, n))', text)

    def test_mql_core_comment_policy_matches_python_hash_namespace(self):
        text = Path(__file__).with_name("BotBridge_s24.mq5").read_text(encoding="utf-8-sig")
        self.assertIn("bool IsCurrentCoreComment", text)
        self.assertIn("StringLen(comment) != StringLen(prefix) + 10", text)
        self.assertIn("StringGetCharacter(comment, index)", text)
        self.assertIn("IsCurrentCoreComment(comment)", text)
        self.assertNotIn('StringFind(comment, "s24_no_adverse:") == 0', text)

    def test_mql_execution_numeric_fields_are_lexically_strict(self):
        text = Path(__file__).with_name("BotBridge_s24.mq5").read_text(encoding="utf-8-sig")
        self.assertIn("bool IsUnsignedIntegerText", text)
        self.assertIn("bool IsUnsignedDecimalText", text)
        for guard in (
            "ValidOpenR1NumericFields(parts)",
            "ValidCloseR1NumericFields(parts)",
            "ValidRepairR1NumericFields(parts)",
            "ValidCoreOpenNumericFields(parts)",
            "ValidCoreCloseNumericFields(parts)",
        ):
            with self.subTest(guard=guard):
                self.assertIn(guard, text)

    def test_mql_request_expiry_is_lexically_strict_before_conversion(self):
        text = Path(__file__).with_name("BotBridge_s24.mq5").read_text(encoding="utf-8-sig")
        self.assertIn("if(!IsUnsignedIntegerText(expiry_text))", text)
        strict_check = text.index("if(!IsUnsignedIntegerText(expiry_text))")
        conversion = text.index("expires_epoch = StringToInteger(expiry_text)")
        self.assertLess(strict_check, conversion)

    def test_mql_query_commands_require_exact_shape_and_xauusd_identity(self):
        text = Path(__file__).with_name("BotBridge_s24.mq5").read_text(encoding="utf-8-sig")
        for exact_dispatch in (
            'if(op == "INFO" && n == 2)',
            'if(op == "HIST" && n == 4)',
            'if(op == "POSITIONS" && n == 3)',
            'if(op == "POSITION" && n == 2)',
            'if(op == "ORDERS" && n == 3)',
            'if(op == "CLOSEDEAL" && n == 3)',
        ):
            with self.subTest(dispatch=exact_dispatch):
                self.assertIn(exact_dispatch, text)
        for guard in (
            "ValidHistNumericFields(parts)",
            "ValidInventoryQueryNumericField(parts)",
            "ValidTicketQueryNumericField(parts)",
            "ValidCloseDealNumericFields(parts)",
        ):
            with self.subTest(guard=guard):
                self.assertIn(guard, text)
        self.assertIn('if(symbol != "XAUUSD")\n         return "ERR|INFO_POLICY_GUARD";', text)
        self.assertIn('if(symbol != "XAUUSD")\n         return "ERR|HIST_POLICY_GUARD";', text)

    def test_account_parser_requires_and_returns_identity_fields(self):
        live_executor.ea_bridge.send_command = lambda *_args, **_kwargs: (
            "OK|2|RETAIL_HEDGING|1|1|1|1|123456|Example-MT5|USD"
        )
        account = live_executor.MT5Executor().get_account_info()
        self.assertIsNotNone(account)
        self.assertEqual(account["login"], 123456)
        self.assertEqual(account["server"], "Example-MT5")

    def test_info_parser_returns_broker_quote_timestamp(self):
        live_executor.ea_bridge.send_command = lambda *_args, **_kwargs: (
            "OK|2064.03|2064.00|1000|0.001|0.01|100.0|0.01|1.0|0.001|100.0|3|0|1767272520123|4|3"
        )
        info = live_executor.MT5Executor().get_symbol_info("XAUUSD")
        self.assertIsNotNone(info)
        self.assertEqual(info.quote_time_msc, 1767272520123)

    def test_nonfinite_or_extra_info_is_rejected(self):
        executor = live_executor.MT5Executor()
        live_executor.ea_bridge.send_command = lambda *_args, **_kwargs: (
            "OK|nan|2064.00|1000|0.001|0.01|100.0|0.01|1.0|0.001|100.0|3|0|1767272520123|4|3"
        )
        self.assertIsNone(executor.get_symbol_info("XAUUSD"))
        live_executor.ea_bridge.send_command = lambda *_args, **_kwargs: (
            "OK|2064.03|2064.00|1000|0.001|0.01|100.0|0.01|1.0|0.001|100.0|3|0|1767272520123|4|3|EXTRA"
        )
        self.assertIsNone(executor.get_symbol_info("XAUUSD"))

    def test_core_open_and_close_reject_nonfinite_extra_and_false_absence(self):
        executor = live_executor.MT5Executor()
        open_kwargs = dict(deviation=50, magic=200024, comment="s24_no_adverse:abc123def0", digits=3, expected_login=123456, expected_server="Example-MT5", expected_owned_positions=0)
        live_executor.ea_bridge.send_command = lambda *_args, **_kwargs: "OK|1001|7001|9001|nan|1767272400|10009"
        self.assertIsNone(executor.open_position("XAUUSD", 0, 0.01, 0.0, 0.0, **open_kwargs))
        live_executor.ea_bridge.send_command = lambda *_args, **_kwargs: "OK|1001|7001|9001|2000.0|1767272400|10009|EXTRA"
        self.assertIsNone(executor.open_position("XAUUSD", 0, 0.01, 0.0, 0.0, **open_kwargs))
        close_kwargs = dict(expected_login=123456, expected_server="Example-MT5", expected_symbol="XAUUSD", expected_magic=200024, expected_comment="s24_no_adverse:abc123def0", expected_identifier=7001, expected_type=0, expected_volume=0.01)
        live_executor.ea_bridge.send_command = lambda *_args, **_kwargs: "OK|1001|0.01|2000.0|2001.0|nan|9001|10009"
        self.assertFalse(executor.close_position(1001, 50, **close_kwargs).success)
        live_executor.ea_bridge.send_command = lambda *_args, **_kwargs: "ERR|10009"
        self.assertEqual(executor.close_position(1001, 50, **close_kwargs).status, "FAILED")

    def test_core_executor_rejects_malformed_comment_namespace_without_ipc(self):
        executor = live_executor.MT5Executor()
        commands = []
        live_executor.ea_bridge.send_command = lambda command, **_kwargs: commands.append(command) or "ERR|UNEXPECTED"
        common = dict(
            expected_login=123456, expected_server="Example-MT5",
        )
        for comment in (
            "s24_no_adverse:",
            "s24_no_adverse:owner",
            "s24_no_adverse:abc123",
            "s24_no_adverse:abc123:foreign",
            "s24_no_adverse_foreign",
        ):
            with self.subTest(comment=comment):
                self.assertIsNone(executor.open_position(
                    "XAUUSD", 0, 0.01, 0.0, 0.0, deviation=50, magic=200024,
                    comment=comment, digits=3, expected_owned_positions=0, **common,
                ))
                self.assertEqual(executor.last_order_error, "OPEN_POLICY_GUARD")
                result = executor.close_position(
                    1001, 50, expected_symbol="XAUUSD", expected_magic=200024,
                    expected_comment=comment, expected_identifier=7001,
                    expected_type=0, expected_volume=0.01, **common,
                )
                self.assertEqual(result.status, "INVALID_REQUEST")
        self.assertEqual(commands, [])

    def test_core_close_no_fill_retcodes_require_exact_zero_deal_receipts(self):
        executor = live_executor.MT5Executor()
        kwargs = dict(
            expected_login=123456, expected_server="Example-MT5", expected_symbol="XAUUSD",
            expected_magic=200024, expected_comment="s24_no_adverse:abc123def0",
            expected_identifier=7001, expected_type=0, expected_volume=0.01,
        )
        cases = {
            "ERR|10018|DEAL=0|LAST=0": "MARKET_CLOSED",
            "ERR|10026|DEAL=0|LAST=0": "TRADE_PERMISSION_GUARD",
            "ERR|10027|DEAL=0|LAST=0": "TRADE_PERMISSION_GUARD",
            "ERR|10018|DEAL=9|LAST=0": "FAILED",
            "ERR|10026|DEAL=9|LAST=0": "FAILED",
            "ERR|10018": "FAILED",
            "ERR|10018|DEAL=x|LAST=0": "FAILED",
        }
        for response, expected in cases.items():
            with self.subTest(response=response):
                live_executor.ea_bridge.send_command = lambda *_args, value=response, **_kwargs: value
                self.assertEqual(executor.close_position(1001, 50, **kwargs).status, expected)

    def test_core_open_and_close_commands_carry_atomic_guards(self):
        commands = []
        def send(command, **_kwargs):
            commands.append(command)
            if command.startswith("OPEN|"):
                return "OK|1001|7001|9001|2000.0|1767272400|10009"
            return "ERR|TRADE_PERMISSION_GUARD"
        live_executor.ea_bridge.send_command = send
        executor = live_executor.MT5Executor()
        ticket = executor.open_position(
            "XAUUSD", 0, 0.01, 0.0, 0.0,
            deviation=50, magic=200024, comment="s24_no_adverse:abc123def0", digits=3,
            expected_login=123456, expected_server="Example-MT5", expected_owned_positions=2,
        )
        self.assertEqual(ticket, 1001)
        self.assertEqual(commands[-1], "OPEN|XAUUSD|0|0.01|0|0|200024|s24_no_adverse:abc123def0|50|123456|Example-MT5|2")
        result = executor.close_position(
            1001, 50, expected_login=123456, expected_server="Example-MT5",
            expected_symbol="XAUUSD", expected_magic=200024,
            expected_comment="s24_no_adverse:abc123def0", expected_identifier=7001,
            expected_type=0, expected_volume=0.01,
        )
        self.assertEqual(result.status, "TRADE_PERMISSION_GUARD")
        self.assertEqual(commands[-1], "CLOSE|1001|50|123456|Example-MT5|XAUUSD|200024|s24_no_adverse:abc123def0|7001|0|0.01")

    def test_hist_parser_accepts_only_monotonic_utc_epochs(self):
        original = live_data_fetcher.ea_bridge.send_command
        try:
            live_data_fetcher.ea_bridge.send_command = lambda *_args, **_kwargs: (
                "OK|1767272400,2000,2001,1999,2000.5,10|1767272460,2000.5,2002,2000,2001,11"
            )
            bars = live_data_fetcher.MT5DataManager().get_historical_data("XAUUSD", 1, 2)
            self.assertIsNotNone(bars)
            self.assertEqual(str(bars.index.tz), "UTC")
            live_data_fetcher.ea_bridge.send_command = lambda *_args, **_kwargs: (
                "OK|2026.01.01 13:00,2000,2001,1999,2000.5,10"
            )
            self.assertIsNone(live_data_fetcher.MT5DataManager().get_historical_data("XAUUSD", 1, 1))
        finally:
            live_data_fetcher.ea_bridge.send_command = original

    def test_position_and_order_records_have_distinct_strict_shapes(self):
        executor = live_executor.MT5Executor()
        live_executor.ea_bridge.send_command = lambda *_args, **_kwargs: (
            "OK|1001,XAUUSD,0,0.01,2000.0,1999.0,2001.0,0.0,240206,1767272400,1767272400000,7001,s24_v206|END,1"
        )
        positions = executor.get_positions("XAUUSD", 240206)
        self.assertEqual([row.ticket for row in positions or []], [1001])
        live_executor.ea_bridge.send_command = lambda *_args, **_kwargs: (
            "OK|77,XAUUSD,2,0.01,2000.0,1999.0,2001.0,240206,s24_v206|END,1"
        )
        orders = executor.get_orders("XAUUSD", 240206)
        self.assertEqual([row.ticket for row in orders or []], [77])

    def test_truncated_inventory_and_legacy_absence_are_rejected(self):
        executor = live_executor.MT5Executor()
        live_executor.ea_bridge.send_command = lambda *_args, **_kwargs: (
            "OK|1001,XAUUSD,0,0.01,2000.0,1999.0,2001.0,0.0,240206,1767272400,1767272400000,7001,s24_v206"
        )
        self.assertIsNone(executor.get_positions("XAUUSD", 240206))
        live_executor.ea_bridge.send_command = lambda *_args, **_kwargs: "ERR|10009"
        self.assertIsNone(executor.confirm_position_absent(1001))

    def test_state_json_rejects_nonfinite_and_duplicate_keys(self):
        with self.assertRaises(ValueError):
            live_s24_bot.strict_json_load(io.StringIO('{"version":1,"version":2}'))
        with self.assertRaises(ValueError):
            live_s24_bot.strict_json_load(io.StringIO('{"lot":NaN}'))
        with tempfile.TemporaryDirectory() as directory:
            target = str(Path(directory) / "state.json")
            with self.assertRaises(ValueError):
                live_s24_bot.atomic_write_json(target, {"lot": math.nan})
            self.assertFalse(Path(target).exists())

    def test_frozen_live_configuration_rejects_execution_drift(self):
        params = json.loads(json.dumps(live_s24_bot.load_params()))
        params["expected_bridge_version"] = "old"
        with self.assertRaisesRegex(ValueError, "expected_bridge_version"):
            live_s24_bot.S24NoAdverseRunner(params)

        params = json.loads(json.dumps(live_s24_bot.load_params()))
        params["entry_time_routing"]["regimes"][0]["start_local"] = "12:59"
        with self.assertRaisesRegex(ValueError, "entry_time_routing_contract"):
            live_s24_bot.S24NoAdverseRunner(params)

    def test_execution_mode_flags_require_booleans_and_exactly_one_active_mode(self):
        for key in ("enabled", "live_trading_enabled", "shadow_forward_enabled"):
            params = json.loads(json.dumps(live_s24_bot.load_params()))
            params[key] = "false"
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, f"{key}_not_boolean"):
                live_s24_bot.S24NoAdverseRunner(params)

        params = json.loads(json.dumps(live_s24_bot.load_params()))
        params["live_trading_enabled"] = True
        params["shadow_forward_enabled"] = True
        with self.assertRaisesRegex(ValueError, "execution_mode_contract"):
            live_s24_bot.S24NoAdverseRunner(params)

    def test_reconciliation_only_mode_cannot_create_shadow_entries(self):
        params = json.loads(json.dumps(live_s24_bot.load_params()))
        params["live_trading_enabled"] = False
        params["shadow_forward_enabled"] = False
        params["runner_shadow"]["opportunity_observer"]["enabled"] = False
        params["runner_shadow"]["state_tagger"]["enabled"] = False
        runner = live_s24_bot.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        rows = []
        runner._trade_row = lambda event, _strat, **kwargs: rows.append((event, kwargs))
        row = live_s24_bot.FakeDM().get_historical_data().iloc[-1].copy()
        runner._open_entry(params["strategies"][0], "LONG", row, live_s24_bot.FakeExecutor().get_symbol_info("XAUUSD"))
        state = runner._st(params["strategies"][0])
        self.assertEqual(state["basket"], [])
        self.assertTrue(any(event == "entry_skip" and data.get("reason") == "execution_disabled" for event, data in rows))

    def test_passive_shadow_enable_flags_require_booleans(self):
        for path in (("runner_shadow",), ("runner_shadow", "opportunity_observer"), ("runner_shadow", "state_tagger")):
            params = json.loads(json.dumps(live_s24_bot.load_params()))
            target = params
            for key in path:
                target = target[key]
            target["enabled"] = "false"
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "enabled_not_boolean"):
                live_s24_bot.S24NoAdverseRunner(params)

    def test_runtime_loop_intervals_are_frozen_before_any_poll_can_run(self):
        for key in ("poll_interval_seconds", "status_log_interval_seconds"):
            for value in (None, "5", 0, -1, math.nan, math.inf):
                params = json.loads(json.dumps(live_s24_bot.load_params()))
                params[key] = value
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, key):
                    live_s24_bot.S24NoAdverseRunner(params)

    def test_ipc_artifact_names_cannot_escape_the_bridge_directory(self):
        environment_keys = (
            "EA_BRIDGE_COMMAND_FILE",
            "EA_BRIDGE_RESPONSE_FILE",
            "EA_BRIDGE_LOCK_FILE",
        )
        original = {key: os.environ.get(key) for key in environment_keys}
        try:
            for key in environment_keys:
                for value in ("../outside.txt", "C:\\outside.txt", "name.txt:stream"):
                    for clear_key in environment_keys:
                        os.environ.pop(clear_key, None)
                    os.environ[key] = value
                    with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                        EABridgeServer(bot_suffix="s24", files_dir="C:\\safe")
            for key in environment_keys:
                os.environ.pop(key, None)
            with self.assertRaises(ValueError):
                EABridgeServer(bot_suffix="../s24", files_dir="C:\\safe")
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_ipc_command_response_and_lock_names_must_be_distinct(self):
        environment_keys = (
            "EA_BRIDGE_COMMAND_FILE",
            "EA_BRIDGE_RESPONSE_FILE",
            "EA_BRIDGE_LOCK_FILE",
        )
        original = {key: os.environ.get(key) for key in environment_keys}
        try:
            os.environ["EA_BRIDGE_COMMAND_FILE"] = "shared.txt"
            os.environ["EA_BRIDGE_RESPONSE_FILE"] = "SHARED.txt"
            os.environ["EA_BRIDGE_LOCK_FILE"] = "lock.txt"
            with self.assertRaisesRegex(ValueError, "must be distinct"):
                EABridgeServer(bot_suffix="s24", files_dir="C:\\safe")
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_ipc_waits_for_complete_response_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = EABridgeServer(bot_suffix="s24test", files_dir=directory)

            def responder() -> None:
                command = Path(bridge.cmd_file)
                deadline = time.time() + 2.0
                while not command.exists() and time.time() < deadline:
                    time.sleep(0.01)
                response = Path(bridge.res_file)
                framed = command.read_text(encoding="utf-8")
                request_id = framed.split("|", 3)[1]
                response.write_text("RES|OK", encoding="utf-8")
                time.sleep(0.15)
                response.write_text(f"RES|RID|{request_id}|OK|ENDRES", encoding="utf-8")

            worker = threading.Thread(target=responder)
            worker.start()
            self.assertEqual(bridge.send_command("ECHO|", timeout=2.0), "OK")
            worker.join(timeout=2.0)

    def test_ipc_never_overwrites_unconsumed_command(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = EABridgeServer(bot_suffix="s24busy", files_dir=directory)
            Path(bridge.cmd_file).write_text("OPEN|old", encoding="utf-8")
            self.assertEqual(bridge.send_command("OPEN|new", timeout=0.2), "ERR|COMMAND_BUSY")
            self.assertEqual(Path(bridge.cmd_file).read_text(encoding="utf-8"), "OPEN|old")

    def test_ipc_removes_only_zero_byte_v7_command_remnant(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = EABridgeServer(bot_suffix="s24legacy", files_dir=directory)
            Path(bridge.cmd_file).write_bytes(b"")

            def responder() -> None:
                command = Path(bridge.cmd_file)
                deadline = time.time() + 2.0
                while (not command.exists() or command.stat().st_size == 0) and time.time() < deadline:
                    time.sleep(0.01)
                request_id = command.read_text(encoding="utf-8").split("|", 3)[1]
                Path(bridge.res_file).write_text(f"RES|RID|{request_id}|OK|ENDRES", encoding="utf-8")

            worker = threading.Thread(target=responder)
            worker.start()
            self.assertEqual(bridge.send_command("ECHO|", timeout=2.0), "OK")
            worker.join(timeout=2.0)

    def test_ipc_ignores_wrong_request_id_until_matching_response(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = EABridgeServer(bot_suffix="s24rid", files_dir=directory)

            def responder() -> None:
                command = Path(bridge.cmd_file)
                deadline = time.time() + 2.0
                while not command.exists() and time.time() < deadline:
                    time.sleep(0.01)
                request_id = command.read_text(encoding="utf-8").split("|", 3)[1]
                response = Path(bridge.res_file)
                response.write_text("RES|RID|00000000000000000000000000000000|OK|stale|ENDRES", encoding="utf-8")
                deadline = time.time() + 2.0
                while response.exists() and time.time() < deadline:
                    time.sleep(0.01)
                response.write_text(f"RES|RID|{request_id}|OK|current|ENDRES", encoding="utf-8")

            worker = threading.Thread(target=responder)
            worker.start()
            self.assertEqual(bridge.send_command("ECHO|", timeout=2.0), "OK|current")
            worker.join(timeout=2.0)

    def test_ipc_lock_is_os_released_without_deleting_lock_file(self):
        with tempfile.TemporaryDirectory() as directory:
            first_bridge = EABridgeServer(bot_suffix="s24lock", files_dir=directory)
            second_bridge = EABridgeServer(bot_suffix="s24lock", files_dir=directory)
            first = first_bridge._acquire_ipc_lock(0.2)
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(second_bridge._acquire_ipc_lock(0.1))
            finally:
                if hasattr(first, "close"):
                    first.close()
                else:
                    os.close(first)
            self.assertTrue(Path(first_bridge.lock_file).exists())
            second = second_bridge._acquire_ipc_lock(0.2)
            self.assertIsNotNone(second)
            second_bridge._release_ipc_lock(second)

    def test_v206_signal_modules_match_frozen_evidence_and_all_modules_are_mounted(self):
        bot24 = Path(__file__).resolve().parent
        expected = {
            "v206_range_strategy.py": "c2066253c07e723ba7b3a167a6affc9a40b7d36e73077694b2aaef808104c147",
            "v206_execution.py": "f1e7155821d88f60ef5dc05d43153eb7a0c75ca88c8cce88771c4943d7e80e64",
        }
        for name, frozen_hash in expected.items():
            current = hashlib.sha256((bot24 / name).read_bytes()).hexdigest()
            self.assertEqual(frozen_hash, current, name)
        compose = (bot24.parent / "docker-compose.yml").read_text(encoding="utf-8")
        for name in (*expected, "v206_live_lane.py"):
            self.assertIn(f"./bot24/{name}:/app/bot24/{name}:ro", compose)

    def test_bot24_credentials_are_host_only_and_compose_wires_fail_closed_defaults(self):
        bot24 = Path(__file__).resolve().parent
        config = (bot24 / "live_config.py").read_text(encoding="utf-8")
        startup = (bot24 / "startup.ini").read_text(encoding="utf-8")
        compose = (bot24.parent / "docker-compose.yml").read_text(encoding="utf-8")

        for name in ("BOT24_MT5_LOGIN", "BOT24_MT5_PASSWORD", "BOT24_MT5_SERVER"):
            self.assertIn(f'os.environ.get("{name}"', config)
            self.assertIn(name, compose)
        self.assertNotRegex(config, r'MT5_PASSWORD\s*=\s*["\'][^"\']+["\']')
        self.assertIn("Login=0", startup)
        self.assertIn("Password=\n", startup)
        self.assertIn("Server=\n", startup)


if __name__ == "__main__":
    unittest.main(verbosity=2)
