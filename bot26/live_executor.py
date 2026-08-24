# -*- coding: utf-8 -*-
"""Ownership-aware file-IPC executor used by generated live bots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ea_bridge import ea_bridge


ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
HEDGING_MARGIN_MODE = 2
REQUIRED_SHARED_ACCOUNT_COMMANDS = {
    "ECHO", "CAPS", "ACCOUNT", "INFO", "HIST", "OPEN", "POSITIONS",
    "POSITION", "ORDERS", "CLOSEDEAL", "MODIFY", "CLOSE",
}


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

    def __bool__(self) -> bool:
        return self.success


class MT5Executor:
    def __init__(self) -> None:
        self.last_order_error: str | None = None

    def get_bridge_capabilities(self) -> dict[str, Any] | None:
        res = ea_bridge.send_command("CAPS|", timeout=10)
        if not res or not res.startswith("OK|CAPS|"):
            return None
        parts = res.split("|", 4)
        if len(parts) < 5:
            return None
        return {"name": parts[2], "version": parts[3], "commands": {x.strip().upper() for x in parts[4].split(",") if x.strip()}}

    def get_account_info(self) -> dict[str, Any] | None:
        res = ea_bridge.send_command("ACCOUNT|", timeout=10)
        if not res or not res.startswith("OK|"):
            return None
        parts = res.split("|")
        if len(parts) < 7:
            return None
        try:
            return {
                "margin_mode": int(parts[1]), "margin_mode_name": parts[2],
                "account_trade_allowed": bool(int(parts[3])), "account_trade_expert": bool(int(parts[4])),
                "terminal_trade_allowed": bool(int(parts[5])), "mql_trade_allowed": bool(int(parts[6])),
            }
        except (TypeError, ValueError):
            return None

    def get_symbol_info(self, symbol: str) -> SymbolInfo | None:
        res = ea_bridge.send_command(f"INFO|{symbol}", timeout=10)
        if not res or not res.startswith("OK|"):
            return None
        parts = res.split("|")
        if len(parts) < 13:
            return None
        try:
            return SymbolInfo(
                ask=float(parts[1]), bid=float(parts[2]), point=float(parts[4]),
                volume_min=float(parts[5]), volume_max=float(parts[6]), volume_step=float(parts[7]),
                digits=int(float(parts[11])), stops_level=int(float(parts[12])),
            )
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_record(item: str) -> LiveRecord:
        parts = [part.strip() for part in item.split(",")]
        if len(parts) == 11:
            parts.insert(10, parts[0])
        if len(parts) < 12:
            raise ValueError(f"invalid live record: {item}")
        return LiveRecord(
            ticket=int(parts[0]), symbol=parts[1], type=int(parts[2]), volume=float(parts[3]),
            open_price=float(parts[4]), sl=float(parts[5]), tp=float(parts[6]), profit=float(parts[7]),
            magic=int(parts[8]), open_time=int(float(parts[9])), identifier=int(parts[10]), comment=parts[11],
        )

    def _records(self, cmd: str) -> list[LiveRecord] | None:
        res = ea_bridge.send_command(cmd, timeout=10)
        if res == "OK":
            return []
        if not res or not res.startswith("OK|"):
            return None
        records: list[LiveRecord] = []
        try:
            for item in res[3:].split("|"):
                if item:
                    records.append(self._parse_record(item))
        except (TypeError, ValueError):
            return None
        return records

    def get_positions(self, symbol: str, magic: int) -> list[LiveRecord] | None:
        return self._records(f"POSITIONS|{symbol}|{int(magic)}")

    def get_orders(self, symbol: str, magic: int) -> list[LiveRecord] | None:
        return self._records(f"ORDERS|{symbol}|{int(magic)}")

    def get_position(self, ticket: int) -> LiveRecord | None | bool:
        res = ea_bridge.send_command(f"POSITION|{int(ticket)}", timeout=10)
        if res in {"ERR|POSITION_NOT_FOUND", "ERR|Position Not Found", "ERR|0", "ERR|10009"}:
            return False
        if not res or not res.startswith("OK|"):
            return None
        try:
            return self._parse_record(res.split("|", 1)[1])
        except (TypeError, ValueError, IndexError):
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
        if len(parts) >= 2 and parts[1] == "NONE":
            return False
        if len(parts) < 13 or parts[1] != "FOUND":
            return None
        try:
            return PositionCloseDeal(
                deal=int(parts[2]), position_id=int(parts[3]), symbol=parts[4], magic=int(parts[5]), reason=parts[6],
                price=float(parts[7]), profit=float(parts[8]), commission=float(parts[9]), swap=float(parts[10]),
                fee=float(parts[11]), deal_time=int(parts[12]),
            )
        except (TypeError, ValueError):
            return None

    def open_position(
        self, symbol: str, order_type: int, lot: float, sl: float, tp: float, *,
        deviation: int, magic: int, comment: str, digits: int,
    ) -> int | None:
        self.last_order_error = None
        safe_comment = str(comment).replace("|", "_").replace(",", "_")[:31]
        sl_text = f"{float(sl):.{digits}f}" if sl else "0"
        tp_text = f"{float(tp):.{digits}f}" if tp else "0"
        res = ea_bridge.send_command(
            f"OPEN|{symbol}|{int(order_type)}|{float(lot):.2f}|{sl_text}|{tp_text}|{int(magic)}|{safe_comment}|{int(deviation)}",
            timeout=15,
        )
        if not res or not res.startswith("OK|"):
            self.last_order_error = res or "NO_RESPONSE"
            return None
        parts = res.split("|")
        try:
            if len(parts) < 5:
                raise ValueError(res)
            ticket, deal, price = int(parts[1]), int(parts[2]), float(parts[3])
            if ticket <= 0 or deal <= 0 or price <= 0:
                raise ValueError(res)
            return ticket
        except (TypeError, ValueError, IndexError):
            self.last_order_error = f"MALFORMED_OK:{res}"
            return None

    def close_position(self, ticket: int, deviation: int = 20) -> CloseResult:
        res = ea_bridge.send_command(f"CLOSE|{int(ticket)}|{int(deviation)}", timeout=15)
        if res and res.startswith("OK|"):
            parts = res.split("|")
            try:
                if len(parts) < 8:
                    raise ValueError(res)
                return CloseResult(True, "CONFIRMED", float(parts[2]), float(parts[3]), float(parts[4]), float(parts[5]))
            except (TypeError, ValueError, IndexError):
                return CloseResult(False, "MALFORMED_OK")
        if res in {"ERR|POSITION_NOT_FOUND", "ERR|Position Not Found", "ERR|0", "ERR|10009"}:
            return CloseResult(False, "MISSING_UNCONFIRMED")
        return CloseResult(False, str(res or "NO_RESPONSE"))
