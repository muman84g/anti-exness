from __future__ import annotations

from dataclasses import dataclass
import math


TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_NO_CHANGES = 10025
KNOWN_PREFILL_REJECTIONS = frozenset(
    {
        "BAD_OPEN_R1_GUARD",
        "OPEN_R1_POLICY_GUARD",
        "OPEN_R1_INVENTORY_GUARD",
        "OPEN_R1_INVENTORY_QUERY",
        "OPEN_R1_ORDER_QUERY",
        "SYMBOL_ADMISSION_GUARD",
        "MARGIN_ADMISSION_GUARD",
        "ACCOUNT_IDENTITY_GUARD",
        "ACCOUNT_MODE_GUARD",
        "TRADE_PERMISSION_GUARD",
        "BAD_OPEN_TYPE",
        "INVALID_OPEN_R1_REQUEST",
    }
)


@dataclass(frozen=True)
class R1OpenResult:
    status: str
    raw_response: str
    ticket: int = 0
    identifier: int = 0
    deal: int = 0
    fill: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    open_time: int = 0
    open_retcode: int = 0
    modify_retcode: int = 0
    reason: str = ""


@dataclass(frozen=True)
class R1RepairResult:
    ok: bool
    status: str
    raw_response: str
    ticket: int = 0
    identifier: int = 0
    fill: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    retcode: int = 0


@dataclass(frozen=True)
class R1CloseResult:
    success: bool
    status: str
    raw_response: str
    ticket: int = 0
    lot: float = 0.0
    open_price: float = 0.0
    close_price: float = 0.0
    profit: float = 0.0
    deal: int = 0
    retcode: int = 0


def _safe_field(value: object, name: str) -> str:
    text = str(value)
    if not text or any(char in text for char in "|,\r\n"):
        raise ValueError(f"invalid {name}")
    return text


def _nonnegative_receipt_int(value: str, prefix: str) -> int:
    if not value.startswith(prefix):
        raise ValueError(f"missing {prefix} receipt field")
    text = value[len(prefix):]
    if not text or not text.isascii() or not text.isdecimal():
        raise ValueError(f"invalid {prefix} receipt field")
    return int(text)


def build_open_r1_command(
    *,
    symbol: str,
    order_type: int,
    lot: float,
    fixed_stop: float,
    magic: int,
    comment: str,
    deviation: int,
    expected_login: int,
    expected_server: str,
    expected_owned_positions: int,
    digits: int,
) -> str:
    symbol_text = _safe_field(symbol, "symbol")
    comment_text = _safe_field(comment, "comment")[:31]
    server_text = _safe_field(expected_server, "server")
    lot_value = float(lot)
    stop_value = float(fixed_stop)
    values_valid = (
        symbol_text == "XAUUSD"
        and int(order_type) in {0, 1}
        and math.isfinite(lot_value)
        and math.isclose(lot_value, 0.01, rel_tol=0.0, abs_tol=1e-12)
        and math.isfinite(stop_value)
        and stop_value > 0.0
        and int(magic) > 0
        and int(deviation) == 50
        and int(expected_login) > 0
        and int(expected_owned_positions) == 0
        and 0 <= int(digits) <= 10
    )
    if not values_valid:
        raise ValueError("invalid OPEN_R1 request")
    return (
        f"OPEN_R1|{symbol_text}|{int(order_type)}|{lot_value:.2f}|"
        f"{stop_value:.{int(digits)}f}|{int(magic)}|{comment_text}|{int(deviation)}|"
        f"{int(expected_login)}|{server_text}|0"
    )


def build_repair_r1_command(
    *,
    ticket: int,
    expected_login: int,
    expected_server: str,
    expected_symbol: str,
    expected_magic: int,
    expected_comment: str,
    expected_identifier: int,
) -> str:
    server = _safe_field(expected_server, "server")
    symbol = _safe_field(expected_symbol, "symbol")
    comment = _safe_field(expected_comment, "comment")[:31]
    if (
        int(ticket) <= 0
        or int(expected_login) <= 0
        or symbol != "XAUUSD"
        or int(expected_magic) <= 0
        or int(expected_identifier) <= 0
    ):
        raise ValueError("invalid REPAIR_R1 request")
    return (
        f"REPAIR_R1|{int(ticket)}|{int(expected_login)}|{server}|{symbol}|"
        f"{int(expected_magic)}|{comment}|{int(expected_identifier)}"
    )


def build_close_r1_command(
    *,
    ticket: int,
    deviation: int,
    expected_login: int,
    expected_server: str,
    expected_symbol: str,
    expected_magic: int,
    expected_comment: str,
    expected_identifier: int,
) -> str:
    server = _safe_field(expected_server, "server")
    symbol = _safe_field(expected_symbol, "symbol")
    comment = _safe_field(expected_comment, "comment")[:31]
    if (
        int(ticket) <= 0
        or int(deviation) != 50
        or int(expected_login) <= 0
        or symbol != "XAUUSD"
        or int(expected_magic) <= 0
        or int(expected_identifier) <= 0
    ):
        raise ValueError("invalid CLOSE_R1 request")
    return (
        f"CLOSE_R1|{int(ticket)}|{int(deviation)}|{int(expected_login)}|{server}|"
        f"{symbol}|{int(expected_magic)}|{comment}|{int(expected_identifier)}"
    )


def parse_open_r1_response(response: str | None) -> R1OpenResult:
    raw = str(response or "")
    parts = raw.split("|") if raw else []
    try:
        if len(parts) == 11 and parts[:2] == ["OK", "R1"]:
            result = R1OpenResult(
                status="CONFIRMED",
                raw_response=raw,
                ticket=int(parts[2]),
                identifier=int(parts[3]),
                deal=int(parts[4]),
                fill=float(parts[5]),
                stop=float(parts[6]),
                target=float(parts[7]),
                open_time=int(parts[8]),
                open_retcode=int(parts[9]),
                modify_retcode=int(parts[10]),
            )
            if not _valid_open_result(result, require_target=True):
                raise ValueError(raw)
            return result
        if len(parts) == 10 and parts[:2] == ["RECOVER", "R1_TP_REQUIRED"]:
            result = R1OpenResult(
                status="REPAIR_REQUIRED",
                raw_response=raw,
                ticket=int(parts[2]),
                identifier=int(parts[3]),
                deal=int(parts[4]),
                fill=float(parts[5]),
                stop=float(parts[6]),
                open_time=int(parts[7]),
                open_retcode=int(parts[8]),
                modify_retcode=int(parts[9]),
                reason="tp_setup_failed_after_confirmed_fill",
            )
            if not _valid_open_result(result, require_target=False):
                raise ValueError(raw)
            return result
    except (TypeError, ValueError, OverflowError):
        return R1OpenResult("AMBIGUOUS", raw, reason="malformed_execution_bearing_response")
    if len(parts) == 2 and parts[0] == "ERR" and parts[1] in KNOWN_PREFILL_REJECTIONS:
        return R1OpenResult("NO_FILL", raw, reason=parts[1])
    if len(parts) == 5 and parts[0] == "ERR":
        try:
            retcode = int(parts[1])
            order = _nonnegative_receipt_int(parts[2], "ORDER=")
            deal = _nonnegative_receipt_int(parts[3], "DEAL=")
            _nonnegative_receipt_int(parts[4], "LAST=")
        except (TypeError, ValueError, OverflowError):
            retcode = 0
            order = deal = -1
        if retcode in {10018, 10026, 10027} and order == 0 and deal == 0:
            return R1OpenResult("NO_FILL", raw, reason=f"RETCODE_{retcode}")
    return R1OpenResult("AMBIGUOUS", raw, reason="response_does_not_prove_fill_or_no_fill")


def _valid_open_result(result: R1OpenResult, *, require_target: bool) -> bool:
    base = (
        result.ticket > 0
        and result.identifier > 0
        and result.deal > 0
        and math.isfinite(result.fill)
        and result.fill > 0.0
        and math.isfinite(result.stop)
        and result.stop > 0.0
        and result.open_time > 0
        and result.open_retcode == TRADE_RETCODE_DONE
    )
    if not base:
        return False
    if require_target:
        return (
            math.isfinite(result.target)
            and result.target > 0.0
            and result.modify_retcode in {TRADE_RETCODE_DONE, TRADE_RETCODE_NO_CHANGES}
        )
    return True


def parse_repair_r1_response(response: str | None) -> R1RepairResult:
    raw = str(response or "")
    parts = raw.split("|") if raw else []
    try:
        if len(parts) == 9 and parts[:2] == ["OK", "R1_REPAIRED"]:
            result = R1RepairResult(
                ok=True,
                status="CONFIRMED",
                raw_response=raw,
                ticket=int(parts[2]),
                identifier=int(parts[3]),
                fill=float(parts[4]),
                stop=float(parts[5]),
                target=float(parts[6]),
                retcode=int(parts[7]),
            )
            # parts[8] is the current position type echoed by the bridge.
            if (
                result.ticket <= 0
                or result.identifier <= 0
                or not all(math.isfinite(value) and value > 0.0 for value in (result.fill, result.stop, result.target))
                or result.retcode not in {TRADE_RETCODE_DONE, TRADE_RETCODE_NO_CHANGES}
                or int(parts[8]) not in {0, 1}
            ):
                raise ValueError(raw)
            return result
    except (TypeError, ValueError, OverflowError):
        return R1RepairResult(False, "AMBIGUOUS", raw)
    return R1RepairResult(False, "FAILED", raw)


def parse_close_r1_response(response: str | None) -> R1CloseResult:
    raw = str(response or "")
    parts = raw.split("|") if raw else []
    try:
        if len(parts) == 9 and parts[:2] == ["OK", "R1_CLOSED"]:
            result = R1CloseResult(
                True,
                "CONFIRMED",
                raw,
                ticket=int(parts[2]),
                lot=float(parts[3]),
                open_price=float(parts[4]),
                close_price=float(parts[5]),
                profit=float(parts[6]),
                deal=int(parts[7]),
                retcode=int(parts[8]),
            )
            if (
                result.ticket <= 0
                or not all(math.isfinite(value) for value in (result.lot, result.open_price, result.close_price, result.profit))
                or result.lot <= 0.0
                or result.open_price <= 0.0
                or result.close_price <= 0.0
                or result.deal <= 0
                or result.retcode != TRADE_RETCODE_DONE
            ):
                raise ValueError(raw)
            return result
    except (TypeError, ValueError, OverflowError):
        return R1CloseResult(False, "AMBIGUOUS", raw)
    if raw == "ERR|POSITION_NOT_FOUND":
        return R1CloseResult(False, "MISSING_UNCONFIRMED", raw)
    if len(parts) == 4 and parts[0] == "ERR":
        try:
            retcode = int(parts[1])
            deal = _nonnegative_receipt_int(parts[2], "DEAL=")
            _nonnegative_receipt_int(parts[3], "LAST=")
        except (TypeError, ValueError, OverflowError):
            retcode = 0
            deal = -1
        if retcode == 10018 and deal == 0:
            return R1CloseResult(False, "MARKET_CLOSED", raw, retcode=retcode)
        if retcode in {10026, 10027} and deal == 0:
            return R1CloseResult(False, "TRADE_PERMISSION_GUARD", raw, retcode=retcode)
    if raw in {
        "ERR|ACCOUNT_IDENTITY_GUARD",
        "ERR|ACCOUNT_MODE_GUARD",
        "ERR|TRADE_PERMISSION_GUARD",
        "ERR|POSITION_OWNERSHIP_GUARD",
        "ERR|CLOSE_R1_POLICY_GUARD",
    }:
        return R1CloseResult(False, raw.split("|", 1)[1], raw)
    return R1CloseResult(False, "AMBIGUOUS", raw)
