"""Strict, read-only file bridge for the CentOS shadow research boundary.

This module deliberately has no dependency on the legacy EA bridge or execution
modules.  A quote is returned to a caller only after the wire response, exact
operator binding, and sampled clock envelope all validate.
Empty account/server/source-hash values are UNKNOWN by design. Runtime values
must be supplied through the explicit nonsecret operator binding after separate
verification; this module performs exact case-sensitive matching only.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, Mapping
import uuid

PROTOCOL = "readonlyv1"
OPS = frozenset({"ECHO", "CAPS", "INFO", "POSITIONS", "ORDERS"})
MAX_FRAME_BYTES = 16_384
MAX_ROWS = 32
POLL_SECONDS = 0.250
DEFAULT_TIMEOUT_SECONDS = 2.0
SYMBOL_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
INT_RE = re.compile(r"^(0|[1-9][0-9]{0,19})$")
DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,12})?$")
CLOCK_EVIDENCE_REFS = (
    "https://get.exness.help/hc/en-us/articles/360014390760-What-is-the-default-timezone-set-for-MetaTrader",
    "https://get.exness.help/hc/en-us/articles/4405235684498-Instrument-trading-hours",
)
_FILE_REQUEST_LOCK = threading.Lock()


class BridgeError(ValueError):
    """Base class for bounded, fail-closed protocol errors."""


class ProtocolError(BridgeError):
    pass


class RemoteError(ProtocolError):
    pass


class FrameTooLarge(ProtocolError):
    pass


class BindingUnverified(BridgeError):
    pass


class BridgeTimeout(BridgeError):
    pass


@dataclass(frozen=True)
class NonsecretBinding:
    """Exact operator binding; empty defaults intentionally cannot attest.

    Set expected_company/server from independent deployment evidence and
    ea_source_sha256 from the frozen readonly EA. The EA inputs must receive
    those exact values and this object's computed binding_sha256. No prefix,
    wildcard, case folding, or normalization is applied.
    """

    expected_company: str = ""
    expected_server: str = ""
    clock_evidence_refs: tuple[str, ...] = CLOCK_EVIDENCE_REFS
    ea_source_sha256: str = ""
    binding_id: str = "EXNESS_MT5_SERVER_UTC0_V001"
    time_basis: str = "UTC_EPOCH_MILLISECONDS"
    timezone: str = "UTC"
    dst_conversion: str = "NONE_UTC_EPOCH"

    @property
    def binding_sha256(self) -> str:
        return canonical_binding_sha256({
            "binding_id": self.binding_id,
            "clock_evidence_refs": list(self.clock_evidence_refs),
            "dst_conversion": self.dst_conversion,
            "ea_source_sha256": self.ea_source_sha256,
            "expected_company": self.expected_company,
            "expected_server": self.expected_server,
            "time_basis": self.time_basis,
            "timezone": self.timezone,
        })


_CODEC_TOKEN = object()


@dataclass(frozen=True, init=False)
class VerifiedBinding:
    """Created only by the response codec after exact EA attestation checks."""

    binding_id: str
    binding_sha256: str
    clock_evidence_refs: tuple[str, ...]
    time_basis: str
    _token: object

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("VerifiedBinding is codec-created")

    @classmethod
    def _from_codec(cls, binding: NonsecretBinding) -> "VerifiedBinding":
        obj = object.__new__(cls)
        for name, value in (("binding_id", binding.binding_id), ("binding_sha256", binding.binding_sha256),
                            ("clock_evidence_refs", tuple(binding.clock_evidence_refs)),
                            ("time_basis", binding.time_basis), ("_token", _CODEC_TOKEN)):
            object.__setattr__(obj, name, value)
        return obj


@dataclass(frozen=True, init=False)
class ClockEvidence:
    schema: str
    event_time_ms: int
    release_time_ms: None
    release_time_upper_bound_ms: int
    release_time_status: str
    broker_original_release_time_ms: None
    ingested_time_ms: int
    available_time_ms: int
    cutoff_time_ms: int
    source_provenance: str
    binding_id: str
    binding_sha256: str
    time_basis: str
    timezone: str
    dst_conversion: str
    clock_evidence_refs: tuple[str, ...]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("ClockEvidence is codec-created")

    @classmethod
    def _from_codec(cls, *values: Any) -> "ClockEvidence":
        obj = object.__new__(cls)
        for name, value in zip(cls.__dataclass_fields__, values):
            object.__setattr__(obj, name, value)
        return obj

    def asdict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "event_time_ms": self.event_time_ms,
            "release_time_ms": None,
            "release_time_upper_bound_ms": self.release_time_upper_bound_ms,
            "release_time_status": self.release_time_status,
            "broker_original_release_time_ms": None,
            "ingested_time_ms": self.ingested_time_ms,
            "available_time_ms": self.available_time_ms,
            "cutoff_time_ms": self.cutoff_time_ms,
            "source_provenance": self.source_provenance,
            "binding_id": self.binding_id,
            "binding_sha256": self.binding_sha256,
            "time_basis": self.time_basis,
            "timezone": self.timezone,
            "dst_conversion": self.dst_conversion,
            "clock_evidence_refs": list(self.clock_evidence_refs),
        }


@dataclass(frozen=True, init=False)
class DecodedInfo:
    symbol: str
    time_msc: int
    bid: Decimal
    ask: Decimal
    binding_id: str
    binding_sha256: str
    received_ms: int
    available_ms: int
    verified_binding: VerifiedBinding

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("DecodedInfo is codec-created")

    @classmethod
    def _from_codec(cls, *values: Any) -> "DecodedInfo":
        obj = object.__new__(cls)
        for name, value in zip(cls.__dataclass_fields__, values):
            object.__setattr__(obj, name, value)
        return obj


def _text(value: Any, name: str, max_len: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ProtocolError(f"invalid {name}")
    if any(ord(c) < 0x20 or ord(c) > 0x7e or c == "|" for c in value):
        raise ProtocolError(f"invalid {name} characters")
    return value


def _symbol(value: Any) -> str:
    value = _text(value, "symbol", 32)
    if not SYMBOL_RE.fullmatch(value):
        raise ProtocolError("invalid symbol")
    return value


def _exact_attestation_value(value: Any) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= 128
            and all(0x20 <= ord(c) <= 0x7e and c not in "|*?" for c in value))


def _identifier(value: Any) -> str:
    value = _text(value, "request id", 32)
    if not ID_RE.fullmatch(value):
        raise ProtocolError("invalid request id")
    return value


def _uint(value: str, field: str, *, allow_zero: bool = True) -> int:
    if not isinstance(value, str) or not INT_RE.fullmatch(value):
        raise ProtocolError(f"invalid {field}")
    result = int(value)
    if not allow_zero and result == 0:
        raise ProtocolError(f"invalid {field}")
    return result


def _decimal(value: str, field: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str) or len(value) > 32 or not DECIMAL_RE.fullmatch(value):
        raise ProtocolError(f"invalid {field}")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ProtocolError(f"invalid {field}") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise ProtocolError(f"invalid {field}")
    return result


def _wire_line(line: str) -> list[str]:
    if not isinstance(line, str) or not line:
        raise ProtocolError("invalid frame size or encoding")
    try:
        frame_size = len(line.encode("ascii", "strict"))
    except UnicodeEncodeError as exc:
        raise ProtocolError("invalid frame encoding") from exc
    if frame_size > MAX_FRAME_BYTES:
        raise ProtocolError("invalid frame size")
    if "\r" in line or "\n" in line or "\x00" in line:
        raise ProtocolError("invalid frame terminator")
    return line.split("|")


def make_request(opcode: str, *, symbol: str | None = None, magic: int | None = None,
                 binding: NonsecretBinding | None = None, request_id: str | None = None) -> str:
    if opcode not in OPS:
        raise ProtocolError("unsupported opcode")
    rid = _identifier(uuid.uuid4().hex if request_id is None else request_id)
    if opcode == "ECHO":
        if symbol is not None or magic is not None or binding is not None:
            raise ProtocolError("ECHO has no arguments")
        fields = [PROTOCOL, rid, opcode]
    elif opcode == "INFO":
        if symbol is None or magic is not None or not isinstance(binding, NonsecretBinding):
            raise ProtocolError("INFO requires symbol and binding")
        fields = [PROTOCOL, rid, opcode, _symbol(symbol), _text(binding.binding_id, "binding id", 64)]
        if not SHA_RE.fullmatch(binding.binding_sha256):
            raise ProtocolError("invalid binding hash")
        fields.append(binding.binding_sha256)
    else:
        if symbol is None or binding is not None:
            raise ProtocolError(f"{opcode} requires symbol")
        fields = [PROTOCOL, rid, opcode, _symbol(symbol)]
        if opcode in {"POSITIONS", "ORDERS"}:
            if not isinstance(magic, int) or isinstance(magic, bool) or not 0 <= magic <= 2_147_483_647:
                raise ProtocolError("invalid magic")
            fields.append(str(magic))
        elif magic is not None:
            raise ProtocolError("CAPS has no magic")
    return "|".join(fields)


def _validate_request(line: str) -> list[str]:
    f = _wire_line(line)
    if len(f) < 3 or f[0] != PROTOCOL or f[2] not in OPS:
        raise ProtocolError("invalid request header")
    _identifier(f[1])
    expected = {"ECHO": 3, "CAPS": 4, "INFO": 6, "POSITIONS": 5, "ORDERS": 5}[f[2]]
    if len(f) != expected:
        raise ProtocolError("invalid request arity")
    if f[2] != "ECHO":
        _symbol(f[3])
    if f[2] == "INFO":
        if f[4] != "EXNESS_MT5_SERVER_UTC0_V001":
            raise ProtocolError("unsupported binding id")
        _text(f[4], "binding id", 64)
        if not SHA_RE.fullmatch(f[5]):
            raise ProtocolError("invalid binding hash")
    if f[2] in {"POSITIONS", "ORDERS"}:
        _uint(f[4], "magic")
    return f


def parse_response(request: str, response: str) -> dict[str, Any]:
    req = _validate_request(request)
    rid, opcode = _identifier(req[1]), req[2]
    f = _wire_line(response)
    if len(f) == 5 and f[:3] == [PROTOCOL, rid, "ERR"] and f[3] == opcode:
        if not re.fullmatch(r"[A-Z0-9_]{1,32}", f[4]):
            raise ProtocolError("invalid remote error code")
        raise RemoteError(f"remote rejected {opcode}: {f[4]}")
    if len(f) < 4 or f[:3] != [PROTOCOL, rid, "OK"] or f[3] != opcode:
        raise ProtocolError("response identity/status/opcode mismatch")
    if opcode == "ECHO":
        if len(f) != 5 or f[4] != "echo":
            raise ProtocolError("invalid ECHO response")
        return {"opcode": opcode, "request_id": rid, "value": "echo"}
    if opcode == "CAPS":
        if len(f) != 12 or f[4] != req[3]:
            raise ProtocolError("invalid CAPS arity or symbol")
        if f[5] not in {"0", "1"} or f[6] not in {"0", "1"}:
            raise ProtocolError("invalid capability flags")
        digits = _uint(f[7], "digits")
        point = _decimal(f[8], "point", positive=True)
        vmin = _decimal(f[9], "volume minimum", positive=True)
        vmax = _decimal(f[10], "volume maximum", positive=True)
        step = _decimal(f[11], "volume step", positive=True)
        if digits > 12 or vmax < vmin:
            raise ProtocolError("inconsistent CAPS values")
        return {"opcode": opcode, "request_id": rid, "symbol": f[4], "hedging": f[5] == "1",
                "close_by": f[6] == "1", "digits": digits, "point": point,
                "volume_min": vmin, "volume_max": vmax, "volume_step": step}
    if opcode == "INFO":
        if len(f) != 11 or f[4] != req[3] or f[8] != req[4] or f[9] != req[5]:
            raise ProtocolError("invalid INFO arity or binding echo")
        tm = _uint(f[5], "time_msc", allow_zero=False)
        bid, ask = _decimal(f[6], "bid", positive=True), _decimal(f[7], "ask", positive=True)
        if ask <= bid or f[10] not in {"0", "1"}:
            raise ProtocolError("invalid INFO quote/attestation")
        return {"opcode": opcode, "request_id": rid, "symbol": f[4], "time_msc": tm,
                "bid": bid, "ask": ask, "binding_id": f[8], "binding_sha256": f[9],
                "attested": f[10] == "1"}
    if opcode in {"POSITIONS", "ORDERS"}:
        if len(f) != 8 or f[4] != req[3] or f[5] != req[4]:
            raise ProtocolError(f"invalid {opcode} arity or selector")
        count = _uint(f[6], "row count")
        rows = [] if f[7] == "-" else f[7].split(";")
        if count > MAX_ROWS or len(rows) != count:
            raise ProtocolError("invalid row count")
        parsed = []
        for row in rows:
            cols = row.split(",")
            expected = 5 if opcode == "POSITIONS" else 6
            if len(cols) != expected:
                raise ProtocolError("invalid row arity")
            item = {"ticket": _uint(cols[0], "ticket", allow_zero=False),
                    "kind": _uint(cols[1], "kind"),
                    "volume": _decimal(cols[2], "volume", positive=True),
                    "magic": _uint(cols[3], "magic")}
            if opcode == "POSITIONS":
                item["identifier"] = _uint(cols[4], "identifier", allow_zero=False)
            else:
                item["state"] = _uint(cols[4], "state")
                item["volume_current"] = _decimal(cols[5], "current volume")
            if item["magic"] != int(req[4]):
                raise ProtocolError("row magic does not match requested selector")
            parsed.append(item)
        return {"opcode": opcode, "request_id": rid, "symbol": f[4], "magic": int(f[5]), "rows": parsed}
    raise ProtocolError("unsupported response opcode")


def _now_ms() -> int:
    # Explicit wall-clock sampling at millisecond resolution, as specified by the contract.
    return time.time_ns() // 1_000_000


def _verified_binding_from_info(info: Mapping[str, Any], binding: NonsecretBinding) -> VerifiedBinding:
    """Response-codec boundary: only exact id/hash plus EA match flag attests configuration."""
    if not isinstance(info, Mapping) or info.get("opcode") != "INFO":
        raise ProtocolError("INFO response required")
    if (not isinstance(binding, NonsecretBinding)
            or not _exact_attestation_value(binding.expected_company)
            or not _exact_attestation_value(binding.expected_server)
            or not isinstance(binding.ea_source_sha256, str) or not SHA_RE.fullmatch(binding.ea_source_sha256)
            or binding.binding_id != "EXNESS_MT5_SERVER_UTC0_V001"
            or not SHA_RE.fullmatch(binding.binding_sha256)
            or tuple(binding.clock_evidence_refs) != CLOCK_EVIDENCE_REFS
            or binding.time_basis != "UTC_EPOCH_MILLISECONDS" or binding.timezone != "UTC"
            or binding.dst_conversion != "NONE_UTC_EPOCH"):
        raise BindingUnverified("operator clock/account binding is UNKNOWN")
    if info.get("attested") is not True or info.get("binding_id") != binding.binding_id \
            or info.get("binding_sha256") != binding.binding_sha256:
        raise BindingUnverified("EA exact-match attestation does not match operator binding")
    return VerifiedBinding._from_codec(binding)


def clock_evidence(info: DecodedInfo, cutoff_time_ms: int) -> ClockEvidence:
    """Create the immutable exact-v002 envelope after codec verification."""
    if not isinstance(info, DecodedInfo):
        raise ProtocolError("codec-decoded INFO required")
    binding = info.verified_binding
    if not isinstance(binding, VerifiedBinding) or binding._token is not _CODEC_TOKEN:
        raise BindingUnverified("codec-verified binding required")
    if info.binding_id != binding.binding_id or info.binding_sha256 != binding.binding_sha256:
        raise BindingUnverified("decoded INFO binding does not match verified binding")
    event = info.time_msc
    bid, ask = info.bid, info.ask
    if not isinstance(event, int) or isinstance(event, bool) or event <= 0:
        raise ProtocolError("invalid event time")
    if not isinstance(bid, Decimal) or not isinstance(ask, Decimal) or not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask <= bid:
        raise ProtocolError("invalid quote values")
    receive = info.received_ms
    available = info.available_ms
    if not isinstance(receive, int) or isinstance(receive, bool) or event > receive:
        raise ProtocolError("source event is later than receive observation")
    if not isinstance(available, int) or isinstance(available, bool) or receive > available:
        raise ProtocolError("invalid availability observation")
    cutoff = cutoff_time_ms
    if not isinstance(cutoff, int) or isinstance(cutoff, bool) or not (event <= receive <= available <= cutoff):
        raise ProtocolError("sampled clock order is invalid")
    refs = tuple(binding.clock_evidence_refs)
    if any(not isinstance(ref, str) or not ref or len(ref) > 256 for ref in refs):
        raise BindingUnverified("invalid clock evidence references")
    return ClockEvidence._from_codec("bot30_sampled_clock_envelope_v002", event, None, receive,
                                     "consumer_observed_upper_bound", None, receive, available, cutoff,
                                     "SAMPLED_CURRENT_BID_ASK_SHADOW_V001", binding.binding_id,
                                     binding.binding_sha256, "UTC_EPOCH_MILLISECONDS", "UTC",
                                     "NONE_UTC_EPOCH", refs)


class SampleGate:
    """Drops only exact duplicate snapshots; changed/equal or backward time fails closed."""

    def __init__(self) -> None:
        self._last: tuple[int, Decimal, Decimal] | None = None

    def observe(self, info: Mapping[str, Any]) -> bool:
        key = (info.get("time_msc"), info.get("bid"), info.get("ask"))
        if (not isinstance(key[0], int) or isinstance(key[0], bool)
                or not isinstance(key[1], Decimal) or not isinstance(key[2], Decimal)
                or not key[1].is_finite() or not key[2].is_finite()
                or key[1] <= 0 or key[2] <= key[1]):
            raise ProtocolError("invalid quote sample")
        if self._last == key:
            return False
        if self._last is not None and key[0] <= self._last[0]:
            raise ProtocolError("non-increasing quote timestamp")
        self._last = key
        return True


class FileBridge:
    """Single-flight client for the two fixed IPC filenames."""

    def __init__(self, directory: str | os.PathLike[str], *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        self.directory = Path(directory)
        self.command_path = self.directory / "cmd_s30_readonly.txt"
        self.response_path = self.directory / "res_s30_readonly.txt"
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or not 0.05 <= timeout_seconds <= 10.0:
            raise ValueError("timeout must be 0.05..10 seconds")
        self.timeout_seconds = timeout_seconds
        self._request_lock = _FILE_REQUEST_LOCK

    def request(self, line: str) -> tuple[dict[str, Any], int]:
        _validate_request(line)
        if not self._request_lock.acquire(blocking=False):
            raise BridgeError("serial bridge request already active")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            fd, tmpname = tempfile.mkstemp(prefix="cmd_readonly_", dir=self.directory)
            try:
                with os.fdopen(fd, "w", encoding="ascii", newline="") as stream:
                    stream.write(line + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmpname, self.command_path)
            finally:
                if os.path.exists(tmpname):
                    os.unlink(tmpname)
            deadline = time.monotonic() + self.timeout_seconds
            while time.monotonic() < deadline:
                try:
                    with self.response_path.open("rb") as response_stream:
                        raw = response_stream.read(MAX_FRAME_BYTES + 1)
                except FileNotFoundError:
                    raw = b""
                response_received_ms = _now_ms()
                if raw:
                    if len(raw) > MAX_FRAME_BYTES:
                        raise FrameTooLarge(f"response exceeds {MAX_FRAME_BYTES}-byte frame limit")
                    try:
                        if raw.endswith(b"\r\n"):
                            frame = raw[:-2]
                        elif raw.endswith(b"\n"):
                            frame = raw[:-1]
                        else:
                            time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))
                            continue
                        if b"\r" in frame or b"\n" in frame:
                            raise ProtocolError("response must contain one frame")
                        response = frame.decode("ascii")
                        parsed = parse_response(line, response)
                        if time.monotonic() >= deadline:
                            raise BridgeTimeout("valid response completed after request deadline")
                        return parsed, response_received_ms
                    except RemoteError:
                        raise
                    except (UnicodeDecodeError, ProtocolError):
                        # A partial/stale atomic-file observation is retried until the fixed deadline.
                        pass
                time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))
            raise BridgeTimeout("read-only bridge response timed out")
        finally:
            self._request_lock.release()


class ShadowReader:
    """Read-only quote/snapshot API; snapshots never mutate a strategy virtual book."""

    def __init__(self, bridge: FileBridge, *, symbol: str, magic: int,
                 binding: NonsecretBinding | None = None):
        self.bridge, self.symbol, self.magic = bridge, _symbol(symbol), magic
        self.binding = binding or NonsecretBinding()
        if not isinstance(magic, int) or isinstance(magic, bool) or not 0 <= magic <= 2_147_483_647:
            raise ValueError("invalid magic")
        self._sample_gate = SampleGate()

    def _call(self, opcode: str, **kwargs: Any) -> tuple[dict[str, Any], int]:
        request = make_request(opcode, symbol=self.symbol, magic=self.magic if opcode in {"POSITIONS", "ORDERS"} else None,
                               binding=self.binding if opcode == "INFO" else None, **kwargs)
        return self.bridge.request(request)

    def echo(self) -> dict[str, Any]:
        parsed, _ = self.bridge.request(make_request("ECHO"))
        return parsed

    def capabilities(self) -> dict[str, Any]:
        return self._call("CAPS")[0]

    def broker_positions(self) -> dict[str, Any]:
        return self._call("POSITIONS")[0]

    def broker_orders(self) -> dict[str, Any]:
        return self._call("ORDERS")[0]

    def sampled_quote(self) -> DecodedInfo | None:
        info, received_ms = self._call("INFO")
        verified = _verified_binding_from_info(info, self.binding)
        if not self._sample_gate.observe(info):
            return None
        return DecodedInfo._from_codec(info["symbol"], info["time_msc"], info["bid"], info["ask"],
                                       info["binding_id"], info["binding_sha256"], received_ms,
                                       _now_ms(), verified)


def canonical_binding_sha256(binding_payload: Mapping[str, Any]) -> str:
    """Hash canonical nonsecret binding JSON; caller retains and supplies the binding itself."""
    raw = json.dumps(binding_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(raw).hexdigest()

