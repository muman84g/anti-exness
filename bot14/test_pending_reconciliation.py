import sys
import types
import unittest
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BOT_DIR))

live_data_fetcher_stub = types.ModuleType("live_data_fetcher")
live_data_fetcher_stub.MT5DataManager = object
sys.modules["live_data_fetcher"] = live_data_fetcher_stub

live_executor_stub = types.ModuleType("live_executor")
live_executor_stub.MT5Executor = object
live_executor_stub.ORDER_TYPE_BUY = 0
live_executor_stub.ORDER_TYPE_SELL = 1
sys.modules["live_executor"] = live_executor_stub

from live_s14_bot import apply_confirmed_pending_open_reconciliations  # noqa: E402


class PendingOpenReconciliationTests(unittest.TestCase):
    def build_states(self):
        return {
            "AUDUSD": {
                "pos_A": None,
                "mc_manager": {"unchanged": True},
                "pending_open": {
                    "symbol": "AUDUSD",
                    "request_id": "3bbbc9ff",
                },
                "reconciliation_required": {
                    "type": "pending_open",
                    "request_id": "3bbbc9ff",
                },
                "sync_block_new_entries": True,
                "sync_block_reason": "Unresolved pending_open request: 3bbbc9ff",
            },
            "GBPUSD": {
                "pos_A": {"ticket": 2002834244},
                "mc_manager": {"unchanged": True},
            },
        }

    def test_exact_confirmed_request_clears_only_matching_audusd_block(self):
        states = self.build_states()
        gbpusd_before = dict(states["GBPUSD"])

        applied = apply_confirmed_pending_open_reconciliations(
            states,
            [
                {
                    "symbol": "AUDUSD",
                    "request_id": "3bbbc9ff",
                    "confirmed_no_position_order_or_deal": True,
                    "confirmed_at_jst": "2026-06-22",
                    "reason": "manual verification",
                }
            ],
        )

        self.assertEqual(
            applied,
            [{"symbol": "AUDUSD", "request_id": "3bbbc9ff"}],
        )
        for key in (
            "pending_open",
            "reconciliation_required",
            "sync_block_new_entries",
            "sync_block_reason",
        ):
            self.assertNotIn(key, states["AUDUSD"])
        self.assertEqual(states["AUDUSD"]["mc_manager"], {"unchanged": True})
        self.assertEqual(states["GBPUSD"], gbpusd_before)
        self.assertEqual(
            states["AUDUSD"]["manual_pending_open_reconciliations"][0][
                "request_id"
            ],
            "3bbbc9ff",
        )

    def test_unconfirmed_or_mismatched_request_is_not_cleared(self):
        for directive in (
            {
                "symbol": "AUDUSD",
                "request_id": "3bbbc9ff",
                "confirmed_no_position_order_or_deal": False,
            },
            {
                "symbol": "AUDUSD",
                "request_id": "different",
                "confirmed_no_position_order_or_deal": True,
            },
            {
                "symbol": "GBPUSD",
                "request_id": "3bbbc9ff",
                "confirmed_no_position_order_or_deal": True,
            },
        ):
            with self.subTest(directive=directive):
                states = self.build_states()
                applied = apply_confirmed_pending_open_reconciliations(
                    states,
                    [directive],
                )
                self.assertEqual(applied, [])
                self.assertIn("pending_open", states["AUDUSD"])


if __name__ == "__main__":
    unittest.main()
