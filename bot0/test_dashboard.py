import ast
import csv
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

import dashboard


FIELDS = [
    "timestamp_utc", "event", "strategy_id", "signal_id", "lane_id", "magic",
    "symbol", "opportunity_id", "basket_id", "ticket", "position_identifier",
    "deal_id", "profit", "profit_unit", "ledger_profit", "currency",
    "execution_class", "live", "deal_time_utc", "note",
]


def write_sources(root: Path, config: dict, rows: list[dict]) -> dashboard.SnapshotCollector:
    params = root / "params.json"
    trades = root / "trades.csv"
    log = root / "bot.log"
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
            self.assertEqual(summary["metrics"]["by_signal"][0]["scope"], "unknown")

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

    # Fixture 6: ledger values are visible but not currency-confirmed realized PnL.
    def test_06_ledger_profit_unit_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = write_sources(Path(tmp), {"account_currency": "USD"}, [close("d", profit="3")])
            summary = dashboard.build_summary(collector=collector)
            self.assertEqual(summary["accounting"]["ledger_profit"], 3.0)
            self.assertEqual(summary["accounting"]["ledger_profit_unit_status"], "unconfirmed")
            self.assertIsNone(summary["accounting"]["execution_classes"]["live"]["realized_pnl"])

    # Fixture 7: currency plus explicit unit unlocks realized PnL.
    def test_07_currency_confirmed_realized(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = write_sources(Path(tmp), {"account_currency": "USD"}, [close("d", profit="3", unit="USD", currency="USD")])
            metric = dashboard.build_summary(collector=collector)["accounting"]["execution_classes"]["live"]
            self.assertEqual(metric["realized_pnl"], 3.0)

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

    # Fixture 11: production AST/read-only boundary and HTTP methods.
    def test_11_production_gate_and_get_only(self):
        dashboard.assert_read_only_ast()
        self.assertEqual(dashboard.production_gate_contract()["allowed_methods"], ["GET"])
        server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            connection = http.client.HTTPConnection(host, port, timeout=2)
            connection.request("GET", "/api/health")
            self.assertEqual(connection.getresponse().status, 200)
            connection.request("POST", "/api/health", body=b"x")
            self.assertEqual(connection.getresponse().status, 405)
            connection.request("GET", "/etc/passwd")
            self.assertEqual(connection.getresponse().status, 404)
            connection.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_real_data_counts_if_available(self):
        path = Path(r"C:\Users\muuma\Downloads\logs\s23_trades.csv")
        if not path.is_file():
            self.skipTest("live exported ledger not present")
        payload = path.read_bytes()
        audit = dashboard._csv_audit(payload)
        self.assertEqual(audit.source_rows, 25501)
        self.assertEqual(len(audit.closes), 525)
        self.assertEqual(audit.direct_opportunity_closes, 494)
        self.assertEqual(audit.unique_entry_join_closes, 31)
        self.assertEqual(audit.ambiguous_opportunity_joins, 0)


if __name__ == "__main__":
    unittest.main()
