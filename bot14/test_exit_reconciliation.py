import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

live_data_fetcher_stub = types.ModuleType("live_data_fetcher")
live_data_fetcher_stub.MT5DataManager = object
sys.modules["live_data_fetcher"] = live_data_fetcher_stub

import live_executor
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
        changed, blocked = bot.handle_missing_state_position(
            "A", position, prices[0], prices[1], 0.0001
        )

        self.assertTrue(changed)
        self.assertFalse(blocked)
        self.assertIsNone(bot.state["pos_A"])
        self.assertEqual(bot.state["next_direction_A"], "SHORT")
        self.assertEqual(bot.mc_manager.calls, [("WIN", None, 1, 0)])

    def test_corroborated_sl_continues_same_direction(self):
        bot, position, prices = make_bot("LOSE", absence=True)
        changed, blocked = bot.handle_missing_state_position(
            "A", position, prices[0], prices[1], 0.0001
        )

        self.assertTrue(changed)
        self.assertFalse(blocked)
        self.assertIsNone(bot.state["pos_A"])
        self.assertEqual(bot.state["next_direction_A"], "LONG")
        self.assertEqual(bot.mc_manager.calls, [("LOSE", None, 1, 0)])

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


if __name__ == "__main__":
    unittest.main()
