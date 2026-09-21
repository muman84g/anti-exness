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
    "symbol", "opportunity_id", "basket_id", "ticket", "deal_id", "profit",
    "currency", "execution_class", "live", "deal_time_utc", "note",
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


class DashboardTests(unittest.TestCase):
    def test_deal_id_dedup_conflict_and_broker_time_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = {"timestamp_utc": "2026-01-01T00:00:02Z", "event": "position_close_confirmed", "strategy_id": "s", "signal_id": "sig", "lane_id": "1", "magic": "1", "symbol": "XAUUSD", "basket_id": "b", "ticket": "t", "deal_id": "d1", "profit": "2.5", "currency": "USD", "execution_class": "live", "deal_time_utc": "2026-01-01T00:00:01Z"}
            rows = [base, {**base, "timestamp_utc": "2026-01-01T00:00:03Z"}, {**base, "timestamp_utc": "2026-01-01T00:00:04Z", "profit": "3.5"}, {**base, "deal_id": "d2", "deal_time_utc": "", "note": ""}]
            collector = write_sources(root, {"account_currency": "USD"}, rows)
            audit = collector.get().audit
            self.assertEqual(len(audit.closes), 0)
            self.assertEqual(audit.duplicate_deals, 1)
            self.assertEqual(audit.conflicting_deals, 1)
            self.assertIn("missing_broker_deal_time", audit.quarantine_reasons)
            self.assertEqual(dashboard._close_time(base)[0], dashboard._parse_utc("2026-01-01T00:00:01Z"))

    def test_live_shadow_and_signal_strategy_curves_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [
                {"event": "position_close_confirmed", "deal_id": "l1", "strategy_id": "s1", "signal_id": "sig1", "profit": "2", "currency": "USD", "execution_class": "live", "deal_time_utc": "2026-01-01T00:00:01Z"},
                {"event": "position_close_confirmed", "deal_id": "s1", "strategy_id": "s1", "signal_id": "sig1", "profit": "-1", "currency": "USD", "execution_class": "shadow", "deal_time_utc": "2026-01-01T00:00:02Z"},
            ]
            collector = write_sources(root, {"account_currency": "USD", "enabled": True, "live_trading_enabled": True}, rows)
            summary = dashboard.build_summary(collector=collector)
            self.assertIsNone(summary["accounting"]["realized_pnl"])
            self.assertEqual(summary["accounting"]["execution_classes"]["live"]["realized_pnl"], 2.0)
            self.assertEqual(summary["accounting"]["execution_classes"]["shadow"]["realized_pnl"], -1.0)
            self.assertEqual(len(summary["metrics"]["by_signal"]), 2)
            self.assertEqual(len(summary["equity_curve"]["by_signal"]), 2)

    def test_currency_unknown_does_not_aggregate(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = write_sources(Path(tmp), {"enabled": True, "live_trading_enabled": True}, [{"event": "position_close_confirmed", "deal_id": "d", "strategy_id": "s", "signal_id": "x", "profit": "2", "execution_class": "live", "deal_time_utc": "2026-01-01T00:00:01Z"}])
            summary = dashboard.build_summary(collector=collector)
            metric = summary["accounting"]["execution_classes"]["live"]
            self.assertIsNone(metric["realized_pnl"])
            self.assertIsNone(metric["profit_factor"])
            self.assertEqual(summary["errors"].count("currency_unknown_total_not_aggregated"), 1)

    def test_last_good_snapshot_survives_refresh_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = write_sources(root, {"account_currency": "USD"}, [{"event": "position_close_confirmed", "deal_id": "d", "profit": "2", "currency": "USD", "execution_class": "live", "deal_time_utc": "2026-01-01T00:00:01Z"}])
            first = collector.get()
            collector.trades.unlink()
            collector.ttl_seconds = 0
            stale = collector.get()
            self.assertEqual(stale.status, "stale_last_good")
            self.assertEqual(stale.audit.closes, first.audit.closes)
            self.assertIsNotNone(stale.last_error)

    def test_tradable_now_fails_closed_and_includes_live_gate(self):
        gate = dashboard._tradable_gate({"enabled": True, "live_trading_enabled": False})
        self.assertFalse(gate["tradable_now"])
        self.assertEqual(gate["gates"]["live_trading_enabled"]["status"], "blocked")
        unknown = dashboard._tradable_gate({"enabled": True})
        self.assertEqual(unknown["status"], "unknown")

    def test_inventory_is_explicitly_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = write_sources(Path(tmp), {"account_currency": "USD"}, [])
            inventory = dashboard.build_summary(collector=collector)["inventory"]
            self.assertIsNone(inventory["open_position_count"])
            self.assertIsNone(inventory["mtm_pnl"])

    def test_http_is_get_only_and_path_allowlisted(self):
        server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            connection = http.client.HTTPConnection(host, port, timeout=2)
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b"read_only", response.read())
            connection.request("POST", "/api/health", body=b"x")
            self.assertEqual(connection.getresponse().status, 405)
            connection.request("GET", "/etc/passwd")
            self.assertEqual(connection.getresponse().status, 404)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
