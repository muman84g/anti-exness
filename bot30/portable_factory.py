"""Root-relative, source-pinned control-book factory for the offline shadow.

This module loads only the fixed source bundle described by source_bindings.json.
It creates the native control recipe, not a market reader or an adopted runtime.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any


PORTABLE_ID = "bot30_cent_os_shadow_portable_v001"
PORTABLE_STATUS = "PRIVATE_CONTROL_RECIPE_ONLY_NOT_MARKET_OR_LIVE"
CONTROL_VARIANT = "R3_CURRENT_QUEUE_CONTROL_V001"
NATIVE_VARIANT = "PRIORITY_R3_NORESET_V001"
ENGINE_VARIANT = "H_BE_P2_PRIORITY01_V001"
LIFECYCLE_MODE = "L_RESET"
FACTORY_TRACE_LIMIT = 8192
ADAPTER_TRACE_LIMIT = 64
MAX_RETAINED_ROWS = 4096
BINDINGS_SHA256 = "7311d4224ec6eefc24ee0e4207e4c1fc33aaf0e43fc0154146e399e5388706f2"
BINDINGS_BYTES = 3630
AUTHORITY_FREEZE_SHA256 = "b9759add7213f2a344bf37410fad146d35627fc6d50cfbae23dc5a369e4b7d36"
AUTHORITY_FREEZE_BYTES = 25745
CLOCK_CONTRACT_SHA256 = "3aa6a1cc5b0f8a531ae37a255cdae410d237d9bc5ba697454e4443ef92fe82aa"
CLOCK_CONTRACT_BYTES = 2768

_TOKEN = object()
_REGISTRY: dict[str, ModuleType] = {}
_RUNTIME: dict[str, Any] | None = None

_PINS: dict[str, tuple[str, str, int]] = {
    "runner": ("vendor_runtime/research/work/improvement_v001/replay/research_runner_budget3600_v001.py", "0fa3fd0fadb9212175d23157ccf66d3f899255ee894c61b4e5e1ceffe963c689", 27970),
    "run_main": ("vendor_runtime/research/work/discovery_v005/run_main_budget3600_v001.py", "368ae857fb9966e5048aefa43ac407be71442341966cd70e455091a99cb60d4c", 62579),
    "research_wrapper": ("vendor_runtime/backtest/code/research_entry_close.py", "16c0c2b368c9864f7c06338c6db02d6f10368557dc177193d8e2a557d374b475", 17220),
    "variants_config": ("vendor_runtime/backtest/candidate-config/variants_v001.json", "c38adcb23a0a402e3a1fe9b2acb38b7fca9717676d6b75990213f5328d40ee5d", 3213),
    "priority_factory": ("vendor_runtime/diagnostics/p2_priority_v001/research_hbe_p2_priority_v001.py", "85682d5eee03e4a3128ea1f0f81e06d1a96711aed8d49bce1986c2cc7e3b3b5d", 8184),
    "hbe_baseline": ("vendor_runtime/diagnostics/structure_identification_v001/research_h_be_hedge_scope_v001.py", "fa6e78a907cbbcee766f993f25dd8c24df02d503517abf9ca30a5f3d6cb84981", 8800),
    "count_component": ("vendor_runtime/diagnostics/mechanism_selection_v001/research_p2_count_v001.py", "713e3478d76874b64989362c260159c67b6a04723fba7635e26e984a295cd166", 6083),
    "native/h2_engine.py": ("vendor_runtime/backtest/code/native_v006/h2_engine.py", "47da2b8b6cbe3a6fc24426e9a11ca55642096e2cf1ad55744e863cf80b9b7396", 110638),
    "native/lifecycle_market_adapter.py": ("vendor_runtime/backtest/code/native_v006/lifecycle_market_adapter.py", "786f6490999379d63ffd3646a65a04b15911abd136923096160c2f5a82bb4c2a", 9226),
    "native/a3_lifecycle_boundary.py": ("vendor_runtime/backtest/code/native_v006/a3_lifecycle_boundary.py", "cd15541aef8b459a31ef79e905c33e32907d1c60596a20e2b47397eaac83dac8", 5010),
    "quote_validator": ("vendor_runtime/support/quote_connection.py", "4907684931e4f9e04959490a12aef1b10abb1699153b2ebc327f239146de7112", 5430),
    "clock_contract": ("clock_contract_v002.json", CLOCK_CONTRACT_SHA256, CLOCK_CONTRACT_BYTES),
    "r3_memory_adapter": ("vendor_runtime/support/r3_memory_adapter_v001.py", "30251ac4e063f500f8e2b2d56ec37f55342651c9861def10c586c06cd4ec29a7", 18235),
    "quantity_rules": ("vendor_runtime/support/quantity_rules_v004.py", "e1c56a83f3d3a57f6e85b0c7c3b04b154641a3eb9e9a20bd5ecb3bd84f6212e4", 8100),
}


class PortableIdentityError(RuntimeError):
    """A root-relative source, fixed recipe, or cache identity was rejected."""


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _root() -> Path:
    return Path(__file__).resolve(strict=True).parent


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PortableIdentityError(f"duplicate source binding key: {key}")
        result[key] = value
    return result


def _assert_no_reparse_components(path: Path, root: Path) -> None:
    """Reject symlink/junction components before reading a bundled file."""
    root_abs = Path(os.path.abspath(root))
    path_abs = Path(os.path.abspath(path))
    try:
        relative = path_abs.relative_to(root_abs)
    except ValueError as exc:
        raise PortableIdentityError("portable dependency escaped its root") from exc
    current = root_abs
    # Walk lexical components. The root itself was resolved from this module.
    current = root_abs
    for component in relative.parts:
        current = current / component
        try:
            info = current.lstat()
        except OSError as exc:
            raise PortableIdentityError("portable dependency path is unavailable") from exc
        attrs = getattr(info, "st_file_attributes", 0)
        is_reparse = bool(attrs & 0x400)
        if current.is_symlink() or is_reparse:
            raise PortableIdentityError("portable dependency has a reparse/symlink component")


def _read_relative(relative: str, expected_sha256: str, expected_bytes: int, *, root: Path) -> tuple[Path, bytes]:
    if (type(relative) is not str or not relative or "\\" in relative
            or PurePosixPath(relative).as_posix() != relative
            or any(part in {"", ".", ".."} for part in PurePosixPath(relative).parts)):
        raise PortableIdentityError("dependency path must be a normalized bundle-relative path")
    lexical = root.joinpath(*PurePosixPath(relative).parts)
    _assert_no_reparse_components(lexical, root)
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
        info = resolved.stat()
        if not resolved.is_file() or info.st_size != expected_bytes:
            raise PortableIdentityError(f"pinned dependency size/type mismatch: {relative}")
        with resolved.open("rb") as stream:
            raw = stream.read(expected_bytes + 1)
    except PortableIdentityError:
        raise
    except (OSError, ValueError) as exc:
        raise PortableIdentityError(f"pinned dependency is missing or outside its root: {relative}") from exc
    if len(raw) != expected_bytes or _sha256(raw) != expected_sha256:
        raise PortableIdentityError(f"pinned dependency bytes differ: {relative}")
    return resolved, raw


def _load_bindings(root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    bindings_path = root / "source_bindings.json"
    _assert_no_reparse_components(bindings_path, root)
    try:
        with bindings_path.open("rb") as stream:
            raw = stream.read(BINDINGS_BYTES + 1)
        if len(raw) != BINDINGS_BYTES or _sha256(raw) != BINDINGS_SHA256:
            raise PortableIdentityError("source_bindings.json does not match its frozen bytes")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except PortableIdentityError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableIdentityError("source_bindings.json is unavailable or invalid") from exc
    expected_top = {
        "artifact", "authority_source_freeze_sha256", "authority_source_freeze_bytes",
        "portable_root", "vendor_root", "files",
    }
    if (type(value) is not dict or set(value) != expected_top
            or value["artifact"] != "bot30_cent_os_shadow_portable_bindings_v001"
            or value["authority_source_freeze_sha256"] != AUTHORITY_FREEZE_SHA256
            or type(value["authority_source_freeze_bytes"]) is not int
            or value["authority_source_freeze_bytes"] != AUTHORITY_FREEZE_BYTES
            or value["portable_root"] != "." or value["vendor_root"] != "vendor_runtime"
            or type(value["files"]) is not list):
        raise PortableIdentityError("portable source binding identity/schema mismatch")
    entries: dict[str, dict[str, Any]] = {}
    for row in value["files"]:
        if type(row) is not dict or set(row) != {"label", "path", "sha256", "bytes"}:
            raise PortableIdentityError("portable dependency entry has an unexpected schema")
        label = row["label"]
        if type(label) is not str or label in entries:
            raise PortableIdentityError("portable dependency label is missing or duplicated")
        expected = _PINS.get(label)
        if (expected is None or row["path"] != expected[0]
                or row["sha256"] != expected[1]
                or type(row["bytes"]) is not int or row["bytes"] != expected[2]):
            raise PortableIdentityError(f"portable dependency pin differs from fixed contract: {label}")
        entries[label] = row
    if set(entries) != set(_PINS):
        raise PortableIdentityError("portable source binding set is incomplete")
    return entries, value


def _module_path_is(
    module: Any,
    expected_path: Path,
    digest: str,
    expected_bytes: int,
    *,
    marker: str = "__portable_verified_source_sha256__",
) -> bool:
    try:
        actual = Path(module.__file__).resolve(strict=True)
        expected = expected_path.resolve(strict=True)
        with actual.open("rb") as stream:
            raw = stream.read(expected_bytes + 1)
        return (os.path.normcase(str(actual)) == os.path.normcase(str(expected))
                and len(raw) == expected_bytes
                and getattr(module, marker, None) == digest
                and _sha256(raw) == digest)
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _execute_pinned(name: str, entry: dict[str, Any], *, root: Path) -> ModuleType:
    path, source = _read_relative(entry["path"], entry["sha256"], entry["bytes"], root=root)
    existing = sys.modules.get(name)
    registered = _REGISTRY.get(name)
    if existing is not None or registered is not None:
        if (existing is None or existing is not registered or type(existing) is not ModuleType
                or getattr(existing, "__portable_verified_token__", None) is not _TOKEN
                or not _module_path_is(existing, path, entry["sha256"], entry["bytes"])):
            raise PortableIdentityError(f"foreign, partial, or orphan module cache rejected: {name}")
        return existing
    try:
        code = compile(source, str(path), "exec", dont_inherit=True)
    except (SyntaxError, TypeError, ValueError) as exc:
        raise PortableIdentityError(f"fixed dependency could not be compiled: {name}") from exc
    module = ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ""
    module.__loader__ = None
    module.__cached__ = None
    module.__spec__ = None
    module.__portable_verified_token__ = _TOKEN
    module.__portable_verified_source_sha256__ = entry["sha256"]
    sys.modules[name] = module
    _REGISTRY[name] = module
    try:
        exec(code, module.__dict__)
        if sys.modules.get(name) is not module:
            raise PortableIdentityError(f"fixed module replaced its own cache entry: {name}")
        module.__portable_verified_token__ = _TOKEN
        module.__portable_verified_source_sha256__ = entry["sha256"]
        if not _module_path_is(module, path, entry["sha256"], entry["bytes"]):
            raise PortableIdentityError(f"fixed module identity changed during execution: {name}")
    except BaseException:
        if sys.modules.get(name) is module:
            sys.modules.pop(name, None)
        if _REGISTRY.get(name) is module:
            _REGISTRY.pop(name, None)
        raise
    return module


def _assert_unloaded(names: tuple[str, ...]) -> None:
    if any(name in sys.modules for name in names):
        raise PortableIdentityError("foreign or previously loaded fixed runtime modules; use a fresh process")


def _external_is(module: Any, entry: dict[str, Any], *, root: Path) -> bool:
    try:
        path = root.joinpath(*PurePosixPath(entry["path"]).parts)
        return (type(module) is ModuleType and sys.modules.get(module.__name__) is module
                and _module_path_is(module, path, entry["sha256"], entry["bytes"], marker="__verified_source_sha256__"))
    except (AttributeError, TypeError, ValueError):
        return False


def _register_external(name: str, module: Any, entry: dict[str, Any], *, root: Path) -> None:
    if not _external_is(module, entry, root=root):
        raise PortableIdentityError(f"verified external dependency identity mismatch: {name}")
    existing = _REGISTRY.get(name)
    if existing is not None and existing is not module:
        raise PortableIdentityError(f"verified external dependency ownership changed: {name}")
    _REGISTRY[name] = module


def _assert_runtime_ownership(runtime: dict[str, Any]) -> None:
    for name, module in _REGISTRY.items():
        if sys.modules.get(name) is not module:
            raise PortableIdentityError(f"portable owned module cache changed: {name}")
    for name, module in runtime["owned_modules"].items():
        if sys.modules.get(name) is not module or _REGISTRY.get(name) is not module:
            raise PortableIdentityError(f"portable exact-module ownership changed: {name}")


def _load_runtime() -> dict[str, Any]:
    global _RUNTIME
    if sys.dont_write_bytecode is not True:
        raise PortableIdentityError("portable loading requires bytecode cache writes disabled")
    if _RUNTIME is not None:
        _assert_runtime_ownership(_RUNTIME)
        return _RUNTIME
    root = _root()
    entries, _ = _load_bindings(root)
    verified: dict[str, dict[str, Any]] = {}
    for label, entry in entries.items():
        path, raw = _read_relative(entry["path"], entry["sha256"], entry["bytes"], root=root)
        verified[label] = {"path": path, "raw": raw, **entry}

    quote = _execute_pinned("_bot30_cent_os_shadow_quote_v001", entries["quote_validator"], root=root)
    r3 = _execute_pinned("_bot30_cent_os_shadow_r3_memory_v001", entries["r3_memory_adapter"], root=root)
    quantity = _execute_pinned("_bot30_cent_os_shadow_quantity_rules_v004", entries["quantity_rules"], root=root)
    runner = _execute_pinned("_bot30_cent_os_shadow_runner_v001", entries["runner"], root=root)
    priority = _execute_pinned("_bot30_cent_os_shadow_priority_factory_v001", entries["priority_factory"], root=root)

    vendor_root = root / "vendor_runtime"
    runner.WORKSPACE = vendor_root / "research"
    runner.CAMPAIGN = vendor_root / "backtest"
    runner.CODE = vendor_root / "backtest" / "code"
    runner.CORE_DIR = runner.CODE / "native_v006"
    runner.REPLAY_ROOT = vendor_root / "research" / "outputs" / "improvement_v001" / "replay"
    runner.RUN_MAIN = root.joinpath(*PurePosixPath(entries["run_main"]["path"]).parts)
    runner.CONFIG_PATH = root.joinpath(*PurePosixPath(entries["variants_config"]["path"]).parts)

    native_names = (
        "h2_engine", "lifecycle_market_adapter", "a3_lifecycle_boundary",
        "bot30_v006_replay_accounting", "bot30_research_entry_close_v001",
        "_task_hbe_p2_priority_baseline_v001", "_task_h_be_hedge_scope_p2_count_v001",
    )
    _assert_unloaded(native_names)
    core = runner._load_runner_and_core()
    if type(core) is not tuple or len(core) != 5:
        raise PortableIdentityError("verified runner returned an unexpected native closure")
    run_main, h2, native_adapter, boundary_module, loaded = core
    if type(loaded) is not dict or set(loaded) != {
        "h2_engine.py", "lifecycle_market_adapter.py", "a3_lifecycle_boundary.py",
    }:
        raise PortableIdentityError("verified runner returned an incomplete native set")
    path_names = {
        "run_main": ("bot30_v006_replay_accounting", entries["run_main"]),
        "research_wrapper": ("bot30_research_entry_close_v001", entries["research_wrapper"]),
        "native/h2_engine.py": ("h2_engine", entries["native/h2_engine.py"]),
        "native/lifecycle_market_adapter.py": ("lifecycle_market_adapter", entries["native/lifecycle_market_adapter.py"]),
        "native/a3_lifecycle_boundary.py": ("a3_lifecycle_boundary", entries["native/a3_lifecycle_boundary.py"]),
    }
    loaded_modules = {
        "bot30_v006_replay_accounting": run_main,
        "bot30_research_entry_close_v001": sys.modules.get("bot30_research_entry_close_v001"),
        "h2_engine": h2,
        "lifecycle_market_adapter": native_adapter,
        "a3_lifecycle_boundary": boundary_module,
    }
    for label, (module_name, entry) in path_names.items():
        module = loaded_modules[module_name]
        path = root.joinpath(*PurePosixPath(entry["path"]).parts)
        if not _module_path_is(module, path, entry["sha256"], entry["bytes"], marker="__verified_source_sha256__"):
            raise PortableIdentityError(f"runner/native exact source identity mismatch: {label}")
    for filename, label in (
        ("h2_engine.py", "native/h2_engine.py"),
        ("lifecycle_market_adapter.py", "native/lifecycle_market_adapter.py"),
        ("a3_lifecycle_boundary.py", "native/a3_lifecycle_boundary.py"),
    ):
        if loaded[filename].get("sha256") != entries[label]["sha256"]:
            raise PortableIdentityError(f"runner native receipt differs: {filename}")

    if run_main._VERIFIED_VENDOR_MODULES is None:
        raise PortableIdentityError("runner did not retain its verified native cache")
    baseline, baseline_sha = priority.load_pinned_hbe_component()
    if baseline_sha != entries["hbe_baseline"]["sha256"] or not _external_is(baseline, entries["hbe_baseline"], root=root):
        raise PortableIdentityError("H_BE baseline did not load exact portable bytes")
    count, count_sha = baseline.load_pinned_count_component()
    if count_sha != entries["count_component"]["sha256"] or not _external_is(count, entries["count_component"], root=root):
        raise PortableIdentityError("P2 count component did not load exact portable bytes")
    _register_external("_task_hbe_p2_priority_baseline_v001", baseline, entries["hbe_baseline"], root=root)
    _register_external("_task_h_be_hedge_scope_p2_count_v001", count, entries["count_component"], root=root)

    owned_modules = {
        name: module for name, module in _REGISTRY.items()
    }
    runtime = {
        "root": root,
        "entries": entries,
        "verified_files": verified,
        "quote_validator": quote,
        "r3_adapter": r3,
        "quantity_rules": quantity,
        "runner": runner,
        "run_main": run_main,
        "wrapper": sys.modules["bot30_research_entry_close_v001"],
        "priority_factory": priority,
        "h2": h2,
        "native_adapter": native_adapter,
        "boundary_module": boundary_module,
        "hbe_baseline": baseline,
        "count_component": count,
        "loaded_native": loaded,
        "owned_modules": owned_modules,
        "clock_contract": json.loads(verified["clock_contract"]["raw"].decode("utf-8"), object_pairs_hook=_reject_duplicate_keys),
    }
    _assert_runtime_ownership(runtime)
    _RUNTIME = runtime
    return runtime


def _online_config(config_type: type, activation_epoch_ms: int) -> Any:
    if type(activation_epoch_ms) is not int or activation_epoch_ms <= 0:
        raise PortableIdentityError("online activation must be a positive exact epoch-millisecond integer")
    try:
        instant = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=activation_epoch_ms)
        raw_date = instant.strftime("%Y.%m.%d")
        raw_time = instant.strftime("%H:%M:%S.%f")[:-3]
        default = config_type()
        config = replace(
            default,
            activation_raw_date=raw_date,
            activation_raw_time=raw_time,
            activation_boundary_label="first accepted actual event_time_ms; fixed for this run",
        )
    except (AttributeError, OverflowError, TypeError, ValueError) as exc:
        raise PortableIdentityError("native config cannot represent the fixed online activation") from exc
    fields = getattr(config_type, "__dataclass_fields__", None)
    if type(fields) is not dict:
        raise PortableIdentityError("native ReplayConfig fields unavailable")
    allowed = {"activation_raw_date", "activation_raw_time", "activation_boundary_label"}
    baseline = config_type()
    changed = []
    for name in fields:
        actual = getattr(config, name)
        expected = getattr(baseline, name)
        if name in allowed:
            if type(actual) is not str:
                changed.append(name)
            continue
        if type(actual) is not type(expected) or actual != expected:
            changed.append(name)
    if changed or config.activation_epoch_ms != activation_epoch_ms:
        raise PortableIdentityError(f"online activation changed unexpected native config fields: {changed}")
    return config


class _BoundedR3Sink:
    def __init__(self):
        self.rows: deque[bytes] = deque(maxlen=MAX_RETAINED_ROWS)
        self.rows_seen = 0
        self.total_bytes = 0
        self.retained_bytes = 0
        self.digest = hashlib.sha256()

    def __call__(self, payload: bytes) -> None:
        if type(payload) is not bytes or len(payload) > 32 * 1024 * 1024:
            raise PortableIdentityError("R3 diagnostic record must be exact bytes within the bounded retention cap")
        if len(self.rows) == MAX_RETAINED_ROWS:
            self.retained_bytes -= len(self.rows.popleft())
        while self.rows and self.retained_bytes + len(payload) > 32 * 1024 * 1024:
            self.retained_bytes -= len(self.rows.popleft())
        self.rows.append(payload)
        self.rows_seen += 1
        self.total_bytes += len(payload)
        self.retained_bytes += len(payload)
        self.digest.update(payload)

    def summary(self) -> dict[str, Any]:
        return {
            "retained_rows": len(self.rows),
            "rows_seen": self.rows_seen,
            "retention_policy": "last_4096_diagnostic_rows_with_32mib_cap",
            "total_bytes": self.total_bytes,
            "retained_bytes": self.retained_bytes,
            "sha256": self.digest.hexdigest(),
        }


def build_control_book(
    *,
    activation_epoch_ms: int,
    boundary: Any,
    stream_harness_type: type,
    run_identity: str,
) -> dict[str, Any]:
    """Build the fixed native control book at the first accepted online event.

    Feed admission, five-clock validation, and wall-clock scheduling stay in
    the caller.  This builder never constructs a source reader or ReplayHarness.
    """
    if type(run_identity) is not str or not run_identity.startswith("bot30_cent_os_shadow_control_"):
        raise ValueError("run_identity must use the fixed portable-control prefix")
    runtime = _load_runtime()
    _assert_runtime_ownership(runtime)
    h2 = runtime["h2"]
    config = _online_config(h2.ReplayConfig, activation_epoch_ms)
    if type(boundary) is not runtime["boundary_module"].Boundary:
        raise PortableIdentityError("boundary must use the exact pinned native Boundary type")
    if (type(boundary.removed_ms) is not int or type(boundary.loaded_ms) is not int
            or type(boundary.ready_ms) is not int
            or (boundary.removed_ms, boundary.loaded_ms, boundary.ready_ms)
            != (activation_epoch_ms, activation_epoch_ms, activation_epoch_ms)):
        raise PortableIdentityError("online L_RESET boundary must be fixed at the first accepted event")
    run_main = runtime["run_main"]
    sink = run_main.StreamingJsonlSink(None)
    if (sink.output_dir is not None or type(sink.current_fills) is not list
            or not callable(getattr(sink, "begin_row", None))):
        raise PortableIdentityError("pinned in-memory native sink schema mismatch")
    trade_writer = stream_harness_type.BoundedTradeWriter()
    ledger = run_main.ModelLedger(trade_writer)
    factory_trace = runtime["priority_factory"].build_hbe_p2_priority_engine(
        sink, wrapper=runtime["wrapper"], config=config, trace_limit=FACTORY_TRACE_LIMIT,
    )
    if (factory_trace.sink is not sink or factory_trace.config is not config
            or factory_trace.research_variant_id != ENGINE_VARIANT):
        raise PortableIdentityError("fixed priority factory did not retain exact engine/sink/config identity")
    adapter_trace: deque[Any] = deque(maxlen=ADAPTER_TRACE_LIMIT)
    adapter = runtime["wrapper"].build_adapter(
        factory_trace, boundary=boundary, lifecycle_mode=LIFECYCLE_MODE,
        trace=adapter_trace,
    )
    if (adapter.engine is not factory_trace or adapter.trace is not adapter_trace
            or adapter.boundary is not boundary or adapter.mode != LIFECYCLE_MODE
            or getattr(factory_trace, "_closeby_composition", None) is not None):
        raise PortableIdentityError("control adapter/native recipe identity or no-CloseBy boundary mismatch")
    r3_sink = _BoundedR3Sink()
    memory = runtime["r3_adapter"].R3Memory(
        runtime["quantity_rules"], f"{PORTABLE_ID}/{CONTROL_VARIANT}/{run_identity}",
        activation_epoch_ms, r3_sink,
    )
    memory.install_first_p2_wrapper(factory_trace)
    state_modules = tuple(
        module.__name__
        for module in (
            runtime["run_main"], runtime["h2"], runtime["native_adapter"],
            runtime["boundary_module"], runtime["wrapper"], runtime["priority_factory"],
            runtime["hbe_baseline"], runtime["count_component"], runtime["r3_adapter"],
            runtime["quantity_rules"], runtime["quote_validator"], stream_harness_type,
        )
        if isinstance(module, ModuleType)
    )
    harness = stream_harness_type.StreamHarness(
        run_main=run_main,
        engine=factory_trace,
        adapter=adapter,
        boundary=boundary,
        event_sink=sink,
        ledger=ledger,
        trade_writer=trade_writer,
        trace_buffer=adapter_trace,
        r3_memory=memory,
        clock_formatter=h2.format_assumed_utc_ms,
        allowed_state_modules=state_modules,
    )
    memory.wrap_harness(harness)
    if (harness.engine is not factory_trace or harness.adapter is not adapter
            or harness.event_sink is not sink or harness.ledger is not ledger
            or harness.trace_buffer is not adapter_trace or harness.r3_memory is not memory
            or memory.record_sink is not r3_sink):
        raise PortableIdentityError("control harness/R3/native object identity mismatch")
    return {
        "portable_id": PORTABLE_ID,
        "status": PORTABLE_STATUS,
        "variant_id": CONTROL_VARIANT,
        "native_variant": NATIVE_VARIANT,
        "engine_variant": ENGINE_VARIANT,
        "h2_p2_first_installed": False,
        "market_authority": False,
        "run_identity": run_identity,
        "activation_epoch_ms": activation_epoch_ms,
        "boundary": boundary,
        "runtime": runtime,
        "harness": harness,
        "engine": factory_trace,
        "adapter": adapter,
        "sink": sink,
        "trade_writer": trade_writer,
        "r3_memory": memory,
        "r3_sink": r3_sink,
        "adapter_trace": adapter_trace,
        "closed": False,
    }


def close_control_book(book: dict[str, Any]) -> None:
    if type(book) is not dict or book.get("closed") is True:
        return
    book["closed"] = True
    book["harness"].close()


def verify_portable_identity() -> dict[str, Any]:
    runtime = _load_runtime()
    _assert_runtime_ownership(runtime)
    return {
        "portable_id": PORTABLE_ID,
        "status": "PORTABLE_SOURCE_IDENTITY_VERIFIED_NO_BOOK",
        "control_variant": CONTROL_VARIANT,
        "native_variant": NATIVE_VARIANT,
        "engine_variant": ENGINE_VARIANT,
        "lifecycle_mode": LIFECYCLE_MODE,
        "h2_p2_first_installed": False,
        "market_authority": False,
        "binding_root": ".",
        "files": {
            label: {"path": row["path"], "sha256": row["sha256"], "bytes": row["bytes"]}
            for label, row in runtime["entries"].items()
        },
        "clock_contract_schema": runtime["clock_contract"].get("schema"),
    }
