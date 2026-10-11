from decimal import Decimal
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

STAGING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGING))
import shadow_bridge as bridge


class ReadonlyWireTests(unittest.TestCase):
    def setUp(self):
        self.rid = "0123456789abcdef0123456789abcdef"
        self.binding = bridge.NonsecretBinding(
            expected_company="Synthetic Exness Company",
            expected_server="Synthetic MT5 Server",
            ea_source_sha256="a" * 64,
        )

    def test_exact_arities_and_request_identity(self):
        req = bridge.make_request("ECHO", request_id=self.rid)
        self.assertEqual(bridge.parse_response(req, f"readonlyv1|{self.rid}|OK|ECHO|echo")["value"], "echo")
        with self.assertRaises(bridge.ProtocolError):
            bridge.make_request("OPEN", symbol="XAUUSD")
        with self.assertRaises(bridge.ProtocolError):
            bridge.parse_response(req, f"readonlyv1|{'f'*32}|OK|ECHO|echo")
        with self.assertRaises(bridge.ProtocolError):
            bridge.parse_response(req, f"readonlyv1|{self.rid}|OK|ECHO|echo|extra")
        with self.assertRaises(bridge.RemoteError):
            bridge.parse_response(req, f"readonlyv1|{self.rid}|ERR|ECHO|UNKNOWN_OPCODE")
        with self.assertRaises(bridge.ProtocolError):
            bridge.parse_response(req, f"readonlyv1|{self.rid}|OK|CAPS|echo")
        with self.assertRaises(bridge.ProtocolError):
            bridge._validate_request(f"readonlyv1|{self.rid}|INFO|XAUUSD")

    def test_info_binding_and_exact_clock_v2_shape(self):
        req = bridge.make_request("INFO", symbol="XAUUSD", binding=self.binding, request_id=self.rid)
        raw = f"readonlyv1|{self.rid}|OK|INFO|XAUUSD|1799999999000|2000.10|2000.12|{self.binding.binding_id}|{self.binding.binding_sha256}|1"
        decoded = bridge.parse_response(req, raw)
        verified = bridge._verified_binding_from_info(decoded, self.binding)
        info = bridge.DecodedInfo._from_codec("XAUUSD", decoded["time_msc"], decoded["bid"], decoded["ask"],
                                              decoded["binding_id"], decoded["binding_sha256"], 1800000000000,
                                              1800000000001, verified)
        envelope = bridge.clock_evidence(info, 1800000000002).asdict()
        self.assertEqual(set(envelope), {
            "schema", "event_time_ms", "release_time_ms", "release_time_upper_bound_ms",
            "release_time_status", "broker_original_release_time_ms", "ingested_time_ms",
            "available_time_ms", "cutoff_time_ms", "source_provenance", "binding_id",
            "binding_sha256", "time_basis", "timezone", "dst_conversion", "clock_evidence_refs"})
        self.assertIsNone(envelope["release_time_ms"])
        self.assertIsNone(envelope["broker_original_release_time_ms"])
        self.assertEqual(envelope["release_time_upper_bound_ms"], envelope["ingested_time_ms"])
        self.assertEqual(envelope["release_time_status"], "consumer_observed_upper_bound")
        self.assertEqual(envelope["event_time_ms"], 1799999999000)
        self.assertEqual(envelope["cutoff_time_ms"], 1800000000002)
        with self.assertRaises(TypeError):
            bridge.VerifiedBinding("caller", "true")
        with self.assertRaises(bridge.BindingUnverified):
            bridge._verified_binding_from_info(decoded, bridge.NonsecretBinding())
        wrong_id = dict(decoded, binding_id="OTHER")
        with self.assertRaises(bridge.BindingUnverified):
            bridge._verified_binding_from_info(wrong_id, self.binding)

    def test_bad_clock_order_and_quote_rejected(self):
        req = bridge.make_request("INFO", symbol="XAUUSD", binding=self.binding, request_id=self.rid)
        raw = f"readonlyv1|{self.rid}|OK|INFO|XAUUSD|1800000000003|2000.10|2000.12|{self.binding.binding_id}|{self.binding.binding_sha256}|1"
        decoded = bridge.parse_response(req, raw)
        verified = bridge._verified_binding_from_info(decoded, self.binding)
        info = bridge.DecodedInfo._from_codec("XAUUSD", decoded["time_msc"], decoded["bid"], decoded["ask"],
                                              decoded["binding_id"], decoded["binding_sha256"], 1800000000002,
                                              1800000000002, verified)
        with self.assertRaises(bridge.ProtocolError):
            bridge.clock_evidence(info, 1800000000003)
        crossed = raw.replace("2000.10|2000.12", "2000.12|2000.10")
        with self.assertRaises(bridge.ProtocolError):
            bridge.parse_response(req, crossed)

    def test_duplicate_is_noop_but_equal_changed_and_reverse_fail(self):
        gate = bridge.SampleGate()
        first = {"time_msc": 10, "bid": Decimal("1.0"), "ask": Decimal("1.1")}
        self.assertTrue(gate.observe(first))
        self.assertFalse(gate.observe(dict(first)))
        with self.assertRaises(bridge.ProtocolError):
            gate.observe({"time_msc": 10, "bid": Decimal("1.01"), "ask": Decimal("1.1")})
        with self.assertRaises(bridge.ProtocolError):
            gate.observe({"time_msc": 9, "bid": Decimal("1.0"), "ask": Decimal("1.1")})

    def test_broker_snapshots_are_separate_and_bounded(self):
        req = bridge.make_request("POSITIONS", symbol="XAUUSD", magic=30, request_id=self.rid)
        raw = f"readonlyv1|{self.rid}|OK|POSITIONS|XAUUSD|30|1|1001,0,0.02,30,7001"
        positions = bridge.parse_response(req, raw)
        self.assertEqual(positions["rows"][0]["identifier"], 7001)
        self.assertNotIn("virtual_book", positions)
        with self.assertRaises(bridge.ProtocolError):
            bridge.parse_response(req, raw.replace("|1|", "|33|"))
        with self.assertRaises(bridge.ProtocolError):
            bridge.parse_response(req, raw + ",comment")
        wrong_row_magic = raw.replace("1001,0,0.02,30,7001", "1001,0,0.02,31,7001")
        with self.assertRaises(bridge.ProtocolError):
            bridge.parse_response(req, wrong_row_magic)
        negative_row_magic = raw.replace("1001,0,0.02,30,7001", "1001,0,0.02,-1,7001")
        with self.assertRaises(bridge.ProtocolError):
            bridge.parse_response(req, negative_row_magic)

    def _fake_response(self, directory, response_factory, *, payload_override=None, ignore_request_id=None):
        command = Path(directory) / "cmd_s30_readonly.txt"
        response = Path(directory) / "res_s30_readonly.txt"

        def producer():
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                try:
                    line = command.read_text(encoding="ascii").strip()
                except FileNotFoundError:
                    time.sleep(0.005)
                    continue
                if ignore_request_id is not None and line.split("|")[1] == ignore_request_id:
                    time.sleep(0.005)
                    continue
                payload = payload_override if payload_override is not None else response_factory(line)
                response.write_bytes(payload if isinstance(payload, bytes) else (payload + "\n").encode("ascii"))
                return

        thread = threading.Thread(target=producer, daemon=True)
        thread.start()
        return thread

    def test_actual_file_poll_unknown_then_valid_binding_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            unknown_reader = bridge.ShadowReader(bridge.FileBridge(directory, timeout_seconds=0.5),
                                                 symbol="XAUUSD", magic=30)

            def unknown_response(line):
                req = line.split("|")
                return (f"readonlyv1|{req[1]}|OK|INFO|XAUUSD|{time.time_ns() // 1_000_000 - 100}|"
                        f"2000.10|2000.12|{req[4]}|{req[5]}|0")

            producer = self._fake_response(directory, unknown_response)
            with self.assertRaises(bridge.BindingUnverified):
                unknown_reader.sampled_quote()
            producer.join(timeout=0.5)
            old_request_id = Path(directory, "cmd_s30_readonly.txt").read_text(encoding="ascii").split("|")[1]

            valid = self.binding
            self.assertNotEqual(valid.binding_sha256, bridge.NonsecretBinding().binding_sha256)
            valid_reader = bridge.ShadowReader(bridge.FileBridge(directory, timeout_seconds=0.5),
                                               symbol="XAUUSD", magic=30, binding=valid)

            def valid_response(line):
                req = line.split("|")
                return (f"readonlyv1|{req[1]}|OK|INFO|XAUUSD|{time.time_ns() // 1_000_000 - 100}|"
                        f"2000.10|2000.12|{req[4]}|{req[5]}|1")

            producer = self._fake_response(directory, valid_response, ignore_request_id=old_request_id)
            decoded = valid_reader.sampled_quote()
            producer.join(timeout=0.5)
            self.assertIsInstance(decoded, bridge.DecodedInfo)
            self.assertEqual(decoded.verified_binding.binding_id, valid.binding_id)
            self.assertEqual(decoded.binding_sha256, valid.binding_sha256)
            self.assertGreaterEqual(decoded.available_ms, decoded.received_ms)
            envelope = bridge.clock_evidence(decoded, time.time_ns() // 1_000_000 + 1)
            self.assertEqual(envelope.release_time_upper_bound_ms, decoded.received_ms)

    def test_file_poll_reads_only_frame_limit_plus_one(self):
        with tempfile.TemporaryDirectory() as directory:
            client = bridge.FileBridge(directory, timeout_seconds=0.5)
            producer = self._fake_response(directory, lambda _line: "", payload_override=b"x" * (bridge.MAX_FRAME_BYTES + 1000000))
            with self.assertRaises(bridge.FrameTooLarge):
                client.request(bridge.make_request("ECHO", request_id=self.rid))
            producer.join(timeout=0.5)
            self.assertEqual(client.response_path.stat().st_size, bridge.MAX_FRAME_BYTES + 1000000)

    def test_valid_frame_finishing_at_deadline_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            client = bridge.FileBridge(directory, timeout_seconds=0.5)
            request = bridge.make_request("ECHO", request_id=self.rid)
            client.response_path.write_text(f"readonlyv1|{self.rid}|OK|ECHO|echo\n", encoding="ascii")
            # Entry and file read start before deadline; parsing/file I/O completes at the deadline.
            with patch.object(bridge.time, "monotonic", side_effect=[0.0, 0.1, 0.5]):
                with self.assertRaises(bridge.BridgeTimeout):
                    client.request(request)

    def test_readonly_ea_has_no_order_mutation_calls(self):
        source = (STAGING / "BotBridge_s30_readonly.mq5").read_text(encoding="utf-8")
        for forbidden in ("CTrade", "OrderSend(", "PositionClose(", "OrderDelete(", "OPEN|", "CLOSE|"):
            self.assertNotIn(forbidden, source)
        for allowed in ("ECHO", "CAPS", "INFO", "POSITIONS", "ORDERS"):
            self.assertIn(allowed, source)


if __name__ == "__main__":
    unittest.main()
