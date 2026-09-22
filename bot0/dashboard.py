"""Read-only bot0 dashboard with a strict bot23 ledger adapter."""
from __future__ import annotations

import ast
import base64
import binascii
import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

DEFAULT_HOST = os.environ.get("BOT0_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("BOT0_PORT", "8230"))
DEFAULT_AUTH_USER = "bot0"
AUTH_REALM = "bot0"
MAX_AUTH_HEADER_BYTES = 8192
MAX_AUTH_B64_BYTES = 4096
MAX_AUTH_DECODED_BYTES = 3072
BOT23_ROOT = Path(os.environ.get("BOT23_ROOT", "/data/bot23"))
BOT23_PARAMS = BOT23_ROOT / "s23_params.json"
BOT23_TRADES = BOT23_ROOT / "logs" / "s23_trades.csv"
BOT23_LOG = BOT23_ROOT / "logs" / "s23_bot.log"
BOT23_EVALUATION = Path(os.environ["BOT23_EVALUATION_PATH"]) if os.environ.get("BOT23_EVALUATION_PATH") else None
BOT23_METADATA = Path(os.environ["BOT23_METADATA_PATH"]) if os.environ.get("BOT23_METADATA_PATH") else None
COLLECTOR_TTL_SECONDS = max(0.1, float(os.environ.get("BOT0_COLLECTOR_TTL_SECONDS", "30")))
MAX_BODY_BYTES = 1_000_000
DEAL_TIME_RE = re.compile(r"(?:^|;)deal_time_utc=([^;\s]+)")
KEY_VALUE_RE = re.compile(r"(?:^|;)([A-Za-z][A-Za-z0-9_]*)=([^;]*)")
EXECUTION_CLASSES = ("live", "shadow", "unknown")


@dataclass(frozen=True)
class AuthConfig:
    username: str
    credential_digest: bytes


AUTH_CONFIG: AuthConfig | None = None


class DashboardError(Exception):
    """A source or request error safe to expose to a dashboard client."""


def load_auth_config() -> AuthConfig:
    """Load the required Basic auth identity from a Git-external JSON file."""
    auth_file_value = os.environ.get("BOT0_AUTH_FILE", "").strip()
    if not auth_file_value:
        raise RuntimeError("BOT0_AUTH_FILE is required")
    auth_path = Path(auth_file_value)
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid BOT0_AUTH_FILE") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("invalid BOT0_AUTH_FILE")
    username = payload.get("username")
    password = payload.get("password")
    expected_username = os.environ.get("BOT0_AUTH_USER", DEFAULT_AUTH_USER).strip()
    if not isinstance(username, str) or not username.strip() or username != expected_username:
        raise RuntimeError("invalid BOT0_AUTH_FILE username")
    if not isinstance(password, str) or not password.strip():
        raise RuntimeError("BOT0_AUTH_FILE password is required")
    credential_digest = hashlib.sha256(f"{username}:{password}".encode("utf-8")).digest()
    return AuthConfig(username=username, credential_digest=credential_digest)


def configure_auth() -> AuthConfig:
    """Load auth before the HTTP server is bound; failures remain fail-closed."""
    global AUTH_CONFIG
    AUTH_CONFIG = load_auth_config()
    return AUTH_CONFIG


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return None


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(path: Path) -> str | None:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        return None


def _stat_identity(stat_result: os.stat_result) -> dict[str, int]:
    return {"device": int(stat_result.st_dev), "inode": int(stat_result.st_ino), "size": int(stat_result.st_size), "mtime_ns": int(stat_result.st_mtime_ns)}


def _path_identity(path: Path) -> dict[str, int] | None:
    try:
        return _stat_identity(path.stat())
    except OSError:
        return None


def _read_stable_bytes(path: Path, attempts: int = 2) -> tuple[bytes, dict[str, Any]]:
    """Read a complete source while proving identity did not rotate mid-read."""
    for _ in range(attempts):
        try:
            path_before = _path_identity(path)
            if path_before is None:
                raise DashboardError(f"source_unavailable:{path.name}")
            with path.open("rb") as handle:
                before = _stat_identity(os.fstat(handle.fileno()))
                payload = handle.read()
                after = _stat_identity(os.fstat(handle.fileno()))
            path_after = _path_identity(path)
        except OSError as exc:
            raise DashboardError(f"source_unavailable:{path.name}") from exc
        if path_before == path_after and before == after == path_after:
            return payload, {**after, "sha256": _sha256_bytes(payload), "stable": True}
    raise DashboardError(f"source_changed_during_read:{path.name}")


def _read_json(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    payload, identity = _read_stable_bytes(path)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DashboardError(f"invalid_json:{path.name}") from exc
    if not isinstance(value, dict):
        raise DashboardError(f"invalid_object:{path.name}")
    return value, identity


def _config_generation(config: dict[str, Any]) -> dict[str, Any]:
    for key in ("config_generation", "params_generation", "generation"):
        value = config.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return {"status": "known", "value": str(value), "key": key}
    return {"status": "unknown", "value": None, "key": None}


GROUPS: tuple[tuple[str, str, str], ...] = (
    ("core", "strategies", "enabled"), ("morning", "morning_session_strategies", "morning_session_enabled"),
    ("midday", "midday_session_strategies", "midday_session_enabled"), ("pre_eu30", "pre_eu30_session_strategies", "pre_eu30_session_enabled"),
    ("trend_recovery", "trend_recovery_strategies", "trend_recovery_enabled"), ("ny0530", "t0530_edge_strategies", "t0530_edge_enabled"),
    ("q01", "q01_variance_release_strategies", "q01_variance_release_enabled"), ("m15", "m15_terminal_strategies", "m15_terminal_enabled"),
    ("h7", "h7_strategies", "h7_enabled"), ("research", "research_entry_strategies", "research_entries_enabled"),
)


def _gate(value: Any) -> dict[str, Any]:
    parsed = _parse_bool(value)
    return {"value": parsed, "status": "enabled" if parsed is True else "blocked" if parsed is False else "unknown"}


def _tradable_gate(config: dict[str, Any]) -> dict[str, Any]:
    configured = config.get("configured_live_enabled", config.get("live_trading_enabled"))
    values = {"enabled": _parse_bool(config.get("enabled")), "configured_live_enabled": _parse_bool(configured)}
    status = "enabled" if all(value is True for value in values.values()) else "blocked" if any(value is False for value in values.values()) else "unknown"
    return {"status": status, "tradable_now": status == "enabled", "gates": {key: _gate(value) for key, value in values.items()}}


def _iter_strategies(config: dict[str, Any]) -> Iterable[tuple[str, bool, dict[str, Any]]]:
    gate = _tradable_gate(config)
    seen: set[str] = set()
    for group, field, gate_name in GROUPS:
        items = config.get(field, [])
        if not isinstance(items, list):
            continue
        group_gate = _parse_bool(config.get(gate_name))
        for raw in items:
            if not isinstance(raw, dict):
                continue
            ident = str(raw.get("id", "")).strip()
            if not ident or ident in seen:
                continue
            seen.add(ident)
            item_enabled = _parse_bool(raw.get("enabled"))
            effective = gate["tradable_now"] and group_gate is True and item_enabled is True
            yield group, effective, {**raw, "group": group, "group_enabled": group_gate, "effective_enabled": effective, "tradable_now": effective, "tradable_gate": gate}


def _safe_strategy(raw: dict[str, Any]) -> dict[str, Any]:
    allowed = ("id", "group", "lane_id", "signal_id", "spec_id", "magic", "comment_prefix", "enabled", "group_enabled", "effective_enabled", "tradable_now", "tradable_gate", "hold_minutes", "max_positions", "lot")
    return {key: raw.get(key) for key in allowed if key in raw}


def _note_values(note: str) -> dict[str, str]:
    return {key: value.strip() for key, value in KEY_VALUE_RE.findall(note or "")}


def _signal_fields(row: dict[str, str]) -> tuple[str, str]:
    """Parse signal identity from columns, semicolon key-values, or JSON note."""
    note = row.get("note", "") or ""
    values = _note_values(note)
    signal = next((str(row.get(key, "")).strip() for key in ("signal_id", "configured_signal_id", "signal") if str(row.get(key, "")).strip()), "")
    if not signal:
        signal = next((values.get(key, "") for key in ("signal_id", "configured_signal_id", "signal") if values.get(key, "")), "")
    variant = next((str(row.get(key, "")).strip() for key in ("signal_variant_id", "variant", "spec_id") if str(row.get(key, "")).strip()), "")
    if not variant:
        variant = next((values.get(key, "") for key in ("signal_variant_id", "variant", "spec_id") if values.get(key, "")), "")
    if not signal and note.lstrip().startswith("{"):
        try:
            parsed = json.loads(note)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            signal = str(parsed.get("signal_id") or parsed.get("signal") or "").strip()
            variant = variant or str(parsed.get("signal_variant_id") or parsed.get("variant") or "").strip()
    return signal, variant


def _close_time(row: dict[str, str]) -> tuple[datetime | None, datetime | None]:
    recorded = _parse_utc(row.get("timestamp_utc"))
    direct = _parse_utc(row.get("deal_time_utc"))
    broker_time = direct or _parse_utc(_note_values(row.get("note", "")).get("deal_time_utc"))
    return broker_time, recorded


@dataclass(frozen=True)
class CloseRow:
    deal_id: str
    strategy_id: str
    signal_id: str
    signal_variant_id: str
    lane_id: str
    magic: str
    basket_id: str
    ticket: str
    position_identifier: str
    opportunity_id: str
    opportunity_attribution: str
    ledger_profit: float | None
    profit: float | None
    profit_unit: str | None
    currency: str | None
    execution_class: str
    close_time: datetime
    recorded_time: datetime | None


@dataclass(frozen=True)
class TradeAudit:
    closes: tuple[CloseRow, ...]
    entries: tuple[dict[str, str], ...]
    source_rows: int
    duplicate_deals: int
    conflicting_deals: int
    quarantined_rows: int
    quarantine_reasons: tuple[str, ...]
    direct_opportunity_closes: int
    unique_entry_join_closes: int
    ambiguous_opportunity_joins: int
    last_event: datetime | None
    last_close: datetime | None


@dataclass(frozen=True)
class SourceSnapshot:
    config: dict[str, Any]
    audit: TradeAudit
    source_metadata: dict[str, Any]
    collected_at: float
    attempted_at: float
    collect_count: int
    rotation_count: int
    status: str
    last_error: str | None


def _csv_audit(payload: bytes) -> TradeAudit:
    try:
        if payload and not payload.endswith(b"\n"):
            raise DashboardError("invalid_trade_tail")
        text = payload.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        if not reader.fieldnames or None in reader.fieldnames or "event" not in reader.fieldnames:
            raise DashboardError("invalid_trade_header")
        if any(not isinstance(name, str) or not name.strip() for name in reader.fieldnames):
            raise DashboardError("invalid_trade_header_empty")
        if len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise DashboardError("invalid_trade_header_duplicate")
        rows: list[dict[str, str]] = []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise DashboardError("invalid_trade_row_width")
            rows.append({key: value or "" for key, value in row.items()})
    except UnicodeDecodeError as exc:
        raise DashboardError("invalid_trade_encoding") from exc
    except csv.Error as exc:
        raise DashboardError("invalid_trade_csv") from exc

    entries = [row for row in rows if row.get("event") in {"entry", "position_open_confirmed", "position_open"} and row.get("opportunity_id")]
    # A basket can contain multiple entries.  It is evidence for display only,
    # never a unique join key.  Position identifier/ticket are the only join
    # keys that can prove one close belongs to one entry.
    indexes: dict[str, dict[str, list[dict[str, str]]]] = {key: {} for key in ("position_identifier", "ticket")}
    for entry in entries:
        for key in indexes:
            value = entry.get(key, "").strip()
            if value:
                indexes[key].setdefault(value, []).append(entry)

    accepted: dict[str, CloseRow] = {}
    semantic_keys: dict[str, tuple[Any, ...]] = {}
    conflict_ids: set[str] = set()
    reasons: list[str] = []
    duplicate = conflicts = quarantined = 0
    last_event = None
    for row in rows:
        recorded = _parse_utc(row.get("timestamp_utc"))
        if recorded and (last_event is None or recorded > last_event):
            last_event = recorded
        if row.get("event") != "position_close_confirmed":
            continue
        deal_id = row.get("deal_id", "").strip()
        if not deal_id:
            quarantined += 1; reasons.append("missing_deal_id"); continue
        broker_time, recorded_time = _close_time(row)
        if broker_time is None:
            quarantined += 1; reasons.append("missing_broker_deal_time"); continue
        profit = _finite_float(row.get("profit"))
        if profit is None:
            quarantined += 1; reasons.append("missing_or_nonfinite_profit"); continue
        opportunity = row.get("opportunity_id", "").strip()
        attribution = "direct" if opportunity else "unresolved"
        if not opportunity:
            candidates: dict[str, dict[str, str]] = {}
            for key in ("position_identifier", "ticket"):
                value = row.get(key, "").strip()
                if value:
                    for entry in indexes[key].get(value, []):
                        candidates[entry.get("opportunity_id", "")] = entry
            if len(candidates) == 1:
                opportunity = next(iter(candidates)); attribution = "unique_entry_join"
            elif len(candidates) > 1:
                attribution = "ambiguous"; reasons.append("ambiguous_opportunity_join")
            else:
                reasons.append("missing_opportunity_id")
        else:
            attribution = "direct"
        signal, variant = _signal_fields(row)
        ledger_profit = _finite_float(row.get("ledger_profit"))
        if ledger_profit is None and profit is not None:
            ledger_profit = profit
        unit = (row.get("profit_unit") or row.get("ledger_profit_unit") or "").strip().upper() or None
        currency = (row.get("currency") or row.get("account_currency") or "").strip().upper() or None
        execution = (row.get("execution_class") or "").strip().lower()
        live = _parse_bool(row.get("live"))
        execution = execution if execution in EXECUTION_CLASSES[:2] else "live" if live is True else "shadow" if live is False else "unknown"
        close = CloseRow(deal_id, row.get("strategy_id", "").strip(), signal, variant, row.get("lane_id", "").strip(), row.get("magic", "").strip(), row.get("basket_id", "").strip(), row.get("ticket", "").strip(), row.get("position_identifier", "").strip(), opportunity, attribution, ledger_profit, profit, unit, currency, execution, broker_time, recorded_time)
        semantic = (close.deal_id, close.strategy_id, close.signal_id, close.signal_variant_id, close.lane_id, close.magic, close.basket_id, close.ticket, close.position_identifier, close.opportunity_id, close.opportunity_attribution, close.ledger_profit, close.profit, close.profit_unit, close.currency, close.execution_class, close.close_time)
        if deal_id in conflict_ids:
            quarantined += 1; reasons.append("conflicting_duplicate_deal"); continue
        prior = semantic_keys.get(deal_id)
        if prior is not None:
            if prior == semantic:
                duplicate += 1
            else:
                conflicts += 1; conflict_ids.add(deal_id); accepted.pop(deal_id, None); quarantined += 2; reasons.append("conflicting_duplicate_deal")
            continue
        semantic_keys[deal_id] = semantic; accepted[deal_id] = close
    accepted_closes = tuple(accepted.values())
    direct = sum(row.opportunity_attribution == "direct" for row in accepted_closes)
    joined = sum(row.opportunity_attribution == "unique_entry_join" for row in accepted_closes)
    ambiguous = sum(row.opportunity_attribution == "ambiguous" for row in accepted_closes)
    last_close = max((row.close_time for row in accepted_closes), default=None)
    return TradeAudit(tuple(accepted_closes), tuple(entries), len(rows), duplicate, conflicts, quarantined, tuple(reasons), direct, joined, ambiguous, last_event, last_close)


def _read_source_batch(paths: dict[str, Path]) -> tuple[dict[str, bytes], dict[str, dict[str, Any]]]:
    """Read all source paths under one pre/read/post identity barrier.

    Optional paths may be omitted from ``paths``.  A caller that includes a
    path requires it to exist; a path that is replaced or created while the
    batch is being read is rejected even when each individual read is valid.
    """
    before = {label: _path_identity(path) for label, path in paths.items()}
    payloads: dict[str, bytes] = {}
    identities: dict[str, dict[str, Any]] = {}
    for label, path in paths.items():
        payload, identity = _read_stable_bytes(path)
        payloads[label] = payload
        identities[label] = identity
    after = {label: _path_identity(path) for label, path in paths.items()}
    if before != after or any(before[label] != {key: identities[label][key] for key in before[label]} for label in paths):
        raise DashboardError("source_changed_during_read:cross_file")
    return payloads, identities


class SnapshotCollector:
    """Strict source collector with immutable last-good snapshot and rotation count."""
    def __init__(self, params: Path, trades: Path, log: Path, ttl_seconds: float = COLLECTOR_TTL_SECONDS, *, evaluation: Path | None = None, metadata: Path | None = None):
        self.params, self.trades, self.log, self.ttl_seconds = params, trades, log, ttl_seconds
        self.evaluation, self.metadata = evaluation, metadata
        self._lock = threading.RLock(); self._snapshot: SourceSnapshot | None = None; self._collect_count = 0; self._rotation_count = 0

    def get(self) -> SourceSnapshot:
        with self._lock:
            now = time.monotonic()
            if self._snapshot and now - self._snapshot.attempted_at < self.ttl_seconds:
                return self._snapshot
            attempted = time.monotonic()
            try:
                paths = {"params": self.params, "trades": self.trades}
                if self.evaluation is not None:
                    paths["evaluation"] = self.evaluation
                if self.metadata is not None:
                    paths["metadata"] = self.metadata
                payloads, source_identities = _read_source_batch(paths)
                try:
                    config = json.loads(payloads["params"].decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise DashboardError("invalid_json:s23_params.json") from exc
                if not isinstance(config, dict):
                    raise DashboardError("invalid_object:s23_params.json")
                audit = _csv_audit(payloads["trades"])
            except DashboardError as exc:
                if self._snapshot is None:
                    raise
                stale = SourceSnapshot(self._snapshot.config, self._snapshot.audit, self._snapshot.source_metadata, self._snapshot.collected_at, attempted, self._snapshot.collect_count, self._snapshot.rotation_count, "stale_last_good", str(exc))
                self._snapshot = stale
                return stale
            self._collect_count += 1
            current_identity = (source_identities["params"].get("sha256"), source_identities["trades"].get("sha256"))
            full_identity = {label: identity.get("sha256") for label, identity in source_identities.items()}
            prior_full_identity = self._snapshot.source_metadata.get("full_source_identity") if self._snapshot else None
            if prior_full_identity is not None and prior_full_identity != full_identity:
                self._rotation_count += 1
            source_meta = {label: {"path_label": path.name, **source_identities[label]} for label, path in paths.items()}
            metadata = {**source_meta, "source_identity": current_identity, "full_source_identity": full_identity, "path_identities": source_identities, "read_contract": "strict_batch_pre_read_post_path_identity", "rotation_coverage": {"source_identity_changes": self._rotation_count, "last_good_cache": True}}
            snapshot = SourceSnapshot(config, audit, metadata, attempted, attempted, self._collect_count, self._rotation_count, "fresh", None)
            self._snapshot = snapshot
            return snapshot


DEFAULT_COLLECTOR = SnapshotCollector(BOT23_PARAMS, BOT23_TRADES, BOT23_LOG, evaluation=BOT23_EVALUATION, metadata=BOT23_METADATA)


def _period_bounds(query: dict[str, list[str]]) -> tuple[datetime | None, datetime | None]:
    def one(key: str) -> datetime | None:
        values = query.get(key, [])
        if len(values) > 1 or (values and len(values[0]) > 32):
            raise DashboardError("invalid_period")
        return _parse_utc(values[0]) if values else None
    start, end = one("from"), one("to")
    if (query.get("from") and start is None) or (query.get("to") and end is None) or (start and end and start > end):
        raise DashboardError("invalid_period")
    return start, end


def _within(value: datetime, start: datetime | None, end: datetime | None) -> bool:
    return (start is None or value >= start) and (end is None or value < end)


def _currency(config: dict[str, Any]) -> str | None:
    value = config.get("account_currency") or config.get("currency")
    return str(value).strip().upper() if isinstance(value, str) and value.strip() else None


def _accounting_usable(rows: list[CloseRow], execution_class: str, currency: str | None) -> bool:
    """Return the single contract shared by monetary metrics and curves."""
    return (
        execution_class in {"live", "shadow"}
        and currency is not None
        and all(row.currency == currency and row.profit_unit in {currency, "ACCOUNT_CURRENCY"} for row in rows)
    )


def _metric(rows: list[CloseRow], scope: str, execution_class: str, currency: str | None) -> dict[str, Any]:
    wins = sum((row.profit or 0) > 0 for row in rows)
    confirmed = currency is not None and all(row.currency == currency and row.profit_unit in {currency, "ACCOUNT_CURRENCY"} for row in rows)
    gross_profit = sum((row.profit or 0) for row in rows if (row.profit or 0) > 0) if confirmed else None
    gross_loss = -sum((row.profit or 0) for row in rows if (row.profit or 0) < 0) if confirmed else None
    usable = _accounting_usable(rows, execution_class, currency)
    return {"scope": scope, "execution_class": execution_class, "deal_count": len(rows), "win_count": wins, "loss_count": len(rows) - wins, "win_rate": wins / len(rows) if rows else None, "profit_factor": gross_profit / gross_loss if usable and gross_loss else None, "realized_pnl": sum((row.profit or 0) for row in rows) if usable else None, "currency": currency if usable else None, "aggregation_status": "usable" if usable else "blocked_currency_or_profit_unit"}


def _equity(rows: list[CloseRow], scope: str, execution_class: str, currency: str | None) -> list[dict[str, Any]]:
    usable = _accounting_usable(rows, execution_class, currency)
    running = 0.0; curve: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: (item.close_time, item.deal_id)):
        running += row.profit or 0
        curve.append({"scope": scope, "execution_class": execution_class, "deal_id": row.deal_id, "close_time_utc": _iso(row.close_time), "cumulative_pnl": running if usable else None, "currency": currency if usable else None, "aggregation_status": "usable" if usable else "blocked_currency_or_profit_unit"})
    return curve


def _grouped_metrics(rows: list[CloseRow], field: str, currency: str | None) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[CloseRow]] = {}
    for row in rows:
        groups.setdefault((row.execution_class, getattr(row, field) or "unknown"), []).append(row)
    return [_metric(items, scope, execution, currency) for (execution, scope), items in sorted(groups.items())]


def _grouped_curve(rows: list[CloseRow], field: str, currency: str | None) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[CloseRow]] = {}
    for row in rows:
        groups.setdefault((row.execution_class, getattr(row, field) or "unknown"), []).append(row)
    return [point for (execution, scope), items in sorted(groups.items()) for point in _equity(items, scope, execution, currency)]


def _optional_source_views(source_meta: dict[str, Any], collector: SnapshotCollector) -> dict[str, dict[str, Any]]:
    """Expose configured optional identities without returning absolute paths."""
    views: dict[str, dict[str, Any]] = {}
    for label, path in (("evaluation", collector.evaluation), ("metadata", collector.metadata)):
        identity = source_meta.get(label)
        views[label] = {
            "configured": path is not None,
            "present": identity is not None,
            "path_alias": label,
            "path_label": identity.get("path_label") if identity else None,
            "file_identity": {key: value for key, value in (identity or {}).items() if key != "path_label"},
            "content_role": "identity_only; not_accounting_input",
        }
    return views


def build_summary(start: datetime | None = None, end: datetime | None = None, collector: SnapshotCollector = DEFAULT_COLLECTOR) -> dict[str, Any]:
    snapshot = collector.get(); config, audit = snapshot.config, snapshot.audit
    rows = [row for row in audit.closes if _within(row.close_time, start, end)]
    currency = _currency(config)
    by_execution = {execution: [row for row in rows if row.execution_class == execution] for execution in EXECUTION_CLASSES}
    gate = _tradable_gate(config); errors: list[str] = []
    if _config_generation(config)["status"] == "unknown": errors.append("config_generation_unknown")
    if currency is None: errors.append("currency_unknown_total_not_aggregated")
    if snapshot.status != "fresh": errors.append("source_refresh_failed_using_last_good_snapshot")
    errors.append("inventory_unknown")
    accounting = {"realized_pnl": None, "ledger_profit": sum((row.ledger_profit or 0) for row in rows) if rows else 0.0, "ledger_profit_unit_status": "unconfirmed", "closed_deal_count": len(rows), "currency": currency, "aggregation_status": "separate_by_execution_class", "duplicate_deal_rows": audit.duplicate_deals, "conflicting_deal_rows": audit.conflicting_deals, "quarantined_rows": audit.quarantined_rows, "quarantine_reasons": sorted(set(audit.quarantine_reasons)), "close_time_basis": "broker deal_time_utc only; recorded timestamp is never a period fallback", "opportunity_attribution": {"direct": audit.direct_opportunity_closes, "unique_entry_join": audit.unique_entry_join_closes, "ambiguous": audit.ambiguous_opportunity_joins}, "execution_classes": {key: _metric(value, key, key, currency) for key, value in by_execution.items()}}
    source_meta = snapshot.source_metadata
    return {"service": "bot0", "adapter": "bot23.v4", "generated_at_utc": _iso(datetime.now(timezone.utc)), "period": {"from_utc": _iso(start), "to_utc_exclusive": _iso(end)}, "config": {"bot": str(config.get("bot_number", "23")), "strategy_id": config.get("strategy_id"), "candidate_id": config.get("candidate_id"), "generation": _config_generation(config), "sha256": source_meta.get("params", {}).get("sha256"), "root_enabled": _parse_bool(config.get("enabled")), "configured_live_enabled": gate["gates"]["configured_live_enabled"]["value"], "account_currency": currency, "currency_status": "known" if currency else "unknown", "tradable_now": gate}, "historical_attribution_basis": "ledger_row_fields_only; current_params_never_backfill_history", "strategies": [_safe_strategy(raw) for _, _, raw in _iter_strategies(config)], "metrics": {"by_strategy": _grouped_metrics(rows, "strategy_id", currency), "by_signal": _grouped_metrics(rows, "signal_id", currency), "by_execution_class": [_metric(items, execution, execution, currency) for execution, items in by_execution.items()]}, "equity_curve": {"by_execution_class": [point for execution, items in by_execution.items() for point in _equity(items, execution, execution, currency)], "by_strategy": _grouped_curve(rows, "strategy_id", currency), "by_signal": _grouped_curve(rows, "signal_id", currency)}, "accounting": accounting, "inventory": {"open_position_count": None, "mtm_pnl": None, "currency": currency, "status": "unknown", "reason": "no_readonly_broker_position_and_bid_ask_snapshot"}, "sources": {"params": source_meta.get("params"), "trades": source_meta.get("trades"), "optional": _optional_source_views(source_meta, collector), "bot_log": {"path_label": collector.log.name, "runtime_liveness": "unknown"}, "collector": {"status": snapshot.status, "last_error": snapshot.last_error, "snapshot_age_seconds": max(0.0, time.monotonic() - snapshot.collected_at), "collection_count": snapshot.collect_count, "rotation_count": snapshot.rotation_count, "read_contract": source_meta.get("read_contract"), "rotation_coverage": source_meta.get("rotation_coverage")}}, "errors": errors}


INDEX_HTML = """<!doctype html><html lang=\"ja\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>bot0 dashboard</title><style>body{font:14px system-ui,sans-serif;max-width:1400px;margin:2rem auto;padding:0 1rem;background:#f7f7f7;color:#222}section{background:#fff;border:1px solid #ddd;border-radius:8px;padding:1rem;margin:1rem 0}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.4rem;border-bottom:1px solid #eee}.muted{color:#666}.warn{color:#a50}.bad{color:#b00}</style><h1>bot0 dashboard</h1><p class=\"muted\">read-only / bot23 adapter v4</p><section id=\"summary\">読み込み中...</section><section><h2>live / shadow</h2><table><thead><tr><th>class</th><th>deals</th><th>win rate</th><th>PF</th><th>PnL</th><th>status</th></tr></thead><tbody id=\"classes\"></tbody></table></section><section><h2>strategy / signal</h2><table><thead><tr><th>kind</th><th>scope</th><th>class</th><th>deals</th><th>win rate</th><th>PF</th><th>PnL</th><th>status</th></tr></thead><tbody id=\"scopes\"></tbody></table></section><script>const esc=v=>String(v??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));const cell=v=>v==null?'unknown':esc(v);async function refresh(){try{const r=await fetch('/api/summary',{cache:'no-store'});if(!r.ok)throw new Error('source unavailable');const d=await r.json(),a=d.accounting||{},i=d.inventory||{},c=d.config||{};document.querySelector('#summary').innerHTML='<p>総計: <b>'+cell(a.closed_deal_count)+' deals</b> / ledger_profit '+cell(a.ledger_profit)+' ('+cell(a.ledger_profit_unit_status)+')</p><p>inventory: '+cell(i.open_position_count)+' / MTM: '+cell(i.mtm_pnl)+'</p><p>configured_live_enabled: '+cell((c.tradable_now||{}).gates?.configured_live_enabled?.status)+' / currency: '+cell(c.account_currency)+'</p><p class=\"muted\">updated '+cell(d.generated_at_utc)+' / errors '+cell((d.errors||[]).join(', '))+'</p>';document.querySelector('#classes').innerHTML=(d.metrics?.by_execution_class||[]).map(m=>'<tr><td>'+cell(m.scope)+'</td><td>'+cell(m.deal_count)+'</td><td>'+cell(m.win_rate)+'</td><td>'+cell(m.profit_factor)+'</td><td>'+cell(m.realized_pnl)+'</td><td>'+cell(m.aggregation_status)+'</td></tr>').join('');const rows=[...(d.metrics?.by_strategy||[]).map(m=>({...m,kind:'strategy'})),...(d.metrics?.by_signal||[]).map(m=>({...m,kind:'signal'}))];document.querySelector('#scopes').innerHTML=rows.map(m=>'<tr><td>'+cell(m.kind)+'</td><td>'+cell(m.scope)+'</td><td>'+cell(m.execution_class)+'</td><td>'+cell(m.deal_count)+'</td><td>'+cell(m.win_rate)+'</td><td>'+cell(m.profit_factor)+'</td><td>'+cell(m.realized_pnl)+'</td><td>'+cell(m.aggregation_status)+'</td></tr>').join('')}catch(e){document.querySelector('#summary').innerHTML='<p class=\"bad\">source unavailable; last good snapshot may be unavailable</p>'}}refresh();setInterval(refresh,30000)</script>"""


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "bot0/4"
    def _send(self, status: HTTPStatus, body: bytes, content_type: str, *, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'")
        for key, value in (extra_headers or {}).items(): self.send_header(key, value)
        self.end_headers(); self.wfile.write(body)

    def _send_unauthorized(self) -> None:
        self._send(HTTPStatus.UNAUTHORIZED, b'{"error":"unauthorized"}', "application/json; charset=utf-8", extra_headers={"WWW-Authenticate": f'Basic realm="{AUTH_REALM}"'})

    def _is_authorized(self) -> bool:
        config = AUTH_CONFIG
        if config is None:
            return False
        headers = self.headers.get_all("Authorization", [])
        if len(headers) != 1:
            return False
        header = headers[0]
        try:
            if len(header.encode("ascii")) > MAX_AUTH_HEADER_BYTES:
                return False
        except UnicodeEncodeError:
            return False
        if header.count(" ") != 1 or "\t" in header:
            return False
        scheme, encoded = header.split(" ", 1)
        if scheme.lower() != "basic" or not encoded or len(encoded) > MAX_AUTH_B64_BYTES or len(encoded) % 4:
            return False
        try:
            encoded_bytes = encoded.encode("ascii")
            raw = base64.b64decode(encoded_bytes, validate=True)
        except (binascii.Error, ValueError, UnicodeError):
            return False
        if len(raw) > MAX_AUTH_DECODED_BYTES or base64.b64encode(raw) != encoded_bytes:
            return False
        presented_digest = hashlib.sha256(raw).digest()
        return hmac.compare_digest(presented_digest, config.credential_digest)

    def _require_auth(self) -> bool:
        if self._is_authorized():
            return True
        self._send_unauthorized()
        return False

    def do_GET(self) -> None:  # noqa: N802
        if not self._require_auth(): return
        parsed = urlsplit(self.path)
        if parsed.path == "/": self._send(HTTPStatus.OK, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8"); return
        if parsed.path == "/api/health": self._send(HTTPStatus.OK, b'{"status":"ok","read_only":true}', "application/json; charset=utf-8"); return
        if parsed.path != "/api/summary": self._send(HTTPStatus.NOT_FOUND, b'{"error":"not_found"}', "application/json; charset=utf-8"); return
        try:
            start, end = _period_bounds(parse_qs(parsed.query, keep_blank_values=False)); body = json.dumps(build_summary(start, end), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"); self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
        except DashboardError as exc:
            self._send(HTTPStatus.SERVICE_UNAVAILABLE if str(exc).startswith("source_") else HTTPStatus.BAD_REQUEST, json.dumps({"error": str(exc)}).encode(), "application/json; charset=utf-8")
        except Exception:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, b'{"error":"internal_error"}', "application/json; charset=utf-8")
    def do_POST(self) -> None:  # noqa: N802
        if not self._require_auth(): return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), MAX_BODY_BYTES)
            if length > 0: self.rfile.read(length)
        except (TypeError, ValueError): pass
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, b'{"error":"get_only"}', "application/json; charset=utf-8")
    def do_HEAD(self) -> None:  # noqa: N802
        if not self._require_auth(): return
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        self.end_headers()
    do_PUT = do_POST; do_DELETE = do_POST; do_OPTIONS = do_HEAD; do_PATCH = do_POST; do_TRACE = do_POST; do_CONNECT = do_POST
    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        if code == HTTPStatus.NOT_IMPLEMENTED and not self._is_authorized():
            self._send_unauthorized()
            return
        super().send_error(code, message, explain)
    def log_message(self, fmt: str, *args: Any) -> None: return


def production_gate_contract() -> dict[str, Any]:
    return {"service": "bot0-dashboard", "configured_live_enabled_field": "configured_live_enabled", "allowed_methods": ["GET"], "source_mounts": "read_only", "broker_client_imports": False, "order_write_capability": False, "historical_attribution": "ledger_row_fields_only", "http_authentication": "basic_required"}


def assert_read_only_ast() -> None:
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    forbidden = {"order_send", "buy", "sell", "close_position", "modify_position", "create_order", "MetaTrader5", "mt5"}
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    if names & forbidden:
        raise RuntimeError(f"read_only_ast_violation:{sorted(names & forbidden)}")


def main() -> None:
    assert_read_only_ast()
    configure_auth()
    server = ThreadingHTTPServer((DEFAULT_HOST, DEFAULT_PORT), DashboardHandler)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
