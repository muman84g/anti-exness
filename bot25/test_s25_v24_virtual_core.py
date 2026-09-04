# -*- coding: utf-8 -*-
"""Regression coverage for the V24 virtual bilateral production path."""

from __future__ import annotations

import copy
import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd

import live_s25_bot as s25


class CountingExecutor(s25.FakeExecutor):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.open_calls = 0

    def open_position(self, *args, **kwargs):
        self.open_calls += 1
        return super().open_position(*args, **kwargs)


class V24VirtualCoreRegressionTests(unittest.TestCase):
    def test_authorized_live_config_still_requires_v24_ack(self):
        params = copy.deepcopy(s25.load_params())
        self.assertIs(params["live_trading_enabled"], True)
        self.assertIs(params["shadow_forward_enabled"], False)
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        with tempfile.TemporaryDirectory(prefix="s25-live-config-") as temp:
            with mock.patch.object(s25, "STATE_FILE", str(Path(temp) / "state.json")):
                for value, expected in (("", False), ("MAN231_LIVE_ACK", False), ("V24_VIRTUAL_CORE_LIVE_ACK", True)):
                    with mock.patch.dict(os.environ, {"BOT25_ENABLE_REAL_TRADING": value}):
                        runner = s25.S25V24Runner(params)
                        self.assertEqual(runner.live_enabled, expected)
                        self.assertFalse(runner.shadow_enabled)
                        self.assertEqual(runner.activation_error, not expected)

    def setUp(self):
        self._historical_quote_patch = mock.patch.object(s25, "_SELF_TEST_HISTORICAL_QUOTES", True)
        self._historical_quote_patch.start()
        self.addCleanup(self._historical_quote_patch.stop)

    @staticmethod
    def _live_position(ticket, side, price):
        return SimpleNamespace(
            ticket=ticket,
            identifier=ticket + 5000,
            symbol="XAUUSD",
            type=s25.ORDER_TYPE_BUY if side == "LONG" else s25.ORDER_TYPE_SELL,
            volume=0.01,
            open_price=price,
            sl=0.0,
            tp=0.0,
            profit=0.0,
            magic=s25.EXPECTED_S25_MAGIC,
            open_time=int(pd.Timestamp("2026-09-04T00:00:00Z").timestamp()) + ticket,
            comment=f"s25_m231_{'L' if side == 'LONG' else 'S'}{ticket:04d}",
        )

    @staticmethod
    def _v5_state(position, *, pending_open=None):
        side = "LONG" if position.type == s25.ORDER_TYPE_BUY else "SHORT"
        return {
            "version": 5,
            "bot": "bot25",
            "strategy_id": "bot25_man231_xauusd_bilateral_core_satellite_v001",
            "last_saved_utc": "2026-09-04T00:05:00+00:00",
            "strategies": {
                "man231_bilateral_book": {
                    "positions": [{
                        "ticket": position.ticket,
                        "position_identifier": position.identifier,
                        "side": side,
                        "lot": position.volume,
                        "entry_price": position.open_price,
                        "entry_time_utc": pd.Timestamp(position.open_time, unit="s", tz="UTC").isoformat(),
                        "open_time_epoch": position.open_time,
                        "owner_symbol": position.symbol,
                        "owner_magic": position.magic,
                        "owner_comment": position.comment,
                        "shadow": False,
                        "close_requested": False,
                    }],
                    "episode_sequence": 9,
                    "current_episode_id": "s25_m231_e000009",
                    "episode_start_quote_utc": "2026-09-04T00:00:00+00:00",
                    "active_wave": 1,
                    "last_atr": 1.0,
                    "last_ema": 4000.0,
                    "last_long_frontier": 4010.0,
                    "last_short_frontier": 4020.0,
                    "last_processed_m5_bar": "2026-09-04T00:00:00+00:00",
                    "last_quote_utc": "2026-09-04T00:05:00+00:00",
                    "pending_open": pending_open,
                    "pending_close_reason": None,
                    "sync_block_new_entries": False,
                    "sync_block_reason": None,
                }
            },
        }

    def test_start_restart_and_frontier_add_order_boundary(self):
        params = copy.deepcopy(s25.load_params())
        params["live_trading_enabled"] = True
        params["shadow_forward_enabled"] = False
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        strategy = params["strategies"][0]

        self.assertEqual(params["strategy_id"], "bot25_v24_xauusd_virtual_bilateral_core_v001")
        self.assertEqual(strategy["physical_seed_orders"], 0)

        with tempfile.TemporaryDirectory(prefix="s25-v24-regression-") as temp:
            state_path = str(Path(temp) / "state" / "s25_bot_state.json")
            trade_path = str(Path(temp) / "logs" / "s25_trades.csv")
            gate_name = params["real_trading_activation_env"]
            gate_value = params["real_trading_activation_value"]

            with (
                mock.patch.object(s25, "STATE_FILE", state_path),
                mock.patch.object(s25, "TRADE_LOG_FILE", trade_path),
                mock.patch.dict(os.environ, {gate_name: gate_value}),
            ):
                first_executor = CountingExecutor()
                first = s25.S25V24Runner(params)
                first.dm = s25.FakeDM()
                first.executor = first_executor
                first._suppress_manual_alerts = True
                self.assertTrue(first.connect_and_preflight())
                first.run_once()

                self.assertEqual(first_executor.open_calls, 0)
                self.assertEqual(first._position_counts(strategy), (0, 0))
                self.assertEqual(first._logical_position_counts(strategy), (1, 1))
                episode_id = first._st(strategy)["current_episode_id"]
                self.assertTrue(episode_id)

                restart_executor = CountingExecutor(quote_time="2026-08-27T00:25:01Z")
                restarted = s25.S25V24Runner(params)
                restarted.dm = s25.FakeDM()
                restarted.executor = restart_executor
                restarted._suppress_manual_alerts = True
                self.assertTrue(restarted.connect_and_preflight())
                restarted.run_once()

                self.assertEqual(restart_executor.open_calls, 0)
                self.assertEqual(restarted._position_counts(strategy), (0, 0))
                self.assertEqual(restarted._logical_position_counts(strategy), (1, 1))
                self.assertEqual(restarted._st(strategy)["current_episode_id"], episode_id)

                state = restarted._st(strategy)
                state["active_wave"] = 1
                state["last_long_frontier"] = 4019.0
                state["last_processed_m5_bar"] = "2026-08-27T00:20:00+00:00"
                restart_executor.info.quote_time_msc = int(
                    pd.Timestamp("2026-08-27T00:30:00Z").timestamp() * 1000
                )
                row = pd.Series(
                    {"atr14": 1.0, "ema200": 4000.0, "break_dir": 0},
                    name=pd.Timestamp("2026-08-27T00:25:00Z"),
                )
                restarted._process_m5_event(
                    strategy,
                    row,
                    restart_executor.info,
                    pd.Timestamp("2026-08-27T00:30:00Z"),
                )

                self.assertEqual(restart_executor.open_calls, 1)
                self.assertEqual(restarted._position_counts(strategy), (1, 0))
                self.assertEqual(restarted._logical_position_counts(strategy), (2, 1))

                with open(trade_path, "r", newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(sum(row["event"] == "episode_start" for row in rows), 1)
                self.assertFalse(any(row["reason"] == "bilateral_seed" for row in rows))
                self.assertTrue(
                    any(
                        row["event"] == "entry"
                        and row["reason"] == "long_frontier_add"
                        for row in rows
                    )
                )

    def test_nonflat_migration_ambiguity_preserves_legacy_state(self):
        params = copy.deepcopy(s25.load_params())
        params["live_trading_enabled"] = True
        params["shadow_forward_enabled"] = False
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        gate_name = params["real_trading_activation_env"]
        gate_value = params["real_trading_activation_value"]
        stored = self._live_position(301, "LONG", 4010.0)
        cases = (
            (
                "pending_open",
                self._v5_state(stored, pending_open={"side": "LONG"}),
                [stored],
                [],
            ),
            (
                "broker_lot_mismatch",
                self._v5_state(stored),
                [SimpleNamespace(**{**vars(stored), "volume": 0.02})],
                [],
            ),
            (
                "pending_order",
                self._v5_state(stored),
                [stored],
                [SimpleNamespace(
                    ticket=9301,
                    symbol="XAUUSD",
                    magic=s25.EXPECTED_S25_MAGIC,
                    comment="s25_m231_pending",
                )],
            ),
            (
                "duplicate_broker_identifier",
                self._v5_state(stored),
                [stored, SimpleNamespace(**{**vars(stored), "ticket": stored.ticket + 1})],
                [],
            ),
            (
                "malformed_entry_price",
                {
                    **self._v5_state(stored),
                    "strategies": {
                        "man231_bilateral_book": {
                            **self._v5_state(stored)["strategies"]["man231_bilateral_book"],
                            "positions": [{
                                **self._v5_state(stored)["strategies"]["man231_bilateral_book"]["positions"][0],
                                "entry_price": "not-a-price",
                            }],
                        }
                    },
                },
                [stored],
                [],
            ),
            (
                "extra_strategy_key",
                {
                    **self._v5_state(stored),
                    "strategies": {
                        **self._v5_state(stored)["strategies"],
                        "unexpected_strategy": {},
                    },
                },
                [stored],
                [],
            ),
        )

        for name, legacy_state, live_positions, live_orders in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory(prefix="s25-v24-v5-reject-") as temp:
                state_path = str(Path(temp) / "state" / "s25_bot_state.json")
                trade_path = str(Path(temp) / "logs" / "s25_trades.csv")
                Path(state_path).parent.mkdir(parents=True, exist_ok=True)
                original = json.dumps(legacy_state, sort_keys=True).encode("utf-8")
                Path(state_path).write_bytes(original)
                with (
                    mock.patch.object(s25, "STATE_FILE", state_path),
                    mock.patch.object(s25, "TRADE_LOG_FILE", trade_path),
                    mock.patch.dict(os.environ, {gate_name: gate_value}),
                ):
                    executor = CountingExecutor(
                        positions=live_positions,
                        orders=live_orders,
                        quote_time="2026-09-04T00:06:00Z",
                    )
                    runner = s25.S25V24Runner(params)
                    runner.dm = s25.FakeDM()
                    runner.executor = executor
                    runner._suppress_manual_alerts = True
                    self.assertFalse(runner.connect_and_preflight())
                    self.assertEqual(executor.open_calls, 0)
                    self.assertEqual(Path(state_path).read_bytes(), original)

    def test_nonflat_shadow_canary_is_read_only(self):
        params = copy.deepcopy(s25.load_params())
        params["live_trading_enabled"] = False
        params["shadow_forward_enabled"] = True
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        strategy = params["strategies"][0]
        live_position = self._live_position(401, "LONG", 4010.0)

        with tempfile.TemporaryDirectory(prefix="s25-v24-shadow-takeover-") as temp:
            state_path = str(Path(temp) / "state" / "s25_bot_state.json")
            trade_path = str(Path(temp) / "logs" / "s25_trades.csv")
            Path(state_path).parent.mkdir(parents=True, exist_ok=True)
            Path(state_path).write_text(json.dumps(self._v5_state(live_position)), encoding="utf-8")
            with (
                mock.patch.object(s25, "STATE_FILE", state_path),
                mock.patch.object(s25, "TRADE_LOG_FILE", trade_path),
            ):
                executor = CountingExecutor(
                    positions=[live_position],
                    quote_time="2026-09-04T00:06:00Z",
                )
                runner = s25.S25V24Runner(params)
                runner.dm = s25.FakeDM()
                runner.executor = executor
                runner._suppress_manual_alerts = True
                self.assertTrue(runner.connect_and_preflight())
                before_positions = copy.deepcopy(runner._st(strategy)["positions"])
                before_state = Path(state_path).read_bytes()
                runner.run_once()

                self.assertEqual(executor.open_calls, 0)
                self.assertEqual(runner._st(strategy)["positions"], before_positions)
                self.assertEqual(Path(state_path).read_bytes(), before_state)
                self.assertEqual(runner._logical_position_counts(strategy), (1, 1))
                with open(trade_path, "r", newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertTrue(
                    any(row["reason"] == "legacy_inventory_shadow_canary_hold" for row in rows)
                )

                executor.positions = []
                before_mismatch = Path(state_path).read_bytes()
                runner.run_once()
                self.assertEqual(executor.open_calls, 0)
                self.assertEqual(Path(state_path).read_bytes(), before_mismatch)
                self.assertEqual(runner._st(strategy)["positions"], before_positions)

    def test_virtual_core_mapping_requires_the_same_side(self):
        params = copy.deepcopy(s25.load_params())
        strategy = params["strategies"][0]
        with tempfile.TemporaryDirectory(prefix="s25-v24-core-side-") as temp:
            with mock.patch.object(s25, "STATE_FILE", str(Path(temp) / "missing.json")):
                runner = s25.S25V24Runner(params)
                state = runner._st(strategy)
                state["episode_start_quote_utc"] = "2026-09-04T00:00:00+00:00"
                state["positions"] = [{
                    "ticket": 10,
                    "position_identifier": 5010,
                    "side": "SHORT",
                    "lot": 0.01,
                    "shadow": False,
                }]
                state["legacy_physical_core_position_ids"] = {"LONG": 5010, "SHORT": None}
                self.assertEqual(runner._virtual_core_flags(strategy), (1, 1))

    def test_nonflat_v5_inventory_is_adopted_without_seed_or_double_core(self):
        params = copy.deepcopy(s25.load_params())
        params["live_trading_enabled"] = True
        params["shadow_forward_enabled"] = False
        params["shadow_opportunity_observer"]["enabled"] = False
        params["shadow_state_tagger"]["enabled"] = False
        strategy = params["strategies"][0]
        live_positions = [
            self._live_position(101, "LONG", 4000.0),
            self._live_position(102, "LONG", 4010.0),
            self._live_position(201, "SHORT", 4020.0),
            self._live_position(202, "SHORT", 4030.0),
        ]

        with tempfile.TemporaryDirectory(prefix="s25-v24-v5-migration-") as temp:
            state_path = str(Path(temp) / "state" / "s25_bot_state.json")
            trade_path = str(Path(temp) / "logs" / "s25_trades.csv")
            legacy_positions = []
            for position in live_positions:
                legacy_positions.append(
                    {
                        "ticket": position.ticket,
                        "position_identifier": position.identifier,
                        "side": "LONG" if position.type == s25.ORDER_TYPE_BUY else "SHORT",
                        "lot": position.volume,
                        "entry_price": position.open_price,
                        "entry_time_utc": pd.Timestamp(position.open_time, unit="s", tz="UTC").isoformat(),
                        "open_time_epoch": position.open_time,
                        "owner_symbol": position.symbol,
                        "owner_magic": position.magic,
                        "owner_comment": position.comment,
                        "shadow": False,
                        "close_requested": False,
                    }
                )
            legacy_strategy_state = {
                "positions": legacy_positions,
                "episode_sequence": 9,
                "current_episode_id": "s25_m231_e000009",
                "episode_start_quote_utc": "2026-09-04T00:00:00+00:00",
                "active_wave": 1,
                "last_atr": 1.0,
                "last_ema": 4000.0,
                "last_long_frontier": 4010.0,
                "last_short_frontier": 4020.0,
                "last_processed_m5_bar": "2026-09-04T00:00:00+00:00",
                "last_quote_utc": "2026-09-04T00:05:00+00:00",
                "pending_open": None,
                "pending_close_reason": None,
                "sync_block_new_entries": False,
                "sync_block_reason": None,
            }
            legacy_state = {
                "version": 5,
                "bot": "bot25",
                "strategy_id": "bot25_man231_xauusd_bilateral_core_satellite_v001",
                "last_saved_utc": "2026-09-04T00:05:00+00:00",
                "strategies": {"man231_bilateral_book": legacy_strategy_state},
            }
            Path(state_path).parent.mkdir(parents=True, exist_ok=True)
            Path(state_path).write_text(json.dumps(legacy_state), encoding="utf-8")
            gate_name = params["real_trading_activation_env"]
            gate_value = params["real_trading_activation_value"]

            with (
                mock.patch.object(s25, "STATE_FILE", state_path),
                mock.patch.object(s25, "TRADE_LOG_FILE", trade_path),
                mock.patch.dict(os.environ, {gate_name: gate_value}),
            ):
                executor = CountingExecutor(positions=live_positions, quote_time="2026-09-04T00:06:00Z")
                runner = s25.S25V24Runner(params)
                runner.dm = s25.FakeDM()
                runner.executor = executor
                runner._suppress_manual_alerts = True

                self.assertEqual(runner._state_identity_status, "compatible_legacy_to_v24_pending")
                self.assertTrue(runner.connect_and_preflight())
                self.assertEqual(executor.open_calls, 0)
                self.assertEqual(runner._position_counts(strategy), (2, 2))
                self.assertEqual(runner._logical_position_counts(strategy), (2, 2))
                self.assertEqual(runner._virtual_core_flags(strategy), (0, 0))
                self.assertEqual(
                    runner._st(strategy)["legacy_physical_core_position_ids"],
                    {"LONG": 5101, "SHORT": 5202},
                )

                restarted_executor = CountingExecutor(
                    positions=live_positions,
                    quote_time="2026-09-04T00:06:01Z",
                )
                restarted = s25.S25V24Runner(params)
                restarted.dm = s25.FakeDM()
                restarted.executor = restarted_executor
                restarted._suppress_manual_alerts = True
                self.assertTrue(restarted.connect_and_preflight())
                self.assertEqual(restarted_executor.open_calls, 0)
                self.assertEqual(restarted._logical_position_counts(strategy), (2, 2))

                closed_core_id = 5101
                restarted_executor.positions = [
                    position for position in restarted_executor.positions
                    if position.identifier != closed_core_id
                ]
                restarted_executor.deals[closed_core_id] = SimpleNamespace(
                    position_id=closed_core_id,
                    symbol="XAUUSD",
                    magic=s25.EXPECTED_S25_MAGIC,
                    deal=88001,
                    price=4012.0,
                    net_profit=1.5,
                    profit=1.5,
                    commission=0.0,
                    swap=0.0,
                    fee=0.0,
                    reason="external_test_close",
                    deal_time=int(pd.Timestamp("2026-09-04T00:07:00Z").timestamp()),
                    exit_volume=0.01,
                )
                self.assertTrue(restarted._sync_strategy(strategy))
                self.assertEqual(restarted._position_counts(strategy), (1, 2))
                self.assertEqual(restarted._virtual_core_flags(strategy), (1, 0))
                self.assertEqual(restarted._logical_position_counts(strategy), (2, 2))

                with open(trade_path, "r", newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                migrated = [row for row in rows if row["event"] == "startup_state_migrated"]
                self.assertEqual(len(migrated), 1)
                self.assertEqual(migrated[0]["reason"], "nonflat_legacy_owned_inventory_upgraded_to_v24")
                self.assertFalse(any(row["reason"] == "bilateral_seed" for row in rows))

                after_close = s25.S25V24Runner(params)
                after_close.dm = s25.FakeDM()
                after_close.executor = restarted_executor
                after_close._suppress_manual_alerts = True
                self.assertTrue(after_close.connect_and_preflight())
                self.assertEqual(after_close._virtual_core_flags(strategy), (1, 0))
                self.assertEqual(after_close._logical_position_counts(strategy), (2, 2))
                self.assertEqual(restarted_executor.open_calls, 0)


if __name__ == "__main__":
    unittest.main()
