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
        if not res or not res.startswith("OK|"):
            return None
        items = res[3:].split("|")
        if not items or re.fullmatch(r"END,[0-9]+", items[-1]) is None:
            return None
        try:
            declared = int(items[-1].split(",", 1)[1])
        except (TypeError, ValueError, OverflowError):
            return None
        rows: list[dict[str, Any]] = []
        for item in items[:-1]:
            parts = item.split(",")
            if len(parts) != 7 or any(part != part.strip() for part in parts):
                return None
            try:
                prices = [float(value) for value in parts[1:5]]
                if re.fullmatch(r"[0-9]+", parts[5]) is None or re.fullmatch(r"[0-9]+", parts[6]) is None:
                    return None
                volume, epoch = int(parts[5]), int(parts[6])
                if (
                    not all(math.isfinite(value) and value > 0.0 for value in prices)
                    or prices[1] < max(prices[0], prices[3])
                    or prices[2] > min(prices[0], prices[3])
                    or prices[1] < prices[2] or volume < 0 or epoch <= 0
                ):
                    return None
                rows.append({"time": parts[0], "Open": prices[0], "High": prices[1], "Low": prices[2], "Close": prices[3], "Volume": volume, "Epoch": epoch})
            except (TypeError, ValueError, OverflowError):
                return None
        if not rows or declared != len(rows):
            return None
        df = pd.DataFrame(rows)
        if df["Epoch"].duplicated().any() or not df["Epoch"].is_monotonic_increasing:
            return None
        idx = pd.DatetimeIndex(pd.to_datetime(df["Epoch"], unit="s", utc=True))
        df.index = idx
        bars = df[["Open", "High", "Low", "Close", "Volume"]]
        return normalize_hist_bars(
            bars,
            drop_latest=drop_latest,
            configured_timezone=broker_timezone,
            options=self.safety_options,
        )
