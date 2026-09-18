"""No-order ED hold and historical-state regression tests."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

import live_s23_bot as bot
from test_s23_regressions import CountingExecutor


class EDWinHoldPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory(prefix="s23-ed-hold-")
        self.addCleanup(self.folder.cleanup)
        self.state_path = Path(self.folder.name) / "state.json"
        state_patch = patch.object(bot, "STATE_FILE", str(self.state_path))
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.params = bot.load_params()
        for key in (
            "shadow_opportunity_observer", "shadow_state_tagger",
            "midday_shadow_opportunity_observer", "midday_shadow_state_tagger",
            "pre_eu30_shadow_opportunity_observer", "pre_eu30_shadow_state_tagger",
        ):
            self.params[key]["enabled"] = False
        self.params["live_trading_enabled"] = False
        self.params["shadow_forward_enabled"] = True
        self.ed = self.params["t0530_edge_strategies"][0]
        seed = bot.S23HorizontalInventoryRunner.__new__(bot.S23HorizontalInventoryRunner)
        seed.params = self.params
        self.base = seed._default_state()

    def _active(self, state: dict, side: str = "LONG") -> dict:
        lane = state["strategies"][self.ed["id"]]
        lane["basket_sequence"] = 1
        lane["current_basket_id"] = f"L{self.ed['lane_id']}-B000001"
        lane["basket"] = [{
            "ticket": 101, "position_identifier": 101, "side": side,
            "lot": 0.01, "entry_price": 100.0,
            "entry_time_utc": "2026-09-18T10:00:00+00:00",
        }]
        return lane

    def _load(self, state: dict) -> bot.S23HorizontalInventoryRunner:
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        return bot.S23HorizontalInventoryRunner(self.params)

    def test_legacy_open_basket_stays_native_after_save_restart(self) -> None:
        state = copy.deepcopy(self.base)
        self._active(state)
        state["routing"].pop("t0530_edge_long_win_hold_policy_id")
        state["routing"].pop("t0530_edge_long_win_hold_params_hash")
        for row in self.params["t0530_edge_strategies"]:
            state["strategies"][row["id"]].pop("t0530_edge_hold_decision")
        runner = self._load(state)
        self.assertEqual(runner._st(self.ed)["t0530_edge_hold_decision"], "native")
        self.assertFalse(runner._st(self.ed)["sync_block_new_entries"])
        runner._save_state()
        restarted = bot.S23HorizontalInventoryRunner(self.params)
        self.assertEqual(restarted._st(self.ed)["t0530_edge_hold_decision"], "native")
        self.assertFalse(restarted._ed_win_hold_state_migrated)

    def test_missing_identity_with_new_lane_field_is_partial(self) -> None:
        state = copy.deepcopy(self.base)
        self._active(state)["t0530_edge_hold_decision"] = "extend"
        state["routing"].pop("t0530_edge_long_win_hold_policy_id")
        state["routing"].pop("t0530_edge_long_win_hold_params_hash")
        runner = self._load(state)
        lane = runner._st(self.ed)
        self.assertTrue(lane["basket"])
        self.assertEqual(lane["t0530_edge_hold_decision"], "native")
        self.assertEqual(lane["sync_block_reason"], "t0530_edge_win_hold_state_partial")
        runner._save_state()
        restarted = bot.S23HorizontalInventoryRunner(self.params)
        self.assertEqual(restarted._st(self.ed)["sync_block_reason"], "t0530_edge_win_hold_state_partial")

    def test_missing_decision_or_short_extension_blocks_new_entries(self) -> None:
        for case in ("missing", "short"):
            with self.subTest(case=case):
                state = copy.deepcopy(self.base)
                lane = self._active(state, "SHORT" if case == "short" else "LONG")
                if case == "missing":
                    lane.pop("t0530_edge_hold_decision")
                else:
                    lane["t0530_edge_hold_decision"] = "extend"
                runner = self._load(state)
                lane = runner._st(self.ed)
                self.assertTrue(lane["basket"])
                self.assertEqual(lane["t0530_edge_hold_decision"], "native")
                self.assertEqual(lane["sync_block_reason"], "t0530_edge_win_hold_state_partial")

    def test_positive_long_extends_and_negative_long_stays_native(self) -> None:
        for bid, expected_hold, expected_decision in (
            (100.2, 60, "extend"), (99.9, 15, "native"),
        ):
            with self.subTest(bid=bid):
                state = copy.deepcopy(self.base)
                self._active(state)["t0530_edge_hold_decision"] = "pending"
                runner = self._load(state)
                observed: list[tuple[int, str]] = []
                runner._monitor_fixed_hold_position = lambda strat, info, poll, reason: observed.append(
                    (int(strat["hold_minutes"]), runner._st(self.ed)["t0530_edge_hold_decision"])
                ) or False
                before = pd.Timestamp("2026-09-18T10:14:59Z")
                due = pd.Timestamp("2026-09-18T10:15:00Z")
                runner._monitor_t0530_edge_position(
                    self.ed, SimpleNamespace(bid=bid, ask=bid + .1, quote_time_msc=int(before.timestamp() * 1000)), before,
                )
                self.assertEqual(observed[-1], (15, "pending"))
                runner._monitor_t0530_edge_position(
                    self.ed, SimpleNamespace(bid=bid, ask=bid + .1, quote_time_msc=int(due.timestamp() * 1000)), due,
                )
                self.assertEqual(observed[-1], (expected_hold, expected_decision))
                restarted = bot.S23HorizontalInventoryRunner(self.params)
                self.assertEqual(restarted._st(self.ed)["t0530_edge_hold_decision"], expected_decision)

    def test_partial_state_preserves_exact_owned_exit_but_blocks_new_entry(self) -> None:
        state = copy.deepcopy(self.base)
        self._active(state).pop("t0530_edge_hold_decision")
        runner = self._load(state)
        lane = runner._st(self.ed)
        self.assertTrue(lane["sync_block_new_entries"])
        runner.live_enabled = True
        executor = CountingExecutor()
        entry = pd.Timestamp("2026-09-18T10:00:00Z")
        position = lane["basket"][0]
        position.update({
            "open_time_epoch": int(entry.timestamp()),
            "owner_symbol": "XAUUSD", "owner_magic": int(self.ed["magic"]),
            "owner_comment": self.ed["comment_prefix"], "shadow": False,
            "lane_id": int(self.ed["lane_id"]),
            "basket_id": lane["current_basket_id"],
        })
        executor.positions = [SimpleNamespace(
            ticket=101, identifier=101, symbol="XAUUSD",
            magic=int(self.ed["magic"]), comment=self.ed["comment_prefix"],
            type=bot.ORDER_TYPE_BUY, volume=0.01, open_price=100.0,
            open_time=int(entry.timestamp()),
        )]
        runner.executor = executor
        self.assertTrue(runner._sync_strategy(self.ed))
        self.assertEqual(lane["sync_block_reason"], "t0530_edge_win_hold_state_partial")
        observed = []
        runner._monitor_fixed_hold_position = lambda strat, *_args: observed.append(int(strat["hold_minutes"])) or True
        due = entry + pd.Timedelta(minutes=15)
        quote = SimpleNamespace(bid=100.2, ask=100.3, quote_time_msc=int(due.timestamp() * 1000))
        runner._process_t0530_edge_exits(quote, due)
        self.assertEqual(observed, [15])
        self.assertTrue(lane["sync_block_new_entries"])

        executor.orders = [SimpleNamespace(ticket=201, symbol="XAUUSD", magic=int(self.ed["magic"]), comment="unexpected")]
        self.assertFalse(runner._sync_strategy(self.ed))
        self.assertTrue(lane["sync_block_new_entries"])

    def test_broker_fill_milliseconds_survive_sync_restart_and_due_gate(self) -> None:
        state = copy.deepcopy(self.base)
        self._active(state)["t0530_edge_hold_decision"] = "pending"
        runner = self._load(state)
        runner.live_enabled = True
        lane = runner._st(self.ed)
        entry = pd.Timestamp("2026-09-18T10:00:00.723Z")
        position = lane["basket"][0]
        position.update({
            "open_time_epoch": int(entry.timestamp()),
            "owner_symbol": "XAUUSD", "owner_magic": int(self.ed["magic"]),
            "owner_comment": self.ed["comment_prefix"], "shadow": False,
            "lane_id": int(self.ed["lane_id"]),
            "basket_id": lane["current_basket_id"],
        })
        executor = CountingExecutor()
        executor.positions = [SimpleNamespace(
            ticket=101, identifier=101, symbol="XAUUSD",
            magic=int(self.ed["magic"]), comment=self.ed["comment_prefix"],
            type=bot.ORDER_TYPE_BUY, volume=0.01, open_price=100.0,
            open_time=int(entry.timestamp()), open_time_msc=int(entry.timestamp() * 1000),
        )]
        runner.executor = executor
        self.assertTrue(runner._sync_strategy(self.ed))
        self.assertEqual(bot.parse_ts(position["entry_time_utc"]), entry)
        runner._save_state()
        restarted = bot.S23HorizontalInventoryRunner(self.params)
        self.assertEqual(bot.parse_ts(restarted._st(self.ed)["basket"][0]["entry_time_utc"]), entry)
        observed = []
        restarted._monitor_fixed_hold_position = lambda strat, *_args: observed.append(int(strat["hold_minutes"])) or True
        before = entry.floor("s") + pd.Timedelta(minutes=15)
        due = entry + pd.Timedelta(minutes=15)
        for quote_time in (before, due):
            restarted._monitor_t0530_edge_position(
                self.ed,
                SimpleNamespace(bid=100.2, ask=100.3, quote_time_msc=int(quote_time.timestamp() * 1000)),
                quote_time,
            )
        self.assertEqual(observed, [15, 60])
        self.assertEqual(restarted._st(self.ed)["t0530_edge_hold_decision"], "extend")

    def test_inconsistent_broker_fill_milliseconds_block_sync(self) -> None:
        state = copy.deepcopy(self.base)
        self._active(state)["t0530_edge_hold_decision"] = "pending"
        runner = self._load(state)
        runner.live_enabled = True
        lane = runner._st(self.ed)
        entry = pd.Timestamp("2026-09-18T10:00:00Z")
        lane["basket"][0].update({
            "open_time_epoch": int(entry.timestamp()),
            "owner_symbol": "XAUUSD", "owner_magic": int(self.ed["magic"]),
            "owner_comment": self.ed["comment_prefix"], "shadow": False,
            "lane_id": int(self.ed["lane_id"]), "basket_id": lane["current_basket_id"],
        })
        executor = CountingExecutor()
        executor.positions = [SimpleNamespace(
            ticket=101, identifier=101, symbol="XAUUSD",
            magic=int(self.ed["magic"]), comment=self.ed["comment_prefix"],
            type=bot.ORDER_TYPE_BUY, volume=0.01, open_price=100.0,
            open_time=int(entry.timestamp()), open_time_msc=int(entry.timestamp() * 1000) + 1000,
        )]
        runner.executor = executor
        self.assertFalse(runner._sync_strategy(self.ed))
        self.assertEqual(lane["sync_block_reason"], "live_position_open_time_inconsistent")
        self.assertTrue(lane["sync_block_new_entries"])


if __name__ == "__main__":
    unittest.main()
