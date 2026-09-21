"""Read-only bot0 dashboard service.

The first adapter is deliberately limited to bot23.  It consumes immutable
configuration and trade-audit files through read-only mounts and has no broker
or command-channel dependency.  The adapter boundary is kept explicit so a
future bot can be added without changing bot23 accounting semantics.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit


APP_ROOT = Path(__file__).resolve().parent
DEFAULT_HOST = os.environ.get("BOT0_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("BOT0_PORT", "8230"))
BOT23_PARAMS = Path(os.environ.get("BOT23_PARAMS", "/data/bot23/s23_params.json"))
BOT23_TRADES = Path(os.environ.get("BOT23_TRADES", "/data/bot23/logs/s23_trades.csv"))
BOT23_LOG = Path(os.environ.get("BOT23_LOG", "/data/bot23/logs/s23_bot.log"))
MAX_BODY_BYTES = 1_000_000
UTC_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


class DashboardError(Exception):
    """Expected input error that is safe to expose to a client."""


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
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
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise DashboardError(f"source_unavailable:{path.name}") from exc
    if not isinstance(value, dict):
        raise DashboardError(f"invalid_object:{path.name}")
    return value


def _sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _file_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _bool_or_false(value: Any) -> bool:
    return _parse_bool(value) is True


def _config_generation(config: dict[str, Any]) -> dict[str, Any]:
    # A candidate id is not a generation marker.  If the source did not
    # publish a generation, keep that fact visible instead of guessing.
    keys = ("config_generation", "params_generation", "generation")
    for key in keys:
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


def _iter_strategies(config: dict[str, Any]) -> Iterable[tuple[str, bool, dict[str, Any]]]:
    root_enabled = _bool_or_false(config.get("enabled"))
    seen: set[str] = set()
    for group, field, gate in GROUPS:
        items = config.get(field, [])
        if not isinstance(items, list):
            continue
        group_enabled = _bool_or_false(config.get(gate)) if gate != "enabled" else root_enabled
        for raw in items:
            if not isinstance(raw, dict):
                continue
            ident = str(raw.get("id", "")).strip()
            if not ident or ident in seen:
                continue
            seen.add(ident)
            item_enabled = _parse_bool(raw.get("enabled")) is True
            effective = root_enabled and group_enabled and item_enabled
            yield group, effective, {**raw, "group": group, "group_enabled": group_enabled, "effective_enabled": effective}


def _safe_strategy(value: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "id", "group", "lane_id", "signal_id", "spec_id", "magic",
        "comment_prefix", "effective_enabled", "group_enabled", "enabled",
        "hold_minutes", "max_positions", "lot",
    )
    return {key: value.get(key) for key in allowed if key in value}


@dataclass(frozen=True)
class CloseRow:
    deal_id: str
    strategy_id: str
    lane_id: str
    magic: str
    basket_id: str
    ticket: str
    profit: float
    close_time: datetime
    close_time_source: str


@dataclass(frozen=True)
class TradeAudit:
    closes: tuple[CloseRow, ...]
    entries: tuple[dict[str, str], ...]
    source_rows: int
    duplicate_deals: int
    conflicting_deals: int
    malformed_rows: int
    last_event: datetime | None
    last_close: datetime | None


def _close_time(row: dict[str, str]) -> tuple[datetime | None, str]:
    note = row.get("note", "")
    match = re.search(r"(?:^|\s)deal_time_utc=([^\s,]+)", note)
    if match:
        parsed = _parse_utc(match.group(1))
        if parsed:
            return parsed, "note.deal_time_utc"
    return _parse_utc(row.get("timestamp_utc")), "timestamp_utc"


def _read_trades(path: Path) -> TradeAudit:
    if not path.is_file():
        raise DashboardError(f"source_unavailable:{path.name}")
    closes: list[CloseRow] = []
    entries: list[dict[str, str]] = []
    by_deal: dict[str, CloseRow] = {}
    duplicate_deals = 0
    conflicting_deals = 0
    malformed = 0
    total = 0
    last_event: datetime | None = None
    last_close: datetime | None = None
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "event" not in reader.fieldnames:
                raise DashboardError("invalid_trade_header")
            for row in reader:
                total += 1
                event_time = _parse_utc(row.get("timestamp_utc"))
                if event_time and (last_event is None or event_time > last_event):
                    last_event = event_time
                event = (row.get("event") or "").strip()
                if event == "entry" and (row.get("ticket") or "").strip():
                    entries.append(row)
                if event != "position_close_confirmed":
                    continue
                deal_id = (row.get("deal_id") or "").strip()
                profit = _finite_float(row.get("profit"))
                close_time, close_source = _close_time(row)
                if not deal_id or profit is None or close_time is None:
                    malformed += 1
                    continue
                close = CloseRow(
                    deal_id=deal_id,
                    strategy_id=(row.get("strategy_id") or "").strip(),
                    lane_id=(row.get("lane_id") or "").strip(),
                    magic=(row.get("magic") or "").strip(),
                    basket_id=(row.get("basket_id") or "").strip(),
                    ticket=(row.get("ticket") or "").strip(),
                    profit=profit,
                    close_time=close_time,
                    close_time_source=close_source,
                )
                prior = by_deal.get(deal_id)
                if prior is not None:
                    duplicate_deals += 1
                    if prior != close:
                        conflicting_deals += 1
                    continue
                by_deal[deal_id] = close
                closes.append(close)
                if last_close is None or close_time > last_close:
                    last_close = close_time
    except UnicodeDecodeError as exc:
        raise DashboardError("invalid_trade_encoding") from exc
    return TradeAudit(tuple(closes), tuple(entries), total, duplicate_deals, conflicting_deals, malformed, last_event, last_close)


def _period_bounds(query: dict[str, list[str]]) -> tuple[datetime | None, datetime | None]:
    def one(key: str) -> datetime | None:
        values = query.get(key, [])
        if len(values) > 1 or (values and len(values[0]) > 32):
            raise DashboardError("invalid_period")
        return _parse_utc(values[0]) if values else None

    start, end = one("from"), one("to")
    if query.get("from") and start is None or query.get("to") and end is None:
        raise DashboardError("invalid_period")
    if start and end and start > end:
        raise DashboardError("invalid_period")
    return start, end


def _within(value: datetime, start: datetime | None, end: datetime | None) -> bool:
    return (start is None or value >= start) and (end is None or value < end)


def _source_info(path: Path, last_event: datetime | None) -> dict[str, Any]:
    now = time.time()
    mtime = _file_mtime(path)
    return {
        "path_label": path.name,
        "file_mtime_utc": _iso(datetime.fromtimestamp(mtime, timezone.utc)) if mtime else None,
        "file_age_seconds": max(0.0, now - mtime) if mtime else None,
        "last_event_utc": _iso(last_event),
        "event_age_seconds": max(0.0, now - last_event.timestamp()) if last_event else None,
        "runtime_liveness": "unknown",
        "freshness": "unknown",
        "freshness_reason": "file_mtime_and_last_event_do_not_prove_bot_liveness",
    }


def build_summary(start: datetime | None = None, end: datetime | None = None) -> dict[str, Any]:
    config = _read_json(BOT23_PARAMS)
    audit = _read_trades(BOT23_TRADES)
    closes = [close for close in audit.closes if _within(close.close_time, start, end)]
    realized = sum(close.profit for close in closes)
    gross_profit = sum(close.profit for close in closes if close.profit > 0)
    gross_loss = -sum(close.profit for close in closes if close.profit < 0)
    # JSON has no finite representation for infinity. A loss-free period is
    # therefore exposed as null; gross profit/loss still make the reason clear.
    pf = gross_profit / gross_loss if gross_loss else None
    closed_tickets = {close.ticket for close in audit.closes if close.ticket}
    entry_tickets = {row.get("ticket", "").strip() for row in audit.entries if row.get("ticket", "").strip()}
    open_estimate = len(entry_tickets - closed_tickets)
    strategies = [_safe_strategy(raw) for _, _, raw in _iter_strategies(config)]
    return {
        "service": "bot0",
        "adapter": "bot23.v1",
        "generated_at_utc": _iso(datetime.now(timezone.utc)),
        "period": {"from_utc": _iso(start), "to_utc_exclusive": _iso(end)},
        "config": {
            "bot": str(config.get("bot_number", "23")),
            "strategy_id": config.get("strategy_id"),
            "candidate_id": config.get("candidate_id"),
            "generation": _config_generation(config),
            "sha256": _sha256(BOT23_PARAMS),
            "root_enabled": _parse_bool(config.get("enabled")),
            "live_trading_enabled": _parse_bool(config.get("live_trading_enabled")),
        },
        "strategies": strategies,
        "accounting": {
            "realized_pnl_usd": realized,
            "closed_deal_count": len(closes),
            "gross_profit_usd": gross_profit,
            "gross_loss_usd": gross_loss,
            "profit_factor": pf,
            "duplicate_deal_rows": audit.duplicate_deals,
            "conflicting_deal_rows": audit.conflicting_deals,
            "malformed_close_rows": audit.malformed_rows,
            "close_identity": "position_close_confirmed.deal_id_unique",
            "close_time_basis": "note.deal_time_utc_when_valid_else_timestamp_utc",
        },
        "inventory": {
            "estimated_open_ticket_count": open_estimate,
            "estimate_basis": "entry.ticket_minus_unique_close.ticket",
            "mtm_pnl_usd": None,
            "mtm_status": "unavailable_no_readonly_bid_ask_snapshot",
        },
        "sources": {
            "params": {"path_label": BOT23_PARAMS.name, "sha256": _sha256(BOT23_PARAMS), "generation": _config_generation(config)},
            "trades": _source_info(BOT23_TRADES, audit.last_event),
            "bot_log": {"path_label": BOT23_LOG.name, "file_mtime_utc": _iso(datetime.fromtimestamp(_file_mtime(BOT23_LOG), timezone.utc)) if _file_mtime(BOT23_LOG) else None, "runtime_liveness": "unknown"},
        },
        "errors": ["config_generation_unknown"] if _config_generation(config)["status"] == "unknown" else [],
    }


INDEX_HTML = """<!doctype html><html lang=\"ja\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>bot0 dashboard</title><style>body{font:14px system-ui,sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem;background:#f7f7f7;color:#222}section{background:#fff;border:1px solid #ddd;border-radius:8px;padding:1rem;margin:1rem 0}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.45rem;border-bottom:1px solid #eee}code{word-break:break-all}.muted{color:#666}.bad{color:#a00}</style><h1>bot0 dashboard</h1><p class=\"muted\">read-only / bot23 adapter v1</p><section id=\"summary\">読み込み中...</section><section><h2>シグナル</h2><table><thead><tr><th>group</th><th>id</th><th>signal</th><th>enabled</th><th>magic</th></tr></thead><tbody id=\"strategies\"></tbody></table></section><script>const esc=v=>String(v??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));async function refresh(){const r=await fetch('/api/summary',{cache:'no-store'});const d=await r.json();const a=d.accounting||{},i=d.inventory||{},c=d.config||{};document.querySelector('#summary').innerHTML='<p>実現損益: <b>'+esc(a.realized_pnl_usd)+'</b> USD / closed '+esc(a.closed_deal_count)+' / PF '+esc(a.profit_factor)+'</p><p>open estimate: '+esc(i.estimated_open_ticket_count)+' / MTM: <span class=\"muted\">'+esc(i.mtm_status)+'</span></p><p>config: '+esc(c.strategy_id)+' / generation '+esc((c.generation||{}).status)+' / runtime '+esc((d.sources?.trades||{}).runtime_liveness)+'</p><p class=\"muted\">updated '+esc(d.generated_at_utc)+'</p>';document.querySelector('#strategies').innerHTML=(d.strategies||[]).map(s=>'<tr><td>'+esc(s.group)+'</td><td>'+esc(s.id)+'</td><td>'+esc(s.signal_id)+'</td><td>'+esc(s.effective_enabled)+'</td><td>'+esc(s.magic)+'</td></tr>').join('')}refresh();setInterval(refresh,30000)</script>"""


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "bot0/1"

    def _headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self._headers(content_type, len(body))
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
            body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
            self._send(HTTPStatus.SERVICE_UNAVAILABLE if str(exc).startswith("source_") else HTTPStatus.BAD_REQUEST, body, "application/json; charset=utf-8")
        except Exception:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, b'{"error":"internal_error"}', "application/json; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        # Consume a bounded request body before replying so a keep-alive
        # client cannot desynchronize the next request. No body is accepted.
        try:
            length = min(int(self.headers.get("Content-Length", "0")), MAX_BODY_BYTES)
            if length > 0:
                self.rfile.read(length)
        except (TypeError, ValueError):
            pass
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, b'{"error":"get_only"}', "application/json; charset=utf-8")

    def do_PUT(self) -> None:  # noqa: N802
        self.do_POST()

    def do_DELETE(self) -> None:  # noqa: N802
        self.do_POST()

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep access logs free of query values and potential user text.
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
