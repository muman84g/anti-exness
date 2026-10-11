"""Synthetic sampled-quote integration tests for the private portable control.

These fixtures exercise the accepted native recipe with invented quotes only.
They do not establish a market baseline, runtime readiness, or adoption case.
"""
from __future__ import annotations

import gc
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

STAGING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGING))

import shadow_bridge as bridge
import shadow_state as state
from portable_control import PortableControl, PortableControlError, RUN_PREFIX


TEST_TEMP_ROOT = Path(tempfile.gettempdir()) / "bot30-portable-control"
TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
BASE_EPOCH_MS = int(datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
_RSS_SAMPLES: list[int] = []


def _runtime_identity(binding: bridge.NonsecretBinding) -> dict[str, str]:
    import bot30_shadow

    source_sha = bot30_shadow.verify_package()
    config_sha = hashlib.sha256((STAGING / "config" / "shadow_v001.json").read_bytes()).hexdigest()
    seed = {
        "source_sha256": source_sha,
        "config_sha256": config_sha,
        "binding_sha256": binding.binding_sha256,
    }
    run_id = RUN_PREFIX + state.digest(seed)[:24]
    return {**seed, "run_id": run_id}


class PortableControlTests(unittest.TestCase):
    def setUp(self) -> None:
        if sys.dont_write_bytecode is not True:
            self.fail("portable native source tests must run with bytecode writes disabled")
        self.temp = tempfile.TemporaryDirectory(prefix="portable-control-", dir=TEST_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.binding = bridge.NonsecretBinding(
            expected_company="Synthetic Exness Company",
            expected_server="Synthetic MT5 Server",
            ea_source_sha256="a" * 64,
        )
        self.verified = bridge._verified_binding_from_info({
            "opcode": "INFO",
            "attested": True,
            "binding_id": self.binding.binding_id,
            "binding_sha256": self.binding.binding_sha256,
        }, self.binding)
        self.identity = _runtime_identity(self.binding)
        self.journal = state.QuoteJournal(self.temp.name, self.identity)
        self.control = PortableControl(
            journal=self.journal,
            run_identity=self.identity["run_id"],
            binding_config=self.binding,
        )
        self.control.recover()
        self.addCleanup(self._close_control)

    def _close_control(self) -> None:
        control = getattr(self, "control", None)
        if control is not None:
            control.close()

    def _info(self, event_ms: int, bid: str, ask: str):
        info = bridge.DecodedInfo._from_codec(
            "XAUUSD", event_ms, Decimal(bid), Decimal(ask),
            self.binding.binding_id, self.binding.binding_sha256,
            event_ms + 1, event_ms + 2, self.verified,
        )
        clock = bridge.clock_evidence(info, event_ms + 3).asdict()
        return info, clock, self.verified

    def _send(self, event_ms: int, bid: str = "2000.150000", ask: str = "2000.170000") -> bool:
        return self.control.on_quote(*self._info(event_ms, bid, ask))

    def _warmup_pending_initial(self):
        """Build the naturally selected initial order after 80 completed bars."""
        accepted = 0
        for minute in range(80):
            start = BASE_EPOCH_MS + minute * 60_000
            self.assertTrue(self._send(start, "2000.000000", "2000.020000"))
            self.assertTrue(self._send(start + 30_000, "2000.300000", "2000.320000"))
            accepted += 2

        signal_ms = BASE_EPOCH_MS + 80 * 60_000
        self.assertTrue(self._send(signal_ms, "2000.300000", "2000.320000"))
        accepted += 1
        harness = self.control._book["harness"]
        engine = harness.engine
        self.assertEqual(harness.rows_processed, accepted)
        self.assertEqual(len(engine.completed_bars), 80)
        self.assertEqual(len(engine.pending_orders), 1)
        entry_order = engine.pending_orders[0]
        self.assertEqual((entry_order["command_type"], entry_order["role"], entry_order["side"]),
                         ("entry", "initial", "SELL"))
        entry_deadline = harness._market_order_deadlines[entry_order["order_id"]]["eligible_after_epoch_ms"]
        order_rows = [row for kind, row in harness.event_capture.rows
                      if kind == "order" and row.get("order_id") == entry_order["order_id"]]
        self.assertEqual(len(order_rows), 1)
        self.assertEqual(order_rows[0]["decision_source_row_sequence"], accepted)
        self.assertEqual(
            harness._market_order_deadlines[entry_order["order_id"]]["creation_source_row_sequence"],
            accepted,
        )
        return accepted, signal_ms, harness, engine, entry_order, entry_deadline

    def _normal_lifecycle(self) -> dict[str, object]:
        """80 completed M1 bars → initial signal → strict-later entry and exit."""
        accepted, _, harness, engine, entry_order, entry_deadline = self._warmup_pending_initial()

        before = self._fill_rows(harness)
        self.assertTrue(self._send(entry_deadline, "2000.300000", "2000.320000"))
        self.assertEqual(len(self._fill_rows(harness)), len(before), "+100 ms must not fill")
        self.assertTrue(self._send(entry_deadline + 1, "2000.300000", "2000.320000"))
        accepted += 2
        entries = [row for row in self._fill_rows(harness) if row["fill_kind"] == "entry"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["side"], "SELL")
        self.assertEqual(entries[0]["fill_price_micro"], 2_000_300_000)

        # The profitable quote creates a native close request.  The next quote
        # is exactly its immutable +100 ms eligibility boundary; the following
        # quote is strictly later and is sampled at the other side of the spread.
        take_profit_ms = entry_deadline + 1 + 1_000
        self.assertTrue(self._send(take_profit_ms, "1999.200000", "1999.220000"))
        accepted += 1
        exits_pending = [row for row in engine.pending_orders if row["command_type"] == "exit"]
        self.assertEqual(len(exits_pending), 1)
        exit_order = exits_pending[0]
        self.assertIs(harness.event_sink.emit, harness._deadline_emit)
        exit_timing = harness._market_order_deadlines[exit_order["order_id"]]
        self.assertEqual(exit_timing["eligible_after_epoch_ms"], exit_timing["request_cutoff_time_ms"] + 100)
        exit_deadline = exit_timing["eligible_after_epoch_ms"]
        before_exit = len([row for row in self._fill_rows(harness) if row["fill_kind"] == "exit"])
        self.assertTrue(self._send(exit_deadline, "2000.150000", "2000.170000"))
        self.assertEqual(len([row for row in self._fill_rows(harness) if row["fill_kind"] == "exit"]), before_exit,
                         "+100 ms exit must not fill")
        self.assertTrue(self._send(exit_deadline + 1, "2000.150000", "2000.170000"))
        accepted += 2
        exits = [row for row in self._fill_rows(harness) if row["fill_kind"] == "exit"]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["side"], "BUY")
        self.assertEqual(exits[0]["fill_price_micro"], 2_000_170_000)
        self.assertEqual(harness.rows_processed, accepted)
        self.assertEqual(self.journal.count, accepted)
        return {
            "accepted_quotes": accepted,
            "entry_source_row_sequence": entries[0]["source_row_sequence"],
            "exit_source_row_sequence": exits[0]["source_row_sequence"],
            "business_digest": self.control.business_digest(),
        }

    @staticmethod
    def _fill_rows(harness) -> list[dict[str, object]]:
        return [row for kind, row in harness.event_capture.rows if kind == "fill"]

    def test_prequote_rejects_without_book_then_valid_quote_recovers(self):
        self.assertIsNone(self.control._book)
        good = self._info(BASE_EPOCH_MS, "2000.150000", "2000.170000")
        bad = bridge.DecodedInfo._from_codec(
            "XAUUSD", BASE_EPOCH_MS, Decimal("2000.170000"), Decimal("2000.150000"),
            self.binding.binding_id, self.binding.binding_sha256,
            BASE_EPOCH_MS + 1, BASE_EPOCH_MS + 2, self.verified,
        )
        self.assertFalse(self.control.on_quote(bad, good[1], self.verified))
        self.assertEqual(self.control.last_rejection, "clock_or_codec_binding_rejected")
        self.assertIsNone(self.control._book)
        self.assertEqual((self.journal.count, self.journal.bytes), (0, 0))
        self.assertTrue(self.control.on_quote(*good))
        self.assertEqual((self.journal.count, self.control._book["harness"].rows_processed), (1, 1))

    def test_equal_time_duplicate_and_backward_reject_then_valid_progresses(self):
        self.assertTrue(self._send(BASE_EPOCH_MS))
        digest = self.control.business_digest()
        self.assertFalse(self._send(BASE_EPOCH_MS))
        self.assertEqual(self.control.last_rejection, "exact_duplicate_snapshot")
        self.assertFalse(self._send(BASE_EPOCH_MS, "2000.160000", "2000.180000"))
        self.assertEqual(self.control.last_rejection, "equal_time_changed_quote")
        self.assertFalse(self._send(BASE_EPOCH_MS - 1))
        self.assertEqual(self.control.last_rejection, "backward_event_time")
        self.assertEqual((self.journal.count, self.control.business_digest()), (1, digest))
        self.assertTrue(self._send(BASE_EPOCH_MS + 1))
        self.assertEqual(self.journal.count, 2)

    def test_natural_warmup_strict_later_entry_and_close(self):
        result = self._normal_lifecycle()
        self.assertEqual(result["accepted_quotes"], 166)
        self.assertLess(result["entry_source_row_sequence"], result["exit_source_row_sequence"])

    def test_pending_market_and_stop_restart_then_stop_cutoff_gate(self):
        """Replay a pending entry, then a live position with its native stop."""
        accepted, _, harness, engine, entry_order, entry_deadline = self._warmup_pending_initial()
        pending_digest = self.control.business_digest()
        self.assertEqual(len(engine.pending_orders), 1)
        self.assertEqual(engine.inventory, {})
        self.assertIn(entry_order["order_id"], harness._market_order_deadlines)
        pending_entry_sidecar = dict(harness._market_order_deadlines[entry_order["order_id"]])

        self.control.close()
        journal = state.QuoteJournal(self.temp.name, self.identity)
        recovered = PortableControl(journal=journal, run_identity=self.identity["run_id"], binding_config=self.binding)
        result = recovered.recover()
        self.control = recovered
        self.assertEqual(result["status"], "FULL_PREFIX_REPLAYED")
        self.assertEqual(result["business_digest"], pending_digest)
        harness = self.control._book["harness"]
        engine = harness.engine
        self.assertEqual(len(engine.pending_orders), 1)
        self.assertEqual(harness._market_order_deadlines[entry_order["order_id"]], pending_entry_sidecar)

        fills_before = len(self._fill_rows(harness))
        self.assertTrue(self._send(entry_deadline, "2000.300000", "2000.320000"))
        self.assertEqual(len(self._fill_rows(harness)), fills_before, "+100 ms entry remains pending")
        self.assertEqual(len(engine.pending_orders), 1)
        self.assertTrue(self._send(entry_deadline + 1, "2000.300000", "2000.320000"))
        accepted += 2
        entries = [row for row in self._fill_rows(harness) if row["fill_kind"] == "entry"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source_row_sequence"], accepted)
        self.assertEqual(entries[0]["fill_price_micro"], 2_000_300_000)

        pending = engine.pending_stop
        self.assertIsNotNone(pending)
        self.assertEqual(len(engine.inventory), 1)
        self.assertEqual(len(engine.pending_orders), 0)
        stop_sidecar = dict(harness._pending_stop_deadlines[pending.order_id])
        stop_trigger_price_micro = pending.trigger_price_micro
        self.assertEqual(stop_sidecar["creation_source_row_sequence"], accepted)
        self.assertEqual(stop_sidecar["request_cutoff_time_ms"], entry_deadline + 1 + 3)
        stop_digest = self.control.business_digest()
        live_position_ids = tuple(engine.inventory)
        stop_id = pending.order_id

        self.control.close()
        journal = state.QuoteJournal(self.temp.name, self.identity)
        recovered = PortableControl(journal=journal, run_identity=self.identity["run_id"], binding_config=self.binding)
        result = recovered.recover()
        self.control = recovered
        self.assertEqual(result["status"], "FULL_PREFIX_REPLAYED")
        self.assertEqual(result["business_digest"], stop_digest)
        harness = self.control._book["harness"]
        engine = harness.engine
        self.assertEqual(tuple(engine.inventory), live_position_ids)
        self.assertEqual(engine.pending_stop.order_id, stop_id)
        self.assertEqual(harness._pending_stop_deadlines[stop_id], stop_sidecar)

        stop_request_cutoff = stop_sidecar["request_cutoff_time_ms"]
        before_rows = len([row for row in self._fill_rows(harness) if row["fill_kind"] == "resting_stop"])
        self.assertTrue(self._send(stop_request_cutoff - 2, "2000.580000", "2000.610000"))
        self.assertEqual(engine.pending_stop.order_id, stop_id)
        self.assertEqual(len([row for row in self._fill_rows(harness) if row["fill_kind"] == "resting_stop"]), before_rows,
                         "a crossed stop before its immutable request cutoff must remain pending")
        self.assertTrue(self._send(stop_request_cutoff + 1, "2000.580000", "2000.610000"))
        stop_fills = [row for row in self._fill_rows(harness) if row["fill_kind"] == "resting_stop"]
        self.assertEqual(len(stop_fills), 1)
        self.assertEqual(stop_fills[0]["side"], "BUY")
        self.assertEqual(stop_fills[0]["fill_price_micro"], stop_trigger_price_micro)
        self.assertIsNone(engine.pending_stop)
        self.assertEqual(len(engine.inventory), 2)
        accepted += 2
        self.assertEqual(harness.rows_processed, accepted)
        self.assertEqual(journal.count, accepted)

    def test_postadvance_failure_is_terminal_and_fresh_prefix_recovers(self):
        self.assertTrue(self._send(BASE_EPOCH_MS))
        committed_digest = self.control.business_digest()
        harness = self.control._book["harness"]
        original_consume = harness.consume

        def consume_then_fail(tick):
            original_consume(tick)
            raise RuntimeError("synthetic post-advance fault")

        harness.consume = consume_then_fail
        with self.assertRaisesRegex(RuntimeError, "synthetic post-advance fault"):
            self._send(BASE_EPOCH_MS + 1_000)
        self.assertTrue(self.control.terminal)
        self.assertEqual(self.journal.count, 1)
        self.assertEqual(harness.rows_processed, 2)
        with self.assertRaises(PortableControlError):
            self._send(BASE_EPOCH_MS + 2_000)

        self.control.close()
        journal = state.QuoteJournal(self.temp.name, self.identity)
        recovered = PortableControl(journal=journal, run_identity=self.identity["run_id"], binding_config=self.binding)
        recovered.recover()
        self.control = recovered
        self.assertEqual(self.control.business_digest(), committed_digest)
        self.assertTrue(self._send(BASE_EPOCH_MS + 1_000))
        self.assertEqual(journal.count, 2)

    def test_partial_owned_module_cache_rejects_without_replacement_then_restores(self):
        self.assertTrue(self._send(BASE_EPOCH_MS))
        factory = self.control._factory
        runtime = self.control._book["runtime"]
        module_name, exact_module = next(iter(runtime["owned_modules"].items()))
        registry_before = dict(factory._REGISTRY)
        self.assertIs(sys.modules.get(module_name), exact_module)
        del sys.modules[module_name]
        try:
            with self.assertRaises(factory.PortableIdentityError):
                factory._load_runtime()
            self.assertIs(factory._REGISTRY[module_name], exact_module)
            self.assertEqual(factory._REGISTRY, registry_before)
        finally:
            sys.modules[module_name] = exact_module
        self.assertIs(factory._load_runtime(), runtime)
        self.assertTrue(self._send(BASE_EPOCH_MS + 1_000))

    def _measure_residual_cycle(self, label: str) -> None:
        self._normal_lifecycle()
        self.control.close()
        self.control = None
        gc.collect()
        import psutil

        rss = psutil.Process().memory_info().rss
        _RSS_SAMPLES.append(rss)
        print(f"RSS_RESIDUAL_{label}_BYTES={rss}", flush=True)

    def test_90_memory_residual_baseline_after_warmup_cycle(self):
        _RSS_SAMPLES.clear()
        self._measure_residual_cycle("warmup")

    def test_91_memory_residual_repeat_one(self):
        self._measure_residual_cycle("repeat1")

    def test_92_memory_residual_repeat_two(self):
        self._measure_residual_cycle("repeat2")

    def test_93_memory_residual_repeat_three_and_growth_gate(self):
        self._measure_residual_cycle("repeat3")
        self.assertEqual(len(_RSS_SAMPLES), 4)
        increases = [after - before for before, after in zip(_RSS_SAMPLES, _RSS_SAMPLES[1:])]
        print("RSS_RESIDUAL_INCREASES_BYTES=" + json.dumps(increases), flush=True)
        self.assertFalse(all(delta > 2 * 1024 * 1024 for delta in increases),
                         "three consecutive residual increases exceeded the fixed 2 MiB gate")


if __name__ == "__main__":
    unittest.main()
