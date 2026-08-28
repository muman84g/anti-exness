"""No-order regression suite for the bot23 ZA live-port safety findings."""

from __future__ import annotations

import json
import math
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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

    def close_position(self, ticket: int, _deviation: int):
        self.close_calls.append(int(ticket))
        return True


class BridgeHealthLoggingRegressionTests(unittest.TestCase):
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
    with patch.object(live_s23_bot.os.path, "exists", return_value=False):
        runner = S23HorizontalInventoryRunner(params)
    runner.state = runner._default_state()
    runner._suppress_manual_alerts = True
    runner._save_state = lambda: None
    runner._trade_row = lambda *_args, **_kwargs: None
    strategy = params["strategies"][0]
    return runner, strategy, runner._st(strategy)


def arm_pending(state: dict, *, atr30: float | None = 1.5, target: float | None = 100.0) -> None:
    state.update(
        {
            "pending_entry_side": "LONG",
            "pending_entry_target": target,
            "pending_entry_expires_utc": dt_text(utc_now() + pd.Timedelta(minutes=5)),
            "pending_entry_atr30": atr30,
            "pending_entry_signal_bar": dt_text(utc_now() - pd.Timedelta(minutes=1)),
        }
    )


def arm_owned_basket(strategy: dict, state: dict, executor: CountingExecutor, *, ticket: int = 9401) -> None:
    position = SimpleNamespace(
        ticket=ticket,
        identifier=ticket,
        symbol="XAUUSD",
        magic=EXPECTED_S23_MAGIC,
        comment=strategy["comment_prefix"],
        type=ORDER_TYPE_BUY,
        volume=0.01,
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


def sample_opportunity(*, side: str = "LONG") -> tuple[dict, pd.Series, pd.Timestamp, SimpleNamespace]:
    poll_time = pd.Timestamp("2026-08-25 13:01:02", tz="UTC")
    event_time = pd.Timestamp("2026-08-25 13:00:00", tz="UTC")
    opportunity = {
        "opportunity_id": f"XAUUSD|{event_time.isoformat()}|{side}",
        "side": side,
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
    return opportunity, row, poll_time, SimpleNamespace(bid=100.0, ask=100.03)


class Bot23ZARegressionTests(unittest.TestCase):
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
        ):
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
        ), patch.object(live_s23_bot, "append_csv", side_effect=lambda _path, row, _fields: rows.append(dict(row))):
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
        state["current_basket_id"] = "L8-B000001"
        position = SimpleNamespace(ticket=ticket, identifier=ticket, symbol="XAUUSD", magic=int(strat["magic"]), comment=strat["comment_prefix"], type=ORDER_TYPE_BUY, volume=0.01)
        executor = CountingExecutor()
        executor.positions = [position]
        results = [
            live_executor.CloseResult(False, "MARKET_CLOSED", retcode=10018),
            live_executor.CloseResult(True, "CONFIRMED", deal_id=8801, retcode=10009),
        ]
        executor.close_position = lambda ticket, _deviation: (executor.close_calls.append(int(ticket)) or results.pop(0))
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

    def test_midday_capacity_one_blocks_second_position(self):
        runner, _za, _state = make_runner(live=False)
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
            def __init__(self):
                super().__init__()
                self.positions = [position]

            def open_position(self, *_args, **_kwargs):
                self.open_calls += 1
                return 7701

        runner.executor = ConfirmingExecutor()
        decision_time = broker_fill - pd.Timedelta(seconds=12)
        row = pd.Series({"Open": 100.0, "Close": 100.0, "AskOpen": 100.03}, name=pd.Timestamp("2026-08-28 00:09", tz="UTC"))
        self.assertTrue(
            runner._open_entry(
                strat,
                "LONG",
                row,
                SimpleNamespace(bid=100.0, ask=100.03),
                execution_time=decision_time,
                apply_portfolio_rearm=False,
                use_confirmed_fill_time=True,
            )
        )
        self.assertEqual(parse_ts(runner._st(strat)["basket"][0]["entry_time_utc"]), broker_fill)

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
            def __init__(self):
                super().__init__()
                self.positions = [position]

            def open_position(self, *_args, **_kwargs):
                self.open_calls += 1
                return 7702

        runner.executor = ConfirmingExecutor()
        decision_time = pd.Timestamp("2026-08-28 00:10:25", tz="UTC")
        row = pd.Series({"Open": 100.0, "Close": 100.0, "AskOpen": 100.03}, name=pd.Timestamp("2026-08-28 00:09", tz="UTC"))
        self.assertTrue(
            runner._open_entry(
                strat,
                "LONG",
                row,
                SimpleNamespace(bid=100.0, ask=100.03),
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
        self.assertEqual(registered["context"]["lane_positions"], {str(lane): 0 for lane in range(1, 9)})
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

    def test_account_identity_mismatch_is_rejected(self):
        runner, _strategy, _state = make_runner()
        with patch.object(live_s23_bot, "MT5_LOGIN", 123456), patch.object(
            live_s23_bot, "MT5_SERVER", "Expected-Server"
        ):
            error = runner._account_identity_error({"login": 123457, "server": "Expected-Server"})

        self.assertIsNotNone(error)
        self.assertIn("observed_login", str(error))

    def test_legacy_bridge_without_account_identity_is_rejected(self):
        runner, _strategy, _state = make_runner()

        error = runner._account_identity_error({"margin_mode": live_s23_bot.HEDGING_MARGIN_MODE})

        self.assertIn("account_identity_unavailable", str(error))

    def test_account_bridge_response_parses_login_and_server(self):
        response = (
            f"OK|{live_s23_bot.HEDGING_MARGIN_MODE}|RETAIL_HEDGING|1|1|1|1|"
            f"{live_s23_bot.MT5_LOGIN}|{live_s23_bot.MT5_SERVER}"
        )
        with patch.object(live_executor.ea_bridge, "send_command", return_value=response):
            account = live_executor.MT5Executor().get_account_info()

        self.assertEqual(account["login"], live_s23_bot.MT5_LOGIN)
        self.assertEqual(account["server"], live_s23_bot.MT5_SERVER)

    def test_symbol_info_parses_broker_quote_timestamp(self):
        response = "OK|2064.030|2064.000|1000.00|0.001|0.01|100.00|0.01|1.0|0.001|100.0|3|0|1787890200123"
        with patch.object(live_executor.ea_bridge, "send_command", return_value=response):
            info = live_executor.MT5Executor().get_symbol_info("XAUUSD")
        self.assertEqual(info.quote_time_msc, 1787890200123)

    def test_close_executor_preserves_market_closed_retcode(self):
        with patch.object(live_executor.ea_bridge, "send_command", return_value="ERR|10018|DEAL=0|LAST=0"):
            result = live_executor.MT5Executor().close_position(12345)
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

    def test_first_consuming_router_preserves_primary_then_uses_next_lane(self):
        runner, _strategy, _state = make_runner(live=False)
        opportunity, row, poll_time, info = sample_opportunity()
        strategies = runner.params["strategies"]
        runner._st(strategies[0])["cooldown_until_utc"] = dt_text(poll_time + pd.Timedelta(minutes=1))
        readiness = {lane: (True, "ready", False) for lane in range(1, 5)}

        runner._route_opportunity(opportunity, row, info, poll_time, readiness)

        self.assertFalse(runner._st(strategies[0])["basket"])
        self.assertEqual(len(runner._st(strategies[1])["basket"]), 1)
        self.assertFalse(runner._st(strategies[2])["basket"])
        self.assertFalse(runner._st(strategies[3])["basket"])
        self.assertEqual(runner.state["routing"]["last_consumed_lane_id"], 2)

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

        runner._route_opportunity(opportunity, row, info, poll_time, readiness)

        strategies = runner.params["strategies"]
        self.assertEqual(executor.open_calls, 1)
        self.assertEqual(runner.state["routing"]["last_consumed_lane_id"], 1)
        self.assertEqual(runner._st(strategies[0])["sync_block_reason"], "ambiguous_open_result")
        self.assertTrue(all(not runner._st(strategy)["basket"] for strategy in strategies))

    def test_pending_fill_on_signal_tick_consumes_before_later_lanes(self):
        runner, _strategy, _state = make_runner(live=False)
        runner.executor = CountingExecutor()
        strategies = runner.params["strategies"]
        opportunity, row, poll_time, info = sample_opportunity()
        arm_pending(runner._st(strategies[0]), atr30=2.5, target=100.03)

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
        runner._notify_manual_action = lambda _strategy, **kwargs: alerts.append(kwargs)

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
            deal_time=int(deal_time.timestamp()),
        )
        with patch.object(live_s23_bot, "utc_now", return_value=pd.Timestamp("2026-08-25 00:05:00", tz="UTC").to_pydatetime()):
            self.assertTrue(runner._sync_strategy(strategy))

        self.assertEqual(state["daily_realized_date_utc"], "2026-08-24")
        self.assertEqual(state["daily_realized_pnl_usd"], -4.25)

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
        runner.state["routing"]["long_target_rearm_until_utc"] = dt_text(start + pd.Timedelta(minutes=8))
        row = pd.Series({"Open": 100.0, "Close": 100.0, "AskOpen": 100.03}, name=start)
        info = SimpleNamespace(bid=100.0, ask=100.03)

        self.assertFalse(runner._open_entry(strategy, "LONG", row, info, execution_time=start, basket_atr30=2.0))
        self.assertFalse(state["basket"])
        self.assertTrue(runner._open_entry(strategy, "SHORT", row, info, execution_time=start, basket_atr30=2.0))
        self.assertEqual(state["basket"][0]["side"], "SHORT")

    def test_active_rearm_does_not_block_existing_long_add(self):
        runner, strategy, state = make_runner(live=False)
        start = pd.Timestamp("2026-08-25 13:10:00", tz="UTC")
        runner.state["routing"]["long_target_rearm_until_utc"] = dt_text(start + pd.Timedelta(minutes=8))
        state["basket"] = [{"side": "LONG", "lot": 0.01, "entry_price": 100.0}]
        state["last_add_price"] = 100.0
        opportunity, row, _poll_time, info = sample_opportunity(side="LONG")
        row["Close"] = 102.0
        info = SimpleNamespace(bid=102.0, ask=102.03)

        consumed, reason = runner._consume_opportunity(strategy, opportunity, row, info, start)

        self.assertTrue(consumed)
        self.assertEqual(reason, "add_attempted")
        self.assertEqual(len(state["basket"]), 2)

    def test_invalid_rearm_timestamp_fails_closed_for_new_long_only(self):
        runner, _strategy, _state = make_runner(live=False)
        runner.state["routing"]["long_target_rearm_until_utc"] = "not-a-timestamp"
        at = pd.Timestamp("2026-08-25 13:10:00", tz="UTC")

        self.assertEqual(runner._portfolio_new_long_basket_block_reason("LONG", at), "long_target_rearm_state_invalid")
        self.assertIsNone(runner._portfolio_new_long_basket_block_reason("SHORT", at))

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

    def test_orders_unavailable_blocks_pending_and_final_open(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor(orders_available=False)
        runner.executor = executor
        arm_pending(state)

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(state["sync_block_reason"], "orders_unavailable")
        quote = SimpleNamespace(bid=99.99, ask=100.0)
        self.assertTrue(runner._monitor_pending_entry(strategy, quote, utc_now()))
        row = pd.Series({"Open": 99.99, "Close": 99.99, "AskOpen": 100.0}, name=pd.Timestamp(utc_now()))
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
        arm_pending(state)

        self.assertFalse(runner._sync_strategy(strategy))
        self.assertEqual(state["sync_block_reason"], "same_magic_unexpected_order")
        self.assertTrue(runner._monitor_pending_entry(strategy, SimpleNamespace(bid=99.99, ask=100.0), utc_now()))
        self.assertEqual(executor.open_calls, 0)

    def test_retry_cooldown_blocks_pending_and_final_open(self):
        runner, strategy, state = make_runner()
        executor = CountingExecutor()
        runner.executor = executor
        arm_pending(state)
        state["open_retry_after_utc"] = dt_text(utc_now() + pd.Timedelta(seconds=30))
        quote = SimpleNamespace(bid=99.99, ask=100.0)

        self.assertTrue(runner._monitor_pending_entry(strategy, quote, utc_now() + pd.Timedelta(seconds=5)))
        row = pd.Series({"Open": 99.99, "Close": 99.99, "AskOpen": 100.0}, name=pd.Timestamp(utc_now()))
        runner._open_entry(strategy, "LONG", row, quote, basket_atr30=1.5, execution_time=utc_now() + pd.Timedelta(seconds=5))
        self.assertEqual(executor.open_calls, 0)
        self.assertFalse(state["basket"])

    def test_high_vol_refreshed_pending_ignores_low_vol_spread_gate(self):
        runner, strategy, state = make_runner(live=False)
        arm_pending(state, atr30=2.5, target=100.0)
        quote = SimpleNamespace(bid=99.70, ask=100.0)

        self.assertTrue(runner._monitor_pending_entry(strategy, quote, utc_now()))
        self.assertEqual(len(state["basket"]), 1)
        self.assertTrue(math.isclose(float(state["frozen_basket_atr30"]), 2.5))

    def test_low_vol_pending_retains_spread_gate(self):
        runner, strategy, state = make_runner(live=False)
        arm_pending(state, atr30=1.5, target=100.0)
        quote = SimpleNamespace(bid=99.80, ask=100.0)

        self.assertFalse(runner._monitor_pending_entry(strategy, quote, utc_now()))
        self.assertFalse(state["basket"])
        self.assertEqual(state["pending_entry_side"], "LONG")

    def test_malformed_pending_state_is_cleared_without_open_or_crash(self):
        malformed = {
            "null_target": {"pending_entry_target": None},
            "null_atr": {"pending_entry_atr30": None},
            "bad_expiry": {"pending_entry_expires_utc": "not-a-timestamp"},
            "bad_side": {"pending_entry_side": "SIDEWAYS"},
        }
        for label, mutation in malformed.items():
            with self.subTest(label=label):
                runner, strategy, state = make_runner()
                executor = CountingExecutor()
                runner.executor = executor
                arm_pending(state)
                state.update(mutation)

                self.assertTrue(runner._monitor_pending_entry(strategy, SimpleNamespace(bid=99.99, ask=100.0), utc_now()))
                self.assertIsNone(state["pending_entry_side"])
                self.assertFalse(state["basket"])
                self.assertEqual(executor.open_calls, 0)

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
        runner._notify_manual_action = lambda *_args, **kwargs: alerts.append(kwargs)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
