"""No-order regression suite for the bot23 ZA live-port safety findings."""

from __future__ import annotations

import csv
import json
import math
import os
import re
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

import live_manual_alerts
import live_executor
import live_s23_bot
from ea_bridge import EABridgeServer
from live_s23_bot import (
    EXPECTED_MIDDAY_MAGICS,
    EXPECTED_MIDDAY_POLICY_ID,
    EXPECTED_MORNING_MAGICS,
    EXPECTED_MORNING_POLICY_ID,
    EXPECTED_PRE_EU30_MAGICS,
    EXPECTED_Q01_MAGICS,
    EXPECTED_Q01_POLICY_ID,
    EXPECTED_Q01_POLICY_PARAMS_HASH,
    EXPECTED_S23_MAGIC,
    EXPECTED_S23_MAGICS,
    LEGACY_S23_MAGICS,
    ORDER_TYPE_BUY,
    S23HorizontalInventoryRunner,
    dt_text,
    load_params,
    parse_ts,
    utc_now,
)


_CANONICAL_RUNTIME_FILES = {
    name: Path(getattr(live_s23_bot, name)).resolve()
    for name in (
        "STATE_FILE",
        "TRADE_LOG_FILE",
        "SIGNAL_EVALUATION_LOG_FILE",
        "RUNNER_LOCK_FILE",
    )
}
_MODULE_RUNTIME_DIR = None
_MODULE_RUNTIME_PATCHES = []


def setUpModule():
    """Keep the complete no-order module away from canonical runtime files."""
    global _MODULE_RUNTIME_DIR, _MODULE_RUNTIME_PATCHES
    _MODULE_RUNTIME_DIR = tempfile.TemporaryDirectory(prefix="bot23-regression-")
    isolated = Path(_MODULE_RUNTIME_DIR.name)
    targets = {
        "STATE_FILE": isolated / "s23_bot_state.json",
        "TRADE_LOG_FILE": isolated / "s23_trades.csv",
        "SIGNAL_EVALUATION_LOG_FILE": isolated / "s23_signal_evaluation.csv",
        "RUNNER_LOCK_FILE": isolated / "s23_runner.lock",
    }
    _MODULE_RUNTIME_PATCHES = [
        patch.object(live_s23_bot, name, str(path))
        for name, path in targets.items()
    ]
    for patcher in _MODULE_RUNTIME_PATCHES:
        patcher.start()


def tearDownModule():
    global _MODULE_RUNTIME_DIR, _MODULE_RUNTIME_PATCHES
    for patcher in reversed(_MODULE_RUNTIME_PATCHES):
        patcher.stop()
    _MODULE_RUNTIME_PATCHES = []
    if _MODULE_RUNTIME_DIR is not None:
        _MODULE_RUNTIME_DIR.cleanup()
        _MODULE_RUNTIME_DIR = None


class CountingExecutor:
    def __init__(self, *, orders=None, orders_available: bool = True):
        self.positions = []
        self.orders = [] if orders is None else list(orders)
        self.orders_available = orders_available
        self.open_calls = 0
        self.close_calls = []
        self.last_order_error = None
        self.close_deal = False

    def get_positions(self, symbol, magic):
        return [row for row in self.positions if row.symbol == symbol and int(row.magic) == int(magic)]

    def get_orders(self, symbol, magic):
        return [row for row in self.orders if row.symbol == symbol and int(row.magic) == int(magic)] if self.orders_available else None

    def get_account_info(self):
        return {
            "margin_mode": live_s23_bot.HEDGING_MARGIN_MODE,
            "margin_mode_name": "RETAIL_HEDGING",
            "account_trade_allowed": True,
            "account_trade_expert": True,
            "terminal_trade_allowed": True,
            "mql_trade_allowed": True,
            "login": live_s23_bot.MT5_LOGIN,
            "server": live_s23_bot.MT5_SERVER,
            "currency": "USD",
        }

    def confirm_position_absent(self, _ticket: int) -> bool:
        return True

    def get_position(self, ticket: int):
        for row in self.positions:
            if int(row.ticket) == int(ticket):
                return row
        return False

    def get_position_close_deal(self, _position_id: int, _opened_at_epoch: int):
        return self.close_deal

    def open_position(self, *_args, **_kwargs):
        self.open_calls += 1
        return None

    def close_position(self, ticket: int, _deviation: int, **_kwargs):
        self.close_calls.append(int(ticket))
        return True


class BridgeHealthLoggingRegressionTests(unittest.TestCase):
    def test_no_order_module_uses_only_isolated_runtime_files(self):
        for name, canonical in _CANONICAL_RUNTIME_FILES.items():
            self.assertNotEqual(Path(getattr(live_s23_bot, name)).resolve(), canonical)

    def test_compiled_bridge_version_contract_matches_runner_and_params(self):
        source = (Path(__file__).with_name("BotBridge_s23.mq5")).read_text(encoding="utf-8")
        version_line = next(
            line.strip() for line in source.splitlines()
            if line.strip().startswith("#define BRIDGE_VERSION ")
        )
        self.assertEqual(
            version_line,
            f'#define BRIDGE_VERSION "{live_s23_bot.EXPECTED_BRIDGE_VERSION}"',
        )
        params = load_params(str(Path(__file__).with_name("s23_params.json")))
        self.assertEqual(params["expected_bridge_version"], live_s23_bot.EXPECTED_BRIDGE_VERSION)

    def test_q01_lane_contract_is_configured_as_independent_lane_22(self):
        params = load_params(str(Path(__file__).with_name("s23_params.json")))
        self.assertEqual(params["q01_policy_id"], EXPECTED_Q01_POLICY_ID)
        self.assertEqual(params["q01_params_hash"], EXPECTED_Q01_POLICY_PARAMS_HASH)
        self.assertEqual(params["expected_q01_magics"], list(EXPECTED_Q01_MAGICS))
        q01 = params["q01_variance_release_strategies"]
        self.assertEqual(len(q01), 1)
        self.assertEqual(q01[0]["lane_id"], 22)
        self.assertEqual(q01[0]["magic"], 230044)
        self.assertEqual(q01[0]["comment_prefix"], "s23_q01_l1")
        self.assertEqual(q01[0]["max_positions"], 1)
        self.assertEqual(q01[0]["hold_minutes"], 30)
        self.assertEqual(q01[0]["cooldown"], 5)

    def test_ea_rejects_noncanonical_execution_and_envelope_numbers_before_conversion(self):
        source = (Path(__file__).with_name("BotBridge_s23.mq5")).read_text(encoding="utf-8")
        open_block = source.split('if(op == "OPEN"', 1)[1].split('if(op == "PENDING"', 1)[0]
        close_block = source.split('if(op == "CLOSE"', 1)[1].split('return "ERR|UNKNOWN_COMMAND"', 1)[0]
        timer = source.split("void OnTimer()", 1)[1]
        self.assertIn("IsRequestId(request_id)", timer)
        self.assertIn("ParseCanonicalUnsignedLong(deadline_text, deadline_msc)", timer)
        self.assertLess(timer.index("IsRequestId(request_id)"), timer.index("HandleCommand(command)"))
        self.assertIn("n != 12 || !ValidOpenNumericFields(parts)", open_block)
        self.assertLess(open_block.index("ValidOpenNumericFields"), open_block.index("StringToDouble"))
        self.assertIn("n != 9 || !ValidCloseNumericFields(parts)", close_block)
        self.assertLess(close_block.index("ValidCloseNumericFields"), close_block.index("StringToInteger"))

    def test_ea_query_surface_is_exactly_scoped_before_broker_reads(self):
        source = (Path(__file__).with_name("BotBridge_s23.mq5")).read_text(encoding="utf-8")
        for exact_guard in (
            'if(op == "INFO" && n == 2)',
            'if(op == "HIST" && n == 4)',
            'if(op == "HISTPAGE" && n == 5)',
            'if(op == "TICKS" && n == 6)',
            'if(op == "POSITIONS" && n == 3)',
            'if(op == "POSITION" && n == 2)',
            'if(op == "ORDERS" && n == 3)',
            'if(op == "CLOSEDEAL" && n == 3)',
        ):
            self.assertIn(exact_guard, source)
        self.assertIn('symbol != "XAUUSD" || !ValidHistoryNumericFields(parts, n)', source)
        self.assertIn("timeframe != PERIOD_M1", source)
        self.assertIn("!IsOwnedMagic(parsed_magic)", source)
        position_block = source.split('if(op == "POSITION"', 1)[1].split(
            'if(op == "ORDERS"', 1,
        )[0]
        self.assertIn('PositionGetString(POSITION_SYMBOL) != "XAUUSD"', position_block)
        self.assertIn("!IsOwnedMagic(selected_magic)", position_block)
        self.assertIn("CanonicalCommentForMagic(selected_magic)", position_block)

    def test_close_command_binds_account_identity_atomically(self):
        source = (Path(__file__).with_name("BotBridge_s23.mq5")).read_text(encoding="utf-8")
        close_block = source.split('if(op == "CLOSE"', 1)[1].split('return "ERR|UNKNOWN_COMMAND"', 1)[0]
        self.assertIn("ACCOUNT_LOGIN", close_block)
        self.assertIn("ACCOUNT_SERVER", close_block)
        self.assertLess(close_block.index("ACCOUNT_LOGIN"), close_block.index("trade.PositionClose"))

        executor = live_executor.MT5Executor()
        with patch.object(
            live_executor.ea_bridge,
            "send_command",
            return_value="ERR|ACCOUNT_IDENTITY_GUARD",
        ) as send:
            result = executor.close_position(
                7703,
                50,
                expected_login=123456,
                expected_server="Expected-Server",
                expected_symbol="XAUUSD",
                expected_magic=230035,
                expected_comment="s23_sv_l1",
                expected_identifier=8803,
            )
        self.assertFalse(result)
        self.assertEqual(result.status, "ACCOUNT_IDENTITY_GUARD")
        self.assertTrue(
            send.call_args.args[0].endswith(
                "|50|123456|Expected-Server|XAUUSD|230035|s23_sv_l1|8803"
            )
        )

    def test_close_command_binds_full_position_ownership_before_broker_call(self):
        source = (Path(__file__).with_name("BotBridge_s23.mq5")).read_text(encoding="utf-8")
        close_block = source.split('if(op == "CLOSE"', 1)[1].split('return "ERR|UNKNOWN_COMMAND"', 1)[0]
        for field in ("POSITION_SYMBOL", "POSITION_MAGIC", "POSITION_COMMENT", "POSITION_IDENTIFIER"):
            self.assertIn(field, close_block)
            self.assertLess(close_block.index(field), close_block.index("trade.PositionClose"))
        self.assertIn("POSITION_OWNERSHIP_GUARD", close_block)

    def test_open_command_binds_account_identity_and_permission_atomically(self):
        source = (Path(__file__).with_name("BotBridge_s23.mq5")).read_text(encoding="utf-8")
        open_block = source.split('if(op == "OPEN"', 1)[1].split('if(op == "PENDING"', 1)[0]
        policy_block = source.split("bool IsCanonicalOpenPolicy", 1)[1].split("bool IsPendingPlaced", 1)[0]
        self.assertIn("ACCOUNT_LOGIN", open_block)
        self.assertIn("ACCOUNT_SERVER", open_block)
        self.assertIn("ACCOUNT_TRADE_ALLOWED", open_block)
        self.assertLess(open_block.index("ACCOUNT_LOGIN"), open_block.index("trade.Buy"))

        executor = live_executor.MT5Executor()
        with patch.object(
            live_executor.ea_bridge,
            "send_command",
            return_value="ERR|ACCOUNT_IDENTITY_GUARD",
        ) as send:
            self.assertIsNone(
                executor.open_position(
                    "XAUUSD", ORDER_TYPE_BUY, 0.01, 0.0, 0.0,
                    deviation=50, magic=230035, comment="s23_sv_l1", digits=3,
                    expected_login=123456, expected_server="Expected-Server",
                    expected_owned_positions=0,
                )
            )
        command = send.call_args.args[0]
        self.assertTrue(command.endswith("|50|123456|Expected-Server|0"))
        self.assertEqual(executor.last_order_error, "ERR|ACCOUNT_IDENTITY_GUARD")

    def test_open_policy_rejects_every_noncanonical_field_before_bridge_write(self):
        canonical = {
            "symbol": "XAUUSD", "order_type": ORDER_TYPE_BUY, "lot": 0.01,
            "sl": 0.0, "tp": 0.0, "deviation": 50, "magic": 230035,
            "comment": "s23_sv_l1", "digits": 3, "expected_login": 123456,
            "expected_server": "Expected-Server", "expected_owned_positions": 0,
        }
        mutations = (
            {"symbol": "XAUUSD.a"}, {"lot": 0.02}, {"deviation": 51},
            {"magic": 999999}, {"comment": "s23_sv_l2"},
            {"sl": 1999.0}, {"tp": 2001.0},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), patch.object(
                live_executor.ea_bridge, "send_command",
            ) as send:
                result = live_executor.MT5Executor().open_position(
                    **{**canonical, **mutation}
                )
                self.assertIsNone(result)
                send.assert_not_called()

    def test_close_policy_rejects_noncanonical_owner_before_bridge_write(self):
        canonical = {
            "ticket": 7703, "deviation": 50, "expected_login": 123456,
            "expected_server": "Expected-Server", "expected_symbol": "XAUUSD",
            "expected_magic": 230035, "expected_comment": "s23_sv_l1",
            "expected_identifier": 8803,
        }
        mutations = (
            {"expected_symbol": "XAUUSD.a"}, {"expected_magic": 999999},
            {"expected_comment": "s23_sv_l2"}, {"expected_identifier": 0},
            {"expected_server": "Expected-Server\rspoof"},
            {"expected_server": "Expected-Server\nspoof"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), patch.object(
                live_executor.ea_bridge, "send_command",
            ) as send:
                result = live_executor.MT5Executor().close_position(
                    **{**canonical, **mutation}
                )
                self.assertFalse(result)
                self.assertEqual(result.status, "INVALID_REQUEST")
                send.assert_not_called()

    def test_bridge_close_deal_aggregates_all_partial_exit_deals(self):
        source = (Path(__file__).with_name("BotBridge_s23.mq5")).read_text(encoding="utf-8")
        close_deal = source.split('if(op == "CLOSEDEAL"', 1)[1].split('if(op == "MODIFY"', 1)[0]

        self.assertIn("total_exit_volume", close_deal)
        self.assertIn("weighted_exit_price", close_deal)
        self.assertIn("total_profit", close_deal)
        self.assertIn("total_commission", close_deal)
        self.assertIn("total_swap", close_deal)
        self.assertIn("total_fee", close_deal)

    def test_manual_alert_timestamp_is_utc(self):
        stamp = datetime.fromisoformat(live_manual_alerts._utc_now())
        self.assertEqual(stamp.utcoffset(), pd.Timedelta(0))

    def test_query_outage_is_bounded_and_recovery_is_summarized(self):
        bridge = EABridgeServer(files_dir=tempfile.gettempdir())
        with self.assertLogs(level="INFO") as captured:
            bridge._record_command_health("INFO", "ERR|TIMEOUT", 100.0, 110.0)
            bridge._record_command_health("INFO", "ERR|TIMEOUT", 115.0, 125.0)
            bridge._record_command_health("INFO", "ERR|TIMEOUT", 175.0, 185.0)
            bridge._record_command_health("INFO", "OK|1", 190.0, 190.1)
        text = "\n".join(captured.output)
        self.assertEqual(text.count("bridge command failed"), 1)
        self.assertEqual(text.count("bridge command still failing"), 1)
        self.assertIn("outage_seconds=90.100", text)
        self.assertIn("attempts=3", text)
        self.assertIn("last_error=TIMEOUT", text)
        self.assertNotIn("OK|1", text)

    def test_trading_command_error_is_not_reported_as_bridge_outage(self):
        bridge = EABridgeServer(files_dir=tempfile.gettempdir())
        with self.assertNoLogs(level="ERROR"):
            bridge._record_command_health("OPEN", "ERR|10027", 100.0, 100.1)
        self.assertFalse(bridge._command_health)

    def test_trade_schema_preflight_rejects_legacy_header_before_connect(self):
        runner, _strategy, _state = make_runner(live=True)
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as handle:
            handle.write("timestamp_utc,event,strategy_id\n")
            path = handle.name
        try:
            with patch.object(live_s23_bot, "TRADE_LOG_FILE", path):
                self.assertFalse(runner.connect_and_preflight())
        finally:
            os.unlink(path)

    def test_trade_ledger_io_failure_is_a_controlled_preflight_reject(self):
        runner, _strategy, _state = make_runner(live=True)
        with patch.object(
            live_s23_bot, "validate_csv_schema",
            side_effect=OSError("trade ledger permission denied"),
        ), self.assertLogs(level="CRITICAL") as captured:
            self.assertFalse(runner.connect_and_preflight())
        self.assertIn(
            "trade audit schema preflight failed",
            "\n".join(captured.output),
        )

    def test_passive_evaluation_schema_mismatch_does_not_stop_owned_close_only_startup(self):
        runner, _strategy, state = make_runner(live=False)
        runner.params["enabled"] = False
        state["basket"] = [{"shadow": True}]
        runner.dm.connect = lambda: True
        runner.executor = live_s23_bot.FakeExecutor()
        runner._legacy_inventory_error = lambda: None

        def validate(path, _fields):
            if path == live_s23_bot.SIGNAL_EVALUATION_LOG_FILE:
                raise RuntimeError("legacy evaluation header")

        with patch.object(
            live_s23_bot, "validate_csv_schema", side_effect=validate,
        ), self.assertLogs(level="ERROR") as captured:
            self.assertTrue(runner.connect_and_preflight())
        self.assertFalse(runner._signal_evaluation_enabled)
        self.assertIn(
            "passive signal evaluation disabled by schema mismatch",
            "\n".join(captured.output),
        )
        with patch.object(live_s23_bot, "validate_csv_schema", return_value=None):
            self.assertTrue(runner.connect_and_preflight())
        self.assertTrue(runner._signal_evaluation_enabled)

    def test_passive_evaluation_io_failure_does_not_stop_owned_close_only_startup(self):
        runner, _strategy, state = make_runner(live=False)
        runner.params["enabled"] = False
        state["basket"] = [{"shadow": True}]
        runner.dm.connect = lambda: True
        runner.executor = live_s23_bot.FakeExecutor()
        runner._legacy_inventory_error = lambda: None

        def validate(path, _fields):
            if path == live_s23_bot.SIGNAL_EVALUATION_LOG_FILE:
                raise OSError("evaluation ledger permission denied")

        with patch.object(
            live_s23_bot, "validate_csv_schema", side_effect=validate,
        ), self.assertLogs(level="ERROR") as captured:
            self.assertTrue(runner.connect_and_preflight())
        self.assertFalse(runner._signal_evaluation_enabled)
        self.assertIn(
            "passive signal evaluation disabled by schema mismatch",
            "\n".join(captured.output),
        )

    def test_executor_parses_bridge_order_record_shape(self):
        response = "OK|7701,XAUUSD,4,0.01,2000.500,1999.000,2002.000,230035,s23_sv_l1|END,1"
        with patch.object(live_executor.ea_bridge, "send_command", return_value=response):
            orders = live_executor.MT5Executor().get_orders("XAUUSD", 230035)
        self.assertIsNotNone(orders)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].ticket, 7701)
        self.assertEqual(orders[0].magic, 230035)
        self.assertEqual(orders[0].comment, "s23_sv_l1")

    def test_executor_requires_complete_inventory_frame_and_declared_count(self):
        record = "7702,XAUUSD,0,0.01,2000.500,0,0,1.25,230035,1782888600,1782888600123,8802,s23_sv_l1"
        executor = live_executor.MT5Executor()
        for response in (
            "OK",
            f"OK|{record}",
            f"OK|{record}|END,2",
            "OK|END,1",
        ):
            with self.subTest(response=response), patch.object(
                live_executor.ea_bridge, "send_command", return_value=response,
            ):
                self.assertIsNone(executor.get_positions("XAUUSD", 230035))
        with patch.object(
            live_executor.ea_bridge, "send_command", return_value="OK|END,0",
        ):
            self.assertEqual(executor.get_positions("XAUUSD", 230035), [])

    def test_position_record_preserves_broker_fill_milliseconds(self):
        record = "7702,XAUUSD,0,0.01,2000.500,0,0,1.25,230035,1782888600,1782888600123,8802,s23_sv_l1"
        with patch.object(
            live_executor.ea_bridge, "send_command", return_value=f"OK|{record}|END,1",
        ):
            positions = live_executor.MT5Executor().get_positions("XAUUSD", 230035)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].open_time_msc, 1782888600123)

    def test_ea_open_checks_symbol_and_margin_before_broker_call(self):
        source = (Path(__file__).with_name("BotBridge_s23.mq5")).read_text(encoding="utf-8")
        open_block = source.split('if(op == "OPEN"', 1)[1].split('if(op == "PENDING"', 1)[0]
        for guard in ("SYMBOL_TRADE_MODE", "SYMBOL_ORDER_MODE", "OrderCalcMargin", "ACCOUNT_MARGIN_FREE"):
            self.assertIn(guard, open_block)
            self.assertLess(open_block.index(guard), open_block.index("trade.Buy"))

    def test_confirmed_open_persists_owned_state_before_csv_audit(self):
        source = (Path(__file__).with_name("live_s23_bot.py")).read_text(encoding="utf-8")
        marker = "Broker-confirmed lifecycle state is authoritative"
        block = source.split(marker, 1)[1].split("return True", 1)[0]
        self.assertLess(block.index("self._save_state()"), block.index("self._trade_row("))

    def test_main_holds_os_runner_lock_before_runtime_construction(self):
        source = (Path(__file__).with_name("live_s23_bot.py")).read_text(encoding="utf-8")
        main_block = source.split("def main()", 1)[1]
        self.assertLess(
            main_block.index("acquire_runner_singleton_lock()"),
            main_block.index("S23HorizontalInventoryRunner(params)"),
        )

    def test_bridge_ignores_wrong_request_response_and_accepts_correlated_complete_one(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = EABridgeServer(bot_suffix="s23", files_dir=directory)

            def responder():
                deadline = time.time() + 2.0
                while time.time() < deadline and not os.path.exists(bridge.cmd_file):
                    time.sleep(0.01)
                envelope = Path(bridge.cmd_file).read_text(encoding="utf-8")
                request_id = envelope.split("|", 3)[1]
                Path(bridge.res_file).write_text(
                    "RES|wrong-request|OK|stale|ENDRES", encoding="utf-8",
                )
                time.sleep(0.05)
                Path(bridge.res_file).write_text(
                    f"RES|{request_id}|OK|Alive|ENDRES", encoding="utf-8",
                )

            worker = threading.Thread(target=responder)
            worker.start()
            result = bridge.send_command("ECHO|", timeout=1.0)
            worker.join(timeout=2.0)
            self.assertEqual(result, "OK|Alive")

    def test_bridge_timeout_leaves_published_request_for_ea_expiry_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = EABridgeServer(bot_suffix="s23", files_dir=directory)
            Path(bridge.lock_file).write_text("stale-path-entry", encoding="utf-8")
            result = bridge.send_command("ECHO|", timeout=0.15)
            self.assertEqual(result, "ERR|TIMEOUT")
            self.assertTrue(os.path.exists(bridge.cmd_file))
            self.assertTrue(Path(bridge.cmd_file).read_text(encoding="utf-8").startswith("REQ|"))

    def test_bridge_local_wait_deadlines_use_monotonic_clock(self):
        source = (Path(__file__).with_name("ea_bridge.py")).read_text(encoding="utf-8")
        acquire = source.split("def _acquire_ipc_lock", 1)[1].split("def _release_ipc_lock", 1)[0]
        send = source.split("def send_command", 1)[1]
        self.assertIn("time.monotonic() + timeout", acquire)
        self.assertIn("while time.monotonic() < deadline", acquire)
        self.assertIn("wait_deadline = time.monotonic() + timeout", send)
        self.assertIn("while time.monotonic() < wait_deadline", send)

    def test_bridge_rejects_invalid_timeout_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = EABridgeServer(bot_suffix="s23", files_dir=directory)
            for timeout in (True, 0, -1, math.nan, math.inf, 301, "10"):
                with self.subTest(timeout=timeout):
                    self.assertEqual(
                        bridge.send_command("ECHO|", timeout=timeout),
                        "ERR|INVALID_TIMEOUT",
                    )
                    self.assertFalse(Path(bridge.cmd_file).exists())
                    self.assertFalse(Path(bridge.claim_file).exists())

    def test_bridge_never_overwrites_an_undrained_command_or_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = EABridgeServer(bot_suffix="s23", files_dir=directory)
            Path(bridge.cmd_file).write_text("REQ|older|1|OPEN|old", encoding="utf-8")
            self.assertEqual(bridge.send_command("ECHO|", timeout=0.15), "ERR|COMMAND_BUSY")
            self.assertEqual(Path(bridge.cmd_file).read_text(encoding="utf-8"), "REQ|older|1|OPEN|old")
            Path(bridge.cmd_file).unlink()
            Path(bridge.claim_file).write_text("REQ|claimed|1|CLOSE|old", encoding="utf-8")
            self.assertEqual(bridge.send_command("ECHO|", timeout=0.15), "ERR|CLAIM_BUSY")
            self.assertEqual(Path(bridge.claim_file).read_text(encoding="utf-8"), "REQ|claimed|1|CLOSE|old")

    def test_bridge_does_not_publish_behind_an_undeletable_response(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = EABridgeServer(bot_suffix="s23", files_dir=directory)
            Path(bridge.res_file).mkdir()
            self.assertEqual(bridge.send_command("ECHO|", timeout=0.15), "ERR|RESPONSE_BUSY")
            self.assertFalse(Path(bridge.cmd_file).exists())

    def test_ny0530_mtm_mdd_applies_explicit_lot_contract_multiplier(self):
        research_root = Path(
            os.environ.get("BOTTER_RESEARCH_ROOT", str(Path(__file__).parents[2]))
        )
        runner_path = (
            research_root / "backtest" / "output" / "backtest227" /
            "candidates" / "xau-ny0530-0830-structural-screen-v001" /
            "run_xau-ny0530-0830-structural-screen-v001.py"
        )
        if not runner_path.is_file():
            self.skipTest(
                "external backtest227 source is unavailable; set BOTTER_RESEARCH_ROOT "
                "to a checkout that contains the research artifact"
            )
        source = runner_path.read_text(encoding="utf-8")
        function = source.split("def mtm_mdd_scan", 1)[1].split("def ", 1)[0]
        self.assertIn("value_multiplier", function)
        self.assertIn("unrealized_price_delta * value_multiplier", function)
        simulate = source.split("def simulate", 1)[1].split("def mtm_mdd_scan", 1)[0]
        self.assertIn("effective_maximum_entry_delay_seconds()", simulate)
        self.assertIn("raw_entry_spread_points > MAX_ENTRY_SPREAD_POINTS", simulate)
        self.assertIn('"effective_maximum_entry_delay_seconds"', source)
        self.assertIn('"max_entry_spread_points"', source)
        self.assertIn("CONFIG = load_candidate_config(CONFIG_PATH)", source)
        self.assertIn("parse_constant=_reject_config_constant", source)
        self.assertIn(".incomplete-", source)
        self.assertIn("args.run_dir.replace(final_run_dir)", source)

        live_contract_source = runner_path.with_name("run_session_vwap_live_contract_dev.py").read_text(encoding="utf-8")
        self.assertIn("parent.base.load_candidate_config(args.config)", live_contract_source)
        self.assertIn(".incomplete-", live_contract_source)
        self.assertIn("args.run_dir.replace(final_run_dir)", live_contract_source)

    def test_ea_claims_correlated_request_durably_and_disables_unused_mutations(self):
        source = (Path(__file__).with_name("BotBridge_s23.mq5")).read_text(encoding="utf-8")
        timer = source.split("void OnTimer()", 1)[1]
        clear_command = source.split("void ClearCommand()", 1)[1].split("string ReadClaim()", 1)[0]
        self.assertIn("FileDelete(InpCommandFile)", clear_command)
        self.assertNotIn("FileOpen", clear_command)
        self.assertIn("ReadClaim()", timer)
        self.assertIn("WriteClaim(envelope)", timer)
        self.assertLess(timer.index("WriteClaim(envelope)"), timer.index("HandleCommand(command)"))
        self.assertIn('"RES|" + request_id', timer)
        self.assertIn("GlobalVariableSetOnCondition", source)
        self.assertIn('if(op == "PENDING" || op == "MODIFY" || op == "CANCEL")', source)
        commands_line = next(line for line in source.splitlines() if "#define BRIDGE_COMMANDS" in line)
        for command in ("PENDING", "MODIFY", "CANCEL"):
            self.assertNotIn(command, commands_line)

    def test_ea_durable_claim_replay_is_idempotent_and_expired_open_is_rejected(self):
        source = (Path(__file__).with_name("BotBridge_s23.mq5")).read_text(encoding="utf-8")
        open_block = source.split('if(op == "OPEN"', 1)[1].split('if(op == "PENDING"', 1)[0]
        policy_block = source.split("bool IsCanonicalOpenPolicy", 1)[1].split("bool IsPendingPlaced", 1)[0]
        timer = source.split("void OnTimer()", 1)[1]
        self.assertIn("expected_owned_positions", open_block)
        self.assertIn("if(n != 12 || !ValidOpenNumericFields(parts))", open_block)
        self.assertIn("sl == 0.0", policy_block)
        self.assertIn("tp == 0.0", policy_block)
        self.assertIn('POSITION_COMMENT) != comment', open_block)
        self.assertLess(open_block.index("owned_positions != expected_owned_positions"), open_block.index("trade.Buy"))
        self.assertIn("recovered_open", timer)
        self.assertIn("if(recovered_open)", timer)
        self.assertIn("ERR|OPEN_RESULT_UNRESOLVED", timer)
        self.assertIn("((long)TimeGMT()) >= deadline_msc / 1000", timer)
        self.assertNotIn("((long)TimeGMT()) * 1000 > deadline_msc", timer)
        self.assertIn("FileIsExist(InpCommandFile)", timer)
        self.assertLess(timer.index("FileIsExist(InpCommandFile)"), timer.index("HandleCommand(command)"))
        write_response = source.split("bool WriteResponse", 1)[1].split("string PositionRecord", 1)[0]
        self.assertIn("uint written = FileWriteString", write_response)
        self.assertIn("observed == response", write_response)
        recovered_open_block = timer.split("if(recovered_open)", 1)[1].split("if(request_expired", 1)[0]
        self.assertNotIn("HandleCommand", recovered_open_block)
        expired_block = timer.split("if(request_expired && !recovered_claim)", 1)[1].split("if(!recovered_claim)", 1)[0]
        self.assertLess(expired_block.index("ClearCommand()"), expired_block.index("FileIsExist(InpCommandFile)"))
        self.assertLess(expired_block.index("FileIsExist(InpCommandFile)"), expired_block.index("ERR|REQUEST_EXPIRED"))
        self.assertIn('if(WriteResponse("RES|" + request_id + "|" + HandleCommand(command) + "|ENDRES"))', timer)
        self.assertIn("if(!recovered_claim)\n         ClearCommand();", timer)
        claim_failed = timer.split("if(!WriteClaim(envelope)", 1)[1].split("}\n      ClearCommand()", 1)[0]
        self.assertLess(claim_failed.index("ClearCommand()"), claim_failed.index("FileIsExist(InpCommandFile)"))
        self.assertLess(claim_failed.index("FileIsExist(InpCommandFile)"), claim_failed.index("ClearClaim()"))
        self.assertLess(claim_failed.index("ClearClaim()"), claim_failed.index("FileIsExist(InpClaimFile)"))
        self.assertLess(claim_failed.index("FileIsExist(InpClaimFile)"), claim_failed.index('WriteResponse("RES|"'))

    def test_ea_consumer_owner_cannot_be_stolen_on_a_heartbeat_timeout(self):
        source = (Path(__file__).with_name("BotBridge_s23.mq5")).read_text(encoding="utf-8")
        acquire = source.split("bool AcquireConsumerOwnership()", 1)[1].split("bool OwnsConsumerNamespace()", 1)[0]
        self.assertIn("if(observed != 0.0)\n      return false;", acquire)
        self.assertNotIn("heartbeat <=", acquire)
    def test_executor_accepts_only_exact_position_not_found_as_absence_proof(self):
        executor = live_executor.MT5Executor()
        with patch.object(
            live_executor.ea_bridge,
            "send_command",
            return_value="ERR|POSITION_NOT_FOUND",
        ):
            self.assertIs(executor.get_position(7701), False)

        for response in ("ERR|10009", "ERR|0", "ERR|Position Not Found"):
            with self.subTest(response=response), patch.object(
                live_executor.ea_bridge,
                "send_command",
                return_value=response,
            ):
                self.assertIsNone(executor.get_position(7701))

    def test_executor_rejects_invalid_position_type(self):
        response = "OK|7702,XAUUSD,99,0.01,2000.500,0,0,1.25,230035,1782888600,1782888600123,8802,s23_sv_l1"
        with patch.object(live_executor.ea_bridge, "send_command", return_value=response):
            self.assertIsNone(live_executor.MT5Executor().get_positions("XAUUSD", 230035))

    def test_executor_rejects_extended_fixed_schema_frames(self):
        executor = live_executor.MT5Executor()
        cases = (
            ("caps", "OK|CAPS|BotBridge_s23|version|ECHO,CAPS|extra", executor.get_bridge_capabilities),
            ("account", "OK|2|RETAIL_HEDGING|1|1|1|1|123456|Expected-Server|USD|extra", executor.get_account_info),
            ("info", "OK|2000.030|2000.000|1000|0.001|0.01|100.0|0.01|0.1|0.001|100|3|0|1782888600123|4|1|extra", lambda: executor.get_symbol_info("XAUUSD")),
            ("position", "OK|7702,XAUUSD,0,0.01,2000.500,0,0,1.25,230035,1782888600,1782888600123,8802,s23_sv_l1,foreign|END,1", lambda: executor.get_positions("XAUUSD", 230035)),
            ("order", "OK|7701,XAUUSD,4,0.01,2000.500,0,0,230035,s23_sv_l1,foreign|END,1", lambda: executor.get_orders("XAUUSD", 230035)),
        )
        for name, response, call in cases:
            with self.subTest(name=name), patch.object(
                live_executor.ea_bridge, "send_command", return_value=response,
            ):
                self.assertIsNone(call())

    def test_executor_rejects_noncanonical_or_duplicate_capability_tokens(self):
        responses = (
            "OK|CAPS|BotBridge_s23|version|ECHO, CAPS",
            "OK|CAPS|BotBridge_s23|version|ECHO,caps",
            "OK|CAPS|BotBridge_s23|version|ECHO,ECHO",
        )
        for response in responses:
            with self.subTest(response=response), patch.object(
                live_executor.ea_bridge, "send_command", return_value=response,
            ):
                self.assertIsNone(live_executor.MT5Executor().get_bridge_capabilities())

    def test_executor_does_not_normalize_inventory_identity_fields(self):
        responses = (
            "OK|7702,XAUUSD,0,0.01,2000.500,0,0,1.25,230035,1782888600,1782888600123,8802,s23_sv_l1 |END,1",
            "OK| 7702,XAUUSD,0,0.01,2000.500,0,0,1.25,230035,1782888600,1782888600123,8802,s23_sv_l1|END,1",
            "OK|7701,XAUUSD,4,0.01,2000.500,0,0,230035,s23_sv_l1 |END,1",
        )
        for response in responses:
            with self.subTest(response=response), patch.object(
                live_executor.ea_bridge, "send_command", return_value=response,
            ):
                if ",4," in response:
                    self.assertIsNone(live_executor.MT5Executor().get_orders("XAUUSD", 230035))
                else:
                    self.assertIsNone(live_executor.MT5Executor().get_positions("XAUUSD", 230035))

    def test_executor_rejects_legacy_position_record_without_identifier(self):
        response = "OK|7702,XAUUSD,0,0.01,2000.500,0,0,1.25,230035,1782888600,8802,s23_sv_l1"
        with patch.object(live_executor.ea_bridge, "send_command", return_value=response):
            self.assertIsNone(live_executor.MT5Executor().get_positions("XAUUSD", 230035))

    def test_executor_does_not_accept_malformed_close_as_confirmed(self):
        response = "OK|7703|0.01|2000.500|0|1.25|0|10009"
        with patch.object(live_executor.ea_bridge, "send_command", return_value=response):
            result = live_executor.MT5Executor().close_position(
                7703, 50, expected_login=123456, expected_server="Expected-Server",
                expected_symbol="XAUUSD", expected_magic=230035,
                expected_comment="s23_sv_l1", expected_identifier=8803,
            )
        self.assertFalse(result)
        self.assertEqual(result.status, "MALFORMED_OK")

    def test_executor_rejects_trailing_fields_in_mutation_confirmations(self):
        executor = live_executor.MT5Executor()
        with patch.object(
            live_executor.ea_bridge,
            "send_command",
            return_value="OK|7703|501|2000.500|10009|unexpected",
        ):
            ticket = executor.open_position(
                "XAUUSD", ORDER_TYPE_BUY, 0.01, 0.0, 0.0,
                deviation=50, magic=230035, comment="s23_sv_l1", digits=3,
                expected_login=123456, expected_server="Expected-Server",
                expected_owned_positions=0,
            )
        self.assertIsNone(ticket)
        self.assertTrue(str(executor.last_order_error).startswith("MALFORMED_OK:"))

        with patch.object(
            live_executor.ea_bridge,
            "send_command",
            return_value="OK|7703|0.01|2000.500|2001.000|0.50|501|10009|unexpected",
        ):
            result = executor.close_position(
                7703, 50, expected_login=123456, expected_server="Expected-Server",
                expected_symbol="XAUUSD", expected_magic=230035,
                expected_comment="s23_sv_l1", expected_identifier=8803,
            )
        self.assertFalse(result)
        self.assertEqual(result.status, "MALFORMED_OK")

    def test_executor_marks_only_prepublication_close_ipc_errors_definitive(self):
        executor = live_executor.MT5Executor()
        unpublished = (
            "ERR|COMMAND_BUSY", "ERR|CLAIM_BUSY", "ERR|LOCK_TIMEOUT",
            "ERR|WRITE_FAILED", "ERR|CLAIM_FAILED", "ERR|REQUEST_EXPIRED",
            "ERR|RESPONSE_BUSY",
        )
        for response in unpublished:
            with self.subTest(response=response), patch.object(
                live_executor.ea_bridge, "send_command", return_value=response,
            ):
                result = executor.close_position(
                    7703, 50, expected_login=123456, expected_server="Expected-Server",
                    expected_symbol="XAUUSD", expected_magic=230035,
                    expected_comment="s23_sv_l1", expected_identifier=8803,
                )
                self.assertFalse(result)
                self.assertEqual(result.status, "IPC_NOT_PUBLISHED")
                self.assertTrue(
                    live_s23_bot.S23HorizontalInventoryRunner._close_result_definitive_no_fill(result)
                )

        with patch.object(
            live_executor.ea_bridge,
            "send_command",
            return_value="ERR|COMMAND_CLEAR_FAILED",
        ):
            ambiguous = executor.close_position(
                7703, 50, expected_login=123456, expected_server="Expected-Server",
                expected_symbol="XAUUSD", expected_magic=230035,
                expected_comment="s23_sv_l1", expected_identifier=8803,
            )
        self.assertEqual(ambiguous.status, "FAILED")
        self.assertFalse(
            live_s23_bot.S23HorizontalInventoryRunner._close_result_definitive_no_fill(ambiguous)
        )

    def test_executor_rejects_nonfinite_symbol_quote(self):
        response = "OK|nan|2000.000|1000|0.001|0.01|100.0|0.01|0.1|0.001|100|3|0|1782888600123|4|1"
        with patch.object(live_executor.ea_bridge, "send_command", return_value=response):
            self.assertIsNone(live_executor.MT5Executor().get_symbol_info("XAUUSD"))

    def test_executor_rejects_runtime_info_without_broker_quote_timestamp(self):
        response = "OK|2000.030|2000.000|1000|0.001|0.01|100.0|0.01|0.1|0.001|100|3|0"
        with patch.object(live_executor.ea_bridge, "send_command", return_value=response):
            self.assertIsNone(live_executor.MT5Executor().get_symbol_info("XAUUSD"))

    def test_executor_rejects_fractional_integer_execution_receipts(self):
        executor = live_executor.MT5Executor()
        cases = (
            (
                "open",
                "OK|7703.0|501|2000.500|10009",
                lambda: executor.open_position(
                    "XAUUSD", 0, 0.01, 0.0, 0.0,
                    deviation=50, magic=230035, comment="s23_sv_l1", digits=3,
                    expected_login=123456, expected_server="Expected-Server",
                    expected_owned_positions=0,
                ),
            ),
            (
                "close",
                "OK|7703.0|0.01|2000.500|2001.000|0.50|501|10009",
                lambda: executor.close_position(
                    7703, 50, expected_login=123456, expected_server="Expected-Server",
                    expected_symbol="XAUUSD", expected_magic=230035,
                    expected_comment="s23_sv_l1", expected_identifier=8803,
                ),
            ),
            (
                "close_deal",
                "OK|FOUND|501.0|7703|XAUUSD|230035|client|2001.000|0.50|0|0|0|1782888600|0.01",
                lambda: executor.get_position_close_deal(7703, 1782880000),
            ),
        )
        for name, response, call in cases:
            with self.subTest(name=name), patch.object(
                live_executor.ea_bridge, "send_command", return_value=response,
            ):
                result = call()
                if name == "close":
                    self.assertFalse(result)
                    self.assertEqual(result.status, "MALFORMED_OK")
                else:
                    self.assertIsNone(result)

    def test_executor_rejects_signed_or_padded_positive_integer_receipts(self):
        executor = live_executor.MT5Executor()
        for ticket in ("+7703", " 7703"):
            response = f"OK|{ticket}|501|2000.500|10009"
            with self.subTest(ticket=ticket), patch.object(
                live_executor.ea_bridge, "send_command", return_value=response,
            ):
                self.assertIsNone(
                    executor.open_position(
                        "XAUUSD", 0, 0.01, 0.0, 0.0,
                        deviation=50, magic=230035, comment="s23_sv_l1", digits=3,
                        expected_login=123456, expected_server="Expected-Server",
                        expected_owned_positions=0,
                    )
                )

    def test_executor_rejects_fractional_integer_symbol_contract_fields(self):
        for digits, stops in (("3.5", "0"), ("3", "0.5")):
            with self.subTest(digits=digits, stops=stops):
                response = (
                    "OK|2000.030|2000.000|1000|0.001|0.01|100.0|0.01|"
                    f"0.1|0.001|100|{digits}|{stops}|1782888600123|4|1"
                )
                with patch.object(live_executor.ea_bridge, "send_command", return_value=response):
                    self.assertIsNone(live_executor.MT5Executor().get_symbol_info("XAUUSD"))

    def test_executor_rejects_fractional_position_open_epoch(self):
        response = "OK|7702,XAUUSD,0,0.01,2000.500,0,0,1.25,230035,1782888600.5,1782888600123,8802,s23_sv_l1"
        with patch.object(live_executor.ea_bridge, "send_command", return_value=response):
            self.assertIsNone(live_executor.MT5Executor().get_positions("XAUUSD", 230035))

    def test_executor_rejects_nonfinite_open_confirmation_payload(self):
        response = "OK|7704|9904|nan|10009"
        executor = live_executor.MT5Executor()
        with patch.object(live_executor.ea_bridge, "send_command", return_value=response):
            ticket = executor.open_position(
                "XAUUSD", ORDER_TYPE_BUY, 0.01, 0.0, 0.0,
                deviation=50, magic=230035, comment="s23_sv_l1", digits=3,
                expected_login=123456, expected_server="Expected-Server",
                expected_owned_positions=0,
            )
        self.assertIsNone(ticket)
        self.assertTrue(str(executor.last_order_error).startswith("MALFORMED_OK:"))

    def test_executor_rejects_legacy_close_deal_without_aggregate_exit_volume(self):
        response = "OK|FOUND|79640|9401|XAUUSD|230031|DEAL_REASON_CLIENT|99.0|-1.0|0|0|0|1782888600"
        with patch.object(live_executor.ea_bridge, "send_command", return_value=response):
            self.assertIsNone(live_executor.MT5Executor().get_position_close_deal(9401, 1))

    def test_executor_rejects_extended_close_deal_frames(self):
        responses = (
            "OK|NONE|unexpected",
            "OK|FOUND|79642|9401|XAUUSD|230031|DEAL_REASON_CLIENT|99.25|-1.0|-0.1|0|0|1782888600|0.0100000000|unexpected",
        )
        for response in responses:
            with self.subTest(response=response), patch.object(
                live_executor.ea_bridge, "send_command", return_value=response,
            ):
                self.assertIsNone(
                    live_executor.MT5Executor().get_position_close_deal(9401, 1)
                )

    def test_executor_parses_aggregate_close_deal_volume_and_net_result(self):
        response = "OK|FOUND|79642|9401|XAUUSD|230031|DEAL_REASON_CLIENT|99.25|-1.0|-0.1|0|0|1782888600|0.0100000000"
        with patch.object(live_executor.ea_bridge, "send_command", return_value=response):
            deal = live_executor.MT5Executor().get_position_close_deal(9401, 1)

        self.assertEqual(deal.exit_volume, 0.01)
        self.assertAlmostEqual(deal.net_profit, -1.1)

    def test_executor_rejects_invalid_open_request_before_bridge_write(self):
        executor = live_executor.MT5Executor()
        with patch.object(live_executor.ea_bridge, "send_command") as send:
            ticket = executor.open_position(
                "XAUUSD", ORDER_TYPE_BUY, math.nan, 0.0, 0.0,
                deviation=50, magic=230035, comment="s23_sv_l1", digits=3,
                expected_login=123456, expected_server="Expected-Server",
                expected_owned_positions=0,
            )
        self.assertIsNone(ticket)
        self.assertEqual(executor.last_order_error, "INVALID_OPEN_REQUEST")
        send.assert_not_called()


class RecordingObserver:
    enabled = True

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = []

    def _record(self, method: str, kwargs: dict):
        if self.fail:
            raise RuntimeError("observer_write_failed")
        self.calls.append((method, dict(kwargs)))

    def observe_quote(self, **kwargs):
        self._record("observe_quote", kwargs)

    def register_opportunity(self, **kwargs):
        self._record("register_opportunity", kwargs)

    def record_route(self, **kwargs):
        self._record("record_route", kwargs)


def make_runner(*, live: bool = True) -> tuple[S23HorizontalInventoryRunner, dict, dict]:
    params = json.loads(json.dumps(load_params()))
    params["live_trading_enabled"] = live
    params["shadow_forward_enabled"] = not live
    params["shadow_opportunity_observer"]["enabled"] = False
    params["shadow_state_tagger"]["enabled"] = False
    params["midday_shadow_opportunity_observer"]["enabled"] = False
    params["midday_shadow_state_tagger"]["enabled"] = False
    params["pre_eu30_shadow_opportunity_observer"]["enabled"] = False
    params["pre_eu30_shadow_state_tagger"]["enabled"] = False
    with patch.object(live_s23_bot.os.path, "exists", return_value=False):
        runner = S23HorizontalInventoryRunner(params)
    runner.state = runner._default_state()
    runner._suppress_manual_alerts = True
    runner._save_state = lambda: None
    runner._trade_row = lambda *_args, **_kwargs: None
    strategy = params["strategies"][0]
    return runner, strategy, runner._st(strategy)


class Bot23Q01VarianceReleaseRegressionTests(unittest.TestCase):
    def test_pre_q01_state_migration_preserves_existing_lane_state(self):
        seed, _strategy, _state = make_runner(live=True)
        legacy = seed._default_state()
        legacy["routing"].pop("q01_policy_id")
        legacy["routing"].pop("q01_params_hash")
        legacy["routing"].pop("q01_last_evaluated_m5_bar")
        q01_id = seed._q01_strategies()[0]["id"]
        legacy["strategies"].pop(q01_id)
        preserved_id = seed.params["strategies"][0]["id"]
        legacy["strategies"][preserved_id]["cooldown_until_bar"] = 12345
        params = json.loads(json.dumps(seed.params))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(legacy, handle)
            state_path = handle.name
        try:
            with patch.object(live_s23_bot, "STATE_FILE", state_path):
                migrated = S23HorizontalInventoryRunner(params)
            self.assertTrue(migrated._q01_state_migrated)
            self.assertEqual(migrated.state["strategies"][preserved_id]["cooldown_until_bar"], 12345)
            self.assertIn(q01_id, migrated.state["strategies"])
            self.assertEqual(migrated.state["strategies"][q01_id]["basket"], [])
            self.assertIsNone(migrated.state["strategies"][q01_id]["q01_retry_opportunity"])
        finally:
            os.unlink(state_path)

    def test_current_q01_state_missing_required_clock_fails_closed(self):
        seed, _strategy, _state = make_runner(live=True)
        malformed = seed._default_state()
        q01_id = seed._q01_strategies()[0]["id"]
        malformed["strategies"][q01_id].pop("q01_last_quote_msc")
        params = json.loads(json.dumps(seed.params))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(malformed, handle)
            state_path = handle.name
        try:
            with patch.object(live_s23_bot, "STATE_FILE", state_path):
                rejected = S23HorizontalInventoryRunner(params)
            self.assertTrue(
                all(
                    lane_state["sync_block_new_entries"]
                    and lane_state["sync_block_reason"] == "state_identity_mismatch"
                    for lane_state in rejected.state["strategies"].values()
                )
            )
        finally:
            os.unlink(state_path)

    def test_q01_topology_is_independent_lane_22(self):
        params = json.loads(json.dumps(load_params()))
        self.assertEqual(
            params["candidate_id"],
            "bot23-integrated-session-vwap-on-t0530-edge-on-q01-v008",
        )
        self.assertEqual(params["candidate_id"], live_s23_bot.EXPECTED_CANDIDATE_ID)
        self.assertFalse(params["q01_live_trading_enabled"])
        q01 = params["q01_variance_release_strategies"]
        self.assertEqual([row["lane_id"] for row in q01], [22])
        self.assertEqual([row["magic"] for row in q01], [230044])
        self.assertEqual([row["comment_prefix"] for row in q01], ["s23_q01_l1"])
        previous_magics = set(
            params["expected_magics"]
            + params["expected_morning_magics"]
            + params["expected_midday_magics"]
            + params["expected_pre_eu30_magics"]
            + params["expected_trend_recovery_magics"]
            + params["expected_session_vwap_magics"]
            + params["expected_t0530_edge_magics"]
        )
        self.assertTrue(previous_magics.isdisjoint(params["expected_q01_magics"]))

    def test_q01_waits_for_completed_m5_release(self):
        runner, _strategy, _state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        bars = pd.DataFrame(
            {"Open": [2000.0] * 10, "High": [2001.0] * 10, "Low": [1999.0] * 10, "Close": [2000.5] * 10},
            index=pd.date_range("2026-01-01T00:00:00Z", periods=10, freq="1min"),
        )
        runner._process_q01_entries(
            bars.iloc[:4],
            SimpleNamespace(
                bid=2000.5,
                ask=2000.6,
                quote_time_msc=int(pd.Timestamp("2026-01-01T00:04:30Z").timestamp() * 1000),
            ),
            pd.Timestamp("2026-01-01T00:04:30Z"),
            {22: True},
        )
        self.assertEqual(executor.open_calls, 0)

    def test_q01_consumes_one_fresh_completed_m5_signal(self):
        runner, _strategy, _state = make_runner(live=True)
        rows = 65
        bars = pd.DataFrame(
            {
                "Open": [2000.0] * rows,
                "High": [2001.0] * rows,
                "Low": [1999.0] * rows,
                "Close": [2000.5] * rows,
            },
            index=pd.date_range("2026-01-01T00:00:00Z", periods=rows, freq="1min"),
        )
        q01_state = runner._st(runner._q01_strategies()[0])
        def consume_open(*_args, **_kwargs):
            q01_state["q01_retry_opportunity"] = None
            return True
        with patch.object(runner, "_q01_signal_side", return_value=("LONG", 1.5)), patch.object(runner, "_attempt_q01_open", side_effect=consume_open) as opened:
            runner._process_q01_entries(
                bars.iloc[:65],
                SimpleNamespace(
                    bid=2000.5,
                    ask=2000.6,
                    quote_time_msc=int(pd.Timestamp("2026-01-01T01:05:01Z").timestamp() * 1000),
                ),
                pd.Timestamp("2026-01-01T01:05:01Z"),
                {22: True},
            )
            runner._process_q01_entries(
                bars.iloc[:65],
                SimpleNamespace(
                    bid=2000.5,
                    ask=2000.6,
                    quote_time_msc=int(pd.Timestamp("2026-01-01T01:05:02Z").timestamp() * 1000),
                ),
                pd.Timestamp("2026-01-01T01:05:02Z"),
                {22: True},
            )
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(runner.state["routing"]["q01_last_evaluated_m5_bar"], "2026-01-01T01:00:00+00:00")

    def test_q01_delayed_poll_processes_next_unseen_m5_in_order(self):
        runner, _strategy, _state = make_runner(live=True)
        rows = 72
        bars = pd.DataFrame(
            {"Open": [2000.0] * rows, "High": [2001.0] * rows, "Low": [1999.0] * rows, "Close": [2000.5] * rows},
            index=pd.date_range("2026-01-01T00:00:00Z", periods=rows, freq="1min"),
        )
        runner.state["routing"]["q01_last_evaluated_m5_bar"] = "2026-01-01T00:55:00+00:00"
        quote_time = pd.Timestamp("2026-01-01T01:11:30Z")
        with patch.object(runner, "_q01_signal_side", return_value=(None, 1.0)) as signal:
            runner._process_q01_entries(
                bars,
                SimpleNamespace(bid=2000.5, ask=2000.6, quote_time_msc=int(quote_time.timestamp() * 1000)),
                quote_time,
                {22: True},
            )
        self.assertEqual(signal.call_args.args[1], pd.Timestamp("2026-01-01T01:00:00Z"))
        self.assertEqual(runner.state["routing"]["q01_last_evaluated_m5_bar"], "2026-01-01T01:00:00+00:00")

    def test_q01_delayed_poll_skips_nonexistent_m5_intervals(self):
        runner, _strategy, _state = make_runner(live=True)
        index = pd.date_range("2026-01-01T00:00:00Z", periods=60, freq="1min").append(
            pd.date_range("2026-01-01T02:00:00Z", periods=10, freq="1min")
        )
        bars = pd.DataFrame(
            {"Open": 2000.0, "High": 2001.0, "Low": 1999.0, "Close": 2000.5},
            index=index,
        )
        runner.state["routing"]["q01_last_evaluated_m5_bar"] = "2026-01-01T00:55:00+00:00"
        quote_time = pd.Timestamp("2026-01-01T02:10:01Z")
        with patch.object(runner, "_q01_signal_side", return_value=(None, 1.0)) as signal:
            runner._process_q01_entries(
                bars,
                SimpleNamespace(bid=2000.5, ask=2000.6, quote_time_msc=int(quote_time.timestamp() * 1000)),
                quote_time,
                {22: True},
            )
        self.assertEqual(signal.call_args.args[1], pd.Timestamp("2026-01-01T02:00:00Z"))
        self.assertEqual(runner.state["routing"]["q01_last_evaluated_m5_bar"], "2026-01-01T02:00:00+00:00")

    def test_q01_signal_formula_matches_frozen_shifted_dev_definition(self):
        index = pd.date_range("2026-01-01T00:00:00Z", periods=160, freq="5min")
        close = pd.Series(
            [2000.0 + index_value * 0.05 + math.sin(index_value * 0.7) * 2.0 for index_value in range(len(index))],
            index=index,
        )
        m5 = pd.DataFrame(
            {
                "Open": close.shift(1).fillna(close.iloc[0]),
                "High": close + 0.2,
                "Low": close - 0.2,
                "Close": close,
            },
            index=index,
        )
        one = close.diff()
        expected_vr = close.diff(4).shift(1).rolling(48).var() / (4 * one.shift(1).rolling(48).var())
        expected_high = m5["High"].shift(1).rolling(12).max()
        expected_low = m5["Low"].shift(1).rolling(12).min()
        for position in range(110, len(index)):
            signal_bar = index[position]
            side, vr_value = S23HorizontalInventoryRunner._q01_signal_side(
                m5,
                signal_bar,
                horizon=4,
                window=48,
                threshold=-math.inf,
                breakout=12,
                warmup=110,
                atr_period=20,
            )
            self.assertAlmostEqual(vr_value, float(expected_vr.iloc[position]), places=12)
            expected_side = (
                "LONG"
                if close.iloc[position] > expected_high.iloc[position]
                else "SHORT"
                if close.iloc[position] < expected_low.iloc[position]
                else None
            )
            self.assertEqual(side, expected_side)

    def test_q01_signal_rejects_before_frozen_110_bar_warmup(self):
        index = pd.date_range("2026-01-01T00:00:00Z", periods=110, freq="5min")
        close = pd.Series([2000.0 + math.sin(value) for value in range(110)], index=index)
        m5 = pd.DataFrame({"Open": close, "High": close + 0.1, "Low": close - 0.1, "Close": close})
        side, vr_value = S23HorizontalInventoryRunner._q01_signal_side(
            m5,
            index[-1],
            horizon=4,
            window=48,
            threshold=-math.inf,
            breakout=12,
            warmup=110,
            atr_period=20,
        )
        self.assertIsNone(side)
        self.assertIsNone(vr_value)

    def test_q01_requests_enough_m1_history_without_changing_legacy_contract(self):
        runner, _strategy, _state = make_runner(live=False)
        requested = []
        index = pd.date_range("2026-01-01T00:00:00Z", periods=600, freq="1min")
        bars = pd.DataFrame(
            {"Open": 2000.0, "High": 2000.2, "Low": 1999.8, "Close": 2000.0, "Volume": 1.0},
            index=index,
        )
        runner.dm.get_historical_data = lambda _symbol, _timeframe, count, *_args, **_kwargs: (requested.append(count) or bars)
        self.assertIsNotNone(runner._get_m1())
        self.assertEqual(requested, [600])
        self.assertEqual(runner.params["m1_bars"], 420)

    @staticmethod
    def _arm_q01_basket(runner, *, entry_time="2026-01-01T00:00:00+00:00"):
        strat = runner._q01_strategies()[0]
        state = runner._st(strat)
        state["basket_sequence"] = 1
        state["current_basket_id"] = "L22-B000001"
        state["basket"] = [
            {
                "side": "LONG",
                "lot": 0.01,
                "entry_price": 2000.0,
                "entry_time_utc": entry_time,
                "basket_id": "L22-B000001",
            }
        ]
        return strat, state

    def test_q01_feed_gap_closes_on_first_arrival_quote_even_when_spread_is_wide(self):
        runner, _strategy, _state = make_runner(live=True)
        strat, state = self._arm_q01_basket(runner)
        previous = pd.Timestamp("2026-01-01T00:10:00Z")
        arrival = previous + pd.Timedelta(seconds=301)
        state["q01_last_quote_msc"] = int(previous.timestamp() * 1000)
        with patch.object(runner, "_close_basket", return_value="closed") as close:
            blocked = runner._monitor_q01_position(
                strat,
                SimpleNamespace(bid=2000.0, ask=2100.0, quote_time_msc=int(arrival.timestamp() * 1000)),
                arrival,
            )
        self.assertTrue(blocked)
        self.assertEqual(close.call_args.args[1], "q01_feed_gap")

    def test_q01_feed_gap_market_closed_persists_intent_and_retries_on_fresh_quote(self):
        runner, _strategy, _state = make_runner(live=True)
        strat, state = self._arm_q01_basket(runner)
        previous = pd.Timestamp("2026-01-01T00:10:00Z")
        arrival = previous + pd.Timedelta(seconds=301)
        retry = arrival + pd.Timedelta(seconds=61)
        state["q01_last_quote_msc"] = int(previous.timestamp() * 1000)
        with patch.object(runner, "_close_basket", side_effect=["market_closed", "requested"]) as close:
            runner._monitor_q01_position(
                strat,
                SimpleNamespace(bid=2000.0, ask=2100.0, quote_time_msc=int(arrival.timestamp() * 1000)),
                arrival,
            )
            self.assertEqual(state["pending_close_reason"], "q01_feed_gap")
            runner._monitor_q01_position(
                strat,
                SimpleNamespace(bid=2000.0, ask=2100.0, quote_time_msc=int(retry.timestamp() * 1000)),
                retry,
            )
        self.assertEqual(close.call_count, 2)
        self.assertEqual([call.args[1] for call in close.call_args_list], ["q01_feed_gap", "q01_feed_gap"])

    def test_q01_exact_300_second_interval_is_not_a_feed_gap(self):
        runner, _strategy, _state = make_runner(live=True)
        strat, state = self._arm_q01_basket(runner, entry_time="2026-01-01T00:20:00+00:00")
        previous = pd.Timestamp("2026-01-01T00:20:00Z")
        arrival = previous + pd.Timedelta(seconds=300)
        state["q01_last_quote_msc"] = int(previous.timestamp() * 1000)
        with patch.object(runner, "_close_basket", return_value="closed") as close:
            blocked = runner._monitor_q01_position(
                strat,
                SimpleNamespace(bid=2000.0, ask=2100.0, quote_time_msc=int(arrival.timestamp() * 1000)),
                arrival,
            )
        self.assertFalse(blocked)
        close.assert_not_called()

    def test_q01_malformed_future_quote_clock_forces_owned_close(self):
        runner, _strategy, _state = make_runner(live=True)
        strat, state = self._arm_q01_basket(runner)
        current = pd.Timestamp("2026-01-01T00:10:00Z")
        state["q01_last_quote_msc"] = int((current + pd.Timedelta(seconds=1)).timestamp() * 1000)
        with patch.object(runner, "_close_basket", return_value="requested") as close:
            blocked = runner._monitor_q01_position(
                strat,
                SimpleNamespace(bid=2000.0, ask=2100.0, quote_time_msc=int(current.timestamp() * 1000)),
                current,
            )
        self.assertTrue(blocked)
        self.assertEqual(close.call_args.args[1], "q01_quote_clock_invalid")

    def test_q01_fixed_hold_does_not_defer_for_wide_spread(self):
        runner, _strategy, _state = make_runner(live=True)
        strat, state = self._arm_q01_basket(runner)
        due_quote = pd.Timestamp("2026-01-01T00:30:01Z")
        state["q01_last_quote_msc"] = int(pd.Timestamp("2026-01-01T00:29:59Z").timestamp() * 1000)
        with patch.object(runner, "_close_basket", return_value="closed") as close:
            blocked = runner._monitor_q01_position(
                strat,
                SimpleNamespace(bid=2000.0, ask=2100.0, quote_time_msc=int(due_quote.timestamp() * 1000)),
                due_quote,
            )
        self.assertTrue(blocked)
        self.assertEqual(close.call_args.args[1], "q01_fixed_hold")
        self.assertFalse(state["time_close_wide_seen"])

    def test_q01_retry_rejects_tampered_canonical_identity(self):
        mutations = {
            "source": lambda opportunity, retry: opportunity.__setitem__("source", "other"),
            "raw_side": lambda opportunity, retry: opportunity.__setitem__("raw_side", "SHORT"),
            "opportunity_id": lambda opportunity, retry: opportunity.__setitem__("opportunity_id", "wrong"),
            "available_time": lambda opportunity, retry: opportunity.__setitem__("available_time", "2026-01-01T00:06:00+00:00"),
            "expiry": lambda opportunity, retry: retry.__setitem__("expires_utc", "2026-01-01T00:13:00+00:00"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                runner, _strategy, _state = make_runner(live=True)
                strat = runner._q01_strategies()[0]
                state = runner._st(strat)
                event = "2026-01-01T00:00:00+00:00"
                release = "2026-01-01T00:05:00+00:00"
                opportunity = {
                    "opportunity_id": f"XAUUSD|{event}|q01_variance_ratio_release|LONG",
                    "source": "q01_variance_ratio_release",
                    "side": "LONG",
                    "raw_side": "LONG",
                    "effective_side": "LONG",
                    "event_time": event,
                    "release_time": release,
                    "available_time": release,
                    "decision_time": "2026-01-01T00:05:01+00:00",
                    "executable_at": "2026-01-01T00:05:01+00:00",
                }
                retry = {"opportunity": opportunity, "expires_utc": "2026-01-01T00:12:00+00:00", "note": "q01"}
                mutate(opportunity, retry)
                state["q01_retry_opportunity"] = retry
                runner.state["routing"]["q01_last_evaluated_m5_bar"] = event
                with patch.object(runner, "_open_entry") as opened:
                    runner._process_q01_retries(
                        SimpleNamespace(
                            bid=2000.0,
                            ask=2000.1,
                            quote_time_msc=int(pd.Timestamp("2026-01-01T00:05:02Z").timestamp() * 1000),
                        ),
                        pd.Timestamp("2026-01-01T00:05:02Z"),
                        {22: True},
                    )
                opened.assert_not_called()
                self.assertEqual(state["sync_block_reason"], "q01_retry_identity_invalid")

    def test_q01_permanent_no_fill_clears_retry_but_preserves_retryable_state(self):
        runner, _strategy, _state = make_runner(live=True)
        runner.params["q01_live_trading_enabled"] = True
        strat = runner._q01_strategies()[0]
        state = runner._st(strat)
        opportunity = {"opportunity_id": "q01-test", "side": "LONG"}
        state["q01_retry_opportunity"] = {"opportunity": opportunity}
        row = pd.Series({"Open": 2000.0, "Close": 2000.0, "AskOpen": 2000.1})
        with patch.object(runner, "_open_entry", return_value=False):
            self.assertFalse(runner._attempt_q01_open(strat, opportunity, row, SimpleNamespace(), pd.Timestamp("2026-01-01T00:00:00Z"), "q01"))
        self.assertIsNone(state["q01_retry_opportunity"])
        state["q01_retry_opportunity"] = {"opportunity": opportunity}
        state["open_retry_after_utc"] = "2026-01-01T00:01:00+00:00"
        with patch.object(runner, "_open_entry", return_value=False):
            runner._attempt_q01_open(strat, opportunity, row, SimpleNamespace(), pd.Timestamp("2026-01-01T00:00:00Z"), "q01")
        self.assertIsNotNone(state["q01_retry_opportunity"])

    def test_q01_open_requests_broker_confirmed_fill_time(self):
        runner, _strategy, _state = make_runner(live=True)
        runner.params["q01_live_trading_enabled"] = True
        strat = runner._q01_strategies()[0]
        opportunity = {"opportunity_id": "q01-fill-clock", "side": "LONG"}
        row = pd.Series({"Open": 2000.0, "Close": 2000.0, "AskOpen": 2000.1})
        with patch.object(runner, "_open_entry", return_value=False) as opened:
            runner._attempt_q01_open(
                strat,
                opportunity,
                row,
                SimpleNamespace(),
                pd.Timestamp("2026-01-01T00:00:00Z"),
                "q01",
            )
        self.assertTrue(opened.call_args.kwargs["use_confirmed_fill_time"])

    def test_q01_confirmed_open_seeds_feed_gap_quote_clock(self):
        runner, _strategy, _state = make_runner(live=True)
        runner.params["q01_live_trading_enabled"] = True
        strat = runner._q01_strategies()[0]
        state = runner._st(strat)
        opportunity = {"opportunity_id": "q01-feed-clock", "side": "LONG"}
        row = pd.Series({"Open": 2000.0, "Close": 2000.0, "AskOpen": 2000.1})
        quote_msc = int(pd.Timestamp("2026-01-01T00:05:01Z").timestamp() * 1000)

        def confirmed_open(*_args, **_kwargs):
            state["basket"] = [{"opportunity_id": "q01-feed-clock"}]
            return True

        with patch.object(runner, "_open_entry", side_effect=confirmed_open):
            self.assertTrue(
                runner._attempt_q01_open(
                    strat,
                    opportunity,
                    row,
                    SimpleNamespace(quote_time_msc=quote_msc),
                    pd.Timestamp("2026-01-01T00:05:01Z"),
                    "q01",
                )
            )
        self.assertEqual(state["q01_last_quote_msc"], quote_msc)

    def test_q01_default_live_gate_consumes_signal_without_order(self):
        runner, _strategy, _state = make_runner(live=True)
        strat = runner._q01_strategies()[0]
        state = runner._st(strat)
        opportunity = {"opportunity_id": "q01-gated", "side": "LONG"}
        state["q01_retry_opportunity"] = {"opportunity": opportunity}
        row = pd.Series({"Open": 2000.0, "Close": 2000.0, "AskOpen": 2000.1})
        with patch.object(runner, "_open_entry") as opened:
            self.assertFalse(
                runner._attempt_q01_open(
                    strat,
                    opportunity,
                    row,
                    SimpleNamespace(quote_time_msc=1767225901000),
                    pd.Timestamp("2026-01-01T00:05:01Z"),
                    "q01",
                )
            )
        opened.assert_not_called()
        self.assertIsNone(state["q01_retry_opportunity"])

    def test_q01_cooldown_is_anchored_to_confirmed_close_time(self):
        runner, _strategy, _state = make_runner(live=True)
        strat, state = self._arm_q01_basket(runner)
        runner._clear_basket_state(
            strat,
            "q01_fixed_hold",
            "2026-01-01T00:30:00+00:00",
            closed_at_utc="2026-01-01T00:31:17+00:00",
        )
        self.assertEqual(state["cooldown_until_utc"], "2026-01-01T00:36:17+00:00")


def arm_pending(
    state: dict,
    *,
    atr30: float | None = 1.5,
    target: float | None = 100.0,
    now: datetime | pd.Timestamp | None = None,
) -> None:
    now = pd.Timestamp(now if now is not None else utc_now())
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    event_time = now - pd.Timedelta(minutes=1)
    state.update(
        {
            "pending_entry_side": "LONG",
            "pending_entry_target": target,
            "pending_entry_expires_utc": dt_text(now + pd.Timedelta(minutes=5)),
            "pending_entry_atr30": atr30,
            "pending_entry_signal_bar": dt_text(event_time),
            "pending_entry_opportunity_id": f"XAUUSD|{dt_text(event_time)}|LONG|LONG|reverse_d60",
            "pending_entry_event_time": dt_text(event_time),
            "pending_entry_release_time": dt_text(now),
        }
    )


def bind_owned_basket_identity(strategy: dict, state: dict, *, sequence: int = 1) -> None:
    lane_id = int(strategy["lane_id"])
    basket_id = f"L{lane_id}-B{int(sequence):06d}"
    state["lane_id"] = lane_id
    state["basket_sequence"] = int(sequence)
    state["current_basket_id"] = basket_id
    for position in state.get("basket", []):
        position["lane_id"] = lane_id
        position["basket_id"] = basket_id


def arm_owned_basket(strategy: dict, state: dict, executor: CountingExecutor, *, ticket: int = 9401) -> None:
    position = SimpleNamespace(
        ticket=ticket,
        identifier=ticket,
        symbol="XAUUSD",
        magic=EXPECTED_S23_MAGIC,
        comment=strategy["comment_prefix"],
        type=ORDER_TYPE_BUY,
        volume=0.01,
        open_price=100.0,
        open_time=1,
    )
    executor.positions = [position]
    state["basket"] = [
        {
            "ticket": ticket,
            "position_identifier": ticket,
            "side": "LONG",
            "lot": 0.01,
            "entry_price": 100.0,
            "entry_time_utc": dt_text(utc_now() - pd.Timedelta(minutes=5)),
            "open_time_epoch": 1,
            "owner_symbol": "XAUUSD",
            "owner_magic": EXPECTED_S23_MAGIC,
            "owner_comment": strategy["comment_prefix"],
            "shadow": False,
        }
    ]
    bind_owned_basket_identity(strategy, state)


def sample_opportunity(*, side: str = "LONG") -> tuple[dict, pd.Series, pd.Timestamp, SimpleNamespace]:
    poll_time = pd.Timestamp("2026-08-25 13:01:02", tz="UTC")
    event_time = pd.Timestamp("2026-08-25 13:00:00", tz="UTC")
    opportunity = {
        "opportunity_id": f"XAUUSD|{event_time.isoformat()}|{side}|{side}|reverse_d60",
        "source": "za",
        "side": side,
        "raw_side": side,
        "effective_side": side,
        "entry_policy": {"policy_id": "reverse_d60"},
        "event_time": event_time.isoformat(),
        "release_time": (event_time + pd.Timedelta(minutes=1)).isoformat(),
        "available_time": (event_time + pd.Timedelta(minutes=1)).isoformat(),
        "decision_time": poll_time.isoformat(),
        "executable_at": poll_time.isoformat(),
    }
    row = pd.Series(
        {"Open": 100.0, "Close": 100.0, "AskOpen": 100.03, "atr30": 2.5, "bb20_mid": 100.0, "bb20_std": 1.0},
        name=event_time,
    )
    return opportunity, row, poll_time, SimpleNamespace(
        bid=100.0,
        ask=100.03,
        quote_time_msc=int(poll_time.timestamp() * 1000),
    )


class Bot23ZARegressionTests(unittest.TestCase):
    def test_missing_state_file_blocks_every_lane_instead_of_resetting_daily_loss(self):
        params = json.loads(json.dumps(load_params()))
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            runner = S23HorizontalInventoryRunner(params)
        lane_states = [runner._st(strategy) for strategy in runner._all_strategies()]
        self.assertTrue(lane_states)
        self.assertTrue(all(state["sync_block_reason"] == "state_file_missing" for state in lane_states))
        self.assertTrue(all(state["sync_block_recoverable"] is False for state in lane_states))

    def test_generic_pending_open_receipt_recovers_exact_non_session_fill(self):
        runner, strategy, state = make_runner(live=True)
        started = pd.Timestamp("2026-08-25T13:01:02.123Z")
        position = SimpleNamespace(
            ticket=7702, identifier=8802, symbol="XAUUSD",
            magic=int(strategy["magic"]), comment=str(strategy["comment_prefix"]),
            type=ORDER_TYPE_BUY, volume=0.01, open_price=2000.5,
            open_time=int(started.timestamp()), open_time_msc=int(started.timestamp() * 1000),
        )
        runner.executor = CountingExecutor()
        runner.executor.positions = [position]
        state.update({
            "pending_open_opportunity_id": "generic-opportunity-1",
            "pending_open_started_utc": dt_text(started),
            "pending_open_expires_utc": dt_text(started + pd.Timedelta(minutes=2)),
            "pending_open_side": "LONG", "pending_open_lot": 0.01,
            "pending_open_symbol": "XAUUSD", "pending_open_magic": int(strategy["magic"]),
            "pending_open_comment": str(strategy["comment_prefix"]),
            "pending_open_signal_bar": "2026-08-25T13:00:00+00:00",
            "pending_open_basket_atr30": 1.5, "pending_open_reverse_used": False,
            "pending_open_expected_positions": 0,
        })

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertEqual(len(state["basket"]), 1)
        self.assertEqual(state["basket"][0]["position_identifier"], 8802)
        self.assertIsNone(state["pending_open_opportunity_id"])
        self.assertEqual(state["frozen_basket_atr30"], 1.5)

    def test_generic_pending_open_receipt_recovers_exact_add_fill(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor, ticket=7701)
        started = pd.Timestamp("2026-08-25T13:01:02.123Z")
        executor.positions.append(SimpleNamespace(
            ticket=7702, identifier=8802, symbol="XAUUSD",
            magic=int(strategy["magic"]), comment=str(strategy["comment_prefix"]),
            type=ORDER_TYPE_BUY, volume=0.01, open_price=2001.0,
            open_time=int(started.timestamp()), open_time_msc=int(started.timestamp() * 1000),
        ))
        state.update({
            "pending_open_opportunity_id": "generic-add-1",
            "pending_open_started_utc": dt_text(started),
            "pending_open_expires_utc": dt_text(started + pd.Timedelta(minutes=2)),
            "pending_open_side": "LONG", "pending_open_lot": 0.01,
            "pending_open_symbol": "XAUUSD", "pending_open_magic": int(strategy["magic"]),
            "pending_open_comment": str(strategy["comment_prefix"]),
            "pending_open_signal_bar": "2026-08-25T13:00:00+00:00",
            "pending_open_basket_atr30": None, "pending_open_reverse_used": False,
            "pending_open_expected_positions": 1,
        })

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertEqual(len(state["basket"]), 2)
        self.assertEqual({row["position_identifier"] for row in state["basket"]}, {7701, 8802})
        self.assertIsNone(state["pending_open_opportunity_id"])

    def test_boolean_configuration_rejects_coercible_strings(self):
        mutations = (
            lambda p: p.__setitem__("live_trading_enabled", "false"),
            lambda p: p["safety"].__setitem__("stale_signal_guard", "true"),
            lambda p: p["session_vwap_strategies"][0].__setitem__("enabled", "false"),
            lambda p: p["eu_entry_admission_clock"].__setitem__("routing_enabled", "true"),
        )
        for mutate in mutations:
            params = json.loads(json.dumps(load_params()))
            mutate(params)
            with self.subTest(params=params):
                with self.assertRaises(ValueError):
                    S23HorizontalInventoryRunner(params)

    def test_execution_numeric_configuration_fails_closed(self):
        mutations = (
            lambda p: p.__setitem__("daily_realized_loss_limit_usd", -1.0),
            lambda p: p.__setitem__("max_signal_delay_minutes", "2"),
            lambda p: p.__setitem__("point_size", float("nan")),
            lambda p: p.__setitem__("price_digits", True),
            lambda p: p.__setitem__("max_entry_spread_points", -1.0),
            lambda p: p.__setitem__("deviation_points", 1.5),
            lambda p: p.__setitem__("bot_log_max_bytes", True),
            lambda p: p.__setitem__("trade_permission_retry_seconds", 0),
            lambda p: p.__setitem__("new_basket_blocked_hours_utc", [14, 14]),
            lambda p: p["session_vwap_history"].__setitem__("page_bars", 5001),
            lambda p: p["session_vwap_history"].__setitem__("retry_seconds", [5, 0]),
        )
        for mutate in mutations:
            params = json.loads(json.dumps(load_params()))
            mutate(params)
            with self.subTest(params=params):
                with self.assertRaises(ValueError):
                    S23HorizontalInventoryRunner(params)

    def test_strategy_scalar_configuration_fails_closed(self):
        mutations = (
            lambda p: p.__setitem__("lane_count", "4"),
            lambda p: p.__setitem__("morning_session_max_positions", True),
            lambda p: p.__setitem__("session_vwap_quantile", float("inf")),
            lambda p: p["expected_session_vwap_magics"].__setitem__(0, "230035"),
            lambda p: p["strategies"][0].__setitem__("magic", "230023"),
            lambda p: p["morning_session_strategies"][0].__setitem__("hold_minutes", 15.0),
            lambda p: p["midday_session_strategies"][0].__setitem__("lot", "0.01"),
            lambda p: p["strategies"][0].__setitem__("entry_require_extreme", 1),
            lambda p: p["strategies"][0].__setitem__("reverse_on_fail", "false"),
        )
        for mutate in mutations:
            params = json.loads(json.dumps(load_params()))
            mutate(params)
            with self.subTest(params=params):
                with self.assertRaises(ValueError):
                    S23HorizontalInventoryRunner(params)

    def test_strategy_topology_configuration_fails_closed(self):
        mutations = (
            lambda p: p["strategies"][0].__setitem__("id", "renamed_lane"),
            lambda p: p["morning_session_strategies"].append({
                **p["morning_session_strategies"][0],
                "enabled": False,
                "id": "extra_disabled_lane",
            }),
            lambda p: p["midday_session_strategies"][0].pop("hold_minutes"),
            lambda p: p["session_vwap_strategies"][0].__setitem__("unused_typo", 1),
            lambda p: p["pre_eu30_session_strategies"][0].__setitem__("pre_eu30_lane_id", 2),
        )
        for mutate in mutations:
            params = json.loads(json.dumps(load_params()))
            mutate(params)
            with self.subTest(params=params):
                with self.assertRaises(ValueError):
                    S23HorizontalInventoryRunner(params)

    def test_params_json_rejects_duplicate_keys_and_nonfinite_constants(self):
        payloads = (
            '{"enabled": true, "enabled": false}',
            '{"enabled": true, "point_size": NaN}',
            '[]',
        )
        for payload in payloads:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8",
            ) as handle:
                handle.write(payload)
                path = handle.name
            try:
                with self.subTest(payload=payload):
                    with self.assertRaises(ValueError):
                        live_s23_bot.load_params(path)
            finally:
                os.unlink(path)

    def test_state_json_duplicate_key_fails_closed_with_load_evidence(self):
        params = json.loads(json.dumps(load_params()))
        payload = '{"bot":"bot23","strategy_id":"bot23_za_horizontal_inventory_v001","version":3,"version":3,"routing":{},"strategies":{}}'
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8",
        ) as handle:
            handle.write(payload)
            path = handle.name
        try:
            with patch.object(live_s23_bot, "STATE_FILE", path):
                runner = S23HorizontalInventoryRunner(params)
            for strategy in runner._all_strategies():
                state = runner._st(strategy)
                self.assertEqual(state["sync_block_reason"], "state_identity_mismatch")
                self.assertIn(
                    "duplicate JSON key",
                    state["sync_block_details"]["observed"]["load_error"],
                )
        finally:
            os.unlink(path)

    def test_atomic_state_write_rejects_nonfinite_without_replacing_destination(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8",
        ) as handle:
            handle.write('{"safe": true}\n')
            path = handle.name
        try:
            with self.assertRaises(ValueError):
                live_s23_bot.atomic_write_json(path, {"unsafe": float("nan")})
            with open(path, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), '{"safe": true}\n')
            self.assertFalse(os.path.exists(f"{path}.tmp"))
        finally:
            if os.path.exists(path):
                os.unlink(path)
            if os.path.exists(f"{path}.tmp"):
                os.unlink(f"{path}.tmp")

    def test_posix_atomic_state_replace_fsyncs_parent_directory(self):
        state_path = os.path.abspath("state/s23_state.json")
        expected_parent = os.path.dirname(state_path)
        with (
            patch.object(live_s23_bot.os, "name", "posix"),
            patch.object(live_s23_bot.os, "open", return_value=321) as open_mock,
            patch.object(live_s23_bot.os, "fsync") as fsync_mock,
            patch.object(live_s23_bot.os, "close") as close_mock,
        ):
            live_s23_bot._fsync_parent_directory(state_path)
        open_mock.assert_called_once()
        self.assertEqual(open_mock.call_args.args[0], expected_parent)
        fsync_mock.assert_called_once_with(321)
        close_mock.assert_called_once_with(321)

    def test_string_state_version_is_rejected_as_foreign_identity(self):
        params = json.loads(json.dumps(load_params()))
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            baseline = S23HorizontalInventoryRunner(params)
        state = baseline._default_state()
        state["version"] = str(state["version"])
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8",
        ) as handle:
            json.dump(state, handle)
            path = handle.name
        try:
            with patch.object(live_s23_bot, "STATE_FILE", path):
                loaded = S23HorizontalInventoryRunner(params)
            reasons = {
                loaded._st(strategy).get("sync_block_reason")
                for strategy in loaded._all_strategies()
            }
            self.assertEqual(reasons, {"state_identity_mismatch"})
        finally:
            os.unlink(path)

    def test_unknown_strategy_state_namespace_is_rejected_as_foreign_identity(self):
        params = json.loads(json.dumps(load_params()))
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            baseline = S23HorizontalInventoryRunner(params)
        state = baseline._default_state()
        state["strategies"]["foreign_lane"] = dict(next(iter(state["strategies"].values())))
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8",
        ) as handle:
            json.dump(state, handle)
            path = handle.name
        try:
            with patch.object(live_s23_bot, "STATE_FILE", path):
                loaded = S23HorizontalInventoryRunner(params)
            for strategy in loaded._all_strategies():
                strategy_state = loaded._st(strategy)
                self.assertEqual(strategy_state["sync_block_reason"], "state_identity_mismatch")
                observed = strategy_state["sync_block_details"]["observed"]
                self.assertFalse(observed["shape_valid"])
                self.assertEqual(observed["unknown_strategy_ids"], ["foreign_lane"])
        finally:
            os.unlink(path)

    def test_current_policy_generation_rejects_missing_owned_state(self):
        params = json.loads(json.dumps(load_params()))
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            baseline = S23HorizontalInventoryRunner(params)
        session_id = params["session_vwap_strategies"][0]["id"]
        za_id = params["strategies"][0]["id"]
        corruptions = (
            lambda state: state["strategies"].pop(session_id),
            lambda state: state["strategies"][session_id].pop("basket"),
            lambda state: state["strategies"][za_id].pop("basket_sequence"),
            lambda state: state["strategies"][session_id].__setitem__("basket", {}),
            lambda state: state["strategies"][za_id].__setitem__("basket_sequence", True),
            lambda state: state["strategies"][session_id].__setitem__("lane_id", 999),
            lambda state: state["strategies"][za_id].__setitem__("current_basket_id", 123),
            lambda state: state["strategies"][za_id].update({
                "basket": [{}], "basket_sequence": 0,
                "current_basket_id": "L1-B000000",
            }),
            lambda state: state["strategies"][session_id].update({
                "basket": [{}], "basket_sequence": 1, "current_basket_id": None,
            }),
            lambda state: state["strategies"][za_id].__setitem__(
                "current_basket_id", "L1-B000001",
            ),
            lambda state: state["routing"].pop("trend_recovery"),
            lambda state: state["routing"].pop("long_target_rearm_request_utc"),
            lambda state: state["routing"].pop("session_vwap_params_hash"),
            lambda state: state["routing"].pop("entry_policy_params_hash"),
            lambda state: (
                state["routing"].pop("entry_policy_id"),
                state["routing"].pop("entry_policy_params_hash"),
                state["strategies"][za_id].update({
                    "basket": [{}], "basket_sequence": 1, "current_basket_id": None,
                }),
            ),
            lambda state: (
                state["routing"].pop("session_vwap_policy_id"),
                state["routing"].pop("session_vwap_params_hash"),
                state["strategies"].pop(session_id),
            ),
        )
        for corrupt in corruptions:
            state = baseline._default_state()
            corrupt(state)
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8",
            ) as handle:
                json.dump(state, handle)
                path = handle.name
            try:
                with patch.object(live_s23_bot, "STATE_FILE", path):
                    loaded = S23HorizontalInventoryRunner(params)
                for strategy in loaded._all_strategies():
                    strategy_state = loaded._st(strategy)
                    self.assertEqual(strategy_state["sync_block_reason"], "state_identity_mismatch")
                    errors = strategy_state["sync_block_details"]["observed"]["state_generation_errors"]
                    self.assertTrue(errors)
            finally:
                os.unlink(path)

    def test_pre_policy_generation_can_migrate_whole_missing_lane_family(self):
        params = json.loads(json.dumps(load_params()))
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            baseline = S23HorizontalInventoryRunner(params)
        state = baseline._default_state()
        session_ids = [row["id"] for row in params["session_vwap_strategies"]]
        for strategy_id in session_ids:
            state["strategies"].pop(strategy_id)
        state["routing"].pop("session_vwap_policy_id")
        state["routing"].pop("session_vwap_params_hash")
        state["routing"].pop("session_vwap_last_evaluated_bar")
        state["routing"].pop("session_vwap_last_unavailable_bar")
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8",
        ) as handle:
            json.dump(state, handle)
            path = handle.name
        try:
            with patch.object(live_s23_bot, "STATE_FILE", path):
                loaded = S23HorizontalInventoryRunner(params)
            self.assertTrue(loaded._session_vwap_state_migrated)
            self.assertTrue(all(strategy_id in loaded.state["strategies"] for strategy_id in session_ids))
            self.assertFalse(any(
                loaded._st(strategy).get("sync_block_reason") == "state_identity_mismatch"
                for strategy in loaded._all_strategies()
            ))
        finally:
            os.unlink(path)

    def test_foreign_za_policy_identity_blocks_without_rewriting_state(self):
        params = json.loads(json.dumps(load_params()))
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            baseline = S23HorizontalInventoryRunner(params)
        cases = (
            ("entry_policy_id", "entry_policy_params_hash", "entry_policy_identity_mismatch"),
            ("portfolio_rearm_policy_id", "portfolio_rearm_params_hash", "portfolio_rearm_policy_identity_mismatch"),
            ("inventory_range_fade_policy_id", "inventory_range_fade_params_hash", "inventory_range_fade_policy_identity_mismatch"),
        )
        for policy_key, hash_key, expected_reason in cases:
            state = baseline._default_state()
            state["routing"][policy_key] = "foreign-policy"
            state["routing"][hash_key] = "f" * 64
            state["routing"]["inventory_range_fade"]["active"] = False
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8",
            ) as handle:
                json.dump(state, handle)
                path = handle.name
            try:
                with patch.object(live_s23_bot, "STATE_FILE", path):
                    loaded = S23HorizontalInventoryRunner(params)
                self.assertEqual(loaded.state["routing"][policy_key], "foreign-policy")
                self.assertEqual(loaded.state["routing"][hash_key], "f" * 64)
                for strategy in loaded.params["strategies"]:
                    lane_state = loaded._st(strategy)
                    self.assertEqual(lane_state["sync_block_reason"], expected_reason)
                    self.assertFalse(lane_state["sync_block_recoverable"])
            finally:
                os.unlink(path)

    def test_root_state_identity_block_is_not_weakened_by_policy_mismatches(self):
        params = json.loads(json.dumps(load_params()))
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            baseline = S23HorizontalInventoryRunner(params)
        state = baseline._default_state()
        state["bot"] = "foreign-bot"
        for policy_key, hash_key, _collection, _routing_keys in live_s23_bot._STATE_GENERATION_CONTRACTS:
            state["routing"][policy_key] = "foreign-policy"
            state["routing"][hash_key] = "f" * 64
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8",
        ) as handle:
            json.dump(state, handle)
            path = handle.name
        try:
            with patch.object(live_s23_bot, "STATE_FILE", path):
                loaded = S23HorizontalInventoryRunner(params)
            for strategy in loaded._all_strategies():
                strategy_state = loaded._st(strategy)
                self.assertEqual(strategy_state["sync_block_reason"], "state_identity_mismatch")
                self.assertEqual(
                    strategy_state["sync_block_details"]["observed"]["bot"],
                    "foreign-bot",
                )
        finally:
            os.unlink(path)

    def test_missing_persisted_position_identifier_fails_closed(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["basket"][0].pop("position_identifier")

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(state["sync_block_reason"], "state_position_identity_invalid")
        self.assertEqual(executor.close_calls, [])

    def test_future_completed_bar_is_not_executable_before_release(self):
        decision = live_s23_bot.stale_signal_decision(
            "2026-08-25T13:01:00Z",
            timeframe_hours=1.0 / 60.0,
            max_delay_minutes=2.0,
            now_utc=pd.Timestamp("2026-08-25T13:01:30Z"),
        )

        self.assertTrue(decision.stale)
        self.assertEqual(decision.reason, "not_released")
        self.assertEqual(decision.entry_due_utc, pd.Timestamp("2026-08-25T13:02:00Z"))

    def test_live_open_blocks_broker_lot_contract_mismatch_before_reservation(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        _opportunity, row, poll_time, _info = sample_opportunity()
        info = live_executor.SymbolInfo(
            bid=100.0, ask=100.03, point=0.001,
            volume_min=0.10, volume_max=100.0, volume_step=0.10,
            digits=3, stops_level=0, quote_time_msc=int(poll_time.timestamp() * 1000),
            margin_free=1000.0, tick_value=0.1, tick_size=0.001,
            contract_size=100.0, trade_mode=4, order_mode=1,
        )

        opened = runner._open_entry(
            strategy, "LONG", row, info,
            execution_time=poll_time, apply_portfolio_rearm=False,
        )

        self.assertFalse(opened)
        self.assertEqual(executor.open_calls, 0)
        self.assertIsNone(state["pending_open_opportunity_id"])
        self.assertFalse(state["basket"])

    def test_shadow_open_ignores_live_broker_lot_contract(self):
        runner, strategy, state = make_runner(live=False)
        _opportunity, row, poll_time, _info = sample_opportunity()
        info = live_executor.SymbolInfo(
            bid=100.0, ask=100.03, point=0.001,
            volume_min=0.10, volume_max=100.0, volume_step=0.10,
            digits=3, stops_level=0, quote_time_msc=int(poll_time.timestamp() * 1000),
            margin_free=1000.0, tick_value=0.1, tick_size=0.001,
            contract_size=100.0, trade_mode=4, order_mode=1,
        )

        opened = runner._open_entry(
            strategy, "LONG", row, info,
            execution_time=poll_time, apply_portfolio_rearm=False,
        )

        self.assertTrue(opened)
        self.assertEqual(len(state["basket"]), 1)
        self.assertTrue(state["basket"][0]["shadow"])

    def test_transient_preclose_position_query_recovers_on_complete_owned_sync(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        position = executor.positions[0]
        responses = iter((None, position))
        executor.get_position = lambda _ticket: next(responses)
        row = pd.Series(
            {"Open": 99.0, "Close": 99.0, "AskOpen": 99.03},
            name=pd.Timestamp("2026-08-25T13:10:00Z"),
        )

        self.assertEqual(runner._close_basket(strategy, "basket_stop", row, -1.0), "failed")
        self.assertTrue(state["sync_block_recoverable"])
        self.assertTrue(runner._sync_strategy(strategy))
        self.assertFalse(state["sync_block_new_entries"])

    def test_malformed_close_response_stays_blocked_after_exact_owned_sync(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        results = iter((
            live_executor.CloseResult(False, "MALFORMED_OK", raw_response="OK|bad"),
            live_executor.CloseResult(True, "CONFIRMED", deal_id=8801, retcode=10009),
        ))
        executor.close_position = lambda ticket, _deviation, **_kwargs: (
            executor.close_calls.append(int(ticket)) or next(results)
        )
        row = pd.Series(
            {"Open": 99.0, "Close": 99.0, "AskOpen": 99.03},
            name=pd.Timestamp("2026-08-25T13:10:00Z"),
        )

        self.assertEqual(runner._close_basket(strategy, "basket_stop", row, -1.0), "failed")
        self.assertEqual(executor.close_calls, [9401])
        self.assertTrue(state["pending_close_reason"])
        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(
            state["sync_block_reason"], "close_submission_result_unresolved",
        )
        self.assertTrue(state["pending_close_reason"])
        self.assertEqual(
            runner._close_basket(strategy, "basket_stop", row, -1.0), "failed",
        )
        self.assertEqual(executor.close_calls, [9401])

    def test_restart_after_durable_pending_close_rearms_only_after_exact_owned_sync(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["pending_close_reason"] = "basket_stop"
        state["pending_close_signal_bar"] = "2026-08-25T13:10:00+00:00"

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertIsNone(state["pending_close_reason"])
        self.assertIsNone(state["pending_close_signal_bar"])
        self.assertFalse(state["sync_block_new_entries"])

    def test_successfully_submitted_close_is_not_rearmed_while_position_visibility_lags(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["pending_close_reason"] = "basket_stop"
        state["pending_close_signal_bar"] = "2026-08-25T13:10:00+00:00"
        state["basket"][0]["close_requested"] = True

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertEqual(state["pending_close_reason"], "basket_stop")
        self.assertTrue(state["basket"][0]["close_requested"])

    def test_successfully_submitted_trend_close_is_not_rearmed_while_position_visibility_lags(self):
        runner, _strategy, _state = make_runner(live=True)
        strategy = runner.params["trend_recovery_strategies"][0]
        state = runner._st(strategy)
        executor = CountingExecutor()
        runner.executor = executor
        executor.positions = [SimpleNamespace(
            ticket=9503, identifier=9503, symbol="XAUUSD",
            magic=int(strategy["magic"]), comment=strategy["comment_prefix"],
            type=live_s23_bot.ORDER_TYPE_SELL, volume=0.01, open_time=1,
        )]
        state["basket"] = [{
            "ticket": 9503, "position_identifier": 9503, "side": "SHORT",
            "lot": 0.01, "entry_price": 100.0,
            "entry_time_utc": "2026-08-25T13:00:00+00:00", "open_time_epoch": 1,
            "owner_symbol": "XAUUSD", "owner_magic": int(strategy["magic"]),
            "owner_comment": strategy["comment_prefix"], "shadow": False,
            "pending_close_reason": "trend_ticket_max_hold",
            "pending_close_signal_bar": "2026-08-25T13:10:00+00:00",
            "close_requested": True,
        }]
        bind_owned_basket_identity(strategy, state)

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertEqual(
            state["basket"][0]["pending_close_reason"], "trend_ticket_max_hold",
        )
        self.assertTrue(state["basket"][0]["close_requested"])

    def test_legacy_nonrecoverable_close_block_migrates_after_exact_owned_sync(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state.update(
            {
                "pending_close_reason": "basket_stop",
                "pending_close_signal_bar": "2026-08-25T13:10:00+00:00",
                "sync_block_new_entries": True,
                "sync_block_reason": "live_time_close_unconfirmed",
                "sync_block_recoverable": False,
                "sync_block_details": {"ticket": 9401, "status": "MALFORMED_OK"},
            }
        )

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertIsNone(state["pending_close_reason"])
        self.assertFalse(state["sync_block_new_entries"])

    def test_partial_confirmed_close_rearms_only_the_remaining_owned_position(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        first = SimpleNamespace(
            ticket=9601, identifier=9601, symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC, comment=strategy["comment_prefix"],
            type=ORDER_TYPE_BUY, volume=0.01, open_time=1,
        )
        second = SimpleNamespace(
            ticket=9602, identifier=9602, symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC, comment=strategy["comment_prefix"],
            type=ORDER_TYPE_BUY, volume=0.01, open_time=1,
        )
        executor.positions = [second]
        state["basket"] = [
            {
                "ticket": position.ticket,
                "position_identifier": position.identifier,
                "side": "LONG",
                "lot": 0.01,
                "entry_price": 100.0,
                "entry_time_utc": "2026-08-25T13:00:00+00:00",
                "open_time_epoch": 1,
                "owner_symbol": "XAUUSD",
                "owner_magic": EXPECTED_S23_MAGIC,
                "owner_comment": strategy["comment_prefix"],
                "shadow": False,
                "close_requested": position.ticket == 9601,
            }
            for position in (first, second)
        ]
        bind_owned_basket_identity(strategy, state)
        state.update(
            {
                "pending_close_reason": "basket_stop",
                "pending_close_signal_bar": "2026-08-25T13:10:00+00:00",
                "sync_block_new_entries": True,
                "sync_block_reason": "live_time_close_failed",
                "sync_block_recoverable": True,
            }
        )
        confirmed = SimpleNamespace(
            position_id=9601,
            symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC,
            net_profit=-1.25,
            price=99.5,
            deal=79601,
            exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-25T13:10:02Z").timestamp()),
        )
        executor.get_position_close_deal = (
            lambda position_id, _opened_at_epoch: confirmed
            if int(position_id) == 9601 else False
        )

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertEqual([position["ticket"] for position in state["basket"]], [9602])
        self.assertFalse(state["basket"][0].get("close_requested"))
        self.assertIsNone(state["pending_close_reason"])
        self.assertFalse(state["sync_block_new_entries"])

    def test_partial_target_confirmation_clears_stale_portfolio_pending_summary(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        first = SimpleNamespace(
            ticket=9603, identifier=9603, symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC, comment=strategy["comment_prefix"],
            type=ORDER_TYPE_BUY, volume=0.01, open_time=1,
        )
        second = SimpleNamespace(
            ticket=9604, identifier=9604, symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC, comment=strategy["comment_prefix"],
            type=ORDER_TYPE_BUY, volume=0.01, open_time=1,
        )
        executor.positions = [second]
        state["basket"] = [
            {
                "ticket": position.ticket,
                "position_identifier": position.identifier,
                "side": "LONG",
                "lot": 0.01,
                "entry_price": 100.0,
                "entry_time_utc": "2026-08-25T13:00:00+00:00",
                "open_time_epoch": 1,
                "owner_symbol": "XAUUSD",
                "owner_magic": EXPECTED_S23_MAGIC,
                "owner_comment": strategy["comment_prefix"],
                "shadow": False,
                "close_requested": position.ticket == 9603,
            }
            for position in (first, second)
        ]
        bind_owned_basket_identity(strategy, state)
        state.update(
            {
                "pending_close_reason": "basket_target",
                "pending_close_signal_bar": "2026-08-25T13:10:00+00:00",
                "sync_block_new_entries": True,
                "sync_block_reason": "live_time_close_failed",
                "sync_block_recoverable": True,
            }
        )
        runner._arm_long_target_portfolio_rearm(
            strategy, pd.Timestamp("2026-08-25T13:10:00Z"),
        )
        confirmed = SimpleNamespace(
            position_id=9603,
            symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC,
            net_profit=1.25,
            price=100.5,
            deal=79603,
            exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-25T13:10:02Z").timestamp()),
        )
        executor.get_position_close_deal = (
            lambda position_id, _opened_at_epoch: confirmed
            if int(position_id) == 9603 else False
        )

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertEqual([position["ticket"] for position in state["basket"]], [9604])
        self.assertFalse(state["basket"][0].get("close_requested"))
        self.assertIsNone(state["pending_close_reason"])
        routing = runner.state["routing"]
        self.assertFalse(routing["long_target_rearm_pending_confirmation"])
        self.assertIsNone(routing["long_target_rearm_request_utc"])
        self.assertIsNone(routing["long_target_rearm_trigger_lane_id"])
        self.assertIsNone(routing["long_target_rearm_trigger_basket_id"])

    def test_partial_target_retry_rearm_preserves_another_lane_pending_summary(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        first = SimpleNamespace(
            ticket=9605, identifier=9605, symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC, comment=strategy["comment_prefix"],
            type=ORDER_TYPE_BUY, volume=0.01, open_time=1,
        )
        second = SimpleNamespace(
            ticket=9606, identifier=9606, symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC, comment=strategy["comment_prefix"],
            type=ORDER_TYPE_BUY, volume=0.01, open_time=1,
        )
        executor.positions = [second]
        state["basket"] = [
            {
                "ticket": position.ticket,
                "position_identifier": position.identifier,
                "side": "LONG",
                "lot": 0.01,
                "entry_price": 100.0,
                "entry_time_utc": "2026-08-25T13:00:00+00:00",
                "open_time_epoch": 1,
                "owner_symbol": "XAUUSD",
                "owner_magic": EXPECTED_S23_MAGIC,
                "owner_comment": strategy["comment_prefix"],
                "shadow": False,
                "close_requested": position.ticket == 9605,
            }
            for position in (first, second)
        ]
        bind_owned_basket_identity(strategy, state)
        state.update({
            "pending_close_reason": "basket_target",
            "pending_close_signal_bar": "2026-08-25T13:10:00+00:00",
            "sync_block_new_entries": True,
            "sync_block_reason": "live_time_close_failed",
            "sync_block_recoverable": True,
        })
        other = runner.params["strategies"][1]
        other_state = runner._st(other)
        other_state.update({
            "basket": [{"side": "LONG"}],
            "current_basket_id": "L2-B000001",
            "pending_close_reason": "basket_target",
            "pending_close_signal_bar": "2026-08-25T13:10:01+00:00",
        })
        runner._arm_long_target_portfolio_rearm(
            strategy, pd.Timestamp("2026-08-25T13:10:00Z"),
        )
        confirmed = SimpleNamespace(
            position_id=9605, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
            net_profit=1.0, price=100.5, deal=79605, exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-25T13:10:02Z").timestamp()),
        )
        executor.get_position_close_deal = (
            lambda position_id, _opened_at_epoch: confirmed
            if int(position_id) == 9605 else False
        )

        self.assertTrue(runner._sync_strategy(strategy))
        routing = runner.state["routing"]
        self.assertTrue(routing["long_target_rearm_pending_confirmation"])
        self.assertEqual(
            parse_ts(routing["long_target_rearm_request_utc"]),
            pd.Timestamp("2026-08-25T13:10:01Z"),
        )
        self.assertEqual(routing["long_target_rearm_trigger_lane_id"], 2)
        self.assertEqual(routing["long_target_rearm_trigger_basket_id"], "L2-B000001")

    def test_mixed_close_result_preserves_successful_request_and_retries_only_failed_ticket(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        positions = [
            SimpleNamespace(
                ticket=ticket, identifier=ticket, symbol="XAUUSD",
                magic=EXPECTED_S23_MAGIC, comment=strategy["comment_prefix"],
                type=ORDER_TYPE_BUY, volume=0.01, open_time=1,
            )
            for ticket in (9611, 9612)
        ]
        executor.positions = positions
        state["basket"] = [
            {
                "ticket": position.ticket,
                "position_identifier": position.identifier,
                "side": "LONG",
                "lot": 0.01,
                "entry_price": 100.0,
                "entry_time_utc": "2026-08-25T13:00:00+00:00",
                "open_time_epoch": 1,
                "owner_symbol": "XAUUSD",
                "owner_magic": EXPECTED_S23_MAGIC,
                "owner_comment": strategy["comment_prefix"],
                "shadow": False,
                "close_requested": position.ticket == 9611,
            }
            for position in positions
        ]
        bind_owned_basket_identity(strategy, state)
        state.update(
            {
                "pending_close_reason": "basket_stop",
                "pending_close_signal_bar": "2026-08-25T13:10:00+00:00",
                "sync_block_new_entries": True,
                "sync_block_reason": "live_time_close_failed",
                "sync_block_recoverable": True,
            }
        )

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertTrue(state["basket"][0]["close_requested"])
        self.assertFalse(state["basket"][1].get("close_requested"))
        self.assertEqual(state["pending_close_reason"], "basket_stop")
        self.assertEqual(
            state["pending_close_signal_bar"], "2026-08-25T13:10:00+00:00",
        )
        self.assertTrue(runner._sync_strategy(strategy))
        row = pd.Series(
            {"Open": 99.0, "Close": 99.0, "AskOpen": 99.03},
            name=pd.Timestamp("2026-08-25T13:11:00Z"),
        )
        runner._close_basket(strategy, "basket_stop", row, -2.0)
        self.assertEqual(executor.close_calls, [9612])

    def test_each_confirmed_close_is_persisted_before_submitting_the_next_ticket(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        positions = [
            SimpleNamespace(
                ticket=ticket, identifier=ticket, symbol="XAUUSD",
                magic=EXPECTED_S23_MAGIC, comment=strategy["comment_prefix"],
                type=ORDER_TYPE_BUY, volume=0.01, open_price=100.0, open_time=1,
            )
            for ticket in (9613, 9614)
        ]
        executor.positions = positions
        state["basket"] = [
            {
                "ticket": position.ticket,
                "position_identifier": position.identifier,
                "side": "LONG",
                "lot": 0.01,
                "entry_price": 100.0,
                "entry_time_utc": "2026-08-25T13:00:00+00:00",
                "open_time_epoch": 1,
                "owner_symbol": "XAUUSD",
                "owner_magic": EXPECTED_S23_MAGIC,
                "owner_comment": strategy["comment_prefix"],
                "shadow": False,
                "close_requested": False,
            }
            for position in positions
        ]
        bind_owned_basket_identity(strategy, state)
        persisted_snapshots = []
        runner._save_state = lambda: persisted_snapshots.append(
            json.loads(json.dumps(runner.state))
        )
        persisted_first_before_second = []

        def close_position(ticket, _deviation, **_kwargs):
            if int(ticket) == 9614:
                saved_basket = persisted_snapshots[-1]["strategies"][strategy["id"]]["basket"]
                persisted_first_before_second.append(
                    bool(saved_basket[0].get("close_requested"))
                )
            return live_executor.CloseResult(
                True, "CONFIRMED", deal_id=89000 + int(ticket), retcode=10009,
            )

        executor.close_position = close_position
        close_time = pd.Timestamp("2026-08-25T13:10:00Z")
        row = pd.Series(
            {"Open": 110.0, "Close": 110.0, "AskOpen": 110.03},
            name=close_time,
        )

        with patch.object(
            live_s23_bot,
            "utc_now",
            return_value=(close_time - pd.Timedelta(seconds=1)).to_pydatetime(),
        ):
            self.assertEqual(
                runner._close_basket(strategy, "basket_target", row, 10.0),
                "requested",
            )
        self.assertEqual(persisted_first_before_second, [True])
        self.assertTrue(state["basket"][0]["close_requested"])
        self.assertTrue(state["basket"][1]["close_requested"])

    def test_close_submission_marker_is_durable_before_broker_command(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor, ticket=96141)
        persisted_snapshots = []
        runner._save_state = lambda: persisted_snapshots.append(
            json.loads(json.dumps(runner.state))
        )
        marker_seen_at_command = []

        def close_position(ticket, _deviation, **_kwargs):
            saved = persisted_snapshots[-1]["strategies"][strategy["id"]]["basket"][0]
            marker_seen_at_command.append(parse_ts(saved.get("close_submission_started_utc")))
            return live_executor.CloseResult(
                True, "CONFIRMED", deal_id=89000 + int(ticket), retcode=10009,
            )

        executor.close_position = close_position
        close_time = pd.Timestamp("2026-08-25T13:10:00Z")
        row = pd.Series(
            {"Open": 110.0, "Close": 110.0, "AskOpen": 110.03},
            name=close_time,
        )

        with patch.object(
            live_s23_bot,
            "utc_now",
            return_value=(close_time - pd.Timedelta(seconds=1)).to_pydatetime(),
        ):
            self.assertEqual(
                runner._close_basket(strategy, "basket_target", row, 10.0),
                "requested",
            )
        self.assertEqual(len(marker_seen_at_command), 1)
        self.assertIsNotNone(marker_seen_at_command[0])
        self.assertLessEqual(marker_seen_at_command[0], close_time)

    def test_unresolved_close_submission_marker_cannot_be_rearmed_or_resent(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor, ticket=96142)
        state["pending_close_reason"] = "basket_stop"
        state["pending_close_signal_bar"] = "2026-08-25T13:10:00+00:00"
        state["basket"][0]["close_submission_started_utc"] = (
            "2026-08-25T13:10:01+00:00"
        )

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(
            state["sync_block_reason"], "close_submission_result_unresolved",
        )
        self.assertEqual(state["pending_close_reason"], "basket_stop")
        self.assertEqual(
            state["basket"][0]["close_submission_started_utc"],
            "2026-08-25T13:10:01+00:00",
        )
        row = pd.Series(
            {"Open": 99.0, "Close": 99.0, "AskOpen": 99.03},
            name=pd.Timestamp("2026-08-25T13:11:00Z"),
        )
        self.assertEqual(
            runner._close_basket(strategy, "basket_stop", row, -2.0),
            "failed",
        )
        self.assertEqual(executor.close_calls, [])

    def test_malformed_close_submission_marker_cannot_be_overwritten_by_direct_close(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor, ticket=96143)
        state["basket"][0]["close_submission_started_utc"] = "not-a-timestamp"
        row = pd.Series(
            {"Open": 99.0, "Close": 99.0, "AskOpen": 99.03},
            name=pd.Timestamp("2026-08-25T13:11:00Z"),
        )

        self.assertEqual(
            runner._close_basket(strategy, "basket_stop", row, -2.0),
            "failed",
        )
        self.assertEqual(executor.close_calls, [])
        self.assertEqual(
            state["sync_block_reason"], "state_position_close_intent_invalid",
        )

        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor, ticket=96144)
        state["pending_close_reason"] = "basket_stop"
        state["pending_close_signal_bar"] = "not-a-timestamp"
        self.assertEqual(
            runner._close_basket(strategy, "basket_stop", row, -2.0),
            "failed",
        )
        self.assertEqual(executor.close_calls, [])
        self.assertEqual(
            state["sync_block_reason"], "state_basket_close_intent_invalid",
        )

    def test_execution_bearing_or_unknown_close_retcodes_are_not_definitive_no_fill(self):
        unresolved_retcodes = (
            10008,  # order placed
            10010,  # partially completed
            10012,  # timeout
            10023,  # order state changed
            10025,  # no changes
            10031,  # no connection
            10036,  # position already closed
            10039,  # close order already exists
            10999,  # unknown future broker code
        )
        for retcode in unresolved_retcodes:
            with self.subTest(retcode=retcode):
                result = live_executor.CloseResult(
                    False, "FAILED", retcode=retcode,
                )
                self.assertFalse(
                    S23HorizontalInventoryRunner._close_result_definitive_no_fill(
                        result
                    )
                )

        definitive_retcodes = (10004, 10006, 10018, 10026, 10027, 10030)
        for retcode in definitive_retcodes:
            with self.subTest(retcode=retcode):
                result = live_executor.CloseResult(
                    False, "FAILED", retcode=retcode,
                )
                self.assertTrue(
                    S23HorizontalInventoryRunner._close_result_definitive_no_fill(
                        result
                    )
                )

    def test_partial_close_retcode_keeps_submission_unresolved_and_prevents_resend(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor, ticket=96145)
        executor.close_position = lambda ticket, *_args, **_kwargs: (
            executor.close_calls.append(int(ticket))
            or live_executor.CloseResult(False, "FAILED", retcode=10010)
        )
        row = pd.Series(
            {"Open": 99.0, "Close": 99.0, "AskOpen": 99.03},
            name=pd.Timestamp("2026-08-25T13:11:00Z"),
        )

        self.assertEqual(
            runner._close_basket(strategy, "basket_stop", row, -2.0),
            "failed",
        )
        self.assertEqual(executor.close_calls, [96145])
        self.assertIsNotNone(
            state["basket"][0].get("close_submission_started_utc")
        )
        self.assertEqual(
            state["sync_block_reason"], "close_submission_result_unresolved",
        )
        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(
            runner._close_basket(strategy, "basket_stop", row, -2.0),
            "failed",
        )
        self.assertEqual(executor.close_calls, [96145])

    def test_close_marker_save_failure_prevents_submitting_the_next_ticket(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        positions = [
            SimpleNamespace(
                ticket=ticket, identifier=ticket, symbol="XAUUSD",
                magic=EXPECTED_S23_MAGIC, comment=strategy["comment_prefix"],
                type=ORDER_TYPE_BUY, volume=0.01, open_price=100.0, open_time=1,
            )
            for ticket in (9615, 9616)
        ]
        executor.positions = positions
        state["basket"] = [
            {
                "ticket": position.ticket,
                "position_identifier": position.identifier,
                "side": "LONG",
                "lot": 0.01,
                "entry_price": 100.0,
                "entry_time_utc": "2026-08-25T13:00:00+00:00",
                "open_time_epoch": 1,
                "owner_symbol": "XAUUSD",
                "owner_magic": EXPECTED_S23_MAGIC,
                "owner_comment": strategy["comment_prefix"],
                "shadow": False,
                "close_requested": False,
            }
            for position in positions
        ]
        bind_owned_basket_identity(strategy, state)
        real_save = runner._save_state

        def fail_after_first_marker():
            if state["basket"][0].get("close_requested"):
                raise OSError("simulated state persistence failure")
            real_save()

        runner._save_state = fail_after_first_marker
        close_time = pd.Timestamp("2026-08-25T13:10:00Z")
        row = pd.Series(
            {"Open": 99.0, "Close": 99.0, "AskOpen": 99.03},
            name=close_time,
        )

        with self.assertRaisesRegex(OSError, "simulated state persistence failure"):
            runner._close_basket(strategy, "basket_stop", row, -2.0)
        self.assertEqual(executor.close_calls, [9615])
        self.assertTrue(state["basket"][0]["close_requested"])
        self.assertFalse(state["basket"][1]["close_requested"])

    def test_each_ticket_is_revalidated_immediately_before_close_submission(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        positions = [
            SimpleNamespace(
                ticket=ticket, identifier=ticket, symbol="XAUUSD",
                magic=EXPECTED_S23_MAGIC, comment=strategy["comment_prefix"],
                type=ORDER_TYPE_BUY, volume=0.01, open_price=100.0, open_time=1,
            )
            for ticket in (9617, 9618)
        ]
        executor.positions = positions
        state["basket"] = [
            {
                "ticket": position.ticket,
                "position_identifier": position.identifier,
                "side": "LONG",
                "lot": 0.01,
                "entry_price": 100.0,
                "entry_time_utc": "2026-08-25T13:00:00+00:00",
                "open_time_epoch": 1,
                "owner_symbol": "XAUUSD",
                "owner_magic": EXPECTED_S23_MAGIC,
                "owner_comment": strategy["comment_prefix"],
                "shadow": False,
                "close_requested": False,
            }
            for position in positions
        ]
        bind_owned_basket_identity(strategy, state)

        def close_position(ticket, _deviation, **_kwargs):
            executor.close_calls.append(int(ticket))
            if int(ticket) == 9617:
                positions[1].magic = EXPECTED_S23_MAGIC + 1
            return live_executor.CloseResult(
                True, "CONFIRMED", deal_id=89000 + int(ticket), retcode=10009,
            )

        executor.close_position = close_position
        row = pd.Series(
            {"Open": 110.0, "Close": 110.0, "AskOpen": 110.03},
            name=pd.Timestamp("2026-08-25T13:10:00Z"),
        )

        self.assertEqual(
            runner._close_basket(strategy, "basket_target", row, 10.0),
            "failed",
        )
        self.assertEqual(executor.close_calls, [9617])
        self.assertTrue(state["basket"][0]["close_requested"])
        self.assertFalse(state["basket"][1]["close_requested"])
        self.assertEqual(
            state["sync_block_reason"], "state_position_ownership_mismatch",
        )

    def test_trend_ticket_is_revalidated_after_durable_intent_before_submission(self):
        runner, _strategy, _state = make_runner(live=True)
        strategy = runner.params["trend_recovery_strategies"][0]
        state = runner._st(strategy)
        executor = CountingExecutor()
        runner.executor = executor
        live_position = SimpleNamespace(
            ticket=9619, identifier=9619, symbol="XAUUSD",
            magic=int(strategy["magic"]), comment=strategy["comment_prefix"],
            type=live_s23_bot.ORDER_TYPE_SELL, volume=0.01, open_time=1,
        )
        executor.positions = [live_position]
        position = {
            "ticket": 9619, "position_identifier": 9619, "side": "SHORT",
            "lot": 0.01, "entry_price": 100.0,
            "entry_time_utc": "2026-08-25T13:00:00+00:00", "open_time_epoch": 1,
            "owner_symbol": "XAUUSD", "owner_magic": int(strategy["magic"]),
            "owner_comment": strategy["comment_prefix"], "shadow": False,
        }
        state["basket"] = [position]
        bind_owned_basket_identity(strategy, state)
        real_save = runner._save_state

        def change_live_owner_while_intent_is_saved():
            if position.get("pending_close_reason"):
                live_position.magic = int(strategy["magic"]) + 1
            real_save()

        runner._save_state = change_live_owner_while_intent_is_saved
        row = pd.Series(
            {"Open": 99.0, "Close": 99.0, "AskOpen": 99.03},
            name=pd.Timestamp("2026-08-25T13:10:00Z"),
        )

        self.assertEqual(
            runner._close_trend_recovery_ticket(
                strategy, position, "trend_ticket_target", row, 1.0,
            ),
            "failed",
        )
        self.assertEqual(executor.close_calls, [])
        self.assertEqual(
            state["sync_block_reason"], "state_position_ownership_mismatch",
        )

    def test_malformed_owned_position_close_intent_fails_closed_while_position_exists(self):
        corruptions = (
            {"close_requested": "false"},
            {"pending_close_reason": 123},
            {"pending_close_reason": "basket_stop", "pending_close_signal_bar": "not-a-timestamp"},
            {"pending_close_reason": "basket_stop", "pending_close_signal_bar": 123},
            {"close_submission_started_utc": 123},
            {"close_submission_started_utc": "not-a-timestamp"},
        )
        for corruption in corruptions:
            with self.subTest(corruption=corruption):
                runner, strategy, state = make_runner(live=True)
                executor = CountingExecutor()
                runner.executor = executor
                arm_owned_basket(strategy, state, executor)
                state["basket"][0].update(corruption)

                self.assertFalse(runner._sync_strategy(strategy))
                self.assertEqual(
                    state["sync_block_reason"], "state_position_close_intent_invalid",
                )
                self.assertEqual(executor.close_calls, [])

    def test_malformed_owned_basket_close_intent_fails_closed_while_position_exists(self):
        corruptions = (
            {"pending_close_reason": 123, "pending_close_signal_bar": None},
            {"pending_close_reason": "basket_stop", "pending_close_signal_bar": "not-a-timestamp"},
            {"pending_close_reason": "basket_stop", "pending_close_signal_bar": 123},
            {"pending_close_reason": None, "pending_close_signal_bar": "2026-08-25T13:10:00Z"},
        )
        for corruption in corruptions:
            with self.subTest(corruption=corruption):
                runner, strategy, state = make_runner(live=True)
                executor = CountingExecutor()
                runner.executor = executor
                arm_owned_basket(strategy, state, executor)
                state.update(corruption)

                self.assertFalse(runner._sync_strategy(strategy))
                self.assertEqual(
                    state["sync_block_reason"], "state_basket_close_intent_invalid",
                )
                self.assertEqual(executor.close_calls, [])

        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["basket"][0]["close_submission_started_utc"] = (
            "2026-08-25T13:10:01Z"
        )
        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(
            state["sync_block_reason"], "state_basket_close_intent_invalid",
        )
        self.assertEqual(executor.close_calls, [])

    def test_close_requested_without_basket_close_intent_fails_closed(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["basket"][0]["close_requested"] = True

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(
            state["sync_block_reason"], "state_basket_close_intent_invalid",
        )
        self.assertEqual(executor.close_calls, [])

    def test_malformed_basket_container_fails_closed_before_reconciliation(self):
        for malformed in ({"ticket": 9401}, 1, "ticket=9401"):
            with self.subTest(malformed=malformed):
                runner, strategy, state = make_runner(live=True)
                executor = CountingExecutor()
                runner.executor = executor
                arm_owned_basket(strategy, state, executor)
                state["basket"] = malformed

                self.assertFalse(runner._sync_strategy(strategy))
                self.assertEqual(state["sync_block_reason"], "state_position_identity_invalid")
                self.assertFalse(state["sync_block_recoverable"])
                self.assertEqual(executor.close_calls, [])
                opportunity, row, _poll_time, info = sample_opportunity()
                context = runner._shadow_context(
                    row,
                    info,
                    {int(strategy["lane_id"]): (False, "state_position_identity_invalid", False)},
                )
                self.assertEqual(context["portfolio_positions"], 0)
                self.assertEqual(runner._inventory_range_snapshot()[3:], (0, 0))

    def test_manual_close_deal_magic_zero_uses_position_lifecycle_identity(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        executor.positions = []
        executor.close_deal = SimpleNamespace(
            position_id=9401,
            symbol="XAUUSD",
            magic=0,
            net_profit=1.25,
            price=101.0,
            deal=79611,
            exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-25T13:10:02Z").timestamp()),
        )

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertEqual(state["basket"], [])
        self.assertFalse(state["sync_block_new_entries"])

    def test_malformed_reverse_flag_cannot_arm_trend_episode_after_confirmed_stop(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        executor.positions = []
        state.update({
            "current_basket_id": "L1-B000001",
            "pending_close_reason": "basket_stop",
            "pending_close_signal_bar": "2026-08-25T13:10:00Z",
            "frozen_basket_atr30": 2.5,
            "reverse_used": "false",
        })
        executor.close_deal = SimpleNamespace(
            position_id=9401,
            symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC,
            net_profit=-18.0,
            price=99.0,
            deal=79612,
            exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-25T13:10:02Z").timestamp()),
        )

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertFalse(runner._trend_recovery_state()["active"])
        self.assertFalse(state["reverse_used"])

    def test_cross_day_close_deals_are_accounted_in_broker_time_order(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        executor.positions = []
        state["basket"] = [
            {
                "ticket": ticket, "position_identifier": ticket, "side": "LONG",
                "lot": 0.01, "entry_price": 100.0,
                "entry_time_utc": "2026-08-25T22:00:00+00:00", "open_time_epoch": 1,
                "owner_symbol": "XAUUSD", "owner_magic": EXPECTED_S23_MAGIC,
                "owner_comment": strategy["comment_prefix"], "shadow": False,
            }
            for ticket in (9631, 9632)
        ]
        bind_owned_basket_identity(strategy, state)
        deals = {
            9631: SimpleNamespace(
                position_id=9631, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
                net_profit=-7.0, price=99.0, deal=79631, exit_volume=0.01,
                deal_time=int(pd.Timestamp("2026-08-26T00:01:00Z").timestamp()),
            ),
            9632: SimpleNamespace(
                position_id=9632, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
                net_profit=3.0, price=101.0, deal=79632, exit_volume=0.01,
                deal_time=int(pd.Timestamp("2026-08-25T23:59:00Z").timestamp()),
            ),
        }
        executor.get_position_close_deal = lambda position_id, _opened_at_epoch: deals[int(position_id)]

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertEqual(state["daily_realized_date_utc"], "2026-08-26")
        self.assertEqual(state["daily_realized_pnl_usd"], -7.0)

    def test_late_confirmed_prior_day_close_cannot_roll_daily_loss_state_backward(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        executor.positions = []
        state["daily_realized_date_utc"] = "2026-08-26"
        state["daily_realized_pnl_usd"] = -12.0
        executor.close_deal = SimpleNamespace(
            position_id=9401,
            symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC,
            net_profit=50.0,
            price=101.0,
            deal=79640,
            exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-25T23:59:00Z").timestamp()),
        )

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertEqual(state["basket"], [])
        self.assertEqual(state["daily_realized_date_utc"], "2026-08-26")
        self.assertEqual(state["daily_realized_pnl_usd"], -12.0)

    def test_older_entry_clock_cannot_roll_daily_loss_gate_backward(self):
        runner, strategy, state = make_runner(live=True)
        runner.params["daily_realized_loss_limit_usd"] = 10.0
        state["daily_realized_date_utc"] = "2026-08-26"
        state["daily_realized_pnl_usd"] = -12.0

        reason = runner._new_basket_block_reason(
            strategy,
            pd.Timestamp("2026-08-25T23:59:00Z"),
        )

        self.assertEqual(reason, "daily_realized_loss_limit")
        self.assertEqual(state["daily_realized_date_utc"], "2026-08-26")
        self.assertEqual(state["daily_realized_pnl_usd"], -12.0)

    def test_malformed_daily_realized_pnl_blocks_entry_without_exception(self):
        runner, strategy, state = make_runner(live=True)
        runner.params["daily_realized_loss_limit_usd"] = 10.0
        state["daily_realized_date_utc"] = "2026-08-26"
        state["daily_realized_pnl_usd"] = "corrupt"

        reason = runner._new_basket_block_reason(
            strategy,
            pd.Timestamp("2026-08-26T01:00:00Z"),
        )

        self.assertEqual(reason, "daily_realized_state_invalid")
        self.assertEqual(state["daily_realized_pnl_usd"], "corrupt")

    def test_coercible_non_numeric_daily_realized_pnl_still_blocks_entry(self):
        for label, value in (("numeric_string", "1000.0"), ("boolean", True)):
            with self.subTest(label=label):
                runner, strategy, state = make_runner(live=True)
                runner.params["daily_realized_loss_limit_usd"] = 10.0
                state["daily_realized_date_utc"] = "2026-08-26"
                state["daily_realized_pnl_usd"] = value

                reason = runner._new_basket_block_reason(
                    strategy,
                    pd.Timestamp("2026-08-26T01:00:00Z"),
                )

                self.assertEqual(reason, "daily_realized_state_invalid")
                self.assertEqual(state["daily_realized_pnl_usd"], value)

    def test_empty_daily_realized_date_cannot_reset_unknown_loss_state(self):
        runner, strategy, state = make_runner(live=True)
        runner.params["daily_realized_loss_limit_usd"] = 10.0
        state["daily_realized_date_utc"] = ""
        state["daily_realized_pnl_usd"] = -12.0

        reason = runner._new_basket_block_reason(
            strategy,
            pd.Timestamp("2026-08-26T01:00:00Z"),
        )

        self.assertEqual(reason, "daily_realized_state_invalid")
        self.assertEqual(state["daily_realized_date_utc"], "")
        self.assertEqual(state["daily_realized_pnl_usd"], -12.0)

    def test_close_confirmation_survives_malformed_daily_realized_pnl(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        executor.positions = []
        state["daily_realized_date_utc"] = "2026-08-26"
        state["daily_realized_pnl_usd"] = "corrupt"
        executor.close_deal = SimpleNamespace(
            position_id=9401,
            symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC,
            net_profit=-5.0,
            price=99.0,
            deal=79641,
            exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-26T01:00:00Z").timestamp()),
        )

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertEqual(state["basket"], [])
        self.assertEqual(state["daily_realized_pnl_usd"], "corrupt")
        self.assertEqual(
            runner._new_basket_block_reason(
                strategy,
                pd.Timestamp("2026-08-26T01:01:00Z"),
            ),
            "daily_realized_state_invalid",
        )

    def test_future_daily_realized_date_blocks_entry_without_rewriting_state(self):
        runner, strategy, state = make_runner(live=True)
        runner.params["daily_realized_loss_limit_usd"] = 10.0
        state["daily_realized_date_utc"] = "2026-08-27"
        state["daily_realized_pnl_usd"] = 5.0

        reason = runner._new_basket_block_reason(
            strategy,
            pd.Timestamp("2026-08-26T01:00:00Z"),
        )

        self.assertEqual(reason, "daily_realized_state_invalid")
        self.assertEqual(state["daily_realized_date_utc"], "2026-08-27")
        self.assertEqual(state["daily_realized_pnl_usd"], 5.0)

    def test_partial_close_deal_cannot_prove_full_close_while_ticket_still_exists(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        still_live = executor.positions[0]
        executor.positions = []
        executor.get_position = lambda _ticket: still_live
        executor.close_deal = SimpleNamespace(
            position_id=9401, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
            net_profit=-1.0, price=99.0, deal=79633, exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-25T13:10:02Z").timestamp()),
        )

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(state["sync_block_reason"], "position_inventory_inconsistent")
        self.assertEqual(len(state["basket"]), 1)
        self.assertTrue(state["sync_block_recoverable"])

    def test_missing_ticket_direct_query_outage_cannot_finalize_close(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        executor.positions = []
        executor.get_position = lambda _ticket: None

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(state["sync_block_reason"], "position_absence_unconfirmed")
        self.assertEqual(len(state["basket"]), 1)
        self.assertTrue(state["sync_block_recoverable"])

    def test_live_ticket_mismatch_blocks_reconciliation_even_when_identifier_matches(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["basket"][0]["ticket"] = 9999

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(state["sync_block_reason"], "state_position_ownership_mismatch")
        self.assertFalse(state["sync_block_recoverable"])

    def test_close_deal_exit_volume_must_match_original_position_lot(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        executor.positions = []
        executor.close_deal = SimpleNamespace(
            position_id=9401, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
            net_profit=-1.0, price=99.0, deal=79641, exit_volume=0.005,
            deal_time=int(pd.Timestamp("2026-08-25T13:10:02Z").timestamp()),
        )

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(state["sync_block_reason"], "close_deal_volume_mismatch")
        self.assertEqual(len(state["basket"]), 1)
        self.assertFalse(state["sync_block_recoverable"])

    def test_duplicate_persisted_position_identity_fails_closed(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["basket"].append(dict(state["basket"][0]))

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(state["sync_block_reason"], "state_position_identity_invalid")
        self.assertFalse(state["sync_block_recoverable"])
        self.assertEqual(executor.close_calls, [])

    def test_nonnumeric_persisted_position_identity_fails_closed_without_exception(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["basket"][0]["position_identifier"] = "broken"

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(state["sync_block_reason"], "state_position_identity_invalid")
        self.assertFalse(state["sync_block_recoverable"])

    def test_coercible_persisted_position_identity_fails_closed(self):
        for field in ("ticket", "position_identifier"):
            for value in ("9401", 9401.0, True):
                with self.subTest(field=field, value=value):
                    runner, strategy, state = make_runner(live=True)
                    executor = CountingExecutor()
                    runner.executor = executor
                    arm_owned_basket(strategy, state, executor)
                    state["basket"][0][field] = value

                    self.assertFalse(runner._sync_strategy(strategy))
                    self.assertEqual(
                        state["sync_block_reason"], "state_position_identity_invalid",
                    )
                    self.assertFalse(state["sync_block_recoverable"])

    def test_actual_open_boundary_rejects_corrupt_existing_basket_identity(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["basket"][0]["ticket"] = "9401"
        opportunity, row, poll_time, info = sample_opportunity()

        with patch.object(live_s23_bot, "utc_now", return_value=poll_time.to_pydatetime()):
            self.assertFalse(
                runner._open_entry(
                    strategy,
                    "LONG",
                    row,
                    info,
                    execution_time=poll_time,
                    opportunity=opportunity,
                )
            )

        self.assertEqual(executor.open_calls, 0)

    def test_duplicate_live_position_identity_fails_closed(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        duplicate = SimpleNamespace(**vars(executor.positions[0]))
        duplicate.ticket = 9402
        executor.positions.append(duplicate)

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(state["sync_block_reason"], "live_position_identity_invalid")
        self.assertFalse(state["sync_block_recoverable"])

    def test_invalid_persisted_lot_fails_closed_without_exception(self):
        for value in ("broken", "0.01", True):
            with self.subTest(value=value):
                runner, strategy, state = make_runner(live=True)
                executor = CountingExecutor()
                runner.executor = executor
                arm_owned_basket(strategy, state, executor)
                state["basket"][0]["lot"] = value

                self.assertFalse(runner._sync_strategy(strategy))
                self.assertEqual(state["sync_block_reason"], "state_position_ownership_mismatch")
                self.assertFalse(state["sync_block_recoverable"])

    def test_invalid_persisted_owner_magic_fails_closed_without_exception(self):
        for value in ("broken", str(EXPECTED_S23_MAGIC), True):
            with self.subTest(value=value):
                runner, strategy, state = make_runner(live=True)
                executor = CountingExecutor()
                runner.executor = executor
                arm_owned_basket(strategy, state, executor)
                state["basket"][0]["owner_magic"] = value

                self.assertFalse(runner._sync_strategy(strategy))
                self.assertEqual(state["sync_block_reason"], "state_position_ownership_mismatch")
                self.assertFalse(state["sync_block_recoverable"])

    def test_persisted_position_lane_and_basket_identity_must_match_parent(self):
        corruptions = (
            ("lane_id", 2),
            ("lane_id", True),
            ("basket_id", "L1-B000002"),
            ("basket_id", 1),
        )
        for field, value in corruptions:
            with self.subTest(field=field, value=value):
                runner, strategy, state = make_runner(live=True)
                executor = CountingExecutor()
                runner.executor = executor
                arm_owned_basket(strategy, state, executor)
                state["basket"][0][field] = value

                self.assertFalse(runner._sync_strategy(strategy))
                self.assertEqual(
                    state["sync_block_reason"], "state_position_ownership_mismatch",
                )
                self.assertFalse(state["sync_block_recoverable"])

    def test_mixed_side_owned_basket_fails_closed_before_exit_monitoring(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        second_position = SimpleNamespace(**vars(executor.positions[0]))
        second_position.ticket = 9402
        second_position.identifier = 9402
        second_position.type = live_s23_bot.ORDER_TYPE_SELL
        executor.positions.append(second_position)
        second_state = dict(state["basket"][0])
        second_state.update({
            "ticket": 9402,
            "position_identifier": 9402,
            "side": "SHORT",
        })
        state["basket"].append(second_state)

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(
            state["sync_block_reason"], "state_basket_side_inconsistent",
        )
        self.assertFalse(state["sync_block_recoverable"])
        self.assertEqual(executor.close_calls, [])

    def test_nonnumeric_persisted_open_time_fails_closed_without_exception(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        executor.positions = []
        state["basket"][0]["open_time_epoch"] = "broken"

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(state["sync_block_reason"], "state_position_lifecycle_invalid")
        self.assertFalse(state["sync_block_recoverable"])

    def test_invalid_persisted_open_time_cannot_use_wide_close_history(self):
        for open_time_epoch in (0, -1, "1", True, 1.5):
            with self.subTest(open_time_epoch=open_time_epoch):
                runner, strategy, state = make_runner(live=True)
                executor = CountingExecutor()
                runner.executor = executor
                arm_owned_basket(strategy, state, executor)
                executor.positions = []
                state["basket"][0]["open_time_epoch"] = open_time_epoch

                self.assertFalse(runner._sync_strategy(strategy))
                self.assertEqual(
                    state["sync_block_reason"],
                    "state_position_lifecycle_invalid",
                )
                self.assertEqual(len(state["basket"]), 1)
                self.assertFalse(state["sync_block_recoverable"])

    def test_common_lane_requires_broker_open_time_for_close_lifecycle(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        executor.positions[0].open_time = 0

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(
            state["sync_block_reason"],
            "confirmed_fill_time_unavailable",
        )
        self.assertTrue(state["sync_block_recoverable"])
        self.assertEqual(len(state["basket"]), 1)

    def test_common_close_rechecks_exact_live_volume_before_submission(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        executor.positions[0].volume = 0.02
        row = pd.Series(
            {"Open": 99.0, "Close": 99.0, "AskOpen": 99.03},
            name=pd.Timestamp("2026-08-25T13:10:00Z"),
        )

        self.assertEqual(runner._close_basket(strategy, "basket_stop", row, -1.0), "failed")
        self.assertEqual(state["sync_block_reason"], "state_position_ownership_mismatch")
        self.assertEqual(executor.close_calls, [])

    def test_common_close_atomic_account_guard_is_nonrecoverable(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        executor.close_position = lambda *_args, **_kwargs: live_executor.CloseResult(
            False, "ACCOUNT_IDENTITY_GUARD",
        )
        row = pd.Series(
            {"Open": 99.0, "Close": 99.0, "AskOpen": 99.03},
            name=pd.Timestamp("2026-08-25T13:10:00Z"),
        )

        self.assertEqual(
            runner._close_basket(strategy, "basket_stop", row, -1.0), "failed",
        )
        self.assertEqual(state["sync_block_reason"], "account_identity_mismatch")
        self.assertFalse(state["sync_block_recoverable"])

    def test_trend_close_rechecks_exact_live_volume_before_submission(self):
        runner, _strategy, _state = make_runner(live=True)
        strategy = runner.params["trend_recovery_strategies"][0]
        state = runner._st(strategy)
        executor = CountingExecutor()
        runner.executor = executor
        executor.positions = [SimpleNamespace(
            ticket=9622, identifier=9622, symbol="XAUUSD",
            magic=int(strategy["magic"]), comment=strategy["comment_prefix"],
            type=live_s23_bot.ORDER_TYPE_SELL, volume=0.02, open_time=1,
        )]
        position = {
            "ticket": 9622, "position_identifier": 9622, "side": "SHORT",
            "lot": 0.01, "entry_price": 100.0,
            "entry_time_utc": "2026-08-25T12:00:00+00:00", "open_time_epoch": 1,
            "owner_symbol": "XAUUSD", "owner_magic": int(strategy["magic"]),
            "owner_comment": strategy["comment_prefix"], "shadow": False,
        }
        state["basket"] = [position]
        row = pd.Series(
            {"Open": 99.0, "Close": 99.0, "AskOpen": 99.03},
            name=pd.Timestamp("2026-08-25T13:10:00Z"),
        )

        self.assertEqual(
            runner._close_trend_recovery_ticket(strategy, position, "trend_ticket_target", row, 1.0),
            "failed",
        )
        self.assertEqual(state["sync_block_reason"], "state_position_ownership_mismatch")
        self.assertEqual(executor.close_calls, [])

    def test_trend_close_atomic_account_guard_is_nonrecoverable(self):
        runner, _strategy, _state = make_runner(live=True)
        strategy = runner.params["trend_recovery_strategies"][0]
        state = runner._st(strategy)
        executor = CountingExecutor()
        runner.executor = executor
        position = SimpleNamespace(
            ticket=9623, identifier=9623, symbol="XAUUSD",
            magic=int(strategy["magic"]), comment=strategy["comment_prefix"],
            type=live_s23_bot.ORDER_TYPE_SELL, volume=0.01, open_price=100.0,
            open_time=int(pd.Timestamp("2026-08-25T12:00:00Z").timestamp()),
        )
        executor.positions = [position]
        state_position = {
            "ticket": 9623, "position_identifier": 9623, "side": "SHORT",
            "lot": 0.01, "entry_price": 100.0,
            "entry_time_utc": "2026-08-25T12:00:00+00:00",
            "open_time_epoch": int(pd.Timestamp("2026-08-25T12:00:00Z").timestamp()),
            "owner_symbol": "XAUUSD", "owner_magic": int(strategy["magic"]),
            "owner_comment": strategy["comment_prefix"], "shadow": False,
        }
        state["basket"] = [state_position]
        bind_owned_basket_identity(strategy, state)
        executor.close_position = lambda *_args, **_kwargs: live_executor.CloseResult(
            False, "ACCOUNT_IDENTITY_GUARD",
        )
        row = pd.Series(
            {"Open": 99.0, "Close": 99.0, "AskOpen": 99.03},
            name=pd.Timestamp("2026-08-25T13:10:00Z"),
        )

        self.assertEqual(
            runner._close_trend_recovery_ticket(
                strategy, state_position, "trend_ticket_target", row, 1.0,
            ),
            "failed",
        )
        self.assertEqual(state["sync_block_reason"], "account_identity_mismatch")
        self.assertFalse(state["sync_block_recoverable"])

    def test_common_lane_restores_broker_open_time_before_elapsed_exits(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        broker_open = pd.Timestamp("2026-08-25T13:00:00Z")
        executor.positions[0].open_time = int(broker_open.timestamp())
        state["basket"][0]["entry_time_utc"] = "2026-08-25T12:00:00+00:00"
        state["basket"][0]["open_time_epoch"] = int(
            pd.Timestamp("2026-08-25T12:00:00Z").timestamp()
        )

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertEqual(parse_ts(state["basket"][0]["entry_time_utc"]), broker_open)
        self.assertEqual(
            state["basket"][0]["open_time_epoch"], int(broker_open.timestamp()),
        )

    def test_common_lane_restores_malformed_open_epoch_even_when_utc_text_matches(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        broker_open = pd.Timestamp("2026-08-25T13:00:00Z")
        executor.positions[0].open_time = int(broker_open.timestamp())
        state["basket"][0]["entry_time_utc"] = dt_text(broker_open)
        state["basket"][0]["open_time_epoch"] = "not-an-integer"

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertEqual(
            state["basket"][0]["open_time_epoch"], int(broker_open.timestamp()),
        )

    def test_common_lane_restores_broker_entry_price_before_pnl_exits(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["basket"][0]["entry_price"] = float("nan")
        executor.positions[0].open_price = 100.25

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertEqual(state["basket"][0]["entry_price"], 100.25)

    def test_common_lane_restores_last_add_anchor_with_broker_entry_price(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["basket"][0]["entry_price"] = float("nan")
        state["last_add_price"] = 999.0
        executor.positions[0].open_price = 100.25

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertEqual(state["basket"][0]["entry_price"], 100.25)
        self.assertEqual(state["last_add_price"], 100.25)

    def test_common_lane_uses_later_basket_position_when_broker_open_seconds_tie(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        executor.positions.append(SimpleNamespace(
            ticket=9402,
            identifier=9402,
            symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC,
            comment=strategy["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=0.01,
            open_price=101.0,
            open_time=1,
        ))
        second = dict(state["basket"][0])
        second.update({
            "ticket": 9402,
            "position_identifier": 9402,
            "entry_price": 101.0,
        })
        state["basket"].append(second)
        state["last_add_price"] = 999.0

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertEqual(state["last_add_price"], 101.0)

    def test_common_max_hold_market_closed_uses_fresh_quote_retry_cooldown(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["basket"][0]["entry_time_utc"] = "2026-08-25T12:00:00+00:00"
        close_attempts = []

        def close_position(ticket, _deviation, **_kwargs):
            close_attempts.append(ticket)
            return live_executor.CloseResult(
                False, "MARKET_CLOSED", retcode=10018,
            )

        executor.close_position = close_position
        first_quote = pd.Timestamp("2026-08-25T14:00:00Z")
        info = SimpleNamespace(
            bid=99.0,
            ask=99.03,
            quote_time_msc=int(first_quote.timestamp() * 1000),
        )
        row = pd.Series(
            {"Open": 99.0, "Close": 99.0, "AskOpen": 99.03},
            name=first_quote,
        )

        self.assertTrue(runner._monitor_open_basket(strategy, info, row, first_quote))
        self.assertEqual(close_attempts, [9401])
        self.assertFalse(state["sync_block_new_entries"])
        self.assertEqual(
            parse_ts(state["time_close_retry_after_utc"]),
            first_quote + pd.Timedelta(seconds=60),
        )
        second_quote = first_quote + pd.Timedelta(seconds=30)
        info.quote_time_msc = int(second_quote.timestamp() * 1000)
        row.name = second_quote
        self.assertTrue(runner._monitor_open_basket(strategy, info, row, second_quote))
        self.assertEqual(close_attempts, [9401])

    def test_trend_ticket_market_closed_uses_fresh_quote_retry_cooldown(self):
        runner, _strategy, _state = make_runner(live=True)
        strategy = runner.params["trend_recovery_strategies"][0]
        state = runner._st(strategy)
        executor = CountingExecutor()
        runner.executor = executor
        executor.positions = [SimpleNamespace(
            ticket=9621, identifier=9621, symbol="XAUUSD",
            magic=int(strategy["magic"]), comment=strategy["comment_prefix"],
            type=live_s23_bot.ORDER_TYPE_SELL, volume=0.01, open_time=1,
        )]
        state["basket"] = [{
            "ticket": 9621, "position_identifier": 9621, "side": "SHORT",
            "lot": 0.01, "entry_price": 100.0,
            "entry_time_utc": "2026-08-25T12:00:00+00:00", "open_time_epoch": 1,
            "owner_symbol": "XAUUSD", "owner_magic": int(strategy["magic"]),
            "owner_comment": strategy["comment_prefix"], "shadow": False,
        }]
        bind_owned_basket_identity(strategy, state)
        close_attempts = []

        def close_position(ticket, _deviation, **_kwargs):
            close_attempts.append(ticket)
            return live_executor.CloseResult(
                False, "MARKET_CLOSED", retcode=10018,
            )

        executor.close_position = close_position
        first_quote = pd.Timestamp("2026-08-25T14:00:00Z")
        info = SimpleNamespace(
            bid=99.0,
            ask=99.03,
            quote_time_msc=int(first_quote.timestamp() * 1000),
        )

        self.assertFalse(runner._process_trend_recovery_exits(info, first_quote))
        self.assertEqual(close_attempts, [9621])
        self.assertFalse(state["sync_block_new_entries"])
        self.assertEqual(
            parse_ts(state["time_close_retry_after_utc"]),
            first_quote + pd.Timedelta(seconds=60),
        )
        second_quote = first_quote + pd.Timedelta(seconds=30)
        info.quote_time_msc = int(second_quote.timestamp() * 1000)
        self.assertFalse(runner._process_trend_recovery_exits(info, second_quote))
        self.assertEqual(close_attempts, [9621])

    def test_common_close_trade_permission_reject_uses_cooldown_and_escalates(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["basket"][0]["entry_time_utc"] = "2026-08-25T12:00:00+00:00"
        runner.params["trade_permission_retry_seconds"] = 60.0
        runner.params["trade_permission_alert_threshold"] = 2
        close_attempts = []
        alerts = []

        def close_position(ticket, _deviation, **_kwargs):
            close_attempts.append(ticket)
            if len(close_attempts) >= 3:
                return live_executor.CloseResult(
                    True, "CONFIRMED", deal_id=89401, retcode=10009,
                )
            return live_executor.CloseResult(False, "FAILED", retcode=10027)

        executor.close_position = close_position
        runner._notify_manual_action = lambda *_args, **kwargs: (alerts.append(kwargs) or True)
        first_quote = pd.Timestamp("2026-08-25T14:00:00Z")

        def monitor(at):
            info = SimpleNamespace(
                bid=99.0, ask=99.03,
                quote_time_msc=int(at.timestamp() * 1000),
            )
            row = pd.Series(
                {"Open": 99.0, "Close": 99.0, "AskOpen": 99.03}, name=at,
            )
            return runner._monitor_open_basket(strategy, info, row, at)

        self.assertTrue(monitor(first_quote))
        self.assertEqual(close_attempts, [9401])
        self.assertEqual(state["close_trade_permission_reject_streak"], 1)
        self.assertEqual(
            parse_ts(state["time_close_retry_after_utc"]),
            first_quote + pd.Timedelta(seconds=60),
        )
        self.assertTrue(runner._sync_strategy(strategy))
        self.assertTrue(monitor(first_quote + pd.Timedelta(seconds=30)))
        self.assertEqual(close_attempts, [9401])

        self.assertTrue(monitor(first_quote + pd.Timedelta(seconds=61)))
        self.assertEqual(close_attempts, [9401, 9401])
        self.assertEqual(state["close_trade_permission_reject_streak"], 2)
        self.assertEqual(len(alerts), 1)
        self.assertTrue(runner._sync_strategy(strategy))
        self.assertTrue(monitor(first_quote + pd.Timedelta(seconds=122)))
        self.assertEqual(close_attempts, [9401, 9401, 9401])
        self.assertEqual(state["close_trade_permission_reject_streak"], 0)
        self.assertFalse(state["close_trade_permission_reject_notified"])
        self.assertIsNone(state["time_close_retry_after_utc"])
        self.assertTrue(state["basket"][0]["close_requested"])

    def test_close_permission_alert_is_independent_of_prior_open_permission_alert(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["basket"][0]["entry_time_utc"] = "2026-08-25T12:00:00+00:00"
        state["autotrading_reject_streak"] = 3
        state["autotrading_reject_notified"] = True
        runner.params["trade_permission_alert_threshold"] = 1
        alerts = []
        runner._notify_manual_action = lambda *_args, **kwargs: (alerts.append(kwargs) or True)
        executor.close_position = lambda *_args, **_kwargs: live_executor.CloseResult(
            False, "FAILED", retcode=10027,
        )
        quote_time = pd.Timestamp("2026-08-25T14:00:00Z")
        info = SimpleNamespace(
            bid=99.0, ask=99.03,
            quote_time_msc=int(quote_time.timestamp() * 1000),
        )
        row = pd.Series(
            {"Open": 99.0, "Close": 99.0, "AskOpen": 99.03}, name=quote_time,
        )

        self.assertTrue(runner._monitor_open_basket(strategy, info, row, quote_time))
        self.assertEqual(len(alerts), 1)
        self.assertIn("close trade permission", alerts[0]["title"])
        self.assertEqual(state["autotrading_reject_streak"], 3)
        self.assertTrue(state["autotrading_reject_notified"])
        self.assertEqual(state["close_trade_permission_reject_streak"], 1)
        self.assertTrue(state["close_trade_permission_reject_notified"])

    def test_failed_close_permission_alert_remains_retryable(self):
        runner, strategy, state = make_runner(live=True)
        runner.params["trade_permission_alert_threshold"] = 1
        runner._notify_manual_action = Mock(return_value=False)
        result = live_executor.CloseResult(False, "TRADE_PERMISSION_GUARD")

        self.assertTrue(
            runner._record_close_trade_permission_reject(
                strategy, result, pd.Timestamp("2026-08-25T14:00:00Z"),
            )
        )
        self.assertFalse(state["close_trade_permission_reject_notified"])
        runner._notify_manual_action.assert_called_once()

    def test_trend_close_trade_permission_guard_uses_cooldown(self):
        runner, _strategy, _state = make_runner(live=True)
        strategy = runner.params["trend_recovery_strategies"][0]
        state = runner._st(strategy)
        executor = CountingExecutor()
        runner.executor = executor
        executor.positions = [SimpleNamespace(
            ticket=9622, identifier=9622, symbol="XAUUSD",
            magic=int(strategy["magic"]), comment=strategy["comment_prefix"],
            type=live_s23_bot.ORDER_TYPE_SELL, volume=0.01, open_price=100.0,
            open_time=1,
        )]
        state["basket"] = [{
            "ticket": 9622, "position_identifier": 9622, "side": "SHORT",
            "lot": 0.01, "entry_price": 100.0,
            "entry_time_utc": "2026-08-25T12:00:00+00:00", "open_time_epoch": 1,
            "owner_symbol": "XAUUSD", "owner_magic": int(strategy["magic"]),
            "owner_comment": strategy["comment_prefix"], "shadow": False,
        }]
        bind_owned_basket_identity(strategy, state)
        runner.params["trade_permission_retry_seconds"] = 60.0
        close_attempts = []
        executor.close_position = lambda ticket, _deviation, **_kwargs: (
            close_attempts.append(ticket)
            or live_executor.CloseResult(False, "TRADE_PERMISSION_GUARD")
        )
        first_quote = pd.Timestamp("2026-08-25T14:00:00Z")
        info = SimpleNamespace(
            bid=99.0, ask=99.03,
            quote_time_msc=int(first_quote.timestamp() * 1000),
        )

        self.assertFalse(runner._process_trend_recovery_exits(info, first_quote))
        self.assertEqual(close_attempts, [9622])
        second_quote = first_quote + pd.Timedelta(seconds=30)
        info.quote_time_msc = int(second_quote.timestamp() * 1000)
        self.assertFalse(runner._process_trend_recovery_exits(info, second_quote))
        self.assertEqual(close_attempts, [9622])

    def test_numeric_future_close_retry_cannot_delay_common_or_trend_exit(self):
        quote_time = pd.Timestamp("2026-08-25T14:00:00Z")

        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["basket"][0]["entry_time_utc"] = "2026-08-25T12:00:00+00:00"
        state["time_close_retry_after_utc"] = int(
            (quote_time + pd.Timedelta(days=1)).value
        )
        info = SimpleNamespace(
            bid=99.0,
            ask=99.03,
            quote_time_msc=int(quote_time.timestamp() * 1000),
        )
        row = pd.Series(
            {"Open": 99.0, "Close": 99.0, "AskOpen": 99.03},
            name=quote_time,
        )
        self.assertTrue(runner._monitor_open_basket(strategy, info, row, quote_time))
        self.assertEqual(executor.close_calls, [9401])
        self.assertIsNone(state["time_close_retry_after_utc"])

        trend_runner, _strategy, _state = make_runner(live=True)
        trend = trend_runner.params["trend_recovery_strategies"][0]
        trend_state = trend_runner._st(trend)
        trend_executor = CountingExecutor()
        trend_runner.executor = trend_executor
        trend_executor.positions = [SimpleNamespace(
            ticket=9622, identifier=9622, symbol="XAUUSD",
            magic=int(trend["magic"]), comment=trend["comment_prefix"],
            type=live_s23_bot.ORDER_TYPE_SELL, volume=0.01,
            open_price=100.0, open_time=1,
        )]
        trend_state["basket"] = [{
            "ticket": 9622, "position_identifier": 9622, "side": "SHORT",
            "lot": 0.01, "entry_price": 100.0,
            "entry_time_utc": "2026-08-25T12:00:00+00:00", "open_time_epoch": 1,
            "owner_symbol": "XAUUSD", "owner_magic": int(trend["magic"]),
            "owner_comment": trend["comment_prefix"], "shadow": False,
        }]
        bind_owned_basket_identity(trend, trend_state)
        trend_state["time_close_retry_after_utc"] = int(
            (quote_time + pd.Timedelta(days=1)).value
        )
        trend_info = SimpleNamespace(
            bid=109.97,
            ask=110.0,
            quote_time_msc=int(quote_time.timestamp() * 1000),
        )
        self.assertFalse(
            trend_runner._process_trend_recovery_exits(trend_info, quote_time)
        )
        self.assertEqual(trend_executor.close_calls, [9622])
        self.assertIsNone(trend_state["time_close_retry_after_utc"])

    def test_out_of_bound_iso_close_retry_cannot_delay_common_or_trend_exit(self):
        quote_time = pd.Timestamp("2026-08-25T14:00:00Z")
        future_retry = dt_text(quote_time + pd.Timedelta(days=1))

        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["basket"][0]["entry_time_utc"] = "2026-08-25T12:00:00+00:00"
        state["time_close_retry_after_utc"] = future_retry
        info = SimpleNamespace(
            bid=99.0, ask=99.03,
            quote_time_msc=int(quote_time.timestamp() * 1000),
        )
        row = pd.Series(
            {"Open": 99.0, "Close": 99.0, "AskOpen": 99.03},
            name=quote_time,
        )
        self.assertTrue(runner._monitor_open_basket(strategy, info, row, quote_time))
        self.assertEqual(executor.close_calls, [9401])
        self.assertIsNone(state["time_close_retry_after_utc"])

        trend_runner, _strategy, _state = make_runner(live=True)
        trend = trend_runner.params["trend_recovery_strategies"][0]
        trend_state = trend_runner._st(trend)
        trend_executor = CountingExecutor()
        trend_runner.executor = trend_executor
        trend_executor.positions = [SimpleNamespace(
            ticket=9623, identifier=9623, symbol="XAUUSD",
            magic=int(trend["magic"]), comment=trend["comment_prefix"],
            type=live_s23_bot.ORDER_TYPE_SELL, volume=0.01,
            open_price=100.0, open_time=1,
        )]
        trend_state["basket"] = [{
            "ticket": 9623, "position_identifier": 9623, "side": "SHORT",
            "lot": 0.01, "entry_price": 100.0,
            "entry_time_utc": "2026-08-25T12:00:00+00:00", "open_time_epoch": 1,
            "owner_symbol": "XAUUSD", "owner_magic": int(trend["magic"]),
            "owner_comment": trend["comment_prefix"], "shadow": False,
        }]
        bind_owned_basket_identity(trend, trend_state)
        trend_state["time_close_retry_after_utc"] = future_retry
        trend_info = SimpleNamespace(
            bid=109.97, ask=110.0,
            quote_time_msc=int(quote_time.timestamp() * 1000),
        )
        self.assertFalse(
            trend_runner._process_trend_recovery_exits(trend_info, quote_time)
        )
        self.assertEqual(trend_executor.close_calls, [9623])
        self.assertIsNone(trend_state["time_close_retry_after_utc"])

    def test_common_max_hold_uses_broker_quote_not_host_poll_time(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["basket"][0]["shadow"] = True
        state["basket"][0]["entry_time_utc"] = "2026-08-25T13:00:00+00:00"
        strategy["failure_to_progress_bars"] = 0
        broker_quote = pd.Timestamp("2026-08-25T13:30:00Z")
        host_poll = pd.Timestamp("2026-08-25T14:30:00Z")
        info = SimpleNamespace(
            bid=100.0,
            ask=100.03,
            quote_time_msc=int(broker_quote.timestamp() * 1000),
        )
        row = pd.Series(
            {"Open": 100.0, "Close": 100.0, "AskOpen": 100.03},
            name=broker_quote,
        )

        self.assertFalse(
            runner._monitor_open_basket(strategy, info, row, host_poll)
        )
        self.assertEqual(executor.close_calls, [])

    def test_nonfinite_persisted_basket_peak_cannot_disable_failure_to_progress(self):
        runner, strategy, state = make_runner(live=False)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["basket"][0]["shadow"] = True
        state["basket"][0]["entry_time_utc"] = "2026-08-25T13:00:00+00:00"
        state["basket_peak_pnl_usd"] = float("nan")
        events = []
        runner._trade_row = lambda event, _strat, **fields: events.append((event, fields))
        at_utc = pd.Timestamp("2026-08-25T13:10:00Z")
        info = SimpleNamespace(bid=100.0, ask=100.03)
        row = pd.Series(
            {"Open": 100.0, "Close": 100.0, "AskOpen": 100.03},
            name=at_utc,
        )

        self.assertTrue(runner._monitor_open_basket(strategy, info, row, at_utc))
        self.assertFalse(state["basket"])
        self.assertTrue(
            any(
                event == "basket_close" and fields.get("reason") == "failure_to_progress"
                for event, fields in events
            )
        )

    def test_coercible_non_numeric_basket_peak_cannot_disable_failure_to_progress(self):
        for label, value in (("numeric_string", "999.0"), ("boolean", True)):
            with self.subTest(label=label):
                runner, strategy, state = make_runner(live=False)
                executor = CountingExecutor()
                runner.executor = executor
                arm_owned_basket(strategy, state, executor)
                state["basket"][0]["shadow"] = True
                state["basket"][0]["entry_time_utc"] = "2026-08-25T13:00:00+00:00"
                state["basket_peak_pnl_usd"] = value
                events = []
                runner._trade_row = lambda event, _strat, **fields: events.append((event, fields))
                at_utc = pd.Timestamp("2026-08-25T13:10:00Z")
                info = SimpleNamespace(bid=100.0, ask=100.03)
                row = pd.Series(
                    {"Open": 100.0, "Close": 100.0, "AskOpen": 100.03},
                    name=at_utc,
                )

                self.assertTrue(runner._monitor_open_basket(strategy, info, row, at_utc))
                self.assertFalse(state["basket"])
                self.assertTrue(
                    any(
                        event == "basket_close" and fields.get("reason") == "failure_to_progress"
                        for event, fields in events
                    )
                )
                self.assertTrue(
                    any(
                        event == "position_lifecycle_recovered"
                        and fields.get("reason") == "basket_peak_pnl_invalid_reset"
                        for event, fields in events
                    )
                )

    def test_malformed_frozen_atr_falls_back_without_disabling_pnl_exits(self):
        runner, strategy, state = make_runner(live=False)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["basket"][0]["shadow"] = True
        state["basket"][0]["entry_time_utc"] = "2026-08-25T13:00:00+00:00"
        state["frozen_basket_atr30"] = "not-a-number"
        events = []
        runner._trade_row = lambda event, _strat, **fields: events.append((event, fields))
        at_utc = pd.Timestamp("2026-08-25T13:10:00Z")
        info = SimpleNamespace(bid=100.0, ask=100.03)
        row = pd.Series(
            {"Open": 100.0, "Close": 100.0, "AskOpen": 100.03},
            name=at_utc,
        )

        self.assertTrue(runner._monitor_open_basket(strategy, info, row, at_utc))
        self.assertFalse(state["basket"])
        self.assertTrue(
            any(
                event == "basket_close" and fields.get("reason") == "failure_to_progress"
                for event, fields in events
            )
        )

    def test_trend_max_hold_uses_broker_quote_not_host_poll_time(self):
        runner, _strategy, _state = make_runner(live=True)
        strategy = runner.params["trend_recovery_strategies"][0]
        state = runner._st(strategy)
        executor = CountingExecutor()
        runner.executor = executor
        executor.positions = [SimpleNamespace(
            ticket=9622, identifier=9622, symbol="XAUUSD",
            magic=int(strategy["magic"]), comment=strategy["comment_prefix"],
            type=live_s23_bot.ORDER_TYPE_SELL, volume=0.01,
            open_time=int(pd.Timestamp("2026-08-25T13:00:00Z").timestamp()),
        )]
        state["basket"] = [{
            "ticket": 9622, "position_identifier": 9622, "side": "SHORT",
            "lot": 0.01, "entry_price": 100.0,
            "entry_time_utc": "2026-08-25T13:00:00+00:00",
            "open_time_epoch": int(pd.Timestamp("2026-08-25T13:00:00Z").timestamp()),
            "owner_symbol": "XAUUSD", "owner_magic": int(strategy["magic"]),
            "owner_comment": strategy["comment_prefix"], "shadow": False,
        }]
        bind_owned_basket_identity(strategy, state)
        runner._trend_recovery_state()["frozen_atr30"] = 3.0
        broker_quote = pd.Timestamp("2026-08-25T13:30:00Z")
        host_poll = pd.Timestamp("2026-08-25T14:30:00Z")
        info = SimpleNamespace(
            bid=100.0,
            ask=100.03,
            quote_time_msc=int(broker_quote.timestamp() * 1000),
        )

        self.assertTrue(runner._process_trend_recovery_exits(info, host_poll))
        self.assertEqual(executor.close_calls, [])

    def test_pending_close_does_not_clear_unrelated_ownership_block(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state.update(
            {
                "pending_close_reason": "basket_stop",
                "pending_close_signal_bar": "2026-08-25T13:10:00+00:00",
                "sync_block_new_entries": True,
                "sync_block_reason": "state_position_ownership_mismatch",
                "sync_block_recoverable": False,
            }
        )

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(state["pending_close_reason"], "basket_stop")
        self.assertEqual(state["sync_block_reason"], "state_position_ownership_mismatch")
        self.assertTrue(state["sync_block_new_entries"])

    def test_trend_ticket_malformed_close_stays_blocked_after_exact_owned_sync(self):
        runner, _strategy, _state = make_runner(live=True)
        strategy = runner.params["trend_recovery_strategies"][0]
        state = runner._st(strategy)
        executor = CountingExecutor()
        runner.executor = executor
        position = SimpleNamespace(
            ticket=9501, identifier=9501, symbol="XAUUSD",
            magic=int(strategy["magic"]), comment=strategy["comment_prefix"],
            type=live_s23_bot.ORDER_TYPE_SELL, volume=0.01, open_time=1,
        )
        executor.positions = [position]
        state["basket"] = [{
            "ticket": 9501, "position_identifier": 9501, "side": "SHORT",
            "lot": 0.01, "entry_price": 100.0,
            "entry_time_utc": "2026-08-25T13:00:00+00:00", "open_time_epoch": 1,
            "owner_symbol": "XAUUSD", "owner_magic": int(strategy["magic"]),
            "owner_comment": strategy["comment_prefix"], "shadow": False,
        }]
        bind_owned_basket_identity(strategy, state)
        close_calls = []
        executor.close_position = lambda ticket, *_args, **_kwargs: (
            close_calls.append(int(ticket))
            or live_executor.CloseResult(
                False, "MALFORMED_OK", raw_response="OK|bad",
            )
        )
        row = pd.Series(
            {"Open": 99.0, "Close": 99.0, "AskOpen": 99.03},
            name=pd.Timestamp("2026-08-25T13:10:00Z"),
        )

        self.assertEqual(
            runner._close_trend_recovery_ticket(
                strategy, state["basket"][0], "trend_ticket_target", row, 1.0,
            ),
            "failed",
        )
        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(
            state["sync_block_reason"], "close_submission_result_unresolved",
        )
        self.assertEqual(
            runner._close_trend_recovery_ticket(
                strategy, state["basket"][0], "trend_ticket_target", row, 1.0,
            ),
            "failed",
        )
        self.assertEqual(close_calls, [9501])

    def test_trend_ticket_durable_pending_close_rearms_after_restart_sync(self):
        runner, _strategy, _state = make_runner(live=True)
        strategy = runner.params["trend_recovery_strategies"][0]
        state = runner._st(strategy)
        executor = CountingExecutor()
        runner.executor = executor
        executor.positions = [SimpleNamespace(
            ticket=9502, identifier=9502, symbol="XAUUSD",
            magic=int(strategy["magic"]), comment=strategy["comment_prefix"],
            type=live_s23_bot.ORDER_TYPE_SELL, volume=0.01, open_time=1,
        )]
        state["basket"] = [{
            "ticket": 9502, "position_identifier": 9502, "side": "SHORT",
            "lot": 0.01, "entry_price": 100.0,
            "entry_time_utc": "2026-08-25T13:00:00+00:00", "open_time_epoch": 1,
            "owner_symbol": "XAUUSD", "owner_magic": int(strategy["magic"]),
            "owner_comment": strategy["comment_prefix"], "shadow": False,
            "pending_close_reason": "trend_ticket_max_hold",
            "pending_close_signal_bar": "2026-08-25T13:10:00+00:00",
        }]
        bind_owned_basket_identity(strategy, state)

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertIsNone(state["basket"][0].get("pending_close_reason"))
        self.assertIsNone(state["basket"][0].get("pending_close_signal_bar"))

    def test_trend_ticket_pending_close_is_not_resent_while_orders_are_unavailable(self):
        runner, _strategy, _state = make_runner(live=True)
        strategy = runner.params["trend_recovery_strategies"][0]
        state = runner._st(strategy)
        executor = CountingExecutor(orders_available=False)
        runner.executor = executor
        executor.positions = [SimpleNamespace(
            ticket=9504, identifier=9504, symbol="XAUUSD",
            magic=int(strategy["magic"]), comment=strategy["comment_prefix"],
            type=live_s23_bot.ORDER_TYPE_SELL, volume=0.01, open_time=1,
        )]
        state["basket"] = [{
            "ticket": 9504, "position_identifier": 9504, "side": "SHORT",
            "lot": 0.01, "entry_price": 100.0,
            "entry_time_utc": "2026-08-25T13:00:00+00:00", "open_time_epoch": 1,
            "owner_symbol": "XAUUSD", "owner_magic": int(strategy["magic"]),
            "owner_comment": strategy["comment_prefix"], "shadow": False,
            "pending_close_reason": "trend_ticket_max_hold",
            "pending_close_signal_bar": "2026-08-25T13:10:00+00:00",
        }]
        info = SimpleNamespace(bid=99.0, ask=99.03)

        self.assertFalse(
            runner._process_trend_recovery_exits(
                info, pd.Timestamp("2026-08-25T14:30:00Z"),
            )
        )
        self.assertEqual(executor.close_calls, [])
        self.assertEqual(
            state["basket"][0]["pending_close_reason"], "trend_ticket_max_hold",
        )

    def test_missing_position_before_close_is_reconciled_as_close_not_foreign_ticket(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        executor.get_position = lambda _ticket: False
        row = pd.Series(
            {"Open": 99.0, "Close": 99.0, "AskOpen": 99.03},
            name=pd.Timestamp("2026-08-25T13:10:00Z"),
        )

        self.assertEqual(runner._close_basket(strategy, "basket_stop", row, -1.0), "failed")
        self.assertEqual(state["sync_block_reason"], "position_missing_before_close")
        self.assertTrue(state["sync_block_recoverable"])
        self.assertEqual(executor.close_calls, [])
    def test_shadow_observer_configuration_is_diagnostic_only_and_fixed(self):
        params = load_params()
        config = params["shadow_opportunity_observer"]
        self.assertTrue(config["enabled"])
        self.assertEqual(config["horizons_minutes"], [1, 5, 15, 30, 60])
        self.assertEqual(config["state_file"], "s23_shadow_observer_state.json")
        self.assertNotIn("shadow_opportunity_observer", params["safety"])
        tagger = params["shadow_state_tagger"]
        self.assertTrue(tagger["enabled"])
        self.assertEqual(tagger["csv"], "s23_shadow_state_tags.csv")
        self.assertNotIn("shadow_state_tagger", params["safety"])

    def test_missing_observer_module_does_not_prevent_runner_construction(self):
        params = json.loads(json.dumps(load_params()))
        params["live_trading_enabled"] = False
        params["shadow_forward_enabled"] = True
        with patch.object(live_s23_bot, "ShadowOpportunityObserver", None), patch.object(
            live_s23_bot.os.path, "exists", return_value=False
        ):
            runner = S23HorizontalInventoryRunner(params)
        self.assertIsNone(runner.shadow_observer)

    def test_missing_state_tagger_module_does_not_prevent_runner_construction(self):
        params = json.loads(json.dumps(load_params()))
        params["live_trading_enabled"] = False
        params["shadow_forward_enabled"] = True
        with patch.object(live_s23_bot, "ShadowStateTagger", None), patch.object(
            live_s23_bot.os.path, "exists", return_value=False
        ):
            runner = S23HorizontalInventoryRunner(params)
        self.assertIsNone(runner.shadow_state_tagger)

    def test_log_policy_is_bounded_and_diagnostic_repeats_are_coalesced(self):
        runner, strategy, _state = make_runner()
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(runner, S23HorizontalInventoryRunner)
        base = pd.Timestamp("2026-08-25 13:00:00", tz="UTC")
        times = [base, base + pd.Timedelta(seconds=6), base + pd.Timedelta(seconds=12), base + pd.Timedelta(seconds=301)]
        rows = []
        with patch.object(live_s23_bot, "utc_now", side_effect=[stamp.to_pydatetime() for stamp in times]), patch.object(
            live_s23_bot,
            "append_csv",
            side_effect=lambda _path, row, _fields: rows.append(dict(row)),
        ), patch.object(live_s23_bot, "append_signal_evaluation_csv"):
            for _ in times:
                runner._trade_row("entry_skip", strategy, reason="orders_unavailable", note="sync_block")

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["event"], "entry_skip")
        self.assertEqual(rows[0]["repeat_count"], 1)
        self.assertEqual(rows[1]["event"], "diagnostic_repeat_summary")
        self.assertEqual(rows[1]["repeat_count"], 3)
        self.assertEqual(rows[1]["repeat_window_seconds"], 301.0)
        self.assertEqual(runner.params["status_log_interval_seconds"], 300)
        self.assertEqual(runner.params["bot_log_max_bytes"], 10 * 1024 * 1024)
        self.assertEqual(runner.params["bot_log_backup_count"], 5)

    def test_diagnostic_reason_transition_flushes_suppressed_count(self):
        runner, strategy, _state = make_runner()
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(runner, S23HorizontalInventoryRunner)
        base = pd.Timestamp("2026-08-25 13:00:00", tz="UTC")
        rows = []
        with patch.object(
            live_s23_bot,
            "utc_now",
            side_effect=[
                base.to_pydatetime(),
                (base + pd.Timedelta(seconds=6)).to_pydatetime(),
                (base + pd.Timedelta(seconds=12)).to_pydatetime(),
            ],
        ), patch.object(live_s23_bot, "append_csv", side_effect=lambda _path, row, _fields: rows.append(dict(row))), patch.object(
            live_s23_bot, "append_signal_evaluation_csv",
        ):
            runner._trade_row("entry_skip", strategy, reason="symbol_info_failed", note="sync_block")
            runner._trade_row("entry_skip", strategy, reason="symbol_info_failed", note="sync_block")
            runner._trade_row("entry_skip", strategy, reason="orders_unavailable", note="sync_block")

        self.assertEqual([row["event"] for row in rows], ["entry_skip", "diagnostic_repeat_summary", "entry_skip"])
        self.assertEqual(rows[1]["repeat_count"], 1)
        self.assertEqual(rows[2]["reason"], "orders_unavailable")
    def test_signal_uses_confirmed_bar_spread_not_current_quote_spread(self):
        runner, strategy, _state = make_runner(live=False)
        row = pd.Series(
            {
                "spread_points": 301.0,
                "atr30": 2.5,
                "vol_ratio": 1.2,
                "ret5": 0.0,
                "ret10": 2.0,
                "Close": 100.0,
                "roll_high30": 99.0,
                "roll_low30": 101.0,
            },
            name=pd.Timestamp("2026-08-25 13:00:00", tz="UTC"),
        )
        self.assertIsNone(runner._signal(row, strategy))
        row["spread_points"] = 30.0
        self.assertEqual(runner._signal(row, strategy), "LONG")

    def test_reverse_d60_uses_current_bid_and_exact_completed_m1_lookback(self):
        runner, _strategy, _state = make_runner(live=False)
        bars = pd.DataFrame(
            {"Close": [4640.0] + [4618.0] * 29 + [4615.0]},
            index=pd.date_range("2026-08-25 12:30:00", periods=31, freq="1min", tz="UTC"),
        )
        effective, policy = runner._apply_entry_policy(
            "SHORT",
            bars,
            SimpleNamespace(bid=4610.0, ask=4610.2),
        )
        self.assertEqual(effective, "LONG")
        self.assertEqual(policy["action"], "reverse_long")
        self.assertEqual(policy["prior30_close"], 4640.0)
        self.assertEqual(policy["signal_bid"], 4610.0)
        self.assertLessEqual(policy["decline_ratio"], -0.006)

    def test_reverse_d60_preserves_short_below_threshold_and_long_always(self):
        runner, _strategy, _state = make_runner(live=False)
        bars = pd.DataFrame(
            {"Close": [4640.0] + [4615.0] * 30},
            index=pd.date_range("2026-08-25 12:30:00", periods=31, freq="1min", tz="UTC"),
        )
        effective, policy = runner._apply_entry_policy("SHORT", bars, SimpleNamespace(bid=4615.0, ask=4615.2))
        self.assertEqual(effective, "SHORT")
        self.assertEqual(policy["reason"], "late_short_drop_threshold_not_met")
        effective, policy = runner._apply_entry_policy("LONG", bars.iloc[:1], SimpleNamespace(bid=4610.0, ask=4610.2))
        self.assertEqual(effective, "LONG")
        self.assertEqual(policy["reason"], "not_short")

    def test_reverse_d60_fails_closed_without_completed_m1_history(self):
        runner, _strategy, _state = make_runner(live=False)
        bars = pd.DataFrame(
            {"Close": [4640.0] * 30},
            index=pd.date_range("2026-08-25 12:31:00", periods=30, freq="1min", tz="UTC"),
        )
        effective, policy = runner._apply_entry_policy("SHORT", bars, SimpleNamespace(bid=4610.0, ask=4610.2))
        self.assertIsNone(effective)
        self.assertEqual(policy["reason"], "insufficient_completed_m1_history")

    def test_entry_policy_state_migration_clears_only_unsubmitted_pending_entries(self):
        params = json.loads(json.dumps(load_params()))
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            seed = S23HorizontalInventoryRunner(params)
        legacy = seed._default_state()
        legacy["routing"].pop("entry_policy_id", None)
        legacy["routing"].pop("entry_policy_params_hash", None)
        first = legacy["strategies"][params["strategies"][0]["id"]]
        first["pending_entry_side"] = "SHORT"
        first["pending_entry_target"] = 4600.0
        first["pending_entry_opportunity_id"] = "old-policy-pending"
        first["basket"] = [{"ticket": 9401, "side": "LONG"}]
        first["basket_sequence"] = 1
        first["current_basket_id"] = "L1-B000001"
        first["pending_open_opportunity_id"] = "unresolved-open-must-remain"
        with tempfile.TemporaryDirectory() as folder:
            state_path = os.path.join(folder, "state.json")
            with open(state_path, "w", encoding="utf-8") as handle:
                json.dump(legacy, handle)
            with patch.object(live_s23_bot, "STATE_FILE", state_path):
                migrated = S23HorizontalInventoryRunner(params)
        migrated_first = migrated.state["strategies"][params["strategies"][0]["id"]]
        self.assertTrue(migrated._entry_policy_state_migrated)
        self.assertIsNone(migrated_first["pending_entry_side"])
        self.assertIsNone(migrated_first["pending_entry_target"])
        self.assertEqual(migrated_first["basket"], [{"ticket": 9401, "side": "LONG"}])
        self.assertEqual(migrated_first["pending_open_opportunity_id"], "unresolved-open-must-remain")
        self.assertEqual(migrated.state["routing"]["entry_policy_id"], "reverse_d60")

    def test_inventory_range_state_migration_preserves_live_inventory_and_za_pending(self):
        params = json.loads(json.dumps(load_params()))
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            seed = S23HorizontalInventoryRunner(params)
        legacy = seed._default_state()
        legacy["routing"].pop("inventory_range_fade_policy_id", None)
        legacy["routing"].pop("inventory_range_fade_params_hash", None)
        legacy["routing"].pop("inventory_range_fade", None)
        first = legacy["strategies"][params["strategies"][0]["id"]]
        first["basket"] = [{"ticket": 9401, "side": "LONG", "entry_price": 100.0}]
        first["basket_sequence"] = 1
        first["current_basket_id"] = "L1-B000001"
        first["pending_entry_side"] = "SHORT"
        first["pending_entry_target"] = 101.0
        with tempfile.TemporaryDirectory() as folder:
            state_path = os.path.join(folder, "state.json")
            with open(state_path, "w", encoding="utf-8") as handle:
                json.dump(legacy, handle)
            with patch.object(live_s23_bot, "STATE_FILE", state_path):
                migrated = S23HorizontalInventoryRunner(params)
        migrated_first = migrated.state["strategies"][params["strategies"][0]["id"]]
        self.assertTrue(migrated._inventory_range_fade_state_migrated)
        self.assertEqual(migrated_first["basket"], first["basket"])
        self.assertEqual(migrated_first["pending_entry_side"], "SHORT")
        self.assertFalse(migrated.state["routing"]["inventory_range_fade"]["active"])

    def test_balanced_range_false_break_creates_one_opposite_pending_opportunity(self):
        runner, strategy, state = make_runner(live=False)
        second = runner.params["strategies"][1]
        state["basket"] = [{"side": "LONG", "entry_price": 100.0}]
        runner._st(second)["basket"] = [{"side": "SHORT", "entry_price": 101.0}]

        def advance(minute: int, close: float) -> None:
            at = pd.Timestamp(f"2026-08-25 13:{minute:02d}:00", tz="UTC")
            runner._advance_inventory_range_fade(pd.Series({"Close": close}, name=at))

        advance(0, 100.5)
        range_state = runner.state["routing"]["inventory_range_fade"]
        self.assertTrue(range_state["active"])
        self.assertEqual((range_state["low"], range_state["high"]), (100.0, 101.0))
        advance(1, 101.1)
        self.assertEqual(range_state["break_side"], "LONG")
        advance(2, 100.9)
        self.assertEqual(range_state["return_confirm_count"], 1)
        advance(3, 100.8)
        self.assertEqual(range_state["pending_side"], "SHORT")

        pending = runner._take_inventory_range_fade_opportunity(
            raw_side=None,
            signal_bar=pd.Timestamp("2026-08-25 13:03:00", tz="UTC"),
            poll_time=pd.Timestamp("2026-08-25 13:04:02", tz="UTC"),
            symbol="XAUUSD",
        )
        self.assertIsNotNone(pending)
        self.assertEqual(pending["side"], "SHORT")
        self.assertEqual(pending["source"], "inventory_range_false_break_fade")
        self.assertIsNone(range_state["pending_side"])

    def test_raw_za_has_priority_over_pending_inventory_range_fade(self):
        runner, _strategy, _state = make_runner(live=False)
        range_state = runner.state["routing"]["inventory_range_fade"]
        range_state.update({"pending_side": "LONG", "pending_origin_bar": "2026-08-25T13:03:00+00:00", "pending_break_side": "SHORT"})
        opportunity = runner._take_inventory_range_fade_opportunity(
            raw_side="SHORT",
            signal_bar=pd.Timestamp("2026-08-25 13:04:00", tz="UTC"),
            poll_time=pd.Timestamp("2026-08-25 13:05:02", tz="UTC"),
            symbol="XAUUSD",
        )
        self.assertIsNone(opportunity)
        self.assertEqual(range_state["pending_side"], "LONG")

    def test_inventory_range_fade_bypasses_only_new_basket_pullback_requirement(self):
        runner, strategy, state = make_runner(live=False)
        opportunity, row, poll_time, info = sample_opportunity(side="SHORT")
        opportunity["source"] = "inventory_range_false_break_fade"
        row["atr30"] = 1.5
        row["bb20_mid"] = 100.0
        row["bb20_std"] = 1.0
        row["Close"] = 100.0

        consumed, reason = runner._consume_opportunity(strategy, opportunity, row, info, poll_time)

        self.assertTrue(consumed)
        self.assertEqual(reason, "entry_attempted")
        self.assertEqual(len(state["basket"]), 1)
        self.assertIsNone(state["pending_entry_side"])

    def test_inventory_range_break_is_invalidated_when_book_loses_balance(self):
        runner, _strategy, state = make_runner(live=False)
        second = runner.params["strategies"][1]
        state["basket"] = [{"side": "LONG", "entry_price": 100.0}]
        runner._st(second)["basket"] = [{"side": "SHORT", "entry_price": 101.0}]
        runner._advance_inventory_range_fade(
            pd.Series({"Close": 100.5}, name=pd.Timestamp("2026-08-25 13:00:00", tz="UTC"))
        )
        runner._advance_inventory_range_fade(
            pd.Series({"Close": 101.1}, name=pd.Timestamp("2026-08-25 13:01:00", tz="UTC"))
        )
        runner._st(second)["basket"] = []
        runner._advance_inventory_range_fade(
            pd.Series({"Close": 100.9}, name=pd.Timestamp("2026-08-25 13:02:00", tz="UTC"))
        )
        range_state = runner.state["routing"]["inventory_range_fade"]
        self.assertEqual(range_state["break_phase"], 0)
        self.assertIsNone(range_state["pending_side"])

    def test_lower_false_break_creates_long_opportunity(self):
        runner, _strategy, state = make_runner(live=False)
        second = runner.params["strategies"][1]
        state["basket"] = [{"side": "LONG", "entry_price": 100.0}]
        runner._st(second)["basket"] = [{"side": "SHORT", "entry_price": 101.0}]
        closes = (100.5, 99.9, 100.1, 100.2)
        for minute, close in enumerate(closes):
            runner._advance_inventory_range_fade(
                pd.Series({"Close": close}, name=pd.Timestamp(f"2026-08-25 13:{minute:02d}:00", tz="UTC"))
            )
        range_state = runner.state["routing"]["inventory_range_fade"]
        self.assertEqual(range_state["pending_side"], "LONG")
        self.assertEqual(range_state["pending_break_side"], "SHORT")


class Bot23MorningSessionRegressionTests(unittest.TestCase):
    def test_state_shape_validation_covers_every_overlay_lane(self):
        params = json.loads(json.dumps(load_params()))
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            seed = S23HorizontalInventoryRunner(params)
        session_id = params["session_vwap_strategies"][0]["id"]
        corruptions = {
            "overlay_lane": lambda state: state["strategies"].__setitem__(session_id, []),
            "routing_root": lambda state: state.__setitem__("routing", []),
            "trend_episode": lambda state: state["routing"].__setitem__("trend_recovery", []),
            "range_overlay": lambda state: state["routing"].__setitem__("inventory_range_fade", []),
        }
        for label, corrupt in corruptions.items():
            with self.subTest(label=label):
                malformed = seed._default_state()
                corrupt(malformed)
                with tempfile.NamedTemporaryFile(
                    "w", suffix=".json", delete=False, encoding="utf-8",
                ) as handle:
                    json.dump(malformed, handle)
                    state_path = handle.name
                try:
                    with patch.object(live_s23_bot, "STATE_FILE", state_path):
                        runner = S23HorizontalInventoryRunner(params)
                finally:
                    os.unlink(state_path)

                for strat in runner._all_strategies():
                    state = runner._st(strat)
                    self.assertTrue(state["sync_block_new_entries"])
                    self.assertEqual(state["sync_block_reason"], "state_identity_mismatch")

    def test_live_session_entry_does_not_substitute_host_time_for_missing_broker_quote_time(self):
        runner, _za, _state = make_runner(live=True)
        runner.params["session_vwap_enabled"] = True
        signal_bar = pd.Timestamp("2026-07-01T09:29:00Z")
        bars = pd.DataFrame(
            {
                "Open": [2000.0], "High": [2000.2], "Low": [1999.8],
                "Close": [2000.1], "Volume": [10],
            },
            index=pd.DatetimeIndex([signal_bar]),
        )
        runner._session_vwap_snapshot = SimpleNamespace(
            bars=bars, ready=True, fresh=True, reason="ready", failures=0,
            retry_after_seconds=0.0,
        )
        runner._open_entry = lambda *_args, **_kwargs: self.fail(
            "live session entry must not use host time when broker quote time is missing"
        )
        readiness = {
            int(row["lane_id"]): True
            for row in runner.params["session_vwap_strategies"]
        }
        with (
            patch.object(live_s23_bot, "session_vwap_entry_history_issue", return_value=None),
            patch.object(
                live_s23_bot, "latest_session_vwap_signal",
                return_value=("LONG", pd.Series({"Z": 1.5, "Q90": 1.2})),
            ),
            patch.object(live_s23_bot, "stale_signal_decision", return_value=SimpleNamespace(stale=False)),
        ):
            runner._process_session_vwap_entries(
                SimpleNamespace(bid=2000.0, ask=2000.1),
                pd.Timestamp("2026-07-01T09:30:01Z"),
                readiness,
            )

        self.assertIsNone(runner.state["routing"]["session_vwap_last_evaluated_bar"])

    def test_compose_mounts_both_clock_modules_for_bot23(self):
        compose_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "docker-compose.yml"))
        with open(compose_path, "r", encoding="utf-8") as handle:
            compose = handle.read()
        self.assertIn(
            "./bot23/eu_entry_admission_clock.py:/app/bot23/eu_entry_admission_clock.py:ro",
            compose,
        )
        self.assertIn(
            "./bot23/position_lifecycle_clock.py:/app/bot23/position_lifecycle_clock.py:ro",
            compose,
        )
        self.assertIn(
            "./bot23/jst1300_pre_eu30_strategy.py:/app/bot23/jst1300_pre_eu30_strategy.py:ro",
            compose,
        )
        self.assertIn(
            "./bot23/session_vwap_overlay.py:/app/bot23/session_vwap_overlay.py:ro",
            compose,
        )
        self.assertIn(
            "./bot23/live_data_fetcher.py:/app/bot23/live_data_fetcher.py:ro",
            compose,
        )
        self.assertIn(
            "./bot23/BotBridge_s23.mq5:/app/bot23/BotBridge_s23.mq5:ro",
            compose,
        )

    def test_bot23_uses_fixed_local_credentials_without_unused_environment_wiring(self):
        bot23 = Path(__file__).resolve().parent
        config = (bot23 / "live_config.py").read_text(encoding="utf-8")
        startup = (bot23 / "startup.ini").read_text(encoding="utf-8")
        compose = (bot23.parent / "docker-compose.yml").read_text(encoding="utf-8")
        violations = []
        for name in ("BOT23_MT5_LOGIN", "BOT23_MT5_PASSWORD", "BOT23_MT5_SERVER"):
            if name in config or name in compose:
                violations.append(f"unused_environment_wiring:{name}")
        for field, pattern in {
            "login": r"(?m)^MT5_LOGIN\s*=\s*[1-9][0-9]*\s*$",
            "password": r"(?m)^MT5_PASSWORD\s*=\s*[\"'][^\"']+[\"']\s*$",
            "server": r"(?m)^MT5_SERVER\s*=\s*[\"'][^\"']+[\"'](?:\s*#.*)?$",
        }.items():
            if re.search(pattern, config) is None:
                violations.append(f"live_config_fixed_{field}_missing")
        for field, pattern in {
            "login": r"(?m)^Login=[1-9][0-9]*\s*$",
            "password": r"(?m)^Password=[^\r\n]+$",
            "server": r"(?m)^Server=[^\r\n]+$",
        }.items():
            if re.search(pattern, startup) is None:
                violations.append(f"startup_fixed_{field}_missing")
        self.assertEqual([], violations, "fixed credential contract violations")

    def test_disabled_session_vwap_overlay_never_fetches_or_evaluates(self):
        runner, _za, _state = make_runner(live=False)
        runner.params["session_vwap_enabled"] = False
        self.assertFalse(runner.params["session_vwap_enabled"])
        runner._session_vwap_snapshot = SimpleNamespace(reason="stale_test_value")
        runner.session_vwap_history.advance = lambda *_args, **_kwargs: self.fail(
            "disabled session-VWAP must not fetch history"
        )
        info = SimpleNamespace(bid=2000.0, ask=2000.1, quote_time_msc=1)

        runner._refresh_session_vwap_history(info, pd.Timestamp("2026-07-01T09:30:01Z"))
        runner._process_session_vwap_entries(info, pd.Timestamp("2026-07-01T09:30:01Z"), {})

        self.assertIsNone(runner._session_vwap_snapshot)
        self.assertIsNone(runner.state["routing"]["session_vwap_last_evaluated_bar"])

    def test_all_lane_families_apply_daily_loss_at_final_open_guard(self):
        runner, _za, _state = make_runner(live=False)
        at = pd.Timestamp("2026-08-25T13:15:00Z")
        row = pd.Series(
            {"Open": 100.0, "Close": 100.0, "AskOpen": 100.03}, name=at,
        )
        info = SimpleNamespace(bid=100.0, ask=100.03)
        for strat in runner._all_strategies():
            with self.subTest(strategy=strat["id"]):
                state = runner._st(strat)
                state["daily_realized_date_utc"] = at.strftime("%Y-%m-%d")
                state["daily_realized_pnl_usd"] = -float(
                    runner.params["daily_realized_loss_limit_usd"]
                )
                self.assertFalse(
                    runner._open_entry(
                        strat, "LONG", row, info,
                        execution_time=at, apply_portfolio_rearm=False,
                    )
                )
                self.assertFalse(state["basket"])

    def test_live_and_shadow_disabled_cannot_create_local_basket(self):
        runner, strat, state = make_runner(live=False)
        runner.shadow_enabled = False
        at = pd.Timestamp("2026-08-25T13:15:00Z")
        row = pd.Series(
            {"Open": 100.0, "Close": 100.0, "AskOpen": 100.03}, name=at,
        )
        self.assertFalse(
            runner._open_entry(
                strat, "LONG", row, SimpleNamespace(bid=100.0, ask=100.03),
                execution_time=at,
            )
        )
        self.assertFalse(state["basket"])

    def test_shadow_mode_never_clears_live_origin_inventory(self):
        runner, strat, state = make_runner(live=False)
        state["basket"] = [{
            "ticket": 700001, "position_identifier": 800001,
            "side": "LONG", "lot": 0.01, "entry_price": 100.0,
            "entry_time_utc": "2026-08-25T13:00:00+00:00",
            "open_time_epoch": 1, "owner_symbol": "XAUUSD",
            "owner_magic": int(strat["magic"]),
            "owner_comment": strat["comment_prefix"], "shadow": False,
        }]
        bind_owned_basket_identity(strat, state)
        row = pd.Series(
            {"Open": 101.0, "Close": 101.0, "AskOpen": 101.03},
            name=pd.Timestamp("2026-08-25T13:10:00Z"),
        )

        self.assertEqual(runner._close_basket(strat, "test", row, 1.0), "failed")
        self.assertEqual(len(state["basket"]), 1)
        self.assertEqual(state["sync_block_reason"], "live_origin_inventory_requires_live_close")

    def test_disabled_session_lane_still_syncs_and_monitors_owned_basket(self):
        runner, _za, _state = make_runner(live=False)
        strat = runner.params["session_vwap_strategies"][0]
        strat["enabled"] = False
        runner._st(strat)["basket"] = [{"shadow": True}]
        synced = []
        monitored = []
        runner._sync_strategy = lambda candidate: (synced.append(candidate["id"]) or True)
        runner._monitor_session_vwap_position = lambda candidate, *_args: (
            monitored.append(candidate["id"]) or False
        )
        at = pd.Timestamp("2026-07-01T12:31:00Z")
        info = SimpleNamespace(
            bid=100.0, ask=100.03, quote_time_msc=int(at.timestamp() * 1000),
        )

        readiness = runner._process_session_vwap_exits(info, at)

        self.assertIn(strat["id"], synced)
        self.assertIn(strat["id"], monitored)
        self.assertFalse(readiness[int(strat["lane_id"])])

    def test_failed_reconciliation_alert_is_retried_until_delivered(self):
        runner, strat, state = make_runner(live=False)
        runner._suppress_manual_alerts = False
        outcomes = iter((False, True))
        attempts = []
        runner._notify_manual_action = lambda *_args, **kwargs: (
            attempts.append(kwargs) or next(outcomes)
        )

        runner._notify_reconciliation_required(strat, "test_reason", {"ticket": 1})
        self.assertIsNone(state["manual_alert_last_signature"])
        runner._notify_reconciliation_required(strat, "test_reason", {"ticket": 1})

        self.assertEqual(len(attempts), 2)
        self.assertIsNotNone(state["manual_alert_last_signature"])

    def test_poll_exception_containment_blocks_entries_without_clearing_inventory(self):
        runner, strat, state = make_runner(live=False)
        state["basket"] = [{"shadow": True}]
        runner._contain_poll_exception(OSError("isolated_poll_failure"))
        self.assertEqual(state["basket"], [{"shadow": True}])
        for candidate in runner._all_strategies():
            self.assertTrue(runner._st(candidate)["sync_block_new_entries"])
            self.assertEqual(runner._st(candidate)["sync_block_reason"], "poll_exception")

    def test_global_disable_with_owned_inventory_starts_close_only_preflight(self):
        runner, strat, state = make_runner(live=False)
        runner.params["enabled"] = False
        state["basket"] = [{"shadow": True}]
        runner.dm.connect = lambda: True
        runner.executor.get_bridge_capabilities = lambda: {
            "name": live_s23_bot.EXPECTED_BRIDGE_NAME,
            "version": live_s23_bot.EXPECTED_BRIDGE_VERSION,
            "commands": set(live_s23_bot.REQUIRED_SHARED_ACCOUNT_COMMANDS),
        }
        runner._legacy_inventory_error = lambda: None
        with patch.object(live_s23_bot, "validate_csv_schema", return_value=None):
            self.assertTrue(runner.connect_and_preflight())
        row = pd.Series(
            {"Open": 100.0, "Close": 100.0, "AskOpen": 100.03},
            name=pd.Timestamp("2026-08-25T13:15:00Z"),
        )
        state["basket"] = []
        self.assertFalse(
            runner._open_entry(
                strat, "LONG", row, SimpleNamespace(bid=100.0, ask=100.03),
                execution_time=row.name,
            )
        )

    def test_preflight_failure_alerts_when_owned_exit_monitoring_stops(self):
        runner, strat, state = make_runner(live=False)
        state["basket"] = [{"shadow": True}]
        runner.dm.connect = lambda: False
        runner._suppress_manual_alerts = False
        alerts = []
        runner._notify_manual_action = lambda *_args, **kwargs: (
            alerts.append(kwargs) or True
        )
        with patch.object(live_s23_bot, "validate_csv_schema", return_value=None):
            self.assertFalse(runner.connect_and_preflight())
        self.assertEqual(len(alerts), 1)
        self.assertEqual(
            state["manual_alert_last_reason"],
            "preflight_exit_monitoring_stopped",
        )

    def test_pre_eu30_ownership_namespace_is_exact_and_disjoint(self):
        runner, _strategy, _state = make_runner(live=False)
        params = runner.params
        lanes = params["pre_eu30_session_strategies"]
        self.assertEqual(tuple(row["magic"] for row in lanes), EXPECTED_PRE_EU30_MAGICS)
        self.assertEqual([row["lane_id"] for row in lanes], [9, 10, 11])
        self.assertEqual([row["hold_minutes"] for row in lanes], [45, 60, 45])
        previous = set(params["expected_magics"] + params["expected_morning_magics"] + params["expected_midday_magics"])
        self.assertTrue(previous.isdisjoint(params["expected_pre_eu30_magics"]))
        self.assertIsNone(runner._ownership_namespace_error())

    def test_pre_eu30_three_signals_fill_three_independent_lanes(self):
        runner, _strategy, _state = make_runner(live=False)
        strategies = runner.params["pre_eu30_session_strategies"]
        bars = pd.DataFrame(
            {"Open": [100.0], "High": [101.0], "Low": [99.0], "Close": [100.5]},
            index=[pd.Timestamp("2026-08-28 04:04:00", tz="UTC")],
        )
        opened = []
        runner._open_entry = lambda strat, side, *_args, **_kwargs: (opened.append((strat["lane_id"], side)) or True)
        sides = {strat["signal_id"]: "LONG" for strat in strategies}
        readiness = {int(strat["lane_id"]): True for strat in strategies}
        info = SimpleNamespace(bid=100.4, ask=100.5)
        with (
            patch.object(live_s23_bot, "pre_eu30_signal_sides", return_value=sides),
            patch.object(live_s23_bot, "stale_signal_decision", return_value=SimpleNamespace(stale=False)),
        ):
            runner._process_pre_eu30_entries(
                bars, bars.iloc[-1], info, pd.Timestamp("2026-08-28 04:05:00", tz="UTC"), readiness
            )
        self.assertEqual(opened, [(9, "LONG"), (10, "LONG"), (11, "LONG")])

    def test_pre_eu30_entries_use_existing_trade_ledger_schema(self):
        runner, _strategy, _state = make_runner(live=False)
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(runner)
        strategies = runner.params["pre_eu30_session_strategies"]
        bars = pd.DataFrame(
            {"Open": [100.0], "High": [101.0], "Low": [99.0], "Close": [100.5]},
            index=[pd.Timestamp("2026-08-28 04:04:00", tz="UTC")],
        )
        sides = {strat["signal_id"]: "LONG" for strat in strategies}
        readiness = {int(strat["lane_id"]): True for strat in strategies}
        info = SimpleNamespace(bid=100.4, ask=100.5)
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as handle:
            path = handle.name
        try:
            with (
                patch.object(live_s23_bot, "TRADE_LOG_FILE", path),
                patch.object(live_s23_bot, "_CSV_SCHEMAS_VALIDATED", set()),
                patch.object(live_s23_bot, "pre_eu30_signal_sides", return_value=sides),
                patch.object(live_s23_bot, "stale_signal_decision", return_value=SimpleNamespace(stale=False)),
            ):
                runner._process_pre_eu30_entries(
                    bars, bars.iloc[-1], info,
                    pd.Timestamp("2026-08-28 04:05:00", tz="UTC"), readiness,
                )
            with open(path, newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
                self.assertEqual(reader.fieldnames, live_s23_bot.TRADE_FIELDS)
            entries = [row for row in rows if row["event"] == "entry"]
            self.assertEqual([int(row["lane_id"]) for row in entries], [9, 10, 11])
            self.assertEqual([int(row["magic"]) for row in entries], list(EXPECTED_PRE_EU30_MAGICS))
            self.assertTrue(all(row["opportunity_id"] for row in entries))
            self.assertEqual([row["strategy_id"] for row in entries], [row["id"] for row in strategies])
        finally:
            os.unlink(path)

    def test_trade_ledger_revalidates_header_after_same_path_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s23_trades.csv")
            row = {"event": "entry", "lane_id": 9}
            live_s23_bot.append_csv(path, row, live_s23_bot.TRADE_FIELDS)
            Path(path).write_text("legacy,header\n1,2\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "CSV schema mismatch"):
                live_s23_bot.append_csv(path, row, live_s23_bot.TRADE_FIELDS)

    def test_trade_ledger_append_flushes_to_stable_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s23_trades.csv")
            with patch.object(live_s23_bot.os, "fsync") as fsync:
                live_s23_bot.append_csv(
                    path, {"event": "entry", "lane_id": 9},
                    live_s23_bot.TRADE_FIELDS,
                )
            fsync.assert_called()

    def test_trade_ledger_rejects_unterminated_tail_before_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s23_trades.csv")
            payload = (
                ",".join(live_s23_bot.TRADE_FIELDS)
                + "\n"
                + ",".join("" for _ in live_s23_bot.TRADE_FIELDS)
            )
            Path(path).write_text(payload, encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "unterminated CSV tail"):
                live_s23_bot.append_csv(
                    path, {"event": "entry", "lane_id": 9},
                    live_s23_bot.TRADE_FIELDS,
                )

    def test_trade_ledger_preflight_rejects_malformed_row_width(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s23_trades.csv")
            Path(path).write_text(
                ",".join(live_s23_bot.TRADE_FIELDS) + "\nonly,two\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "CSV row width mismatch"):
                live_s23_bot.validate_csv_schema(
                    path, live_s23_bot.TRADE_FIELDS,
                )

    def test_unterminated_matching_close_row_is_not_deduplication_proof(self):
        runner, strategy, _state = make_runner(live=False)
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s23_trades.csv")
            row = {
                "timestamp_utc": "2026-08-24T23:59:58+00:00",
                "event": "position_close_confirmed",
                "strategy_id": strategy["id"],
                "lane_id": strategy["lane_id"],
                "magic": strategy["magic"],
                "symbol": "XAUUSD",
                "mt5_symbol": "XAUUSD",
                "basket_id": "L1-B000001",
                "ticket": 9401,
                "position_identifier": 9401,
                "deal_id": 77031,
            }
            live_s23_bot.append_csv(path, row, live_s23_bot.TRADE_FIELDS)
            Path(path).write_bytes(Path(path).read_bytes().rstrip(b"\r\n"))
            with patch.object(live_s23_bot, "TRADE_LOG_FILE", path):
                with self.assertRaisesRegex(RuntimeError, "unterminated CSV tail"):
                    runner._trade_row(
                        "position_close_confirmed", strategy,
                        basket_id="L1-B000001", ticket=9401,
                        position_identifier=9401, deal_id=77031,
                    )

    def test_confirmed_close_deal_identity_conflict_fails_closed(self):
        runner, strategy, _state = make_runner(live=False)
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s23_trades.csv")
            with patch.object(live_s23_bot, "TRADE_LOG_FILE", path):
                runner._trade_row(
                    "position_close_confirmed", strategy,
                    basket_id="L1-B000001", ticket=9401,
                    position_identifier=9401, deal_id=77032,
                )
                with self.assertRaisesRegex(
                    RuntimeError, "confirmed close deal identity conflict",
                ):
                    runner._trade_row(
                        "position_close_confirmed", strategy,
                        basket_id="L1-B000001", ticket=9402,
                        position_identifier=9402, deal_id=77032,
                    )

    def test_preflight_rejects_duplicate_confirmed_close_deal_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s23_trades.csv")
            row = {
                "event": "position_close_confirmed",
                "lane_id": 1,
                "position_identifier": 9401,
                "deal_id": 77033,
            }
            live_s23_bot.append_csv(path, row, live_s23_bot.TRADE_FIELDS)
            live_s23_bot.append_csv(path, row, live_s23_bot.TRADE_FIELDS)

            with self.assertRaisesRegex(
                RuntimeError, "duplicate confirmed close deal",
            ):
                live_s23_bot.validate_csv_schema(
                    path, live_s23_bot.TRADE_FIELDS,
                )

    def test_pre_eu30_entry_stops_at_summer_boundary_but_exit_clock_remains_independent(self):
        runner, _strategy, _state = make_runner(live=False)
        bars = pd.DataFrame(
            {"Open": [100.0], "High": [101.0], "Low": [99.0], "Close": [100.5]},
            index=[pd.Timestamp("2026-08-28 06:29:00", tz="UTC")],
        )
        runner._open_entry = lambda *_args, **_kwargs: self.fail("entry must not be submitted at the EU boundary")
        readiness = {int(strat["lane_id"]): True for strat in runner.params["pre_eu30_session_strategies"]}
        runner._process_pre_eu30_entries(
            bars, bars.iloc[-1], SimpleNamespace(bid=100.4, ask=100.5),
            pd.Timestamp("2026-08-28 06:30:00", tz="UTC"), readiness,
        )
        self.assertEqual(
            live_s23_bot.fixed_hold_due_at([pd.Timestamp("2026-08-28 06:25:00", tz="UTC")], 45),
            pd.Timestamp("2026-08-28 07:10:00", tz="UTC"),
        )

    def test_pre_eu30_state_migration_preserves_existing_inventory(self):
        seed, za, _state = make_runner(live=False)
        state = seed._default_state()
        state["strategies"][za["id"]]["basket"] = [{"ticket": 12345}]
        state["strategies"][za["id"]]["basket_sequence"] = 1
        state["strategies"][za["id"]]["current_basket_id"] = "L1-B000001"
        for strat in seed.params["pre_eu30_session_strategies"]:
            state["strategies"].pop(strat["id"])
        state["routing"].pop("pre_eu30_policy_id")
        state["routing"].pop("pre_eu30_policy_params_hash")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(state, handle)
            state_path = handle.name
        try:
            with patch.object(live_s23_bot, "STATE_FILE", state_path):
                migrated = S23HorizontalInventoryRunner(seed.params)
            self.assertEqual(migrated._st(za)["basket"], [{"ticket": 12345}])
            self.assertTrue(migrated._pre_eu30_session_state_migrated)
            for strat in migrated.params["pre_eu30_session_strategies"]:
                self.assertEqual(migrated._st(strat)["basket"], [])
        finally:
            os.unlink(state_path)

    def test_session_vwap_state_migration_preserves_existing_inventory(self):
        seed, za, _state = make_runner(live=False)
        state = seed._default_state()
        state["strategies"][za["id"]]["basket"] = [{"ticket": 12345}]
        state["strategies"][za["id"]]["basket_sequence"] = 1
        state["strategies"][za["id"]]["current_basket_id"] = "L1-B000001"
        for strat in seed.params["session_vwap_strategies"]:
            state["strategies"].pop(strat["id"])
        state["routing"].pop("session_vwap_policy_id")
        state["routing"].pop("session_vwap_params_hash")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(state, handle)
            state_path = handle.name
        try:
            with patch.object(live_s23_bot, "STATE_FILE", state_path):
                migrated = S23HorizontalInventoryRunner(seed.params)
            self.assertEqual(migrated._st(za)["basket"], [{"ticket": 12345}])
            self.assertTrue(migrated._session_vwap_state_migrated)
            for strat in migrated.params["session_vwap_strategies"]:
                self.assertEqual(migrated._st(strat)["basket"], [])
        finally:
            os.unlink(state_path)

    def test_session_vwap_identity_mismatch_blocks_only_private_lanes(self):
        seed, za, _state = make_runner(live=False)
        state = seed._default_state()
        state["routing"]["session_vwap_params_hash"] = "foreign"
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(state, handle)
            state_path = handle.name
        try:
            with patch.object(live_s23_bot, "STATE_FILE", state_path):
                loaded = S23HorizontalInventoryRunner(seed.params)
            self.assertFalse(loaded._st(za)["sync_block_new_entries"])
            for strat in loaded.params["session_vwap_strategies"]:
                self.assertTrue(loaded._st(strat)["sync_block_new_entries"])
                self.assertEqual(
                    loaded._st(strat)["sync_block_reason"],
                    "session_vwap_policy_identity_mismatch",
                )
        finally:
            os.unlink(state_path)

    def test_session_vwap_continuity_failure_does_not_consume_signal_bar(self):
        runner, _za, _state = make_runner(live=False)
        runner.params["session_vwap_enabled"] = True
        idx = pd.DatetimeIndex(
            [pd.Timestamp("2026-06-10T09:29:00Z"), pd.Timestamp("2026-07-01T09:29:00Z")]
        )
        sparse = pd.DataFrame(
            {
                "Open": [2000.0, 2001.0],
                "High": [2000.2, 2001.2],
                "Low": [1999.8, 2000.8],
                "Close": [2000.0, 2001.0],
                "Volume": [10, 10],
            },
            index=idx,
        )
        runner._session_vwap_snapshot = SimpleNamespace(
            bars=sparse,
            ready=True,
            fresh=True,
            reason="ready",
            failures=0,
            retry_after_seconds=0.0,
        )
        runner.session_vwap_history.ready = True
        runner.session_vwap_history.next_start_pos = 12345
        runner._open_entry = lambda *_args, **_kwargs: self.fail("sparse history must not reach OPEN")
        info = SimpleNamespace(
            bid=2001.0,
            ask=2001.2,
            quote_time_msc=int(pd.Timestamp("2026-07-01T09:30:01Z").timestamp() * 1000),
        )
        readiness = {int(row["lane_id"]): True for row in runner.params["session_vwap_strategies"]}
        runner._process_session_vwap_entries(
            info,
            pd.Timestamp("2026-07-01T09:30:01Z"),
            readiness,
        )
        routing = runner.state["routing"]
        self.assertIsNone(routing["session_vwap_last_evaluated_bar"])
        self.assertEqual(routing["session_vwap_last_unavailable_bar"], "2026-07-01T09:29:00+00:00")
        self.assertFalse(runner.session_vwap_history.ready)
        self.assertEqual(runner.session_vwap_history.next_start_pos, 0)

    def test_session_vwap_future_decision_receipt_blocks_older_bar_replay(self):
        runner, _za, _state = make_runner(live=False)
        runner.params["session_vwap_enabled"] = True
        signal_bar = pd.Timestamp("2026-07-01T09:29:00Z")
        runner._session_vwap_snapshot = SimpleNamespace(
            bars=pd.DataFrame(
                {
                    "Open": [2000.0], "High": [2000.2], "Low": [1999.8],
                    "Close": [2000.1], "Volume": [10],
                },
                index=pd.DatetimeIndex([signal_bar]),
            ),
            ready=True, fresh=True, reason="ready", failures=0,
            retry_after_seconds=0.0,
        )
        future_receipt = dt_text(signal_bar + pd.Timedelta(minutes=5))
        runner.state["routing"]["session_vwap_last_evaluated_bar"] = future_receipt
        runner._open_entry = lambda *_args, **_kwargs: self.fail(
            "an older bar must not pass a future durable receipt"
        )
        info = SimpleNamespace(
            bid=2000.0, ask=2000.1,
            quote_time_msc=int(pd.Timestamp("2026-07-01T09:30:01Z").timestamp() * 1000),
        )
        readiness = {
            int(row["lane_id"]): True
            for row in runner.params["session_vwap_strategies"]
        }
        with (
            patch.object(live_s23_bot, "session_vwap_entry_history_issue", return_value=None),
            patch.object(
                live_s23_bot, "latest_session_vwap_signal",
                return_value=("LONG", pd.Series({"Z": 1.5, "Q90": 1.2})),
            ) as signal_mock,
            patch.object(live_s23_bot, "stale_signal_decision", return_value=SimpleNamespace(stale=False)),
        ):
            runner._process_session_vwap_entries(
                info, pd.Timestamp("2026-07-01T09:30:01Z"), readiness,
            )

        signal_mock.assert_not_called()
        self.assertEqual(
            runner.state["routing"]["session_vwap_last_evaluated_bar"],
            future_receipt,
        )
        for strat in runner.params["session_vwap_strategies"]:
            self.assertTrue(runner._st(strat)["sync_block_new_entries"])
            self.assertEqual(
                runner._st(strat)["sync_block_reason"],
                "session_vwap_decision_receipt_future",
            )

    def test_session_vwap_signal_exception_does_not_commit_decision_receipt(self):
        runner, _za, _state = make_runner(live=False)
        runner.params["session_vwap_enabled"] = True
        signal_bar = pd.Timestamp("2026-07-01T09:29:00Z")
        runner._session_vwap_snapshot = SimpleNamespace(
            bars=pd.DataFrame(
                {
                    "Open": [2000.0], "High": [2000.2], "Low": [1999.8],
                    "Close": [2000.1], "Volume": [10],
                },
                index=pd.DatetimeIndex([signal_bar]),
            ),
            ready=True, fresh=True, reason="ready", failures=0,
            retry_after_seconds=0.0,
        )
        info = SimpleNamespace(
            bid=2000.0, ask=2000.1,
            quote_time_msc=int(pd.Timestamp("2026-07-01T09:30:01Z").timestamp() * 1000),
        )
        readiness = {
            int(row["lane_id"]): True
            for row in runner.params["session_vwap_strategies"]
        }
        events = []
        runner._trade_row = lambda event, _strat, **fields: events.append((event, fields))
        with (
            patch.object(live_s23_bot, "session_vwap_entry_history_issue", return_value=None),
            patch.object(
                live_s23_bot,
                "latest_session_vwap_signal",
                side_effect=ValueError("session_volume_nonpositive"),
            ),
        ):
            runner._process_session_vwap_entries(
                info, pd.Timestamp("2026-07-01T09:30:01Z"), readiness,
            )

        self.assertIsNone(runner.state["routing"]["session_vwap_last_evaluated_bar"])
        self.assertFalse(runner.session_vwap_history.ready)
        self.assertTrue(any(
            event == "session_vwap_decision"
            and fields.get("reason") == "not_evaluated_signal_error"
            for event, fields in events
        ))

    def test_session_vwap_retry_only_lane_stays_ready_past_session_end_until_expiry(self):
        runner, _za, _state = make_runner(live=False)
        runner.params["session_vwap_enabled"] = True
        strat = runner.params["session_vwap_strategies"][0]
        runner._st(strat)["session_vwap_retry_opportunity"] = {"preserved": True}
        runner._sync_strategy = lambda candidate: candidate is strat
        runner._monitor_session_vwap_position = lambda *_args, **_kwargs: False
        after_session = pd.Timestamp("2026-07-01T12:30:30Z")
        info = SimpleNamespace(
            bid=2000.0, ask=2000.1,
            quote_time_msc=int(after_session.timestamp() * 1000),
        )

        readiness = runner._process_session_vwap_exits(info, after_session)

        self.assertTrue(readiness[int(strat["lane_id"])])
        for other in runner.params["session_vwap_strategies"][1:]:
            self.assertFalse(readiness[int(other["lane_id"])])

    def test_session_vwap_completed_revision_blocks_all_private_lanes(self):
        runner, _za, _state = make_runner(live=False)
        runner.params["session_vwap_enabled"] = True
        runner.session_vwap_history.advance = lambda *_args, **_kwargs: SimpleNamespace(
            bars=pd.DataFrame(), ready=True, fresh=False,
            reason="completed_bar_revision_conflict", failures=1,
            retry_after_seconds=5.0,
        )
        at = pd.Timestamp("2026-07-01T09:30:01Z")
        info = SimpleNamespace(
            bid=2000.0, ask=2000.1,
            quote_time_msc=int(at.timestamp() * 1000),
        )

        runner._refresh_session_vwap_history(info, at)

        for strat in runner.params["session_vwap_strategies"]:
            self.assertEqual(
                runner._st(strat)["sync_block_reason"],
                "session_vwap_completed_bar_revision_conflict",
            )

    def test_session_vwap_future_m1_does_not_advance_decision_receipt(self):
        runner, _za, _state = make_runner(live=False)
        runner.params["session_vwap_enabled"] = True
        signal_bar = pd.Timestamp("2026-07-01T09:29:00Z")
        runner._session_vwap_snapshot = SimpleNamespace(
            bars=pd.DataFrame(
                {
                    "Open": [2000.0], "High": [2000.2], "Low": [1999.8],
                    "Close": [2000.1], "Volume": [10],
                },
                index=pd.DatetimeIndex([signal_bar]),
            ),
            ready=True, fresh=True, reason="ready", failures=0,
            retry_after_seconds=0.0,
        )
        readiness = {
            int(row["lane_id"]): True
            for row in runner.params["session_vwap_strategies"]
        }
        info = SimpleNamespace(
            bid=2000.0,
            ask=2000.1,
            quote_time_msc=int(pd.Timestamp("2026-07-01T09:29:30Z").timestamp() * 1000),
        )
        with (
            patch.object(live_s23_bot, "session_vwap_entry_history_issue", return_value=None),
            patch.object(
                live_s23_bot, "latest_session_vwap_signal",
                return_value=("LONG", pd.Series({"Z": 1.5, "Q90": 1.2})),
            ),
        ):
            runner._process_session_vwap_entries(
                info, pd.Timestamp("2026-07-01T09:29:30Z"), readiness,
            )

        self.assertIsNone(runner.state["routing"]["session_vwap_last_evaluated_bar"])

    def test_session_vwap_trade_permission_reject_retries_same_signal_after_cooldown(self):
        runner, _za, _state = make_runner(live=True)
        runner.params["session_vwap_enabled"] = True
        # Keep the retry pending across the next completed M1 so this test
        # verifies that a newer bar cannot replace the saved signal identity.
        runner.params["trade_permission_retry_seconds"] = 90.0
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        executor = CountingExecutor()
        executor.last_order_error = "ERR|10027|DEAL=0"
        runner.executor = executor
        signal_bar = pd.Timestamp("2026-07-01T09:29:00Z")
        bars = pd.DataFrame(
            {
                "Open": [2000.0], "High": [2000.2], "Low": [1999.8],
                "Close": [2000.1], "Volume": [10],
            },
            index=pd.DatetimeIndex([signal_bar]),
        )
        runner._session_vwap_snapshot = SimpleNamespace(
            bars=bars, ready=True, fresh=True, reason="ready", failures=0,
            retry_after_seconds=0.0,
        )
        info = SimpleNamespace(
            bid=2000.0, ask=2000.1,
            quote_time_msc=int(pd.Timestamp("2026-07-01T09:30:01Z").timestamp() * 1000),
        )
        readiness = {int(row["lane_id"]): True for row in runner.params["session_vwap_strategies"]}
        with (
            patch.object(
                live_s23_bot,
                "utc_now",
                return_value=pd.Timestamp("2026-07-01T09:30:01Z").to_pydatetime(),
            ),
            patch.object(live_s23_bot, "session_vwap_entry_history_issue", return_value=None),
            patch.object(
                live_s23_bot,
                "latest_session_vwap_signal",
                return_value=("LONG", pd.Series({"Z": 1.5, "Q90": 1.2})),
            ) as signal_mock,
            patch.object(live_s23_bot, "stale_signal_decision", return_value=SimpleNamespace(stale=False)),
        ):
            runner._process_session_vwap_entries(info, pd.Timestamp("2026-07-01T09:30:01Z"), readiness)
            self.assertEqual(executor.open_calls, 1)
            self.assertIsNotNone(state["open_retry_after_utc"])
            self.assertEqual(
                runner.state["routing"]["session_vwap_last_evaluated_bar"],
                dt_text(signal_bar),
            )
            self.assertEqual(
                state["session_vwap_retry_opportunity"]["signal_bar_time"],
                dt_text(signal_bar),
            )

            newer_bar = pd.Timestamp("2026-07-01T09:30:00Z")
            newer_bars = pd.concat(
                [
                    bars,
                    pd.DataFrame(
                        {
                            "Open": [2000.1], "High": [2000.3], "Low": [1999.9],
                            "Close": [2000.2], "Volume": [11],
                        },
                        index=pd.DatetimeIndex([newer_bar]),
                    ),
                ]
            )
            runner._session_vwap_snapshot = SimpleNamespace(
                bars=newer_bars, ready=True, fresh=True, reason="ready", failures=0,
                retry_after_seconds=0.0,
            )
            info.quote_time_msc = int(pd.Timestamp("2026-07-01T09:31:01Z").timestamp() * 1000)
            signal_mock.return_value = (None, pd.Series({"Z": 0.1, "Q90": 1.2}))
            runner._process_session_vwap_entries(info, pd.Timestamp("2026-07-01T09:31:01Z"), readiness)
            self.assertEqual(executor.open_calls, 1)
            self.assertEqual(
                state["session_vwap_retry_opportunity"]["signal_bar_time"],
                dt_text(signal_bar),
            )

            restarted, _za2, _state2 = make_runner(live=True)
            restarted.params["session_vwap_enabled"] = True
            restarted.state = json.loads(json.dumps(runner.state))
            restarted._session_vwap_snapshot = runner._session_vwap_snapshot
            restarted.executor = executor
            restarted_state = restarted._st(strat)
            restarted_state["open_retry_after_utc"] = "2026-07-01T09:30:00+00:00"
            restarted._process_session_vwap_entries(
                info,
                pd.Timestamp("2026-07-01T09:31:31Z"),
                readiness,
            )
            self.assertEqual(executor.open_calls, 2)
            self.assertEqual(
                restarted_state["session_vwap_retry_opportunity"]["signal_bar_time"],
                dt_text(signal_bar),
            )

    def test_session_vwap_does_not_reuse_same_direction_signal_known_at_close(self):
        runner, _za, _state = make_runner(live=False)
        runner.params["session_vwap_enabled"] = True
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        state["basket"] = [{"side": "LONG", "lot": 0.01, "entry_price": 2000.0}]
        close_request_bar = pd.Timestamp("2026-07-01T09:45:00Z")
        confirmed_close = pd.Timestamp("2026-07-01T09:47:03Z")
        runner._clear_basket_state(
            strat,
            "session_vwap_fixed_hold",
            dt_text(close_request_bar),
            closed_at_utc=confirmed_close,
        )
        self.assertEqual(state["last_closed_side"], "LONG")
        self.assertEqual(state["last_closed_at_utc"], dt_text(confirmed_close))
        # This bar became available after the close request, but before the
        # broker-confirmed close. It must not escape through another lane.
        signal_bar = pd.Timestamp("2026-07-01T09:46:00Z")
        bars = pd.DataFrame(
            {
                "Open": [2000.0], "High": [2000.2], "Low": [1999.8],
                "Close": [2000.1], "Volume": [10],
            },
            index=pd.DatetimeIndex([signal_bar]),
        )
        runner._session_vwap_snapshot = SimpleNamespace(
            bars=bars, ready=True, fresh=True, reason="ready", failures=0,
            retry_after_seconds=0.0,
        )
        events = []
        runner._trade_row = lambda event, _strat, **fields: events.append((event, fields))
        runner._open_entry = lambda *_args, **_kwargs: self.fail(
            "same-direction signal known at close must not reopen"
        )
        info = SimpleNamespace(
            bid=2000.0, ask=2000.1,
            quote_time_msc=int(pd.Timestamp("2026-07-01T09:47:04Z").timestamp() * 1000),
        )
        readiness = {int(row["lane_id"]): True for row in runner.params["session_vwap_strategies"]}
        with (
            patch.object(live_s23_bot, "session_vwap_entry_history_issue", return_value=None),
            patch.object(
                live_s23_bot,
                "latest_session_vwap_signal",
                return_value=("LONG", pd.Series({"Z": 1.5, "Q90": 1.2})),
            ),
            patch.object(live_s23_bot, "stale_signal_decision", return_value=SimpleNamespace(stale=False)),
        ):
            runner._process_session_vwap_entries(
                info,
                pd.Timestamp("2026-07-01T09:47:04Z"),
                readiness,
            )
        reasons = [fields.get("reason") for event, fields in events if event == "session_vwap_decision"]
        self.assertIn("stale_same_direction_after_close", reasons)
        self.assertFalse(state["basket"])

    def test_session_vwap_malformed_close_ledger_fails_closed(self):
        runner, _za, _state = make_runner(live=False)
        runner.params["session_vwap_enabled"] = True
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        state["last_closed_side"] = "LONG"
        state["last_closed_at_utc"] = "not-a-timestamp"
        signal_bar = pd.Timestamp("2026-07-01T09:46:00Z")
        runner._session_vwap_snapshot = SimpleNamespace(
            bars=pd.DataFrame(
                {
                    "Open": [2000.0], "High": [2000.2], "Low": [1999.8],
                    "Close": [2000.1], "Volume": [10],
                },
                index=pd.DatetimeIndex([signal_bar]),
            ),
            ready=True,
            fresh=True,
            reason="ready",
            failures=0,
            retry_after_seconds=0.0,
        )
        events = []
        runner._trade_row = lambda event, _strat, **fields: events.append((event, fields))
        runner._open_entry = lambda *_args, **_kwargs: self.fail(
            "malformed same-direction close ledger must not reopen"
        )
        info = SimpleNamespace(
            bid=2000.0,
            ask=2000.1,
            quote_time_msc=int(pd.Timestamp("2026-07-01T09:47:04Z").timestamp() * 1000),
        )
        readiness = {
            int(row["lane_id"]): True
            for row in runner.params["session_vwap_strategies"]
        }
        with (
            patch.object(live_s23_bot, "session_vwap_entry_history_issue", return_value=None),
            patch.object(
                live_s23_bot,
                "latest_session_vwap_signal",
                return_value=("LONG", pd.Series({"Z": 1.5, "Q90": 1.2})),
            ),
            patch.object(
                live_s23_bot,
                "stale_signal_decision",
                return_value=SimpleNamespace(stale=False),
            ),
        ):
            runner._process_session_vwap_entries(
                info,
                pd.Timestamp("2026-07-01T09:47:04Z"),
                readiness,
            )

        reasons = [
            fields.get("reason")
            for event, fields in events
            if event == "session_vwap_decision"
        ]
        self.assertIn("last_closed_state_invalid", reasons)
        self.assertFalse(state["basket"])

    def test_session_vwap_malformed_close_side_invalidates_ledger(self):
        runner, _za, _state = make_runner(live=False)
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        state["last_closed_side"] = "SIDEWAYS"
        state["last_closed_at_utc"] = "2026-07-01T09:47:03Z"

        cutoff, invalid = runner._session_vwap_closed_cutoff("LONG")

        self.assertIsNone(cutoff)
        self.assertTrue(invalid)

    def test_session_vwap_numeric_close_time_invalidates_ledger(self):
        runner, _za, _state = make_runner(live=False)
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        state["last_closed_side"] = "LONG"
        state["last_closed_at_utc"] = 123

        cutoff, invalid = runner._session_vwap_closed_cutoff(
            "LONG", pd.Timestamp("2026-07-01T09:47:04Z")
        )

        self.assertIsNone(cutoff)
        self.assertTrue(invalid)

    def test_session_vwap_future_close_ledger_is_invalid(self):
        runner, _za, _state = make_runner(live=False)
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        state["last_closed_side"] = "LONG"
        state["last_closed_at_utc"] = "2026-07-02T09:47:03Z"

        cutoff, invalid = runner._session_vwap_closed_cutoff(
            "LONG", pd.Timestamp("2026-07-01T09:47:04Z")
        )

        self.assertIsNone(cutoff)
        self.assertTrue(invalid)

    def test_session_vwap_live_close_deal_blocks_signal_reuse_across_lanes(self):
        runner, _za, _state = make_runner(live=True)
        runner.params["session_vwap_enabled"] = True
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        position_id = 8816
        close_request_bar = pd.Timestamp("2026-07-01T09:45:00Z")
        confirmed_close = pd.Timestamp("2026-07-01T09:47:03Z")
        state["basket"] = [{
            "ticket": 9916,
            "position_identifier": position_id,
            "side": "LONG",
            "lot": float(strat["lot"]),
            "entry_price": 2000.0,
            "entry_time_utc": "2026-07-01T09:30:02+00:00",
            "open_time_epoch": int(pd.Timestamp("2026-07-01T09:30:02Z").timestamp()),
            "owner_symbol": "XAUUSD",
            "owner_magic": int(strat["magic"]),
            "owner_comment": strat["comment_prefix"],
            "opportunity_id": "prior-long",
            "shadow": False,
        }]
        bind_owned_basket_identity(strat, state)
        state["pending_close_reason"] = "session_vwap_fixed_hold"
        state["pending_close_signal_bar"] = dt_text(close_request_bar)
        executor = CountingExecutor()
        executor.close_deal = SimpleNamespace(
            position_id=position_id,
            symbol="XAUUSD",
            magic=int(strat["magic"]),
            net_profit=1.25,
            price=2000.2,
            deal=77116,
            exit_volume=0.01,
            deal_time=int(confirmed_close.timestamp()),
        )
        runner.executor = executor
        events = []
        runner._trade_row = lambda event, _strat, **fields: events.append((event, fields))

        self.assertTrue(runner._sync_strategy(strat))
        self.assertEqual(state["last_closed_side"], "LONG")
        self.assertEqual(parse_ts(state["last_closed_at_utc"]), confirmed_close)

        signal_bar = pd.Timestamp("2026-07-01T09:46:00Z")
        runner._session_vwap_snapshot = SimpleNamespace(
            bars=pd.DataFrame(
                {"Open": [2000.0], "High": [2000.2], "Low": [1999.8], "Close": [2000.1], "Volume": [10]},
                index=pd.DatetimeIndex([signal_bar]),
            ),
            ready=True,
            fresh=True,
            reason="ready",
            failures=0,
            retry_after_seconds=0.0,
        )
        runner._open_entry = lambda *_args, **_kwargs: self.fail(
            "signal available before the live close deal must not reopen in another lane"
        )
        info = SimpleNamespace(
            bid=2000.0,
            ask=2000.1,
            quote_time_msc=int(pd.Timestamp("2026-07-01T09:47:04Z").timestamp() * 1000),
        )
        readiness = {int(row["lane_id"]): True for row in runner.params["session_vwap_strategies"]}
        with (
            patch.object(live_s23_bot, "session_vwap_entry_history_issue", return_value=None),
            patch.object(live_s23_bot, "latest_session_vwap_signal", return_value=("LONG", pd.Series({"Z": 1.5, "Q90": 1.2}))),
            patch.object(live_s23_bot, "stale_signal_decision", return_value=SimpleNamespace(stale=False)),
        ):
            runner._process_session_vwap_entries(
                info,
                pd.Timestamp("2026-07-01T09:47:04Z"),
                readiness,
            )
        reasons = [fields.get("reason") for event, fields in events if event == "session_vwap_decision"]
        self.assertIn("stale_same_direction_after_close", reasons)

    def test_session_vwap_restart_adopts_one_exact_pending_open_fill(self):
        runner, _za, _state = make_runner(live=True)
        runner.params["session_vwap_enabled"] = True
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        opportunity_id = "XAUUSD|2026-07-01T09:29:00+00:00|session_vwap_extension_fade|LONG"
        opportunity = {
            "opportunity_id": opportunity_id,
            "source": "session_vwap_extension_fade",
            "side": "LONG",
            "raw_side": "LONG",
            "effective_side": "LONG",
            "event_time": "2026-07-01T09:29:00+00:00",
            "release_time": "2026-07-01T09:30:00+00:00",
            "available_time": "2026-07-01T09:30:00+00:00",
        }
        state["pending_open_opportunity_id"] = opportunity_id
        state["pending_open_started_utc"] = "2026-07-01T09:30:01+00:00"
        state.update({
            "pending_open_expires_utc": "2026-07-01T09:32:00+00:00",
            "pending_open_side": "LONG", "pending_open_lot": float(strat["lot"]),
            "pending_open_symbol": "XAUUSD", "pending_open_magic": int(strat["magic"]),
            "pending_open_comment": str(strat["comment_prefix"]),
            "pending_open_signal_bar": "2026-07-01T09:29:00+00:00",
            "pending_open_reverse_used": False, "pending_open_expected_positions": 0,
        })
        state["session_vwap_retry_opportunity"] = {
            "opportunity": opportunity,
            "signal_bar_time": "2026-07-01T09:29:00+00:00",
            "expires_utc": "2026-07-01T09:32:00+00:00",
            "note": "session_vwap_retry",
        }
        ticket = 9913
        position = SimpleNamespace(
            ticket=ticket,
            identifier=8813,
            symbol="XAUUSD",
            magic=int(strat["magic"]),
            comment=strat["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=float(strat["lot"]),
            open_price=2000.15,
            open_time=int(pd.Timestamp("2026-07-01T09:30:02Z").timestamp()),
        )
        executor = CountingExecutor()
        executor.positions = [position]
        runner.executor = executor

        self.assertTrue(runner._sync_strategy(strat))
        self.assertEqual(len(state["basket"]), 1)
        self.assertEqual(state["basket"][0]["position_identifier"], 8813)
        self.assertEqual(state["basket"][0]["opportunity_id"], opportunity_id)
        self.assertIsNone(state["pending_open_opportunity_id"])
        self.assertIsNone(state["session_vwap_retry_opportunity"])

    def test_session_vwap_restart_rejects_fill_when_basket_sequence_is_invalid(self):
        runner, _za, _state = make_runner(live=True)
        runner.params["session_vwap_enabled"] = True
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        signal_bar = pd.Timestamp("2026-07-01T09:29:00Z")
        release_time = signal_bar + pd.Timedelta(minutes=1)
        opportunity_id = f"XAUUSD|{dt_text(signal_bar)}|session_vwap_extension_fade|LONG"
        state.update({
            "basket_sequence": "broken",
            "pending_open_opportunity_id": opportunity_id,
            "pending_open_started_utc": "2026-07-01T09:30:01+00:00",
            "pending_open_expires_utc": "2026-07-01T09:32:00+00:00",
            "pending_open_side": "LONG", "pending_open_lot": float(strat["lot"]),
            "pending_open_symbol": "XAUUSD", "pending_open_magic": int(strat["magic"]),
            "pending_open_comment": str(strat["comment_prefix"]),
            "pending_open_signal_bar": "2026-07-01T09:29:00+00:00",
            "pending_open_reverse_used": False, "pending_open_expected_positions": 0,
            "session_vwap_retry_opportunity": {
                "opportunity": {
                    "opportunity_id": opportunity_id,
                    "source": "session_vwap_extension_fade",
                    "side": "LONG",
                    "raw_side": "LONG",
                    "effective_side": "LONG",
                    "event_time": dt_text(signal_bar),
                    "release_time": dt_text(release_time),
                    "available_time": dt_text(release_time),
                },
                "signal_bar_time": dt_text(signal_bar),
                "expires_utc": dt_text(release_time + pd.Timedelta(minutes=2)),
                "note": "session_vwap_retry",
            },
        })
        executor = CountingExecutor()
        executor.positions = [SimpleNamespace(
            ticket=9914,
            identifier=8814,
            symbol="XAUUSD",
            magic=int(strat["magic"]),
            comment=strat["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=float(strat["lot"]),
            open_price=2000.15,
            open_time=int(pd.Timestamp("2026-07-01T09:30:02Z").timestamp()),
        )]
        runner.executor = executor

        self.assertFalse(runner._sync_strategy(strat))
        self.assertFalse(state["basket"])
        self.assertEqual(state["sync_block_reason"], "live_positions_without_state")

    def test_session_vwap_restart_rejects_coercible_numeric_receipt_timestamps(self):
        runner, _za, _state = make_runner(live=True)
        runner.params["session_vwap_enabled"] = True
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        signal_bar = pd.Timestamp("2026-07-01T09:29:00Z")
        release_time = signal_bar + pd.Timedelta(minutes=1)
        opportunity_id = f"XAUUSD|{dt_text(signal_bar)}|session_vwap_extension_fade|LONG"
        state["pending_open_opportunity_id"] = opportunity_id
        state["pending_open_started_utc"] = int((release_time + pd.Timedelta(seconds=1)).value)
        state["session_vwap_retry_opportunity"] = {
            "opportunity": {
                "opportunity_id": opportunity_id,
                "source": "session_vwap_extension_fade",
                "side": "LONG",
                "raw_side": "LONG",
                "effective_side": "LONG",
                "event_time": int(signal_bar.value),
                "release_time": int(release_time.value),
                "available_time": int(release_time.value),
            },
            "signal_bar_time": int(signal_bar.value),
            "expires_utc": int((release_time + pd.Timedelta(minutes=2)).value),
            "note": "session_vwap_retry",
        }
        position = SimpleNamespace(
            ticket=9914,
            identifier=8814,
            symbol="XAUUSD",
            magic=int(strat["magic"]),
            comment=strat["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=float(strat["lot"]),
            open_price=2000.15,
            open_time=int((release_time + pd.Timedelta(seconds=2)).timestamp()),
        )
        executor = CountingExecutor()
        executor.positions = [position]
        runner.executor = executor

        self.assertFalse(runner._sync_strategy(strat))
        self.assertFalse(state["basket"])
        self.assertEqual(state["sync_block_reason"], "live_positions_without_state")
        self.assertEqual(state["pending_open_opportunity_id"], opportunity_id)

    def test_session_vwap_restart_rejects_pending_started_after_signal_expiry(self):
        runner, _za, _state = make_runner(live=True)
        runner.params["session_vwap_enabled"] = True
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        signal_bar = pd.Timestamp("2026-07-01T09:29:00Z")
        opportunity_id = f"XAUUSD|{dt_text(signal_bar)}|session_vwap_extension_fade|LONG"
        state["pending_open_opportunity_id"] = opportunity_id
        state["pending_open_started_utc"] = "2026-07-01T09:40:01+00:00"
        state["session_vwap_retry_opportunity"] = {
            "opportunity": {
                "opportunity_id": opportunity_id,
                "source": "session_vwap_extension_fade",
                "side": "LONG",
                "raw_side": "LONG",
                "effective_side": "LONG",
                "event_time": dt_text(signal_bar),
                "release_time": dt_text(signal_bar + pd.Timedelta(minutes=1)),
                "available_time": dt_text(signal_bar + pd.Timedelta(minutes=1)),
            },
            "signal_bar_time": dt_text(signal_bar),
            "expires_utc": dt_text(signal_bar + pd.Timedelta(minutes=3)),
            "note": "session_vwap_retry",
        }
        position = SimpleNamespace(
            ticket=9922,
            identifier=8822,
            symbol="XAUUSD",
            magic=int(strat["magic"]),
            comment=strat["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=float(strat["lot"]),
            open_price=2000.15,
            open_time=int(pd.Timestamp("2026-07-01T09:40:02Z").timestamp()),
        )
        executor = CountingExecutor()
        executor.positions = [position]
        runner.executor = executor

        self.assertFalse(runner._sync_strategy(strat))
        self.assertFalse(state["basket"])
        self.assertEqual(state["sync_block_reason"], "live_positions_without_state")
        self.assertEqual(state["pending_open_opportunity_id"], opportunity_id)

    def test_session_vwap_restart_rejects_tampered_pending_opportunity_identity(self):
        runner, _za, _state = make_runner(live=True)
        runner.params["session_vwap_enabled"] = True
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        signal_bar = pd.Timestamp("2026-07-01T09:29:00Z")
        opportunity_id = f"XAUUSD|{dt_text(signal_bar)}|session_vwap_extension_fade|LONG"
        state["pending_open_opportunity_id"] = opportunity_id
        state["pending_open_started_utc"] = "2026-07-01T09:30:01+00:00"
        state["session_vwap_retry_opportunity"] = {
            "opportunity": {
                "opportunity_id": opportunity_id,
                "source": "tampered_source",
                "side": "LONG",
                "raw_side": "LONG",
                "effective_side": "LONG",
                "event_time": dt_text(signal_bar),
                "release_time": dt_text(signal_bar + pd.Timedelta(minutes=1)),
                "available_time": dt_text(signal_bar + pd.Timedelta(minutes=1)),
            },
            "signal_bar_time": dt_text(signal_bar),
            "expires_utc": dt_text(signal_bar + pd.Timedelta(minutes=3)),
            "note": "session_vwap_retry",
        }
        position = SimpleNamespace(
            ticket=9921,
            identifier=8821,
            symbol="XAUUSD",
            magic=int(strat["magic"]),
            comment=strat["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=float(strat["lot"]),
            open_price=2000.15,
            open_time=int(pd.Timestamp("2026-07-01T09:30:02Z").timestamp()),
        )
        executor = CountingExecutor()
        executor.positions = [position]
        runner.executor = executor

        self.assertFalse(runner._sync_strategy(strat))
        self.assertFalse(state["basket"])
        self.assertEqual(state["sync_block_reason"], "live_positions_without_state")
        self.assertEqual(state["pending_open_opportunity_id"], opportunity_id)

    def test_session_vwap_retry_rejects_side_that_disagrees_with_opportunity_id(self):
        runner, _za, _state = make_runner(live=True)
        runner.params["session_vwap_enabled"] = True
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        signal_bar = pd.Timestamp("2026-07-01T09:29:00Z")
        opportunity_id = f"XAUUSD|{dt_text(signal_bar)}|session_vwap_extension_fade|LONG"
        state["session_vwap_retry_opportunity"] = {
            "opportunity": {
                "opportunity_id": opportunity_id,
                "source": "session_vwap_extension_fade",
                "side": "SHORT",
                "raw_side": "SHORT",
                "effective_side": "SHORT",
                "event_time": dt_text(signal_bar),
                "release_time": dt_text(signal_bar + pd.Timedelta(minutes=1)),
                "available_time": dt_text(signal_bar + pd.Timedelta(minutes=1)),
            },
            "signal_bar_time": dt_text(signal_bar),
            "expires_utc": dt_text(signal_bar + pd.Timedelta(minutes=3)),
            "note": "session_vwap_retry",
        }
        routed = []
        events = []
        runner._open_entry = lambda _strat, side, *_args, **_kwargs: (routed.append(side) or False)
        runner._trade_row = lambda event, _strat, **fields: events.append((event, fields))
        info = SimpleNamespace(bid=2000.0, ask=2000.1)

        runner._process_session_vwap_retries(
            info,
            pd.Timestamp("2026-07-01T09:30:30Z"),
            {int(strat["lane_id"]): True},
        )

        self.assertEqual(routed, [])
        self.assertIsNone(state["session_vwap_retry_opportunity"])
        reasons = [fields.get("reason") for event, fields in events if event == "session_vwap_decision"]
        self.assertIn("retry_state_invalid", reasons)

    def test_session_vwap_non_object_retry_state_is_durably_discarded(self):
        runner, _za, _state = make_runner(live=False)
        runner.params["session_vwap_enabled"] = True
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        state["session_vwap_retry_opportunity"] = "corrupt-retry"
        events = []
        runner._trade_row = lambda event, _strat, **fields: events.append((event, fields))

        runner._process_session_vwap_retries(
            SimpleNamespace(bid=2000.0, ask=2000.1),
            pd.Timestamp("2026-07-01T09:30:30Z"),
            {int(strat["lane_id"]): True},
        )

        self.assertIsNone(state["session_vwap_retry_opportunity"])
        self.assertTrue(any(
            event == "session_vwap_decision"
            and fields.get("reason") == "retry_state_invalid"
            for event, fields in events
        ))

    def test_session_vwap_retry_rejects_numeric_future_open_cooldown(self):
        runner, _za, _state = make_runner(live=True)
        runner.params["session_vwap_enabled"] = True
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        signal_bar = pd.Timestamp("2026-07-01T09:29:00Z")
        release_time = signal_bar + pd.Timedelta(minutes=1)
        opportunity_id = f"XAUUSD|{dt_text(signal_bar)}|session_vwap_extension_fade|LONG"
        state["session_vwap_retry_opportunity"] = {
            "opportunity": {
                "opportunity_id": opportunity_id,
                "source": "session_vwap_extension_fade",
                "side": "LONG",
                "raw_side": "LONG",
                "effective_side": "LONG",
                "event_time": dt_text(signal_bar),
                "release_time": dt_text(release_time),
                "available_time": dt_text(release_time),
            },
            "signal_bar_time": dt_text(signal_bar),
            "expires_utc": dt_text(release_time + pd.Timedelta(minutes=2)),
            "note": "session_vwap_retry",
        }
        state["open_retry_after_utc"] = int(
            (release_time + pd.Timedelta(days=1)).value
        )
        routed = []
        events = []
        runner._open_entry = lambda *_args, **_kwargs: (routed.append(True) or False)
        runner._trade_row = lambda event, _strat, **fields: events.append((event, fields))

        runner._process_session_vwap_retries(
            SimpleNamespace(bid=2000.0, ask=2000.1),
            release_time + pd.Timedelta(seconds=30),
            {int(strat["lane_id"]): True},
        )

        self.assertEqual(routed, [])
        self.assertIsNone(state["session_vwap_retry_opportunity"])
        reasons = [fields.get("reason") for event, fields in events if event == "session_vwap_decision"]
        self.assertIn("open_retry_state_invalid", reasons)

    def test_session_vwap_retry_is_not_submitted_before_signal_release(self):
        runner, _za, _state = make_runner(live=True)
        runner.params["session_vwap_enabled"] = True
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        signal_bar = pd.Timestamp("2026-07-01T09:31:00Z")
        release_time = signal_bar + pd.Timedelta(minutes=1)
        opportunity_id = f"XAUUSD|{dt_text(signal_bar)}|session_vwap_extension_fade|LONG"
        state["session_vwap_retry_opportunity"] = {
            "opportunity": {
                "opportunity_id": opportunity_id,
                "source": "session_vwap_extension_fade",
                "side": "LONG",
                "raw_side": "LONG",
                "effective_side": "LONG",
                "event_time": dt_text(signal_bar),
                "release_time": dt_text(release_time),
                "available_time": dt_text(release_time),
            },
            "signal_bar_time": dt_text(signal_bar),
            "expires_utc": dt_text(release_time + pd.Timedelta(minutes=2)),
            "note": "session_vwap_retry",
        }
        routed = []
        runner._open_entry = lambda *_args, **_kwargs: (routed.append(True) or False)

        runner._process_session_vwap_retries(
            SimpleNamespace(bid=2000.0, ask=2000.1),
            pd.Timestamp("2026-07-01T09:31:30Z"),
            {int(strat["lane_id"]): True},
        )

        self.assertEqual(routed, [])
        self.assertIsNotNone(state["session_vwap_retry_opportunity"])

    def test_session_vwap_retry_expiry_uses_broker_quote_clock_when_host_lags(self):
        runner, _za, _state = make_runner(live=True)
        runner.params["session_vwap_enabled"] = True
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        signal_bar = pd.Timestamp("2026-07-01T09:29:00Z")
        opportunity_id = f"XAUUSD|{dt_text(signal_bar)}|session_vwap_extension_fade|LONG"
        state["session_vwap_retry_opportunity"] = {
            "opportunity": {
                "opportunity_id": opportunity_id,
                "source": "session_vwap_extension_fade",
                "side": "LONG",
                "raw_side": "LONG",
                "effective_side": "LONG",
                "event_time": dt_text(signal_bar),
                "release_time": dt_text(signal_bar + pd.Timedelta(minutes=1)),
                "available_time": dt_text(signal_bar + pd.Timedelta(minutes=1)),
            },
            "signal_bar_time": dt_text(signal_bar),
            "expires_utc": dt_text(signal_bar + pd.Timedelta(minutes=3)),
            "note": "session_vwap_retry",
        }
        broker_time = pd.Timestamp("2026-07-01T09:33:00Z")
        host_time = pd.Timestamp("2026-07-01T09:31:00Z")
        info = SimpleNamespace(
            bid=2000.0,
            ask=2000.1,
            quote_time_msc=int(broker_time.timestamp() * 1000),
        )
        runner._session_vwap_snapshot = SimpleNamespace(
            bars=pd.DataFrame(), ready=False, fresh=False, reason="not_ready",
            failures=0, retry_after_seconds=0.0,
        )
        runner._open_entry = lambda *_args, **_kwargs: self.fail(
            "broker-expired retry must not reach OPEN when the host clock lags"
        )

        runner._process_session_vwap_entries(
            info,
            host_time,
            {int(strat["lane_id"]): True},
        )

        self.assertIsNone(state["session_vwap_retry_opportunity"])

    def test_session_vwap_retry_expiry_uses_host_clock_when_broker_quote_is_stale(self):
        runner, _za, _state = make_runner(live=True)
        runner.params["session_vwap_enabled"] = True
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        signal_bar = pd.Timestamp("2026-07-01T09:29:00Z")
        opportunity_id = f"XAUUSD|{dt_text(signal_bar)}|session_vwap_extension_fade|LONG"
        state["session_vwap_retry_opportunity"] = {
            "opportunity": {
                "opportunity_id": opportunity_id,
                "source": "session_vwap_extension_fade",
                "side": "LONG",
                "raw_side": "LONG",
                "effective_side": "LONG",
                "event_time": dt_text(signal_bar),
                "release_time": dt_text(signal_bar + pd.Timedelta(minutes=1)),
                "available_time": dt_text(signal_bar + pd.Timedelta(minutes=1)),
            },
            "signal_bar_time": dt_text(signal_bar),
            "expires_utc": dt_text(signal_bar + pd.Timedelta(minutes=3)),
            "note": "session_vwap_retry",
        }
        broker_time = pd.Timestamp("2026-07-01T09:30:30Z")
        host_time = pd.Timestamp("2026-07-01T09:40:00Z")
        info = SimpleNamespace(
            bid=2000.0,
            ask=2000.1,
            quote_time_msc=int(broker_time.timestamp() * 1000),
        )
        runner._session_vwap_snapshot = SimpleNamespace(
            bars=pd.DataFrame(), ready=False, fresh=False, reason="not_ready",
            failures=0, retry_after_seconds=0.0,
        )
        runner._open_entry = lambda *_args, **_kwargs: self.fail(
            "a host-expired retry must not use an old broker quote to reach OPEN"
        )

        runner._process_session_vwap_entries(
            info,
            host_time,
            {int(strat["lane_id"]): True},
        )

        self.assertIsNone(state["session_vwap_retry_opportunity"])

    def test_session_vwap_trade_permission_cooldown_starts_from_broker_execution_time(self):
        runner, _za, _state = make_runner(live=True)
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        executor = CountingExecutor()
        executor.last_order_error = "ERR|10027|DEAL=0"
        runner.executor = executor
        signal_bar = pd.Timestamp("2026-07-01T09:29:00Z")
        broker_time = pd.Timestamp("2026-07-01T09:30:05Z")
        host_time = broker_time + pd.Timedelta(seconds=1)
        opportunity_id = f"XAUUSD|{dt_text(signal_bar)}|session_vwap_extension_fade|LONG"
        opportunity = {
            "opportunity_id": opportunity_id,
            "source": "session_vwap_extension_fade",
            "side": "LONG",
            "event_time": dt_text(signal_bar),
            "release_time": dt_text(signal_bar + pd.Timedelta(minutes=1)),
        }
        price_row = pd.Series(
            {"Open": 2000.0, "Close": 2000.1, "AskOpen": 2000.15},
            name=signal_bar,
        )
        info = SimpleNamespace(
            bid=2000.1,
            ask=2000.15,
            quote_time_msc=int(broker_time.timestamp() * 1000),
        )

        with patch.object(live_s23_bot, "utc_now", return_value=host_time.to_pydatetime()):
            self.assertTrue(
                runner._open_entry(
                    strat,
                    "LONG",
                    price_row,
                    info,
                    execution_time=broker_time,
                    opportunity=opportunity,
                    apply_portfolio_rearm=False,
                    use_confirmed_fill_time=True,
                )
            )

        self.assertEqual(
            parse_ts(state["open_retry_after_utc"]),
            broker_time + pd.Timedelta(seconds=30),
        )

    def test_session_vwap_initial_open_rejects_confirmed_position_lot_mismatch(self):
        runner, _za, _state = make_runner(live=True)
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        signal_bar = pd.Timestamp("2026-07-01T09:29:00Z")
        opportunity_id = f"XAUUSD|{dt_text(signal_bar)}|session_vwap_extension_fade|LONG"
        opportunity = {
            "opportunity_id": opportunity_id,
            "source": "session_vwap_extension_fade",
            "side": "LONG",
            "event_time": dt_text(signal_bar),
            "release_time": dt_text(signal_bar + pd.Timedelta(minutes=1)),
        }
        position = SimpleNamespace(
            ticket=9918,
            identifier=8818,
            symbol="XAUUSD",
            magic=int(strat["magic"]),
            comment=strat["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=float(strat["lot"]) * 2.0,
            open_price=2000.15,
            open_time=int(pd.Timestamp("2026-07-01T09:30:02Z").timestamp()),
        )
        executor = CountingExecutor()
        def confirm_mismatched_position(*_args, **_kwargs):
            executor.open_calls += 1
            executor.positions = [position]
            return 9918
        executor.open_position = confirm_mismatched_position
        runner.executor = executor
        price_row = pd.Series(
            {"Open": 2000.0, "Close": 2000.1, "AskOpen": 2000.15},
            name=signal_bar,
        )
        submit_time = pd.Timestamp("2026-07-01T09:30:02Z")
        info = SimpleNamespace(
            bid=2000.1,
            ask=2000.15,
            quote_time_msc=int(submit_time.timestamp() * 1000),
        )

        with patch.object(live_s23_bot, "utc_now", return_value=submit_time.to_pydatetime()):
            self.assertTrue(
                runner._open_entry(
                    strat,
                    "LONG",
                    price_row,
                    info,
                    execution_time=submit_time,
                    opportunity=opportunity,
                    apply_portfolio_rearm=False,
                    use_confirmed_fill_time=True,
                )
            )
        self.assertFalse(state["basket"])
        self.assertEqual(state["pending_open_opportunity_id"], opportunity_id)
        self.assertEqual(state["sync_block_reason"], "open_confirmation_mismatch")

    def test_session_vwap_initial_open_accepts_exact_position_in_submission_window(self):
        runner, _za, _state = make_runner(live=True)
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        signal_bar = pd.Timestamp("2026-07-01T09:29:00Z")
        submit_time = pd.Timestamp("2026-07-01T09:30:01Z")
        opportunity_id = f"XAUUSD|{dt_text(signal_bar)}|session_vwap_extension_fade|LONG"
        opportunity = {
            "opportunity_id": opportunity_id,
            "source": "session_vwap_extension_fade",
            "side": "LONG",
            "event_time": dt_text(signal_bar),
            "release_time": dt_text(signal_bar + pd.Timedelta(minutes=1)),
        }
        position = SimpleNamespace(
            ticket=9920,
            identifier=8820,
            symbol="XAUUSD",
            magic=int(strat["magic"]),
            comment=strat["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=float(strat["lot"]),
            open_price=2000.15,
            open_time=int((submit_time + pd.Timedelta(seconds=1)).timestamp()),
        )
        executor = CountingExecutor()
        def confirm_exact_position(*_args, **_kwargs):
            executor.open_calls += 1
            executor.positions = [position]
            return 9920
        executor.open_position = confirm_exact_position
        runner.executor = executor
        price_row = pd.Series(
            {"Open": 2000.0, "Close": 2000.1, "AskOpen": 2000.15},
            name=signal_bar,
        )
        info = SimpleNamespace(
            bid=2000.1,
            ask=2000.15,
            quote_time_msc=int(submit_time.timestamp() * 1000),
        )

        with patch.object(live_s23_bot, "utc_now", return_value=submit_time.to_pydatetime()):
            self.assertTrue(
                runner._open_entry(
                    strat,
                    "LONG",
                    price_row,
                    info,
                    execution_time=submit_time,
                    opportunity=opportunity,
                    apply_portfolio_rearm=False,
                    use_confirmed_fill_time=True,
                )
            )
        self.assertEqual(len(state["basket"]), 1)
        self.assertEqual(state["basket"][0]["position_identifier"], 8820)
        self.assertEqual(state["basket"][0]["lot"], float(strat["lot"]))
        self.assertIsNone(state["pending_open_opportunity_id"])

    def test_session_vwap_ambiguous_open_does_not_adopt_old_same_lane_position(self):
        runner, _za, _state = make_runner(live=True)
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        signal_bar = pd.Timestamp("2026-07-01T09:29:00Z")
        opportunity_id = f"XAUUSD|{dt_text(signal_bar)}|session_vwap_extension_fade|LONG"
        opportunity = {
            "opportunity_id": opportunity_id,
            "source": "session_vwap_extension_fade",
            "side": "LONG",
            "event_time": dt_text(signal_bar),
            "release_time": dt_text(signal_bar + pd.Timedelta(minutes=1)),
        }
        old_position = SimpleNamespace(
            ticket=9919,
            identifier=8819,
            symbol="XAUUSD",
            magic=int(strat["magic"]),
            comment=strat["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=float(strat["lot"]),
            open_price=2000.15,
            open_time=int(pd.Timestamp("2026-07-01T08:30:02Z").timestamp()),
        )
        executor = CountingExecutor()
        executor.last_order_error = "NO_RESPONSE"
        def expose_old_position_after_ambiguous_open(*_args, **_kwargs):
            executor.open_calls += 1
            executor.positions = [old_position]
            return None
        executor.open_position = expose_old_position_after_ambiguous_open
        runner.executor = executor
        price_row = pd.Series(
            {"Open": 2000.0, "Close": 2000.1, "AskOpen": 2000.15},
            name=signal_bar,
        )
        submit_time = pd.Timestamp("2026-07-01T09:30:02Z")
        info = SimpleNamespace(
            bid=2000.1,
            ask=2000.15,
            quote_time_msc=int(submit_time.timestamp() * 1000),
        )

        with patch.object(live_s23_bot, "utc_now", return_value=submit_time.to_pydatetime()):
            self.assertTrue(
                runner._open_entry(
                    strat,
                    "LONG",
                    price_row,
                    info,
                    execution_time=submit_time,
                    opportunity=opportunity,
                    apply_portfolio_rearm=False,
                    use_confirmed_fill_time=True,
                )
            )
        self.assertFalse(state["basket"])
        self.assertEqual(state["pending_open_opportunity_id"], opportunity_id)
        self.assertEqual(state["sync_block_reason"], "open_confirmation_mismatch")

    def test_session_vwap_restart_does_not_adopt_lot_mismatch(self):
        runner, _za, _state = make_runner(live=True)
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        opportunity_id = "XAUUSD|2026-07-01T09:29:00+00:00|session_vwap_extension_fade|LONG"
        state["pending_open_opportunity_id"] = opportunity_id
        state["pending_open_started_utc"] = "2026-07-01T09:30:01+00:00"
        state["session_vwap_retry_opportunity"] = {
            "opportunity": {"opportunity_id": opportunity_id, "side": "LONG"},
            "signal_bar_time": "2026-07-01T09:29:00+00:00",
            "expires_utc": "2026-07-01T09:32:00+00:00",
            "note": "session_vwap_retry",
        }
        position = SimpleNamespace(
            ticket=9914,
            identifier=8814,
            symbol="XAUUSD",
            magic=int(strat["magic"]),
            comment=strat["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=float(strat["lot"]) * 2.0,
            open_price=2000.15,
            open_time=int(pd.Timestamp("2026-07-01T09:30:02Z").timestamp()),
        )
        executor = CountingExecutor()
        executor.positions = [position]
        runner.executor = executor

        self.assertFalse(runner._sync_strategy(strat))
        self.assertFalse(state["basket"])
        self.assertEqual(state["sync_block_reason"], "live_positions_without_state")
        self.assertEqual(state["pending_open_opportunity_id"], opportunity_id)

    def test_session_vwap_restart_does_not_adopt_old_position_from_same_lane(self):
        runner, _za, _state = make_runner(live=True)
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        opportunity_id = "XAUUSD|2026-07-01T09:29:00+00:00|session_vwap_extension_fade|LONG"
        state["pending_open_opportunity_id"] = opportunity_id
        state["pending_open_started_utc"] = "2026-07-01T09:30:01+00:00"
        state["session_vwap_retry_opportunity"] = {
            "opportunity": {"opportunity_id": opportunity_id, "side": "LONG"},
            "signal_bar_time": "2026-07-01T09:29:00+00:00",
            "expires_utc": "2026-07-01T09:32:00+00:00",
            "note": "session_vwap_retry",
        }
        position = SimpleNamespace(
            ticket=9915,
            identifier=8815,
            symbol="XAUUSD",
            magic=int(strat["magic"]),
            comment=strat["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=float(strat["lot"]),
            open_price=2000.15,
            open_time=int(pd.Timestamp("2026-07-01T08:30:02Z").timestamp()),
        )
        executor = CountingExecutor()
        executor.positions = [position]
        runner.executor = executor

        self.assertFalse(runner._sync_strategy(strat))
        self.assertFalse(state["basket"])
        self.assertEqual(state["sync_block_reason"], "live_positions_without_state")
        self.assertEqual(state["pending_open_opportunity_id"], opportunity_id)

    def test_session_vwap_restart_adoption_retains_unrelated_policy_block(self):
        runner, _za, _state = make_runner(live=True)
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        opportunity_id = "XAUUSD|2026-07-01T09:29:00+00:00|session_vwap_extension_fade|LONG"
        state.update({
            "pending_open_opportunity_id": opportunity_id,
            "pending_open_started_utc": "2026-07-01T09:30:01+00:00",
            "pending_open_expires_utc": "2026-07-01T09:32:00+00:00",
            "pending_open_side": "LONG", "pending_open_lot": float(strat["lot"]),
            "pending_open_symbol": "XAUUSD", "pending_open_magic": int(strat["magic"]),
            "pending_open_comment": str(strat["comment_prefix"]),
            "pending_open_signal_bar": "2026-07-01T09:29:00+00:00",
            "pending_open_reverse_used": False, "pending_open_expected_positions": 0,
            "session_vwap_retry_opportunity": {
                "opportunity": {
                    "opportunity_id": opportunity_id,
                    "source": "session_vwap_extension_fade",
                    "side": "LONG",
                    "raw_side": "LONG",
                    "effective_side": "LONG",
                    "event_time": "2026-07-01T09:29:00+00:00",
                    "release_time": "2026-07-01T09:30:00+00:00",
                    "available_time": "2026-07-01T09:30:00+00:00",
                },
                "signal_bar_time": "2026-07-01T09:29:00+00:00",
                "expires_utc": "2026-07-01T09:32:00+00:00",
                "note": "session_vwap_retry",
            },
            "sync_block_new_entries": True,
            "sync_block_reason": "session_vwap_policy_identity_mismatch",
            "sync_block_recoverable": False,
            "sync_block_details": {"observed_policy_id": "old-policy"},
        })
        position = SimpleNamespace(
            ticket=9917,
            identifier=8817,
            symbol="XAUUSD",
            magic=int(strat["magic"]),
            comment=strat["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=float(strat["lot"]),
            open_price=2000.15,
            open_time=int(pd.Timestamp("2026-07-01T09:30:02Z").timestamp()),
        )
        executor = CountingExecutor()
        executor.positions = [position]
        runner.executor = executor

        self.assertTrue(runner._sync_strategy(strat), "exact owned exposure must remain exit-monitorable")
        self.assertEqual(len(state["basket"]), 1)
        self.assertTrue(state["sync_block_new_entries"])
        self.assertEqual(state["sync_block_reason"], "session_vwap_policy_identity_mismatch")
        self.assertFalse(state["sync_block_recoverable"])

        executor.positions = []
        executor.close_deal = SimpleNamespace(
            position_id=8817,
            symbol="XAUUSD",
            magic=int(strat["magic"]),
            net_profit=1.0,
            price=2000.25,
            deal=77117,
            exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-07-01T09:47:03Z").timestamp()),
        )
        state["pending_close_reason"] = "session_vwap_fixed_hold"
        state["pending_close_signal_bar"] = "2026-07-01T09:47:00+00:00"

        self.assertFalse(
            runner._sync_strategy(strat),
            "flat close confirmation must not convert an unrelated policy block into entry readiness",
        )
        self.assertFalse(state["basket"])
        self.assertTrue(state["sync_block_new_entries"])
        self.assertEqual(state["sync_block_reason"], "session_vwap_policy_identity_mismatch")

    def test_midday_ownership_namespace_is_exact_and_disjoint(self):
        runner, _strategy, _state = make_runner(live=False)
        params = runner.params
        midday = params["midday_session_strategies"]
        self.assertEqual(tuple(row["magic"] for row in midday), EXPECTED_MIDDAY_MAGICS)
        self.assertEqual(params["midday_session_policy_id"], EXPECTED_MIDDAY_POLICY_ID)
        self.assertEqual([row["lane_id"] for row in midday], [8])
        self.assertEqual([row["hold_minutes"] for row in midday], [60])
        self.assertTrue(set(params["expected_magics"]).isdisjoint(params["expected_midday_magics"]))
        self.assertTrue(set(params["expected_morning_magics"]).isdisjoint(params["expected_midday_magics"]))
        self.assertIsNone(runner._ownership_namespace_error())

    def test_pre_midday_state_migration_preserves_za_and_morning_and_adds_empty_lane(self):
        params = json.loads(json.dumps(load_params()))
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            seed_runner = S23HorizontalInventoryRunner(params)
        state = seed_runner._default_state()
        za = params["strategies"][0]
        morning = params["morning_session_strategies"][0]
        midday = params["midday_session_strategies"][0]
        state["strategies"][za["id"]]["basket"] = [{"ticket": 12345}]
        state["strategies"][morning["id"]]["basket"] = [{"ticket": 22345}]
        state["strategies"][za["id"]]["basket_sequence"] = 1
        state["strategies"][za["id"]]["current_basket_id"] = "L1-B000001"
        state["strategies"][morning["id"]]["basket_sequence"] = 1
        state["strategies"][morning["id"]]["current_basket_id"] = "L5-B000001"
        state["strategies"].pop(midday["id"])
        state["routing"].pop("midday_policy_id")
        state["routing"].pop("midday_policy_params_hash")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(state, handle)
            state_path = handle.name
        try:
            with patch.object(live_s23_bot, "STATE_FILE", state_path):
                migrated = S23HorizontalInventoryRunner(params)
            self.assertEqual(migrated._st(za)["basket"], [{"ticket": 12345}])
            self.assertEqual(migrated._st(morning)["basket"], [{"ticket": 22345}])
            self.assertEqual(migrated._st(midday)["basket"], [])
            self.assertTrue(migrated._midday_session_state_migrated)
        finally:
            os.unlink(state_path)

    def test_morning_ownership_namespace_is_exact_and_disjoint(self):
        runner, _strategy, _state = make_runner(live=False)
        params = runner.params
        morning = params["morning_session_strategies"]
        self.assertEqual(tuple(row["magic"] for row in morning), EXPECTED_MORNING_MAGICS)
        self.assertEqual(params["morning_session_policy_id"], EXPECTED_MORNING_POLICY_ID)
        self.assertTrue(set(params["expected_magics"]).isdisjoint(params["expected_morning_magics"]))
        self.assertEqual([row["hold_minutes"] for row in morning], [15, 55, 45])
        self.assertIsNone(runner._ownership_namespace_error())

    def test_old_state_migration_adds_empty_morning_lanes_without_touching_za(self):
        params = json.loads(json.dumps(load_params()))
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            seed_runner = S23HorizontalInventoryRunner(params)
        state = seed_runner._default_state()
        za = params["strategies"][0]
        state["strategies"][za["id"]]["basket"] = [{"ticket": 12345}]
        state["strategies"][za["id"]]["basket_sequence"] = 1
        state["strategies"][za["id"]]["current_basket_id"] = "L1-B000001"
        state["strategies"][za["id"]]["pending_entry_side"] = "SHORT"
        for row in params["morning_session_strategies"]:
            state["strategies"].pop(row["id"])
        state["routing"].pop("morning_policy_id")
        state["routing"].pop("morning_policy_params_hash")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(state, handle)
            state_path = handle.name
        try:
            with patch.object(live_s23_bot, "STATE_FILE", state_path):
                migrated = S23HorizontalInventoryRunner(params)
            self.assertEqual(migrated._st(za)["basket"], [{"ticket": 12345}])
            self.assertEqual(migrated._st(za)["pending_entry_side"], "SHORT")
            self.assertTrue(migrated._morning_session_state_migrated)
            for row in params["morning_session_strategies"]:
                self.assertEqual(migrated._st(row)["basket"], [])
        finally:
            os.unlink(state_path)

    @staticmethod
    def _bars(index: pd.DatetimeIndex, close: list[float] | None = None) -> pd.DataFrame:
        values = close or [100.0] * len(index)
        return pd.DataFrame(
            {
                "Open": values,
                "High": [value + 1.0 for value in values],
                "Low": [value - 1.0 for value in values],
                "Close": values,
                "AskOpen": [value + 0.03 for value in values],
                "Volume": [1.0] * len(index),
            },
            index=index,
        )

    def test_false_break_direction_control_signal(self):
        index = pd.date_range("2026-08-28 00:00", periods=62, freq="1min", tz="UTC")
        bars = self._bars(index)
        bars.loc[index[:60], ["High", "Low"]] = [101.0, 99.0]
        bars.loc[index[60], ["Open", "High", "Low", "Close"]] = [100.0, 102.0, 99.5, 100.5]
        bars.loc[index[61], ["Open", "High", "Low", "Close"]] = [100.5, 100.6, 99.0, 99.5]
        sides = S23HorizontalInventoryRunner._morning_signal_sides(bars)
        self.assertEqual(sides["jst09_range_false_break_confirm_direction_control"], "LONG")

    def test_midday_round_sweep_long_and_short_match_fixed_definition(self):
        runner, _za, _state = make_runner(live=False)
        strat = runner.params["midday_session_strategies"][0]
        index = pd.date_range("2026-08-28 01:00", periods=62, freq="1min", tz="UTC")
        long_bars = self._bars(index)
        long_bars.loc[index[-1], ["Open", "High", "Low", "Close"]] = [100.0, 100.3, 99.8, 100.1]
        self.assertEqual(runner._midday_signal_side(long_bars, strat), "LONG")
        short_bars = self._bars(index)
        short_bars.loc[index[-1], ["Open", "High", "Low", "Close"]] = [100.0, 100.2, 99.7, 99.9]
        self.assertEqual(runner._midday_signal_side(short_bars, strat), "SHORT")

    def test_midday_round_sweep_requires_onset_and_utc02_to04_release(self):
        runner, _za, _state = make_runner(live=False)
        strat = runner.params["midday_session_strategies"][0]
        index = pd.date_range("2026-08-28 01:00", periods=62, freq="1min", tz="UTC")
        bars = self._bars(index)
        bars.loc[index[-2:], ["Open", "High", "Low", "Close"]] = [100.0, 100.3, 99.8, 100.1]
        self.assertIsNone(runner._midday_signal_side(bars, strat))
        outside_index = pd.date_range("2026-08-28 00:57", periods=62, freq="1min", tz="UTC")
        outside = self._bars(outside_index)
        outside.loc[outside_index[-1], ["Open", "High", "Low", "Close"]] = [100.0, 100.3, 99.8, 100.1]
        self.assertIsNone(runner._midday_signal_side(outside, strat))

    def test_midday_session_end_at_utc04_is_exclusive(self):
        runner, _za, _state = make_runner(live=False)
        strat = runner.params["midday_session_strategies"][0]
        index = pd.date_range("2026-08-28 02:58", periods=62, freq="1min", tz="UTC")
        bars = self._bars(index)
        bars.loc[index[-1], ["Open", "High", "Low", "Close"]] = [100.0, 100.3, 99.8, 100.1]
        self.assertEqual(index[-1], pd.Timestamp("2026-08-28 03:59", tz="UTC"))
        self.assertIsNone(runner._midday_signal_side(bars, strat))

    def test_midday_fixed_hold_uses_actual_entry_time_and_is_idempotent(self):
        runner, _za, _state = make_runner(live=False)
        strat = runner.params["midday_session_strategies"][0]
        state = runner._st(strat)
        entered = pd.Timestamp("2026-08-28 02:10:30", tz="UTC")
        state["basket"] = [{"side": "SHORT", "lot": 0.01, "entry_price": 100.0, "entry_time_utc": dt_text(entered)}]
        state["current_basket_id"] = "L8-B000001"
        quote = SimpleNamespace(bid=99.0, ask=99.03)
        self.assertFalse(runner._monitor_midday_position(strat, quote, entered + pd.Timedelta(minutes=59, seconds=59)))
        self.assertTrue(state["basket"])
        self.assertTrue(runner._monitor_midday_position(strat, quote, entered + pd.Timedelta(minutes=60)))
        self.assertFalse(state["basket"])
        state["pending_close_reason"] = "midday_fixed_hold"
        self.assertTrue(runner._monitor_midday_position(strat, quote, entered + pd.Timedelta(minutes=61)))

    def test_fixed_hold_ignores_pre_deadline_and_duplicate_broker_quotes(self):
        runner, _za, _state = make_runner(live=False)
        strat = runner.params["midday_session_strategies"][0]
        state = runner._st(strat)
        entered = pd.Timestamp("2026-08-28 02:10:30", tz="UTC")
        due = entered + pd.Timedelta(minutes=60)
        state["basket"] = [{"side": "SHORT", "lot": 0.01, "entry_price": 100.0, "entry_time_utc": dt_text(entered)}]
        state["current_basket_id"] = "L8-B000001"
        old_quote = SimpleNamespace(bid=99.0, ask=99.03, quote_time_msc=int((due - pd.Timedelta(milliseconds=1)).timestamp() * 1000))
        self.assertTrue(runner._monitor_midday_position(strat, old_quote, due + pd.Timedelta(seconds=5)))
        self.assertTrue(state["basket"])
        wide_time = due + pd.Timedelta(seconds=6)
        wide = SimpleNamespace(bid=99.0, ask=99.5, quote_time_msc=int(wide_time.timestamp() * 1000))
        self.assertTrue(runner._monitor_midday_position(strat, wide, wide_time))
        self.assertEqual(state["time_close_stable_count"], 0)
        self.assertTrue(runner._monitor_midday_position(strat, wide, wide_time + pd.Timedelta(seconds=5)))
        self.assertEqual(state["time_close_stable_count"], 0)

    def test_midday_fixed_hold_uses_broker_quote_when_host_clock_lags(self):
        runner, _za, _state = make_runner(live=False)
        strat = runner.params["midday_session_strategies"][0]
        state = runner._st(strat)
        entered = pd.Timestamp("2026-08-28 02:10:30", tz="UTC")
        due = entered + pd.Timedelta(minutes=60)
        state["basket"] = [{
            "side": "SHORT", "lot": 0.01, "entry_price": 100.0,
            "entry_time_utc": dt_text(entered),
        }]
        state["current_basket_id"] = "L8-B000001"
        quote = SimpleNamespace(
            bid=99.0,
            ask=99.03,
            quote_time_msc=int(due.timestamp() * 1000),
        )
        host_time = due - pd.Timedelta(seconds=30)
        self.assertTrue(runner._monitor_midday_position(strat, quote, host_time))
        self.assertFalse(state["basket"])

    def test_fixed_hold_waits_for_three_fresh_narrow_quotes_after_wide_spread(self):
        runner, _za, _state = make_runner(live=False)
        strat = runner.params["midday_session_strategies"][0]
        state = runner._st(strat)
        entered = pd.Timestamp("2026-08-28 02:10:30", tz="UTC")
        due = entered + pd.Timedelta(minutes=60)
        state["basket"] = [{"side": "LONG", "lot": 0.01, "entry_price": 100.0, "entry_time_utc": dt_text(entered)}]
        state["current_basket_id"] = "L8-B000001"
        wide = SimpleNamespace(bid=100.0, ask=100.5, quote_time_msc=int(due.timestamp() * 1000))
        runner._monitor_midday_position(strat, wide, due)
        for count in (1, 2):
            quote_time = due + pd.Timedelta(seconds=count)
            narrow = SimpleNamespace(bid=100.1, ask=100.13, quote_time_msc=int(quote_time.timestamp() * 1000))
            self.assertTrue(runner._monitor_midday_position(strat, narrow, quote_time))
            self.assertTrue(state["basket"])
            self.assertEqual(state["time_close_stable_count"], count)
        quote_time = due + pd.Timedelta(seconds=3)
        narrow = SimpleNamespace(bid=100.1, ask=100.13, quote_time_msc=int(quote_time.timestamp() * 1000))
        self.assertTrue(runner._monitor_midday_position(strat, narrow, quote_time))
        self.assertFalse(state["basket"])

    def test_malformed_fixed_hold_close_state_cannot_stop_or_delay_due_exit(self):
        entered = pd.Timestamp("2026-08-28 02:10:30", tz="UTC")
        due = entered + pd.Timedelta(minutes=60)
        corruptions = (
            {"time_close_last_quote_msc": "not-an-integer"},
            {"time_close_last_quote_msc": str(int((due + pd.Timedelta(hours=1)).timestamp() * 1000))},
            {"time_close_last_quote_msc": int((due + pd.Timedelta(hours=1)).timestamp() * 1000)},
            {"time_close_retry_after_utc": int((due + pd.Timedelta(hours=1)).value)},
            {"time_close_retry_after_utc": dt_text(due + pd.Timedelta(hours=1))},
            {
                "time_close_wide_seen": True,
                "time_close_defer_started_utc": dt_text(due),
                "time_close_stable_count": "not-an-integer",
            },
            {
                "time_close_wide_seen": True,
                "time_close_defer_started_utc": int((due + pd.Timedelta(hours=1)).value),
            },
            {
                "time_close_wide_seen": True,
                "time_close_defer_started_utc": dt_text(due + pd.Timedelta(hours=1)),
            },
            {"time_close_wide_seen": "not-a-boolean"},
            {
                "time_close_wide_seen": True,
                "time_close_defer_started_utc": "not-a-timestamp",
            },
        )
        for corruption in corruptions:
            with self.subTest(corruption=corruption):
                runner, _za, _state = make_runner(live=False)
                strat = runner.params["midday_session_strategies"][0]
                state = runner._st(strat)
                state["basket"] = [{
                    "side": "LONG", "lot": 0.01, "entry_price": 100.0,
                    "entry_time_utc": dt_text(entered),
                }]
                state["current_basket_id"] = "L8-B000001"
                state.update(corruption)
                quote = SimpleNamespace(
                    bid=100.1,
                    ask=100.13,
                    quote_time_msc=int(due.timestamp() * 1000),
                )

                self.assertTrue(runner._monitor_midday_position(strat, quote, due))
                self.assertFalse(state["basket"])

    def test_malformed_defer_state_preserves_valid_market_closed_retry_cooldown(self):
        runner, _za, _state = make_runner(live=False)
        strat = runner.params["midday_session_strategies"][0]
        state = runner._st(strat)
        entered = pd.Timestamp("2026-08-28 02:10:30", tz="UTC")
        due = entered + pd.Timedelta(minutes=60)
        retry_after = due + pd.Timedelta(seconds=60)
        state["basket"] = [{
            "side": "LONG", "lot": 0.01, "entry_price": 100.0,
            "entry_time_utc": dt_text(entered),
        }]
        state["current_basket_id"] = "L8-B000001"
        state["time_close_retry_after_utc"] = dt_text(retry_after)
        state["time_close_stable_count"] = "not-an-integer"
        quote_time = due + pd.Timedelta(seconds=30)
        quote = SimpleNamespace(
            bid=100.1,
            ask=100.13,
            quote_time_msc=int(quote_time.timestamp() * 1000),
        )

        self.assertTrue(runner._monitor_midday_position(strat, quote, quote_time))
        self.assertTrue(state["basket"])
        self.assertEqual(
            parse_ts(state["time_close_retry_after_utc"]), retry_after,
        )

    def test_market_closed_10018_retries_after_sixty_seconds_without_permanent_block(self):
        runner, _za, _state = make_runner(live=True)
        strat = runner.params["midday_session_strategies"][0]
        state = runner._st(strat)
        entered = pd.Timestamp("2026-08-28 02:10:30", tz="UTC")
        due = entered + pd.Timedelta(minutes=60)
        ticket = 9801
        state["basket"] = [{
            "ticket": ticket, "position_identifier": ticket, "side": "LONG", "lot": 0.01,
            "entry_price": 100.0, "entry_time_utc": dt_text(entered), "open_time_epoch": int(entered.timestamp()),
            "owner_symbol": "XAUUSD", "owner_magic": int(strat["magic"]), "owner_comment": strat["comment_prefix"],
        }]
        bind_owned_basket_identity(strat, state)
        position = SimpleNamespace(ticket=ticket, identifier=ticket, symbol="XAUUSD", magic=int(strat["magic"]), comment=strat["comment_prefix"], type=ORDER_TYPE_BUY, volume=0.01)
        executor = CountingExecutor()
        executor.positions = [position]
        results = [
            live_executor.CloseResult(False, "MARKET_CLOSED", retcode=10018),
            live_executor.CloseResult(True, "CONFIRMED", deal_id=8801, retcode=10009),
        ]
        executor.close_position = lambda ticket, _deviation, **_kwargs: (executor.close_calls.append(int(ticket)) or results.pop(0))
        runner.executor = executor
        first = SimpleNamespace(bid=100.0, ask=100.03, quote_time_msc=int(due.timestamp() * 1000))
        self.assertTrue(runner._monitor_midday_position(strat, first, due))
        self.assertEqual(executor.close_calls, [ticket])
        self.assertFalse(state["sync_block_new_entries"])
        second_time = due + pd.Timedelta(seconds=30)
        second = SimpleNamespace(bid=100.0, ask=100.03, quote_time_msc=int(second_time.timestamp() * 1000))
        self.assertTrue(runner._monitor_midday_position(strat, second, second_time))
        self.assertEqual(executor.close_calls, [ticket])
        third_time = due + pd.Timedelta(seconds=61)
        third = SimpleNamespace(bid=100.0, ask=100.03, quote_time_msc=int(third_time.timestamp() * 1000))
        self.assertTrue(runner._monitor_midday_position(strat, third, third_time))
        self.assertEqual(executor.close_calls, [ticket, ticket])
        self.assertEqual(state["pending_close_reason"], "midday_fixed_hold")
        self.assertFalse(state["sync_block_new_entries"])

    def test_session_vwap_market_closed_10018_uses_fixed_hold_retry_path(self):
        runner, _za, _state = make_runner(live=True)
        strat = runner.params["session_vwap_strategies"][0]
        state = runner._st(strat)
        entered = pd.Timestamp("2026-08-28 09:30:30", tz="UTC")
        due = entered + pd.Timedelta(minutes=15)
        ticket = 9813
        state["basket"] = [{
            "ticket": ticket, "position_identifier": ticket, "side": "LONG", "lot": 0.01,
            "entry_price": 100.0, "entry_time_utc": dt_text(entered),
            "open_time_epoch": int(entered.timestamp()), "owner_symbol": "XAUUSD",
            "owner_magic": int(strat["magic"]), "owner_comment": strat["comment_prefix"],
        }]
        bind_owned_basket_identity(strat, state)
        position = SimpleNamespace(
            ticket=ticket, identifier=ticket, symbol="XAUUSD", magic=int(strat["magic"]),
            comment=strat["comment_prefix"], type=ORDER_TYPE_BUY, volume=0.01,
        )
        executor = CountingExecutor()
        executor.positions = [position]
        executor.close_position = lambda ticket, _deviation, **_kwargs: (
            executor.close_calls.append(int(ticket))
            or live_executor.CloseResult(False, "MARKET_CLOSED", retcode=10018)
        )
        runner.executor = executor
        quote = SimpleNamespace(
            bid=100.0, ask=100.03, quote_time_msc=int(due.timestamp() * 1000),
        )

        self.assertTrue(runner._monitor_session_vwap_position(strat, quote, due))
        self.assertEqual(executor.close_calls, [ticket])
        self.assertFalse(state["sync_block_new_entries"])
        self.assertIsNone(state["pending_close_reason"])
        self.assertEqual(
            state["time_close_retry_after_utc"],
            dt_text(due + pd.Timedelta(seconds=60)),
        )

    def test_midday_capacity_one_blocks_second_position(self):
        runner, _za, _state = make_runner(live=False)
        runner.params["midday_session_enabled"] = True
        strat = runner.params["midday_session_strategies"][0]
        signal_bar = pd.Timestamp("2026-08-28 02:10", tz="UTC")
        bars = self._bars(pd.date_range(signal_bar - pd.Timedelta(minutes=99), signal_bar, freq="1min", tz="UTC"))
        quote = SimpleNamespace(bid=100.0, ask=100.03)
        with patch.object(runner, "_midday_signal_side", return_value="LONG"), patch.object(
            live_s23_bot, "stale_signal_decision", return_value=SimpleNamespace(stale=False)
        ):
            runner._process_midday_entries(bars, bars.iloc[-1], quote, signal_bar + pd.Timedelta(minutes=1), {8: True})
            next_bars = pd.concat([bars, self._bars(pd.DatetimeIndex([signal_bar + pd.Timedelta(minutes=1)]))])
            runner._process_midday_entries(next_bars, next_bars.iloc[-1], quote, signal_bar + pd.Timedelta(minutes=2), {8: True})
        self.assertEqual(len(runner._st(strat)["basket"]), 1)

    def test_midday_opportunity_is_passively_observed_and_tagged_before_routing(self):
        runner, _za, _state = make_runner(live=False)
        runner.params["midday_session_enabled"] = True
        strat = runner.params["midday_session_strategies"][0]
        observer = RecordingObserver()
        tag_calls = []
        runner.midday_shadow_observer = observer
        runner.midday_shadow_state_tagger = SimpleNamespace(
            enabled=True,
            tag_opportunity=lambda **kwargs: tag_calls.append(dict(kwargs)),
        )
        signal_bar = pd.Timestamp("2026-08-28 02:10", tz="UTC")
        bars = self._bars(pd.date_range(signal_bar - pd.Timedelta(minutes=99), signal_bar, freq="1min", tz="UTC"))
        quote = SimpleNamespace(bid=100.0, ask=100.03)
        with patch.object(runner, "_midday_signal_side", return_value="LONG"), patch.object(
            live_s23_bot, "stale_signal_decision", return_value=SimpleNamespace(stale=False)
        ):
            runner._process_midday_entries(bars, bars.iloc[-1], quote, signal_bar + pd.Timedelta(minutes=1), {8: True})
        self.assertEqual([method for method, _kwargs in observer.calls], ["register_opportunity", "record_route"])
        registered = observer.calls[0][1]["opportunity"]
        self.assertEqual(registered["source"], "jst1113_round_sweep")
        self.assertEqual(registered["effective_side"], "LONG")
        self.assertEqual(observer.calls[1][1]["status"], "consumed")
        self.assertEqual(len(tag_calls), 1)

    def test_midday_master_switch_blocks_orders_but_keeps_shadow_evidence(self):
        runner, _za, _state = make_runner(live=False)
        self.assertFalse(runner.params["midday_session_enabled"])
        strat = runner.params["midday_session_strategies"][0]
        observer = RecordingObserver()
        tag_calls = []
        runner.midday_shadow_observer = observer
        runner.midday_shadow_state_tagger = SimpleNamespace(
            enabled=True,
            tag_opportunity=lambda **kwargs: tag_calls.append(dict(kwargs)),
        )
        signal_bar = pd.Timestamp("2026-08-28 02:10", tz="UTC")
        bars = self._bars(pd.date_range(signal_bar - pd.Timedelta(minutes=99), signal_bar, freq="1min", tz="UTC"))
        quote = SimpleNamespace(bid=100.0, ask=100.03)
        with patch.object(runner, "_midday_signal_side", return_value="LONG"), patch.object(
            live_s23_bot, "stale_signal_decision", return_value=SimpleNamespace(stale=False)
        ):
            runner._process_midday_entries(
                bars, bars.iloc[-1], quote,
                signal_bar + pd.Timedelta(minutes=1), {8: True},
            )
        self.assertFalse(runner._st(strat)["basket"])
        self.assertEqual(len(tag_calls), 1)
        self.assertEqual([method for method, _kwargs in observer.calls], ["register_opportunity", "record_route"])
        self.assertEqual(observer.calls[1][1]["status"], "unconsumed")
        self.assertEqual(observer.calls[1][1]["reason"], "midday_session_disabled")

    def test_midday_master_switch_preserves_owned_exit_monitoring(self):
        runner, _za, _state = make_runner(live=False)
        self.assertFalse(runner.params["midday_session_enabled"])
        strat = runner.params["midday_session_strategies"][0]
        state = runner._st(strat)
        state["basket"] = [{
            "side": "LONG", "lot": 0.01, "entry_price": 100.0,
            "entry_time_utc": "2026-08-28T02:10:00+00:00",
        }]
        quote = SimpleNamespace(bid=99.0, ask=99.03)
        poll_time = pd.Timestamp("2026-08-28 03:10", tz="UTC")
        with patch.object(runner, "_sync_strategy", return_value=True), patch.object(
            runner, "_monitor_midday_position", return_value=True,
        ) as monitor:
            readiness = runner._process_midday_exits(quote, poll_time)
        monitor.assert_called_once_with(strat, quote, poll_time)
        self.assertFalse(readiness[8])

    def test_price_effort_direction_control_signal(self):
        index = pd.date_range("2026-08-28 00:10", periods=20, freq="1min", tz="UTC")
        closes = [102.0 - 0.1 * i for i in range(20)]
        bars = self._bars(index, closes)
        bars["High"] = 103.0
        bars.iloc[-1, bars.columns.get_loc("High")] = 104.0
        bars.iloc[-1, bars.columns.get_loc("Close")] = 99.0
        sides = S23HorizontalInventoryRunner._morning_signal_sides(bars)
        self.assertEqual(sides["price_effort_divergence_edge_direction_control"], "LONG")

    def test_compression_release_is_primary_and_completion_aligned(self):
        index = pd.date_range("2026-08-27 23:00", "2026-08-28 00:19", freq="1min", tz="UTC")
        bars = self._bars(index)
        for start in pd.date_range("2026-08-27 23:00", periods=4, freq="15min", tz="UTC"):
            mask = (bars.index >= start) & (bars.index < start + pd.Timedelta(minutes=15))
            bars.loc[mask, "High"] = 105.0
            bars.loc[mask, "Low"] = 95.0
        compressed = (bars.index >= pd.Timestamp("2026-08-28 00:00", tz="UTC")) & (bars.index < pd.Timestamp("2026-08-28 00:15", tz="UTC"))
        bars.loc[compressed, "High"] = 101.0
        bars.loc[compressed, "Low"] = 99.0
        release_leg = bars.index >= pd.Timestamp("2026-08-28 00:15", tz="UTC")
        bars.loc[release_leg, ["Open", "High", "Low", "Close"]] = [101.5, 103.0, 101.0, 102.0]
        sides = S23HorizontalInventoryRunner._morning_signal_sides(bars)
        self.assertEqual(sides["m15_compression_m5_edge_release_primary"], "LONG")

    def test_compression_release_does_not_repeat_while_m5_side_is_unchanged(self):
        index = pd.date_range("2026-08-27 23:00", "2026-08-28 00:24", freq="1min", tz="UTC")
        bars = self._bars(index)
        for start in pd.date_range("2026-08-27 23:00", periods=4, freq="15min", tz="UTC"):
            mask = (bars.index >= start) & (bars.index < start + pd.Timedelta(minutes=15))
            bars.loc[mask, "High"] = 105.0
            bars.loc[mask, "Low"] = 95.0
        compressed = (bars.index >= pd.Timestamp("2026-08-28 00:00", tz="UTC")) & (bars.index < pd.Timestamp("2026-08-28 00:15", tz="UTC"))
        bars.loc[compressed, "High"] = 101.0
        bars.loc[compressed, "Low"] = 99.0
        release_leg = bars.index >= pd.Timestamp("2026-08-28 00:15", tz="UTC")
        bars.loc[release_leg, ["Open", "High", "Low", "Close"]] = [101.5, 103.0, 101.0, 102.0]
        sides = S23HorizontalInventoryRunner._morning_signal_sides(bars)
        self.assertIsNone(sides["m15_compression_m5_edge_release_primary"])

    def test_morning_future_m1_does_not_advance_lane_receipt(self):
        runner, _za, _state = make_runner(live=False)
        strat = runner.params["morning_session_strategies"][0]
        signal_bar = pd.Timestamp("2026-08-28T00:10:00Z")
        price_row = pd.Series(
            {"Open": 99.0, "Close": 100.0, "AskOpen": 100.03},
            name=signal_bar,
        )
        with patch.object(
            runner,
            "_morning_signal_sides",
            return_value={str(strat["signal_id"]): "LONG"},
        ):
            runner._process_morning_entries(
                pd.DataFrame([price_row]),
                price_row,
                SimpleNamespace(bid=100.0, ask=100.03),
                pd.Timestamp("2026-08-28T00:10:30Z"),
                {int(strat["lane_id"]): True},
            )

        self.assertIsNone(runner._st(strat)["last_evaluated_bar"])

    def test_morning_fixed_hold_uses_actual_entry_time_and_is_idempotent(self):
        runner, _za, _state = make_runner(live=False)
        strat = runner.params["morning_session_strategies"][0]
        state = runner._st(strat)
        entered = pd.Timestamp("2026-08-28 00:10:30", tz="UTC")
        state["basket"] = [{"side": "LONG", "lot": 0.01, "entry_price": 100.0, "entry_time_utc": dt_text(entered)}]
        state["current_basket_id"] = "L5-B000001"
        quote = SimpleNamespace(bid=101.0, ask=101.03)
        self.assertFalse(runner._monitor_morning_position(strat, quote, entered + pd.Timedelta(minutes=14, seconds=59)))
        self.assertTrue(state["basket"])
        self.assertTrue(runner._monitor_morning_position(strat, quote, entered + pd.Timedelta(minutes=15)))
        self.assertFalse(state["basket"])
        state["pending_close_reason"] = "morning_fixed_hold"
        self.assertTrue(runner._monitor_morning_position(strat, quote, entered + pd.Timedelta(minutes=30)))

    def test_live_morning_entry_uses_confirmed_broker_fill_time(self):
        runner, _za, _state = make_runner(live=True)
        strat = runner.params["morning_session_strategies"][0]
        broker_fill = pd.Timestamp("2026-08-28 00:10:37", tz="UTC")
        position = SimpleNamespace(
            ticket=7701,
            identifier=7701,
            symbol="XAUUSD",
            magic=int(strat["magic"]),
            comment=strat["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=0.01,
            open_price=100.03,
            open_time=int(broker_fill.timestamp()),
        )

        class ConfirmingExecutor(CountingExecutor):
            def open_position(self, *_args, **_kwargs):
                self.open_calls += 1
                self.positions = [position]
                return 7701

        runner.executor = ConfirmingExecutor()
        decision_time = broker_fill - pd.Timedelta(seconds=12)
        row = pd.Series({"Open": 100.0, "Close": 100.0, "AskOpen": 100.03}, name=pd.Timestamp("2026-08-28 00:09", tz="UTC"))
        with patch.object(live_s23_bot, "utc_now", return_value=decision_time.to_pydatetime()):
            self.assertTrue(
                runner._open_entry(
                    strat,
                    "LONG",
                    row,
                    SimpleNamespace(
                        bid=100.0,
                        ask=100.03,
                        quote_time_msc=int(decision_time.timestamp() * 1000),
                    ),
                    execution_time=decision_time,
                    apply_portfolio_rearm=False,
                    use_confirmed_fill_time=True,
                )
            )
        self.assertEqual(parse_ts(runner._st(strat)["basket"][0]["entry_time_utc"]), broker_fill)

    def test_live_open_cannot_fabricate_missing_position_identifier_from_ticket(self):
        runner, _za, _state = make_runner(live=True)
        strat = runner.params["morning_session_strategies"][0]
        decision_time = pd.Timestamp("2026-08-28 00:10:25", tz="UTC")
        position = SimpleNamespace(
            ticket=7702,
            identifier=0,
            symbol="XAUUSD",
            magic=int(strat["magic"]),
            comment=strat["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=0.01,
            open_price=100.03,
            open_time=int(decision_time.timestamp()),
        )

        class MissingIdentifierExecutor(CountingExecutor):
            def open_position(self, *_args, **_kwargs):
                self.open_calls += 1
                self.positions = [position]
                return 7702

        runner.executor = MissingIdentifierExecutor()
        row = pd.Series(
            {"Open": 100.0, "Close": 100.0, "AskOpen": 100.03},
            name=pd.Timestamp("2026-08-28 00:09", tz="UTC"),
        )
        with patch.object(live_s23_bot, "utc_now", return_value=decision_time.to_pydatetime()):
            self.assertTrue(
                runner._open_entry(
                    strat,
                    "LONG",
                    row,
                    SimpleNamespace(
                        bid=100.0,
                        ask=100.03,
                        quote_time_msc=int(decision_time.timestamp() * 1000),
                    ),
                    execution_time=decision_time,
                    apply_portfolio_rearm=False,
                    use_confirmed_fill_time=True,
                )
            )
        state = runner._st(strat)
        self.assertEqual(state["basket"], [])
        self.assertEqual(state["sync_block_reason"], "live_position_identity_invalid")

    def test_live_fixed_hold_never_substitutes_poll_time_for_missing_fill_time(self):
        runner, _za, _state = make_runner(live=True)
        strat = runner.params["morning_session_strategies"][0]
        position = SimpleNamespace(
            ticket=7702,
            identifier=7702,
            symbol="XAUUSD",
            magic=int(strat["magic"]),
            comment=strat["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=0.01,
            open_price=100.03,
            open_time=0,
        )

        class ConfirmingExecutor(CountingExecutor):
            def open_position(self, *_args, **_kwargs):
                self.open_calls += 1
                self.positions = [position]
                return 7702

        runner.executor = ConfirmingExecutor()
        decision_time = pd.Timestamp("2026-08-28 00:10:25", tz="UTC")
        row = pd.Series({"Open": 100.0, "Close": 100.0, "AskOpen": 100.03}, name=pd.Timestamp("2026-08-28 00:09", tz="UTC"))
        with patch.object(live_s23_bot, "utc_now", return_value=decision_time.to_pydatetime()):
            self.assertTrue(
                runner._open_entry(
                    strat,
                    "LONG",
                    row,
                    SimpleNamespace(
                        bid=100.0,
                        ask=100.03,
                        quote_time_msc=int(decision_time.timestamp() * 1000),
                    ),
                    execution_time=decision_time,
                    apply_portfolio_rearm=False,
                    use_confirmed_fill_time=True,
                )
            )
        state = runner._st(strat)
        self.assertIsNone(state["basket"][0]["entry_time_utc"])
        self.assertEqual(state["sync_block_reason"], "confirmed_fill_time_unavailable")
        self.assertTrue(state["sync_block_recoverable"])
        self.assertTrue(
            runner._monitor_morning_position(
                strat,
                SimpleNamespace(bid=100.0, ask=100.03),
                decision_time + pd.Timedelta(minutes=30),
            )
        )
        self.assertEqual(state["sync_block_reason"], "confirmed_fill_time_unavailable")
        self.assertTrue(state["sync_block_recoverable"])

        broker_fill = pd.Timestamp("2026-08-28 00:10:37", tz="UTC")
        position.open_time = int(broker_fill.timestamp())
        self.assertTrue(runner._sync_strategy(strat))
        self.assertEqual(parse_ts(state["basket"][0]["entry_time_utc"]), broker_fill)
        self.assertFalse(state["sync_block_new_entries"])

    def test_live_common_lane_never_substitutes_poll_time_for_missing_fill_time(self):
        runner, strat, state = make_runner(live=True)
        decision_time = pd.Timestamp("2026-08-28 00:10:25", tz="UTC")
        position = SimpleNamespace(
            ticket=7705,
            identifier=7705,
            symbol="XAUUSD",
            magic=int(strat["magic"]),
            comment=strat["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=float(strat["lot"]),
            open_price=100.03,
            open_time=0,
        )

        class ConfirmingExecutor(CountingExecutor):
            def open_position(self, *_args, **_kwargs):
                self.open_calls += 1
                self.positions = [position]
                return 7705

        runner.executor = ConfirmingExecutor()
        row = pd.Series(
            {"Open": 100.0, "Close": 100.0, "AskOpen": 100.03},
            name=pd.Timestamp("2026-08-28 00:09", tz="UTC"),
        )
        with patch.object(live_s23_bot, "utc_now", return_value=decision_time.to_pydatetime()):
            self.assertTrue(
                runner._open_entry(
                    strat,
                    "LONG",
                    row,
                    SimpleNamespace(
                        bid=100.0,
                        ask=100.03,
                        quote_time_msc=int(decision_time.timestamp() * 1000),
                    ),
                    execution_time=decision_time,
                    apply_portfolio_rearm=False,
                    use_confirmed_fill_time=False,
                )
            )

        self.assertIsNone(state["basket"][0]["entry_time_utc"])
        self.assertEqual(state["basket"][0]["open_time_epoch"], 0)
        self.assertEqual(state["sync_block_reason"], "confirmed_fill_time_unavailable")
        self.assertTrue(state["sync_block_recoverable"])

    def test_flat_morning_lanes_skip_reconciliation_outside_session(self):
        runner, _za, _state = make_runner(live=True)
        executor = CountingExecutor()
        calls = {"positions": 0, "orders": 0, "save": 0}
        original_positions = executor.get_positions
        original_orders = executor.get_orders
        executor.get_positions = lambda *args: (calls.__setitem__("positions", calls["positions"] + 1) or original_positions(*args))
        executor.get_orders = lambda *args: (calls.__setitem__("orders", calls["orders"] + 1) or original_orders(*args))
        runner.executor = executor
        runner._save_state = lambda: calls.__setitem__("save", calls["save"] + 1)
        readiness = runner._process_morning_exits(SimpleNamespace(bid=100.0, ask=100.03), pd.Timestamp("2026-08-28 03:00", tz="UTC"))
        self.assertEqual(readiness, {5: False, 6: False, 7: False})
        self.assertEqual(calls, {"positions": 0, "orders": 0, "save": 0})

    def test_run_once_uses_separate_quote_and_post_hist_decision_clocks(self):
        runner, _za, _state = make_runner(live=False)
        quote_time = pd.Timestamp("2026-08-28 00:30:01", tz="UTC")
        decision_time = pd.Timestamp("2026-08-28 00:30:08", tz="UTC")
        bars = self._bars(pd.date_range("2026-08-27 22:51", periods=100, freq="1min", tz="UTC"))
        captured: dict[str, pd.Timestamp] = {}

        class QuoteExecutor(CountingExecutor):
            def get_symbol_info(self, _symbol):
                return SimpleNamespace(bid=100.0, ask=100.03)

        runner.executor = QuoteExecutor()
        with patch.object(live_s23_bot, "utc_now", side_effect=[quote_time.to_pydatetime(), decision_time.to_pydatetime()]), patch.object(
            runner, "_observer_call"
        ), patch.object(
            runner, "_process_morning_exits", side_effect=lambda _info, stamp: captured.setdefault("quote", stamp) or {}
        ), patch.object(
            runner, "_get_m1", return_value=bars
        ), patch.object(
            runner, "_process_morning_entries", side_effect=lambda _bars, _row, _info, stamp, _ready: captured.setdefault("decision", stamp)
        ), patch.object(
            runner, "_advance_inventory_range_fade"
        ), patch.object(
            runner, "_sync_strategy", return_value=False
        ), patch.object(
            runner, "_signal", return_value=None
        ), patch.object(
            runner, "_take_inventory_range_fade_opportunity", return_value=None
        ):
            runner.run_once()
        self.assertEqual(captured["quote"], quote_time)
        self.assertEqual(captured["decision"], decision_time)

    def test_three_morning_signals_fill_only_three_independent_lanes(self):
        runner, _za, _state = make_runner(live=False)
        signal_bar = pd.Timestamp("2026-08-28 00:30", tz="UTC")
        bars = self._bars(pd.date_range(signal_bar - pd.Timedelta(minutes=99), signal_bar, freq="1min", tz="UTC"))
        price_row = bars.iloc[-1]
        quote = SimpleNamespace(bid=100.0, ask=100.03)
        sides = {row["signal_id"]: "LONG" for row in runner.params["morning_session_strategies"]}
        with patch.object(runner, "_morning_signal_sides", return_value=sides), patch.object(
            live_s23_bot, "stale_signal_decision", return_value=SimpleNamespace(stale=False)
        ):
            runner._process_morning_entries(bars, price_row, quote, signal_bar + pd.Timedelta(minutes=1), {5: True, 6: True, 7: True})
        self.assertEqual([len(runner._st(row)["basket"]) for row in runner.params["morning_session_strategies"]], [1, 1, 1])

    def test_foreign_magic_is_not_adopted_by_morning_lane(self):
        runner, _za, _state = make_runner(live=True)
        strat = runner.params["morning_session_strategies"][0]
        executor = CountingExecutor()
        executor.positions = [SimpleNamespace(ticket=9, identifier=9, symbol="XAUUSD", magic=999999, comment="foreign")]
        runner.executor = executor
        self.assertTrue(runner._sync_strategy(strat))
        self.assertFalse(runner._st(strat)["basket"])

    def test_foreign_magic_is_not_adopted_by_midday_lane(self):
        runner, _za, _state = make_runner(live=True)
        strat = runner.params["midday_session_strategies"][0]
        executor = CountingExecutor()
        executor.positions = [SimpleNamespace(ticket=10, identifier=10, symbol="XAUUSD", magic=999999, comment="foreign")]
        runner.executor = executor
        self.assertTrue(runner._sync_strategy(strat))
        self.assertFalse(runner._st(strat)["basket"])

    def test_inventory_range_return_after_fifteen_minutes_is_rejected(self):
        runner, _strategy, state = make_runner(live=False)
        second = runner.params["strategies"][1]
        state["basket"] = [{"side": "LONG", "entry_price": 100.0}]
        runner._st(second)["basket"] = [{"side": "SHORT", "entry_price": 101.0}]
        runner._advance_inventory_range_fade(
            pd.Series({"Close": 100.5}, name=pd.Timestamp("2026-08-25 13:00:00", tz="UTC"))
        )
        runner._advance_inventory_range_fade(
            pd.Series({"Close": 101.1}, name=pd.Timestamp("2026-08-25 13:01:00", tz="UTC"))
        )
        runner._advance_inventory_range_fade(
            pd.Series({"Close": 100.9}, name=pd.Timestamp("2026-08-25 13:17:00", tz="UTC"))
        )
        range_state = runner.state["routing"]["inventory_range_fade"]
        self.assertEqual(range_state["break_phase"], 0)
        self.assertIsNone(range_state["pending_side"])

    def test_malformed_inventory_range_state_is_cleared_without_order_path(self):
        runner, _strategy, _state = make_runner(live=False)
        range_state = runner.state["routing"]["inventory_range_fade"]
        range_state.update({"active": True, "low": "bad", "high": 101.0, "break_phase": "bad"})
        runner._advance_inventory_range_fade(
            pd.Series({"Close": 100.5}, name=pd.Timestamp("2026-08-25 13:00:00", tz="UTC"))
        )
        reset_state = runner.state["routing"]["inventory_range_fade"]
        self.assertFalse(reset_state["active"])
        self.assertEqual(reset_state["break_phase"], 0)
        self.assertIsNone(reset_state["pending_side"])
        self.assertTrue(all(not runner._st(strategy)["basket"] for strategy in runner.params["strategies"]))

    def test_range_state_rejects_boolean_identity_coercion(self):
        corruptions = (
            {"active": "false", "low": 100.0, "high": 101.0},
            {"active": True, "low": "100.0", "high": 101.0},
            {
                "active": False,
                "pending_side": "LONG",
                "pending_origin_bar": None,
                "pending_break_side": None,
            },
            {
                "active": True,
                "low": 100.0,
                "high": 101.0,
                "break_phase": True,
                "break_side": "LONG",
                "break_time_utc": "2026-08-25T12:59:00Z",
            },
            {
                "active": True,
                "low": 100.0,
                "high": 101.0,
                "return_confirm_count": True,
            },
        )
        for corruption in corruptions:
            with self.subTest(corruption=corruption):
                runner, _strategy, _state = make_runner(live=False)
                events = []
                runner._trade_row = lambda event, _strat, **fields: events.append(
                    (event, fields)
                )
                runner.state["routing"]["inventory_range_fade"].update(corruption)

                runner._advance_inventory_range_fade(
                    pd.Series(
                        {"Close": 100.5},
                        name=pd.Timestamp("2026-08-25 13:00:00", tz="UTC"),
                    )
                )

                self.assertTrue(any(
                    event == "inventory_range_invalidated"
                    and fields.get("reason") == "malformed_persisted_range_state"
                    for event, fields in events
                ))

    def test_range_receipt_rejects_malformed_and_preserves_future_high_watermark(self):
        current_bar = pd.Timestamp("2026-08-25 13:00:00", tz="UTC")
        for receipt, expected_reason in (
            (0, "malformed_persisted_range_state"),
            (dt_text(current_bar + pd.Timedelta(minutes=5)), "decision_receipt_nonmonotonic"),
        ):
            with self.subTest(receipt=receipt):
                runner, _strategy, _state = make_runner(live=False)
                range_state = runner.state["routing"]["inventory_range_fade"]
                range_state.update({
                    "last_state_bar": receipt,
                    "active": True,
                    "low": 100.0,
                    "high": 101.0,
                })
                events = []
                runner._trade_row = lambda event, _strat, **fields: events.append(
                    (event, fields)
                )

                runner._advance_inventory_range_fade(
                    pd.Series({"Close": 100.5}, name=current_bar)
                )

                self.assertTrue(any(
                    event == "inventory_range_invalidated"
                    and fields.get("reason") == expected_reason
                    for event, fields in events
                ))
                if isinstance(receipt, str):
                    self.assertEqual(range_state["last_state_bar"], receipt)
                    self.assertTrue(range_state["active"])
                else:
                    self.assertFalse(
                        runner.state["routing"]["inventory_range_fade"]["active"]
                    )

    def test_range_future_break_time_cannot_create_earlier_opportunity(self):
        runner, _strategy, state = make_runner(live=False)
        second = runner.params["strategies"][1]
        state["basket"] = [{"side": "LONG", "entry_price": 100.0}]
        runner._st(second)["basket"] = [{"side": "SHORT", "entry_price": 101.0}]
        range_state = runner.state["routing"]["inventory_range_fade"]
        range_state.update({
            "active": False,
            "low": 100.0,
            "high": 101.0,
            "break_phase": 1,
            "break_side": "LONG",
            "break_time_utc": "2026-08-25T13:10:00+00:00",
            "return_confirm_count": 1,
        })
        events = []
        runner._trade_row = lambda event, _strat, **fields: events.append((event, fields))

        runner._advance_inventory_range_fade(
            pd.Series(
                {"Close": 100.5},
                name=pd.Timestamp("2026-08-25T13:00:00Z"),
            )
        )

        range_state = runner.state["routing"]["inventory_range_fade"]
        self.assertIsNone(range_state["pending_side"])
        self.assertEqual(range_state["break_phase"], 0)
        self.assertTrue(any(
            event == "inventory_range_invalidated"
            and fields.get("reason") == "malformed_persisted_range_state"
            for event, fields in events
        ))

    def test_range_dispatch_revalidates_pending_provenance_and_high_watermark(self):
        signal_bar = pd.Timestamp("2026-08-25 13:01:00", tz="UTC")
        cases = (
            {
                "pending_side": "LONG",
                "pending_origin_bar": None,
                "pending_break_side": None,
                "last_dispatch_bar": None,
            },
            {
                "pending_side": "LONG",
                "pending_origin_bar": dt_text(signal_bar - pd.Timedelta(minutes=1)),
                "pending_break_side": "SHORT",
                "last_dispatch_bar": dt_text(signal_bar + pd.Timedelta(minutes=5)),
            },
            {
                "pending_side": "LONG",
                "pending_origin_bar": dt_text(signal_bar - pd.Timedelta(minutes=1)),
                "pending_break_side": "SHORT",
                "last_dispatch_bar": 0,
            },
        )
        for mutation in cases:
            with self.subTest(mutation=mutation):
                runner, _strategy, _state = make_runner(live=False)
                range_state = runner.state["routing"]["inventory_range_fade"]
                range_state.update(mutation)
                events = []
                runner._trade_row = lambda event, _strat, **fields: events.append(
                    (event, fields)
                )

                opportunity = runner._take_inventory_range_fade_opportunity(
                    raw_side=None,
                    signal_bar=signal_bar,
                    poll_time=signal_bar + pd.Timedelta(minutes=1),
                    symbol="XAUUSD",
                )

                self.assertIsNone(opportunity)
                self.assertIsNone(range_state["pending_side"])
                self.assertTrue(any(
                    event == "inventory_range_invalidated"
                    for event, _fields in events
                ))

    def test_range_dispatch_cannot_bypass_future_state_receipt(self):
        runner, _strategy, _state = make_runner(live=False)
        range_state = runner.state["routing"]["inventory_range_fade"]
        range_state.update({
            "pending_side": "LONG",
            "pending_origin_bar": "2026-08-25T13:00:00+00:00",
            "pending_break_side": "SHORT",
            "last_state_bar": "2026-08-25T14:00:00+00:00",
            "last_dispatch_bar": None,
        })

        opportunity = runner._take_inventory_range_fade_opportunity(
            raw_side=None,
            signal_bar=pd.Timestamp("2026-08-25T13:01:00Z"),
            poll_time=pd.Timestamp("2026-08-25T13:02:00Z"),
            symbol="XAUUSD",
        )

        self.assertIsNone(opportunity)
        self.assertIsNone(range_state["pending_side"])
        self.assertEqual(range_state["last_state_bar"], "2026-08-25T14:00:00+00:00")

    def test_range_pending_preserves_origin_identity_across_next_bar(self):
        runner, _strategy, _state = make_runner(live=False)
        origin_bar = pd.Timestamp("2026-08-25 13:00:00", tz="UTC")
        current_bar = origin_bar + pd.Timedelta(minutes=1)
        range_state = runner.state["routing"]["inventory_range_fade"]
        range_state.update({
            "pending_side": "LONG",
            "pending_origin_bar": dt_text(origin_bar),
            "pending_break_side": "SHORT",
            "last_state_bar": dt_text(origin_bar),
            "last_dispatch_bar": None,
        })

        opportunity = runner._take_inventory_range_fade_opportunity(
            raw_side=None,
            signal_bar=current_bar,
            poll_time=current_bar + pd.Timedelta(minutes=1),
            symbol="XAUUSD",
        )

        self.assertIsNotNone(opportunity)
        self.assertEqual(opportunity["event_time"], dt_text(origin_bar))
        self.assertEqual(
            opportunity["release_time"],
            dt_text(origin_bar + pd.Timedelta(minutes=1)),
        )
        self.assertIn(dt_text(origin_bar), opportunity["opportunity_id"])
        self.assertEqual(range_state["last_dispatch_bar"], dt_text(origin_bar))

    def test_range_opportunity_cannot_bypass_global_receipt_high_watermark(self):
        origin_bar = pd.Timestamp("2026-08-25 13:00:00", tz="UTC")
        signal_bar = origin_bar + pd.Timedelta(minutes=1)
        for global_receipt in (
            0,
            dt_text(signal_bar + pd.Timedelta(minutes=5)),
        ):
            with self.subTest(global_receipt=global_receipt):
                runner, _strategy, _state = make_runner(live=False)
                executor = CountingExecutor()
                executor.get_symbol_info = lambda *_args: SimpleNamespace(
                    bid=100.0, ask=100.03,
                )
                runner.executor = executor
                bars = pd.DataFrame(
                    {
                        "Open": [100.0, 100.0], "Close": [100.0, 100.0],
                        "AskOpen": [100.03, 100.03], "atr30": [2.5, 2.5],
                        "bb20_mid": [100.0, 100.0], "bb20_std": [1.0, 1.0],
                        "spread_points": [30.0, 30.0],
                    },
                    index=pd.DatetimeIndex([origin_bar, signal_bar]),
                )
                runner._get_m1 = lambda: bars.copy()
                runner._signal = lambda *_args: None
                range_state = runner.state["routing"]["inventory_range_fade"]
                range_state.update({
                    "last_state_bar": dt_text(signal_bar),
                    "pending_side": "LONG",
                    "pending_origin_bar": dt_text(origin_bar),
                    "pending_break_side": "SHORT",
                    "last_dispatch_bar": None,
                })
                runner.state["routing"]["last_routed_signal_bar"] = global_receipt
                runner._route_opportunity = lambda *_args, **_kwargs: self.fail(
                    "range must not bypass an invalid or future global receipt"
                )
                with patch.object(
                    live_s23_bot, "utc_now",
                    return_value=pd.Timestamp("2026-08-25 13:02:00Z").to_pydatetime(),
                ):
                    runner.run_once()

                self.assertIsNone(range_state["pending_side"])
                if isinstance(global_receipt, str):
                    self.assertEqual(
                        runner.state["routing"]["last_routed_signal_bar"],
                        global_receipt,
                    )

    def test_run_once_routes_qualified_raw_short_as_effective_long(self):
        runner, _strategy, _state = make_runner(live=False)
        executor = CountingExecutor()
        executor.get_symbol_info = lambda *_args: SimpleNamespace(bid=4610.0, ask=4610.2)
        runner.executor = executor
        index = pd.date_range("2026-08-25 12:30:00", periods=31, freq="1min", tz="UTC")
        bars = pd.DataFrame(
            {
                "Open": [4640.0] + [4615.0] * 30,
                "Close": [4640.0] + [4615.0] * 30,
                "AskOpen": [4640.2] + [4615.2] * 30,
                "atr30": [2.5] * 31,
                "bb20_mid": [4615.0] * 31,
                "bb20_std": [1.0] * 31,
                "spread_points": [200.0] * 31,
            },
            index=index,
        )
        runner._get_m1 = lambda: bars.copy()
        runner._signal = lambda *_args: "SHORT"
        runner._prepare_lane = lambda strat, *_args: (True, "ready", False)
        captured = []
        runner._route_opportunity = lambda opportunity, *_args: (captured.append(dict(opportunity)) or (2, "entry_attempted"))
        observer = RecordingObserver()
        runner.shadow_observer = observer
        fresh = SimpleNamespace(stale=False)
        fixed_now = pd.Timestamp("2026-08-25 13:01:02", tz="UTC").to_pydatetime()
        with patch.object(live_s23_bot, "stale_signal_decision", return_value=fresh), patch.object(
            live_s23_bot, "utc_now", return_value=fixed_now
        ):
            runner.run_once()
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["raw_side"], "SHORT")
        self.assertEqual(captured[0]["side"], "LONG")
        self.assertEqual(captured[0]["entry_policy"]["action"], "reverse_long")
        self.assertEqual([method for method, _kwargs in observer.calls], ["observe_quote", "register_opportunity", "record_route"])
        registered = observer.calls[1][1]
        self.assertEqual(registered["opportunity"]["effective_side"], "LONG")
        self.assertEqual(registered["context"]["portfolio_positions"], 0)
        self.assertEqual(registered["context"]["lane_positions"], {str(lane): 0 for lane in range(1, 13)})
        self.assertEqual(observer.calls[2][1]["consumed_lane_id"], 2)

    def test_policy_rejected_raw_opportunity_is_registered_before_rejection(self):
        runner, _strategy, _state = make_runner(live=False)
        observer = RecordingObserver()
        runner.shadow_observer = observer
        executor = CountingExecutor()
        executor.get_symbol_info = lambda *_args: SimpleNamespace(bid=4610.0, ask=4610.2)
        runner.executor = executor
        index = pd.date_range("2026-08-25 12:30:00", periods=31, freq="1min", tz="UTC")
        bars = pd.DataFrame(
            {
                "Open": [4640.0] * 31,
                "Close": [4640.0] * 31,
                "AskOpen": [4640.2] * 31,
                "atr30": [2.5] * 31,
                "ret10": [-30.0] * 31,
                "vol_ratio": [1.2] * 31,
                "spread_points": [200.0] * 31,
            },
            index=index,
        )
        runner._get_m1 = lambda: bars.copy()
        runner._signal = lambda *_args: "SHORT"
        runner._prepare_lane = lambda strat, *_args: (True, "ready", False)
        runner._apply_entry_policy = lambda *_args: (
            None,
            {"policy_id": "reverse_d60", "action": "block", "reason": "insufficient_completed_m1_history"},
        )
        fixed_now = pd.Timestamp("2026-08-25 13:01:02", tz="UTC").to_pydatetime()
        with patch.object(live_s23_bot, "utc_now", return_value=fixed_now):
            runner.run_once()
        self.assertEqual([method for method, _kwargs in observer.calls], ["observe_quote", "register_opportunity", "record_route"])
        self.assertEqual(observer.calls[1][1]["opportunity"]["effective_side"], "")
        self.assertEqual(observer.calls[2][1]["status"], "policy_rejected")
        self.assertEqual(executor.open_calls, 0)

    def test_observer_failure_does_not_change_route_or_order_path(self):
        runner, _strategy, _state = make_runner(live=False)
        runner.shadow_observer = RecordingObserver(fail=True)
        opportunity_row, price_row, poll_time, info = sample_opportunity()
        routed = []
        runner._consume_opportunity = lambda strat, opportunity, *_args: (routed.append((strat["lane_id"], opportunity["opportunity_id"])) or (True, "entry_attempted"))
        lane_readiness = {int(strat["lane_id"]): (True, "ready", False) for strat in runner.params["strategies"]}
        self.assertIsNone(runner._observer_call("register_opportunity", opportunity=opportunity_row, at=poll_time, bid=info.bid, ask=info.ask, context={}))
        lane_id, reason = runner._route_opportunity(opportunity_row, price_row, info, poll_time, lane_readiness)
        self.assertEqual((lane_id, reason), (1, "entry_attempted"))
        self.assertEqual(routed, [(1, opportunity_row["opportunity_id"])])

    def test_state_tagger_failure_does_not_change_route_or_order_path(self):
        runner, _strategy, _state = make_runner(live=False)
        failing = SimpleNamespace(enabled=True)
        failing.tag_opportunity = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("tag_write_failed"))
        runner.shadow_state_tagger = failing
        opportunity_row, price_row, poll_time, info = sample_opportunity()
        routed = []
        runner._consume_opportunity = lambda strat, opportunity, *_args: (routed.append((strat["lane_id"], opportunity["opportunity_id"])) or (True, "entry_attempted"))
        lane_readiness = {int(strat["lane_id"]): (True, "ready", False) for strat in runner.params["strategies"]}
        self.assertIsNone(
            runner._state_tagger_call(
                "tag_opportunity", opportunity=opportunity_row, at=poll_time,
                bars=pd.DataFrame(), bid=info.bid, ask=info.ask, context={},
            )
        )
        lane_id, reason = runner._route_opportunity(opportunity_row, price_row, info, poll_time, lane_readiness)
        self.assertEqual((lane_id, reason), (1, "entry_attempted"))
        self.assertEqual(routed, [(1, opportunity_row["opportunity_id"])])

    def test_same_confirmed_m1_opportunity_routes_only_once_across_polls(self):
        runner, strategy, _state = make_runner(live=False)
        executor = CountingExecutor()
        executor.get_symbol_info = lambda *_args: SimpleNamespace(bid=100.0, ask=100.03)
        runner.executor = executor
        index = pd.date_range("2026-08-25 13:00:00", periods=2, freq="1min", tz="UTC")
        bars = pd.DataFrame(
            {
                "Open": [100.0, 100.0],
                "Close": [100.0, 100.0],
                "AskOpen": [100.03, 100.03],
                "atr30": [2.5, 2.5],
                "bb20_mid": [100.0, 100.0],
                "bb20_std": [1.0, 1.0],
                "spread_points": [30.0, 30.0],
            },
            index=index,
        )
        runner._get_m1 = lambda: bars.copy()
        runner._signal = lambda *_args: "LONG"
        fresh = SimpleNamespace(stale=False)
        fixed_now = pd.Timestamp("2026-08-25 13:02:00", tz="UTC").to_pydatetime()
        with patch.object(live_s23_bot, "stale_signal_decision", return_value=fresh), patch.object(live_s23_bot, "utc_now", return_value=fixed_now):
            runner.run_once()
            runner.run_once()

        total_positions = sum(len(runner._st(row)["basket"]) for row in runner.params["strategies"])
        self.assertEqual(total_positions, 1)
        self.assertEqual(runner.state["routing"]["last_consumed_lane_id"], 1)

    def test_malformed_global_decision_receipt_consumes_current_bar_without_reopen(self):
        runner, _strategy, _state = make_runner(live=False)
        executor = CountingExecutor()
        executor.get_symbol_info = lambda *_args: SimpleNamespace(bid=100.0, ask=100.03)
        runner.executor = executor
        index = pd.date_range("2026-08-25 13:00:00", periods=2, freq="1min", tz="UTC")
        bars = pd.DataFrame(
            {
                "Open": [100.0, 100.0], "Close": [100.0, 100.0],
                "AskOpen": [100.03, 100.03], "atr30": [2.5, 2.5],
                "bb20_mid": [100.0, 100.0], "bb20_std": [1.0, 1.0],
                "spread_points": [30.0, 30.0],
            },
            index=index,
        )
        runner._get_m1 = lambda: bars.copy()
        runner._signal = lambda *_args: "LONG"
        fresh = SimpleNamespace(stale=False)
        fixed_now = pd.Timestamp("2026-08-25 13:02:00", tz="UTC").to_pydatetime()
        with patch.object(live_s23_bot, "stale_signal_decision", return_value=fresh), patch.object(live_s23_bot, "utc_now", return_value=fixed_now):
            runner.run_once()
            for strat in runner.params["strategies"]:
                runner._st(strat)["basket"] = []
                runner._st(strat)["last_closed_signal_bar"] = None
            runner.state["routing"]["last_routed_signal_bar"] = 0
            runner.run_once()

        total_positions = sum(len(runner._st(row)["basket"]) for row in runner.params["strategies"])
        self.assertEqual(total_positions, 0)
        self.assertEqual(
            runner.state["routing"]["last_routed_signal_bar"],
            dt_text(index[-1]),
        )

    def test_future_global_and_lane_receipts_block_older_bar_replay(self):
        current_bar = pd.Timestamp("2026-08-25 13:01:00", tz="UTC")
        future_receipt = dt_text(current_bar + pd.Timedelta(minutes=5))

        lane_runner, lane, _state = make_runner(live=False)
        lane_runner._st(lane)["last_evaluated_bar"] = future_receipt
        self.assertFalse(
            lane_runner._reserve_lane_evaluation_bar(
                lane, dt_text(current_bar), "test_decision",
            )
        )
        self.assertEqual(lane_runner._st(lane)["last_evaluated_bar"], future_receipt)

        runner, _strategy, _state = make_runner(live=False)
        executor = CountingExecutor()
        executor.get_symbol_info = lambda *_args: SimpleNamespace(bid=100.0, ask=100.03)
        runner.executor = executor
        index = pd.date_range("2026-08-25 13:00:00", periods=2, freq="1min", tz="UTC")
        runner._get_m1 = lambda: pd.DataFrame(
            {
                "Open": [100.0, 100.0], "Close": [100.0, 100.0],
                "AskOpen": [100.03, 100.03], "atr30": [2.5, 2.5],
                "bb20_mid": [100.0, 100.0], "bb20_std": [1.0, 1.0],
                "spread_points": [30.0, 30.0],
            },
            index=index,
        )
        runner._signal = lambda *_args: "LONG"
        runner.state["routing"]["last_routed_signal_bar"] = future_receipt
        with (
            patch.object(live_s23_bot, "stale_signal_decision", return_value=SimpleNamespace(stale=False)),
            patch.object(
                live_s23_bot, "utc_now",
                return_value=pd.Timestamp("2026-08-25 13:02:00Z").to_pydatetime(),
            ),
        ):
            runner.run_once()
        self.assertEqual(
            sum(len(runner._st(row)["basket"]) for row in runner.params["strategies"]),
            0,
        )
        self.assertEqual(
            runner.state["routing"]["last_routed_signal_bar"], future_receipt,
        )

    def test_four_lane_ownership_namespace_is_exact(self):
        runner, _strategy, _state = make_runner()
        self.assertIsNone(runner._ownership_namespace_error())
        self.assertEqual(tuple(row["magic"] for row in runner.params["strategies"]), EXPECTED_S23_MAGICS)
        self.assertEqual([row["lane_id"] for row in runner.params["strategies"]], [1, 2, 3, 4])
        frozen_fields = {
            "lot", "session_start_utc", "session_end_utc", "mode", "impulse_bars", "impulse_atr",
            "add_atr", "max_positions", "add_profit_guard_ratio", "basket_target_usd", "basket_stop_usd",
            "max_hold_bars", "cooldown", "vol_min", "failure_to_progress_bars",
            "failure_to_progress_peak_usd", "entry_wait_z", "entry_wait_sigma", "entry_wait_minutes",
            "entry_require_extreme", "target_atr_mult", "stop_atr_mult",
            "failure_to_progress_peak_atr_mult", "entry_max_spread_atr_ratio",
            "adaptive_fixed_exit_atr_threshold", "reverse_on_fail",
        }
        baseline = {key: runner.params["strategies"][0][key] for key in frozen_fields}
        for row in runner.params["strategies"][1:]:
            self.assertEqual({key: row[key] for key in frozen_fields}, baseline)

    def test_legacy_single_lane_inventory_blocks_cutover(self):
        runner, _strategy, _state = make_runner()
        legacy_magic = LEGACY_S23_MAGICS[0]
        legacy_position = SimpleNamespace(
            ticket=920023,
            identifier=920023,
            symbol="XAUUSD",
            magic=legacy_magic,
            comment="s23_loss_abort",
            type=ORDER_TYPE_BUY,
            volume=0.01,
        )
        runner.executor = CountingExecutor()
        runner.executor.positions = [legacy_position]

        error = runner._legacy_inventory_error()

        self.assertIsNotNone(error)
        self.assertIn("legacy_inventory_not_flat", str(error))
        self.assertIn(str(legacy_magic), str(error))

    def test_flat_legacy_single_lane_namespace_allows_cutover(self):
        runner, _strategy, _state = make_runner()
        runner.executor = CountingExecutor()

        self.assertIsNone(runner._legacy_inventory_error())

    def test_frozen_lane_parameter_drift_refuses_preflight_contract(self):
        runner, strategy, _state = make_runner()
        strategy["add_atr"] = 0.66

        error = runner._ownership_namespace_error()

        self.assertIsNotNone(error)
        self.assertIn("frozen_lane_contract_drift", str(error))
        self.assertIn("add_atr", str(error))

    def test_bridge_version_contract_cannot_be_weakened_by_params(self):
        runner, _strategy, _state = make_runner()
        runner.params["expected_bridge_version"] = "2026-08-29-s23-histpage-v5"

        error = runner._ownership_namespace_error()

        self.assertEqual(error, "invalid_expected_bridge_version=2026-08-29-s23-histpage-v5")

    def test_bridge_name_contract_cannot_be_weakened_by_params(self):
        runner, _strategy, _state = make_runner()
        runner.params["expected_bridge_name"] = "BotBridge_s99"

        error = runner._ownership_namespace_error()

        self.assertEqual(error, "invalid_expected_bridge_name=BotBridge_s99")

    def test_legacy_jst_admission_notation_is_rejected(self):
        runner, _strategy, _state = make_runner()
        runner.params["eu_entry_admission_clock"]["notation"] = "per_market_dst"

        error = runner._ownership_namespace_error()

        self.assertEqual(error, "invalid_entry_admission_notation=per_market_dst")

    def test_legacy_jst_boundary_fields_are_rejected(self):
        runner, _strategy, _state = make_runner()
        start = runner.params["eu_entry_admission_clock"]["blocks"][0]["start"]
        start["dst_jst"] = start.pop("dst_utc")
        start["standard_jst"] = start.pop("standard_utc")

        error = runner._ownership_namespace_error()

        self.assertIsNotNone(error)
        self.assertIn("invalid_entry_admission_blocks", str(error))

    def test_account_identity_mismatch_is_rejected(self):
        runner, _strategy, _state = make_runner()
        with patch.object(live_s23_bot, "MT5_LOGIN", 123456), patch.object(
            live_s23_bot, "MT5_SERVER", "Expected-Server"
        ):
            error = runner._account_identity_error({"login": 123457, "server": "Expected-Server", "currency": "USD"})

        self.assertIsNotNone(error)
        self.assertEqual(
            error,
            "account_identity_mismatch:login_match=false;server_match=true;currency_match=true",
        )
        self.assertNotIn("123456", str(error))
        self.assertNotIn("123457", str(error))

    def test_legacy_bridge_without_account_identity_is_rejected(self):
        runner, _strategy, _state = make_runner()

        error = runner._account_identity_error({"margin_mode": live_s23_bot.HEDGING_MARGIN_MODE})

        self.assertIn("account_identity_unavailable", str(error))

    def test_account_bridge_response_parses_login_and_server(self):
        response = (
            f"OK|{live_s23_bot.HEDGING_MARGIN_MODE}|RETAIL_HEDGING|1|1|1|1|"
            f"{live_s23_bot.MT5_LOGIN}|{live_s23_bot.MT5_SERVER}|USD"
        )
        with patch.object(live_executor.ea_bridge, "send_command", return_value=response):
            account = live_executor.MT5Executor().get_account_info()

        self.assertEqual(account["login"], live_s23_bot.MT5_LOGIN)
        self.assertEqual(account["server"], live_s23_bot.MT5_SERVER)
        self.assertEqual(account["currency"], "USD")

    def test_account_bridge_rejects_nonbinary_permission_flags(self):
        response = (
            f"OK|{live_s23_bot.HEDGING_MARGIN_MODE}|RETAIL_HEDGING|2|1|1|1|"
            f"{live_s23_bot.MT5_LOGIN}|{live_s23_bot.MT5_SERVER}|USD"
        )
        with patch.object(live_executor.ea_bridge, "send_command", return_value=response):
            self.assertIsNone(live_executor.MT5Executor().get_account_info())

    def test_symbol_info_parses_broker_quote_timestamp(self):
        response = "OK|2064.030|2064.000|1000.00|0.001|0.01|100.00|0.01|0.1|0.001|100.0|3|0|1787890200123|4|1"
        with patch.object(live_executor.ea_bridge, "send_command", return_value=response):
            info = live_executor.MT5Executor().get_symbol_info("XAUUSD")
        self.assertEqual(info.quote_time_msc, 1787890200123)
        self.assertEqual(info.contract_size, 100.0)
        self.assertEqual(info.tick_value, 0.1)
        self.assertEqual(info.trade_mode, 4)
        self.assertEqual(info.order_mode, 1)

    def test_close_executor_preserves_market_closed_retcode(self):
        with patch.object(live_executor.ea_bridge, "send_command", return_value="ERR|10018|DEAL=0|LAST=0"):
            result = live_executor.MT5Executor().close_position(
                12345, expected_login=123456, expected_server="Expected-Server",
                expected_symbol="XAUUSD", expected_magic=230035,
                expected_comment="s23_sv_l1", expected_identifier=8803,
            )
        self.assertFalse(result)
        self.assertEqual(result.status, "MARKET_CLOSED")
        self.assertEqual(result.retcode, 10018)

    def test_live_preflight_rejects_bridge_without_quote_timestamp(self):
        runner, _strategy, _state = make_runner(live=True)
        runner.dm.connect = lambda: True
        runner.executor = live_s23_bot.FakeExecutor()
        missing_csv = os.path.join(tempfile.gettempdir(), "s23_missing_trade_audit_for_preflight.csv")
        if os.path.exists(missing_csv):
            os.unlink(missing_csv)
        with patch.object(live_s23_bot, "TRADE_LOG_FILE", missing_csv):
            self.assertFalse(runner.connect_and_preflight())

    def test_live_preflight_rejects_bridge_before_atomic_command_guard_v8(self):
        runner, _strategy, _state = make_runner(live=True)
        runner.dm.connect = lambda: True
        runner.executor = live_s23_bot.FakeExecutor()
        original_caps = runner.executor.get_bridge_capabilities
        runner.executor.get_bridge_capabilities = lambda: {
            **original_caps(), "version": "2026-08-29-s23-histpage-v5",
        }
        missing_csv = os.path.join(tempfile.gettempdir(), "s23_old_bridge_for_preflight.csv")
        if os.path.exists(missing_csv):
            os.unlink(missing_csv)

        with patch.object(live_s23_bot, "TRADE_LOG_FILE", missing_csv), self.assertLogs(level="CRITICAL") as captured:
            self.assertFalse(runner.connect_and_preflight())
        self.assertIn("wrong bridge version", "\n".join(captured.output))

    def test_live_preflight_rejects_unexpected_bridge_command_surface(self):
        runner, _strategy, state = make_runner(live=False)
        runner.params["enabled"] = False
        state["basket"] = [{"shadow": True}]
        runner.dm.connect = lambda: True
        runner.executor = live_s23_bot.FakeExecutor()
        original_caps = runner.executor.get_bridge_capabilities
        runner.executor.get_bridge_capabilities = lambda: {
            **original_caps(), "commands": original_caps()["commands"] | {"PENDING"},
        }
        runner._legacy_inventory_error = lambda: None
        with patch.object(live_s23_bot, "validate_csv_schema", return_value=None):
            self.assertFalse(runner.connect_and_preflight())

    def test_migration_save_failure_returns_no_go_and_preserves_retry_flag(self):
        runner, _strategy, _state = make_runner(live=False)
        runner.dm.connect = lambda: True
        runner.executor = live_s23_bot.FakeExecutor()
        runner._session_vwap_state_migrated = True
        missing_csv = os.path.join(tempfile.gettempdir(), "s23_missing_migration_save_audit.csv")
        if os.path.exists(missing_csv):
            os.unlink(missing_csv)

        with (
            patch.object(live_s23_bot, "TRADE_LOG_FILE", missing_csv),
            patch.object(runner, "_save_state", side_effect=OSError("disk unavailable")),
            self.assertLogs(level="ERROR") as captured,
        ):
            self.assertFalse(runner.connect_and_preflight())

        self.assertTrue(runner._session_vwap_state_migrated)
        self.assertIn("migrated state could not be persisted", "\n".join(captured.output))

    def test_first_consuming_router_preserves_primary_then_uses_next_lane(self):
        runner, _strategy, _state = make_runner(live=False)
        opportunity, row, poll_time, info = sample_opportunity()
        strategies = runner.params["strategies"]
        runner._st(strategies[0])["cooldown_until_utc"] = dt_text(poll_time + pd.Timedelta(minutes=1))
        readiness = {lane: (True, "ready", False) for lane in range(1, 5)}

        with patch.object(live_s23_bot, "utc_now", return_value=poll_time.to_pydatetime()):
            runner._route_opportunity(opportunity, row, info, poll_time, readiness)

        self.assertFalse(runner._st(strategies[0])["basket"])
        self.assertEqual(len(runner._st(strategies[1])["basket"]), 1)
        self.assertFalse(runner._st(strategies[2])["basket"])
        self.assertFalse(runner._st(strategies[3])["basket"])
        self.assertEqual(runner.state["routing"]["last_consumed_lane_id"], 2)

    def test_malformed_persisted_cooldown_cannot_admit_new_za_exposure(self):
        for label, value in (
            ("bad_text", "not-a-timestamp"),
            ("falsey_integer", 0),
            ("boolean", False),
            ("valid_but_out_of_bound_future", "2026-08-25T14:00:00+00:00"),
        ):
            with self.subTest(label=label):
                runner, strategy, state = make_runner(live=False)
                opportunity, row, poll_time, info = sample_opportunity()
                state["cooldown_until_utc"] = value

                consumed, reason = runner._consume_opportunity(
                    strategy, opportunity, row, info, poll_time,
                )

                self.assertFalse(consumed)
                self.assertEqual(reason, "cooldown_state_invalid")
                self.assertFalse(state["basket"])

    def test_pending_arm_consumes_once_without_duplicate_lane_entry(self):
        runner, _strategy, _state = make_runner(live=False)
        opportunity, row, poll_time, info = sample_opportunity()
        row["atr30"] = 1.5
        row["Close"] = 103.0
        info = SimpleNamespace(bid=103.0, ask=103.03)
        readiness = {lane: (True, "ready", False) for lane in range(1, 5)}

        runner._route_opportunity(opportunity, row, info, poll_time, readiness)

        strategies = runner.params["strategies"]
        self.assertEqual(runner._st(strategies[0])["pending_entry_opportunity_id"], opportunity["opportunity_id"])
        self.assertTrue(all(not runner._st(strategy)["basket"] for strategy in strategies))
        self.assertTrue(all(runner._st(strategy)["pending_entry_side"] is None for strategy in strategies[1:]))
        self.assertEqual(runner.state["routing"]["last_consumed_lane_id"], 1)

    def test_ambiguous_live_open_is_consumed_and_never_falls_through(self):
        runner, _strategy, _state = make_runner(live=True)
        executor = CountingExecutor()
        executor.last_order_error = "UNKNOWN_OPEN_FAILURE"
        runner.executor = executor
        opportunity, row, poll_time, info = sample_opportunity()
        readiness = {lane: (True, "ready", False) for lane in range(1, 5)}

        with patch.object(live_s23_bot, "utc_now", return_value=poll_time.to_pydatetime()):
            runner._route_opportunity(opportunity, row, info, poll_time, readiness)

        strategies = runner.params["strategies"]
        self.assertEqual(executor.open_calls, 1)
        self.assertEqual(runner.state["routing"]["last_consumed_lane_id"], 1)
        self.assertEqual(runner._st(strategies[0])["sync_block_reason"], "ambiguous_open_result")
        self.assertTrue(all(not runner._st(strategy)["basket"] for strategy in strategies))

    def test_market_closed_open_is_definitive_no_fill_with_bounded_retry(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        executor.last_order_error = "ERR|10018|ORDER=0|DEAL=0|LAST=0"
        runner.executor = executor
        opportunity, row, poll_time, info = sample_opportunity()

        with patch.object(live_s23_bot, "utc_now", return_value=poll_time.to_pydatetime()):
            self.assertTrue(
                runner._open_entry(
                    strategy,
                    "LONG",
                    row,
                    info,
                    execution_time=poll_time - pd.Timedelta(minutes=5),
                    admission_time=poll_time,
                    opportunity=opportunity,
                )
            )
        self.assertFalse(state["basket"])
        self.assertIsNone(state["pending_open_opportunity_id"])
        self.assertFalse(state["sync_block_new_entries"])
        self.assertEqual(
            parse_ts(state["open_retry_after_utc"]),
            poll_time + pd.Timedelta(seconds=60),
        )

    def test_live_open_rejects_stale_broker_quote_before_submission(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        opportunity, row, poll_time, info = sample_opportunity()
        info.quote_time_msc = int(
            (poll_time - pd.Timedelta(minutes=5)).timestamp() * 1000
        )
        events = []
        runner._trade_row = lambda event, _strat, **fields: events.append((event, fields))

        with patch.object(live_s23_bot, "utc_now", return_value=poll_time.to_pydatetime()):
            self.assertFalse(
                runner._open_entry(
                    strategy,
                    "LONG",
                    row,
                    info,
                    execution_time=poll_time,
                    opportunity=opportunity,
                )
            )

        self.assertEqual(executor.open_calls, 0)
        self.assertFalse(state["basket"])
        self.assertIn(
            "broker_quote_clock_out_of_bounds",
            [fields.get("reason") for event, fields in events if event == "entry_skip"],
        )

    def test_live_open_rejects_missing_broker_quote_clock_before_submission(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        opportunity, row, poll_time, info = sample_opportunity()
        del info.quote_time_msc

        with patch.object(live_s23_bot, "utc_now", return_value=poll_time.to_pydatetime()):
            self.assertFalse(
                runner._open_entry(
                    strategy,
                    "LONG",
                    row,
                    info,
                    execution_time=poll_time,
                    opportunity=opportunity,
                )
            )

        self.assertEqual(executor.open_calls, 0)
        self.assertFalse(state["basket"])

    def test_live_open_rechecks_blocked_hour_after_persisting_reservation(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        opportunity, row, _poll_time, info = sample_opportunity()
        before_boundary = pd.Timestamp("2026-08-25T13:59:59Z")
        after_boundary = pd.Timestamp("2026-08-25T14:00:01Z")
        info.quote_time_msc = int(before_boundary.timestamp() * 1000)
        clock_calls = 0

        def advancing_clock():
            nonlocal clock_calls
            clock_calls += 1
            stamp = before_boundary if clock_calls == 1 else after_boundary
            return stamp.to_pydatetime()

        with patch.object(live_s23_bot, "utc_now", side_effect=advancing_clock):
            opened = runner._open_entry(
                strategy,
                "LONG",
                row,
                info,
                basket_atr30=2.5,
                execution_time=before_boundary,
                opportunity=opportunity,
                apply_portfolio_rearm=False,
            )

        self.assertFalse(opened)
        self.assertEqual(executor.open_calls, 0)
        self.assertIsNone(state["pending_open_opportunity_id"])
        self.assertIsNone(state["pending_open_started_utc"])

    def test_live_open_revalidates_owned_inventory_after_persisting_reservation(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        opportunity, row, poll_time, info = sample_opportunity()
        real_save = runner._save_state

        def expose_intervening_owned_position():
            if state.get("pending_open_opportunity_id") and not executor.positions:
                executor.positions.append(
                    SimpleNamespace(
                        ticket=9620,
                        identifier=9620,
                        symbol="XAUUSD",
                        magic=int(strategy["magic"]),
                        comment=strategy["comment_prefix"],
                        type=ORDER_TYPE_BUY,
                        volume=0.01,
                        open_price=100.0,
                        open_time=int(poll_time.timestamp()),
                    )
                )
            real_save()

        runner._save_state = expose_intervening_owned_position
        with patch.object(live_s23_bot, "utc_now", return_value=poll_time.to_pydatetime()):
            opened = runner._open_entry(
                strategy,
                "LONG",
                row,
                info,
                basket_atr30=2.5,
                execution_time=poll_time,
                opportunity=opportunity,
                apply_portfolio_rearm=False,
            )

        self.assertFalse(opened)
        self.assertEqual(executor.open_calls, 0)
        self.assertIsNone(state["pending_open_opportunity_id"])
        self.assertEqual(state["sync_block_reason"], "live_positions_without_state")

    def test_live_open_revalidates_account_identity_after_persisting_reservation(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        opportunity, row, poll_time, info = sample_opportunity()
        valid_account = executor.get_account_info()

        def switched_account():
            account = dict(valid_account)
            account["login"] = int(live_s23_bot.MT5_LOGIN) + 1
            return account

        executor.get_account_info = switched_account
        with patch.object(live_s23_bot, "utc_now", return_value=poll_time.to_pydatetime()):
            opened = runner._open_entry(
                strategy,
                "LONG",
                row,
                info,
                basket_atr30=2.5,
                execution_time=poll_time,
                opportunity=opportunity,
                apply_portfolio_rearm=False,
            )

        self.assertFalse(opened)
        self.assertEqual(executor.open_calls, 0)
        self.assertIsNone(state["pending_open_opportunity_id"])
        self.assertEqual(state["sync_block_reason"], "account_identity_mismatch")
        self.assertFalse(state["sync_block_recoverable"])

    def test_live_open_rechecks_trade_permission_after_persisting_reservation(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        opportunity, row, poll_time, info = sample_opportunity()
        valid_account = executor.get_account_info()

        def disabled_permission():
            account = dict(valid_account)
            account["terminal_trade_allowed"] = False
            return account

        executor.get_account_info = disabled_permission
        with patch.object(live_s23_bot, "utc_now", return_value=poll_time.to_pydatetime()):
            opened = runner._open_entry(
                strategy,
                "LONG",
                row,
                info,
                basket_atr30=2.5,
                execution_time=poll_time,
                opportunity=opportunity,
                apply_portfolio_rearm=False,
            )

        self.assertFalse(opened)
        self.assertEqual(executor.open_calls, 0)
        self.assertIsNone(state["pending_open_opportunity_id"])
        self.assertEqual(state["autotrading_reject_streak"], 1)
        self.assertIsNotNone(state["open_retry_after_utc"])

    def test_atomic_account_guard_rejection_is_definitive_no_fill(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        executor.last_order_error = "ERR|ACCOUNT_IDENTITY_GUARD"
        runner.executor = executor
        opportunity, row, poll_time, info = sample_opportunity()

        with patch.object(live_s23_bot, "utc_now", return_value=poll_time.to_pydatetime()):
            self.assertTrue(
                runner._open_entry(
                    strategy,
                    "LONG",
                    row,
                    info,
                    basket_atr30=2.5,
                    execution_time=poll_time,
                    opportunity=opportunity,
                    apply_portfolio_rearm=False,
                )
            )

        self.assertEqual(executor.open_calls, 1)
        self.assertFalse(state["basket"])
        self.assertIsNone(state["pending_open_opportunity_id"])
        self.assertEqual(state["sync_block_reason"], "account_identity_mismatch")
        self.assertFalse(state["sync_block_recoverable"])

    def test_atomic_account_guard_never_adopts_position_seen_after_rejection(self):
        runner, strategy, state = make_runner(live=True)
        opportunity, row, poll_time, info = sample_opportunity()
        unrelated = SimpleNamespace(
            ticket=8801,
            identifier=8801,
            symbol="XAUUSD",
            magic=int(strategy["magic"]),
            comment=strategy["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=float(strategy["lot"]),
            open_price=100.03,
            open_time=int(poll_time.timestamp()),
        )

        class SwitchedAccountExecutor(CountingExecutor):
            def open_position(self, *_args, **_kwargs):
                self.open_calls += 1
                self.last_order_error = "ERR|ACCOUNT_IDENTITY_GUARD"
                self.positions = [unrelated]
                return None

        executor = SwitchedAccountExecutor()
        runner.executor = executor
        with patch.object(live_s23_bot, "utc_now", return_value=poll_time.to_pydatetime()):
            self.assertTrue(
                runner._open_entry(
                    strategy,
                    "LONG",
                    row,
                    info,
                    basket_atr30=2.5,
                    execution_time=poll_time,
                    opportunity=opportunity,
                    apply_portfolio_rearm=False,
                )
            )

        self.assertEqual(executor.open_calls, 1)
        self.assertFalse(state["basket"])
        self.assertIsNone(state["pending_open_opportunity_id"])
        self.assertEqual(state["sync_block_reason"], "account_identity_mismatch")
        self.assertEqual(
            state["sync_block_details"]["atomic_open_guard"],
            "ACCOUNT_IDENTITY_GUARD",
        )

    def test_confirmed_open_blocks_extra_post_command_owned_position(self):
        runner, strategy, state = make_runner(live=True)
        opportunity, row, poll_time, info = sample_opportunity()
        confirmed = SimpleNamespace(
            ticket=8811,
            identifier=8811,
            symbol="XAUUSD",
            magic=int(strategy["magic"]),
            comment=strategy["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=float(strategy["lot"]),
            open_price=100.03,
            open_time=int(poll_time.timestamp()),
        )
        extra = SimpleNamespace(
            ticket=8812,
            identifier=8812,
            symbol="XAUUSD",
            magic=int(strategy["magic"]),
            comment=strategy["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=float(strategy["lot"]),
            open_price=100.04,
            open_time=int(poll_time.timestamp()),
        )

        class ExtraFillExecutor(CountingExecutor):
            def open_position(self, *_args, **_kwargs):
                self.open_calls += 1
                self.positions = [confirmed, extra]
                return 8811

        executor = ExtraFillExecutor()
        runner.executor = executor
        with patch.object(live_s23_bot, "utc_now", return_value=poll_time.to_pydatetime()):
            self.assertTrue(
                runner._open_entry(
                    strategy,
                    "LONG",
                    row,
                    info,
                    basket_atr30=2.5,
                    execution_time=poll_time,
                    opportunity=opportunity,
                    apply_portfolio_rearm=False,
                )
            )

        self.assertEqual([row["ticket"] for row in state["basket"]], [8811])
        self.assertEqual(
            state["sync_block_reason"],
            "post_open_owned_inventory_delta_invalid",
        )
        self.assertEqual(state["sync_block_details"]["unexpected_tickets"], [8812])
        self.assertFalse(state["sync_block_recoverable"])

    def test_definitive_open_reject_never_adopts_concurrent_untracked_position(self):
        for rejection in (
            "ERR|10018|ORDER=0|DEAL=0|LAST=0",
            "ERR|10026|ORDER=0|DEAL=0|LAST=0",
            "ERR|TRADE_PERMISSION_GUARD",
        ):
            with self.subTest(rejection=rejection):
                runner, strategy, state = make_runner(live=True)
                opportunity, row, poll_time, info = sample_opportunity()
                unrelated = SimpleNamespace(
                    ticket=8821,
                    identifier=8821,
                    symbol="XAUUSD",
                    magic=int(strategy["magic"]),
                    comment=strategy["comment_prefix"],
                    type=ORDER_TYPE_BUY,
                    volume=float(strategy["lot"]),
                    open_price=100.03,
                    open_time=int(poll_time.timestamp()),
                )

                class DefinitiveRejectExecutor(CountingExecutor):
                    def open_position(self, *_args, **_kwargs):
                        self.open_calls += 1
                        self.last_order_error = rejection
                        self.positions = [unrelated]
                        return None

                runner.executor = DefinitiveRejectExecutor()
                with patch.object(
                    live_s23_bot, "utc_now", return_value=poll_time.to_pydatetime(),
                ):
                    self.assertTrue(
                        runner._open_entry(
                            strategy,
                            "LONG",
                            row,
                            info,
                            basket_atr30=2.5,
                            execution_time=poll_time,
                            opportunity=opportunity,
                            apply_portfolio_rearm=False,
                        )
                    )

                self.assertFalse(state["basket"])
                self.assertIsNone(state["pending_open_opportunity_id"])
                self.assertEqual(
                    state["sync_block_reason"],
                    "definitive_open_reject_with_untracked_inventory",
                )
                self.assertEqual(state["sync_block_details"]["observed_tickets"], [8821])
                self.assertFalse(state["sync_block_recoverable"])

    def test_confirmed_open_blocks_order_created_during_submission_window(self):
        runner, strategy, state = make_runner(live=True)
        opportunity, row, poll_time, info = sample_opportunity()
        confirmed = SimpleNamespace(
            ticket=8831,
            identifier=8831,
            symbol="XAUUSD",
            magic=int(strategy["magic"]),
            comment=strategy["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=float(strategy["lot"]),
            open_price=100.03,
            open_time=int(poll_time.timestamp()),
        )
        concurrent_order = SimpleNamespace(
            ticket=8832,
            symbol="XAUUSD",
            magic=int(strategy["magic"]),
            comment=strategy["comment_prefix"],
        )

        class ConcurrentOrderExecutor(CountingExecutor):
            def open_position(self, *_args, **_kwargs):
                self.open_calls += 1
                self.positions = [confirmed]
                self.orders = [concurrent_order]
                return 8831

        runner.executor = ConcurrentOrderExecutor()
        with patch.object(live_s23_bot, "utc_now", return_value=poll_time.to_pydatetime()):
            self.assertTrue(
                runner._open_entry(
                    strategy,
                    "LONG",
                    row,
                    info,
                    basket_atr30=2.5,
                    execution_time=poll_time,
                    opportunity=opportunity,
                    apply_portfolio_rearm=False,
                )
            )

        self.assertEqual([item["ticket"] for item in state["basket"]], [8831])
        self.assertEqual(
            state["sync_block_reason"],
            "post_open_owned_inventory_delta_invalid",
        )
        self.assertEqual(state["sync_block_details"]["unexpected_order_tickets"], [8832])
        self.assertFalse(state["sync_block_recoverable"])

    def test_confirmed_open_rejects_duplicate_post_command_position_identifier(self):
        runner, strategy, state = make_runner(live=True)
        opportunity, row, poll_time, info = sample_opportunity()
        confirmed = SimpleNamespace(
            ticket=8841,
            identifier=8941,
            symbol="XAUUSD",
            magic=int(strategy["magic"]),
            comment=strategy["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=float(strategy["lot"]),
            open_price=100.03,
            open_time=int(poll_time.timestamp()),
        )
        duplicate_identifier = SimpleNamespace(
            ticket=8842,
            identifier=8941,
            symbol="XAUUSD",
            magic=int(strategy["magic"]),
            comment=strategy["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=float(strategy["lot"]),
            open_price=100.04,
            open_time=int(poll_time.timestamp()),
        )

        class DuplicateIdentifierExecutor(CountingExecutor):
            def open_position(self, *_args, **_kwargs):
                self.open_calls += 1
                self.positions = [confirmed, duplicate_identifier]
                return 8841

        runner.executor = DuplicateIdentifierExecutor()
        with patch.object(live_s23_bot, "utc_now", return_value=poll_time.to_pydatetime()):
            self.assertTrue(
                runner._open_entry(
                    strategy,
                    "LONG",
                    row,
                    info,
                    basket_atr30=2.5,
                    execution_time=poll_time,
                    opportunity=opportunity,
                    apply_portfolio_rearm=False,
                )
            )

        self.assertFalse(state["basket"])
        self.assertEqual(state["sync_block_reason"], "live_position_identity_invalid")
        self.assertEqual(state["sync_block_details"]["duplicate_position_ids"], [8941])
        self.assertFalse(state["sync_block_recoverable"])

    def test_atomic_permission_guard_rejection_uses_bounded_retry(self):
        runner, strategy, state = make_runner(live=True)
        runner.params["trade_permission_alert_threshold"] = 1
        runner._notify_manual_action = Mock(return_value=False)
        executor = CountingExecutor()
        executor.last_order_error = "ERR|TRADE_PERMISSION_GUARD"
        runner.executor = executor
        opportunity, row, poll_time, info = sample_opportunity()

        with patch.object(live_s23_bot, "utc_now", return_value=poll_time.to_pydatetime()):
            self.assertTrue(
                runner._open_entry(
                    strategy,
                    "LONG",
                    row,
                    info,
                    basket_atr30=2.5,
                    execution_time=poll_time,
                    opportunity=opportunity,
                    apply_portfolio_rearm=False,
                )
            )

        self.assertEqual(executor.open_calls, 1)
        self.assertFalse(state["basket"])
        self.assertIsNone(state["pending_open_opportunity_id"])
        self.assertEqual(state["autotrading_reject_streak"], 1)
        self.assertIsNotNone(state["open_retry_after_utc"])
        self.assertFalse(state["sync_block_new_entries"])
        self.assertFalse(state["autotrading_reject_notified"])
        runner._notify_manual_action.assert_called_once()

    def test_unpublished_ipc_open_error_clears_new_receipt_without_adopting_old_work(self):
        for error in (
            "ERR|COMMAND_BUSY", "ERR|CLAIM_BUSY", "ERR|LOCK_TIMEOUT",
            "ERR|WRITE_FAILED", "ERR|CLAIM_FAILED", "ERR|REQUEST_EXPIRED",
            "ERR|RESPONSE_BUSY",
        ):
            with self.subTest(error=error):
                runner, strategy, state = make_runner(live=True)
                executor = CountingExecutor()
                executor.last_order_error = error
                runner.executor = executor
                opportunity, row, poll_time, info = sample_opportunity()
                with patch.object(live_s23_bot, "utc_now", return_value=poll_time.to_pydatetime()):
                    self.assertTrue(
                        runner._open_entry(
                            strategy, "LONG", row, info, basket_atr30=2.5,
                            execution_time=poll_time, opportunity=opportunity,
                            apply_portfolio_rearm=False,
                        )
                    )
                self.assertIsNone(state["pending_open_opportunity_id"])
                self.assertEqual(state["sync_block_reason"], "ipc_open_not_published")
                self.assertFalse(state["sync_block_recoverable"])

    def test_live_open_fails_closed_when_post_reservation_inventory_query_fails(self):
        for unavailable_query in ("positions", "orders"):
            with self.subTest(unavailable_query=unavailable_query):
                runner, strategy, state = make_runner(live=True)
                executor = CountingExecutor()
                runner.executor = executor
                opportunity, row, poll_time, info = sample_opportunity()
                if unavailable_query == "positions":
                    executor.get_positions = lambda _symbol, _magic: None
                else:
                    executor.orders_available = False

                with patch.object(
                    live_s23_bot, "utc_now", return_value=poll_time.to_pydatetime(),
                ):
                    opened = runner._open_entry(
                        strategy,
                        "LONG",
                        row,
                        info,
                        basket_atr30=2.5,
                        execution_time=poll_time,
                        opportunity=opportunity,
                        apply_portfolio_rearm=False,
                    )

                self.assertFalse(opened)
                self.assertEqual(executor.open_calls, 0)
                self.assertIsNone(state["pending_open_opportunity_id"])
                self.assertEqual(
                    state["sync_block_reason"], f"{unavailable_query}_unavailable",
                )
                self.assertTrue(state["sync_block_recoverable"])

    def test_live_open_fails_closed_when_order_appears_during_reservation_save(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        opportunity, row, poll_time, info = sample_opportunity()
        real_save = runner._save_state

        def expose_intervening_order():
            if state.get("pending_open_opportunity_id") and not executor.orders:
                executor.orders.append(
                    SimpleNamespace(
                        ticket=9621,
                        symbol="XAUUSD",
                        magic=int(strategy["magic"]),
                        comment=strategy["comment_prefix"],
                    )
                )
            real_save()

        runner._save_state = expose_intervening_order
        with patch.object(live_s23_bot, "utc_now", return_value=poll_time.to_pydatetime()):
            opened = runner._open_entry(
                strategy,
                "LONG",
                row,
                info,
                basket_atr30=2.5,
                execution_time=poll_time,
                opportunity=opportunity,
                apply_portfolio_rearm=False,
            )

        self.assertFalse(opened)
        self.assertEqual(executor.open_calls, 0)
        self.assertIsNone(state["pending_open_opportunity_id"])
        self.assertEqual(state["sync_block_reason"], "same_magic_unexpected_order")

    def test_ambiguous_open_never_adopts_position_opened_before_reservation(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        opportunity, row, poll_time, info = sample_opportunity()
        executor.last_order_error = "NO_RESPONSE"
        old_position = SimpleNamespace(
            ticket=9622,
            identifier=9622,
            symbol="XAUUSD",
            magic=int(strategy["magic"]),
            comment=strategy["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=0.01,
            open_price=100.0,
            open_time=int((poll_time - pd.Timedelta(hours=1)).timestamp()),
        )

        def expose_old_position_after_ambiguous_open(*_args, **_kwargs):
            executor.open_calls += 1
            executor.positions = [old_position]
            return None

        executor.open_position = expose_old_position_after_ambiguous_open
        with patch.object(live_s23_bot, "utc_now", return_value=poll_time.to_pydatetime()):
            opened = runner._open_entry(
                strategy,
                "LONG",
                row,
                info,
                basket_atr30=2.5,
                execution_time=poll_time,
                opportunity=opportunity,
                apply_portfolio_rearm=False,
                use_confirmed_fill_time=False,
            )

        self.assertTrue(opened)
        self.assertEqual(executor.open_calls, 1)
        self.assertFalse(state["basket"])
        self.assertEqual(state["sync_block_reason"], "open_confirmation_mismatch")

    def test_pending_fill_on_signal_tick_consumes_before_later_lanes(self):
        runner, _strategy, _state = make_runner(live=False)
        runner.executor = CountingExecutor()
        strategies = runner.params["strategies"]
        opportunity, row, poll_time, info = sample_opportunity()
        arm_pending(
            runner._st(strategies[0]), atr30=2.5, target=100.03,
            now=poll_time,
        )

        readiness = {
            int(strategy["lane_id"]): runner._prepare_lane(strategy, row, info, poll_time)
            for strategy in strategies
        }
        runner._route_opportunity(opportunity, row, info, poll_time, readiness)

        self.assertEqual(len(runner._st(strategies[0])["basket"]), 1)
        self.assertTrue(all(not runner._st(strategy)["basket"] for strategy in strategies[1:]))
        self.assertEqual(runner.state["routing"]["last_consumed_lane_id"], 1)

    def test_restart_with_open_reservation_blocks_without_new_order(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        state["pending_open_opportunity_id"] = "reserved-opportunity"
        state["pending_open_started_utc"] = "2026-08-25T13:01:02+00:00"

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(state["sync_block_reason"], "unresolved_open_action")
        self.assertFalse(state["sync_block_recoverable"])
        self.assertEqual(executor.open_calls, 0)

    def test_flat_unresolved_open_requires_three_clean_confirmations_before_recovery(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        state["pending_open_opportunity_id"] = "reserved-opportunity"
        state["pending_open_started_utc"] = "2026-08-25T13:01:02+00:00"

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertFalse(runner._sync_strategy(strategy))
        self.assertFalse(runner._sync_strategy(strategy))
        self.assertTrue(runner._sync_strategy(strategy))

        self.assertIsNone(state["pending_open_opportunity_id"])
        self.assertIsNone(state["pending_open_started_utc"])
        self.assertFalse(state["sync_block_new_entries"])
        self.assertEqual(executor.open_calls, 0)

    def test_flat_confirmation_streak_resets_when_broker_query_is_unavailable(self):
        for unavailable_query in ("positions", "orders"):
            with self.subTest(unavailable_query=unavailable_query):
                runner, strategy, state = make_runner(live=True)
                executor = CountingExecutor()
                runner.executor = executor
                state["pending_open_opportunity_id"] = "reserved-opportunity"
                state["pending_open_started_utc"] = "2026-08-25T13:01:02+00:00"

                self.assertFalse(runner._sync_strategy(strategy))
                self.assertFalse(runner._sync_strategy(strategy))
                self.assertFalse(runner._sync_strategy(strategy))
                self.assertEqual(state["flat_clear_confirmation_count"], 2)

                if unavailable_query == "positions":
                    original_get_positions = executor.get_positions
                    executor.get_positions = lambda _symbol, _magic: None
                else:
                    executor.orders_available = False
                self.assertFalse(runner._sync_strategy(strategy))
                self.assertEqual(state["flat_clear_confirmation_count"], 0)
                self.assertEqual(
                    state["flat_clear_confirmation_reason"], None,
                )
                self.assertEqual(
                    state["sync_block_reason"], "unresolved_open_action",
                )
                self.assertEqual(
                    state["pending_open_opportunity_id"], "reserved-opportunity",
                )

                if unavailable_query == "positions":
                    executor.get_positions = original_get_positions
                else:
                    executor.orders_available = True
                self.assertFalse(runner._sync_strategy(strategy))
                self.assertFalse(runner._sync_strategy(strategy))
                self.assertTrue(runner._sync_strategy(strategy))
                self.assertIsNone(state["pending_open_opportunity_id"])

    def test_malformed_flat_clear_state_stays_blocked_without_sync_exception(self):
        corruptions = (
            {"flat_clear_confirmation_count": "not-an-integer"},
            {"flat_clear_confirmation_count": True},
            {"sync_block_details": "not-a-dict"},
        )
        for corruption in corruptions:
            with self.subTest(corruption=corruption):
                runner, strategy, state = make_runner(live=True)
                runner.executor = CountingExecutor()
                state["pending_open_opportunity_id"] = "reserved-opportunity"
                state["pending_open_started_utc"] = "2026-08-25T13:01:02+00:00"
                self.assertFalse(runner._sync_strategy(strategy))
                self.assertEqual(state["sync_block_reason"], "unresolved_open_action")
                state.update(corruption)

                self.assertFalse(runner._sync_strategy(strategy))
                self.assertTrue(state["sync_block_new_entries"])
                self.assertEqual(state["sync_block_reason"], "unresolved_open_action")

    def test_unresolved_add_reservation_allows_exact_owned_basket_exit_monitoring(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["pending_open_opportunity_id"] = "reserved-add-opportunity"
        state["pending_open_started_utc"] = "2026-08-25T13:01:02+00:00"

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertTrue(state["sync_block_new_entries"])
        self.assertEqual(state["sync_block_reason"], "unresolved_open_action")
        self.assertEqual(runner._entry_submission_block_reason(strategy), "unresolved_open_action")

        poll_time = pd.Timestamp("2026-08-25 13:10:00", tz="UTC")
        row = pd.Series({"Open": 81.0, "Close": 81.0, "AskOpen": 81.03}, name=poll_time)
        self.assertTrue(
            runner._monitor_open_basket(
                strategy,
                SimpleNamespace(bid=81.0, ask=81.03),
                row,
                poll_time,
            )
        )
        self.assertEqual(executor.close_calls, [9401])
        self.assertEqual(state["pending_close_reason"], "basket_stop")

    def test_run_once_closes_exact_owned_basket_during_unresolved_add(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["pending_open_opportunity_id"] = "reserved-add-opportunity"
        state["pending_open_started_utc"] = "2026-08-25T13:01:02+00:00"
        executor.get_symbol_info = lambda *_args: SimpleNamespace(bid=81.0, ask=81.03)
        bars = pd.DataFrame(
            {
                "Open": [100.0, 81.0],
                "Close": [100.0, 81.0],
                "AskOpen": [100.03, 81.03],
            },
            index=pd.date_range("2026-08-25 13:09:00", periods=2, freq="1min", tz="UTC"),
        )
        runner._get_m1 = lambda: bars.copy()
        runner._advance_inventory_range_fade = lambda *_args, **_kwargs: None
        runner._signal = lambda *_args, **_kwargs: None

        runner.run_once()

        self.assertEqual(executor.close_calls, [9401])
        self.assertEqual(state["pending_close_reason"], "basket_stop")
        self.assertTrue(state["sync_block_new_entries"])
        self.assertEqual(state["sync_block_reason"], "unresolved_open_action")

    def test_unresolved_add_with_unexpected_live_position_stays_fully_blocked(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        executor.positions.append(
            SimpleNamespace(
                ticket=9402,
                identifier=9402,
                symbol="XAUUSD",
                magic=EXPECTED_S23_MAGIC,
                comment=strategy["comment_prefix"],
                type=ORDER_TYPE_BUY,
                volume=0.01,
            )
        )
        state["pending_open_opportunity_id"] = "reserved-add-opportunity"
        state["pending_open_started_utc"] = "2026-08-25T13:01:02+00:00"

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(state["sync_block_reason"], "state_ticket_unowned_or_foreign")
        self.assertEqual(executor.close_calls, [])

    def test_live_lot_mismatch_blocks_reconciliation(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        executor.positions[0].volume = 0.02

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(state["sync_block_reason"], "state_position_ownership_mismatch")
        self.assertFalse(state["sync_block_recoverable"])

    def test_transient_query_failure_does_not_overwrite_nonrecoverable_block(self):
        runner, strategy, state = make_runner()
        runner.executor = CountingExecutor()
        runner.executor.get_symbol_info = lambda *_args: None
        state.update(
            {
                "sync_block_new_entries": True,
                "sync_block_reason": "state_position_ownership_mismatch",
                "sync_block_recoverable": False,
                "sync_block_details": {"ticket": 9401},
            }
        )

        runner.run_once()

        self.assertTrue(state["sync_block_new_entries"])
        self.assertEqual(state["sync_block_reason"], "state_position_ownership_mismatch")
        self.assertFalse(state["sync_block_recoverable"])
        self.assertEqual(state["sync_block_details"], {"ticket": 9401})

    def test_repeated_transient_failure_over_nonrecoverable_block_logs_once(self):
        runner, strategy, state = make_runner()
        state.update(
            {
                "sync_block_new_entries": True,
                "sync_block_reason": "state_position_ownership_mismatch",
                "sync_block_recoverable": False,
            }
        )
        with patch.object(live_s23_bot.logging, "warning") as warning:
            runner._set_sync_block(strategy, "symbol_info_failed", recoverable=True)
            runner._set_sync_block(strategy, "symbol_info_failed", recoverable=True)

        self.assertEqual(warning.call_count, 1)
        self.assertEqual(state["sync_block_reason"], "state_position_ownership_mismatch")

    def test_transient_symbol_info_failure_recovers_owned_basket_monitoring(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        info = SimpleNamespace(bid=100.0, ask=100.03)
        responses = iter((None, info))
        executor.get_symbol_info = lambda *_args: next(responses)
        bars = pd.DataFrame(
            {"Open": [100.0, 100.0], "Close": [100.0, 100.0], "AskOpen": [100.03, 100.03]},
            index=pd.date_range("2026-08-25 13:24:00", periods=2, freq="1min", tz="UTC"),
        )
        runner._get_m1 = lambda: bars.copy()

        runner.run_once()
        self.assertTrue(state["sync_block_new_entries"])
        self.assertEqual(state["sync_block_reason"], "symbol_info_failed")

        with patch.object(runner, "_monitor_open_basket", return_value=True) as monitor:
            runner.run_once()

        self.assertFalse(state["sync_block_new_entries"])
        self.assertIsNone(state["sync_block_reason"])
        self.assertEqual(monitor.call_count, 4)
        self.assertEqual({call.args[0]["lane_id"] for call in monitor.call_args_list}, {1, 2, 3, 4})

    def test_symbol_info_failure_with_open_inventory_requests_manual_alert(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        executor.get_symbol_info = lambda *_args: None
        alerts = []
        runner._suppress_manual_alerts = False
        runner._notify_manual_action = lambda _strategy, **kwargs: (alerts.append(kwargs) or True)

        runner.run_once()

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["reason"], "symbol_info_failed")
        self.assertIn("automated basket exits cannot run", alerts[0]["action"])

    def test_recoverable_sync_block_clears_after_complete_owned_inventory_sync(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state.update(
            {
                "sync_block_new_entries": True,
                "sync_block_reason": "symbol_info_failed",
                "sync_block_recoverable": True,
            }
        )
        events = []
        runner._trade_row = lambda event, _strategy, **kwargs: events.append((event, kwargs.get("reason")))

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertFalse(state["sync_block_new_entries"])
        self.assertIsNone(state["sync_block_reason"])
        self.assertIn(("sync_block_cleared_owned", "symbol_info_failed"), events)

    def test_confirmed_close_is_attributed_to_broker_deal_utc_day(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        executor.positions = []
        deal_time = pd.Timestamp("2026-08-24 23:59:58", tz="UTC")
        executor.close_deal = SimpleNamespace(
            position_id=9401,
            symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC,
            net_profit=-4.25,
            price=99.5,
            deal=77000,
            exit_volume=0.01,
            deal_time=int(deal_time.timestamp()),
        )
        with patch.object(live_s23_bot, "utc_now", return_value=pd.Timestamp("2026-08-25 00:05:00", tz="UTC").to_pydatetime()):
            self.assertTrue(runner._sync_strategy(strategy))

        self.assertEqual(state["daily_realized_date_utc"], "2026-08-24")
        self.assertEqual(state["daily_realized_pnl_usd"], -4.25)
        self.assertEqual(parse_ts(state["last_closed_at_utc"]), deal_time)
        self.assertEqual(state["last_closed_side"], "LONG")

    def test_confirmed_close_audit_failure_preserves_unaccounted_state(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        executor.positions = []
        executor.close_deal = SimpleNamespace(
            position_id=9401,
            symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC,
            net_profit=-4.25,
            price=99.5,
            deal=77010,
            exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-24T23:59:58Z").timestamp()),
        )

        def fail_confirmed_close_audit(event, _strategy, **_kwargs):
            if event == "position_close_confirmed":
                raise OSError("operational close audit unavailable")

        runner._trade_row = fail_confirmed_close_audit
        with self.assertRaisesRegex(OSError, "operational close audit unavailable"):
            runner._sync_strategy(strategy)

        self.assertEqual(len(state["basket"]), 1)
        self.assertEqual(state["daily_realized_pnl_usd"], 0.0)

    def test_confirmed_close_audit_is_idempotent_by_deal_and_position(self):
        runner, strategy, _state = make_runner(live=False)
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        with tempfile.TemporaryDirectory() as tmp:
            trade_path = os.path.join(tmp, "s23_trades.csv")
            evaluation_path = os.path.join(tmp, "s23_signal_evaluation.csv")
            with patch.object(live_s23_bot, "TRADE_LOG_FILE", trade_path):
                for _ in range(2):
                    runner._trade_row(
                        "position_close_confirmed", strategy,
                        opportunity_id=(
                            "XAUUSD|2026-08-24T23:00:00+00:00|LONG|LONG|"
                            "reverse_d60"
                        ),
                        basket_id="L1-B000001", ticket=9401,
                        position_identifier=9401, deal_id=77011,
                        side="LONG", profit=-4.25, reason="basket_stop",
                    )
            with open(trade_path, newline="", encoding="utf-8") as handle:
                trade_rows = list(csv.DictReader(handle))
            with open(evaluation_path, newline="", encoding="utf-8") as handle:
                evaluation_rows = list(csv.DictReader(handle))
        self.assertEqual(
            len([row for row in trade_rows if row["event"] == "position_close_confirmed"]),
            1,
        )
        self.assertEqual(
            len([row for row in evaluation_rows if row["event"] == "position_close_confirmed"]),
            1,
        )

    def test_confirmed_close_retry_after_state_save_failure_is_exactly_once(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        persisted_before = json.loads(json.dumps(runner.state))
        executor.positions = []
        executor.close_deal = SimpleNamespace(
            position_id=9401, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
            net_profit=-4.25, price=99.5, deal=77012, exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-24T23:59:58Z").timestamp()),
        )
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        runner._save_state = Mock(side_effect=OSError("state disk unavailable"))

        with tempfile.TemporaryDirectory() as tmp:
            trade_path = os.path.join(tmp, "s23_trades.csv")
            evaluation_path = os.path.join(tmp, "s23_signal_evaluation.csv")
            with patch.object(live_s23_bot, "TRADE_LOG_FILE", trade_path):
                with self.assertRaisesRegex(OSError, "state disk unavailable"):
                    runner._sync_strategy(strategy)

                retry_runner, retry_strategy, _retry_state = make_runner()
                retry_runner.state = persisted_before
                retry_runner.executor = executor
                retry_runner._trade_row = (
                    S23HorizontalInventoryRunner._trade_row.__get__(
                        retry_runner, S23HorizontalInventoryRunner,
                    )
                )
                retry_runner._save_state = lambda: None
                self.assertTrue(retry_runner._sync_strategy(retry_strategy))

            with open(trade_path, newline="", encoding="utf-8") as handle:
                trade_rows = list(csv.DictReader(handle))
            with open(evaluation_path, newline="", encoding="utf-8") as handle:
                evaluation_rows = list(csv.DictReader(handle))

        retry_state = retry_runner._st(retry_strategy)
        self.assertFalse(retry_state["basket"])
        self.assertEqual(retry_state["daily_realized_pnl_usd"], -4.25)
        self.assertEqual(
            len([row for row in trade_rows if row["deal_id"] == "77012"]), 1,
        )
        self.assertEqual(
            len([row for row in evaluation_rows if row["deal_id"] == "77012"]),
            1,
        )

    def test_confirmed_close_state_save_failure_restores_same_runner_state(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state_before = json.loads(json.dumps(runner.state))
        executor.positions = []
        executor.close_deal = SimpleNamespace(
            position_id=9401, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
            net_profit=-4.25, price=99.5, deal=77013, exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-24T23:59:58Z").timestamp()),
        )
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        runner._save_state = Mock(side_effect=OSError("state disk unavailable"))

        with tempfile.TemporaryDirectory() as tmp:
            trade_path = os.path.join(tmp, "s23_trades.csv")
            with patch.object(live_s23_bot, "TRADE_LOG_FILE", trade_path):
                with self.assertRaisesRegex(OSError, "state disk unavailable"):
                    runner._sync_strategy(strategy)

        self.assertEqual(runner.state, state_before)

    def test_confirmed_close_interrupt_restores_state_and_clears_save_deferral(self):
        runner, strategy, state = make_runner()
        state_before = json.loads(json.dumps(runner.state))
        runner._post_close_audit_deal_id = 77019
        runner._post_close_trade_keys.add(("stale",))
        runner._post_close_evaluation_keys.add(("stale",))

        def interrupt_after_consumption():
            state["basket"] = []
            raise KeyboardInterrupt("simulated service interrupt")

        with self.assertRaisesRegex(KeyboardInterrupt, "simulated service interrupt"):
            runner._confirmed_close_state_step(
                state_before, interrupt_after_consumption,
            )

        self.assertEqual(runner.state, state_before)
        self.assertIsNone(runner._post_close_audit_deal_id)
        self.assertFalse(runner._post_close_trade_keys)
        self.assertFalse(runner._post_close_evaluation_keys)

        writes = []
        runner._save_state = S23HorizontalInventoryRunner._save_state.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        with patch.object(
            live_s23_bot, "atomic_write_json",
            side_effect=lambda _path, payload: writes.append(
                json.loads(json.dumps(payload))
            ),
        ):
            runner._save_state()
        self.assertEqual(len(writes), 1)

    def test_confirmed_close_step_boundary_interrupt_rolls_back_entire_sync(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state_before = json.loads(json.dumps(runner.state))
        executor.positions = []
        executor.close_deal = SimpleNamespace(
            position_id=9401, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
            net_profit=-4.25, price=99.5, deal=77020, exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-25T13:30:00Z").timestamp()),
        )

        # Simulate an interrupt after transaction begin and basket consumption,
        # but before control enters the first individually guarded state step.
        runner._confirmed_close_state_step = Mock(
            side_effect=KeyboardInterrupt("simulated step-boundary interrupt")
        )
        with self.assertRaisesRegex(
            KeyboardInterrupt, "simulated step-boundary interrupt",
        ):
            runner._sync_strategy(strategy)

        self.assertEqual(runner.state, state_before)
        self.assertIsNone(runner._post_close_audit_deal_id)
        self.assertFalse(runner._post_close_trade_keys)
        self.assertFalse(runner._post_close_evaluation_keys)

    def test_confirmed_close_every_step_boundary_replays_exactly_once(self):
        for fail_at in range(1, 7):
            with self.subTest(fail_at=fail_at):
                runner, strategy, state = make_runner()
                executor = CountingExecutor()
                runner.executor = executor
                arm_owned_basket(strategy, state, executor)
                state["pending_close_reason"] = "basket_stop"
                state["pending_close_signal_bar"] = "2026-08-25T13:29:00+00:00"
                state["reverse_used"] = True
                state["frozen_basket_atr30"] = 1.5
                state_before = json.loads(json.dumps(runner.state))
                executor.positions = []
                deal_id = 77100 + fail_at
                executor.close_deal = SimpleNamespace(
                    position_id=9401, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
                    net_profit=-4.25, price=99.5, deal=deal_id,
                    exit_volume=0.01,
                    deal_time=int(
                        pd.Timestamp("2026-08-25T13:30:00Z").timestamp()
                    ),
                )
                runner._trade_row = (
                    S23HorizontalInventoryRunner._trade_row.__get__(
                        runner, S23HorizontalInventoryRunner,
                    )
                )
                original_step = runner._confirmed_close_state_step
                step_count = 0

                def interrupt_at_selected_boundary(
                    snapshot, action, *, final_commit=False,
                ):
                    nonlocal step_count
                    step_count += 1
                    if step_count == fail_at:
                        raise KeyboardInterrupt(
                            f"simulated boundary interrupt {fail_at}"
                        )
                    return original_step(
                        snapshot, action, final_commit=final_commit,
                    )

                with tempfile.TemporaryDirectory() as tmp:
                    trade_path = os.path.join(tmp, "s23_trades.csv")
                    with patch.object(live_s23_bot, "TRADE_LOG_FILE", trade_path):
                        runner._confirmed_close_state_step = (
                            interrupt_at_selected_boundary
                        )
                        with self.assertRaisesRegex(
                            KeyboardInterrupt,
                            f"simulated boundary interrupt {fail_at}",
                        ):
                            runner._sync_strategy(strategy)

                        self.assertEqual(step_count, fail_at)
                        self.assertEqual(runner.state, state_before)
                        self.assertIsNone(runner._post_close_audit_deal_id)
                        self.assertIsNone(runner._post_close_state_before)
                        self.assertFalse(runner._post_close_trade_keys)
                        self.assertFalse(runner._post_close_evaluation_keys)

                        runner._confirmed_close_state_step = original_step
                        self.assertTrue(runner._sync_strategy(strategy))

                    with open(
                        trade_path, newline="", encoding="utf-8",
                    ) as handle:
                        trade_rows = list(csv.DictReader(handle))

                converged = runner._st(strategy)
                self.assertFalse(converged["basket"])
                self.assertEqual(converged["daily_realized_pnl_usd"], -4.25)
                self.assertEqual(
                    len([
                        row for row in trade_rows
                        if row["event"] == "position_close_confirmed"
                        and row["deal_id"] == str(deal_id)
                    ]),
                    1,
                )

    def test_confirmed_close_interrupt_after_durable_commit_keeps_memory_converged(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["pending_close_reason"] = "basket_stop"
        state["pending_close_signal_bar"] = "2026-08-25T13:29:00+00:00"
        executor.positions = []
        deal_id = 77107
        executor.close_deal = SimpleNamespace(
            position_id=9401, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
            net_profit=-4.25, price=99.5, deal=deal_id, exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-25T13:30:00Z").timestamp()),
        )
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        original_save = S23HorizontalInventoryRunner._save_state.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        interrupted = False

        def save_then_interrupt_after_commit():
            nonlocal interrupted
            original_save()
            if runner._post_close_commit_in_progress and not interrupted:
                interrupted = True
                raise KeyboardInterrupt("interrupt after durable state replace")

        with tempfile.TemporaryDirectory() as tmp:
            trade_path = os.path.join(tmp, "s23_trades.csv")
            state_path = os.path.join(tmp, "s23_bot_state.json")
            with (
                patch.object(live_s23_bot, "TRADE_LOG_FILE", trade_path),
                patch.object(live_s23_bot, "STATE_FILE", state_path),
            ):
                runner._save_state = save_then_interrupt_after_commit
                with self.assertRaisesRegex(
                    KeyboardInterrupt, "interrupt after durable state replace",
                ):
                    runner._sync_strategy(strategy)

                with open(state_path, encoding="utf-8") as handle:
                    durable_state = json.load(handle)
                self.assertEqual(runner.state, durable_state)
                self.assertFalse(runner._st(strategy)["basket"])
                self.assertEqual(
                    runner._st(strategy)["daily_realized_pnl_usd"], -4.25,
                )
                self.assertIsNone(runner._post_close_audit_deal_id)
                self.assertIsNone(runner._post_close_state_before)
                self.assertFalse(runner._post_close_commit_in_progress)

                runner._save_state = original_save
                self.assertTrue(runner._sync_strategy(strategy))

            with open(trade_path, newline="", encoding="utf-8") as handle:
                trade_rows = list(csv.DictReader(handle))

        self.assertEqual(
            len([
                row for row in trade_rows
                if row["event"] == "position_close_confirmed"
                and row["deal_id"] == str(deal_id)
            ]),
            1,
        )

    def test_confirmed_close_interrupt_during_commit_cleanup_keeps_memory_converged(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["pending_close_reason"] = "basket_stop"
        state["pending_close_signal_bar"] = "2026-08-25T13:29:00+00:00"
        executor.positions = []
        deal_id = 77108
        executor.close_deal = SimpleNamespace(
            position_id=9401, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
            net_profit=-4.25, price=99.5, deal=deal_id, exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-25T13:30:00Z").timestamp()),
        )
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        runner._save_state = S23HorizontalInventoryRunner._save_state.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        original_end = (
            S23HorizontalInventoryRunner._end_confirmed_close_state_transaction.__get__(
                runner, S23HorizontalInventoryRunner,
            )
        )
        interrupted = False

        def interrupt_after_commit_marker_clear():
            nonlocal interrupted
            if runner._post_close_commit_in_progress and not interrupted:
                interrupted = True
                # Reproduce an asynchronous interrupt at the vulnerable v11
                # cleanup boundary, after the durable replace and after the
                # process-local marker was cleared but before cleanup returned.
                runner._post_close_audit_deal_id = None
                runner._post_close_state_before = None
                runner._post_close_commit_in_progress = False
                raise KeyboardInterrupt("interrupt during commit cleanup")
            original_end()

        with tempfile.TemporaryDirectory() as tmp:
            trade_path = os.path.join(tmp, "s23_trades.csv")
            state_path = os.path.join(tmp, "s23_bot_state.json")
            with (
                patch.object(live_s23_bot, "TRADE_LOG_FILE", trade_path),
                patch.object(live_s23_bot, "STATE_FILE", state_path),
            ):
                runner._end_confirmed_close_state_transaction = (
                    interrupt_after_commit_marker_clear
                )
                with self.assertRaisesRegex(
                    KeyboardInterrupt, "interrupt during commit cleanup",
                ):
                    runner._sync_strategy(strategy)

                with open(state_path, encoding="utf-8") as handle:
                    durable_state = json.load(handle)
                self.assertEqual(runner.state, durable_state)
                self.assertFalse(runner._st(strategy)["basket"])
                self.assertEqual(
                    runner._st(strategy)["daily_realized_pnl_usd"], -4.25,
                )
                self.assertIsNone(runner._post_close_audit_deal_id)
                self.assertIsNone(runner._post_close_state_before)
                self.assertFalse(runner._post_close_commit_in_progress)

                runner._end_confirmed_close_state_transaction = original_end
                self.assertTrue(runner._sync_strategy(strategy))

            with open(trade_path, newline="", encoding="utf-8") as handle:
                trade_rows = list(csv.DictReader(handle))

        self.assertEqual(
            len([
                row for row in trade_rows
                if row["event"] == "position_close_confirmed"
                and row["deal_id"] == str(deal_id)
            ]),
            1,
        )

    def test_confirmed_close_defers_intermediate_saves_until_atomic_final_commit(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["pending_close_reason"] = "basket_stop"
        state["pending_close_signal_bar"] = "2026-08-25T13:29:00+00:00"
        state["reverse_used"] = True
        state["frozen_basket_atr30"] = 1.5
        state_before = json.loads(json.dumps(runner.state))
        executor.positions = []
        executor.close_deal = SimpleNamespace(
            position_id=9401, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
            net_profit=-4.25, price=99.5, deal=77018, exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-25T13:30:00Z").timestamp()),
        )
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        runner._save_state = S23HorizontalInventoryRunner._save_state.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        attempted_states = []

        def fail_final_commit(_path, payload):
            attempted_states.append(json.loads(json.dumps(payload)))
            raise OSError("simulated power loss at final commit")

        with tempfile.TemporaryDirectory() as tmp:
            trade_path = os.path.join(tmp, "s23_trades.csv")
            state_path = os.path.join(tmp, "s23_bot_state.json")
            with (
                patch.object(live_s23_bot, "TRADE_LOG_FILE", trade_path),
                patch.object(live_s23_bot, "STATE_FILE", state_path),
                patch.object(
                    live_s23_bot, "atomic_write_json", side_effect=fail_final_commit,
                ),
            ):
                with self.assertRaisesRegex(
                    OSError, "simulated power loss at final commit",
                ):
                    runner._sync_strategy(strategy)

        # The recovery helper normally saves when it arms.  During a confirmed
        # close transaction that write must be deferred, leaving only the one
        # complete final-state commit attempt.
        self.assertEqual(len(attempted_states), 1)
        attempted_strategy = attempted_states[0]["strategies"][strategy["id"]]
        self.assertFalse(attempted_strategy["basket"])
        self.assertTrue(attempted_states[0]["routing"]["trend_recovery"]["active"])
        self.assertEqual(runner.state, state_before)

    def test_confirmed_close_ambiguous_replace_replays_to_exactly_once_state(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state_before = json.loads(json.dumps(runner.state))
        executor.positions = []
        executor.close_deal = SimpleNamespace(
            position_id=9401, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
            net_profit=-4.25, price=99.5, deal=77021, exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-25T13:30:00Z").timestamp()),
        )
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        runner._save_state = S23HorizontalInventoryRunner._save_state.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        durable_state = {}
        fail_after_first_replace = True

        def replace_then_maybe_fail(_path, payload):
            nonlocal fail_after_first_replace
            durable_state.clear()
            durable_state.update(json.loads(json.dumps(payload)))
            if fail_after_first_replace:
                fail_after_first_replace = False
                raise OSError("simulated parent fsync failure after replace")

        with tempfile.TemporaryDirectory() as tmp:
            trade_path = os.path.join(tmp, "s23_trades.csv")
            state_path = os.path.join(tmp, "s23_bot_state.json")
            with (
                patch.object(live_s23_bot, "TRADE_LOG_FILE", trade_path),
                patch.object(live_s23_bot, "STATE_FILE", state_path),
                patch.object(
                    live_s23_bot, "atomic_write_json",
                    side_effect=replace_then_maybe_fail,
                ),
            ):
                with self.assertRaisesRegex(
                    OSError, "simulated parent fsync failure after replace",
                ) as raised:
                    runner._sync_strategy(strategy)

                # The replace itself may already have made the complete new
                # state visible even though durability confirmation failed.
                replaced_strategy = durable_state["strategies"][strategy["id"]]
                self.assertFalse(replaced_strategy["basket"])
                self.assertEqual(replaced_strategy["daily_realized_pnl_usd"], -4.25)
                self.assertEqual(runner.state, state_before)

                # Normal poll containment may rewrite the retryable old state.
                # The next broker-confirmed replay must still converge without
                # double-applying PnL or appending a duplicate deal audit row.
                runner._contain_poll_exception(raised.exception)
                retry_runner, retry_strategy, _retry_state = make_runner()
                retry_runner.state = json.loads(json.dumps(durable_state))
                retry_runner.executor = executor
                retry_runner._trade_row = (
                    S23HorizontalInventoryRunner._trade_row.__get__(
                        retry_runner, S23HorizontalInventoryRunner,
                    )
                )
                retry_runner._save_state = (
                    S23HorizontalInventoryRunner._save_state.__get__(
                        retry_runner, S23HorizontalInventoryRunner,
                    )
                )
                self.assertTrue(retry_runner._sync_strategy(retry_strategy))

            with open(trade_path, newline="", encoding="utf-8") as handle:
                trade_rows = list(csv.DictReader(handle))

        converged = durable_state["strategies"][strategy["id"]]
        self.assertFalse(converged["basket"])
        self.assertEqual(converged["daily_realized_pnl_usd"], -4.25)
        self.assertEqual(
            len([row for row in trade_rows if row["deal_id"] == "77021"]), 1,
        )

    def test_partial_confirmed_close_attempts_one_complete_state_commit(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        first = SimpleNamespace(
            ticket=96201, identifier=96201, symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC, comment=strategy["comment_prefix"],
            type=ORDER_TYPE_BUY, volume=0.01, open_time=1,
        )
        second = SimpleNamespace(
            ticket=96202, identifier=96202, symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC, comment=strategy["comment_prefix"],
            type=ORDER_TYPE_BUY, volume=0.01, open_time=2,
        )
        executor.positions = [second]
        state["basket"] = [
            {
                "ticket": position.ticket,
                "position_identifier": position.identifier,
                "side": "LONG",
                "lot": 0.01,
                "entry_price": 100.0,
                "entry_time_utc": "2026-08-25T13:00:00+00:00",
                "open_time_epoch": position.open_time,
                "owner_symbol": "XAUUSD",
                "owner_magic": EXPECTED_S23_MAGIC,
                "owner_comment": strategy["comment_prefix"],
                "shadow": False,
                "close_requested": position.ticket == 96201,
            }
            for position in (first, second)
        ]
        bind_owned_basket_identity(strategy, state)
        state.update(
            {
                "pending_close_reason": "basket_stop",
                "pending_close_signal_bar": "2026-08-25T13:10:00+00:00",
                "sync_block_new_entries": True,
                "sync_block_reason": "live_time_close_failed",
                "sync_block_recoverable": True,
            }
        )
        confirmed = SimpleNamespace(
            position_id=96201, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
            net_profit=-1.25, price=99.5, deal=796201, exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-25T13:10:02Z").timestamp()),
        )
        executor.get_position_close_deal = (
            lambda position_id, _opened_at_epoch: confirmed
            if int(position_id) == 96201 else False
        )
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        runner._save_state = S23HorizontalInventoryRunner._save_state.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        attempted_states = []
        transaction_started = False
        original_begin = runner._begin_confirmed_close_state_transaction

        def begin_and_mark(deal_id):
            nonlocal transaction_started
            state_before = original_begin(deal_id)
            transaction_started = True
            return state_before

        def capture_transaction_write(_path, payload):
            if transaction_started:
                attempted_states.append(json.loads(json.dumps(payload)))

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(
                    live_s23_bot, "TRADE_LOG_FILE",
                    os.path.join(tmp, "s23_trades.csv"),
                ),
                patch.object(
                    live_s23_bot, "STATE_FILE",
                    os.path.join(tmp, "s23_bot_state.json"),
                ),
                patch.object(
                    live_s23_bot, "atomic_write_json",
                    side_effect=capture_transaction_write,
                ),
                patch.object(
                    runner, "_begin_confirmed_close_state_transaction",
                    side_effect=begin_and_mark,
                ),
            ):
                self.assertTrue(runner._sync_strategy(strategy))

        self.assertEqual(len(attempted_states), 1)
        committed = attempted_states[0]["strategies"][strategy["id"]]
        self.assertEqual(
            [position["ticket"] for position in committed["basket"]], [96202],
        )
        self.assertFalse(committed["basket"][0].get("close_requested"))
        self.assertIsNone(committed["pending_close_reason"])
        self.assertFalse(committed["sync_block_new_entries"])

    def test_confirmed_close_derived_transition_failure_restores_state(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["pending_close_reason"] = "basket_target"
        state["pending_close_signal_bar"] = "2026-08-24T23:59:00+00:00"
        state_before = json.loads(json.dumps(runner.state))
        executor.positions = []
        executor.close_deal = SimpleNamespace(
            position_id=9401, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
            net_profit=4.25, price=100.5, deal=77014, exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-24T23:59:58Z").timestamp()),
        )

        def fail_rearm(*_args, **_kwargs):
            runner.state["routing"]["long_target_rearm_pending_confirmation"] = False
            raise OSError("portfolio rearm audit unavailable")

        runner._confirm_long_target_portfolio_rearm = fail_rearm
        runner._trade_row = lambda *_args, **_kwargs: None

        with self.assertRaisesRegex(OSError, "portfolio rearm audit unavailable"):
            runner._sync_strategy(strategy)

        self.assertEqual(runner.state, state_before)

    def test_confirmed_close_retry_does_not_duplicate_derived_rearm_audit(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["pending_close_reason"] = "basket_target"
        state["pending_close_signal_bar"] = "2026-08-24T23:59:00+00:00"
        executor.positions = []
        executor.close_deal = SimpleNamespace(
            position_id=9401, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
            net_profit=4.25, price=100.5, deal=77015, exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-24T23:59:58Z").timestamp()),
        )
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        runner._save_state = Mock(side_effect=OSError("state disk unavailable"))

        with tempfile.TemporaryDirectory() as tmp:
            trade_path = os.path.join(tmp, "s23_trades.csv")
            with patch.object(live_s23_bot, "TRADE_LOG_FILE", trade_path):
                with self.assertRaisesRegex(OSError, "state disk unavailable"):
                    runner._sync_strategy(strategy)
                runner._save_state = lambda: None
                self.assertTrue(runner._sync_strategy(strategy))
            with open(trade_path, newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(
            len([row for row in rows if row["event"] == "portfolio_rearm_started"]),
            1,
        )

    def test_confirmed_close_retry_repairs_partial_derived_audit_exactly_once(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["pending_close_reason"] = "basket_target"
        state["pending_close_signal_bar"] = "2026-08-24T23:59:00+00:00"
        pending_strategy = runner.params["strategies"][1]
        pending_state = runner._st(pending_strategy)
        pending_state.update(
            {
                "pending_entry_side": "LONG",
                "pending_entry_opportunity_id": "pending-long-retry-1",
                "pending_entry_signal_bar": "2026-08-24T23:58:00+00:00",
            }
        )
        executor.positions = []
        executor.close_deal = SimpleNamespace(
            position_id=9401, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
            net_profit=4.25, price=100.5, deal=77016, exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-24T23:59:58Z").timestamp()),
        )
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        original_append = live_s23_bot.append_csv
        failed = False

        def fail_rearm_once(path, row, fields):
            nonlocal failed
            if row.get("event") == "portfolio_rearm_started" and not failed:
                failed = True
                raise OSError("derived rearm row unavailable")
            return original_append(path, row, fields)

        with tempfile.TemporaryDirectory() as tmp:
            trade_path = os.path.join(tmp, "s23_trades.csv")
            evaluation_path = os.path.join(tmp, "s23_signal_evaluation.csv")
            with patch.object(live_s23_bot, "TRADE_LOG_FILE", trade_path), patch.object(
                live_s23_bot, "append_csv", side_effect=fail_rearm_once,
            ):
                with self.assertRaisesRegex(OSError, "derived rearm row unavailable"):
                    runner._sync_strategy(strategy)
                self.assertTrue(runner._sync_strategy(strategy))
            with open(trade_path, newline="", encoding="utf-8") as handle:
                trade_rows = list(csv.DictReader(handle))
            with open(evaluation_path, newline="", encoding="utf-8") as handle:
                evaluation_rows = list(csv.DictReader(handle))

        for rows in (trade_rows, evaluation_rows):
            self.assertEqual(
                len([row for row in rows if row["event"] == "pending_cancelled"]),
                1,
            )
            self.assertEqual(
                len([row for row in rows if row["event"] == "portfolio_rearm_started"]),
                1,
            )

    def test_confirmed_close_restart_repairs_missing_passive_derived_audit(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["pending_close_reason"] = "basket_target"
        state["pending_close_signal_bar"] = "2026-08-24T23:59:00+00:00"
        executor.positions = []
        executor.close_deal = SimpleNamespace(
            position_id=9401, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
            net_profit=4.25, price=100.5, deal=77017, exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-24T23:59:58Z").timestamp()),
        )
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        runner._signal_evaluation_enabled = False
        runner._save_state = Mock(side_effect=OSError("state disk unavailable"))

        with tempfile.TemporaryDirectory() as tmp:
            trade_path = os.path.join(tmp, "s23_trades.csv")
            evaluation_path = os.path.join(tmp, "s23_signal_evaluation.csv")
            with patch.object(live_s23_bot, "TRADE_LOG_FILE", trade_path):
                with self.assertRaisesRegex(OSError, "state disk unavailable"):
                    runner._sync_strategy(strategy)

                retry_runner, retry_strategy, _retry_state = make_runner()
                retry_runner.state = json.loads(json.dumps(runner.state))
                retry_runner.executor = executor
                retry_runner._trade_row = (
                    S23HorizontalInventoryRunner._trade_row.__get__(
                        retry_runner, S23HorizontalInventoryRunner,
                    )
                )
                retry_runner._save_state = lambda: None
                self.assertTrue(retry_runner._sync_strategy(retry_strategy))

            with open(trade_path, newline="", encoding="utf-8") as handle:
                trade_rows = list(csv.DictReader(handle))
            with open(evaluation_path, newline="", encoding="utf-8") as handle:
                evaluation_rows = list(csv.DictReader(handle))

        for event in ("position_close_confirmed", "portfolio_rearm_started"):
            self.assertEqual(
                len([row for row in trade_rows if row["event"] == event]), 1,
            )
            self.assertEqual(
                len([row for row in evaluation_rows if row["event"] == event]), 1,
            )

    def test_multi_ticket_close_audit_partial_failure_retries_without_duplicates(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor, ticket=9411)
        second = dict(state["basket"][0])
        second.update(
            {
                "ticket": 9412,
                "position_identifier": 9412,
                "entry_price": 100.5,
                "open_time_epoch": 2,
            }
        )
        state["basket"].append(second)
        executor.positions = []
        deals = {
            9411: SimpleNamespace(
                position_id=9411, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
                net_profit=-1.25, price=99.5, deal=77021, exit_volume=0.01,
                deal_time=int(pd.Timestamp("2026-08-24T23:59:57Z").timestamp()),
            ),
            9412: SimpleNamespace(
                position_id=9412, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
                net_profit=-2.75, price=99.4, deal=77022, exit_volume=0.01,
                deal_time=int(pd.Timestamp("2026-08-24T23:59:58Z").timestamp()),
            ),
        }
        executor.get_position_close_deal = (
            lambda position_id, _opened_at_epoch: deals[int(position_id)]
        )
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        original_append = live_s23_bot.append_csv
        confirmed_attempts = 0

        def fail_second_confirmed(path, row, fields):
            nonlocal confirmed_attempts
            if row.get("event") == "position_close_confirmed":
                confirmed_attempts += 1
                if confirmed_attempts == 2:
                    raise OSError("second close row unavailable")
            return original_append(path, row, fields)

        with tempfile.TemporaryDirectory() as tmp:
            trade_path = os.path.join(tmp, "s23_trades.csv")
            evaluation_path = os.path.join(tmp, "s23_signal_evaluation.csv")
            with patch.object(live_s23_bot, "TRADE_LOG_FILE", trade_path), patch.object(
                live_s23_bot, "append_csv", side_effect=fail_second_confirmed,
            ):
                with self.assertRaisesRegex(OSError, "second close row unavailable"):
                    runner._sync_strategy(strategy)
                self.assertEqual(len(state["basket"]), 2)
                self.assertEqual(state["daily_realized_pnl_usd"], 0.0)
                self.assertTrue(runner._sync_strategy(strategy))

            with open(trade_path, newline="", encoding="utf-8") as handle:
                trade_rows = list(csv.DictReader(handle))
            with open(evaluation_path, newline="", encoding="utf-8") as handle:
                evaluation_rows = list(csv.DictReader(handle))

        self.assertFalse(state["basket"])
        self.assertEqual(state["daily_realized_pnl_usd"], -4.0)
        self.assertEqual(
            sorted(row["deal_id"] for row in trade_rows), ["77021", "77022"],
        )
        self.assertEqual(
            sorted(row["deal_id"] for row in evaluation_rows),
            ["77021", "77022"],
        )

    def test_confirmed_close_deal_resolves_unresolved_submission_block(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor, ticket=96146)
        state["pending_close_reason"] = "basket_stop"
        state["pending_close_signal_bar"] = "2026-08-25T13:10:00+00:00"
        state["basket"][0]["close_submission_started_utc"] = (
            "2026-08-25T13:10:01+00:00"
        )
        state.update(
            {
                "sync_block_new_entries": True,
                "sync_block_reason": "close_submission_result_unresolved",
                "sync_block_recoverable": False,
                "sync_block_details": {"tickets": [96146]},
            }
        )
        executor.positions = []
        executor.close_deal = SimpleNamespace(
            position_id=96146,
            symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC,
            net_profit=-2.5,
            price=99.75,
            deal=79646,
            exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-25T13:10:02Z").timestamp()),
        )

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertFalse(state["basket"])
        self.assertFalse(state["sync_block_new_entries"])
        self.assertIsNone(state["sync_block_reason"])

    def test_resolved_unresolved_ticket_rearms_only_exact_owned_remainder(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor, ticket=96147)
        first_state = state["basket"][0]
        first_state["close_submission_started_utc"] = (
            "2026-08-25T13:10:01+00:00"
        )
        second_live = SimpleNamespace(
            ticket=96148,
            identifier=96148,
            symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC,
            comment=strategy["comment_prefix"],
            type=ORDER_TYPE_BUY,
            volume=0.01,
            open_price=100.5,
            open_time=int(pd.Timestamp("2026-08-25T13:01:00Z").timestamp()),
        )
        second_state = dict(first_state)
        second_state.update(
            {
                "ticket": 96148,
                "position_identifier": 96148,
                "entry_price": 100.5,
                "entry_time_utc": "2026-08-25T13:01:00+00:00",
                "open_time_epoch": second_live.open_time,
                "close_submission_started_utc": None,
                "close_requested": False,
            }
        )
        state["basket"].append(second_state)
        state["pending_close_reason"] = "basket_stop"
        state["pending_close_signal_bar"] = "2026-08-25T13:10:00+00:00"
        state.update(
            {
                "sync_block_new_entries": True,
                "sync_block_reason": "close_submission_result_unresolved",
                "sync_block_recoverable": False,
                "sync_block_details": {"tickets": [96147]},
            }
        )
        executor.positions = [second_live]
        executor.close_deal = SimpleNamespace(
            position_id=96147,
            symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC,
            net_profit=-1.5,
            price=99.8,
            deal=79647,
            exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-25T13:10:02Z").timestamp()),
        )

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertEqual(
            [position["ticket"] for position in state["basket"]], [96148]
        )
        self.assertFalse(state["sync_block_new_entries"])
        self.assertIsNone(state["pending_close_reason"])
        self.assertEqual(executor.close_calls, [])

    def test_unresolved_block_without_marker_cannot_rearm_on_owned_sync_alone(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor, ticket=96149)
        state["pending_close_reason"] = "basket_stop"
        state["pending_close_signal_bar"] = "2026-08-25T13:10:00+00:00"
        state.update(
            {
                "sync_block_new_entries": True,
                "sync_block_reason": "close_submission_result_unresolved",
                "sync_block_recoverable": False,
                "sync_block_details": {"tickets": [96149]},
            }
        )

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(
            state["sync_block_reason"], "close_submission_result_unresolved"
        )
        self.assertEqual(state["pending_close_reason"], "basket_stop")
        self.assertEqual(executor.close_calls, [])

    def test_confirmed_close_with_orders_unavailable_downgrades_only_to_order_block(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor(orders_available=False)
        runner.executor = executor
        arm_owned_basket(strategy, state, executor, ticket=96150)
        state["pending_close_reason"] = "basket_stop"
        state["pending_close_signal_bar"] = "2026-08-25T13:10:00+00:00"
        state["basket"][0]["close_submission_started_utc"] = (
            "2026-08-25T13:10:01+00:00"
        )
        state.update(
            {
                "sync_block_new_entries": True,
                "sync_block_reason": "close_submission_result_unresolved",
                "sync_block_recoverable": False,
                "sync_block_details": {"tickets": [96150]},
            }
        )
        executor.positions = []
        executor.close_deal = SimpleNamespace(
            position_id=96150,
            symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC,
            net_profit=-1.0,
            price=99.9,
            deal=79650,
            exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-25T13:10:02Z").timestamp()),
        )

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertFalse(state["basket"])
        self.assertEqual(state["sync_block_reason"], "orders_unavailable")
        self.assertTrue(state["sync_block_recoverable"])

        executor.orders_available = True
        self.assertTrue(runner._sync_strategy(strategy))
        self.assertFalse(state["sync_block_new_entries"])

    def test_confirmed_close_ledger_preserves_entry_opportunity_id(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state["basket"][0]["opportunity_id"] = "XAUUSD|2026-08-25T13:00:00+00:00|TEST|LONG"
        executor.positions = []
        executor.close_deal = SimpleNamespace(
            position_id=9401,
            symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC,
            net_profit=3.25,
            price=101.25,
            deal=77001,
            exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-25 13:45:00", tz="UTC").timestamp()),
        )
        rows = []
        runner._trade_row = lambda event, _strategy, **kwargs: rows.append((event, kwargs))

        self.assertTrue(runner._sync_strategy(strategy))

        confirmed = [kwargs for event, kwargs in rows if event == "position_close_confirmed"]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(
            confirmed[0]["opportunity_id"],
            "XAUUSD|2026-08-25T13:00:00+00:00|TEST|LONG",
        )

    def test_close_deal_without_broker_timestamp_keeps_owned_state_fail_closed(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        executor.positions = []
        executor.close_deal = SimpleNamespace(
            position_id=9401,
            symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC,
            net_profit=3.25,
            price=101.25,
            deal=77002,
            exit_volume=0.01,
            deal_time=0,
        )

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(len(state["basket"]), 1)
        self.assertEqual(state["sync_block_reason"], "close_deal_timestamp_invalid")

        state["basket"][0]["open_time_epoch"] = int(
            pd.Timestamp("2026-08-25T13:00:00Z").timestamp()
        )
        executor.close_deal = SimpleNamespace(
            position_id=9401,
            symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC,
            net_profit=3.25,
            price=101.25,
            deal=77002,
            exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-25T12:59:59Z").timestamp()),
        )
        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(len(state["basket"]), 1)
        self.assertEqual(state["sync_block_reason"], "close_deal_timestamp_invalid")

        executor.close_deal = SimpleNamespace(
            position_id=9401,
            symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC,
            net_profit=3.25,
            price=101.25,
            deal=77002,
            exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-25T13:45:00Z").timestamp()),
        )
        self.assertTrue(runner._sync_strategy(strategy))
        self.assertFalse(state["basket"])
        self.assertFalse(state["sync_block_new_entries"])
        self.assertIsNone(state["sync_block_reason"])

    def test_close_deal_with_invalid_execution_payload_keeps_owned_state_fail_closed(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        executor.positions = []
        executor.close_deal = SimpleNamespace(
            position_id=9401,
            symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC,
            net_profit=float("nan"),
            price=0.0,
            deal=0,
            exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-25T13:45:00Z").timestamp()),
        )

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(len(state["basket"]), 1)
        self.assertEqual(state["sync_block_reason"], "close_deal_payload_invalid")
        self.assertEqual(state["daily_realized_pnl_usd"], 0.0)

    def test_live_target_requests_close_then_waits_for_deal_confirmation(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        poll_time = pd.Timestamp("2026-08-25 13:10:00", tz="UTC")
        row = pd.Series({"Open": 111.0, "Close": 111.0, "AskOpen": 111.03}, name=poll_time)

        self.assertTrue(runner._monitor_open_basket(strategy, SimpleNamespace(bid=111.0, ask=111.03), row, poll_time))
        self.assertEqual(executor.close_calls, [9401])
        self.assertEqual(state["pending_close_reason"], "basket_target")
        self.assertEqual(len(state["basket"]), 1, "state must retain inventory until the close deal is confirmed")
        self.assertTrue(runner.state["routing"]["long_target_rearm_pending_confirmation"])
        self.assertIsNone(runner.state["routing"]["long_target_rearm_until_utc"])

        executor.positions = []
        executor.close_deal = SimpleNamespace(
            position_id=9401,
            symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC,
            net_profit=10.75,
            price=111.0,
            deal=77003,
            exit_volume=0.01,
            deal_time=int((poll_time + pd.Timedelta(seconds=2)).timestamp()),
        )
        self.assertTrue(runner._sync_strategy(strategy))
        self.assertFalse(state["basket"])
        self.assertEqual(state["daily_realized_pnl_usd"], 10.75)
        routing = runner.state["routing"]
        self.assertFalse(routing["long_target_rearm_pending_confirmation"])
        self.assertEqual(parse_ts(routing["long_target_rearm_confirmed_utc"]), poll_time + pd.Timedelta(seconds=2))
        self.assertEqual(parse_ts(routing["long_target_rearm_until_utc"]), poll_time + pd.Timedelta(minutes=8, seconds=2))

    def test_target_rearm_cancels_all_pending_longs_but_preserves_pending_short(self):
        runner, strategy, state = make_runner(live=False)
        strategies = runner.params["strategies"]
        state["basket"] = [{"side": "LONG"}]
        state["current_basket_id"] = "basket-trigger"
        for other in strategies[1:3]:
            arm_pending(runner._st(other))
        short_state = runner._st(strategies[3])
        arm_pending(short_state)
        short_state["pending_entry_side"] = "SHORT"

        runner._arm_long_target_portfolio_rearm(strategy, pd.Timestamp("2026-08-25 13:10:00", tz="UTC"))

        self.assertTrue(runner.state["routing"]["long_target_rearm_pending_confirmation"])
        self.assertIsNone(runner._st(strategies[1])["pending_entry_side"])
        self.assertIsNone(runner._st(strategies[2])["pending_entry_side"])
        self.assertEqual(short_state["pending_entry_side"], "SHORT")

    def test_active_rearm_blocks_only_new_long_baskets(self):
        runner, strategy, state = make_runner(live=False)
        start = pd.Timestamp("2026-08-25 13:10:00", tz="UTC")
        runner.state["routing"].update({
            "long_target_rearm_confirmed_utc": dt_text(start),
            "long_target_rearm_until_utc": dt_text(start + pd.Timedelta(minutes=8)),
            "long_target_rearm_trigger_lane_id": 1,
            "long_target_rearm_trigger_basket_id": "L1-B000001",
        })
        row = pd.Series({"Open": 100.0, "Close": 100.0, "AskOpen": 100.03}, name=start)
        info = SimpleNamespace(bid=100.0, ask=100.03)

        self.assertFalse(runner._open_entry(strategy, "LONG", row, info, execution_time=start, basket_atr30=2.0))
        self.assertFalse(state["basket"])
        self.assertTrue(runner._open_entry(strategy, "SHORT", row, info, execution_time=start, basket_atr30=2.0))
        self.assertEqual(state["basket"][0]["side"], "SHORT")

    def test_short_target_confirmation_does_not_arm_or_invalidate_long_rearm(self):
        runner, strategy, state = make_runner(live=False)
        state.update({
            "basket": [{"side": "SHORT"}],
            "current_basket_id": "L1-B000001",
            "pending_close_reason": "basket_target",
            "pending_close_signal_bar": "2026-08-25T13:10:00+00:00",
        })
        at = pd.Timestamp("2026-08-25T13:10:01Z")

        self.assertIsNone(
            runner._portfolio_new_long_basket_block_reason("LONG", at),
        )
        routing = runner.state["routing"]
        self.assertFalse(routing["long_target_rearm_pending_confirmation"])
        self.assertIsNone(routing["long_target_rearm_request_utc"])

    def test_short_target_pending_does_not_pollute_confirmed_long_active_rearm(self):
        runner, first, first_state = make_runner(live=False)
        second = runner.params["strategies"][1]
        second_state = runner._st(second)
        first_state.update({
            "basket": [{"side": "LONG"}],
            "current_basket_id": "L1-B000001",
            "pending_close_reason": "basket_target",
            "pending_close_signal_bar": "2026-08-25T13:10:00+00:00",
        })
        second_state.update({
            "basket": [{"side": "SHORT"}],
            "current_basket_id": "L2-B000001",
            "pending_close_reason": "basket_target",
            "pending_close_signal_bar": "2026-08-25T13:10:01+00:00",
        })
        confirmed = pd.Timestamp("2026-08-25T13:10:02Z")

        runner._arm_long_target_portfolio_rearm(first, confirmed)
        runner._confirm_long_target_portfolio_rearm(
            first, confirmed, "L1-B000001",
        )
        runner._clear_basket_state(
            first,
            "basket_target",
            "2026-08-25T13:10:00+00:00",
            closed_at_utc=confirmed,
        )

        routing = runner.state["routing"]
        self.assertFalse(routing["long_target_rearm_pending_confirmation"])
        self.assertEqual(
            runner._portfolio_new_long_basket_block_reason(
                "LONG", confirmed + pd.Timedelta(seconds=1),
            ),
            "long_target_portfolio_rearm",
        )
        self.assertEqual(
            parse_ts(routing["long_target_rearm_until_utc"]),
            confirmed + pd.Timedelta(minutes=8),
        )

    def test_malformed_target_basket_still_fails_closed_for_long_rearm(self):
        for label, basket in (
            ("empty", []),
            ("mixed", [{"side": "LONG"}, {"side": "SHORT"}]),
            ("invalid_side", [{"side": "UNKNOWN"}]),
        ):
            with self.subTest(label=label):
                runner, _strategy, state = make_runner(live=False)
                state.update({
                    "basket": basket,
                    "current_basket_id": "L1-B000001",
                    "pending_close_reason": "basket_target",
                    "pending_close_signal_bar": "2026-08-25T13:10:00+00:00",
                })

                self.assertEqual(
                    runner._portfolio_new_long_basket_block_reason(
                        "LONG", pd.Timestamp("2026-08-25T13:10:01Z"),
                    ),
                    "long_target_rearm_state_invalid",
                )

    def test_active_rearm_does_not_block_existing_long_add(self):
        runner, strategy, state = make_runner(live=False)
        start = pd.Timestamp("2026-08-25 13:10:00", tz="UTC")
        runner.state["routing"].update({
            "long_target_rearm_confirmed_utc": dt_text(start),
            "long_target_rearm_until_utc": dt_text(start + pd.Timedelta(minutes=8)),
            "long_target_rearm_trigger_lane_id": 1,
            "long_target_rearm_trigger_basket_id": "L1-B000001",
        })
        state["basket"] = [{"side": "LONG", "lot": 0.01, "entry_price": 100.0}]
        bind_owned_basket_identity(strategy, state)
        state["last_add_price"] = 100.0
        opportunity, row, _poll_time, info = sample_opportunity(side="LONG")
        row["Close"] = 102.0
        info = SimpleNamespace(bid=102.0, ask=102.03)

        consumed, reason = runner._consume_opportunity(strategy, opportunity, row, info, start)

        self.assertTrue(consumed)
        self.assertEqual(reason, "add_attempted")
        self.assertEqual(len(state["basket"]), 2)

    def test_non_target_full_close_cancels_stale_unconfirmed_target_rearm(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        basket_id = state["current_basket_id"]
        runner._arm_long_target_portfolio_rearm(
            strategy, pd.Timestamp("2026-08-25T13:10:00Z"),
        )
        self.assertTrue(
            runner.state["routing"]["long_target_rearm_pending_confirmation"],
        )

        state["pending_close_reason"] = "basket_stop"
        state["pending_close_signal_bar"] = "2026-08-25T13:11:00+00:00"
        state["basket"][0]["close_requested"] = True
        executor.positions = []
        executor.close_deal = SimpleNamespace(
            position_id=9401,
            symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC,
            net_profit=-2.0,
            price=99.0,
            deal=79401,
            exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-25T13:11:02Z").timestamp()),
        )

        self.assertTrue(runner._sync_strategy(strategy))
        routing = runner.state["routing"]
        self.assertFalse(routing["long_target_rearm_pending_confirmation"])
        self.assertIsNone(routing["long_target_rearm_request_utc"])
        self.assertIsNone(routing["long_target_rearm_trigger_lane_id"])
        self.assertIsNone(routing["long_target_rearm_trigger_basket_id"])
        self.assertFalse(state["basket"])
        self.assertEqual(state["last_closed_reason"], "basket_stop")
        self.assertEqual(basket_id, "L1-B000001")

    def test_concurrent_target_close_keeps_other_basket_pending_after_first_confirmation(self):
        runner, first, first_state = make_runner(live=False)
        second = runner.params["strategies"][1]
        second_state = runner._st(second)
        first_state.update({
            "basket": [{"side": "LONG"}],
            "current_basket_id": "L1-B000001",
            "pending_close_reason": "basket_target",
            "pending_close_signal_bar": "2026-08-25T13:10:00+00:00",
        })
        second_state.update({
            "basket": [{"side": "LONG"}],
            "current_basket_id": "L2-B000001",
            "pending_close_reason": "basket_target",
            "pending_close_signal_bar": "2026-08-25T13:10:01+00:00",
        })
        start = pd.Timestamp("2026-08-25T13:10:00Z")

        runner._arm_long_target_portfolio_rearm(first, start)
        runner._arm_long_target_portfolio_rearm(second, start + pd.Timedelta(seconds=1))
        runner._confirm_long_target_portfolio_rearm(
            first, start + pd.Timedelta(seconds=2), "L1-B000001",
        )

        routing = runner.state["routing"]
        self.assertTrue(routing["long_target_rearm_pending_confirmation"])
        self.assertEqual(
            runner._portfolio_new_long_basket_block_reason(
                "LONG", start + pd.Timedelta(seconds=3),
            ),
            "long_target_rearm_pending_close_confirmation",
        )
        self.assertEqual(routing["long_target_rearm_trigger_lane_id"], 1)
        self.assertEqual(routing["long_target_rearm_trigger_basket_id"], "L1-B000001")

        runner._clear_basket_state(
            first,
            "basket_target",
            "2026-08-25T13:10:00+00:00",
            closed_at_utc=start + pd.Timedelta(seconds=2),
        )
        second_state["pending_close_reason"] = "basket_stop"

        runner._cancel_unconfirmed_long_target_rearm_after_other_close(
            second, "L2-B000001", "basket_stop",
        )
        self.assertFalse(routing["long_target_rearm_pending_confirmation"])
        self.assertEqual(
            runner._portfolio_new_long_basket_block_reason(
                "LONG", start + pd.Timedelta(seconds=4),
            ),
            "long_target_portfolio_rearm",
        )
        self.assertEqual(routing["long_target_rearm_trigger_lane_id"], 1)
        self.assertEqual(routing["long_target_rearm_trigger_basket_id"], "L1-B000001")

    def test_target_market_closed_with_no_submission_clears_unconfirmed_rearm(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        executor.close_position = lambda _ticket, _deviation, **_kwargs: live_executor.CloseResult(
            False, "MARKET_CLOSED", retcode=10018,
        )
        close_time = pd.Timestamp("2026-08-25T13:10:00Z")
        row = pd.Series(
            {"Open": 110.0, "Close": 110.0, "AskOpen": 110.03},
            name=close_time,
        )

        self.assertEqual(
            runner._close_basket(strategy, "basket_target", row, 10.0),
            "market_closed",
        )
        routing = runner.state["routing"]
        self.assertFalse(routing["long_target_rearm_pending_confirmation"])
        self.assertIsNone(routing["long_target_rearm_request_utc"])
        self.assertIsNone(state["pending_close_reason"])
        self.assertFalse(state["basket"][0].get("close_requested"))

    def test_mixed_target_close_market_closed_preserves_submitted_ticket_intent(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        first = SimpleNamespace(
            ticket=9651, identifier=9651, symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC, comment=strategy["comment_prefix"],
            type=ORDER_TYPE_BUY, volume=0.01, open_price=100.0, open_time=1,
        )
        second = SimpleNamespace(**vars(first))
        second.ticket = 9652
        second.identifier = 9652
        executor.positions = [first, second]
        state["basket"] = [
            {
                "ticket": position.ticket,
                "position_identifier": position.identifier,
                "side": "LONG",
                "lot": 0.01,
                "entry_price": 100.0,
                "entry_time_utc": "2026-08-25T13:00:00+00:00",
                "open_time_epoch": 1,
                "owner_symbol": "XAUUSD",
                "owner_magic": EXPECTED_S23_MAGIC,
                "owner_comment": strategy["comment_prefix"],
                "shadow": False,
                "close_requested": False,
            }
            for position in (first, second)
        ]
        bind_owned_basket_identity(strategy, state)
        results = [
            live_executor.CloseResult(True, "CONFIRMED", deal_id=89651, retcode=10009),
            live_executor.CloseResult(False, "MARKET_CLOSED", retcode=10018),
        ]
        executor.close_position = lambda _ticket, _deviation, **_kwargs: results.pop(0)
        close_time = pd.Timestamp("2026-08-25T13:10:00Z")
        row = pd.Series(
            {"Open": 110.0, "Close": 110.0, "AskOpen": 110.03},
            name=close_time,
        )

        self.assertEqual(
            runner._close_basket(strategy, "basket_target", row, 10.0),
            "market_closed",
        )
        self.assertTrue(state["basket"][0]["close_requested"])
        self.assertFalse(state["basket"][1]["close_requested"])
        self.assertEqual(state["pending_close_reason"], "basket_target")
        self.assertEqual(parse_ts(state["pending_close_signal_bar"]), close_time)
        self.assertTrue(
            runner.state["routing"]["long_target_rearm_pending_confirmation"],
        )

    def test_retried_target_market_closed_preserves_prior_submitted_ticket_intent(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        positions = [
            SimpleNamespace(
                ticket=ticket, identifier=ticket, symbol="XAUUSD",
                magic=EXPECTED_S23_MAGIC, comment=strategy["comment_prefix"],
                type=ORDER_TYPE_BUY, volume=0.01, open_price=100.0, open_time=1,
            )
            for ticket in (9661, 9662)
        ]
        executor.positions = positions
        state["basket"] = [
            {
                "ticket": position.ticket,
                "position_identifier": position.identifier,
                "side": "LONG",
                "lot": 0.01,
                "entry_price": 100.0,
                "entry_time_utc": "2026-08-25T13:00:00+00:00",
                "open_time_epoch": 1,
                "owner_symbol": "XAUUSD",
                "owner_magic": EXPECTED_S23_MAGIC,
                "owner_comment": strategy["comment_prefix"],
                "shadow": False,
                "close_requested": position.ticket == 9661,
            }
            for position in positions
        ]
        bind_owned_basket_identity(strategy, state)
        state["pending_close_reason"] = "basket_target"
        state["pending_close_signal_bar"] = "2026-08-25T13:09:00+00:00"
        runner._arm_long_target_portfolio_rearm(
            strategy, pd.Timestamp("2026-08-25T13:09:02Z"),
        )
        executor.close_position = lambda _ticket, _deviation, **_kwargs: live_executor.CloseResult(
            False, "MARKET_CLOSED", retcode=10018,
        )
        close_time = pd.Timestamp("2026-08-25T13:10:00Z")
        row = pd.Series(
            {"Open": 110.0, "Close": 110.0, "AskOpen": 110.03},
            name=close_time,
        )

        self.assertEqual(
            runner._close_basket(strategy, "basket_target", row, 10.0),
            "market_closed",
        )
        self.assertTrue(state["basket"][0]["close_requested"])
        self.assertFalse(state["basket"][1]["close_requested"])
        self.assertEqual(state["pending_close_reason"], "basket_target")
        self.assertTrue(
            runner.state["routing"]["long_target_rearm_pending_confirmation"],
        )

    def test_older_target_confirmation_cannot_shorten_active_portfolio_rearm(self):
        runner, first, _first_state = make_runner(live=False)
        second = runner.params["strategies"][1]
        newer = pd.Timestamp("2026-08-25T13:10:00Z")
        older = pd.Timestamp("2026-08-25T13:05:00Z")

        runner._confirm_long_target_portfolio_rearm(
            first, newer, "L1-B000001",
        )
        runner._confirm_long_target_portfolio_rearm(
            second, older, "L2-B000001",
        )

        routing = runner.state["routing"]
        self.assertEqual(
            parse_ts(routing["long_target_rearm_confirmed_utc"]), newer,
        )
        self.assertEqual(
            parse_ts(routing["long_target_rearm_until_utc"]),
            newer + pd.Timedelta(minutes=8),
        )
        self.assertEqual(routing["long_target_rearm_trigger_lane_id"], 1)
        self.assertEqual(routing["long_target_rearm_trigger_basket_id"], "L1-B000001")

    def test_invalid_rearm_timestamp_fails_closed_for_new_long_only(self):
        for label, value in (
            ("bad_text", "not-a-timestamp"),
            ("empty_text", ""),
            ("falsey_integer", 0),
            ("valid_but_out_of_bound_future", "2026-08-25T14:00:00+00:00"),
        ):
            with self.subTest(label=label):
                runner, _strategy, _state = make_runner(live=False)
                runner.state["routing"]["long_target_rearm_until_utc"] = value
                at = pd.Timestamp("2026-08-25 13:10:00", tz="UTC")

                self.assertEqual(runner._portfolio_new_long_basket_block_reason("LONG", at), "long_target_rearm_state_invalid")
                self.assertIsNone(runner._portfolio_new_long_basket_block_reason("SHORT", at))

    def test_malformed_rearm_confirmation_flag_fails_closed_for_new_long(self):
        for label, value in (("falsey_integer", 0), ("truthy_string", "false")):
            with self.subTest(label=label):
                runner, _strategy, _state = make_runner(live=False)
                runner.state["routing"]["long_target_rearm_pending_confirmation"] = value

                self.assertEqual(
                    runner._portfolio_new_long_basket_block_reason(
                        "LONG", pd.Timestamp("2026-08-25 13:10:00Z")
                    ),
                    "long_target_rearm_state_invalid",
                )

        runner, _strategy, _state = make_runner(live=False)
        runner.state["routing"]["long_target_rearm_pending_confirmation"] = True
        self.assertEqual(
            runner._portfolio_new_long_basket_block_reason(
                "LONG", pd.Timestamp("2026-08-25 13:10:00Z")
            ),
            "long_target_rearm_state_invalid",
        )

    def test_nonrecoverable_sync_block_is_not_cleared_by_owned_inventory_sync(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state.update(
            {
                "sync_block_new_entries": True,
                "sync_block_reason": "state_position_ownership_mismatch",
                "sync_block_recoverable": False,
            }
        )

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertTrue(state["sync_block_new_entries"])
        self.assertEqual(state["sync_block_reason"], "state_position_ownership_mismatch")

    def test_malformed_recoverable_flag_cannot_clear_nonrecoverable_owned_block(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state.update(
            {
                "sync_block_new_entries": True,
                "sync_block_reason": "state_position_ownership_mismatch",
                "sync_block_recoverable": "false",
            }
        )

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertTrue(state["sync_block_new_entries"])
        self.assertEqual(
            state["sync_block_reason"], "sync_block_state_invalid",
        )
        self.assertFalse(state["sync_block_recoverable"])
        self.assertIn(
            "state_position_ownership_mismatch",
            state["sync_block_details"]["previous_reason"],
        )

    def test_malformed_sync_and_pending_open_flags_fail_closed_before_open(self):
        now = pd.Timestamp("2026-08-25T13:10:00Z")
        corruptions = (
            ("sync_block_falsey_integer", {"sync_block_new_entries": 0}, "sync_block_state_invalid"),
            ("sync_block_falsey_text", {"sync_block_new_entries": ""}, "sync_block_state_invalid"),
            ("pending_open_falsey_integer", {"pending_open_opportunity_id": 0}, "pending_open_state_invalid"),
            ("pending_open_empty_text", {"pending_open_opportunity_id": ""}, "pending_open_state_invalid"),
        )
        for label, mutation, expected in corruptions:
            with self.subTest(label=label):
                runner, strategy, state = make_runner(live=True)
                state.update(mutation)

                self.assertEqual(
                    runner._entry_submission_block_reason(strategy, now),
                    expected,
                )

    def test_malformed_nonrecoverable_block_cannot_be_downgraded_by_transient_failure(self):
        runner, strategy, state = make_runner(live=True)
        state.update({
            "sync_block_new_entries": True,
            "sync_block_reason": "state_position_ownership_mismatch",
            "sync_block_recoverable": "false",
        })

        runner._set_sync_block(strategy, "positions_unavailable", recoverable=True)

        self.assertTrue(state["sync_block_new_entries"])
        self.assertFalse(state["sync_block_recoverable"])
        self.assertEqual(state["sync_block_reason"], "sync_block_state_invalid")

    def test_confirmed_close_cannot_clear_malformed_nonrecoverable_block(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state.update({
            "sync_block_new_entries": True,
            "sync_block_reason": "state_position_ownership_mismatch",
            "sync_block_recoverable": "false",
        })
        executor.positions = []
        executor.close_deal = SimpleNamespace(
            position_id=9401,
            symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC,
            net_profit=1.0,
            price=101.0,
            deal=79680,
            exit_volume=0.01,
            deal_time=int(pd.Timestamp("2026-08-26T01:00:00Z").timestamp()),
        )

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertFalse(state["basket"])
        self.assertTrue(state["sync_block_new_entries"])
        self.assertFalse(state["sync_block_recoverable"])
        self.assertEqual(state["sync_block_reason"], "sync_block_state_invalid")

    def test_orders_unavailable_blocks_pending_and_final_open(self):
        now = pd.Timestamp("2026-08-25T13:10:00Z")
        runner, strategy, state = make_runner()
        executor = CountingExecutor(orders_available=False)
        runner.executor = executor
        arm_pending(state, now=now)

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(state["sync_block_reason"], "orders_unavailable")
        quote = SimpleNamespace(bid=99.99, ask=100.0)
        self.assertTrue(runner._monitor_pending_entry(strategy, quote, now))
        row = pd.Series({"Open": 99.99, "Close": 99.99, "AskOpen": 100.0}, name=now)
        runner._open_entry(strategy, "LONG", row, quote, basket_atr30=1.5)
        self.assertEqual(executor.open_calls, 0)
        self.assertFalse(state["basket"])

        executor.orders_available = True
        self.assertTrue(runner._sync_strategy(strategy))
        self.assertFalse(state["sync_block_new_entries"])

    def test_orders_unavailable_keeps_owned_basket_monitoring_but_blocks_entry(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor(orders_available=False)
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        executor.get_symbol_info = lambda *_args: SimpleNamespace(bid=100.0, ask=100.03)
        bars = pd.DataFrame(
            {"Open": [100.0, 100.0], "Close": [100.0, 100.0], "AskOpen": [100.03, 100.03]},
            index=pd.date_range("2026-08-25 14:55:00", periods=2, freq="1min", tz="UTC"),
        )
        runner._get_m1 = lambda: bars.copy()

        with patch.object(runner, "_monitor_open_basket", return_value=False) as monitor:
            runner.run_once()

        self.assertEqual(monitor.call_count, 1)
        self.assertEqual(monitor.call_args.args[0]["lane_id"], 1)
        self.assertTrue(state["sync_block_new_entries"])
        self.assertEqual(state["sync_block_reason"], "orders_unavailable")
        self.assertEqual(executor.open_calls, 0)

    def test_same_magic_unexpected_order_blocks_pending(self):
        now = pd.Timestamp("2026-08-25T13:10:00Z")
        foreign_order = SimpleNamespace(
            ticket=9301,
            identifier=9301,
            symbol="XAUUSD",
            magic=EXPECTED_S23_MAGIC,
            comment="s22_foreign",
            type=ORDER_TYPE_BUY,
        )
        runner, strategy, state = make_runner()
        executor = CountingExecutor(orders=[foreign_order])
        runner.executor = executor
        arm_pending(state, now=now)

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(state["sync_block_reason"], "same_magic_unexpected_order")
        self.assertTrue(runner._monitor_pending_entry(strategy, SimpleNamespace(bid=99.99, ask=100.0), now))
        self.assertEqual(executor.open_calls, 0)

    def test_retry_cooldown_blocks_pending_and_final_open(self):
        now = pd.Timestamp("2026-08-25T13:10:00Z")
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_pending(state, now=now)
        state["open_retry_after_utc"] = dt_text(now + pd.Timedelta(seconds=30))
        quote = SimpleNamespace(bid=99.99, ask=100.0)

        self.assertTrue(runner._monitor_pending_entry(strategy, quote, now + pd.Timedelta(seconds=5)))
        row = pd.Series({"Open": 99.99, "Close": 99.99, "AskOpen": 100.0}, name=now)
        runner._open_entry(strategy, "LONG", row, quote, basket_atr30=1.5, execution_time=now + pd.Timedelta(seconds=5))
        self.assertEqual(executor.open_calls, 0)
        self.assertFalse(state["basket"])

    def test_malformed_basket_sequence_blocks_before_live_open(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        opportunity, row, poll_time, info = sample_opportunity()

        for lane in runner._all_strategies():
            lane_state = runner._st(lane)
            lane_state["basket_sequence"] = "not-an-integer"
            self.assertEqual(
                runner._entry_submission_block_reason(lane, poll_time),
                "basket_sequence_state_invalid",
            )
            lane_state["basket_sequence"] = 0

        arm_owned_basket(strategy, state, executor)
        state["basket_sequence"] = 0
        state["current_basket_id"] = "L1-B000000"
        state["basket"][0]["basket_id"] = "L1-B000000"
        self.assertEqual(
            runner._entry_submission_block_reason(strategy, poll_time),
            "basket_sequence_state_invalid",
        )
        state["basket"] = []
        state["current_basket_id"] = None

        state["basket_sequence"] = "not-an-integer"

        self.assertFalse(
            runner._open_entry(
                strategy,
                "LONG",
                row,
                info,
                basket_atr30=1.5,
                execution_time=poll_time,
                opportunity=opportunity,
            )
        )
        self.assertEqual(executor.open_calls, 0)
        self.assertFalse(state["basket"])

    def test_final_open_guard_blocks_at_lane_capacity(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        first_state = dict(state["basket"][0])
        second_state = dict(first_state)
        second_state.update({"ticket": 9402, "position_identifier": 9402})
        state["basket"].append(second_state)
        opportunity, row, poll_time, info = sample_opportunity(side="LONG")

        self.assertEqual(
            runner._entry_submission_block_reason(strategy, poll_time),
            "lane_capacity_full",
        )
        self.assertFalse(
            runner._open_entry(
                strategy,
                "LONG",
                row,
                info,
                basket_atr30=1.5,
                execution_time=poll_time,
                opportunity=opportunity,
            )
        )
        self.assertEqual(executor.open_calls, 0)
        self.assertEqual(len(state["basket"]), 2)

        state["basket"][1].update({"ticket": 9401, "position_identifier": 9401})
        self.assertEqual(
            runner._entry_submission_block_reason(strategy, poll_time),
            "state_position_identity_invalid",
        )

    def test_final_open_guard_rejects_opposite_side_add(self):
        runner, strategy, state = make_runner(live=False)
        state["basket"] = [{"side": "SHORT", "lot": 0.01, "entry_price": 100.0}]
        bind_owned_basket_identity(strategy, state)
        opportunity, row, poll_time, info = sample_opportunity(side="LONG")

        self.assertFalse(
            runner._open_entry(
                strategy,
                "LONG",
                row,
                info,
                basket_atr30=1.5,
                execution_time=poll_time,
                opportunity=opportunity,
            )
        )
        self.assertEqual(len(state["basket"]), 1)
        self.assertEqual(state["basket"][0]["side"], "SHORT")

    def test_final_open_guard_rejects_invalid_side_before_order_mapping(self):
        for invalid_side in ("INVALID", None, 1, True, []):
            with self.subTest(invalid_side=invalid_side):
                runner, strategy, state = make_runner(live=True)
                executor = CountingExecutor()
                runner.executor = executor
                opportunity, row, poll_time, info = sample_opportunity(side="LONG")

                self.assertFalse(
                    runner._open_entry(
                        strategy,
                        invalid_side,
                        row,
                        info,
                        basket_atr30=1.5,
                        execution_time=poll_time,
                        opportunity=opportunity,
                    )
                )
                self.assertEqual(executor.open_calls, 0)
                self.assertFalse(state["basket"])

    def test_malformed_trade_permission_state_blocks_before_live_open(self):
        corruptions = (
            {"autotrading_reject_streak": "not-an-integer"},
            {"autotrading_reject_streak": 2, "autotrading_reject_notified": "not-a-boolean"},
        )
        for corruption in corruptions:
            with self.subTest(corruption=corruption):
                runner, strategy, state = make_runner(live=True)
                executor = CountingExecutor()
                executor.last_order_error = "ERR|10027|DEAL=0"
                runner.executor = executor
                state.update(corruption)
                opportunity, row, poll_time, info = sample_opportunity()

                self.assertEqual(
                    runner._entry_submission_block_reason(strategy, poll_time),
                    "trade_permission_state_invalid",
                )
                self.assertFalse(
                    runner._open_entry(
                        strategy,
                        "LONG",
                        row,
                        info,
                        basket_atr30=1.5,
                        execution_time=poll_time,
                        opportunity=opportunity,
                    )
                )
                self.assertEqual(executor.open_calls, 0)
                self.assertFalse(state["basket"])

    def test_malformed_persisted_open_retry_time_fails_closed(self):
        now = pd.Timestamp("2026-08-25T13:10:00Z")
        for label, value in (
            ("bad_text", "not-a-timestamp"),
            ("falsey_integer", 0),
            ("boolean", False),
            ("valid_but_out_of_bound_future", dt_text(now + pd.Timedelta(days=1))),
        ):
            with self.subTest(label=label):
                runner, strategy, state = make_runner()
                state["open_retry_after_utc"] = value

                self.assertEqual(
                    runner._entry_submission_block_reason(strategy, now),
                    "open_retry_state_invalid",
                )

    def test_high_vol_refreshed_pending_ignores_low_vol_spread_gate(self):
        runner, strategy, state = make_runner(live=False)
        now = pd.Timestamp("2026-08-25T13:10:00Z")
        arm_pending(state, atr30=2.5, target=100.0, now=now)
        quote = SimpleNamespace(bid=99.70, ask=100.0)

        self.assertTrue(runner._monitor_pending_entry(strategy, quote, now))
        self.assertEqual(len(state["basket"]), 1)
        self.assertTrue(math.isclose(float(state["frozen_basket_atr30"]), 2.5))

    def test_pending_za_entry_cannot_fill_after_blocked_hour_begins(self):
        runner, strategy, state = make_runner(live=False)
        poll_time = pd.Timestamp("2026-08-25T14:00:01Z")
        arm_pending(state, atr30=2.5, target=100.0, now=poll_time)

        self.assertTrue(
            runner._monitor_pending_entry(
                strategy,
                SimpleNamespace(bid=99.70, ask=100.0),
                poll_time,
            )
        )
        self.assertFalse(state["basket"])
        self.assertIsNone(state["pending_entry_side"])

    def test_pending_za_entry_cannot_fill_before_release_time(self):
        runner, strategy, state = make_runner(live=False)
        release_time = pd.Timestamp("2026-08-25T13:10:00Z")
        arm_pending(
            state, atr30=2.5, target=100.0, now=release_time,
        )
        poll_time = release_time - pd.Timedelta(seconds=1)

        self.assertTrue(
            runner._monitor_pending_entry(
                strategy,
                SimpleNamespace(bid=99.70, ask=100.0),
                poll_time,
            )
        )
        self.assertFalse(state["basket"])
        self.assertIsNone(state["pending_entry_side"])

    def test_final_za_open_guard_rechecks_daily_loss_before_submission(self):
        runner, strategy, state = make_runner(live=False)
        state["daily_realized_date_utc"] = "2026-08-25"
        state["daily_realized_pnl_usd"] = -27.0
        at = pd.Timestamp("2026-08-25T13:10:00Z")
        row = pd.Series(
            {"Open": 100.0, "Close": 100.0, "AskOpen": 100.03},
            name=pd.Timestamp("2026-08-25T13:09:00Z"),
        )

        self.assertFalse(
            runner._open_entry(
                strategy,
                "LONG",
                row,
                SimpleNamespace(bid=100.0, ask=100.03),
                basket_atr30=2.5,
                execution_time=at,
                apply_portfolio_rearm=False,
            )
        )
        self.assertFalse(state["basket"])

    def test_low_vol_pending_retains_spread_gate(self):
        runner, strategy, state = make_runner(live=False)
        now = pd.Timestamp("2026-08-25T13:10:00Z")
        arm_pending(state, atr30=1.5, target=100.0, now=now)
        quote = SimpleNamespace(bid=99.80, ask=100.0)

        self.assertFalse(runner._monitor_pending_entry(strategy, quote, now))
        self.assertFalse(state["basket"])
        self.assertEqual(state["pending_entry_side"], "LONG")

    def test_malformed_pending_state_is_cleared_without_open_or_crash(self):
        now = pd.Timestamp("2026-08-25T13:10:00Z")
        malformed = {
            "null_target": {"pending_entry_target": None},
            "null_atr": {"pending_entry_atr30": None},
            "string_target": {"pending_entry_target": "100.0"},
            "boolean_target": {"pending_entry_target": True},
            "string_atr": {"pending_entry_atr30": "1.5"},
            "boolean_atr": {"pending_entry_atr30": True},
            "numeric_opportunity": {"pending_entry_opportunity_id": 12345},
            "bad_expiry": {"pending_entry_expires_utc": "not-a-timestamp"},
            "bad_side": {"pending_entry_side": "SIDEWAYS"},
        }
        for label, mutation in malformed.items():
            with self.subTest(label=label):
                runner, strategy, state = make_runner()
                executor = CountingExecutor()
                runner.executor = executor
                arm_pending(state, now=now)
                state.update(mutation)

                self.assertTrue(runner._monitor_pending_entry(strategy, SimpleNamespace(bid=99.99, ask=100.0), now))
                self.assertIsNone(state["pending_entry_side"])
                self.assertFalse(state["basket"])
                self.assertEqual(executor.open_calls, 0)

    def test_restart_pending_requires_complete_canonical_signal_identity(self):
        now = pd.Timestamp("2026-08-25T13:10:00Z")
        malformed = {
            "nonpositive_target": {"pending_entry_target": 0.0},
            "missing_opportunity": {"pending_entry_opportunity_id": None},
            "forged_opportunity": {"pending_entry_opportunity_id": "forged"},
            "event_mismatch": {"pending_entry_event_time": "2026-08-25T13:08:00Z"},
            "release_mismatch": {"pending_entry_release_time": "2026-08-25T13:09:30Z"},
            "expiry_beyond_canonical_window": {"pending_entry_expires_utc": "2026-08-25T14:00:00Z"},
        }
        for label, mutation in malformed.items():
            with self.subTest(label=label):
                runner, strategy, state = make_runner(live=False)
                arm_pending(state, atr30=2.5, target=100.0)
                state.update(
                    {
                        "pending_entry_signal_bar": "2026-08-25T13:09:00Z",
                        "pending_entry_event_time": "2026-08-25T13:09:00Z",
                        "pending_entry_release_time": "2026-08-25T13:10:00Z",
                        "pending_entry_expires_utc": "2026-08-25T13:15:00Z",
                        "pending_entry_opportunity_id": "XAUUSD|2026-08-25T13:09:00+00:00|LONG|LONG|reverse_d60",
                    }
                )
                state.update(mutation)

                self.assertTrue(
                    runner._monitor_pending_entry(
                        strategy,
                        SimpleNamespace(bid=99.97, ask=100.0),
                        now,
                    )
                )
                self.assertIsNone(state["pending_entry_side"])
                self.assertFalse(state["basket"])

    def test_state_identity_mismatch_alerts_once_and_refuses_preflight(self):
        runner, strategy, state = make_runner()
        state.update(
            {
                "sync_block_new_entries": True,
                "sync_block_reason": "state_identity_mismatch",
                "sync_block_recoverable": False,
                "sync_block_details": {"observed": {"bot": "bot22"}, "expected": {"bot": "bot23"}},
            }
        )
        alerts = []
        runner._suppress_manual_alerts = False
        runner._notify_manual_action = lambda *_args, **kwargs: (alerts.append(kwargs) or True)

        self.assertFalse(runner.connect_and_preflight())
        self.assertFalse(runner.connect_and_preflight())
        self.assertEqual(len(alerts), 1)
        self.assertEqual(state["manual_alert_last_reason"], "state_identity_mismatch")

    def test_alert_helper_default_identity_is_bot23(self):
        self.assertEqual(live_manual_alerts.DEFAULT_BOT_ID, "bot23")

    def test_malformed_alert_url_is_contained(self):
        live_manual_alerts._LAST_SENT_EPOCH_BY_KEY.clear()
        with patch.dict(os.environ, {"BOT_MANUAL_ALERT_WEBHOOK_URL": "not-a-valid-url", "BOT_MANUAL_ALERT_ENABLED": "1"}, clear=False):
            self.assertFalse(
                live_manual_alerts.notify_manual_action_required(
                    bot_id="bot23",
                    symbol="XAUUSD",
                    title="test",
                    reason="test",
                    action="none",
                    key="bot23:test:malformed-url",
                )
            )


class Bot23TrendRecoveryRegressionTests(unittest.TestCase):
    def test_reverse_policy_is_persisted_on_originating_long_basket(self):
        runner, strategy, state = make_runner(live=False)
        opportunity, row, poll_time, quote = sample_opportunity(side="LONG")
        opportunity["entry_policy"] = {"policy_id": "reverse_d60", "action": "reverse_long"}

        self.assertTrue(
            runner._open_entry(
                strategy, "LONG", row, quote, basket_atr30=2.5,
                execution_time=poll_time, opportunity=opportunity,
            )
        )
        self.assertTrue(state["reverse_used"])

    def test_only_reverse_long_stop_arms_recovery_episode(self):
        runner, strategy, state = make_runner(live=False)
        row = pd.Series(
            {"Open": 100.0, "Close": 100.0, "AskOpen": 100.03},
            name=pd.Timestamp("2026-08-25 13:10:00", tz="UTC"),
        )
        state.update({
            "basket": [{"side": "LONG", "lot": 0.01, "entry_price": 100.0}],
            "current_basket_id": "L1-B000001", "frozen_basket_atr30": 2.5,
            "reverse_used": False,
        })
        runner._close_basket(strategy, "basket_stop", row, -18.0)
        self.assertFalse(runner._trend_recovery_state()["active"])

        state.update({
            "basket": [{"side": "LONG", "lot": 0.01, "entry_price": 100.0}],
            "current_basket_id": "L1-B000002", "frozen_basket_atr30": 2.5,
            "reverse_used": True,
        })
        runner._close_basket(strategy, "basket_stop", row, -18.0)
        episode = runner._trend_recovery_state()
        self.assertTrue(episode["active"])
        self.assertEqual(episode["origin_basket_id"], "L1-B000002")

    def test_completed_bullish_m1_opens_at_most_two_shorts(self):
        runner, strategy, _state = make_runner(live=False)
        self.assertTrue(
            runner._arm_trend_recovery_episode(
                strategy, pd.Timestamp("2026-08-25 13:00:00", tz="UTC"), "L1-B000001", 2.5
            )
        )
        quote = SimpleNamespace(bid=100.0, ask=100.03)
        for minute in (1, 2, 3):
            bar_time = pd.Timestamp(f"2026-08-25 13:0{minute}:00", tz="UTC")
            row = pd.Series({"Open": 99.0, "Close": 100.0, "AskOpen": 100.03}, name=bar_time)
            runner._process_trend_recovery_entry(row, quote, bar_time + pd.Timedelta(minutes=1, seconds=2), True)

        trend = runner.params["trend_recovery_strategies"][0]
        basket = runner._st(trend)["basket"]
        self.assertEqual(len(basket), 2)
        self.assertTrue(all(pos["side"] == "SHORT" for pos in basket))
        self.assertEqual(runner._trend_recovery_state()["total_entries"], 2)
        self.assertFalse(runner._trend_recovery_state()["active"])

    def test_malformed_active_trend_episode_is_invalidated_without_open(self):
        runner, strategy, _state = make_runner(live=False)
        self.assertTrue(
            runner._arm_trend_recovery_episode(
                strategy,
                pd.Timestamp("2026-08-25 13:00:00", tz="UTC"),
                "L1-B000001",
                2.5,
            )
        )
        episode = runner._trend_recovery_state()
        episode["total_entries"] = "not-an-integer"
        bar_time = pd.Timestamp("2026-08-25 13:01:00", tz="UTC")
        row = pd.Series(
            {"Open": 99.0, "Close": 100.0, "AskOpen": 100.03},
            name=bar_time,
        )

        runner._process_trend_recovery_entry(
            row,
            SimpleNamespace(bid=100.0, ask=100.03),
            bar_time + pd.Timedelta(minutes=1, seconds=2),
            True,
        )

        self.assertFalse(episode["active"])
        self.assertEqual(episode["end_reason"], "episode_state_invalid")
        trend = runner.params["trend_recovery_strategies"][0]
        self.assertFalse(runner._st(trend)["basket"])

    def test_trend_episode_rejects_boolean_identity_coercion(self):
        runner, strategy, _state = make_runner(live=False)
        self.assertTrue(
            runner._arm_trend_recovery_episode(
                strategy,
                pd.Timestamp("2026-08-25 13:00:00", tz="UTC"),
                "L1-B000001",
                2.5,
            )
        )
        episode = runner._trend_recovery_state()
        episode["active"] = "false"
        bar_time = pd.Timestamp("2026-08-25 13:01:00", tz="UTC")
        row = pd.Series(
            {"Open": 99.0, "Close": 100.0, "AskOpen": 100.03},
            name=bar_time,
        )

        runner._process_trend_recovery_entry(
            row,
            SimpleNamespace(bid=100.0, ask=100.03),
            bar_time + pd.Timedelta(minutes=1, seconds=2),
            True,
        )

        self.assertFalse(episode["active"])
        self.assertEqual(episode["end_reason"], "episode_state_invalid")
        trend = runner.params["trend_recovery_strategies"][0]
        self.assertFalse(runner._st(trend)["basket"])

    def test_trend_episode_rejects_coercible_non_numeric_identity(self):
        corruptions = (
            {"frozen_atr30": "2.5"},
            {"episode_id": 12345},
            {"started_utc": 1787643600000000000},
        )
        for corruption in corruptions:
            with self.subTest(corruption=corruption):
                runner, strategy, _state = make_runner(live=False)
                self.assertTrue(
                    runner._arm_trend_recovery_episode(
                        strategy,
                        pd.Timestamp("2026-08-25 13:00:00", tz="UTC"),
                        "L1-B000001",
                        2.5,
                    )
                )
                episode = runner._trend_recovery_state()
                episode.update(corruption)
                bar_time = pd.Timestamp("2026-08-25 13:01:00", tz="UTC")
                runner._process_trend_recovery_entry(
                    pd.Series(
                        {"Open": 99.0, "Close": 100.0, "AskOpen": 100.03},
                        name=bar_time,
                    ),
                    SimpleNamespace(bid=100.0, ask=100.03),
                    bar_time + pd.Timedelta(minutes=1, seconds=2),
                    True,
                )

                self.assertFalse(episode["active"])
                self.assertEqual(episode["end_reason"], "episode_state_invalid")
                trend = runner.params["trend_recovery_strategies"][0]
                self.assertFalse(runner._st(trend)["basket"])

    def test_trend_future_processed_receipt_blocks_older_bar_replay(self):
        runner, strategy, _state = make_runner(live=False)
        self.assertTrue(
            runner._arm_trend_recovery_episode(
                strategy,
                pd.Timestamp("2026-08-25 13:00:00", tz="UTC"),
                "L1-B000001",
                2.5,
            )
        )
        episode = runner._trend_recovery_state()
        future_receipt = "2026-08-25T13:06:00+00:00"
        episode["last_processed_m1_bar"] = future_receipt
        current_bar = pd.Timestamp("2026-08-25 13:01:00", tz="UTC")

        runner._process_trend_recovery_entry(
            pd.Series(
                {"Open": 99.0, "Close": 100.0, "AskOpen": 100.03},
                name=current_bar,
            ),
            SimpleNamespace(bid=100.0, ask=100.03),
            current_bar + pd.Timedelta(minutes=1, seconds=2),
            True,
        )

        trend = runner.params["trend_recovery_strategies"][0]
        self.assertFalse(runner._st(trend)["basket"])
        self.assertEqual(episode["last_processed_m1_bar"], future_receipt)

    def test_trend_future_receipt_cannot_prevent_episode_expiry(self):
        runner, strategy, _state = make_runner(live=False)
        self.assertTrue(
            runner._arm_trend_recovery_episode(
                strategy,
                pd.Timestamp("2026-08-25T13:00:00Z"),
                "L1-B000001",
                2.5,
            )
        )
        episode = runner._trend_recovery_state()
        episode["last_processed_m1_bar"] = "2026-08-26T13:06:00+00:00"

        runner._process_trend_recovery_entry(
            pd.Series(
                {"Open": 99.0, "Close": 100.0, "AskOpen": 100.03},
                name=pd.Timestamp("2026-08-25T13:31:00Z"),
            ),
            SimpleNamespace(bid=100.0, ask=100.03),
            pd.Timestamp("2026-08-25T13:32:00Z"),
            True,
        )

        self.assertFalse(episode["active"])
        self.assertEqual(episode["end_reason"], "entry_window_expired")

    def test_trend_episode_requires_canonical_identity_and_window(self):
        mutations = (
            {"episode_id": "TR-forged"},
            {"entry_until_utc": "2026-08-26T13:30:00+00:00"},
            {"origin_basket_id": 12345},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                runner, strategy, _state = make_runner(live=False)
                self.assertTrue(
                    runner._arm_trend_recovery_episode(
                        strategy,
                        pd.Timestamp("2026-08-25 13:00:00", tz="UTC"),
                        "L1-B000001",
                        2.5,
                    )
                )
                episode = runner._trend_recovery_state()
                episode.update(mutation)
                current_bar = pd.Timestamp("2026-08-25 13:01:00", tz="UTC")

                runner._process_trend_recovery_entry(
                    pd.Series(
                        {"Open": 99.0, "Close": 100.0, "AskOpen": 100.03},
                        name=current_bar,
                    ),
                    SimpleNamespace(bid=100.0, ask=100.03),
                    current_bar + pd.Timedelta(minutes=1, seconds=2),
                    True,
                )

                self.assertFalse(episode["active"])
                self.assertEqual(episode["end_reason"], "episode_state_invalid")
                trend = runner.params["trend_recovery_strategies"][0]
                self.assertFalse(runner._st(trend)["basket"])

    def test_trend_episode_does_not_process_before_canonical_start(self):
        runner, strategy, _state = make_runner(live=False)
        self.assertTrue(
            runner._arm_trend_recovery_episode(
                strategy,
                pd.Timestamp("2026-08-25 13:00:00", tz="UTC"),
                "L1-B000001",
                2.5,
            )
        )
        episode = runner._trend_recovery_state()
        episode["started_utc"] = "2026-08-26T13:00:00+00:00"
        episode["entry_until_utc"] = "2026-08-26T13:30:00+00:00"
        current_bar = pd.Timestamp("2026-08-25 13:01:00", tz="UTC")

        runner._process_trend_recovery_entry(
            pd.Series(
                {"Open": 99.0, "Close": 100.0, "AskOpen": 100.03},
                name=current_bar,
            ),
            SimpleNamespace(bid=100.0, ask=100.03),
            current_bar + pd.Timedelta(minutes=1, seconds=2),
            True,
        )

        trend = runner.params["trend_recovery_strategies"][0]
        self.assertFalse(runner._st(trend)["basket"])
        self.assertTrue(episode["active"])

    def test_trend_future_m1_is_not_consumed_before_release(self):
        runner, strategy, _state = make_runner(live=False)
        self.assertTrue(
            runner._arm_trend_recovery_episode(
                strategy,
                pd.Timestamp("2026-08-25T13:00:00Z"),
                "L1-B000001",
                2.5,
            )
        )
        episode = runner._trend_recovery_state()
        future_bar = pd.Timestamp("2026-08-25T13:01:00Z")

        runner._process_trend_recovery_entry(
            pd.Series(
                {"Open": 99.0, "Close": 100.0, "AskOpen": 100.03},
                name=future_bar,
            ),
            SimpleNamespace(bid=100.0, ask=100.03),
            pd.Timestamp("2026-08-25T13:01:30Z"),
            True,
        )

        trend = runner.params["trend_recovery_strategies"][0]
        self.assertFalse(runner._st(trend)["basket"])
        self.assertIsNone(episode["last_processed_m1_bar"])

    def test_one_ticket_stop_closes_all_recovery_positions(self):
        runner, strategy, _state = make_runner(live=False)
        runner.executor = CountingExecutor()
        runner._arm_trend_recovery_episode(
            strategy, pd.Timestamp("2026-08-25 13:00:00", tz="UTC"), "L1-B000001", 2.5
        )
        quote = SimpleNamespace(bid=100.0, ask=100.03)
        for minute in (1, 2):
            bar_time = pd.Timestamp(f"2026-08-25 13:0{minute}:00", tz="UTC")
            row = pd.Series({"Open": 99.0, "Close": 100.0, "AskOpen": 100.03}, name=bar_time)
            runner._process_trend_recovery_entry(row, quote, bar_time + pd.Timedelta(minutes=1, seconds=2), True)

        trend = runner.params["trend_recovery_strategies"][0]
        self.assertEqual(len(runner._st(trend)["basket"]), 2)
        runner._process_trend_recovery_exits(
            SimpleNamespace(bid=109.97, ask=110.0),
            pd.Timestamp("2026-08-25 13:04:00", tz="UTC"),
        )
        self.assertFalse(runner._st(trend)["basket"])
        self.assertEqual(runner._st(trend)["last_closed_reason"], "trend_any_ticket_stop")
        self.assertFalse(runner._trend_recovery_state()["active"])

    def test_individual_target_closes_only_one_recovery_ticket(self):
        runner, strategy, _state = make_runner(live=False)
        runner.executor = CountingExecutor()
        runner._arm_trend_recovery_episode(
            strategy, pd.Timestamp("2026-08-25 13:00:00", tz="UTC"), "L1-B000001", 2.5
        )
        quote = SimpleNamespace(bid=100.0, ask=100.03)
        for minute in (1, 2):
            bar_time = pd.Timestamp(f"2026-08-25 13:0{minute}:00", tz="UTC")
            row = pd.Series({"Open": 99.0, "Close": 100.0, "AskOpen": 100.03}, name=bar_time)
            runner._process_trend_recovery_entry(row, quote, bar_time + pd.Timedelta(minutes=1, seconds=2), True)

        trend = runner.params["trend_recovery_strategies"][0]
        runner._process_trend_recovery_exits(
            SimpleNamespace(bid=88.97, ask=89.0),
            pd.Timestamp("2026-08-25 13:04:00", tz="UTC"),
        )
        self.assertEqual(len(runner._st(trend)["basket"]), 1)

    def test_live_episode_arms_only_after_broker_close_confirmation(self):
        runner, strategy, state = make_runner(live=True)
        executor = CountingExecutor()
        runner.executor = executor
        arm_owned_basket(strategy, state, executor)
        state.update({
            "current_basket_id": "L1-B000001", "frozen_basket_atr30": 2.5,
            "reverse_used": True, "pending_close_reason": "basket_stop",
            "pending_close_signal_bar": "2026-08-25 13:10:00+00:00",
        })
        executor.positions = []
        close_time = pd.Timestamp("2026-08-25 13:10:03", tz="UTC")
        executor.close_deal = SimpleNamespace(
            position_id=9401, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
            net_profit=-18.0, deal_time=int(close_time.timestamp()), deal=501, exit_volume=0.01,
            price=98.0,
        )

        self.assertTrue(runner._sync_strategy(strategy))
        self.assertTrue(runner._trend_recovery_state()["active"])
        self.assertEqual(runner._trend_recovery_state()["started_utc"], dt_text(close_time))


class SignalEvaluationAttributionTests(unittest.TestCase):
    def test_every_lane_has_explicit_signal_identity(self):
        runner, _strategy, _state = make_runner(live=False)
        self.assertTrue(runner.params["session_vwap_enabled"])
        self.assertEqual(len(runner._all_strategies()), 22)
        for strategy in runner._all_strategies():
            with self.subTest(strategy=strategy["id"]):
                self.assertTrue(str(strategy.get("spec_id") or ""))
                self.assertTrue(str(strategy.get("signal_id") or ""))
                self.assertNotEqual(runner._strategy_group(strategy), "unknown")

    def test_za_variants_are_independently_attributed(self):
        runner, strategy, _state = make_runner(live=False)
        cases = (
            (
                "XAUUSD|2026-07-01T10:00:00+00:00|LONG|LONG|reverse_d60",
                "LONG", "za_horizontal_primary", "none", "LONG", "LONG",
            ),
            (
                "XAUUSD|2026-07-01T10:00:00+00:00|SHORT|LONG|reverse_d60",
                "LONG", "za_late_short_reverse_long", "reverse_long", "SHORT", "LONG",
            ),
            (
                "XAUUSD|2026-07-01T10:00:00+00:00|INVENTORY_RANGE_FADE|SHORT|balanced",
                "SHORT", "za_inventory_range_false_break_fade", "opposite_breakout", "", "SHORT",
            ),
        )
        for opportunity_id, side, variant, transform, raw_side, effective_side in cases:
            with self.subTest(variant=variant):
                observed = runner._signal_attribution(strategy, opportunity_id, side)
                self.assertEqual(observed["configured_signal_id"], "za_horizontal_impulse")
                self.assertEqual(observed["signal_variant_id"], variant)
                self.assertEqual(observed["signal_transform_id"], transform)
                self.assertEqual(observed["raw_side"], raw_side)
                self.assertEqual(observed["effective_side"], effective_side)

    def test_za_position_without_opportunity_identity_is_not_counted_as_primary(self):
        runner, strategy, _state = make_runner(live=False)
        observed = runner._signal_attribution(strategy, "", "LONG")
        self.assertEqual(observed["signal_variant_id"], "za_unattributed_legacy")
        self.assertEqual(observed["signal_transform_id"], "unknown")

    def test_shadow_mixed_za_basket_close_is_split_by_position_without_double_count(self):
        runner, strategy, state = make_runner(live=False)
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        basket_id = "L1-B000001"
        state["current_basket_id"] = basket_id
        state["basket_sequence"] = 1
        state["basket"] = [
            {
                "ticket": "SHADOW-1", "position_identifier": "SHADOW-1",
                "side": "LONG", "lot": 0.01, "entry_price": 100.0,
                "shadow": True, "basket_id": basket_id,
                "opportunity_id": "XAUUSD|2026-07-01T10:00:00+00:00|LONG|LONG|reverse_d60",
            },
            {
                "ticket": "SHADOW-2", "position_identifier": "SHADOW-2",
                "side": "LONG", "lot": 0.02, "entry_price": 101.0,
                "shadow": True, "basket_id": basket_id,
                "opportunity_id": "XAUUSD|2026-07-01T10:01:00+00:00|SHORT|LONG|reverse_d60",
            },
        ]
        price_row = pd.Series(
            {"Open": 102.0, "Close": 102.0, "AskOpen": 102.1},
            name=pd.Timestamp("2026-07-01T10:10:00Z"),
        )
        expected_pnl = runner._basket_pnl(strategy, 102.0, 102.1)
        operational_rows = []
        evaluation_rows = []
        with patch.object(
            live_s23_bot, "append_csv",
            side_effect=lambda _path, row, _fields: operational_rows.append(dict(row)),
        ), patch.object(
            live_s23_bot, "append_signal_evaluation_csv",
            side_effect=lambda _path, row, _fields: evaluation_rows.append(dict(row)),
        ):
            self.assertEqual(
                runner._close_basket(strategy, "basket_target", price_row, expected_pnl),
                "closed",
            )

        close_rows = [row for row in operational_rows if row["event"] == "basket_close"]
        self.assertEqual(len(close_rows), 1)
        evaluation_rows = [
            row for row in evaluation_rows
            if row["event"] == "position_close_attributed"
        ]
        self.assertEqual(len(evaluation_rows), 2)
        self.assertEqual(
            {row["event"] for row in evaluation_rows},
            {"position_close_attributed"},
        )
        self.assertEqual(
            {row["signal_variant_id"] for row in evaluation_rows},
            {"za_horizontal_primary", "za_late_short_reverse_long"},
        )
        self.assertAlmostEqual(
            sum(float(row["profit"]) for row in evaluation_rows), expected_pnl,
        )
        self.assertTrue(all(row["basket_id"] == basket_id for row in evaluation_rows))
        self.assertFalse(state["basket"])

    def test_shadow_single_position_close_keeps_its_opportunity_identity(self):
        runner, _strategy, _state = make_runner(live=False)
        strategy = runner.params["trend_recovery_strategies"][0]
        state = runner._st(strategy)
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        basket_id = "L12-B000001"
        opportunity_id = "TR-1-L1-B000001"
        position = {
            "ticket": "SHADOW-TR-1", "position_identifier": "SHADOW-TR-1",
            "side": "LONG", "lot": 0.01, "entry_price": 100.0,
            "shadow": True, "basket_id": basket_id,
            "opportunity_id": opportunity_id,
        }
        state.update({
            "current_basket_id": basket_id, "basket_sequence": 1,
            "basket": [position],
        })
        price_row = pd.Series(
            {"Open": 101.0, "Close": 101.0, "AskOpen": 101.1},
            name=pd.Timestamp("2026-07-01T10:10:00Z"),
        )
        evaluation_rows = []
        with patch.object(live_s23_bot, "append_csv"), patch.object(
            live_s23_bot, "append_signal_evaluation_csv",
            side_effect=lambda _path, row, _fields: evaluation_rows.append(dict(row)),
        ):
            self.assertEqual(
                runner._close_trend_recovery_ticket(
                    strategy, position, "trend_ticket_target", price_row, 1.0,
                ),
                "closed",
            )
        self.assertEqual(len(evaluation_rows), 1)
        self.assertEqual(evaluation_rows[0]["event"], "position_close_attributed")
        self.assertEqual(evaluation_rows[0]["opportunity_id"], opportunity_id)
        self.assertEqual(evaluation_rows[0]["profit"], 1.0)

    def test_malformed_evaluation_allocation_does_not_block_trade_audit(self):
        runner, strategy, _state = make_runner(live=False)
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        operational_rows = []
        with patch.object(
            live_s23_bot, "append_csv",
            side_effect=lambda _path, row, _fields: operational_rows.append(dict(row)),
        ), patch.object(
            live_s23_bot, "append_signal_evaluation_csv",
        ) as evaluation_write, self.assertLogs(level="ERROR") as captured:
            runner._trade_row(
                "basket_close", strategy, profit=1.0,
                _evaluation_allocations="corrupt",
            )
        self.assertEqual(len(operational_rows), 1)
        evaluation_write.assert_not_called()
        self.assertIn("allocations invalid", "\n".join(captured.output))

    def test_trade_and_signal_ledgers_are_separate_and_joinable(self):
        runner, strategy, _state = make_runner(live=False)
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        opportunity_id = (
            "XAUUSD|2026-07-01T10:00:00+00:00|SHORT|LONG|reverse_d60"
        )
        with tempfile.TemporaryDirectory() as tmp:
            trade_path = os.path.join(tmp, "s23_trades.csv")
            with patch.object(live_s23_bot, "TRADE_LOG_FILE", trade_path):
                runner._trade_row(
                    "entry", strategy, opportunity_id=opportunity_id,
                    basket_id="L1-B000001", ticket=101, side="LONG", profit="",
                )
            evaluation_path = os.path.join(tmp, "s23_signal_evaluation.csv")
            with open(trade_path, newline="", encoding="utf-8") as handle:
                trade_rows = list(csv.DictReader(handle))
            with open(evaluation_path, newline="", encoding="utf-8") as handle:
                evaluation_rows = list(csv.DictReader(handle))
        self.assertEqual(len(trade_rows), 1)
        self.assertEqual(len(evaluation_rows), 1)
        self.assertEqual(evaluation_rows[0]["opportunity_id"], opportunity_id)
        self.assertEqual(evaluation_rows[0]["strategy_id"], strategy["id"])
        self.assertEqual(evaluation_rows[0]["signal_variant_id"], "za_late_short_reverse_long")

    def test_live_confirmed_close_keeps_exact_za_variant_and_broker_net_pnl(self):
        runner, strategy, _state = make_runner(live=True)
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        opportunity_id = (
            "XAUUSD|2026-07-01T10:00:00+00:00|SHORT|LONG|reverse_d60"
        )
        evaluation_rows = []
        with patch.object(live_s23_bot, "append_csv"), patch.object(
            live_s23_bot, "append_signal_evaluation_csv",
            side_effect=lambda _path, row, _fields: evaluation_rows.append(dict(row)),
        ):
            runner._trade_row(
                "position_close_confirmed", strategy,
                opportunity_id=opportunity_id, basket_id="L1-B000001",
                ticket=101, position_identifier=201, deal_id=301,
                side="LONG", profit=4.75, reason="basket_target",
            )
        self.assertEqual(len(evaluation_rows), 1)
        row = evaluation_rows[0]
        self.assertEqual(row["signal_variant_id"], "za_late_short_reverse_long")
        self.assertEqual(row["ticket"], 101)
        self.assertEqual(row["position_identifier"], 201)
        self.assertEqual(row["deal_id"], 301)
        self.assertEqual(row["profit"], 4.75)

    def test_passive_evaluation_failure_does_not_suppress_trade_audit(self):
        runner, strategy, _state = make_runner(live=False)
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        rows = []
        with patch.object(
            live_s23_bot, "append_csv",
            side_effect=lambda _path, row, _fields: rows.append(dict(row)),
        ), patch.object(
            live_s23_bot, "append_signal_evaluation_csv",
            side_effect=OSError("evaluation disk unavailable"),
        ), self.assertLogs(level="ERROR") as captured:
            runner._trade_row("entry", strategy, side="LONG", ticket=101)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "entry")
        self.assertFalse(runner._signal_evaluation_enabled)
        self.assertIn("passive signal evaluation write failed", "\n".join(captured.output))

    def test_passive_evaluation_construction_failure_does_not_escape_trade_audit(self):
        runner, strategy, _state = make_runner(live=False)
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        operational_rows = []
        with patch.object(
            live_s23_bot, "append_csv",
            side_effect=lambda _path, row, _fields: operational_rows.append(dict(row)),
        ), patch.object(
            runner, "_append_signal_evaluation_row",
            side_effect=RuntimeError("attribution construction failed"),
        ), self.assertLogs(level="ERROR") as captured:
            runner._trade_row("position_close_confirmed", strategy, profit=1.25)
        self.assertEqual(len(operational_rows), 1)
        self.assertEqual(operational_rows[0]["event"], "position_close_confirmed")
        self.assertFalse(runner._signal_evaluation_enabled)
        self.assertIn(
            "passive signal evaluation processing failed",
            "\n".join(captured.output),
        )

    def test_passive_evaluation_construction_failure_cannot_stop_shadow_close(self):
        runner, strategy, state = make_runner(live=False)
        runner._trade_row = S23HorizontalInventoryRunner._trade_row.__get__(
            runner, S23HorizontalInventoryRunner,
        )
        basket_id = "L1-B000001"
        state.update({
            "current_basket_id": basket_id,
            "basket_sequence": 1,
            "basket": [{
                "ticket": "SHADOW-1", "position_identifier": "SHADOW-1",
                "side": "LONG", "lot": 0.01, "entry_price": 100.0,
                "shadow": True, "basket_id": basket_id,
                "opportunity_id": "XAUUSD|2026-07-01T10:00:00+00:00|LONG|LONG|reverse_d60",
            }],
        })
        price_row = pd.Series(
            {"Open": 101.0, "Close": 101.0, "AskOpen": 101.1},
            name=pd.Timestamp("2026-07-01T10:10:00Z"),
        )
        with patch.object(live_s23_bot, "append_csv"), patch.object(
            runner, "_append_signal_evaluation_row",
            side_effect=RuntimeError("attribution construction failed"),
        ), self.assertLogs(level="ERROR"):
            self.assertEqual(
                runner._close_basket(strategy, "basket_target", price_row, 1.0),
                "closed",
            )
        self.assertFalse(state["basket"])
        self.assertEqual(state["last_closed_reason"], "basket_target")
        self.assertEqual(state["daily_realized_pnl_usd"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
