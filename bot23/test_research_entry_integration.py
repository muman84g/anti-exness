import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import live_s23_bot  # noqa: E402


def _params():
    return json.loads(Path(HERE, "s23_params.json").read_text(encoding="utf-8"))


def _runner_with_state(state_path: Path, state: dict):
    state_path.write_text(json.dumps(state), encoding="utf-8")
    params = _params()
    params["live_trading_enabled"] = False
    params["shadow_forward_enabled"] = True
    for key in (
        "shadow_opportunity_observer", "shadow_state_tagger",
        "midday_shadow_opportunity_observer", "midday_shadow_state_tagger",
        "pre_eu30_shadow_opportunity_observer", "pre_eu30_shadow_state_tagger",
    ):
        params[key]["enabled"] = False
    with patch.object(live_s23_bot, "STATE_FILE", str(state_path)):
        runner = live_s23_bot.S23HorizontalInventoryRunner(params)
    return runner


class ResearchStateGenerationTests(unittest.TestCase):
    def test_legacy_deployed_shape_save_restart_preserves_existing_lanes(self):
        fixture = json.loads(
            Path(HERE, "test_fixtures", "s23_bot_state_legacy_20260826.json").read_text(encoding="utf-8")
        )
        original_lanes = copy.deepcopy(fixture["strategies"])
        params = _params()
        params["live_trading_enabled"] = False
        params["shadow_forward_enabled"] = True
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps(fixture), encoding="utf-8")
            with patch.object(live_s23_bot, "STATE_FILE", str(state_path)):
                migrated = live_s23_bot.S23HorizontalInventoryRunner(params)
                self.assertTrue(migrated._research_entry_state_migrated)
                for lane_id, original in original_lanes.items():
                    for key, value in original.items():
                        self.assertEqual(migrated.state["strategies"][lane_id][key], value)
                migrated._save_state()
                restarted = live_s23_bot.S23HorizontalInventoryRunner(params)
            self.assertFalse(restarted._research_entry_state_migrated)
            self.assertEqual(
                restarted.state["routing"]["research_entry_policy_id"],
                live_s23_bot.EXPECTED_RESEARCH_ENTRY_POLICY_ID,
            )
            for strategy_id in live_s23_bot._EXPECTED_STRATEGY_IDS_BY_COLLECTION["research_entry_strategies"]:
                self.assertFalse(restarted.state["strategies"][strategy_id]["sync_block_new_entries"])
            for lane_id, original in original_lanes.items():
                for key, value in original.items():
                    self.assertEqual(restarted.state["strategies"][lane_id][key], value)

    def test_old_state_migrates_only_when_all_six_research_lanes_are_missing(self):
        params = _params()
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            seed = live_s23_bot.S23HorizontalInventoryRunner(params)
        old = seed._default_state()
        for strategy_id in live_s23_bot._EXPECTED_STRATEGY_IDS_BY_COLLECTION["research_entry_strategies"]:
            old["strategies"].pop(strategy_id)
        old["routing"].pop("research_entry_policy_id")
        old["routing"].pop("research_entry_params_hash")
        with tempfile.TemporaryDirectory() as tmp:
            runner = _runner_with_state(Path(tmp) / "state.json", old)
        self.assertEqual(
            runner.state["routing"]["research_entry_policy_id"],
            live_s23_bot.EXPECTED_RESEARCH_ENTRY_POLICY_ID,
        )
        self.assertTrue(runner._research_entry_state_migrated)
        self.assertTrue(
            all(
                strategy_id in runner.state["strategies"]
                for strategy_id in live_s23_bot._EXPECTED_STRATEGY_IDS_BY_COLLECTION["research_entry_strategies"]
            )
        )
        for strategy_id in live_s23_bot._EXPECTED_STRATEGY_IDS_BY_COLLECTION["research_entry_strategies"]:
            lane = runner.state["strategies"][strategy_id]
            self.assertFalse(lane["sync_block_new_entries"])
            self.assertIsNone(lane["sync_block_reason"])

    def test_partial_family_is_rejected_closed(self):
        params = _params()
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            seed = live_s23_bot.S23HorizontalInventoryRunner(params)
        state = seed._default_state()
        research_ids = live_s23_bot._EXPECTED_STRATEGY_IDS_BY_COLLECTION["research_entry_strategies"]
        state["strategies"].pop(research_ids[0])
        state["routing"].pop("research_entry_policy_id")
        state["routing"].pop("research_entry_params_hash")
        with tempfile.TemporaryDirectory() as tmp:
            runner = _runner_with_state(Path(tmp) / "state.json", state)
        self.assertEqual(
            runner.state["strategies"][research_ids[1]]["sync_block_reason"],
            "state_identity_mismatch",
        )

    def test_clock_ledger_failure_rejects_only_the_research_entry(self):
        params = _params()
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            runner = live_s23_bot.S23HorizontalInventoryRunner(params)
        runner.research_session_calendar = object()
        rows = []
        runner._trade_row = lambda *args, **kwargs: rows.append((args, kwargs))
        strat = runner._research_entry_strategies()[0]
        stamp = pd.Timestamp("2026-09-21T13:01:02Z")
        clocks = live_s23_bot.research_opportunity_clock_fields(
            "2026-09-21T13:00:00Z", stamp, stamp,
        )
        with patch.object(live_s23_bot, "append_csv", side_effect=OSError("disk unavailable")):
            written = runner._write_research_clock_lineage(
                strat, "opp-1", clocks, stamp, stamp,
                "2026-09-21T13:00:00+00:00", "LONG",
            )
        self.assertFalse(written)
        self.assertEqual(rows[0][1]["reason"], "research_clock_ledger_unavailable")
        self.assertFalse(runner._st(strat)["sync_block_new_entries"])

    def test_clock_ledger_failure_never_reaches_open_entry_caller_path(self):
        params = _params()
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            runner = live_s23_bot.S23HorizontalInventoryRunner(params)
        runner.research_session_calendar = object()
        strat = next(
            row for row in runner._research_entry_strategies()
            if int(row["lane_id"]) == 30
        )
        signal_bar = pd.Timestamp("2026-09-21T13:00:00Z")
        poll_time = signal_bar + pd.Timedelta(minutes=1, seconds=2)
        bars = pd.DataFrame(
            {"Open": [1.0], "High": [1.1], "Low": [0.9], "Close": [1.0]},
            index=pd.DatetimeIndex([signal_bar]),
        )
        price_row = bars.iloc[-1]
        price_row.name = signal_bar
        info = SimpleNamespace(ask=1.01, bid=1.00, quote_time_msc=int(poll_time.timestamp() * 1000))
        readiness = {int(strat["lane_id"]): True}
        with (
            patch.object(runner, "_research_entry_strategies", return_value=[strat]),
            patch.object(runner, "_reserve_lane_evaluation_bar", return_value=True),
            patch.object(runner, "_save_state"),
            patch.object(live_s23_bot, "evaluate_research_entry", return_value=SimpleNamespace(side="LONG", variant="test")),
            patch.object(live_s23_bot, "entry_session_guard", return_value=True),
            patch.object(runner, "_write_research_clock_lineage", return_value=False) as ledger,
            patch.object(runner, "_open_entry") as open_entry,
        ):
            runner._process_research_entry_entries(bars, price_row, info, poll_time, readiness)
        ledger.assert_called_once()
        open_entry.assert_not_called()

    def test_clock_ledger_success_writes_all_five_clocks(self):
        params = _params()
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            runner = live_s23_bot.S23HorizontalInventoryRunner(params)
        strat = runner._research_entry_strategies()[0]
        stamp = pd.Timestamp("2026-09-21T13:01:02Z")
        clocks = live_s23_bot.research_opportunity_clock_fields(
            "2026-09-21T13:00:00Z", stamp, stamp,
        )
        captured = []
        with patch.object(live_s23_bot, "append_csv", side_effect=lambda path, row, fields: captured.append((path, row, fields))):
            written = runner._write_research_clock_lineage(
                strat, "opp-1", clocks, stamp, stamp,
                "2026-09-21T13:00:00+00:00", "LONG",
            )
        self.assertTrue(written)
        self.assertEqual(captured[0][2], live_s23_bot.RESEARCH_CLOCK_FIELDS)
        for name in ("event_time", "release_time", "ingested_time", "available_time", "cutoff_time"):
            self.assertTrue(captured[0][1][name])

    def test_null_policy_identity_is_rejected_when_family_is_complete(self):
        params = _params()
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            seed = live_s23_bot.S23HorizontalInventoryRunner(params)
        state = seed._default_state()
        state["routing"].pop("research_entry_policy_id")
        state["routing"].pop("research_entry_params_hash")
        research_ids = live_s23_bot._EXPECTED_STRATEGY_IDS_BY_COLLECTION["research_entry_strategies"]
        with tempfile.TemporaryDirectory() as tmp:
            runner = _runner_with_state(Path(tmp) / "state.json", state)
        for strategy_id in research_ids:
            self.assertEqual(
                runner.state["strategies"][strategy_id]["sync_block_reason"],
                "research_entry_policy_identity_absent_with_partial_family",
            )

    def test_current_generation_invalid_lane_core_is_rejected_closed(self):
        params = _params()
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            seed = live_s23_bot.S23HorizontalInventoryRunner(params)
        state = seed._default_state()
        research_id = live_s23_bot._EXPECTED_STRATEGY_IDS_BY_COLLECTION["research_entry_strategies"][0]
        state["strategies"][research_id]["lane_id"] = 999
        with tempfile.TemporaryDirectory() as tmp:
            runner = _runner_with_state(Path(tmp) / "state.json", state)
        self.assertEqual(
            runner.state["strategies"][research_id]["sync_block_reason"],
            "state_identity_mismatch",
        )

    def test_policy_identity_mismatch_blocks_only_research_lanes(self):
        params = _params()
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            seed = live_s23_bot.S23HorizontalInventoryRunner(params)
        state = seed._default_state()
        state["routing"]["research_entry_policy_id"] = "research_entries_old"
        with tempfile.TemporaryDirectory() as tmp:
            runner = _runner_with_state(Path(tmp) / "state.json", state)
        for strategy in params["research_entry_strategies"]:
            self.assertEqual(
                runner.state["strategies"][strategy["id"]]["sync_block_reason"],
                "research_entry_policy_identity_mismatch",
            )


class ResearchTopologyIntegrationTests(unittest.TestCase):
    def test_current_enabled_lane_set_keeps_only_ny0530_and_ir_research(self):
        params = _params()
        enabled_lanes = {
            int(row["lane_id"])
            for row in params["t0530_edge_strategies"] + params["research_entry_strategies"]
            if row.get("enabled", True)
        }
        self.assertEqual(enabled_lanes, {18, 19, 20, 21, 30})
        self.assertTrue(params["research_entries_enabled"])

    def test_disabled_research_lane_with_basket_still_reaches_sync_and_exit(self):
        params = _params()
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            seed = live_s23_bot.S23HorizontalInventoryRunner(params)
        state = seed._default_state()
        with tempfile.TemporaryDirectory() as tmp:
            runner = _runner_with_state(Path(tmp) / "state.json", state)
            strat = next(
                row for row in runner._research_entry_strategies()
                if int(row["lane_id"]) == 25
            )
            self.assertFalse(strat["enabled"])
            runner.params["research_entries_enabled"] = False
            runner.research_session_calendar = object()
            runner._st(strat)["basket"] = [{"entry_time_utc": "2026-09-21T13:00:00Z", "side": "LONG"}]
            poll_time = pd.Timestamp("2026-09-21T13:01:02Z")
            info = SimpleNamespace(
                quote_time_msc=int(poll_time.timestamp() * 1000), bid=2500.0, ask=2500.1,
            )
            with (
                patch.object(runner, "_sync_strategy", return_value=True) as sync,
                patch.object(runner, "_monitor_fixed_hold_position", return_value=False) as monitor,
                patch.object(runner, "_save_state"),
            ):
                readiness = runner._process_research_entry_exits(info, poll_time)
            sync.assert_called_once_with(strat)
            monitor.assert_called_once_with(
                strat, info, poll_time, "research_entry_fixed_hold", defer_for_spread=False,
            )
            self.assertFalse(readiness[25])

    def test_params_validation_and_lane_contract(self):
        params = _params()
        live_s23_bot.validate_boolean_config(params)
        live_s23_bot.validate_strategy_topology_config(params)
        live_s23_bot.validate_execution_numeric_config(params)
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            runner = live_s23_bot.S23HorizontalInventoryRunner(params)
        self.assertIsNone(runner._ownership_namespace_error())

    def test_research_module_byte_drift_is_rejected(self):
        params = _params()
        with patch.object(live_s23_bot.os.path, "exists", return_value=False):
            runner = live_s23_bot.S23HorizontalInventoryRunner(params)
        with tempfile.TemporaryDirectory() as tmp:
            mutated = Path(tmp) / "research_entries.py"
            mutated.write_bytes(Path(HERE, "research_entries.py").read_bytes() + b"\n# drift\n")
            with patch.object(live_s23_bot, "RESEARCH_ENTRY_MODULE_PATH", str(mutated)):
                self.assertEqual(
                    runner._ownership_namespace_error(),
                    "research_entry_module_hash_mismatch",
                )
        self.assertEqual(
            [row["lane_id"] for row in params["research_entry_strategies"]],
            list(range(25, 31)),
        )


if __name__ == "__main__":
    unittest.main()
