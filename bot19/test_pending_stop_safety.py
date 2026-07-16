import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import live_executor
from live_s19_bot import S19SnowballBot, load_params


class FakeExecutor:
    def __init__(self, fail_on=None, live_orders=None, live_positions=None):
        self.fail_on = fail_on
        self.live_orders = list(live_orders or [])
        self.live_positions = list(live_positions or [])
        self.calls = []
        self.canceled = []
        self.last_order_error = None

    def place_stop_order(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.fail_on is not None and len(self.calls) == self.fail_on:
            self.last_order_error = "SIMULATED_FAIL"
            return None
        return 7000 + len(self.calls)

    def cancel_order(self, ticket):
        self.canceled.append(int(ticket))
        return True

    def get_positions(self, symbol, magic=None):
        return list(self.live_positions)

    def get_orders(self, symbol, magic=None):
        return list(self.live_orders)

    def modify_position_sl_tp(self, *args, **kwargs):
        return True


def make_info():
    return SimpleNamespace(
        bid=1.25000,
        ask=1.25009,
        point=0.00001,
        digits=5,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        contract_size=100000.0,
    )


def make_bot(name, executor):
    params = load_params()
    params.update(
        {
            "state_file": os.path.join(tempfile.gettempdir(), f"{name}_state.json"),
            "trade_log_file": os.path.join(tempfile.gettempdir(), f"{name}_trades.csv"),
            "policy_log_file": os.path.join(tempfile.gettempdir(), f"{name}_policy.csv"),
        }
    )
    try:
        os.remove(params["state_file"])
    except FileNotFoundError:
        pass
    bot = S19SnowballBot(params, policy=None)
    bot.executor = executor
    bot.dm = object()
    bot.state = bot.default_state()
    return bot


class PendingStopSafetyTests(unittest.TestCase):
    def test_live_params_are_enabled_for_server_pending_stop(self):
        params = load_params()
        self.assertTrue(params["enabled"])
        self.assertTrue(params["live_trading_enabled"])
        self.assertFalse(params["shadow_forward_enabled"])
        self.assertTrue(params["use_server_pending_entry"])
        self.assertEqual(params["profiles"][0]["symbol"], "GBPUSD")
        self.assertEqual(params["profiles"][0]["magic"], 190019)

    def test_cycle_start_places_four_server_pending_stops(self):
        executor = FakeExecutor()
        bot = make_bot("s19_success", executor)
        regime = {"entry_allowed": True, "signal_fresh": True}

        self.assertTrue(bot.start_cycle(1.25000, make_info(), 1.25009, regime))

        self.assertEqual(len(bot.state["virtual_orders"]), 4)
        self.assertIsNone(bot.state["pending_open"])
        self.assertIsNone(bot.state["reconciliation_required"])
        self.assertEqual([order["pending_ticket"] for order in bot.state["virtual_orders"]], [7001, 7002, 7003, 7004])
        order_types = [call[0][1] for call in executor.calls]
        self.assertEqual(order_types, [4, 4, 5, 5])

    def test_partial_pending_failure_cancels_created_orders_and_blocks(self):
        executor = FakeExecutor(fail_on=3)
        bot = make_bot("s19_partial_fail", executor)
        regime = {"entry_allowed": True, "signal_fresh": True}

        self.assertFalse(bot.start_cycle(1.25000, make_info(), 1.25009, regime))

        self.assertEqual(bot.state["virtual_orders"], [])
        self.assertEqual(executor.canceled, [7001, 7002])
        self.assertTrue(bot.state["sync_block_new_entries"])
        self.assertIsInstance(bot.state["pending_open"], dict)
        self.assertEqual(bot.state["pending_open"]["status"], "OPEN_RESPONSE_UNCONFIRMED")
        self.assertEqual(bot.state["reconciliation_required"]["type"], "pending_open")

    def test_cycle_start_without_live_context_does_not_increment_or_place_orders(self):
        executor = FakeExecutor()
        bot = make_bot("s19_missing_context", executor)

        self.assertFalse(bot.start_cycle(1.25000))

        self.assertEqual(bot.state["cycle_id"], 0)
        self.assertEqual(bot.state["virtual_orders"], [])
        self.assertEqual(executor.calls, [])
        self.assertTrue(bot.state["sync_block_new_entries"])
        self.assertEqual(bot.state["sync_block_reason"], "pending cycle start requires tick/regime context")

    def test_untracked_live_pending_order_blocks_new_entries(self):
        extra_order = SimpleNamespace(ticket=9999, symbol="GBPUSD", magic=190019, direction="LONG")
        executor = FakeExecutor(live_orders=[extra_order])
        bot = make_bot("s19_untracked_pending", executor)
        tick = {"bid": 1.25000, "ask": 1.25009, "info": make_info(), "spread_points": 9.0}
        regime = {"entry_allowed": True, "signal_fresh": True}

        self.assertFalse(bot.sync_live_positions(tick, regime))
        self.assertIn("untracked live pending orders", bot.state["sync_block_reason"])

    def test_cancel_order_does_not_treat_invalid_request_as_absent(self):
        executor = live_executor.MT5Executor.__new__(live_executor.MT5Executor)
        with patch.object(live_executor.ea_bridge, "send_command", return_value="ERR|10013"):
            self.assertFalse(executor.cancel_order(12345))
        with patch.object(live_executor.ea_bridge, "send_command", return_value="ERR|ORDER_NOT_FOUND"):
            self.assertTrue(executor.cancel_order(12345))

    def test_bridge_capabilities_parse_and_reject_stale_bridge(self):
        executor = live_executor.MT5Executor.__new__(live_executor.MT5Executor)
        caps_response = (
            "OK|CAPS|BotBridge_s19|test|"
            "ECHO,INFO,HIST,PENDING,POSITIONS,POSITION,ORDERS,MODIFY,CANCEL,CLOSE"
        )
        with patch.object(live_executor.ea_bridge, "send_command", return_value=caps_response):
            caps = executor.get_bridge_capabilities()
        self.assertEqual(caps["name"], "BotBridge_s19")
        self.assertIn("ORDERS", caps["commands"])

        with patch.object(live_executor.ea_bridge, "send_command", return_value="ERR|UNKNOWN_COMMAND"):
            self.assertIsNone(executor.get_bridge_capabilities())


if __name__ == "__main__":
    unittest.main()
