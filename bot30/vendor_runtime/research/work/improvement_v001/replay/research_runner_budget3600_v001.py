"""One-pass, three-book comparison bridge for already-normalized QuoteEvents.

This module has no source-file reader or market CLI. A caller may supply one
ordered iterator only after its fixed-copy receipt and parent launch GO pass.
Synthetic fixtures use a separate explicit fixture-only entry path.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable


WORKSPACE = Path(__file__).resolve().parents[3]
CAMPAIGN = Path(r"C:\botter\backtest\output\backtest_30")
CODE = CAMPAIGN / "code"
CORE_DIR = CODE / "native_v006"
REPLAY_ROOT = WORKSPACE / "outputs" / "improvement_v001" / "replay"
RUN_MAIN = WORKSPACE / "work" / "discovery_v005" / "run_main_budget3600_v001.py"
RUN_MAIN_SHA256 = "368ae857fb9966e5048aefa43ac407be71442341966cd70e455091a99cb60d4c"
RESEARCH_WRAPPER_SHA256 = "16c0c2b368c9864f7c06338c6db02d6f10368557dc177193d8e2a557d374b475"
SPEC_SHA256 = "5b66597d5692413bf58cf79b0f694fd72af6f404082b8ec26299336d8183fb43"
SPEC_PATH = WORKSPACE / "outputs" / "improvement_v001" / "design" / "strategy_comparison_spec_v001.json"
CONFIG_PATH = CAMPAIGN / "candidate-config" / "variants_v001.json"
MAX_OUTPUT_BYTES = 128 * 1024 * 1024
FAILURE_RESERVE_BYTES = 64 * 1024
MAX_RUNTIME_SECONDS = 600
MAX_RSS_BYTES = 1024 * 1024 * 1024
OBSERVE_SECONDS = 5
EVENT_KINDS = ("order", "fill", "order_event", "state", "branch")
CURVE_FIELDS = (
    "point_kind", "epoch_ms_assumed_utc", "source_row_sequence", "raw_time",
    "realized_gross_usd", "realized_fee_usd", "realized_net_usd",
    "unrealized_gross_usd", "open_fee_reserve_usd", "net_equity_usd",
    "open_count", "pending_count",
)
VARIANT_DIRS = {
    "B0_NATIVE": "B0_NATIVE",
    "E1_P1_RANGE_AND": "E1_P1_RANGE_AND",
    "X1_INITIAL_DECISION_REF_BE_TRAIL": "X1_INITIAL_DECISION_REF_BE_TRAIL",
}
_LOADED_RUNTIME: tuple[Any, Any, Any, Any, dict[str, Any]] | None = None
_LOADED_WRAPPER: Any | None = None


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_verified_source_module(name: str, path: Path, expected_sha256: str) -> Any:
    """Execute exactly the source bytes whose digest was checked.

    Timestamp/size based bytecode caches are deliberately bypassed. The marker
    is also required when a module is already cached, so a caller cannot seed
    this process with a module that merely points at the pinned path.
    """
    resolved = path.resolve(strict=True)
    source_bytes = resolved.read_bytes()
    actual_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if actual_sha256 != expected_sha256:
        raise RuntimeError(f"verified Python source hash mismatch: {resolved}")

    cached = sys.modules.get(name)
    if cached is not None:
        cached_path = Path(getattr(cached, "__file__", "")).resolve(strict=True)
        if os.path.normcase(str(cached_path)) != os.path.normcase(str(resolved)):
            raise RuntimeError(f"cached Python module path mismatch: {name}")
        if getattr(cached, "__verified_source_sha256__", None) != actual_sha256:
            raise RuntimeError(f"cached Python module execution bytes are unverified: {name}")
        return cached

    spec = importlib.util.spec_from_file_location(name, resolved)
    if spec is None:
        raise RuntimeError(f"cannot create module spec: {resolved}")
    module = importlib.util.module_from_spec(spec)
    module.__verified_source_sha256__ = actual_sha256
    sys.modules[name] = module
    try:
        code = compile(source_bytes, str(resolved), "exec", dont_inherit=True)
        exec(code, module.__dict__)
        # Source code may not overwrite the verifier's execution marker.
        module.__verified_source_sha256__ = actual_sha256
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


class OutputBudget:
    """Share output and wall-clock accounting across source and replay stages."""

    def __init__(self, max_bytes: int = MAX_OUTPUT_BYTES, *, started_at: float | None = None):
        self.max_bytes = max_bytes
        self.written_bytes = 0
        self.started_at = time.monotonic() if started_at is None else started_at
        self._next_observation = self.started_at

    def check(self, run_main: Any) -> None:
        now = time.monotonic()
        if now - self.started_at >= MAX_RUNTIME_SECONDS:
            raise RuntimeError("aggregate replay runtime limit exceeded")
        if now >= self._next_observation:
            rss = run_main._process_rss_bytes()
            if rss > MAX_RSS_BYTES:
                raise RuntimeError("aggregate replay RSS limit exceeded")
            self._next_observation = now + OBSERVE_SECONDS

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)

    def write(self, stream: Any, payload: bytes) -> None:
        if self.written_bytes + len(payload) > self.max_bytes - FAILURE_RESERVE_BYTES:
            raise RuntimeError(f"aggregate comparison output limit exceeded: {self.max_bytes} bytes")
        stream.write(payload)
        self.written_bytes += len(payload)

    def write_failure(self, stream: Any, payload: bytes) -> None:
        """Use the reserved tail of the shared output budget for one failure receipt."""
        if self.written_bytes + len(payload) > self.max_bytes:
            raise RuntimeError(f"aggregate failure receipt exceeds output limit: {self.max_bytes} bytes")
        stream.write(payload)
        self.written_bytes += len(payload)

    def write_text(self, stream: Any, text: str) -> None:
        self.write(stream, text.encode("utf-8"))


class BoundedTextWriter:
    def __init__(self, stream: Any, budget: OutputBudget):
        self.stream = stream
        self.budget = budget

    def write(self, text: str) -> int:
        self.budget.write(self.stream, text.encode("utf-8"))
        return len(text)

    def flush(self) -> None:
        self.stream.flush()


def _load_runner_and_core() -> tuple[Any, Any, Any, Any, dict[str, Any]]:
    global _LOADED_RUNTIME
    if _LOADED_RUNTIME is not None:
        return _LOADED_RUNTIME
    names = ("h2_engine", "lifecycle_market_adapter", "a3_lifecycle_boundary")
    cached = [name for name in names if name in sys.modules]
    if cached:
        raise RuntimeError(f"unexpected preloaded native modules; refusing to replace caller state: {cached}")
    module = _load_verified_source_module(
        "bot30_v006_replay_accounting", RUN_MAIN, RUN_MAIN_SHA256
    )
    module.VENDOR_PATH = CORE_DIR
    module._VERIFIED_VENDOR_MODULES = None
    wrapper = _load_research_wrapper()
    core = wrapper.load_native_core()
    loaded: dict[str, dict[str, str]] = {}
    module_names = {
        "h2_engine.py": "h2_engine",
        "lifecycle_market_adapter.py": "lifecycle_market_adapter",
        "a3_lifecycle_boundary.py": "a3_lifecycle_boundary",
    }
    for filename, module_name in module_names.items():
        native_module = core[module_name]
        expected_hash = module.EXPECTED_VENDOR_HASHES[filename]
        native_path = Path(native_module.__file__).resolve(strict=True)
        if getattr(native_module, "__verified_source_sha256__", None) != expected_hash:
            raise RuntimeError(f"native module lacks matching verified-byte marker: {filename}")
        if os.path.normcase(str(native_path)) != os.path.normcase(str(CORE_DIR / filename)):
            raise RuntimeError(f"native module path differs from frozen core: {filename}")
        if _sha256(native_path) != expected_hash:
            raise RuntimeError(f"native module source changed after verified load: {filename}")
        loaded[filename] = {"loaded_path": str(native_path), "sha256": expected_hash}
    # run_main's loader accepts only its complete pinned cache. Seed that cache
    # from the wrapper's already byte-verified modules; it must not import by
    # timestamp/size bytecode after this point.
    module._VERIFIED_VENDOR_MODULES = (
        core["h2_engine"], core["lifecycle_market_adapter"],
        core["a3_lifecycle_boundary"], loaded,
    )
    h2, adapter, boundary, loaded = module._load_vendor_modules()
    if {name: row["sha256"] for name, row in loaded.items()} != module.EXPECTED_VENDOR_HASHES:
        raise RuntimeError("native core pins did not match the accounting runner")
    for native_module, filename in zip((h2, adapter, boundary), module.EXPECTED_VENDOR_HASHES):
        if getattr(native_module, "__verified_source_sha256__", None) != module.EXPECTED_VENDOR_HASHES[filename]:
            raise RuntimeError(f"accounting runner returned an unverified native module: {filename}")
    _LOADED_RUNTIME = (module, h2, adapter, boundary, loaded)
    return _LOADED_RUNTIME


def _load_research_wrapper() -> Any:
    global _LOADED_WRAPPER
    if _LOADED_WRAPPER is not None:
        return _LOADED_WRAPPER
    wrapper_path = CODE / "research_entry_close.py"
    _LOADED_WRAPPER = _load_verified_source_module(
        "bot30_research_entry_close_v001", wrapper_path, RESEARCH_WRAPPER_SHA256
    )
    return _LOADED_WRAPPER


def make_quote_event(**fields: Any) -> Any:
    """Construct QuoteEvent only from the pinned engine; no file access."""
    _, h2, _, _, _ = _load_runner_and_core()
    return h2.QuoteEvent(**fields)


def _price_fields(sample: dict[str, Any], *, point_kind: str, epoch: int, source_sequence: int, raw_time: str) -> dict[str, Any]:
    usd = _LOADED_RUNTIME[0]._usd
    return {
        "point_kind": point_kind,
        "epoch_ms_assumed_utc": epoch,
        "source_row_sequence": source_sequence,
        "raw_time": raw_time,
        "realized_gross_usd": sample["realized_gross_usd"],
        "realized_fee_usd": sample["realized_fee_usd"],
        "realized_net_usd": sample["realized_net_usd"],
        "unrealized_gross_usd": usd(sample["_unrealized_gross_micro"]),
        "open_fee_reserve_usd": usd(sample["_open_fee_reserve_micro"]),
        "net_equity_usd": sample["net_equity_usd"],
        "open_count": sample["open_count"],
        "pending_count": sample["pending_count"],
    }


def _snapshot_curve(snapshot: dict[str, Any]) -> dict[str, Any]:
    usd = _LOADED_RUNTIME[0]._usd
    account = snapshot["account"]
    exact = snapshot["account_exact_micro"]
    return {
        "point_kind": "common_cut_C_inclusive",
        "epoch_ms_assumed_utc": snapshot["cutoff_epoch_ms_inclusive"],
        "source_row_sequence": snapshot["quote_source_row_sequence_included"],
        "raw_time": snapshot["quote_raw_time_included"],
        "realized_gross_usd": usd(exact["realized_gross_micro"]),
        "realized_fee_usd": usd(exact["realized_fee_micro"]),
        "realized_net_usd": account["realized_net_usd"],
        "unrealized_gross_usd": usd(exact["unrealized_gross_micro"]),
        "open_fee_reserve_usd": usd(exact["open_fee_reserve_micro"]),
        "net_equity_usd": account["net_equity_usd"],
        "open_count": account["open_count"],
        "pending_count": account["pending_count"],
    }


def bundle_identity() -> dict[str, Any]:
    """Return code/config identities for parent GO and independent audit."""
    return {
        "comparison_spec_sha256": _sha256(SPEC_PATH),
        "candidate_config_sha256": _sha256(CONFIG_PATH),
        "accounting_runner_sha256": _sha256(RUN_MAIN),
        "research_wrapper_sha256": _sha256(CODE / "research_entry_close.py"),
        "research_runner_sha256": _sha256(Path(__file__).resolve()),
        "native_core_sha256": {
            name: _sha256(CORE_DIR / name)
            for name in ("h2_engine.py", "lifecycle_market_adapter.py", "a3_lifecycle_boundary.py")
        },
    }


def _validate_market_gate(launch_go: dict[str, Any], source_receipt: dict[str, Any], spec_sha256: str) -> None:
    if spec_sha256 != SPEC_SHA256:
        raise RuntimeError("comparison spec SHA does not match the frozen spec")
    if launch_go.get("status") != "GO" or launch_go.get("review_status") != "GO":
        raise RuntimeError("market replay requires explicit parent and independent-review GO")
    if launch_go.get("audit_status") != "GO":
        raise RuntimeError("market replay requires the independent artifact-only audit GO")
    if launch_go.get("spec_sha256") != SPEC_SHA256:
        raise RuntimeError("launch GO is not bound to the frozen comparison spec")
    if source_receipt.get("status") != "PASS_FIXED_COPY_COVERAGE":
        raise RuntimeError("market replay requires the fixed-copy coverage receipt")
    if launch_go.get("source_receipt_sha256") != source_receipt.get("receipt_sha256"):
        raise RuntimeError("launch GO source receipt identity mismatch")
    if not source_receipt.get("fixed_copy_sha256") or not launch_go.get("launch_id"):
        raise RuntimeError("launch GO/source receipt identity fields are incomplete")
    if launch_go.get("code_identity") != bundle_identity():
        raise RuntimeError("launch GO code/config identity differs from current verified bundle")


def _prepare_output(target: Path, *, fixture_only: bool) -> None:
    resolved = target.resolve()
    if fixture_only:
        root = REPLAY_ROOT.resolve()
    else:
        root = (CAMPAIGN / "runs").resolve()
    if os.path.normcase(str(resolved.parent)) != os.path.normcase(str(root)):
        raise ValueError("run output must be a new direct child of the assigned output root")
    target.mkdir(parents=True, exist_ok=False)


def _create_variant_runs(output_dir: Path, budget: OutputBudget, run_main: Any, h2: Any, wrapper: Any, cutoff: int) -> dict[str, dict[str, Any]]:
    variant_runs: dict[str, dict[str, Any]] = {}
    try:
        for variant_id, dirname in VARIANT_DIRS.items():
            variant_dir = output_dir / dirname
            variant_dir.mkdir()
            event_streams: dict[str, Any] = {}
            opened: list[Any] = []
            harness = None
            try:
                for kind in EVENT_KINDS:
                    stream = (variant_dir / f"{kind}.jsonl").open("xb")
                    opened.append(stream)
                    event_streams[kind] = stream
                group_raw = (variant_dir / "groups.jsonl").open("xb")
                opened.append(group_raw)
                group_stream = BoundedTextWriter(group_raw, budget)
                curve_raw = (variant_dir / "equity_curve.csv").open("xb")
                opened.append(curve_raw)
                curve_stream = BoundedTextWriter(curve_raw, budget)
                csv_writer = csv.DictWriter(curve_stream, fieldnames=CURVE_FIELDS, lineterminator="\n")
                csv_writer.writeheader()
                harness = run_main.ReplayHarness(output_dir=None, common_cutoff_epoch_ms=cutoff)
                harness.engine = wrapper.build_engine(harness.event_sink, variant_id=variant_id, config=h2.ReplayConfig())
                harness.adapter = wrapper.build_adapter(
                    harness.engine,
                    boundary=harness.boundary,
                    lifecycle_mode="L_RESET",
                    trace=harness.trace_buffer,
                )
                harness.ledger.trade_writer = group_stream
                original_emit = harness.event_sink.emit

                def emit(kind: str, row: dict[str, Any], *, _orig=original_emit, _streams=event_streams) -> None:
                    _orig(kind, row)
                    if kind in _streams:
                        budget.write(_streams[kind], _json_bytes(row))

                harness.event_sink.emit = emit
                variant_runs[variant_id] = {
                    "harness": harness,
                    "variant_dir": variant_dir,
                    "event_streams": event_streams,
                    "group_raw": group_raw,
                    "curve_raw": curve_raw,
                    "curve_writer": csv_writer,
                    "last_minute": None,
                    "last_minute_sample": None,
                    "last_minute_tick": None,
                    "cut_written": False,
                }
            except BaseException:
                if harness is not None:
                    try:
                        harness.close()
                    except Exception:
                        pass
                for stream in opened:
                    if not stream.closed:
                        stream.close()
                raise
    except BaseException:
        # A later book's constructor failure must not leak streams from earlier
        # books; the outer runner then preserves its failure receipt.
        for state in variant_runs.values():
            for stream in state["event_streams"].values():
                if not stream.closed:
                    stream.close()
            for key in ("curve_raw", "group_raw"):
                stream = state[key]
                if not stream.closed:
                    stream.close()
            state["harness"].close()
        raise
    return variant_runs


def _run_books(
    quotes: Iterable[Any],
    *,
    output_dir: Path,
    common_cutoff_epoch_ms: int,
    run_identity: str,
    source_identity: dict[str, Any],
    fixture_only: bool,
    source_receipt: dict[str, Any] | None,
    budget: OutputBudget | None = None,
) -> dict[str, Any]:
    budget = budget if budget is not None else OutputBudget()
    run_main, h2, _, _, loaded = _load_runner_and_core()
    budget.check(run_main)
    wrapper = _load_research_wrapper()
    if wrapper.load_native_core()["h2_engine"] is not h2:
        raise RuntimeError("research wrapper and accounting runner loaded different engine modules")
    _prepare_output(output_dir, fixture_only=fixture_only)
    variant_runs: dict[str, dict[str, Any]] = {}

    rows = 0
    last_sequence: int | None = None
    last_epoch: int | None = None
    stream_hash = hashlib.sha256()
    try:
        variant_runs = _create_variant_runs(output_dir, budget, run_main, h2, wrapper, common_cutoff_epoch_ms)
        for quote in quotes:
            budget.check(run_main)
            if not isinstance(quote, h2.QuoteEvent):
                raise TypeError("all books must receive the pinned native QuoteEvent type")
            if last_sequence is not None and quote.source_row_sequence <= last_sequence:
                raise ValueError("source row sequence must increase; runner never sorts")
            if last_epoch is not None and quote.assumed_utc_epoch_ms < last_epoch:
                raise ValueError("assumed UTC timestamps must be nondecreasing")
            row_bytes = _json_bytes({
                "source_row_sequence": quote.source_row_sequence,
                "assumed_utc_epoch_ms": quote.assumed_utc_epoch_ms,
                "bid_micro": quote.bid_micro,
                "ask_micro": quote.ask_micro,
                "flags": quote.flags,
                "carried_sides": list(quote.carried_sides),
            })
            stream_hash.update(row_bytes)
            minute = quote.assumed_utc_epoch_ms // 60_000
            for variant_id, state in variant_runs.items():
                harness = state["harness"]
                previous_minute = state["last_minute"]
                if previous_minute is not None and minute != previous_minute:
                    prior_tick = state["last_minute_tick"]
                    prior_sample = state["last_minute_sample"]
                    state["curve_writer"].writerow(_price_fields(
                        prior_sample, point_kind="minute_end", epoch=prior_tick.assumed_utc_epoch_ms,
                        source_sequence=prior_tick.source_row_sequence, raw_time=prior_tick.raw_time_label,
                    ))
                harness.consume(quote)
                state["last_minute"] = minute
                state["last_minute_sample"] = harness.last_sample
                state["last_minute_tick"] = quote
                if harness.cutoff_snapshot is not None and not state["cut_written"]:
                    # The harness snapshots before the first row after C; when
                    # the stream ends at/before C it is emitted after finish.
                    if quote.assumed_utc_epoch_ms > common_cutoff_epoch_ms:
                        state["curve_writer"].writerow(_snapshot_curve(harness.cutoff_snapshot))
                        state["cut_written"] = True
            rows += 1
            last_sequence = quote.source_row_sequence
            last_epoch = quote.assumed_utc_epoch_ms
        if rows == 0:
            raise ValueError("empty quote stream cannot be a comparison run")
        summaries: dict[str, Any] = {}
        for variant_id, state in variant_runs.items():
            harness = state["harness"]
            sample = state["last_minute_sample"]
            tick = state["last_minute_tick"]
            state["curve_writer"].writerow(_price_fields(
                sample, point_kind="minute_end", epoch=tick.assumed_utc_epoch_ms,
                source_sequence=tick.source_row_sequence, raw_time=tick.raw_time_label,
            ))
            summary = harness.finish({
                "run_identity": run_identity,
                "source_identity": source_identity,
                "fixed_copy_receipt": source_receipt,
                "fixture_only": fixture_only,
                "source_payload_read_by_runner": False,
            })
            if not state["cut_written"] and harness.cutoff_snapshot is not None:
                state["curve_writer"].writerow(_snapshot_curve(harness.cutoff_snapshot))
                state["cut_written"] = True
            state["curve_writer"].writerow(_price_fields(
                sample, point_kind="EOF", epoch=tick.assumed_utc_epoch_ms,
                source_sequence=tick.source_row_sequence, raw_time=tick.raw_time_label,
            ))
            summary["comparison_bridge"] = {
                "run_identity": run_identity,
                "variant_id": variant_id,
                "spec_sha256": SPEC_SHA256,
                "runner_sha256": RUN_MAIN_SHA256,
                "native_core_hashes": {key: row["sha256"] for key, row in loaded.items()},
                "same_single_input_pass": True,
                "quote_stream_sha256": stream_hash.hexdigest(),
                "curve_grid": "last quote per observed minute, inclusive common C, separate EOF",
                "all_tick_drawdown_from_native_integer_ledger": True,
                "curve_separates_realized_gross_fee_net_unrealized_and_open_fee_reserve": True,
                "no_old_first_of_minute_or_event_density_curve_used": True,
            }
            summary["resource"]["aggregate_elapsed_seconds"] = round(budget.elapsed_seconds, 3)
            summary["resource"]["aggregate_output_bytes"] = budget.written_bytes
            summaries[variant_id] = summary
            summary_file = (state["variant_dir"] / "summary.json").open("xb")
            budget.write(summary_file, _json_bytes(summary))
            summary_file.close()
        manifest = {
            "identity": run_identity,
            "status": "PASS_FIXTURE_ONLY" if fixture_only else "PASS_STREAM_COMPLETE",
            "spec_sha256": SPEC_SHA256,
            "runner_sha256": RUN_MAIN_SHA256,
            "source_identity": source_identity,
            "source_receipt": source_receipt,
            "source_payload_read_by_runner": False,
            "source_rows_processed_once": rows,
            "source_quote_stream_sha256": stream_hash.hexdigest(),
            "common_cutoff_epoch_ms_inclusive": common_cutoff_epoch_ms,
            "native_core_hashes": {key: row["sha256"] for key, row in loaded.items()},
            "variants": list(VARIANT_DIRS),
            "same_ordered_quotes_to_all_books": True,
            "retry": 0,
            "resources": {
                "elapsed_seconds": round(budget.elapsed_seconds, 3),
                "output_bytes": budget.written_bytes,
                "output_limit_bytes": MAX_OUTPUT_BYTES,
                "rss_limit_bytes": MAX_RSS_BYTES,
                "runtime_limit_seconds": MAX_RUNTIME_SECONDS,
                "worker_count": 1,
            },
            "variant_summaries": summaries,
        }
        manifest_file = (output_dir / "comparison_manifest.json").open("xb")
        budget.write(manifest_file, _json_bytes(manifest))
        manifest_file.close()
        for state in variant_runs.values():
            state["curve_raw"].flush()
            state["curve_raw"].close()
            state["group_raw"].flush()
            state["group_raw"].close()
            for stream in state["event_streams"].values():
                stream.flush()
                stream.close()
            state["harness"].close()
        return manifest
    except BaseException as exc:
        receipt = {
            "identity": run_identity,
            "status": "FAILED_PRESERVED_NO_RETRY",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "rows_processed_once": rows,
            "last_source_row_sequence": last_sequence,
            "last_epoch_ms_assumed_utc": last_epoch,
            "source_identity": source_identity,
            "source_receipt": source_receipt,
            "spec_sha256": SPEC_SHA256,
            "retry": 0,
            "output_bytes_before_failure": budget.written_bytes,
        }
        fail_path = output_dir / "failure.json"
        with fail_path.open("xb") as failure_stream:
            budget.write_failure(failure_stream, _json_bytes(receipt))
        for state in variant_runs.values():
            for stream in state["event_streams"].values():
                if not stream.closed:
                    stream.flush()
                    stream.close()
            for key in ("curve_raw", "group_raw"):
                stream = state[key]
                if not stream.closed:
                    stream.flush()
                    stream.close()
            try:
                state["harness"].close()
            except Exception:
                pass
        raise


def run_market_comparison(
    quotes: Iterable[Any],
    *,
    output_dir: str | Path,
    run_identity: str,
    source_identity: dict[str, Any],
    source_receipt: dict[str, Any],
    launch_go: dict[str, Any],
    common_cutoff_epoch_ms: int,
    budget: OutputBudget | None = None,
) -> dict[str, Any]:
    """Market-run entrypoint; authorization is checked before consuming quotes."""
    _validate_market_gate(launch_go, source_receipt, SPEC_SHA256)
    if type(common_cutoff_epoch_ms) is not int:
        raise TypeError("common C must be a frozen integer epoch-millisecond value")
    return _run_books(
        quotes,
        output_dir=Path(output_dir),
        common_cutoff_epoch_ms=common_cutoff_epoch_ms,
        run_identity=run_identity,
        source_identity=source_identity,
        fixture_only=False,
        source_receipt=source_receipt,
        budget=budget,
    )


def run_source_free_fixture(
    quotes: Iterable[Any],
    *,
    output_dir: str | Path,
    run_identity: str,
    common_cutoff_epoch_ms: int,
    budget: OutputBudget | None = None,
) -> dict[str, Any]:
    """Explicit synthetic-only path. It cannot be used with a market receipt."""
    if not run_identity.startswith("source_free_"):
        raise ValueError("fixture path requires a source_free_ run identity")
    return _run_books(
        quotes,
        output_dir=Path(output_dir),
        common_cutoff_epoch_ms=common_cutoff_epoch_ms,
        run_identity=run_identity,
        source_identity={"fixture_only": True, "market_source_read": False},
        fixture_only=True,
        source_receipt=None,
        budget=budget,
    )
