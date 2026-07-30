# -*- coding: utf-8 -*-
"""Read/write executor template.

Runner safety code should use read-only methods for preflight and sync. OPEN is
available only for an explicitly authorized live bot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ea_bridge import ea_bridge


ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1


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
    comment: str


class MT5Executor:
    def get_bridge_capabilities(self) -> dict[str, Any] | None:
        res = ea_bridge.send_command("CAPS|", timeout=10)
        if not res.startswith("OK|CAPS|"):
            return None
        parts = res.split("|")
        return {"name": parts[2], "version": parts[3], "commands": set(parts[4].split(","))}

    def get_symbol_info(self, symbol: str) -> SymbolInfo | None:
        res = ea_bridge.send_command(f"INFO|{symbol}", timeout=10)
        if not res.startswith("OK|"):
            return None
        parts = res.split("|")
        if len(parts) < 13:
            return None
        return SymbolInfo(
            ask=float(parts[1]),
            bid=float(parts[2]),
            point=float(parts[4]),
            volume_min=float(parts[5]),
            volume_max=float(parts[6]),
            volume_step=float(parts[7]),
            digits=int(float(parts[11])),
            stops_level=int(float(parts[12])),
        )

    def _records(self, cmd: str) -> list[LiveRecord] | None:
        res = ea_bridge.send_command(cmd, timeout=10)
        if res == "OK":
            return []
        if not res.startswith("OK|"):
            return None
        records = []
        for item in res[3:].split("|"):
            parts = [part.strip() for part in item.split(",")]
            if len(parts) < 11:
                continue
            records.append(
                LiveRecord(
                    ticket=int(parts[0]),
                    symbol=parts[1],
                    type=int(parts[2]),
                    volume=float(parts[3]),
                    open_price=float(parts[4]),
                    sl=float(parts[5]),
                    tp=float(parts[6]),
                    profit=float(parts[7]),
                    magic=int(parts[8]),
                    open_time=int(float(parts[9])),
                    comment=parts[10],
                )
            )
        return records

    def get_positions(self, symbol: str, magic: int) -> list[LiveRecord] | None:
        return self._records(f"POSITIONS|{symbol}|{int(magic)}")

    def get_orders(self, symbol: str, magic: int) -> list[LiveRecord] | None:
        return self._records(f"ORDERS|{symbol}|{int(magic)}")

    def open_position(
        self,
        symbol: str,
        order_type: int,
        lot: float,
        sl: float,
        tp: float,
        *,
        deviation: int,
        magic: int,
        comment: str,
        digits: int,
    ) -> int | None:
        side = "BUY" if order_type == ORDER_TYPE_BUY else "SELL"
        res = ea_bridge.send_command(
            f"OPEN|{symbol}|{side}|{lot:.2f}|{sl:.{digits}f}|{tp:.{digits}f}|{int(deviation)}|{int(magic)}|{comment}",
            timeout=15,
        )
        if not res.startswith("OK|"):
            return None
        parts = res.split("|")
        try:
            return int(parts[-1])
        except (ValueError, IndexError):
            return None

    def close_position(self, ticket: int, deviation: int = 20) -> bool:
        res = ea_bridge.send_command(f"CLOSE|{int(ticket)}|{int(deviation)}", timeout=15)
        return bool(res.startswith("OK|") or res == "OK")
