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
            self.assertEqual(summary["accounting"]["ledger_profit"], 3.0)
            self.assertEqual(summary["accounting"]["ledger_profit_unit_status"], "unconfirmed")
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
