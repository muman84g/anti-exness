import sys
import tempfile
import types
import unittest
import csv
from pathlib import Path
from unittest.mock import patch


BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

live_data_fetcher_stub = types.ModuleType("live_data_fetcher")
live_data_fetcher_stub.MT5DataManager = object
sys.modules["live_data_fetcher"] = live_data_fetcher_stub

import live_executor
import live_s14_bot
from live_executor import MT5Executor
from live_s14_bot import (
    S14TradingBot,
    apply_confirmed_missing_position_reconciliations,
)


class RecordingManager:
    def __init__(self):
        self.calls = []

    def update_mc(self, *args):
        self.calls.append(args)

    def to_dict(self):
        return {"calls": self.calls}


def make_bot(price_hint="WIN", absence=True):
    bot = S14TradingBot.__new__(S14TradingBot)
    bot.mc_manager = RecordingManager()
    bot.executor = type(
        "Executor",
        (),
        {"confirm_position_absent": lambda self, ticket: absence},
    )()
    position = {
        "ticket": 12345,
        "direction": "LONG",
        "entry_price": 1.3000,
        "sl": 1.2957,
        "tp": 1.3043,
        "lot_size": 0.01,
        "bet_units": 1,
    }
    bot.state = {
        "pos_A": position,
        "pos_B": None,
        "next_direction_A": None,
        "next_direction_B": None,
        "pair_initialized": False,
        "initial_anchor_A": 1.3000,
        "pair_mode": "INITIALIZING",
    }
    bot.save_state = lambda: True
    bot.active_symbol = "GBPUSD"
    bot.logged_trades = []

    def record_trade(*args, **kwargs):
        bot.logged_trades.append((args, kwargs))
        return True

    bot.log_trade_csv = record_trade
    if price_hint == "WIN":
        prices = (1.3044, 1.3045)
    elif price_hint == "LOSE":
        prices = (1.2956, 1.2957)
    else:
        prices = (1.3001, 1.3002)
    return bot, position, prices


class ExitReconciliationTests(unittest.TestCase):
    def test_operator_confirmed_legacy_exits_require_exact_match(self):
        manager = RecordingManager()
        states = {
            "GBPUSD": {
                "pos_A": {
                    "ticket": 2002834244,
                    "direction": "LONG",
                    "bet_units": 1,
                    "missing_on_mt5": True,
                },
                "pos_B": {
                    "ticket": 2003802449,
                    "direction": "LONG",
                    "bet_units": 1,
                    "missing_on_mt5": True,
                },
                "next_direction_A": None,
                "next_direction_B": None,
                "pair_initialized": True,
                "initial_anchor_A": 1.32053,
                "pair_mode": "CAPITAL",
                "reconciliation_required": {"ticket": 2003802449},
            }
        }
        directives = [
            {
                "symbol": "GBPUSD",
                "bot_type": "B",
                "ticket": 2003802449,
                "direction": "LONG",
                "outcome": "LOSE",
                "confirmed_by_operator": True,
            },
            {
                "symbol": "GBPUSD",
                "bot_type": "A",
                "ticket": 2002834244,
                "direction": "LONG",
                "outcome": "WIN",
                "confirmed_by_operator": True,
            },
            {
                "symbol": "GBPUSD",
                "bot_type": "A",
                "ticket": 999,
                "direction": "LONG",
                "outcome": "WIN",
                "confirmed_by_operator": True,
            },
        ]

        applied = apply_confirmed_missing_position_reconciliations(
            states,
            {"GBPUSD": manager},
            directives,
        )

        self.assertEqual(len(applied), 2)
        self.assertIsNone(states["GBPUSD"]["pos_A"])
        self.assertIsNone(states["GBPUSD"]["pos_B"])
        self.assertEqual(states["GBPUSD"]["next_direction_A"], "SHORT")
        self.assertEqual(states["GBPUSD"]["next_direction_B"], "LONG")
        self.assertEqual(
            manager.calls,
            [(None, "LOSE", 0, 1), ("WIN", None, 1, 0)],
        )
        self.assertEqual(
            len(states["GBPUSD"]["manual_missing_position_reconciliations"]),
            2,
        )

    def test_executor_confirms_only_explicit_not_found(self):
        executor = MT5Executor.__new__(MT5Executor)
        for response in ("ERR|POSITION_NOT_FOUND", "ERR|Position Not Found", "ERR|0", "ERR|10009"):
            with self.subTest(response=response), patch.object(
                live_executor.ea_bridge,
                "send_command",
                return_value=response,
            ):
                self.assertTrue(executor.confirm_position_absent(12345))

        with patch.object(
            live_executor.ea_bridge,
            "send_command",
            return_value="OK|12345,GBPUSD,0,0.01,1.3000,1.2957,1.3043,140034,test",
        ):
            self.assertFalse(executor.confirm_position_absent(12345))
        with patch.object(
            live_executor.ea_bridge,
            "send_command",
            return_value="ERR|TIMEOUT",
        ):
            self.assertIsNone(executor.confirm_position_absent(12345))

    def test_corroborated_tp_removes_position_and_reverses(self):
        bot, position, prices = make_bot("WIN", absence=True)
        symbol_info = type("Info", (), {"price_unit_value": 100000.0})()
        changed, blocked = bot.handle_missing_state_position(
            "A", position, prices[0], prices[1], 0.0001, symbol_info
        )

        self.assertTrue(changed)
        self.assertFalse(blocked)
        self.assertIsNone(bot.state["pos_A"])
        self.assertEqual(bot.state["next_direction_A"], "SHORT")
        self.assertEqual(bot.mc_manager.calls, [("WIN", None, 1, 0)])
        args, kwargs = bot.logged_trades[0]
        self.assertEqual(args[0], "EXIT_SYNC_WIN")
        self.assertEqual(args[1], 12345)
        self.assertAlmostEqual(args[5], 1.3043)
        self.assertAlmostEqual(args[6], 4.3)
        self.assertIn("PNL_ESTIMATED_FROM_BROKER_TICK_VALUE", args[7])
        self.assertTrue(kwargs["deduplicate"])

    def test_corroborated_sl_continues_same_direction(self):
        bot, position, prices = make_bot("LOSE", absence=True)
        symbol_info = type("Info", (), {"price_unit_value": 100000.0})()
        changed, blocked = bot.handle_missing_state_position(
            "A", position, prices[0], prices[1], 0.0001, symbol_info
        )

        self.assertTrue(changed)
        self.assertFalse(blocked)
        self.assertIsNone(bot.state["pos_A"])
        self.assertEqual(bot.state["next_direction_A"], "LONG")
        self.assertEqual(bot.mc_manager.calls, [("LOSE", None, 1, 0)])
        args, kwargs = bot.logged_trades[0]
        self.assertEqual(args[0], "EXIT_SYNC_LOSE")
        self.assertAlmostEqual(args[5], 1.2957)
        self.assertAlmostEqual(args[6], -4.3)
        self.assertTrue(kwargs["deduplicate"])

    def test_missing_exit_without_tick_value_logs_blank_pnl(self):
        bot, position, prices = make_bot("LOSE", absence=True)
        changed, blocked = bot.handle_missing_state_position(
            "A", position, prices[0], prices[1], 0.0001
        )

        self.assertTrue(changed)
        self.assertFalse(blocked)
        args, _ = bot.logged_trades[0]
        self.assertIsNone(args[6])
        self.assertIn("PNL_UNAVAILABLE", args[7])

    def test_missing_exit_log_failure_blocks_state_transition(self):
        bot, position, prices = make_bot("LOSE", absence=True)
        bot.log_trade_csv = lambda *args, **kwargs: False
        changed, blocked = bot.handle_missing_state_position(
            "A", position, prices[0], prices[1], 0.0001
        )

        self.assertFalse(changed)
        self.assertTrue(blocked)
        self.assertIs(bot.state["pos_A"], position)
        self.assertEqual(bot.mc_manager.calls, [])

    def test_manual_price_hint_remains_blocked(self):
        bot, position, prices = make_bot("MANUAL", absence=True)
        changed, blocked = bot.handle_missing_state_position(
            "A", position, prices[0], prices[1], 0.0001
        )

        self.assertTrue(changed)
        self.assertTrue(blocked)
        self.assertIs(bot.state["pos_A"], position)
        self.assertEqual(bot.mc_manager.calls, [])

    def test_bridge_timeout_remains_blocked_even_at_tp(self):
        bot, position, prices = make_bot("WIN", absence=None)
        changed, blocked = bot.handle_missing_state_position(
            "A", position, prices[0], prices[1], 0.0001
        )

        self.assertTrue(changed)
        self.assertTrue(blocked)
        self.assertIs(bot.state["pos_A"], position)
        self.assertEqual(bot.mc_manager.calls, [])

    def test_trade_csv_deduplicates_same_exit_action_and_ticket(self):
        bot = S14TradingBot.__new__(S14TradingBot)
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            live_s14_bot,
            "LOG_DIR",
            temp_dir,
        ):
            first = bot.log_trade_csv(
                "EXIT_SYNC_LOSE",
                12345,
                "GBPUSD",
                "LONG",
                0.01,
                1.2957,
                -4.3,
                "LOSE:TEST",
                deduplicate=True,
            )
            second = bot.log_trade_csv(
                "EXIT_SYNC_LOSE",
                12345,
                "GBPUSD",
                "LONG",
                0.01,
                1.2957,
                -4.3,
                "LOSE:TEST",
                deduplicate=True,
            )
            csv_file = Path(temp_dir) / "s14_trades.csv"
            with csv_file.open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
