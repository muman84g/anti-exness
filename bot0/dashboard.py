"""Read-only bot0 dashboard with an isolated bot23 source adapter."""
from __future__ import annotations

import csv
import hashlib
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
BOT23_ROOT = Path(os.environ.get("BOT23_ROOT", "/data/bot23"))
BOT23_PARAMS = BOT23_ROOT / "s23_params.json"
BOT23_TRADES = BOT23_ROOT / "logs" / "s23_trades.csv"
BOT23_LOG = BOT23_ROOT / "logs" / "s23_bot.log"
COLLECTOR_TTL_SECONDS = max(0.1, float(os.environ.get("BOT0_COLLECTOR_TTL_SECONDS", "30")))
MAX_BODY_BYTES = 1_000_000
DEAL_TIME_RE = re.compile(r"(?:^|\s)deal_time_utc=([^\s,]+)")


class DashboardError(Exception):
    """A source or request error safe to expose to the dashboard client."""


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


def _file_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise DashboardError(f"source_unavailable:{path.name}") from exc
    if not isinstance(value, dict):
        raise DashboardError(f"invalid_object:{path.name}")
    return value


def _config_generation(config: dict[str, Any]) -> dict[str, Any]:
    for key in ("config_generation", "params_generation", "generation"):
        value = config.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return {"status": "known", "value": str(value), "key": key}
    return {"status": "unknown", "value": None, "key": None}


GROUPS: tuple[tuple[str, str, str], ...] = (
    ("core", "strategies", "enabled"),
    ("morning", "morning_session_strategies", "morning_session_enabled"),
    ("midday", "midday_session_strategies", "midday_session_enabled"),
    ("pre_eu30", "pre_eu30_session_strategies", "pre_eu30_session_enabled"),
    ("trend_recovery", "trend_recovery_strategies", "trend_recovery_enabled"),
    ("ny0530", "t0530_edge_strategies", "t0530_edge_enabled"),
    ("q01", "q01_variance_release_strategies", "q01_variance_release_enabled"),
    ("m15", "m15_terminal_strategies", "m15_terminal_enabled"),
    ("h7", "h7_strategies", "h7_enabled"),
    ("research", "research_entry_strategies", "research_entries_enabled"),
)


def _gate(value: Any) -> dict[str, Any]:
    parsed = _parse_bool(value)
    return {"value": parsed, "status": "enabled" if parsed is True else "blocked" if parsed is False else "unknown"}


def _tradable_gate(config: dict[str, Any]) -> dict[str, Any]:
    values = {
        "enabled": config.get("enabled"),
        "live_trading_enabled": config.get("live_trading_enabled"),
        "trading_enabled": config.get("trading_enabled", config.get("trade_enabled")),
        "allow_entries": config.get("allow_entries", config.get("entry_enabled")),
    }
    present = {key: _gate(value) for key, value in values.items() if value is not None}
    required = {"enabled", "live_trading_enabled"}
    status = "blocked" if any(item["status"] == "blocked" for item in present.values()) else "unknown" if any(key not in present for key in required) or any(item["status"] == "unknown" for item in present.values()) else "enabled"
    return {"status": status, "tradable_now": status == "enabled", "gates": present}


def _iter_strategies(config: dict[str, Any]) -> Iterable[tuple[str, bool, dict[str, Any]]]:
    root = _tradable_gate(config)
    seen: set[str] = set()
    for group, field, gate_name in GROUPS:
        items = config.get(field, [])
        if not isinstance(items, list):
            continue
        group_gate = _parse_bool(config.get(gate_name)) if gate_name != "enabled" else _parse_bool(config.get("enabled"))
        for raw in items:
            if not isinstance(raw, dict):
                continue
            ident = str(raw.get("id", "")).strip()
            if not ident or ident in seen:
                continue
            seen.add(ident)
            item_enabled = _parse_bool(raw.get("enabled"))
            effective = root["tradable_now"] and group_gate is True and item_enabled is True
            enriched = {**raw, "group": group, "group_enabled": group_gate, "effective_enabled": effective, "tradable_now": effective, "tradable_gate": root}
            yield group, effective, enriched


def _safe_strategy(raw: dict[str, Any]) -> dict[str, Any]:
    allowed = ("id", "group", "lane_id", "signal_id", "spec_id", "magic", "comment_prefix", "enabled", "group_enabled", "effective_enabled", "tradable_now", "tradable_gate", "hold_minutes", "max_positions", "lot")
    return {key: raw.get(key) for key in allowed if key in raw}


@dataclass(frozen=True)
class CloseRow:
    deal_id: str
    strategy_id: str
    signal_id: str
    lane_id: str
    magic: str
    basket_id: str
    ticket: str
    profit: float
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
    last_event: datetime | None
    last_close: datetime | None


@dataclass(frozen=True)
class SourceSnapshot:
    config: dict[str, Any]
    audit: TradeAudit
    collected_at: float
    attempted_at: float
    collect_count: int
    status: str
    last_error: str | None


def _close_time(row: dict[str, str]) -> tuple[datetime | None, datetime | None]:
    recorded = _parse_utc(row.get("timestamp_utc"))
    direct = _parse_utc(row.get("deal_time_utc"))
    match = DEAL_TIME_RE.search(row.get("note", ""))
    broker_time = direct or (_parse_utc(match.group(1)) if match else None)
    return broker_time, recorded


def _read_trades(path: Path) -> TradeAudit:
    if not path.is_file():
        raise DashboardError(f"source_unavailable:{path.name}")
    accepted: dict[str, CloseRow] = {}
    conflict_ids: set[str] = set()
    entries: list[dict[str, str]] = []
    reasons: list[str] = []
    duplicate = conflicts = quarantined = total = 0
    last_event: datetime | None = None
    last_close: datetime | None = None
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or None in reader.fieldnames or "event" not in reader.fieldnames:
                raise DashboardError("invalid_trade_header")
            for row in reader:
                total += 1
                if None in row or any(value is None for value in row.values()):
                    quarantined += 1
                    reasons.append("malformed_row")
                    continue
                recorded = _parse_utc(row.get("timestamp_utc"))
                if recorded and (last_event is None or recorded > last_event):
                    last_event = recorded
                event = (row.get("event") or "").strip()
                if event == "entry" and (row.get("ticket") or "").strip():
                    entries.append(row)
                if event != "position_close_confirmed":
                    continue
                deal_id = (row.get("deal_id") or "").strip()
                profit = _finite_float(row.get("profit"))
                broker_time, recorded_time = _close_time(row)
                currency = (row.get("currency") or row.get("account_currency") or "").strip().upper() or None
                execution = (row.get("execution_class") or "").strip().lower()
                live = _parse_bool(row.get("live"))
                execution_class = execution if execution in {"live", "shadow"} else "live" if live is True else "shadow" if live is False else "unknown"
                if not deal_id:
                    quarantined += 1; reasons.append("missing_deal_id"); continue
                if profit is None:
                    quarantined += 1; reasons.append("invalid_profit"); continue
                if broker_time is None:
                    quarantined += 1; reasons.append("missing_broker_deal_time"); continue
                close = CloseRow(deal_id, (row.get("strategy_id") or "").strip(), (row.get("signal_id") or "").strip(), (row.get("lane_id") or "").strip(), (row.get("magic") or "").strip(), (row.get("basket_id") or "").strip(), (row.get("ticket") or "").strip(), profit, currency, execution_class, broker_time, recorded_time)
                if deal_id in conflict_ids:
                    quarantined += 1; reasons.append("conflicting_duplicate_deal"); continue
                prior = accepted.get(deal_id)
                if prior is not None:
                    duplicate += 1
                    if prior != close:
                        conflicts += 1
                        conflict_ids.add(deal_id)
                        accepted.pop(deal_id, None)
                        quarantined += 2
                        reasons.append("conflicting_duplicate_deal")
                    continue
                accepted[deal_id] = close
                if last_close is None or broker_time > last_close:
                    last_close = broker_time
    except UnicodeDecodeError as exc:
        raise DashboardError("invalid_trade_encoding") from exc
    return TradeAudit(tuple(accepted.values()), tuple(entries), total, duplicate, conflicts, quarantined, tuple(reasons), last_event, last_close)


class SnapshotCollector:
    """One collector with an immutable last-good snapshot on refresh failure."""

    def __init__(self, params: Path, trades: Path, log: Path, ttl_seconds: float = COLLECTOR_TTL_SECONDS):
        self.params, self.trades, self.log = params, trades, log
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._snapshot: SourceSnapshot | None = None
        self._collect_count = 0

    def get(self) -> SourceSnapshot:
        with self._lock:
            now = time.monotonic()
            if self._snapshot and now - self._snapshot.attempted_at < self.ttl_seconds:
                return self._snapshot
            attempted = time.monotonic()
            try:
                config = _read_json(self.params)
                audit = _read_trades(self.trades)
            except DashboardError as exc:
                if self._snapshot is None:
                    raise
                return SourceSnapshot(self._snapshot.config, self._snapshot.audit, self._snapshot.collected_at, attempted, self._snapshot.collect_count, "stale_last_good", str(exc))
            self._collect_count += 1
            snapshot = SourceSnapshot(config, audit, attempted, attempted, self._collect_count, "fresh", None)
            self._snapshot = snapshot
            return snapshot


DEFAULT_COLLECTOR = SnapshotCollector(BOT23_PARAMS, BOT23_TRADES, BOT23_LOG)


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


def _source_info(path: Path, last_event: datetime | None) -> dict[str, Any]:
    mtime = _file_mtime(path)
    now = time.time()
    return {"path_label": path.name, "file_mtime_utc": _iso(datetime.fromtimestamp(mtime, timezone.utc)) if mtime else None, "file_age_seconds": max(0.0, now - mtime) if mtime else None, "last_event_utc": _iso(last_event), "event_age_seconds": max(0.0, now - last_event.timestamp()) if last_event else None, "runtime_liveness": "unknown", "freshness": "unknown", "freshness_reason": "file age does not prove bot liveness"}


def _metric(rows: list[CloseRow], scope: str, execution_class: str, currency: str | None) -> dict[str, Any]:
    wins = sum(row.profit > 0 for row in rows)
    currency_match = currency is not None and all(row.currency == currency for row in rows)
    gross_profit = sum(row.profit for row in rows if row.profit > 0) if currency_match else None
    gross_loss = -sum(row.profit for row in rows if row.profit < 0) if currency_match else None
    usable = currency_match and execution_class in {"live", "shadow"}
    return {"scope": scope, "execution_class": execution_class, "deal_count": len(rows), "win_count": wins, "loss_count": len(rows) - wins, "win_rate": wins / len(rows) if rows else None, "profit_factor": gross_profit / gross_loss if usable and gross_loss else None, "realized_pnl": sum(row.profit for row in rows) if usable else None, "currency": currency if usable else None, "aggregation_status": "usable" if usable else "blocked_currency_or_execution_class"}


def _equity(rows: list[CloseRow], scope: str, execution_class: str, currency: str | None) -> list[dict[str, Any]]:
    usable = currency is not None and execution_class in {"live", "shadow"} and all(row.currency == currency for row in rows)
    running = 0.0
    curve: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: (item.close_time, item.deal_id)):
        running += row.profit
        curve.append({"scope": scope, "execution_class": execution_class, "deal_id": row.deal_id, "close_time_utc": _iso(row.close_time), "cumulative_pnl": running if usable else None, "currency": currency if usable else None, "aggregation_status": "usable" if usable else "blocked_currency_or_execution_class"})
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


def build_summary(start: datetime | None = None, end: datetime | None = None, collector: SnapshotCollector = DEFAULT_COLLECTOR) -> dict[str, Any]:
    snapshot = collector.get()
    config, audit = snapshot.config, snapshot.audit
    rows = [row for row in audit.closes if _within(row.close_time, start, end)]
    currency = _currency(config)
    by_execution = {execution: [row for row in rows if row.execution_class == execution] for execution in ("live", "shadow", "unknown")}
    gate = _tradable_gate(config)
    errors = []
    if _config_generation(config)["status"] == "unknown":
        errors.append("config_generation_unknown")
    if currency is None:
        errors.append("currency_unknown_total_not_aggregated")
    if snapshot.status != "fresh":
        errors.append("source_refresh_failed_using_last_good_snapshot")
    errors.append("inventory_unknown")
    accounting = {"realized_pnl": None, "closed_deal_count": len(rows), "currency": currency, "aggregation_status": "separate_by_execution_class", "duplicate_deal_rows": audit.duplicate_deals, "conflicting_deal_rows": audit.conflicting_deals, "quarantined_rows": audit.quarantined_rows, "quarantine_reasons": sorted(set(audit.quarantine_reasons)), "close_time_basis": "broker deal_time_utc only; recorded timestamp is never a period fallback", "execution_classes": {key: _metric(value, key, key, currency) for key, value in by_execution.items()}}
    return {"service": "bot0", "adapter": "bot23.v3", "generated_at_utc": _iso(datetime.now(timezone.utc)), "period": {"from_utc": _iso(start), "to_utc_exclusive": _iso(end)}, "config": {"bot": str(config.get("bot_number", "23")), "strategy_id": config.get("strategy_id"), "candidate_id": config.get("candidate_id"), "generation": _config_generation(config), "sha256": _sha256(collector.params), "root_enabled": _parse_bool(config.get("enabled")), "live_trading_enabled": _parse_bool(config.get("live_trading_enabled")), "account_currency": currency, "currency_status": "known" if currency else "unknown", "tradable_now": gate}, "strategies": [_safe_strategy(raw) for _, _, raw in _iter_strategies(config)], "metrics": {"by_strategy": _grouped_metrics(rows, "strategy_id", currency), "by_signal": _grouped_metrics(rows, "signal_id", currency), "by_execution_class": [_metric(items, execution, execution, currency) for execution, items in by_execution.items()]}, "equity_curve": {"by_execution_class": [point for execution, items in by_execution.items() for point in _equity(items, execution, execution, currency)], "by_strategy": _grouped_curve(rows, "strategy_id", currency), "by_signal": _grouped_curve(rows, "signal_id", currency)}, "accounting": accounting, "inventory": {"open_position_count": None, "mtm_pnl": None, "currency": currency, "status": "unknown", "reason": "no_readonly_broker_position_and_bid_ask_snapshot"}, "sources": {"params": {"path_label": collector.params.name, "sha256": _sha256(collector.params), "generation": _config_generation(config)}, "trades": _source_info(collector.trades, audit.last_event), "bot_log": {"path_label": collector.log.name, "file_mtime_utc": _iso(datetime.fromtimestamp(_file_mtime(collector.log), timezone.utc)) if _file_mtime(collector.log) else None, "runtime_liveness": "unknown"}, "collector": {"status": snapshot.status, "last_error": snapshot.last_error, "snapshot_age_seconds": max(0.0, time.monotonic() - snapshot.collected_at), "collection_count": snapshot.collect_count}}, "errors": errors}


INDEX_HTML = """<!doctype html><html lang=\"ja\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>bot0 dashboard</title><style>body{font:14px system-ui,sans-serif;max-width:1400px;margin:2rem auto;padding:0 1rem;background:#f7f7f7;color:#222}section{background:#fff;border:1px solid #ddd;border-radius:8px;padding:1rem;margin:1rem 0}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.4rem;border-bottom:1px solid #eee}.muted{color:#666}.warn{color:#a50}.bad{color:#b00}</style><h1>bot0 dashboard</h1><p class=\"muted\">read-only / bot23 adapter v3</p><section id=\"summary\">読み込み中...</section><section><h2>live / shadow</h2><table><thead><tr><th>class</th><th>deals</th><th>win rate</th><th>PF</th><th>PnL</th><th>status</th></tr></thead><tbody id=\"classes\"></tbody></table></section><section><h2>strategy / signal</h2><table><thead><tr><th>kind</th><th>scope</th><th>class</th><th>deals</th><th>win rate</th><th>PF</th><th>PnL</th><th>status</th></tr></thead><tbody id=\"scopes\"></tbody></table></section><script>const esc=v=>String(v??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));const cell=v=>v==null?'unknown':esc(v);async function refresh(){try{const r=await fetch('/api/summary',{cache:'no-store'});if(!r.ok)throw new Error('source unavailable');const d=await r.json(),a=d.accounting||{},i=d.inventory||{},c=d.config||{};document.querySelector('#summary').innerHTML='<p>総計: <b>class別表示</b> / '+cell(a.closed_deal_count)+' deals / <span class=\"warn\">'+cell(a.aggregation_status)+'</span></p><p>inventory: '+cell(i.open_position_count)+' / MTM: '+cell(i.mtm_pnl)+'</p><p>tradable_now: '+cell((c.tradable_now||{}).status)+' / currency: '+cell(c.account_currency)+'</p><p class=\"muted\">updated '+cell(d.generated_at_utc)+' / errors '+cell((d.errors||[]).join(', '))+'</p>';document.querySelector('#classes').innerHTML=(d.metrics?.by_execution_class||[]).map(m=>'<tr><td>'+cell(m.scope)+'</td><td>'+cell(m.deal_count)+'</td><td>'+cell(m.win_rate)+'</td><td>'+cell(m.profit_factor)+'</td><td>'+cell(m.realized_pnl)+'</td><td>'+cell(m.aggregation_status)+'</td></tr>').join('');const rows=[...(d.metrics?.by_strategy||[]).map(m=>({...m,kind:'strategy'})),...(d.metrics?.by_signal||[]).map(m=>({...m,kind:'signal'}))];document.querySelector('#scopes').innerHTML=rows.map(m=>'<tr><td>'+cell(m.kind)+'</td><td>'+cell(m.scope)+'</td><td>'+cell(m.execution_class)+'</td><td>'+cell(m.deal_count)+'</td><td>'+cell(m.win_rate)+'</td><td>'+cell(m.profit_factor)+'</td><td>'+cell(m.realized_pnl)+'</td><td>'+cell(m.aggregation_status)+'</td></tr>').join('')}catch(e){document.querySelector('#summary').innerHTML='<p class=\"bad\">source unavailable; last good snapshot may be unavailable</p>'}}refresh();setInterval(refresh,30000)</script>"""


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "bot0/3"

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            self._send(HTTPStatus.OK, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/health":
            self._send(HTTPStatus.OK, b'{"status":"ok","read_only":true}', "application/json; charset=utf-8")
            return
        if parsed.path != "/api/summary":
            self._send(HTTPStatus.NOT_FOUND, b'{"error":"not_found"}', "application/json; charset=utf-8")
            return
        try:
            start, end = _period_bounds(parse_qs(parsed.query, keep_blank_values=False))
            body = json.dumps(build_summary(start, end), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
        except DashboardError as exc:
            self._send(HTTPStatus.SERVICE_UNAVAILABLE if str(exc).startswith("source_") else HTTPStatus.BAD_REQUEST, json.dumps({"error": str(exc)}).encode(), "application/json; charset=utf-8")
        except Exception:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, b'{"error":"internal_error"}', "application/json; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = min(int(self.headers.get("Content-Length", "0")), MAX_BODY_BYTES)
            if length > 0:
                self.rfile.read(length)
        except (TypeError, ValueError):
            pass
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, b'{"error":"get_only"}', "application/json; charset=utf-8")

    do_PUT = do_POST
    do_DELETE = do_POST

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer((DEFAULT_HOST, DEFAULT_PORT), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
