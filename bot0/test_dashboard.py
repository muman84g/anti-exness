import csv
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import dashboard


class DashboardTests(unittest.TestCase):
    def test_deal_id_dedup_and_close_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "trades.csv"
            fields = ["timestamp_utc", "event", "strategy_id", "lane_id", "magic", "symbol", "opportunity_id", "basket_id", "ticket", "deal_id", "profit", "note"]
            rows = [
                {"timestamp_utc":"2026-01-01T00:00:02Z","event":"position_close_confirmed","strategy_id":"s","lane_id":"1","magic":"1","symbol":"XAUUSD","opportunity_id":"","basket_id":"b","ticket":"t","deal_id":"d1","profit":"2.5","note":"deal_time_utc=2026-01-01T00:00:01Z"},
                {"timestamp_utc":"2026-01-01T00:00:03Z","event":"position_close_confirmed","strategy_id":"s","lane_id":"1","magic":"1","symbol":"XAUUSD","opportunity_id":"","basket_id":"b","ticket":"t","deal_id":"d1","profit":"2.5","note":"deal_time_utc=2026-01-01T00:00:01Z"},
                {"timestamp_utc":"2026-01-01T00:00:04Z","event":"entry","strategy_id":"s","lane_id":"1","magic":"1","symbol":"XAUUSD","opportunity_id":"o","basket_id":"b","ticket":"t2","deal_id":"","profit":"","note":""},
            ]
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader(); writer.writerows(rows)
            audit = dashboard._read_trades(path)
            self.assertEqual(len(audit.closes), 1)
            self.assertEqual(audit.duplicate_deals, 1)
            self.assertEqual(audit.closes[0].close_time_source, "note.deal_time_utc")

    def test_effective_enabled_includes_root_and_group(self):
        config = {"enabled": True, "t0530_edge_enabled": False, "t0530_edge_strategies": [{"id":"x","enabled":True}]}
        rows = list(dashboard._iter_strategies(config))
        self.assertFalse(rows[0][2]["effective_enabled"])

    def test_unknown_generation_is_explicit(self):
        self.assertEqual(dashboard._config_generation({})["status"], "unknown")

    def test_period_uses_exclusive_end(self):
        self.assertTrue(dashboard._within(dashboard._parse_utc("2026-01-01T00:00:00Z"), None, dashboard._parse_utc("2026-01-01T00:00:01Z")))
        self.assertFalse(dashboard._within(dashboard._parse_utc("2026-01-01T00:00:01Z"), None, dashboard._parse_utc("2026-01-01T00:00:01Z")))

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
