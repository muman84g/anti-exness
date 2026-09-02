# -*- coding: utf-8 -*-
"""MT5 HIST fetcher template."""

from __future__ import annotations

from typing import Any

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
        rows: list[dict[str, Any]] = []
        malformed = 0
        for item in res[3:].split("|"):
            parts = item.split(",")
            if len(parts) != 6 or any(part != part.strip() for part in parts):
                malformed += 1
                continue
            try:
                epoch = int(parts[0])
                if epoch <= 0:
                    raise ValueError("nonpositive epoch")
                rows.append(
                    {
                        "time": epoch,
                        "Open": float(parts[1]),
                        "High": float(parts[2]),
                        "Low": float(parts[3]),
                        "Close": float(parts[4]),
                        "Volume": int(float(parts[5])),
                    }
                )
            except (TypeError, ValueError, OverflowError):
                malformed += 1
                continue
        if not rows or malformed:
            return None
        df = pd.DataFrame(rows)
        try:
            idx = pd.DatetimeIndex(pd.to_datetime(df["time"], unit="s", utc=True))
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
