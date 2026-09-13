from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

import pandas as pd

import live_s24_bot as s24


ROOT = Path(__file__).resolve().parent


class S24RAD070Tests(unittest.TestCase):
    def setUp(self):
        self._state_directory = tempfile.TemporaryDirectory()
        self._previous_runtime_paths = (
            s24.LOG_DIR, s24.STATE_DIR, s24.LOG_FILE, s24.TRADE_LOG_FILE,
            s24.SHADOW_RUNNER_LOG_FILE, s24.STATE_FILE, s24.RUNNER_LOCK_FILE,
        )
        root = Path(self._state_directory.name)
        s24.LOG_DIR = str(root / "logs")
        s24.STATE_DIR = str(root / "state")
        s24.LOG_FILE = str(root / "logs" / "s24_bot.log")
        s24.TRADE_LOG_FILE = str(root / "logs" / "s24_trades.csv")
        s24.SHADOW_RUNNER_LOG_FILE = str(root / "logs" / "s24_shadow_runner_trades.csv")
        s24.STATE_FILE = str(root / "state" / "state.json")
        s24.RUNNER_LOCK_FILE = str(root / "state" / "s24_runner.lock")

    def tearDown(self):
        (
            s24.LOG_DIR, s24.STATE_DIR, s24.LOG_FILE, s24.TRADE_LOG_FILE,
            s24.SHADOW_RUNNER_LOG_FILE, s24.STATE_FILE, s24.RUNNER_LOCK_FILE,
        ) = self._previous_runtime_paths
        self._state_directory.cleanup()

    def _load_seed(self, params, seed):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(seed), encoding="utf-8")
            previous = s24.STATE_FILE
            s24.STATE_FILE = str(path)
            try:
                return s24.S24NoAdverseRunner(params)
            finally:
                s24.STATE_FILE = previous

    def test_frozen_rad_identity_is_independent(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        self.assertIsNone(s24.S24NoAdverseRunner._config_error(params))
        rad = params["strategies"][1]
        self.assertEqual((rad["lane_id"], rad["magic"], rad["comment_prefix"]), (207, 240207, "s24_rad070"))
        self.assertNotEqual(rad["magic"], params["strategies"][0]["magic"])
        self.assertNotEqual(rad["magic"], params["v206_strategy"]["magic"])

    def test_rad_signal_exists_only_on_completed_m30_boundary(self):
        index = pd.date_range("2026-01-01T00:00:00Z", periods=12 * 30, freq="min")
        rows = []
        for minute in range(len(index)):
            block = minute // 30
            price = 2000.0 + block * 4.0 + (minute % 30) * 0.05
            radius = 1.0 + block * 0.2
            rows.append({"Open": price, "High": price + radius, "Low": price - radius, "Close": price + 0.02, "Volume": 1.0})
        enriched = s24.add_features(pd.DataFrame(rows, index=index), 0.001)
        possible = enriched.index[(enriched["rad_long"] | enriched["rad_short"])]
        self.assertTrue(all(ts.minute in {29, 59} for ts in possible))
        non_boundary = ~enriched.index.minute.isin([29, 59])
        self.assertFalse(bool(enriched.loc[non_boundary, ["rad_long", "rad_short"]].to_numpy().any()))

    def test_rad_threshold_and_direction_contract(self):
        row = pd.Series({"spread_points": 100.0, "rad_score": 0.70, "rad_long": True, "rad_short": False})
        side, reason = s24.S24NoAdverseRunner._strategy_signal_decision(
            SimpleNamespace(params={"max_entry_spread_points": 300.0}), row, {"mode": "rad070_m30"}
        )
        self.assertEqual((side, reason), ("LONG", "rad070_long_signal"))

    def test_exact_pre_rad_v2_state_migrates_once_without_losing_core(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        seed_runner = s24.S24NoAdverseRunner(params)
        seed = seed_runner._default_state()
        core_id, rad_id = [row["id"] for row in params["strategies"]]
        seed["version"] = 2
        seed.pop("rad070_state_generation")
        seed["strategies"].pop(rad_id)
        seed["strategies"][core_id]["cooldown_until_bar"] = 7

        runner = self._load_seed(params, seed)

        self.assertFalse(runner._fatal_state_identity_mismatch)
        self.assertEqual(runner.state["version"], 3)
        self.assertEqual(runner.state["rad070_state_generation"], 2)
        self.assertEqual(runner.state["strategies"][core_id]["cooldown_until_bar"], 7)
        self.assertEqual(runner.state["strategies"][rad_id]["basket"], [])

    def test_exact_deployed_rad_v2_state_migrates_without_recreating_lanes(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        seed = s24.S24NoAdverseRunner(params)._default_state()
        core_id, rad_id = [row["id"] for row in params["strategies"]]
        seed["version"] = 2
        seed.pop("rad070_state_generation")
        for state in seed["strategies"].values():
            state.pop("protection_repair_retry_after_utc")
            state.pop("protection_repair_failure_count")
        seed["strategies"][core_id]["cooldown_until_bar"] = 7
        seed["strategies"][rad_id]["cooldown_until_bar"] = 11
        rad_position = {
            "ticket": 1001, "position_identifier": 7001, "side": "LONG", "lot": 0.01,
            "entry_price": 2000.0, "entry_time_utc": "2026-09-13T06:00:00+00:00",
            "open_time_epoch": int(pd.Timestamp("2026-09-13T06:00:00Z").timestamp()),
            "owner_symbol": "XAUUSD", "owner_magic": 240207,
            "owner_comment": "s24_rad070:abc123def0", "signal_bar_time": "2026-09-13T05:59:00+00:00",
            "close_submission_started_utc": None, "close_requested": False, "shadow": False,
        }
        seed["strategies"][rad_id]["basket"] = [rad_position]

        runner = self._load_seed(params, seed)

        self.assertFalse(runner._fatal_state_identity_mismatch)
        self.assertEqual(runner.state["version"], 3)
        self.assertEqual(runner.state["rad070_state_generation"], 2)
        self.assertEqual(runner.state["strategies"][core_id]["cooldown_until_bar"], 7)
        self.assertEqual(runner.state["strategies"][rad_id]["cooldown_until_bar"], 11)
        self.assertEqual(runner.state["strategies"][rad_id]["basket"], [rad_position])
        self.assertIsNone(runner.state["strategies"][rad_id]["protection_repair_retry_after_utc"])
        self.assertEqual(runner.state["strategies"][rad_id]["protection_repair_failure_count"], 0)

    def test_exact_inactive_rad_v2_bootstrap_tombstone_is_cleared(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        seed = s24.S24NoAdverseRunner(params)._default_state()
        rad_id = params["strategies"][1]["id"]
        seed["version"] = 2
        seed.pop("rad070_state_generation")
        for state in seed["strategies"].values():
            state.pop("protection_repair_retry_after_utc")
            state.pop("protection_repair_failure_count")
        rad_state = seed["strategies"][rad_id]
        rad_state.update({
            "sync_block_new_entries": True,
            "sync_block_reason": "state_container_invalid",
            "sync_block_recoverable": False,
            "sync_block_details": {"cause": "not_object", "quarantine_key": rad_id},
        })
        seed["quarantined_strategy_states"][rad_id] = None

        runner = self._load_seed(params, seed)

        self.assertFalse(runner._fatal_state_identity_mismatch)
        self.assertNotIn(rad_id, runner.state["quarantined_strategy_states"])
        self.assertFalse(runner.state["strategies"][rad_id]["sync_block_new_entries"])
        self.assertIsNone(runner.state["strategies"][rad_id]["sync_block_reason"])

    def test_rad_v2_bootstrap_tombstone_with_lifecycle_evidence_stays_blocked(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        seed = s24.S24NoAdverseRunner(params)._default_state()
        rad_id = params["strategies"][1]["id"]
        seed["version"] = 2
        seed.pop("rad070_state_generation")
        for state in seed["strategies"].values():
            state.pop("protection_repair_retry_after_utc")
            state.pop("protection_repair_failure_count")
        rad_state = seed["strategies"][rad_id]
        rad_state.update({
            "pending_open_opportunity_id": "unknown-live-opportunity",
            "sync_block_new_entries": True,
            "sync_block_reason": "state_container_invalid",
            "sync_block_recoverable": False,
            "sync_block_details": {"cause": "not_object", "quarantine_key": rad_id},
        })
        seed["quarantined_strategy_states"][rad_id] = None

        runner = self._load_seed(params, seed)

        self.assertTrue(runner.state["strategies"][rad_id]["sync_block_new_entries"])
        self.assertEqual(runner.state["strategies"][rad_id]["sync_block_reason"], "state_container_invalid")
        self.assertIn(rad_id, runner.state["quarantined_strategy_states"])

    def test_v3_generation1_rad_bootstrap_tombstone_is_cleared_once(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        seed = s24.S24NoAdverseRunner(params)._default_state()
        rad_id = params["strategies"][1]["id"]
        seed["rad070_state_generation"] = 1
        seed["strategies"][rad_id].update({
            "sync_block_new_entries": True,
            "sync_block_reason": "state_container_invalid",
            "sync_block_recoverable": False,
            "sync_block_details": {"cause": "not_object", "quarantine_key": rad_id},
        })
        seed["quarantined_strategy_states"][rad_id] = None

        runner = self._load_seed(params, seed)

        self.assertEqual(runner.state["rad070_state_generation"], 2)
        self.assertFalse(runner.state["strategies"][rad_id]["sync_block_new_entries"])
        self.assertIsNone(runner.state["strategies"][rad_id]["sync_block_reason"])
        self.assertNotIn(rad_id, runner.state["quarantined_strategy_states"])

    def test_current_generation_rad_bootstrap_tombstone_is_never_auto_cleared(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        seed = s24.S24NoAdverseRunner(params)._default_state()
        rad_id = params["strategies"][1]["id"]
        seed["strategies"][rad_id].update({
            "sync_block_new_entries": True,
            "sync_block_reason": "state_container_invalid",
            "sync_block_recoverable": False,
            "sync_block_details": {"cause": "not_object", "quarantine_key": rad_id},
        })
        seed["quarantined_strategy_states"][rad_id] = None

        runner = self._load_seed(params, seed)

        self.assertTrue(runner.state["strategies"][rad_id]["sync_block_new_entries"])
        self.assertEqual(runner.state["strategies"][rad_id]["sync_block_reason"], "state_container_invalid")
        self.assertIn(rad_id, runner.state["quarantined_strategy_states"])

    def test_deployed_rad_v2_state_with_unknown_shape_still_fails_closed(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        seed = s24.S24NoAdverseRunner(params)._default_state()
        seed["version"] = 2
        seed.pop("rad070_state_generation")
        for state in seed["strategies"].values():
            state.pop("protection_repair_retry_after_utc")
            state.pop("protection_repair_failure_count")
        seed["strategies"][params["strategies"][1]["id"]].pop("last_consumed_signal_bar")

        runner = self._load_seed(params, seed)

        self.assertTrue(runner._fatal_state_identity_mismatch)
        self.assertTrue(all(runner._st(row)["sync_block_reason"] == "state_identity_mismatch" for row in params["strategies"]))

    def test_deployed_rad_v2_migration_accepts_only_the_exact_historical_schema(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        seed = s24.S24NoAdverseRunner(params)._default_state()
        seed["version"] = 2
        seed.pop("rad070_state_generation")
        for state in seed["strategies"].values():
            state.pop("protection_repair_retry_after_utc")
            state.pop("protection_repair_failure_count")

        root_mutations = {
            "missing_root": lambda row: row.pop("quarantined_shadow_runner_states"),
            "extra_root": lambda row: row.update({"foreign_root": True}),
            "missing_core_strategy": lambda row: row["strategies"].pop(params["strategies"][0]["id"]),
            "extra_strategy": lambda row: row["strategies"].update({"foreign_lane": {}}),
        }
        for name, mutate in root_mutations.items():
            with self.subTest(name=name):
                candidate = json.loads(json.dumps(seed))
                mutate(candidate)
                self.assertTrue(self._load_seed(params, candidate)._fatal_state_identity_mismatch)

        for strategy in params["strategies"]:
            sid = strategy["id"]
            for key in tuple(seed["strategies"][sid]):
                with self.subTest(strategy=sid, missing_key=key):
                    candidate = json.loads(json.dumps(seed))
                    candidate["strategies"][sid].pop(key)
                    self.assertTrue(self._load_seed(params, candidate)._fatal_state_identity_mismatch)
            with self.subTest(strategy=sid, extra_key="foreign_field"):
                candidate = json.loads(json.dumps(seed))
                candidate["strategies"][sid]["foreign_field"] = None
                self.assertTrue(self._load_seed(params, candidate)._fatal_state_identity_mismatch)

    def test_current_v3_missing_rad_container_fails_closed(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        seed = s24.S24NoAdverseRunner(params)._default_state()
        seed["strategies"].pop(params["strategies"][1]["id"])

        runner = self._load_seed(params, seed)

        self.assertTrue(runner._fatal_state_identity_mismatch)
        self.assertTrue(all(runner._st(row)["sync_block_new_entries"] for row in params["strategies"]))
        self.assertTrue(all(runner._st(row)["sync_block_reason"] == "state_identity_mismatch" for row in params["strategies"]))

    def test_current_v3_with_both_strategy_containers_loads(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        seed = s24.S24NoAdverseRunner(params)._default_state()

        runner = self._load_seed(params, seed)

        self.assertFalse(runner._fatal_state_identity_mismatch)
        self.assertEqual(set(runner.state["strategies"]), {row["id"] for row in params["strategies"]})

    def test_current_v3_extra_strategy_container_fails_closed(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        seed = s24.S24NoAdverseRunner(params)._default_state()
        seed["strategies"]["foreign_lane"] = {}

        runner = self._load_seed(params, seed)

        self.assertTrue(runner._fatal_state_identity_mismatch)
        self.assertTrue(all(runner._st(row)["sync_block_reason"] == "state_identity_mismatch" for row in params["strategies"]))

    def test_current_v3_partial_rad_signal_identity_fails_closed(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        seed = s24.S24NoAdverseRunner(params)._default_state()
        seed["strategies"][params["strategies"][1]["id"]].pop("last_consumed_signal_bar")

        runner = self._load_seed(params, seed)

        self.assertTrue(runner._fatal_state_identity_mismatch)
        self.assertEqual(runner._st(params["strategies"][1])["sync_block_reason"], "state_identity_mismatch")

    def test_current_v3_partial_core_container_also_fails_closed(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        seed = s24.S24NoAdverseRunner(params)._default_state()
        seed["strategies"][params["strategies"][0]["id"]].pop("last_consumed_signal_bar")

        runner = self._load_seed(params, seed)

        self.assertFalse(runner._fatal_state_identity_mismatch)
        core_id = params["strategies"][0]["id"]
        self.assertEqual(runner._st(params["strategies"][0])["sync_block_reason"], "state_container_invalid")
        self.assertNotIn("last_consumed_signal_bar", runner.state["quarantined_strategy_states"][core_id])

    def test_current_v3_boolean_generation_marker_fails_closed(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        seed = s24.S24NoAdverseRunner(params)._default_state()
        seed["rad070_state_generation"] = True

        runner = self._load_seed(params, seed)

        self.assertTrue(runner._fatal_state_identity_mismatch)

    def test_root_state_version_requires_a_strict_integer(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        for invalid_version in (True, 3.0, "3"):
            with self.subTest(invalid_version=invalid_version):
                seed = s24.S24NoAdverseRunner(params)._default_state()
                seed["version"] = invalid_version

                runner = self._load_seed(params, seed)

                self.assertTrue(runner._fatal_state_identity_mismatch)
                self.assertTrue(all(runner._st(row)["sync_block_reason"] == "state_identity_mismatch" for row in params["strategies"]))

    def test_current_root_state_requires_exact_keys_and_valid_save_clock(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        mutations = {
            "extra_root_key": lambda seed: seed.update({"unexpected": True}),
            "missing_root_key": lambda seed: seed.pop("quarantined_shadow_runner_states"),
            "invalid_last_saved": lambda seed: seed.update({"last_saved_utc": "not-a-timestamp"}),
        }
        for case, mutate in mutations.items():
            with self.subTest(case=case):
                seed = s24.S24NoAdverseRunner(params)._default_state()
                mutate(seed)

                runner = self._load_seed(params, seed)

                self.assertTrue(runner._fatal_state_identity_mismatch)
                self.assertTrue(all(runner._st(row)["sync_block_reason"] == "state_identity_mismatch" for row in params["strategies"]))

    def test_rad_repair_retry_state_requires_complete_identity(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        runner = s24.S24NoAdverseRunner(params)
        rad = params["strategies"][1]
        state = runner._default_state()["strategies"][rad["id"]]
        state["protection_repair_failure_count"] = 1
        self.assertEqual(runner._core_state_shape_error(rad, state), "protection_repair_retry_identity_invalid")

    def test_rad_repair_retry_state_rejects_impossible_failure_count(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        runner = s24.S24NoAdverseRunner(params)
        rad = params["strategies"][1]
        state = runner._default_state()["strategies"][rad["id"]]
        state["protection_repair_failure_count"] = 4
        state["protection_repair_retry_after_utc"] = "2026-09-13T12:00:30+00:00"
        self.assertEqual(runner._core_state_shape_error(rad, state), "protection_repair_failure_count_invalid")

    def test_rad_active_repair_cooldown_rebuilds_missing_entry_block(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        saves = []
        runner._save_state = lambda: saves.append(True)
        runner._trade_row = lambda *_args, **_kwargs: None
        rad = params["strategies"][1]
        state_pos = {
            "ticket": 1001, "position_identifier": 7001, "side": "LONG", "lot": 0.01,
            "owner_symbol": "XAUUSD", "owner_magic": 240207,
            "owner_comment": "s24_rad070:abc123def0",
        }
        live = SimpleNamespace(
            ticket=1001, identifier=7001, type=0, volume=0.01, symbol="XAUUSD", magic=240207,
            comment="s24_rad070:abc123def0", open_price=2000.25, sl=0.0, tp=0.0,
        )
        st = runner._st(rad)
        st["protection_repair_failure_count"] = 1
        st["protection_repair_retry_after_utc"] = "2026-09-13T12:00:30+00:00"
        runner.executor = SimpleNamespace(repair_fixed_position=lambda **_kwargs: self.fail("cooldown must suppress repair"))

        with mock.patch.object(s24, "utc_now", return_value=pd.Timestamp("2026-09-13T12:00:05Z")):
            self.assertFalse(runner._ensure_rad_fixed_protection(rad, state_pos, live))

        self.assertEqual(st["sync_block_reason"], "rad_fixed_protection_repair_required")
        self.assertTrue(st["sync_block_new_entries"])
        self.assertTrue(st["sync_block_recoverable"])
        self.assertEqual(saves, [True])

    def test_rad_close_clears_old_protection_repair_cooldown(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        rad = params["strategies"][1]
        st = runner._st(rad)
        st["protection_repair_failure_count"] = 2
        st["protection_repair_retry_after_utc"] = "2026-09-13T12:00:30+00:00"

        runner._clear_basket_state(rad, "broker_or_external_close_confirmed", "2026-09-13T11:59:00+00:00")

        self.assertEqual(st["protection_repair_failure_count"], 0)
        self.assertIsNone(st["protection_repair_retry_after_utc"])

    def test_rad_protection_repair_is_ownership_bound_and_clears_only_its_block(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        rad = params["strategies"][1]
        state_pos = {
            "ticket": 1001, "position_identifier": 7001, "side": "LONG", "lot": 0.01,
            "owner_symbol": "XAUUSD", "owner_magic": 240207,
            "owner_comment": "s24_rad070:abc123def0",
        }
        live = SimpleNamespace(
            ticket=1001, identifier=7001, type=0, volume=0.01, symbol="XAUUSD", magic=240207,
            comment="s24_rad070:abc123def0", open_price=2000.25, sl=1982.0, tp=2030.25,
        )
        calls = []
        runner.executor = SimpleNamespace(repair_fixed_position=lambda **kwargs: calls.append(kwargs) or SimpleNamespace(success=True, raw_response="OK"))

        self.assertTrue(runner._ensure_rad_fixed_protection(rad, state_pos, live))
        self.assertEqual(calls[0]["expected_identifier"], 7001)
        self.assertEqual((calls[0]["stop_distance"], calls[0]["target_distance"]), (18.0, 30.0))

        runner._set_sync_block(rad, "foreign_unrelated_block", recoverable=False)
        self.assertTrue(runner._ensure_rad_fixed_protection(rad, state_pos, live))
        self.assertEqual(runner._st(rad)["sync_block_reason"], "foreign_unrelated_block")

        runner.state = runner._default_state()
        live.identifier = 7999
        before = len(calls)
        self.assertFalse(runner._ensure_rad_fixed_protection(rad, state_pos, live))
        self.assertEqual(len(calls), before)
        self.assertEqual(runner._st(rad)["sync_block_reason"], "rad_fixed_protection_ownership_mismatch")

    def test_rad_protection_repair_never_submits_when_live_is_disabled(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner.live_enabled = False
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        rad = params["strategies"][1]
        state_pos = {
            "ticket": 1001, "position_identifier": 7001, "side": "LONG", "lot": 0.01,
            "owner_symbol": "XAUUSD", "owner_magic": 240207,
            "owner_comment": "s24_rad070:abc123def0",
        }
        live = SimpleNamespace(
            ticket=1001, identifier=7001, type=0, volume=0.01, symbol="XAUUSD", magic=240207,
            comment="s24_rad070:abc123def0", open_price=2000.25, sl=0.0, tp=0.0,
        )
        runner.executor = SimpleNamespace(
            repair_fixed_position=lambda **_kwargs: self.fail("disabled live mode must not repair a broker position")
        )

        self.assertFalse(runner._ensure_rad_fixed_protection(rad, state_pos, live))

        st = runner._st(rad)
        self.assertEqual(st["sync_block_reason"], "live_disabled_with_owned_inventory")
        self.assertEqual(st["sync_block_details"]["deferred_action"], "rad_fixed_protection_repair")

    def test_rad_protection_repair_never_submits_without_admissible_quote_clock(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        rad = params["strategies"][1]
        state_pos = {
            "ticket": 1001, "position_identifier": 7001, "side": "LONG", "lot": 0.01,
            "owner_symbol": "XAUUSD", "owner_magic": 240207,
            "owner_comment": "s24_rad070:abc123def0",
        }
        live = SimpleNamespace(
            ticket=1001, identifier=7001, type=0, volume=0.01, symbol="XAUUSD", magic=240207,
            comment="s24_rad070:abc123def0", open_price=2000.25, sl=0.0, tp=0.0,
        )
        runner.executor = SimpleNamespace(
            repair_fixed_position=lambda **_kwargs: self.fail("quote-less reconciliation must be broker read-only")
        )

        self.assertFalse(
            runner._ensure_rad_fixed_protection(rad, state_pos, live, allow_repair=False)
        )

        st = runner._st(rad)
        self.assertEqual(st["sync_block_reason"], "rad_fixed_protection_repair_required")
        self.assertEqual(st["sync_block_details"]["error"], "quote_clock_unavailable_for_repair")

    def test_rad_protection_repair_uses_durable_cooldown_and_escalates(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        runner._notify_reconciliation_required = lambda *_args, **_kwargs: None
        rad = params["strategies"][1]
        state_pos = {
            "ticket": 1001, "position_identifier": 7001, "side": "LONG", "lot": 0.01,
            "owner_symbol": "XAUUSD", "owner_magic": 240207,
            "owner_comment": "s24_rad070:abc123def0",
        }
        live = SimpleNamespace(
            ticket=1001, identifier=7001, type=0, volume=0.01, symbol="XAUUSD", magic=240207,
            comment="s24_rad070:abc123def0", open_price=2000.25, sl=0.0, tp=0.0,
        )
        calls = []
        runner.executor = SimpleNamespace(
            repair_fixed_position=lambda **kwargs: calls.append(kwargs) or SimpleNamespace(success=False, raw_response="ERR|REPAIR_FIXED_FAILED|10030")
        )
        times = [pd.Timestamp("2026-09-13T12:00:00Z")]

        with mock.patch.object(s24, "utc_now", side_effect=lambda: times[0]):
            self.assertFalse(runner._ensure_rad_fixed_protection(rad, state_pos, live))
            times[0] += pd.Timedelta(seconds=5)
            self.assertFalse(runner._ensure_rad_fixed_protection(rad, state_pos, live))
            self.assertEqual(len(calls), 1)
            for _ in range(2):
                times[0] += pd.Timedelta(seconds=31)
                self.assertFalse(runner._ensure_rad_fixed_protection(rad, state_pos, live))
            times[0] += pd.Timedelta(seconds=31)
            self.assertFalse(runner._ensure_rad_fixed_protection(rad, state_pos, live))

        st = runner._st(rad)
        self.assertEqual(len(calls), 3)
        self.assertEqual(st["protection_repair_failure_count"], 3)
        self.assertFalse(st["sync_block_recoverable"])

    def test_rad_protection_repair_rejects_impossible_future_retry_clock(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        rad = params["strategies"][1]
        state_pos = {
            "ticket": 1001, "position_identifier": 7001, "side": "LONG", "lot": 0.01,
            "owner_symbol": "XAUUSD", "owner_magic": 240207,
            "owner_comment": "s24_rad070:abc123def0",
        }
        live = SimpleNamespace(
            ticket=1001, identifier=7001, type=0, volume=0.01, symbol="XAUUSD", magic=240207,
            comment="s24_rad070:abc123def0", open_price=2000.25, sl=0.0, tp=0.0,
        )
        runner.executor = SimpleNamespace(
            repair_fixed_position=lambda **_kwargs: self.fail("invalid durable clock must not submit a repair")
        )
        st = runner._st(rad)
        st["protection_repair_failure_count"] = 1
        st["protection_repair_retry_after_utc"] = "2099-01-01T00:00:00+00:00"

        with mock.patch.object(s24, "utc_now", return_value=pd.Timestamp("2026-09-13T12:00:00Z")):
            self.assertFalse(runner._ensure_rad_fixed_protection(rad, state_pos, live))

        self.assertEqual(st["sync_block_details"]["error"], "durable_repair_clock_invalid")
        self.assertFalse(st["sync_block_recoverable"])

    def test_exact_rad_protection_durably_clears_retry_without_clearing_other_block(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        saves = []
        runner._save_state = lambda: saves.append(True)
        rad = params["strategies"][1]
        state_pos = {
            "ticket": 1001, "position_identifier": 7001, "side": "LONG", "lot": 0.01,
            "owner_symbol": "XAUUSD", "owner_magic": 240207,
            "owner_comment": "s24_rad070:abc123def0",
        }
        live = SimpleNamespace(
            ticket=1001, identifier=7001, type=0, volume=0.01, symbol="XAUUSD", magic=240207,
            comment="s24_rad070:abc123def0", open_price=2000.25, sl=1982.25, tp=2030.25,
        )
        st = runner._st(rad)
        st["protection_repair_retry_after_utc"] = "2026-09-13T12:00:30+00:00"
        st["protection_repair_failure_count"] = 2
        runner._set_sync_block(rad, "foreign_unrelated_block", recoverable=False)

        self.assertTrue(runner._ensure_rad_fixed_protection(rad, state_pos, live))

        self.assertEqual(saves, [True])
        self.assertIsNone(st["protection_repair_retry_after_utc"])
        self.assertEqual(st["protection_repair_failure_count"], 0)
        self.assertEqual(st["sync_block_reason"], "foreign_unrelated_block")

    def test_rad_restart_adopts_only_exact_pending_open_fill(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        rows = []
        runner._trade_row = lambda event, _strat, **kwargs: rows.append((event, kwargs))
        rad = params["strategies"][1]
        st = runner._st(rad)
        signal = pd.Timestamp("2026-09-13T11:59:00Z")
        pending_id = f"s24-open:{rad['id']}:{s24.dt_text(signal)}:LONG:1"
        comment = f"s24_rad070:{hashlib.sha256(pending_id.encode('utf-8')).hexdigest()[:10]}"
        st["pending_open_opportunity_id"] = pending_id
        st["pending_open_started_utc"] = "2026-09-13T12:00:05+00:00"
        live = SimpleNamespace(
            ticket=1001, identifier=7001, type=0, volume=0.01, symbol="XAUUSD", magic=240207,
            comment=comment, open_price=2000.25, sl=1982.25, tp=2030.25,
            open_time=int(pd.Timestamp("2026-09-13T12:00:05Z").timestamp()), profit=0.0,
        )
        runner.executor = SimpleNamespace(
            get_positions=lambda *_args: [live], get_orders=lambda *_args: [],
            confirm_position_absent=lambda *_args: False,
        )

        self.assertTrue(runner._sync_strategy(rad))
        self.assertEqual(st["basket"][0]["position_identifier"], 7001)
        self.assertIsNone(st["pending_open_opportunity_id"])
        self.assertTrue(any(event == "position_lifecycle_recovered" and row.get("reason") == "rad_crash_after_fill_adopted" for event, row in rows))

        runner.state = runner._default_state()
        st = runner._st(rad)
        st["pending_open_opportunity_id"] = pending_id
        st["pending_open_started_utc"] = "2026-09-13T12:00:05+00:00"
        live.comment = "s24_rad070:ffffffffff"
        self.assertFalse(runner._sync_strategy(rad))
        self.assertEqual(st["basket"], [])
        self.assertEqual(st["sync_block_reason"], "live_positions_without_state")

    def test_rad_max_hold_uses_fresh_quote_clock_not_last_completed_m1(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        runner._sync_strategy = lambda _strat: True
        closes = []
        runner._close_basket = lambda _strat, reason, _row, _pnl: closes.append(reason)
        rad = params["strategies"][1]
        st = runner._st(rad)
        st["basket"] = [{
            "ticket": 1001, "position_identifier": 7001, "side": "LONG", "lot": 0.01,
            "entry_price": 2000.0, "entry_time_utc": "2026-09-13T06:00:00+00:00",
            "open_time_epoch": int(pd.Timestamp("2026-09-13T06:00:00Z").timestamp()),
            "owner_symbol": "XAUUSD", "owner_magic": 240207,
            "owner_comment": "s24_rad070:abc123def0", "signal_bar_time": "2026-09-13T05:59:00+00:00",
            "close_submission_started_utc": None, "close_requested": False, "shadow": False,
        }]
        st["last_add_price"] = 2000.0
        bars = s24.FakeDM().get_historical_data().tail(2).copy()
        bars.index = pd.DatetimeIndex(["2026-09-13T11:58:00Z", "2026-09-13T11:59:00Z"])
        info = SimpleNamespace(
            bid=2000.0, ask=2000.1,
            quote_time_msc=int(pd.Timestamp("2026-09-13T12:00:00Z").timestamp() * 1000),
        )

        runner._run_strategy(rad, bars, info)

        self.assertEqual(closes, ["max_hold"])

    def test_rad_exit_quote_before_fill_fails_closed(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        saves = []
        runner._save_state = lambda: saves.append(True)
        runner._sync_strategy = lambda _strat: True
        closes = []
        runner._close_basket = lambda _strat, reason, _row, _pnl: closes.append(reason)
        rad = params["strategies"][1]
        st = runner._st(rad)
        st["basket"] = [{
            "ticket": 1001, "position_identifier": 7001, "side": "LONG", "lot": 0.01,
            "entry_price": 2000.0, "entry_time_utc": "2026-09-13T12:00:05+00:00",
            "open_time_epoch": int(pd.Timestamp("2026-09-13T12:00:05Z").timestamp()),
            "owner_symbol": "XAUUSD", "owner_magic": 240207,
            "owner_comment": "s24_rad070:abc123def0", "signal_bar_time": "2026-09-13T11:59:00+00:00",
            "close_submission_started_utc": None, "close_requested": False, "shadow": False,
        }]
        st["last_add_price"] = 2000.0
        bars = s24.FakeDM().get_historical_data().tail(2).copy()
        bars.index = pd.DatetimeIndex(["2026-09-13T11:58:00Z", "2026-09-13T11:59:00Z"])
        info = SimpleNamespace(
            bid=2000.0, ask=2000.1,
            quote_time_msc=int(pd.Timestamp("2026-09-13T12:00:00Z").timestamp() * 1000),
        )

        runner._run_strategy(rad, bars, info)

        self.assertEqual(closes, [])
        self.assertTrue(saves)
        self.assertEqual(st["sync_block_reason"], "exit_quote_before_fill")
        self.assertTrue(st["sync_block_new_entries"])

    def test_rad_repair_wait_blocks_entry_but_keeps_owned_exit_monitoring(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner._save_state = lambda: None
        runner._trade_row = lambda *_args, **_kwargs: None
        runner._notify_reconciliation_required = lambda *_args, **_kwargs: None
        rad = params["strategies"][1]
        opened = pd.Timestamp("2026-09-13T06:00:00Z")
        state_pos = {
            "ticket": 1001, "position_identifier": 7001, "side": "LONG", "lot": 0.01,
            "entry_price": 2000.0, "entry_time_utc": s24.dt_text(opened),
            "open_time_epoch": int(opened.timestamp()), "owner_symbol": "XAUUSD",
            "owner_magic": 240207, "owner_comment": "s24_rad070:abc123def0",
            "signal_bar_time": "2026-09-13T05:59:00+00:00",
            "close_submission_started_utc": None, "close_requested": False, "shadow": False,
        }
        st = runner._st(rad)
        st["basket"] = [state_pos]
        st["last_add_price"] = 2000.0
        live = SimpleNamespace(
            ticket=1001, identifier=7001, type=0, volume=0.01, symbol="XAUUSD", magic=240207,
            comment="s24_rad070:abc123def0", open_price=2000.0, sl=0.0, tp=0.0,
            open_time=int(opened.timestamp()), profit=-20.0,
        )
        runner.executor = SimpleNamespace(
            get_positions=lambda *_args: [live], get_orders=lambda *_args: [],
            repair_fixed_position=lambda **_kwargs: SimpleNamespace(success=False, raw_response="ERR|REPAIR_FIXED_FAILED|10030"),
            confirm_position_absent=lambda *_args: False,
        )
        closes = []
        runner._close_basket = lambda _strat, reason, _row, _pnl: closes.append(reason)
        bars = s24.FakeDM().get_historical_data().tail(2).copy()
        bars.index = pd.DatetimeIndex(["2026-09-13T06:00:00Z", "2026-09-13T06:01:00Z"])
        info = SimpleNamespace(
            bid=1980.0, ask=1980.1,
            quote_time_msc=int(pd.Timestamp("2026-09-13T06:02:00Z").timestamp() * 1000),
        )

        runner._run_strategy(rad, bars, info)

        self.assertEqual(st["sync_block_reason"], "rad_fixed_protection_repair_required")
        self.assertTrue(st["sync_block_new_entries"])
        self.assertEqual(closes, ["basket_stop"])

    def test_rad_history_outage_keeps_owned_software_stop_monitoring(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        runner = s24.S24NoAdverseRunner(params)
        runner.state = runner._default_state()
        runner.live_enabled = True
        runner._save_state = lambda: None
        runner._sync_strategy = lambda _strat: True
        rad = params["strategies"][1]
        opened = pd.Timestamp("2026-09-13T11:00:00Z")
        st = runner._st(rad)
        st["basket"] = [{
            "ticket": 1001, "position_identifier": 7001, "side": "LONG", "lot": 0.01,
            "entry_price": 2000.0, "entry_time_utc": s24.dt_text(opened),
            "open_time_epoch": int(opened.timestamp()), "owner_symbol": "XAUUSD",
            "owner_magic": 240207, "owner_comment": "s24_rad070:abc123def0",
            "signal_bar_time": "2026-09-13T10:59:00+00:00",
            "close_submission_started_utc": None, "close_requested": False, "shadow": False,
        }]
        st["last_add_price"] = 2000.0
        closes = []
        runner._close_basket = lambda _strat, reason, _row, pnl: closes.append((reason, pnl))
        now = pd.Timestamp("2026-09-13T12:00:00Z")
        info = SimpleNamespace(
            bid=1980.0, ask=1980.1,
            quote_time_msc=int(now.timestamp() * 1000),
        )

        with mock.patch.object(s24, "utc_now", return_value=now.to_pydatetime()):
            runner._manage_core_without_history(info)

        self.assertEqual(closes, [("basket_stop", -20.0)])


if __name__ == "__main__":
    unittest.main()
