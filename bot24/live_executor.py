# -*- coding: utf-8 -*-
"""Ownership-aware file-IPC executor used by generated live bots."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any

from ea_bridge import ea_bridge
from v206_execution import (
    R1CloseResult,
    R1OpenResult,
    R1RepairResult,
    build_close_r1_command,
    build_open_r1_command,
    build_repair_r1_command,
    parse_close_r1_response,
    parse_open_r1_response,
    parse_repair_r1_response,
)


ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
HEDGING_MARGIN_MODE = 2
TRADE_RETCODE_DONE = 10009
S24_V206_POLICY = {240206: "s24_v206"}
S24_CORE_MAGIC = 200024
S24_CORE_COMMENT_PREFIX = "s24_no_adverse"
REQUIRED_SHARED_ACCOUNT_COMMANDS = {
    "ECHO", "CAPS", "ACCOUNT", "INFO", "HIST", "OPEN", "OPEN_R1", "REPAIR_R1", "CLOSE_R1",
    "POSITIONS", "POSITION", "ORDERS", "CLOSEDEAL", "CLOSE",
}


def _valid_core_comment(value: Any, *, allow_legacy: bool) -> bool:
    comment = str(value or "")
    if allow_legacy and comment == S24_CORE_COMMENT_PREFIX:
        return True
    return re.fullmatch(rf"{re.escape(S24_CORE_COMMENT_PREFIX)}:[0-9a-f]{{10}}", comment) is not None


def _strict_int_text(value: Any) -> int:
    text = value if isinstance(value, str) else ""
    if re.fullmatch(r"-?[0-9]+", text) is None:
        raise ValueError(f"invalid integer field: {value!r}")
    return int(text)


@dataclass
class SymbolInfo:
    bid: float
    ask: float
    point: float
    volume_min: float
    volume_max: float
    volume_step: float
    digits: int
    stops_level: int
    margin_free: float = 0.0
    tick_value: float = 0.0
    tick_size: float = 0.0
    contract_size: float = 0.0
    trade_mode: int = 0
    order_mode: int = 0
    quote_time_msc: int | None = None


@dataclass
class LiveRecord:
    ticket: int
    symbol: str
    type: int
    volume: float
    open_price: float
    sl: float
    tp: float
    profit: float
    magic: int
    open_time: int
    open_time_msc: int
    identifier: int
    comment: str


@dataclass
class PositionCloseDeal:
    deal: int
    position_id: int
    symbol: str
    magic: int
    reason: str
    price: float
    profit: float
    commission: float
    swap: float
    fee: float
    deal_time: int
    exit_volume: float

    @property
    def net_profit(self) -> float:
        return self.profit + self.commission + self.swap + self.fee


@dataclass
class CloseResult:
    success: bool
    status: str = "CONFIRMED"
    lot: float = 0.0
    open_price: float = 0.0
    close_price: float = 0.0
    profit: float = 0.0
    deal_id: int = 0
    retcode: int | None = None
    raw_response: str = ""

    def __bool__(self) -> bool:
        return self.success


class MT5Executor:
    def __init__(self) -> None:
        self.last_order_error: str | None = None
        self.last_open_identifier: int | None = None
        self.last_open_deal: int | None = None
        self.last_open_price: float | None = None
        self.last_open_time: int | None = None

    def get_bridge_capabilities(self) -> dict[str, Any] | None:
        res = ea_bridge.send_command("CAPS|", timeout=10)
        if not res or not res.startswith("OK|CAPS|"):
            return None
        parts = res.split("|")
        if len(parts) != 5:
            return None
        commands = parts[4].split(",")
        if not commands or any(re.fullmatch(r"[A-Z][A-Z0-9_]*", item) is None for item in commands) or len(commands) != len(set(commands)):
            return None
        return {"name": parts[2], "version": parts[3], "commands": set(commands)}

    def get_account_info(self) -> dict[str, Any] | None:
        res = ea_bridge.send_command("ACCOUNT|", timeout=10)
        if not res or not res.startswith("OK|"):
            return None
        parts = res.split("|")
        if len(parts) != 10:
            return None
        try:
            permissions = [_strict_int_text(parts[index]) for index in range(3, 7)]
            if any(value not in {0, 1} for value in permissions):
                return None
            return {
                "margin_mode": _strict_int_text(parts[1]), "margin_mode_name": parts[2],
                "account_trade_allowed": bool(permissions[0]), "account_trade_expert": bool(permissions[1]),
                "terminal_trade_allowed": bool(permissions[2]), "mql_trade_allowed": bool(permissions[3]),
                "login": _strict_int_text(parts[7]), "server": parts[8], "currency": parts[9],
            }
        except (TypeError, ValueError, OverflowError):
            return None

    def get_symbol_info(self, symbol: str) -> SymbolInfo | None:
        res = ea_bridge.send_command(f"INFO|{symbol}", timeout=10)
        if not res or not res.startswith("OK|"):
            return None
        parts = res.split("|")
        if len(parts) != 16 or not parts[13]:
            return None
        try:
            ask, bid, margin_free, point = map(float, parts[1:5])
            volume_min, volume_max, volume_step = map(float, parts[5:8])
            tick_value, tick_size, contract_size = map(float, parts[8:11])
            digits = _strict_int_text(parts[11])
            stops_level = _strict_int_text(parts[12])
            quote_time_msc = _strict_int_text(parts[13])
            trade_mode = _strict_int_text(parts[14])
            order_mode = _strict_int_text(parts[15])
            numeric = (ask, bid, margin_free, point, volume_min, volume_max, volume_step, tick_value, tick_size, contract_size)
            if (
                not all(math.isfinite(value) for value in numeric)
                or ask <= 0.0 or bid <= 0.0 or ask < bid or margin_free < 0.0 or point <= 0.0
                or volume_min <= 0.0 or volume_max < volume_min or volume_step <= 0.0
                or tick_value <= 0.0 or tick_size <= 0.0 or contract_size <= 0.0
                or digits < 0 or stops_level < 0 or quote_time_msc <= 0
                or trade_mode not in {0, 1, 2, 3, 4} or order_mode < 0
            ):
                return None
            return SymbolInfo(
                ask=ask, bid=bid, point=point, volume_min=volume_min, volume_max=volume_max,
                volume_step=volume_step, digits=digits, stops_level=stops_level,
                margin_free=margin_free, tick_value=tick_value, tick_size=tick_size,
                contract_size=contract_size, trade_mode=trade_mode, order_mode=order_mode,
                quote_time_msc=quote_time_msc,
            )
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _parse_record(item: str) -> LiveRecord:
        parts = item.split(",")
        if len(parts) != 13 or any(part != part.strip() for part in parts):
            raise ValueError(f"invalid live record: {item}")
        record = LiveRecord(
            ticket=_strict_int_text(parts[0]), symbol=parts[1], type=_strict_int_text(parts[2]), volume=float(parts[3]),
            open_price=float(parts[4]), sl=float(parts[5]), tp=float(parts[6]), profit=float(parts[7]),
            magic=_strict_int_text(parts[8]), open_time=_strict_int_text(parts[9]),
            open_time_msc=_strict_int_text(parts[10]), identifier=_strict_int_text(parts[11]), comment=parts[12],
        )
        if (
            record.ticket <= 0 or not record.symbol or record.type not in {ORDER_TYPE_BUY, ORDER_TYPE_SELL}
            or not math.isfinite(record.volume) or record.volume <= 0.0
            or not math.isfinite(record.open_price) or record.open_price <= 0.0
            or not all(math.isfinite(value) for value in (record.sl, record.tp, record.profit))
            or record.open_time <= 0 or record.open_time_msc <= 0
            or record.open_time_msc // 1000 != record.open_time or record.identifier <= 0
        ):
            raise ValueError(f"invalid live record values: {item}")
        return record

    @staticmethod
    def _parse_order_record(item: str) -> LiveRecord:
        parts = item.split(",")
        if len(parts) != 9 or any(part != part.strip() for part in parts):
            raise ValueError(f"invalid live order record: {item}")
        record = LiveRecord(
            ticket=_strict_int_text(parts[0]), symbol=parts[1], type=_strict_int_text(parts[2]), volume=float(parts[3]),
            open_price=float(parts[4]), sl=float(parts[5]), tp=float(parts[6]), profit=0.0,
            magic=_strict_int_text(parts[7]), open_time=0, open_time_msc=0,
            identifier=_strict_int_text(parts[0]), comment=parts[8],
        )
        if (
            record.ticket <= 0 or not record.symbol or record.type not in {2, 3, 4, 5, 6, 7}
            or not math.isfinite(record.volume) or record.volume <= 0.0
            or not math.isfinite(record.open_price) or record.open_price <= 0.0
            or not all(math.isfinite(value) for value in (record.sl, record.tp))
        ):
            raise ValueError(f"invalid live order record values: {item}")
        return record

    def _records(self, cmd: str, *, order_records: bool = False) -> list[LiveRecord] | None:
        res = ea_bridge.send_command(cmd, timeout=10)
        if not res or not res.startswith("OK|"):
            return None
        items = res[3:].split("|")
        if not items or re.fullmatch(r"END,[0-9]+", items[-1]) is None:
            return None
        records: list[LiveRecord] = []
        try:
            declared = _strict_int_text(items[-1].split(",", 1)[1])
            parser = self._parse_order_record if order_records else self._parse_record
            for item in items[:-1]:
                if item:
                    records.append(parser(item))
            if declared != len(records):
                return None
        except (TypeError, ValueError, OverflowError):
            return None
        return records

    def get_positions(self, symbol: str, magic: int) -> list[LiveRecord] | None:
        return self._records(f"POSITIONS|{symbol}|{int(magic)}")

    def get_orders(self, symbol: str, magic: int) -> list[LiveRecord] | None:
        return self._records(f"ORDERS|{symbol}|{int(magic)}", order_records=True)

    def get_position(self, ticket: int) -> LiveRecord | None | bool:
        res = ea_bridge.send_command(f"POSITION|{int(ticket)}", timeout=10)
        if res == "ERR|POSITION_NOT_FOUND":
            return False
        if not res or not res.startswith("OK|"):
            return None
        try:
            return self._parse_record(res.split("|", 1)[1])
        except (TypeError, ValueError, OverflowError, IndexError):
            return None

    def confirm_position_absent(self, ticket: int) -> bool | None:
        result = self.get_position(ticket)
        if result is False:
            return True
        if result is None:
            return None
        return False

    def get_position_close_deal(self, position_id: int, opened_at_epoch: int) -> PositionCloseDeal | bool | None:
        res = ea_bridge.send_command(f"CLOSEDEAL|{int(position_id)}|{int(opened_at_epoch)}", timeout=10)
        if not res or not res.startswith("OK|"):
            return None
        parts = res.split("|")
        if len(parts) == 2 and parts[1] == "NONE":
            return False
        if len(parts) != 14 or parts[1] != "FOUND":
            return None
        try:
            deal = PositionCloseDeal(
                deal=_strict_int_text(parts[2]), position_id=_strict_int_text(parts[3]), symbol=parts[4], magic=_strict_int_text(parts[5]), reason=parts[6],
                price=float(parts[7]), profit=float(parts[8]), commission=float(parts[9]), swap=float(parts[10]),
                fee=float(parts[11]), deal_time=_strict_int_text(parts[12]), exit_volume=float(parts[13]),
            )
            if (
                deal.deal <= 0 or deal.position_id != int(position_id) or not deal.symbol
                or not math.isfinite(deal.price) or deal.price <= 0.0
                or not all(math.isfinite(value) for value in (deal.profit, deal.commission, deal.swap, deal.fee, deal.exit_volume))
                or deal.deal_time <= 0 or deal.exit_volume <= 0.0
            ):
                return None
            return deal
        except (TypeError, ValueError, OverflowError):
            return None

    def open_position(
        self, symbol: str, order_type: int, lot: float, sl: float, tp: float, *,
        deviation: int, magic: int, comment: str, digits: int,
        expected_login: int, expected_server: str, expected_owned_positions: int,
    ) -> int | None:
        self.last_order_error = None
        self.last_open_identifier = None
        self.last_open_deal = None
        self.last_open_price = None
        self.last_open_time = None
        try:
            lot_value = float(lot)
            sl_value = float(sl)
            tp_value = float(tp)
            digits_value = int(digits)
            deviation_value = int(deviation)
            magic_value = int(magic)
            expected_login_value = int(expected_login)
            expected_server_value = str(expected_server)
            expected_owned_positions_value = int(expected_owned_positions)
            request_valid = (
                bool(symbol) and "|" not in str(symbol) and "," not in str(symbol)
                and int(order_type) in {ORDER_TYPE_BUY, ORDER_TYPE_SELL}
                and math.isfinite(lot_value) and lot_value > 0.0
                and math.isfinite(sl_value) and sl_value >= 0.0
                and math.isfinite(tp_value) and tp_value >= 0.0
                and sl_value == 0.0 and tp_value == 0.0
                and 0 <= digits_value <= 10 and deviation_value >= 0 and magic_value > 0
                and expected_login_value > 0 and expected_server_value
                and all(token not in expected_server_value for token in ("|", ",", "\r", "\n"))
                and 0 <= expected_owned_positions_value < 8
            )
        except (TypeError, ValueError, OverflowError):
            request_valid = False
        if not request_valid:
            self.last_order_error = "INVALID_OPEN_REQUEST"
            return None
        safe_comment = str(comment).replace("|", "_").replace(",", "_")[:31]
        if (
            str(symbol) != "XAUUSD"
            or not math.isclose(lot_value, 0.01, rel_tol=0.0, abs_tol=1e-12)
            or deviation_value != 50
            or magic_value != S24_CORE_MAGIC
            or not _valid_core_comment(safe_comment, allow_legacy=False)
        ):
            self.last_order_error = "OPEN_POLICY_GUARD"
            return None
        sl_text = f"{sl_value:.{digits_value}f}" if sl_value else "0"
        tp_text = f"{tp_value:.{digits_value}f}" if tp_value else "0"
        res = ea_bridge.send_command(
            f"OPEN|{symbol}|{int(order_type)}|{lot_value:.2f}|{sl_text}|{tp_text}|{magic_value}|{safe_comment}|{deviation_value}|{expected_login_value}|{expected_server_value}|{expected_owned_positions_value}",
            timeout=15,
        )
        if not res or not res.startswith("OK|"):
            self.last_order_error = res or "NO_RESPONSE"
            return None
        parts = res.split("|")
        try:
            if len(parts) != 7:
                raise ValueError(res)
            ticket = _strict_int_text(parts[1])
            identifier = _strict_int_text(parts[2])
            deal = _strict_int_text(parts[3])
            price = float(parts[4])
            open_time = _strict_int_text(parts[5])
            retcode = _strict_int_text(parts[6])
            if ticket <= 0 or identifier <= 0 or deal <= 0 or not math.isfinite(price) or price <= 0.0 or open_time <= 0 or retcode != TRADE_RETCODE_DONE:
                raise ValueError(res)
            self.last_open_identifier = identifier
            self.last_open_deal = deal
            self.last_open_price = price
            self.last_open_time = open_time
            return ticket
        except (TypeError, ValueError, OverflowError, IndexError):
            self.last_order_error = f"MALFORMED_OK:{res}"
            return None

    def open_r1_position(
        self,
        symbol: str,
        order_type: int,
        lot: float,
        fixed_stop: float,
        *,
        deviation: int,
        magic: int,
        comment: str,
        digits: int,
        expected_login: int,
        expected_server: str,
        expected_owned_positions: int,
    ) -> R1OpenResult:
        safe_comment = str(comment).replace("|", "_").replace(",", "_")[:31]
        if S24_V206_POLICY.get(int(magic)) != safe_comment:
            return R1OpenResult("NO_FILL", "ERR|OPEN_R1_POLICY_GUARD", reason="OPEN_R1_POLICY_GUARD")
        try:
            command = build_open_r1_command(
                symbol=symbol,
                order_type=order_type,
                lot=lot,
                fixed_stop=fixed_stop,
                magic=magic,
                comment=safe_comment,
                deviation=deviation,
                expected_login=expected_login,
                expected_server=expected_server,
                expected_owned_positions=expected_owned_positions,
                digits=digits,
            )
        except (TypeError, ValueError, OverflowError):
            return R1OpenResult("NO_FILL", "ERR|INVALID_OPEN_R1_REQUEST", reason="INVALID_OPEN_R1_REQUEST")
        return parse_open_r1_response(ea_bridge.send_command(command, timeout=15))

    def repair_r1_position(
        self,
        *,
        ticket: int,
        expected_login: int,
        expected_server: str,
        expected_symbol: str,
        expected_magic: int,
        expected_comment: str,
        expected_identifier: int,
    ) -> R1RepairResult:
        if S24_V206_POLICY.get(int(expected_magic)) != str(expected_comment):
            return R1RepairResult(False, "FAILED", "ERR|REPAIR_R1_POLICY_GUARD")
        try:
            command = build_repair_r1_command(
                ticket=ticket,
                expected_login=expected_login,
                expected_server=expected_server,
                expected_symbol=expected_symbol,
                expected_magic=expected_magic,
                expected_comment=expected_comment,
                expected_identifier=expected_identifier,
            )
        except (TypeError, ValueError, OverflowError):
            return R1RepairResult(False, "FAILED", "ERR|INVALID_REPAIR_R1_REQUEST")
        return parse_repair_r1_response(ea_bridge.send_command(command, timeout=15))

    def close_r1_position(
        self,
        *,
        ticket: int,
        deviation: int,
        expected_login: int,
        expected_server: str,
        expected_symbol: str,
        expected_magic: int,
        expected_comment: str,
        expected_identifier: int,
    ) -> R1CloseResult:
        if S24_V206_POLICY.get(int(expected_magic)) != str(expected_comment):
            return R1CloseResult(False, "CLOSE_R1_POLICY_GUARD", "ERR|CLOSE_R1_POLICY_GUARD")
        try:
            command = build_close_r1_command(
                ticket=ticket,
                deviation=deviation,
                expected_login=expected_login,
                expected_server=expected_server,
                expected_symbol=expected_symbol,
                expected_magic=expected_magic,
                expected_comment=expected_comment,
                expected_identifier=expected_identifier,
            )
        except (TypeError, ValueError, OverflowError):
            return R1CloseResult(False, "INVALID_REQUEST", "ERR|INVALID_CLOSE_R1_REQUEST")
        return parse_close_r1_response(ea_bridge.send_command(command, timeout=15))

    def close_position(
        self,
        ticket: int,
        deviation: int = 20,
        *,
        expected_login: int,
        expected_server: str,
        expected_symbol: str,
        expected_magic: int,
        expected_comment: str,
        expected_identifier: int,
        expected_type: int,
        expected_volume: float,
    ) -> CloseResult:
        try:
            ticket_value = int(ticket)
            deviation_value = int(deviation)
            expected_login_value = int(expected_login)
            expected_server_value = str(expected_server)
            expected_symbol_value = str(expected_symbol)
            expected_magic_value = int(expected_magic)
            expected_comment_value = str(expected_comment)
            expected_identifier_value = int(expected_identifier)
            expected_type_value = int(expected_type)
            expected_volume_value = float(expected_volume)
        except (TypeError, ValueError, OverflowError):
            return CloseResult(False, "INVALID_REQUEST")
        if (
            ticket_value <= 0 or deviation_value != 50 or expected_login_value <= 0
            or not expected_server_value or any(token in expected_server_value for token in ("|", ",", "\r", "\n"))
            or expected_symbol_value != "XAUUSD" or expected_magic_value != S24_CORE_MAGIC
            or not _valid_core_comment(expected_comment_value, allow_legacy=True)
            or expected_identifier_value <= 0 or expected_type_value not in {ORDER_TYPE_BUY, ORDER_TYPE_SELL}
            or not math.isfinite(expected_volume_value)
            or not math.isclose(expected_volume_value, 0.01, rel_tol=0.0, abs_tol=1e-12)
        ):
            return CloseResult(False, "INVALID_REQUEST")
        res = ea_bridge.send_command(
            f"CLOSE|{ticket_value}|{deviation_value}|{expected_login_value}|{expected_server_value}|{expected_symbol_value}|{expected_magic_value}|{expected_comment_value}|{expected_identifier_value}|{expected_type_value}|{expected_volume_value:.2f}",
            timeout=15,
        )
        if res and res.startswith("OK|"):
            parts = res.split("|")
            try:
                if len(parts) != 8:
                    raise ValueError(res)
                response_ticket = _strict_int_text(parts[1])
                lot, open_price, close_price, profit = map(float, parts[2:6])
                deal_id = _strict_int_text(parts[6])
                retcode = _strict_int_text(parts[7])
                if (
                    response_ticket != ticket_value
                    or not all(math.isfinite(value) for value in (lot, open_price, close_price, profit))
                    or lot <= 0.0 or open_price <= 0.0 or close_price <= 0.0
                    or deal_id <= 0 or retcode != TRADE_RETCODE_DONE
                ):
                    raise ValueError(res)
                return CloseResult(
                    True, "CONFIRMED", lot, open_price, close_price, profit,
                    deal_id=deal_id, retcode=retcode, raw_response=res,
                )
            except (TypeError, ValueError, OverflowError, IndexError):
                return CloseResult(False, "MALFORMED_OK", raw_response=res or "")
        if res == "ERR|POSITION_NOT_FOUND":
            return CloseResult(False, "MISSING_UNCONFIRMED", raw_response=res or "")
        guard_statuses = {
            "ERR|ACCOUNT_IDENTITY_GUARD": "ACCOUNT_IDENTITY_GUARD",
            "ERR|ACCOUNT_MODE_GUARD": "ACCOUNT_MODE_GUARD",
            "ERR|TRADE_PERMISSION_GUARD": "TRADE_PERMISSION_GUARD",
            "ERR|POSITION_OWNERSHIP_GUARD": "POSITION_OWNERSHIP_GUARD",
            "ERR|CLOSE_POLICY_GUARD": "CLOSE_POLICY_GUARD",
        }
        if res in guard_statuses:
            return CloseResult(False, guard_statuses[res], raw_response=res)
        if res in {
            "ERR|COMMAND_BUSY", "ERR|CLAIM_BUSY", "ERR|LOCK_TIMEOUT", "ERR|WRITE_FAILED",
            "ERR|CLAIM_FAILED", "ERR|REQUEST_EXPIRED", "ERR|RESPONSE_BUSY", "ERR|INVALID_TIMEOUT",
        }:
            return CloseResult(False, "IPC_NOT_PUBLISHED", raw_response=res or "")
        retcode = None
        no_deal_rejection = False
        match = re.fullmatch(r"ERR\|([0-9]+)\|DEAL=([0-9]+)\|LAST=([0-9]+)", str(res or ""))
        if match is not None:
            try:
                retcode = int(match.group(1))
                no_deal_rejection = int(match.group(2)) == 0
            except (TypeError, ValueError, OverflowError):
                retcode = None
                no_deal_rejection = False
        if no_deal_rejection and retcode == 10018:
            status = "MARKET_CLOSED"
        elif no_deal_rejection and retcode in {10026, 10027}:
            status = "TRADE_PERMISSION_GUARD"
        else:
            status = "FAILED"
        return CloseResult(False, status, retcode=retcode, raw_response=res or "")
