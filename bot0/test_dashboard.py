import ast
import base64
import csv
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

import dashboard


FIELDS = [
    "timestamp_utc", "event", "strategy_id", "signal_id", "lane_id", "magic",
    "symbol", "mt5_symbol", "side", "opportunity_id", "basket_id", "ticket", "position_identifier",
    "signal_variant_id", "configured_signal_id", "signal", "variant", "spec_id",
    "owner_magic", "owner_comment", "close_deal_magic",
    "deal_id", "profit", "profit_unit", "ledger_profit", "currency",
    "execution_class", "live", "deal_time_utc", "signal_bar_time", "event_time", "release_time",
    "available_time", "decision_time", "reason", "note",
]


def write_sources(root: Path, config: dict, rows: list[dict]) -> dashboard.SnapshotCollector:
    params = root / "params.json"
    trades = root / "trades.csv"
    log = root / "bot.log"
    config = dict(config)
    if not any(key in config for key in ("strategies", "morning_session_strategies", "midday_session_strategies", "signals", "signal_definitions")):
        config.setdefault("enabled", True)
        config.setdefault("live_trading_enabled", True)
        config["strategies"] = [{"id": "s", "enabled": True, "signal_id": "sig"}]
    params.write_text(json.dumps(config), encoding="utf-8")
    with trades.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in FIELDS} for row in rows)
    log.write_text("", encoding="utf-8")
    return dashboard.SnapshotCollector(params, trades, log, ttl_seconds=0.01)


def close(deal: str, *, opp: str = "opp", ticket: str = "t", pos: str = "p",
          profit: str = "2", unit: str = "", currency: str = "", signal: str = "sig",
          note: str = "deal_time_utc=2026-01-01T00:00:01Z",
          timestamp: str = "2026-01-01T00:00:02Z", **extra: str) -> dict:
    return {"event": "position_close_confirmed", "deal_id": deal, "strategy_id": "s",
            "signal_id": signal, "opportunity_id": opp, "ticket": ticket,
            "position_identifier": pos, "profit": profit, "profit_unit": unit,
            "currency": currency, "execution_class": "live", "live": "True",
            "deal_time_utc": "", "timestamp_utc": timestamp, "note": note, **extra}


class DashboardTests(unittest.TestCase):
    def ny_pair(self, *, lane=1, signal_time="2026-09-23T09:30:00+00:00", side="LONG", owner=True):
        strategy = f"ny0530_edge_lane_{lane}"
        lane_id, magic, comment = str(lane + 17), str(230039 + lane), f"s23_ed_l{lane}"
        opportunity = f"XAUUSD|{signal_time}|t0530_edge_break_fade|{side}"
        entry = {"event": "entry", "strategy_id": strategy, "lane_id": lane_id, "magic": magic,
                 "symbol": "XAUUSD", "mt5_symbol": "XAUUSD", "side": side, "opportunity_id": opportunity,
                 "basket_id": f"basket-{lane}", "ticket": f"ticket-{lane}", "position_identifier": "", "live": "True", "signal_bar_time": signal_time,
                 "event_time": signal_time, "release_time": "2026-09-23T09:31:00+00:00",
                 "available_time": "2026-09-23T09:31:00+00:00", "decision_time": "2026-09-23T09:31:12+00:00",
                 "timestamp_utc": "2026-09-23T09:31:15+00:00", "note": "t0530_edge_w15_onset_hold_15m"}
        note = f"deal_time_utc=2026-09-23T09:46:20+00:00;owner_magic={magic};owner_comment={comment}" if owner else "deal_time_utc=2026-09-23T09:46:20+00:00"
        closed = {"event": "position_close_confirmed", "strategy_id": strategy, "lane_id": lane_id, "magic": magic,
                  "symbol": "XAUUSD", "mt5_symbol": "XAUUSD", "side": side, "opportunity_id": opportunity,
                  "basket_id": f"basket-{lane}", "ticket": f"ticket-{lane}", "position_identifier": f"ticket-{lane}", "live": "True",
                  "signal_bar_time": "2026-09-23T09:46:15+00:00", "deal_id": f"deal-{lane}",
                  "profit": "1", "deal_time_utc": "2026-09-23T09:46:20+00:00", "note": note}
        return entry, closed

    def research_pair(self, *, opportunity="XAUUSD|2026-09-23T01:20:00+00:00|curvature_fade_short|curvature_fade_short|LONG", close_side="LONG", close_note="deal_time_utc=2026-09-23T02:06:20+00:00;owner_magic=230049;owner_comment=s23_rs_l27", entry_note="curvature_fade_short", entry_ticket="43279674", close_ticket="43279674", entry_position="", close_position="43279674", strategy="research_path_curvature_lane_27", lane="27", magic="230049", symbol="XAUUSD"):
        entry = {"event": "entry", "strategy_id": strategy, "lane_id": lane, "magic": magic,
                 "symbol": symbol, "mt5_symbol": symbol, "side": "LONG", "opportunity_id": opportunity, "ticket": entry_ticket,
                 "position_identifier": entry_position, "basket_id": "research-basket", "live": "True",
                 "signal_bar_time": "2026-09-23 01:20:00+00:00", "event_time": "2026-09-23T01:20:00+00:00",
                 "release_time": "2026-09-23T01:20:00+00:00", "available_time": "2026-09-23T01:20:00+00:00",
                 "decision_time": "2026-09-23T01:20:01+00:00", "timestamp_utc": "2026-09-23T01:21:00+00:00", "note": entry_note}
        closed = {"event": "position_close_confirmed", "strategy_id": strategy, "lane_id": lane, "magic": magic,
                  "symbol": symbol, "mt5_symbol": symbol, "side": close_side, "opportunity_id": opportunity, "ticket": close_ticket,
                  "basket_id": "research-basket", "live": "True", "position_identifier": close_position,
                  "note": close_note, "deal_id": "40552841", "profit": "-9.41",
                  "execution_class": "live", "live": "True", "deal_time_utc": "2026-09-23T02:06:20+00:00",
                  "signal_bar_time": "2026-09-23T02:06:16.605000+00:00"}
        return entry, closed

    def test_v142_research_identity_restored_without_close_bar_time_join(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = self.research_pair()
            audit = write_sources(Path(tmp), {}, list(rows)).get().audit
            self.assertEqual(len(audit.closes), 1)
            self.assertEqual((audit.closes[0].signal_id, audit.closes[0].signal_variant_id), ("curvature_fade_short", "curvature_fade_short"))
            self.assertEqual(audit.closes[0].profit, -9.41)
            self.assertEqual(audit.closes[0].close_time, dashboard._parse_utc("2026-09-23T02:06:20+00:00"))

    def test_v142_ir_allows_only_source_defined_variants(self):
        for variant in ("interrupted_reapproach", "IR_pause_reapproach"):
            with tempfile.TemporaryDirectory() as tmp:
                opportunity = f"XAUUSD|2026-09-23T01:20:00+00:00|ir_original_priority_union|{variant}|LONG"
                entry = {"event": "entry", "strategy_id": "research_ir_union_lane_30", "lane_id": "30", "magic": "230052",
                         "symbol": "XAUUSD", "mt5_symbol": "XAUUSD", "side": "LONG", "opportunity_id": opportunity, "ticket": "ticket-ir",
                         "position_identifier": "", "basket_id": "research-ir", "live": "True",
                         "signal_bar_time": "2026-09-23T01:20:00+00:00", "event_time": "2026-09-23T01:20:00+00:00",
                         "release_time": "2026-09-23T01:20:00+00:00", "available_time": "2026-09-23T01:20:00+00:00",
                         "decision_time": "2026-09-23T01:20:01+00:00", "timestamp_utc": "2026-09-23T01:21:00+00:00", "note": variant}
                closed = {"event": "position_close_confirmed", "strategy_id": "research_ir_union_lane_30", "lane_id": "30", "magic": "230052",
                          "symbol": "XAUUSD", "mt5_symbol": "XAUUSD", "side": "LONG", "opportunity_id": opportunity, "ticket": "ticket-ir",
                          "basket_id": "research-ir", "live": "True",
                          "position_identifier": "ticket-ir", "signal_bar_time": "2026-09-23T02:06:00+00:00",
                          "deal_id": "ir-deal", "profit": "1", "deal_time_utc": "2026-09-23T02:06:20+00:00",
                          "note": f"deal_time_utc=2026-09-23T02:06:20+00:00;owner_magic=230052;owner_comment=s23_rs_l30"}
                audit = write_sources(Path(tmp), {}, [entry, closed]).get().audit
                self.assertEqual(audit.closes[0].signal_variant_id, variant)

    def test_v142_research_ambiguity_conflict_and_unknown_fail_closed(self):
        bad_opportunity = "XAUUSD|2026-09-23T01:20:00+00:00|curvature_fade_short|unknown_variant|LONG"
        cases = [
            (self.research_pair(opportunity=bad_opportunity),),
            (self.research_pair(opportunity="not-a-v142-opportunity"),),
            (self.research_pair(close_side="SHORT"),),
            (self.research_pair(close_note="deal_time_utc=2026-09-23T02:06:20+00:00;owner_magic=230050;owner_comment=s23_rs_l27"),),
        ]
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as tmp:
                    audit = write_sources(Path(tmp), {}, list(case[0])).get().audit
                    self.assertEqual(audit.closes[0].opportunity_attribution, "unresolved")
                    self.assertEqual((audit.closes[0].signal_id, audit.closes[0].signal_variant_id), ("", None))
        with tempfile.TemporaryDirectory() as tmp:
            entry, closed = self.research_pair()
            audit = write_sources(Path(tmp), {}, [entry, {**entry}, closed]).get().audit
            self.assertEqual(audit.closes[0].opportunity_attribution, "unresolved")

    def test_v142_research_position_identity_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry, closed = self.research_pair(close_position="different")
            audit = write_sources(Path(tmp), {}, [entry, closed]).get().audit
            self.assertEqual(audit.closes[0].opportunity_attribution, "unresolved")

    def test_v142_research_recovery_rows_on_same_ticket_must_match_entry_identity(self):
        for field, value in (("magic", "230050"), ("strategy_id", "research_path_speed_lane_28"),
                             ("symbol", "OTHER"), ("mt5_symbol", "OTHER"),
                             ("basket_id", "other-basket"), ("live", "False"),
                             ("opportunity_id", "other-opportunity"), ("side", "SHORT")):
            entry, closed = self.research_pair()
            recovery = {**entry, "event": "position_lifecycle_recovered",
                        "timestamp_utc": "2026-09-23T01:22:00+00:00",
                        "reason": "confirmed_broker_fill_time_restored", field: value}
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                audit = write_sources(Path(tmp), {}, [entry, recovery, closed]).get().audit
                self.assertEqual(audit.closes[0].opportunity_attribution, "unresolved")
                self.assertEqual(audit.closes[0].attribution_reason, "research_conflicting_ticket_position_row")

    def test_v142_research_writer_shaped_recovery_allows_omitted_side_and_opportunity(self):
        entry, closed = self.research_pair()
        recovery = {key: entry[key] for key in (
            "strategy_id", "lane_id", "magic", "symbol", "mt5_symbol", "basket_id", "ticket", "live",
        )}
        recovery.update({"event": "position_lifecycle_recovered", "position_identifier": entry["ticket"],
                         "timestamp_utc": "2026-09-23T01:22:00+00:00",
                         "reason": "confirmed_broker_fill_time_restored",
                         "note": "previous_entry_time_utc=2026-09-23T01:20:00+00:00;broker_entry_time_utc=2026-09-23T01:20:00+00:00"})
        with tempfile.TemporaryDirectory() as tmp:
            audit = write_sources(Path(tmp), {}, [entry, recovery, closed]).get().audit
            self.assertEqual(audit.closes[0].opportunity_attribution, "direct")
            self.assertEqual(audit.closes[0].signal_id, "curvature_fade_short")

    def test_v142_research_explicit_identity_and_entry_clock_mismatch_fail_closed(self):
        for mismatch in ("signal", "entry_clock"):
            entry, closed = self.research_pair()
            if mismatch == "signal":
                closed["signal_id"] = "other_signal"
            else:
                entry["signal_bar_time"] = "2026-09-23T01:19:00+00:00"
            with tempfile.TemporaryDirectory() as tmp:
                audit = write_sources(Path(tmp), {}, [entry, closed]).get().audit
                self.assertEqual(audit.closes[0].opportunity_attribution, "unresolved")
                self.assertEqual(audit.closes[0].signal_id, "")

    def test_ny0530_four_part_identity_checks_lane_entry_clocks_and_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            pair1 = self.ny_pair(lane=1)
            pair2 = self.ny_pair(lane=2)
            audit = write_sources(Path(tmp), {}, [*pair1, *pair2]).get().audit
            lane1 = next(row for row in audit.closes if row.deal_id == "deal-1")
            lane2 = next(row for row in audit.closes if row.deal_id == "deal-2")
            self.assertEqual((lane1.signal_id, lane1.signal_variant_id, lane1.opportunity_attribution), ("t0530_edge_break_fade", None, "direct"))
            self.assertEqual((lane2.signal_id, lane2.signal_variant_id), ("t0530_edge_break_fade", None))
            self.assertEqual(pair1[0]["opportunity_id"], pair2[0]["opportunity_id"])

    def test_ny0530_missing_owner_or_bad_entry_clock_stays_unattributed(self):
        cases = [list(self.ny_pair(owner=False))]
        bad_clock = self.ny_pair()
        bad_clock[0]["available_time"] = "2026-09-23T09:30:30+00:00"
        cases.append(list(bad_clock))
        duplicate = self.ny_pair()
        cases.append([duplicate[0], {**duplicate[0]}, duplicate[1]])
        conflict = self.ny_pair()
        competing = {**conflict[0], "opportunity_id": "XAUUSD|2026-09-23T09:29:00+00:00|t0530_edge_break_fade|LONG"}
        cases.append([conflict[0], competing, conflict[1]])
        bad_close_time = self.ny_pair()
        bad_close_time[1]["deal_time_utc"] = "2026-09-23T09:31:14+00:00"
        bad_close_time[1]["note"] = bad_close_time[1]["note"].replace("2026-09-23T09:46:20+00:00", "2026-09-23T09:31:14+00:00")
        cases.append(list(bad_close_time))
        for rows in cases:
            with self.subTest(rows=rows):
                with tempfile.TemporaryDirectory() as tmp:
                    audit = write_sources(Path(tmp), {}, rows).get().audit
                    self.assertEqual(audit.closes[0].opportunity_attribution, "unresolved")
                    self.assertEqual(audit.closes[0].signal_id, "")

    def test_ny0530_recovery_witness_allows_raw_pair_attribution_and_reports_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry, closed = self.ny_pair(owner=False)
            recovery = {"event": "position_lifecycle_recovered", "strategy_id": entry["strategy_id"],
                        "lane_id": entry["lane_id"], "magic": entry["magic"], "symbol": entry["symbol"],
                        "mt5_symbol": entry["mt5_symbol"], "ticket": entry["ticket"],
                        "position_identifier": closed["position_identifier"], "reason": "confirmed_broker_fill_time_restored",
                        "basket_id": entry["basket_id"], "live": entry["live"],
                        "timestamp_utc": "2026-09-23T09:32:00+00:00"}
            audit = write_sources(Path(tmp), {}, [entry, recovery, closed]).get().audit
            self.assertEqual(audit.closes[0].opportunity_attribution, "direct")
            self.assertEqual(audit.closes[0].owner_evidence, "broker_fill_recovery_witness; broker_owner_unverified")

    def test_ny0530_present_but_conflicting_owner_fields_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry, closed = self.ny_pair()
            closed["note"] = closed["note"].replace("owner_comment=s23_ed_l1", "owner_comment=s23_ed_l2")
            audit = write_sources(Path(tmp), {}, [entry, closed]).get().audit
            self.assertEqual(audit.closes[0].opportunity_attribution, "unresolved")

    def test_research_wrong_owner_metadata_clocks_and_competing_ticket_fail_closed(self):
        mutators = (
            lambda entry, closed: entry.update(mt5_symbol="OTHER"),
            lambda entry, closed: entry.update(basket_id="other-basket"),
            lambda entry, closed: entry.update(live="False"),
            lambda entry, closed: closed.update(note=closed["note"] + ";close_deal_magic=230050"),
            lambda entry, closed: entry.update(timestamp_utc="2026-09-23T02:07:00+00:00"),
            lambda entry, closed: entry.update(timestamp_utc="2026-09-23T01:00:00+00:00"),
            lambda entry, closed: closed.update(note=closed["note"] + ";owner_magic=230050;owner_magic=230049"),
            lambda entry, closed: closed.update(signal_id="curvature_fade_short", note=closed["note"] + ";signal_id=other"),
            lambda entry, closed: closed.update(signal_variant_id="curvature_fade_short", note=closed["note"] + ";variant=other"),
            lambda entry, closed: closed.update(signal_id="curvature_fade_short", configured_signal_id="other"),
            lambda entry, closed: closed.update(signal_variant_id="curvature_fade_short", variant="other"),
            lambda entry, closed: closed.update(signal_id="curvature_fade_short", note='{"signal_id":"other","variant":"other"}'),
            lambda entry, closed: closed.update(note="deal_time_utc=2026-09-23T02:06:20+00:00;owner_magic=230050;owner_magic=;owner_comment=s23_rs_l27"),
        )
        for mutate in mutators:
            with self.subTest(mutate=mutate.__code__.co_firstlineno):
                entry, closed = self.research_pair()
                mutate(entry, closed)
                with tempfile.TemporaryDirectory() as tmp:
                    audit = write_sources(Path(tmp), {}, [entry, closed]).get().audit
                    self.assertEqual(audit.closes[0].opportunity_attribution, "unresolved")
                    self.assertTrue(audit.closes[0].attribution_reason)
        entry, closed = self.research_pair()
        competitor = {**entry, "opportunity_id": "XAUUSD|2026-09-23T01:19:00+00:00|curvature_fade_short|curvature_fade_short|LONG"}
        with tempfile.TemporaryDirectory() as tmp:
            audit = write_sources(Path(tmp), {}, [entry, competitor, closed]).get().audit
            self.assertEqual(audit.closes[0].opportunity_attribution, "unresolved")

    def test_unattributed_reason_is_exposed_in_api_audit(self):
        entry, closed = self.research_pair(close_side="SHORT")
        with tempfile.TemporaryDirectory() as tmp:
            collector = write_sources(Path(tmp), {}, [entry, closed])
            reason = collector.get().audit.closes[0].attribution_reason
            summary = dashboard.build_summary(collector=collector, as_of_utc=dashboard._parse_utc("2026-09-23T03:00:00+00:00"))
            self.assertEqual(summary["audit"]["attribution_reasons"], {reason: 1})

    def test_ny0530_recovery_witness_must_be_unique_and_between_entry_and_close(self):
        entry, closed = self.ny_pair(owner=False)
        template = {"event": "position_lifecycle_recovered", "strategy_id": entry["strategy_id"],
                    "lane_id": entry["lane_id"], "magic": entry["magic"], "symbol": entry["symbol"],
                    "mt5_symbol": entry["mt5_symbol"], "ticket": entry["ticket"],
                    "position_identifier": closed["position_identifier"], "reason": "confirmed_broker_fill_time_restored",
                    "basket_id": entry["basket_id"], "live": entry["live"],
                    "timestamp_utc": "2026-09-23T09:32:00+00:00"}
        cases = [[{**template, "timestamp_utc": "2026-09-23T09:31:00+00:00"}],
                 [{**template, "timestamp_utc": "2026-09-23T09:46:21+00:00"}],
                 [{**template, "basket_id": ""}],
                 [{**template, "live": ""}],
                 [{**template, "basket_id": "", "live": ""}],
                 [{**template, "note": "signal_id=wrong"}],
                 [template, {**template}]]
        for witnesses in cases:
            with self.subTest(witnesses=len(witnesses), when=witnesses[0]["timestamp_utc"]):
                with tempfile.TemporaryDirectory() as tmp:
                    audit = write_sources(Path(tmp), {}, [entry, *witnesses, closed]).get().audit
                    self.assertEqual(audit.closes[0].opportunity_attribution, "unresolved")
                    self.assertTrue(audit.closes[0].attribution_reason)

    def test_strict_identity_parser_rejects_duplicate_empty_conflicting_and_nested_evidence(self):
        bad_notes = (
            'owner_magic=230049; owner_magic=230049',
            'owner_magic=230049; owner_magic=; owner_comment=s23_rs_l27',
            '{"signal_id":"curvature_fade_short","signal_id":"curvature_fade_short"}',
            '{"signal_id":"curvature_fade_short","signal":"other"}',
            '{"signal_id":"curvature_fade_short","nested":{"owner_magic":"230049"}}',
            '{"signal_id":null}',
            '{"signal_id":"curvature_fade_short"};owner_magic=230049',
        )
        for note in bad_notes:
            entry, closed = self.research_pair()
            closed["note"] = note
            with self.subTest(note=note), tempfile.TemporaryDirectory() as tmp:
                audit = write_sources(Path(tmp), {}, [entry, closed]).get().audit
                self.assertEqual(audit.closes[0].opportunity_attribution, "unresolved")
                self.assertTrue(audit.closes[0].attribution_reason)

    def test_alias_keys_are_case_space_normalized_and_conflicts_fail_closed(self):
        entry, closed = self.research_pair()
        closed["note"] = "Signal_ID=curvature_fade_short; owner_magic=230049; owner_comment=s23_rs_l27"
        with tempfile.TemporaryDirectory() as tmp:
            audit = write_sources(Path(tmp), {}, [entry, closed]).get().audit
            self.assertEqual(audit.closes[0].opportunity_attribution, "direct")
        entry, closed = self.research_pair()
        closed["note"] = " Signal_ID=curvature_fade_short; signal_id=curvature_fade_short "
        with tempfile.TemporaryDirectory() as tmp:
            audit = write_sources(Path(tmp), {}, [entry, closed]).get().audit
            self.assertEqual(audit.closes[0].opportunity_attribution, "unresolved")

    def test_ny0530_competing_ticket_identity_conflicts_and_physical_symbol_fail_closed(self):
        for field, value in (("side", "SHORT"), ("symbol", "OTHER"), ("mt5_symbol", "OTHER"),
                             ("basket_id", "other-basket"), ("live", "False")):
            entry, closed = self.ny_pair(owner=False)
            competing = {**entry, field: value, "opportunity_id": "XAUUSD|2026-09-23T09:29:00+00:00|t0530_edge_break_fade|LONG"}
            witness = {"event": "position_lifecycle_recovered", "strategy_id": entry["strategy_id"],
                       "lane_id": entry["lane_id"], "magic": entry["magic"], "symbol": entry["symbol"],
                       "mt5_symbol": entry["mt5_symbol"], "ticket": entry["ticket"],
                       "position_identifier": closed["position_identifier"], "reason": "confirmed_broker_fill_time_restored",
                       "timestamp_utc": "2026-09-23T09:32:00+00:00"}
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                audit = write_sources(Path(tmp), {}, [entry, competing, witness, closed]).get().audit
                self.assertEqual(audit.closes[0].opportunity_attribution, "unresolved")
        entry, closed = self.ny_pair(owner=True)
        entry["symbol"] = closed["symbol"] = "OTHER"
        with tempfile.TemporaryDirectory() as tmp:
            audit = write_sources(Path(tmp), {}, [entry, closed]).get().audit
            self.assertEqual(audit.closes[0].opportunity_attribution, "unresolved")

    def test_ny0530_explicit_signal_variant_conflicts_fail_closed(self):
        for key, value, note_suffix in (("signal_id", "wrong", ""), ("signal_variant_id", "wrong", ""),
                                        ("signal_id", "t0530_edge_break_fade", ";signal_id=wrong"),
                                        ("signal_variant_id", "", ";variant=wrong"),
                                        ("signal_id", "t0530_edge_break_fade", "")):
            entry, closed = self.ny_pair(owner=True)
            closed[key] = value
            closed["note"] += note_suffix
            if key == "signal_id" and value == "t0530_edge_break_fade" and not note_suffix:
                closed["configured_signal_id"] = "wrong"
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                audit = write_sources(Path(tmp), {}, [entry, closed]).get().audit
                self.assertEqual(audit.closes[0].opportunity_attribution, "unresolved")
                self.assertTrue(audit.closes[0].attribution_reason)

    def test_conflicting_broker_time_sources_are_quarantined(self):
        row = close("d", deal_time_utc="2026-01-01T00:00:03Z")
        with tempfile.TemporaryDirectory() as tmp:
            audit = write_sources(Path(tmp), {}, [row]).get().audit
            self.assertEqual(audit.closes, ())
            self.assertIn("conflicting_deal_time_sources", audit.quarantine_reasons)

    # Fixture 1: semicolon key-value deal_time is the period clock.
    def test_01_semicolon_deal_time(self):
        row = close("d", timestamp="2026-01-01T00:00:09Z", note="reason=x;deal_time_utc=2026-01-01T00:00:01Z;owner_magic=1")
        audit = dashboard._csv_audit(("event,deal_id,timestamp_utc,note,profit\nposition_close_confirmed,d,2026-01-01T00:00:09Z,reason=x\\;deal_time_utc=2026-01-01T00:00:01Z,2\n").encode())
        self.assertEqual(audit.closes[0].close_time, dashboard._parse_utc("2026-01-01T00:00:01Z"))

    # Fixture 2: direct and unique entry joins use ledger identity only.
    def test_02_direct_and_unique_entry_join(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [
                {"event": "entry", "deal_id": "e", "ticket": "t", "position_identifier": "p", "opportunity_id": "joined"},
                close("d1", opp="direct", ticket="x", pos="x"),
                close("d2", opp="", ticket="t", pos="p"),
            ]
            audit = write_sources(root, {"account_currency": "USD"}, rows).get().audit
            self.assertEqual((audit.direct_opportunity_closes, audit.unique_entry_join_closes), (1, 1))
            self.assertEqual(audit.closes[-1].opportunity_id, "joined")

    # Fixture 3: current params cannot fill historical signal/magic fields.
    def test_03_current_params_never_backfill_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = write_sources(Path(tmp), {"account_currency": "USD", "strategies": [{"id": "current", "signal_id": "new"}]}, [close("d", signal="")])
            summary = dashboard.build_summary(collector=collector)
            self.assertEqual(summary["historical_attribution_basis"], "ledger_row_fields_only; current_params_never_backfill_history")
            self.assertEqual(summary["metrics"]["by_signal"], [])
            self.assertEqual(summary["audit"]["visibility"]["hidden_unmapped_strategy"], 1)

    # Fixture 4: signal formats remain parser-compatible.
    def test_04_signal_formats(self):
        self.assertEqual(dashboard._signal_fields({"note": "signal_id=semicolon_signal;signal_variant_id=v1"}), ("semicolon_signal", "v1"))
        self.assertEqual(dashboard._signal_fields({"note": '{"signal_id":"json_signal","variant":"v2"}'}), ("json_signal", "v2"))
        self.assertEqual(dashboard._signal_fields({"signal_id": "column_signal", "spec_id": "spec"}), ("column_signal", "spec"))

    # Fixture 5: recorded timestamp differences are not semantic conflicts.
    def test_05_semantic_duplicate_excludes_recorded_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = close("d", timestamp="2026-01-01T00:00:02Z")
            audit = write_sources(root, {}, [row, {**row, "timestamp_utc": "2026-01-01T00:00:03Z"}]).get().audit
            self.assertEqual(len(audit.closes), 1)
            self.assertEqual(audit.duplicate_deals, 1)
            self.assertEqual(audit.conflicting_deals, 0)

    def test_05b_conflicting_final_set_recomputes_attribution_and_last_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = close("d", opp="direct", timestamp="2026-01-01T00:00:02Z", note="deal_time_utc=2026-01-01T00:00:01Z")
            conflicting = close("d", opp="other", timestamp="2026-01-01T00:00:03Z", note="deal_time_utc=2026-01-01T00:00:09Z")
            audit = write_sources(root, {}, [first, conflicting]).get().audit
            self.assertEqual(len(audit.closes), 0)
            self.assertEqual((audit.direct_opportunity_closes, audit.unique_entry_join_closes, audit.ambiguous_opportunity_joins), (0, 0, 0))
            self.assertIsNone(audit.last_close)

    def test_05c_missing_and_nonfinite_profit_are_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [close("missing", profit=""), close("nan", profit="nan"), close("inf", profit="inf"), close("ok", profit="1")]
            audit = write_sources(Path(tmp), {}, rows).get().audit
            self.assertEqual([row.deal_id for row in audit.closes], ["ok"])
            self.assertEqual(audit.quarantined_rows, 3)
            self.assertEqual(audit.quarantine_reasons.count("missing_or_nonfinite_profit"), 3)

    # Fixture 6: ledger values are visible but not currency-confirmed realized PnL.
    def test_06_ledger_profit_unit_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = write_sources(Path(tmp), {"account_currency": "USD"}, [close("d", profit="3")])
            summary = dashboard.build_summary(collector=collector)
            self.assertIsNone(summary["accounting"]["ledger_profit"])
            self.assertEqual(summary["accounting"]["ledger_profit_unit_status"], "raw_unverified_no_series")
            self.assertIn("profit", {item["value_field"] for item in summary["raw_ledger_metrics"]})
            self.assertIsNone(summary["accounting"]["execution_classes"]["live"]["realized_pnl"])

    # Fixture 7: currency plus explicit unit unlocks realized PnL.
    def test_07_currency_confirmed_realized(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = write_sources(Path(tmp), {"account_currency": "USD"}, [close("d", profit="3", unit="USD", currency="USD")])
            metric = dashboard.build_summary(collector=collector)["accounting"]["execution_classes"]["live"]
            self.assertEqual(metric["realized_pnl"], 3.0)

    def test_07b_unknown_execution_blocks_equity_curve_like_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = write_sources(Path(tmp), {"account_currency": "USD"}, [
                close("d", profit="3", unit="USD", currency="USD", execution_class="", live=""),
            ])
            summary = dashboard.build_summary(collector=collector)
            metric = summary["accounting"]["execution_classes"]["unknown"]
            curve = summary["equity_curve"]["by_execution_class"]
            self.assertIsNone(metric["realized_pnl"])
            self.assertEqual(metric["aggregation_status"], "blocked_currency_or_profit_unit")
            self.assertEqual(len(curve), 1)
            self.assertIsNone(curve[0]["cumulative_pnl"])
            self.assertEqual(curve[0]["aggregation_status"], "blocked_currency_or_profit_unit")

    # Fixture 8: the configured gate has an explicit stable name.
    def test_08_configured_live_enabled_gate(self):
        gate = dashboard._tradable_gate({"enabled": True, "live_trading_enabled": False})
        self.assertFalse(gate["tradable_now"])
        self.assertEqual(gate["gates"]["configured_live_enabled"]["status"], "blocked")

    # Fixture 9: strict CSV width errors fail closed and retain last good data.
    def test_09_strict_csv_last_good_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = write_sources(root, {}, [close("d")])
            first = collector.get()
            collector.trades.write_text("event,deal_id\nposition_close_confirmed,d,extra\n", encoding="utf-8")
            collector.ttl_seconds = 0
            stale = collector.get()
            self.assertEqual(stale.status, "stale_last_good")
            self.assertEqual(stale.audit.closes, first.audit.closes)
            self.assertIn("invalid_trade_row_width", stale.last_error)

    def test_09b_stale_failure_is_cached_for_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = write_sources(root, {}, [close("d")])
            collector.get()
            collector.trades.write_text("event,deal_id\nposition_close_confirmed,d,extra\n", encoding="utf-8")
            collector.ttl_seconds = 0
            with mock.patch.object(dashboard, "_read_source_batch", wraps=dashboard._read_source_batch) as reader:
                first = collector.get()
                collector.ttl_seconds = 60
                second = collector.get()
            self.assertIs(first, second)
            self.assertEqual(reader.call_count, 1)
            self.assertEqual(second.status, "stale_last_good")

    def test_09c_duplicate_and_empty_headers_reject(self):
        duplicate = b"event,event\nposition_close_confirmed,d\n"
        empty = b"event,\nposition_close_confirmed,d,\n"
        with self.assertRaisesRegex(dashboard.DashboardError, "invalid_trade_header_duplicate"):
            dashboard._csv_audit(duplicate)
        with self.assertRaisesRegex(dashboard.DashboardError, "invalid_trade_header_empty"):
            dashboard._csv_audit(empty)

    def test_09d_atomic_replace_and_cross_file_race_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.csv"
            replacement = root / "replacement.csv"
            source.write_bytes(b"old")
            replacement.write_bytes(b"new")
            real_identity = dashboard._path_identity
            replaced = False

            def replace_after_identity(path):
                nonlocal replaced
                identity = real_identity(path)
                if path == source and not replaced:
                    replaced = True
                    dashboard.os.replace(replacement, source)
                return identity

            with mock.patch.object(dashboard, "_path_identity", replace_after_identity):
                payload, _ = dashboard._read_stable_bytes(source)
            self.assertEqual(payload, b"new")

            params = root / "params.json"
            trades = root / "trades.csv"
            params.write_text("{}", encoding="utf-8")
            trades.write_text("event,deal_id\n", encoding="utf-8")
            real_reader = dashboard._read_stable_bytes
            raced = False

            def race_reader(path, *args, **kwargs):
                nonlocal raced
                result = real_reader(path, *args, **kwargs)
                if path == params and not raced:
                    raced = True
                    replacement_trade = root / "trades.new"
                    replacement_trade.write_bytes(trades.read_bytes() + b"position_close_confirmed,d\n")
                    dashboard.os.replace(replacement_trade, trades)
                return result

            with mock.patch.object(dashboard, "_read_stable_bytes", race_reader):
                with self.assertRaisesRegex(dashboard.DashboardError, "source_changed_during_read:cross_file"):
                    dashboard._read_source_batch({"params": params, "trades": trades})

    # Fixture 10: a replacement source is accepted and counted as rotation.
    def test_10_rotation_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = write_sources(root, {}, [close("d1")])
            collector.get()
            collector.trades.write_text(collector.trades.read_text(encoding="utf-8").replace("d1", "d2"), encoding="utf-8")
            collector.ttl_seconds = 0
            snap = collector.get()
            self.assertEqual(snap.status, "fresh")
            self.assertEqual(snap.rotation_count, 1)

    def test_10b_optional_evaluation_and_metadata_are_in_same_identity_barrier(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = write_sources(root, {}, [close("d")])
            evaluation = root / "evaluation.csv"
            metadata = root / "metadata.json"
            evaluation.write_bytes(b"header\n")
            metadata.write_bytes(b"{}\n")
            collector = dashboard.SnapshotCollector(collector.params, collector.trades, collector.log, ttl_seconds=0, evaluation=evaluation, metadata=metadata)
            snapshot = collector.get()
            self.assertEqual(set(snapshot.source_metadata["full_source_identity"]), {"params", "trades", "evaluation", "metadata"})
            self.assertEqual(snapshot.source_metadata["read_contract"], "strict_batch_pre_read_post_path_identity")

    def test_10c_optional_sources_are_visible_and_not_accounting_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = write_sources(root, {}, [close("d")])
            evaluation = root / "evaluation.csv"
            metadata = root / "metadata.json"
            evaluation.write_bytes(b"header\n")
            metadata.write_bytes(b"{}\n")
            collector = dashboard.SnapshotCollector(collector.params, collector.trades, collector.log, ttl_seconds=0, evaluation=evaluation, metadata=metadata)
            sources = dashboard.build_summary(collector=collector)["sources"]["optional"]
            self.assertEqual(sources["evaluation"]["path_alias"], "evaluation")
            self.assertEqual(sources["evaluation"]["path_label"], "evaluation.csv")
            self.assertEqual(sources["evaluation"]["file_identity"]["sha256"], dashboard._sha256(evaluation))
            self.assertTrue(sources["metadata"]["present"])
            self.assertEqual(sources["metadata"]["content_role"], "identity_only; not_accounting_input")

    def _start_authenticated_server(self, root: Path, password: str = "unit-test-secret"):
        auth_file = root / "auth.json"
        auth_file.write_text(json.dumps({"username": "bot0", "password": password}), encoding="utf-8")
        env = {"BOT0_AUTH_FILE": str(auth_file), "BOT0_AUTH_USER": "bot0"}
        patcher = mock.patch.dict(os.environ, env, clear=False)
        patcher.start()
        previous = dashboard.AUTH_CONFIG
        dashboard.AUTH_CONFIG = dashboard.load_auth_config()
        server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, patcher, previous, password

    # Fixture 11: production AST/read-only boundary and authenticated routes.
    def test_11_production_gate_and_basic_auth_protects_all_routes(self):
        dashboard.assert_read_only_ast()
        contract = dashboard.production_gate_contract()
        self.assertEqual(contract["allowed_methods"], ["GET"])
        self.assertEqual(contract["http_authentication"], "basic_required")
        with tempfile.TemporaryDirectory() as tmp:
            fixture = write_sources(Path(tmp), {}, [close("api")])
            server, thread, patcher, previous, password = self._start_authenticated_server(Path(tmp))
            try:
                host, port = server.server_address

                def request(path: str, *, credentials: tuple[str, str] | None = None, method: str = "GET", authorization_headers: list[str] | None = None):
                    connection = http.client.HTTPConnection(host, port, timeout=2)
                    headers = {}
                    if credentials is not None:
                        token = base64.b64encode(f"{credentials[0]}:{credentials[1]}".encode()).decode()
                        headers["Authorization"] = f"Basic {token}"
                    if authorization_headers is None:
                        connection.request(method, path, headers=headers)
                    else:
                        connection.putrequest(method, path)
                        for value in authorization_headers:
                            connection.putheader("Authorization", value)
                        connection.endheaders()
                    response = connection.getresponse()
                    body = response.read()
                    result = (response.status, dict(response.getheaders()), body)
                    connection.close()
                    return result

                for path in ("/", "/api/health", "/api/summary", "/unknown"):
                    status, headers, body = request(path)
                    self.assertEqual(status, 401, path)
                    self.assertEqual(headers.get("WWW-Authenticate"), 'Basic realm="bot0"')
                    self.assertNotIn(password.encode(), body)
                self.assertEqual(request("/unknown", method="HEAD")[0], 401)

                status, headers, body = request("/api/health", credentials=("bot0", "wrong"))
                self.assertEqual(status, 401)
                self.assertEqual(headers.get("WWW-Authenticate"), 'Basic realm="bot0"')
                self.assertNotIn(password.encode(), body)

                valid_token = base64.b64encode(f"bot0:{password}".encode()).decode()
                self.assertEqual(request("/api/health", authorization_headers=[f"Basic {valid_token}", f"Basic {valid_token}"])[0], 401)
                self.assertEqual(request("/api/health", authorization_headers=["Basic not-base64"])[0], 401)
                self.assertEqual(request("/api/health", authorization_headers=[f"Basic {valid_token}="])[0], 401)
                self.assertEqual(request("/api/health", authorization_headers=["Basic " + "A" * (dashboard.MAX_AUTH_B64_BYTES + 4)])[0], 401)
                self.assertEqual(request("/api/health", method="PROPFIND")[0], 401)

                status, _, body = request("/api/health", credentials=("bot0", password))
                self.assertEqual(status, 200)
                self.assertIn(b'"status":"ok"', body)
                self.assertEqual(request("/", credentials=("bot0", password))[0], 200)
                build_summary = dashboard.build_summary
                with mock.patch.object(dashboard, "build_summary", side_effect=lambda start=None, end=None, as_of_utc=None: build_summary(start, end, collector=fixture, as_of_utc=as_of_utc)):
                    summary_status, _, summary_body = request("/api/summary?from=2026-01-01T00:00:00Z&to=2026-01-01T00:00:10Z&as_of_utc=2026-01-01T00:00:30Z", credentials=("bot0", password))
                self.assertEqual(summary_status, 200, summary_body.decode("utf-8", errors="replace"))
                self.assertIn(b'"overview"', summary_body)
                self.assertIn(b'"request_clipped":true', summary_body)
                self.assertEqual(request("/unknown", credentials=("bot0", password))[0], 404)
                self.assertEqual(request("/api/health", credentials=("bot0", password), method="POST")[0], 405)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
                dashboard.AUTH_CONFIG = previous
                patcher.stop()

    def test_12_auth_startup_fails_closed_without_valid_password_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_file = Path(tmp) / "auth.json"
            auth_file.write_text(json.dumps({"username": "bot0", "password": ""}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"BOT0_AUTH_FILE": str(auth_file), "BOT0_AUTH_USER": "bot0"}, clear=False):
                with self.assertRaisesRegex(RuntimeError, "password"):
                    dashboard.load_auth_config()
                with mock.patch.object(dashboard, "ThreadingHTTPServer") as server_ctor:
                    with self.assertRaisesRegex(RuntimeError, "password"):
                        dashboard.main()
                    server_ctor.assert_not_called()

            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("BOT0_AUTH_FILE", None)
                with self.assertRaisesRegex(RuntimeError, "BOT0_AUTH_FILE"):
                    dashboard.load_auth_config()

    def test_13_auth_file_username_must_match_configured_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_file = Path(tmp) / "auth.json"
            auth_file.write_text(json.dumps({"username": "other", "password": "unit-test-secret"}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"BOT0_AUTH_FILE": str(auth_file), "BOT0_AUTH_USER": "bot0"}, clear=False):
                with self.assertRaisesRegex(RuntimeError, "username"):
                    dashboard.load_auth_config()

    def test_14_healthcheck_reads_auth_file_without_secret_in_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            server, thread, patcher, previous, password = self._start_authenticated_server(Path(tmp))
            try:
                env = os.environ.copy()
                env["BOT0_PORT"] = str(server.server_address[1])
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).with_name("healthcheck.py"))],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertNotIn(password, result.args)
                self.assertNotIn(password, result.stdout)
                self.assertNotIn(password, result.stderr)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
                dashboard.AUTH_CONFIG = previous
                patcher.stop()

    def test_15_main_view_filters_inactive_and_unmapped_identities(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"enabled": True, "live_trading_enabled": True, "strategies": [
                {"id": "active", "enabled": True, "signal_id": "sig_active"},
                {"id": "off", "enabled": False, "signal_id": "sig_off"},
            ]}
            rows = [
                close("active", signal="sig_active", strategy_id="active", ledger_profit="2", unit="USD", currency="USD"),
                close("off", signal="sig_off", strategy_id="off", ledger_profit="3", unit="USD", currency="USD"),
                close("unknown", signal="sig_unknown", strategy_id="old", ledger_profit="4", unit="USD", currency="USD"),
            ]
            collector = write_sources(Path(tmp), config, rows)
            summary = dashboard.build_summary(collector=collector)
            self.assertEqual([item["scope"] for item in summary["metrics"]["by_strategy"]], ["active"])
            self.assertEqual([item["scope"] for item in summary["metrics"]["by_signal"]], ["sig_active"])
            self.assertEqual(summary["audit"]["visibility"]["hidden_rows"], 2)
            self.assertEqual(summary["audit"]["visibility"]["hidden_inactive_strategy"], 1)
            self.assertEqual(summary["audit"]["visibility"]["hidden_unmapped_strategy"], 1)
            self.assertNotIn("sig_off", {item["scope"] for item in summary["metrics"]["by_signal"]})

    def test_16_verified_metrics_stay_null_and_raw_fields_do_not_mix(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [close("d", profit="1", ledger_profit="2", unit="", currency="")]
            summary = dashboard.build_summary(collector=write_sources(Path(tmp), {"account_currency": "USD"}, rows))
            self.assertIsNone(summary["metrics"]["by_signal"][0]["realized_pnl"])
            raw = summary["raw_ledger_metrics"]
            self.assertEqual({item["value_field"] for item in raw}, {"ledger_profit", "profit"})
            self.assertEqual({item["raw_value_total"] for item in raw}, {1.0, 2.0})
            self.assertEqual({item["aggregation_status"] for item in raw}, {"raw_unverified"})

    def test_17_raw_pf_edge_cases_are_defined_without_zero_division(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [close("win", profit="2", ledger_profit="2"), close("loss", profit="-1", ledger_profit="-1")]
            summary = dashboard.build_summary(collector=write_sources(Path(tmp), {}, rows))
            by_field = {item["value_field"]: item for item in summary["raw_ledger_metrics"]}
            self.assertEqual(by_field["ledger_profit"]["raw_pf"], 2.0)
            self.assertEqual(by_field["ledger_profit"]["raw_pf_status"], "defined")
            win_root = Path(tmp) / "win"
            win_root.mkdir()
            only_win = dashboard.build_summary(collector=write_sources(win_root, {}, [close("win", profit="2", ledger_profit="2")]))
            self.assertIsNone(only_win["raw_ledger_metrics"][0]["raw_pf"])
            self.assertEqual(only_win["raw_ledger_metrics"][0]["raw_pf_status"], "no_negative_values")

    def test_18_signal_chart_uses_utc_half_open_month_and_zero_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"enabled": True, "live_trading_enabled": True, "strategies": [{"id": "s", "enabled": True, "signal_id": "sig"}]}
            rows = [
                close("at_start", signal="sig", ledger_profit="2", note="deal_time_utc=2026-01-28T12:00:00Z"),
                close("middle", signal="sig", ledger_profit="-1", note="deal_time_utc=2026-01-31T00:00:00Z"),
                close("at_end", signal="sig", ledger_profit="9", note="deal_time_utc=2026-02-28T12:00:00Z"),
            ]
            collector = write_sources(Path(tmp), config, rows)
            audit = collector.get().audit
            charts = dashboard._signal_charts(list(audit.closes), {"sig"}, snapshot_status="fresh", now=dashboard._parse_utc("2026-02-28T12:00:00Z"), active_pairs={("s", "sig", None)})
            chart = next(item for item in charts if item["value_field"] == "ledger_profit")
            self.assertEqual(chart["period"], {"from_utc": "2026-01-28T12:00:00Z", "to_utc_exclusive": "2026-02-28T12:00:00Z"})
            self.assertEqual([point["deal_id"] for point in chart["points"]], [None, "at_start", "middle"])
            self.assertEqual([point["cumulative_value"] for point in chart["points"]], [0.0, 2.0, 1.0])
            self.assertIn("<svg", chart["svg"])

    def test_19_identity_join_does_not_infer_from_magic(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                {"event": "entry", "deal_id": "e", "magic": "42", "opportunity_id": "joined"},
                close("d", opp="", ticket="", pos="", magic="42"),
            ]
            audit = write_sources(Path(tmp), {}, rows).get().audit
            self.assertEqual(audit.closes[0].opportunity_id, "")
            self.assertEqual(audit.closes[0].opportunity_attribution, "unresolved")
            self.assertEqual(audit.unique_entry_join_closes, 0)

    def test_20_mixed_units_and_currencies_are_separate_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                close("usd", profit="1", ledger_profit="2", unit="USD", currency="USD"),
                close("jpy", profit="3", ledger_profit="4", unit="JPY", currency="JPY"),
                close("unknown", profit="5", ledger_profit="6", unit="", currency=""),
            ]
            summary = dashboard.build_summary(collector=write_sources(Path(tmp), {}, rows))
            ledger_groups = [item for item in summary["raw_ledger_metrics"] if item["value_field"] == "ledger_profit"]
            self.assertEqual({(item["value_unit"], item["currency"]) for item in ledger_groups}, {("USD", "USD"), ("JPY", "JPY"), (None, None)})

    def test_21_enabled_identity_requires_exact_strategy_signal_pair_and_variant(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"enabled": True, "live_trading_enabled": True, "strategies": [
                {"id": "s1", "enabled": True, "signal_id": "shared", "signal_variant_id": "v1"},
                {"id": "s2", "enabled": True, "signal_id": "shared", "signal_variant_id": "v2"},
            ]}
            rows = [
                close("ok", strategy_id="s1", signal="shared", signal_variant_id="v1", ledger_profit="1"),
                close("wrong_signal", strategy_id="s1", signal="other", ledger_profit="2"),
                close("wrong_variant", strategy_id="s1", signal="shared", signal_variant_id="v2", ledger_profit="3"),
                close("ok2", strategy_id="s2", signal="shared", signal_variant_id="v2", ledger_profit="4"),
            ]
            summary = dashboard.build_summary(collector=write_sources(Path(tmp), config, rows))
            self.assertEqual(summary["audit"]["visible_rows_in_period"], 2)
            self.assertEqual(summary["audit"]["visibility"]["pair_mismatch_rows"], 1)
            self.assertEqual(summary["audit"]["visibility"]["unmapped_signal_rows"], 1)
            self.assertEqual(summary["accounting"]["closed_deal_count"], 2)

    def test_22_hidden_rows_do_not_enter_accounting_attribution(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"enabled": True, "live_trading_enabled": True, "strategies": [
                {"id": "active", "enabled": True, "signal_id": "active_signal"},
                {"id": "off", "enabled": False, "signal_id": "off_signal"},
            ]}
            rows = [
                close("active", strategy_id="active", signal="active_signal", opp="visible"),
                close("off", strategy_id="off", signal="off_signal", opp="hidden"),
            ]
            summary = dashboard.build_summary(collector=write_sources(Path(tmp), config, rows))
            self.assertEqual(summary["accounting"]["opportunity_attribution"]["direct"], 1)
            self.assertEqual(summary["accounting"]["closed_deal_count"], 1)

    def test_23_chart_series_matches_raw_metric_and_empty_stale_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"enabled": True, "live_trading_enabled": True, "strategies": [{"id": "s", "enabled": True, "signal_id": "sig"}]}
            row = close("missing_ledger", signal="sig", profit="1", ledger_profit="", note="deal_time_utc=2026-01-15T00:00:00Z")
            collector = write_sources(Path(tmp), config, [row])
            summary = dashboard.build_summary(collector=collector, as_of_utc=dashboard._parse_utc("2026-02-01T00:00:00Z"))
            ledger_raw = [item for item in summary["raw_ledger_metrics"] if item["value_field"] == "ledger_profit"]
            ledger_chart = [item for item in summary["signal_charts"] if item["value_field"] == "ledger_profit"]
            self.assertEqual(len(ledger_raw), len(ledger_chart))
            self.assertEqual(ledger_raw[0]["coverage"]["denominator_close_rows"], 1)
            self.assertEqual(ledger_raw[0]["coverage"]["missing_value_rows"], 1)
            self.assertEqual(ledger_chart[0]["coverage"]["missing_value_rows"], 1)
            empty = dashboard._signal_charts([], {"sig"}, snapshot_status="stale", now=dashboard._parse_utc("2026-02-01T00:00:00Z"), active_pairs={("s", "sig", None)})
            self.assertEqual(empty[0]["status"], "stale_empty")
            self.assertEqual(empty[0]["points"][0]["cumulative_value"], 0.0)

    def test_24_top_raw_total_is_null_for_multiple_exact_series(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"enabled": True, "live_trading_enabled": True, "strategies": [
                {"id": "s1", "enabled": True, "signal_id": "sig1"},
                {"id": "s2", "enabled": True, "signal_id": "sig2"},
            ]}
            rows = [close("a", strategy_id="s1", signal="sig1", ledger_profit="2", unit="USD", currency="USD"), close("b", strategy_id="s2", signal="sig2", ledger_profit="3", unit="USD", currency="USD")]
            summary = dashboard.build_summary(collector=write_sources(Path(tmp), config, rows))
            self.assertIsNone(summary["accounting"]["ledger_profit"])
            self.assertEqual(summary["accounting"]["ledger_profit_unit_status"], "raw_unverified_multiple_series")
            self.assertIn("multiple_series", summary["accounting"]["ledger_profit_reason"])

    def test_25_empty_execution_bucket_cannot_claim_currency_or_verified_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"account_currency": "USD", "enabled": True, "live_trading_enabled": True,
                      "strategies": [{"id": "s", "enabled": True, "signal_id": "sig"}]}
            row = close("shadow", execution_class="shadow", live="False", profit="2", unit="USD", currency="USD")
            summary = dashboard.build_summary(collector=write_sources(Path(tmp), config, [row]))
            for execution in ("live", "shadow"):
                metric = summary["accounting"]["execution_classes"][execution]
                self.assertEqual(metric["deal_count"], 0 if execution == "live" else 1)
                if execution == "live":
                    self.assertIsNone(metric["realized_pnl"])
                    self.assertIsNone(metric["profit_factor"])
                    self.assertIsNone(metric["currency"])
                    self.assertEqual(metric["aggregation_status"], "blocked_no_values")

    def test_26_none_variant_is_exact_and_variant_is_in_raw_and_chart_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"enabled": True, "live_trading_enabled": True,
                      "strategies": [{"id": "s", "enabled": True, "signal_id": "sig"}]}
            rows = [
                close("none", signal="sig", ledger_profit="1", unit="USD", currency="USD"),
                close("unexpected", signal="sig", signal_variant_id="v1", ledger_profit="2", unit="USD", currency="USD"),
            ]
            summary = dashboard.build_summary(collector=write_sources(Path(tmp), config, rows))
            self.assertEqual(summary["audit"]["visible_rows_in_period"], 1)
            self.assertEqual(summary["audit"]["visibility"]["pair_mismatch_rows"], 1)
            self.assertEqual({item["signal_variant_id"] for item in summary["raw_ledger_metrics"]}, {None})
            self.assertEqual({item["signal_variant_id"] for item in summary["signal_charts"]}, {None})

    def test_27_same_strategy_signal_variants_are_separate_raw_and_chart_series(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"enabled": True, "live_trading_enabled": True,
                      "strategies": [{"id": "s", "enabled": True}],
                      "signals": [
                          {"strategy_id": "s", "signal_id": "sig", "signal_variant_id": "v1", "enabled": True},
                          {"strategy_id": "s", "signal_id": "sig", "signal_variant_id": "v2", "enabled": True},
                      ]}
            rows = [
                close("v1", signal="sig", signal_variant_id="v1", ledger_profit="1", unit="USD", currency="USD"),
                close("v2", signal="sig", signal_variant_id="v2", ledger_profit="2", unit="USD", currency="USD"),
            ]
            summary = dashboard.build_summary(collector=write_sources(Path(tmp), config, rows))
            raw = [item for item in summary["raw_ledger_metrics"] if item["value_field"] == "ledger_profit"]
            charts = [item for item in summary["signal_charts"] if item["value_field"] == "ledger_profit"]
            self.assertEqual({item["signal_variant_id"] for item in raw}, {"v1", "v2"})
            self.assertEqual({item["signal_variant_id"] for item in charts}, {"v1", "v2"})
            self.assertEqual({item["raw_value_total"] for item in raw}, {1.0, 2.0})

    def test_overview_utc_day_week_month_rollover_and_exclusive_as_of(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                close("before_month", profit="4", unit="USD", currency="USD", note="deal_time_utc=2026-04-30T23:59:59Z", timestamp="2026-04-30T23:59:59Z", ticket="a"),
                close("month_start", profit="3", unit="USD", currency="USD", note="deal_time_utc=2026-05-01T00:00:00Z", timestamp="2026-05-01T00:00:00Z", ticket="b"),
                close("before_asof", profit="2", unit="USD", currency="USD", note="deal_time_utc=2026-05-01T00:00:59Z", timestamp="2026-05-01T00:00:59Z", ticket="c"),
                close("at_asof", profit="90", unit="USD", currency="USD", note="deal_time_utc=2026-05-01T00:01:00Z", timestamp="2026-05-01T00:01:00Z", ticket="d"),
            ]
            summary = dashboard.build_summary(collector=write_sources(Path(tmp), {}, rows), as_of_utc=dashboard._parse_utc("2026-05-01T00:01:00Z"))
            overview = summary["overview"]["periods"]
            self.assertEqual((overview["day"]["period"]["from_utc"], overview["day"]["period"]["to_utc_exclusive"]), ("2026-05-01T00:00:00Z", "2026-05-01T00:01:00Z"))
            self.assertFalse(overview["day"]["period"]["request_clipped"])
            self.assertEqual(overview["day"]["period"]["status"], "raw_unverified")
            self.assertEqual(overview["week"]["period"]["from_utc"], "2026-04-27T00:00:00Z")
            self.assertEqual(overview["month"]["period"]["from_utc"], "2026-05-01T00:00:00Z")
            self.assertEqual(overview["day"]["curves"]["period_live_close_count"], 2)
            self.assertEqual(overview["day"]["curves"]["series"][0]["raw_value_total"], 5.0)
            self.assertEqual(overview["week"]["curves"]["series"][0]["raw_value_total"], 9.0)
            self.assertEqual(overview["month"]["curves"]["series"][0]["raw_value_total"], 5.0)
            day_signal = overview["day"]["signal_totals"][0]
            self.assertIsNone(day_signal["signal_variant_id"])
            self.assertEqual((day_signal["deal_count"], day_signal["raw_value_total"]), (2, 5.0))

    def test_overview_intersects_request_from_to_and_reports_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = write_sources(Path(tmp), {}, [
                close("inside", profit="7", ticket="inside", note="deal_time_utc=2026-05-01T00:00:40Z"),
                close("outside", profit="90", ticket="outside", note="deal_time_utc=2026-05-01T00:00:55Z"),
            ])
            summary = dashboard.build_summary(
                dashboard._parse_utc("2026-05-01T00:00:30Z"), dashboard._parse_utc("2026-05-01T00:00:50Z"),
                collector=collector, as_of_utc=dashboard._parse_utc("2026-05-01T00:01:00Z"),
            )
            day = summary["overview"]["periods"]["day"]
            self.assertEqual(day["period"]["from_utc"], "2026-05-01T00:00:30Z")
            self.assertEqual(day["period"]["to_utc_exclusive"], "2026-05-01T00:00:50Z")
            self.assertTrue(day["period"]["request_clipped"])
            self.assertEqual(day["curves"]["period_live_close_count"], 1)
            self.assertEqual(day["curves"]["series"][0]["raw_value_total"], 7.0)

    def test_overview_touching_half_open_boundaries_are_outside_but_natural_midnight_is_empty(self):
        as_of = dashboard._parse_utc("2026-05-01T00:01:00Z")
        day_start = dashboard._parse_utc("2026-05-01T00:00:00Z")
        cases = (
            ("to_equals_base_start", None, day_start),
            ("from_equals_base_end", as_of, None),
        )
        for label, request_start, request_end in cases:
            with self.subTest(boundary=label):
                overview = dashboard._overview_summary(
                    [], set(), as_of_utc=as_of, snapshot_status="fresh",
                    source_start=request_start, source_end=request_end,
                )
                day = overview["periods"]["day"]
                self.assertEqual(day["status"], "outside_request_window")
                self.assertEqual(day["period"]["status"], "outside_request_window")
                self.assertEqual(day["period"]["from_utc"], day["period"]["to_utc_exclusive"])

        midnight = dashboard._parse_utc("2026-05-01T00:00:00Z")
        natural = dashboard._overview_summary(
            [], set(), as_of_utc=midnight, snapshot_status="fresh",
        )["periods"]["day"]
        self.assertEqual(natural["status"], "empty")
        self.assertEqual(natural["period"]["status"], "empty")
        self.assertEqual(natural["period"]["from_utc"], natural["period"]["to_utc_exclusive"])

    def test_overview_current_effective_live_profit_coverage_and_separate_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"enabled": True, "live_trading_enabled": True, "strategies": [
                {"id": "s", "enabled": True, "signal_id": "sig"},
                {"id": "off", "enabled": False, "signal_id": "disabled_sig"},
            ]}
            rows = [
                close("usd", profit="2", unit="USD", currency="USD", ticket="usd", note="deal_time_utc=2026-01-01T00:00:01Z"),
                close("jpy", profit="3", unit="JPY", currency="JPY", ticket="jpy", note="deal_time_utc=2026-01-01T00:00:02Z"),
                close("missing", profit="", unit="", currency="", ticket="missing", note="deal_time_utc=2026-01-01T00:00:03Z"),
                close("shadow", profit="1000", unit="USD", currency="USD", execution_class="shadow", live="False", ticket="shadow", note="deal_time_utc=2026-01-01T00:00:04Z"),
                close("disabled", strategy_id="off", signal="disabled_sig", profit="500", unit="USD", currency="USD", ticket="disabled", note="deal_time_utc=2026-01-01T00:00:05Z"),
            ]
            summary = dashboard.build_summary(collector=write_sources(Path(tmp), config, rows), as_of_utc=dashboard._parse_utc("2026-01-01T00:10:00Z"))
            day = summary["overview"]["periods"]["day"]
            self.assertEqual(day["curves"]["period_live_close_count"], 2)
            self.assertEqual(day["curves"]["status"], "multiple_exact_series")
            self.assertEqual({(s["value_unit"], s["currency"]) for s in day["curves"]["series"]}, {("USD", "USD"), ("JPY", "JPY")})
            self.assertTrue(all(s["coverage"]["denominator_live_visible_close_rows"] == 2 for s in day["curves"]["series"]))
            self.assertTrue(all(s["coverage"]["status"] == "incomplete" for s in day["curves"]["series"]))
            self.assertIn("missing_or_nonfinite_profit", summary["audit"]["source_quality"]["quarantine_reasons"])
            self.assertEqual({row["signal_id"] for row in day["signal_totals"]}, {"sig"})
            self.assertTrue(all(row["execution_class"] == "live" and row["value_field"] == "profit" for row in day["signal_totals"]))

    def test_overview_defensively_marks_missing_profit_without_ledger_fallback(self):
        start = dashboard._parse_utc("2026-01-01T00:00:00Z")
        end = dashboard._parse_utc("2026-01-01T00:10:00Z")
        rows = [
            dashboard.CloseRow("a", "s", "sig", None, "", "", "", "a", "", "", "direct", None, 2.0, "USD", "USD", "live", dashboard._parse_utc("2026-01-01T00:00:01Z"), None),
            dashboard.CloseRow("b", "s", "sig", None, "", "", "", "b", "", "", "direct", None, 3.0, "USD", "USD", "live", dashboard._parse_utc("2026-01-01T00:00:02Z"), None),
            dashboard.CloseRow("missing", "s", "sig", None, "", "", "", "m", "", "", "direct", 88.0, None, "USD", "USD", "live", dashboard._parse_utc("2026-01-01T00:00:03Z"), None),
        ]
        overview = dashboard._overview_series(rows, period_start=start, period_end=end, snapshot_status="fresh")
        series = overview["series"][0]
        self.assertEqual(series["raw_value_total"], 5.0)
        self.assertEqual(series["deal_count"], 2)
        self.assertEqual(series["coverage"]["denominator_live_visible_close_rows"], 3)
        self.assertEqual(series["coverage"]["missing_value_rows"], 1)
        self.assertEqual(series["status"], "blocked_incomplete_coverage")

    def test_overview_equal_timestamp_order_is_stable_by_deal_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                close("z", profit="2", ticket="tz", note="deal_time_utc=2026-01-01T00:00:05Z"),
                close("a", profit="-1", ticket="ta", note="deal_time_utc=2026-01-01T00:00:05Z"),
            ]
            summary = dashboard.build_summary(collector=write_sources(Path(tmp), {}, rows), as_of_utc=dashboard._parse_utc("2026-01-01T00:01:00Z"))
            series = summary["overview"]["periods"]["day"]["curves"]["series"][0]
            self.assertEqual([point["deal_id"] for point in series["points"]], [None, "a", "z"])
            self.assertEqual([point["cumulative_value"] for point in series["points"]], [0.0, -1.0, 1.0])
            self.assertIn("viewBox", series["svg"])
            self.assertIn("<path", series["svg"])
            self.assertNotIn("<polyline", series["svg"])
            step_svg = dashboard._time_svg_chart([
                {"close_time_utc": "2026-01-01T00:00:00Z", "cumulative_value": 0},
                {"close_time_utc": "2026-01-01T00:02:00Z", "cumulative_value": 1},
                {"close_time_utc": "2026-01-01T00:08:00Z", "cumulative_value": 2},
            ], dashboard._parse_utc("2026-01-01T00:00:00Z"), dashboard._parse_utc("2026-01-01T00:10:00Z"))
            self.assertIn("H 130.00", step_svg)
            self.assertIn("H 430.00", step_svg)
            self.assertIn("H 530.00", step_svg)

    def test_overview_empty_stale_state_and_top_ui_mount(self):
        as_of = dashboard._parse_utc("2026-01-01T00:10:00Z")
        overview = dashboard._overview_summary([], {("s", "sig", None)}, as_of_utc=as_of, snapshot_status="stale")
        for period in overview["periods"].values():
            self.assertEqual(period["curves"]["status"], "stale_empty")
            self.assertEqual(period["signal_totals"][0]["status"], "stale_empty")
        self.assertLess(dashboard.INDEX_HTML.index('id="overview"'), dashboard.INDEX_HTML.index('id="summary"'))
        self.assertIn("今週 UTC", dashboard.INDEX_HTML)
        self.assertIn("今月 UTC", dashboard.INDEX_HTML)
        self.assertIn("損益概況を取得できません", dashboard.INDEX_HTML)

    def test_real_data_counts_if_available(self):
        path = Path(r"C:\Users\muuma\Downloads\logs\s23_trades.csv")
        if not path.is_file():
            self.skipTest("live exported ledger not present")
        payload = path.read_bytes()
        audit = dashboard._csv_audit(payload)
        self.assertEqual(audit.source_rows, 26383)
        self.assertEqual(len(audit.closes), 546)
        self.assertEqual(audit.direct_opportunity_closes, 515)
        self.assertEqual(audit.unique_entry_join_closes, 31)
        self.assertEqual(audit.ambiguous_opportunity_joins, 0)
        params_path = Path(__file__).parents[1] / "bot23" / "s23_params.json"
        if params_path.is_file():
            config = json.loads(params_path.read_text(encoding="utf-8"))
            visible, visibility, _ = dashboard._visible_rows(list(audit.closes), config)
            self.assertEqual(len(visible), 52)
            self.assertEqual(visibility["hidden_rows"], 494)
            self.assertEqual(visibility["hidden_pair_mismatch"], 0)
            self.assertEqual(sum(row.strategy_id.startswith("ny0530_edge_lane_") and row.signal_id == "t0530_edge_break_fade" for row in visible), 36)
            self.assertEqual(sum(row.owner_evidence == "broker_fill_recovery_witness; broker_owner_unverified" for row in audit.closes), 30)
            self.assertEqual(sum(row.owner_evidence == "broker_owner_fields_verified" for row in audit.closes), 6)
            collector = dashboard.SnapshotCollector(params_path, path, path.parent / "s23_bot.log", ttl_seconds=0.01)
            summary = dashboard.build_summary(collector=collector, as_of_utc=dashboard._parse_utc("2026-09-24T00:00:00Z"))
            raw = next(item for item in summary["raw_ledger_metrics"] if item["strategy_id"] == "research_path_curvature_lane_27" and item["value_field"] == "profit")
            chart = next(item for item in summary["signal_charts"] if item["strategy_id"] == "research_path_curvature_lane_27" and item["value_field"] == "profit")
            self.assertEqual((raw["deal_count"], raw["raw_value_total"]), (1, -9.41))
            self.assertIn({"close_time_utc": "2026-09-23T02:06:20Z", "deal_id": "40552841", "cumulative_value": -9.41}, chart["points"])
            self.assertEqual(summary["audit"]["owner_evidence"]["broker_fill_recovery_witness; broker_owner_unverified"], 30)
            self.assertIsNone(summary["accounting"]["realized_pnl"])
            self.assertIsNone(summary["accounting"]["execution_classes"]["live"]["profit_factor"])
            month = summary["overview"]["periods"]["month"]
            ny_totals = [row for row in month["signal_totals"] if row["strategy_id"].startswith("ny0530_edge_lane_")]
            self.assertEqual(sum(row["deal_count"] for row in ny_totals), 36)
            pair_counts = {row["strategy_id"]: row["deal_count"] for row in ny_totals}
            self.assertEqual(pair_counts, {"ny0530_edge_lane_1": 18, "ny0530_edge_lane_2": 10, "ny0530_edge_lane_3": 5, "ny0530_edge_lane_4": 3})
            self.assertEqual(sum({(row["strategy_id"], row["signal_id"], row["signal_variant_id"]): row["period_live_close_count"] for row in month["signal_totals"]}.values()), month["curves"]["period_live_close_count"])
            curve_totals = {(row["value_unit"], row["currency"]): row["raw_value_total"] for row in month["curves"]["series"]}
            signal_totals = {}
            for row in month["signal_totals"]:
                key = (row["value_unit"], row["currency"])
                signal_totals[key] = signal_totals.get(key, 0.0) + (row["raw_value_total"] or 0.0)
            self.assertEqual(signal_totals, curve_totals)


if __name__ == "__main__":
    unittest.main()
