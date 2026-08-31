# -*- coding: utf-8 -*-
"""MT5 HIST fetcher template."""

from __future__ import annotations

from typing import Any
import math
import re

import pandas as pd

from ea_bridge import ea_bridge
from live_safety import LiveSafetyOptions, normalize_hist_bars


class MT5DataManager:
    def __init__(self, safety_options: LiveSafetyOptions | None = None):
        self.safety_options = safety_options or LiveSafetyOptions()

    def connect(self) -> bool:
        try:
            ea_bridge.start_server()
        except Exception:
            return False
        return bool(ea_bridge.send_command("ECHO|", timeout=5).startswith("OK|"))

    def get_historical_data(
        self,
        mt5_symbol: str,
        timeframe: int,
        num_bars: int,
        broker_timezone: str = "UTC",
        *,
        drop_latest: bool = False,
    ) -> pd.DataFrame | None:
        res = ea_bridge.send_command(f"HIST|{mt5_symbol}|{int(timeframe)}|{int(num_bars)}", timeout=10)
        return self._parse_history_response(
            res,
            broker_timezone,
            drop_latest=drop_latest,
            expected_rows=int(num_bars),
        )

    def get_historical_page(
        self,
        mt5_symbol: str,
        timeframe: int,
        start_pos: int,
        num_bars: int,
        broker_timezone: str = "UTC",
    ) -> pd.DataFrame | None:
        """Fetch a bounded MT5 CopyRates page without changing legacy HIST."""
        res = ea_bridge.send_command(
            f"HISTPAGE|{mt5_symbol}|{int(timeframe)}|{int(start_pos)}|{int(num_bars)}",
            timeout=10,
        )
        return self._parse_history_response(
            res,
            broker_timezone,
            drop_latest=False,
            strict_rows=True,
            expected_rows=int(num_bars),
        )

    def _parse_history_response(
        self,
        res: str | None,
        broker_timezone: str,
        *,
        drop_latest: bool,
        strict_rows: bool = False,
        expected_rows: int | None = None,
    ) -> pd.DataFrame | None:
        if not res or not res.startswith("OK|"):
            return None
        rows: list[dict[str, Any]] = []
        for item in res[3:].split("|"):
            parts = item.split(",")
            if len(parts) != 6 or any(part != part.strip() for part in parts):
                return None
            try:
                volume_text = parts[5]
                if not re.fullmatch(r"[0-9]+", volume_text):
                    raise ValueError(f"invalid integer volume: {volume_text!r}")
                open_price = float(parts[1])
                high_price = float(parts[2])
                low_price = float(parts[3])
                close_price = float(parts[4])
                volume = int(volume_text)
                if (
                    not all(
                        math.isfinite(value)
                        for value in (open_price, high_price, low_price, close_price)
                    )
                    or min(open_price, high_price, low_price, close_price) <= 0.0
                    or high_price < max(open_price, low_price, close_price)
                    or low_price > min(open_price, high_price, close_price)
                    or volume < 0
                ):
                    raise ValueError("invalid OHLCV row")
                rows.append(
                    {
                        "time": parts[0],
                        "Open": open_price,
                        "High": high_price,
                        "Low": low_price,
                        "Close": close_price,
                        "Volume": volume,
                    }
                )
            except ValueError:
                return None
        if not rows:
            return None
        if expected_rows is not None and (
            expected_rows <= 0 or len(rows) != int(expected_rows)
        ):
            return None
        df = pd.DataFrame(rows)
        try:
            idx = pd.DatetimeIndex(pd.to_datetime(df["time"], format="%Y.%m.%d %H:%M"))
        except (TypeError, ValueError, OverflowError):
            return None
        if idx.has_duplicates or not idx.is_monotonic_increasing:
            return None
        df.index = idx
        bars = df[["Open", "High", "Low", "Close", "Volume"]]
        return normalize_hist_bars(
            bars,
            drop_latest=drop_latest,
            configured_timezone=broker_timezone,
            options=self.safety_options,
        )
