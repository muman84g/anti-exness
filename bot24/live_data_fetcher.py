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
        for item in res[3:].split("|"):
            parts = [part.strip() for part in item.split(",")]
            if len(parts) < 6:
                continue
            try:
                rows.append(
                    {
                        "time": parts[0],
                        "Open": float(parts[1]),
                        "High": float(parts[2]),
                        "Low": float(parts[3]),
                        "Close": float(parts[4]),
                        "Volume": int(float(parts[5])),
                    }
                )
            except ValueError:
                continue
        if not rows:
            return None
        df = pd.DataFrame(rows)
        try:
            idx = pd.DatetimeIndex(pd.to_datetime(df["time"], format="%Y.%m.%d %H:%M"))
        except ValueError:
            idx = pd.DatetimeIndex(pd.to_datetime(df["time"]))
        df.index = idx
        bars = df[["Open", "High", "Low", "Close", "Volume"]]
        return normalize_hist_bars(
            bars,
            drop_latest=drop_latest,
            configured_timezone=broker_timezone,
            options=self.safety_options,
        )
