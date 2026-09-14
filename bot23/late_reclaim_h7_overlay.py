"""Frozen raw-tick late-reclaim Long signal used by bot23 lane 24."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import numpy as np
import pandas as pd

from raw_tick_shadow_collector import Cursor, Tick, next_cursor, parse_page

POLICY_ID = "late_reclaim_long_h7"
POLICY_PARAMS = {
    "direction": "LONG", "first45_z_lte": -1.0,
    "reclaim_fraction_gte": 0.5, "range_location_60m_lte": 0.15,
    "cooldown_minutes": 30, "hold_minutes": 7,
}
POLICY_PARAMS_HASH = hashlib.sha256(
    json.dumps(POLICY_PARAMS, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


@dataclass(frozen=True)
class MinuteFeature:
    minute_msc: int
    open_bid: float
    high_bid: float
    low_bid: float
    close_bid: float
    first45_move: float
    last15_move: float


def fetch_ticks(send: Callable[..., str], symbol: str, start_msc: int, end_msc: int) -> list[Tick]:
    cursor = Cursor(start_msc, 0, 0)
    rows: list[Tick] = []
    for _ in range(1000):
        response = send(f"TICKS|{symbol}|{cursor.from_msc}|{end_msc}|2000|{cursor.skip_at_from_msc}", timeout=15)
        page, more = parse_page(response)
        if any(t.time_msc < start_msc or t.time_msc > end_msc for t in page):
            raise ValueError("H7 TICKS response outside requested completed-minute window")
        rows.extend(page)
        if not page or not more:
            break
        cursor = next_cursor(cursor, page[-1])
    else:
        raise RuntimeError("H7 TICKS pagination limit exceeded")
    if not rows or any(rows[i].time_msc > rows[i + 1].time_msc for i in range(len(rows) - 1)):
        raise ValueError("H7 requires non-empty ordered raw ticks")
    return rows


def minute_feature(minute: pd.Timestamp, ticks: list[Tick]) -> MinuteFeature:
    stamp = pd.Timestamp(minute)
    if stamp.tzinfo is None:
        raise ValueError("H7 minute must be timezone-aware")
    start = int(stamp.tz_convert("UTC").timestamp() * 1000)
    end = start + 60_000 - 1
    if any(t.time_msc < start or t.time_msc > end for t in ticks):
        raise ValueError("H7 tick outside completed minute")
    times = np.asarray([t.time_msc for t in ticks], dtype=np.int64)
    bids = np.asarray([t.bid for t in ticks], dtype=np.float64)
    cut = int(np.searchsorted(times, start + 45_000, side="left"))
    if cut <= 0 or cut >= len(ticks):
        raise ValueError("H7 minute lacks first45 or last15 observations")
    return MinuteFeature(start, float(bids[0]), float(bids.max()), float(bids.min()),
                         float(bids[-1]), float(bids[cut - 1] - bids[0]),
                         float(bids[-1] - bids[cut]))


def signal(features: list[MinuteFeature]) -> dict[str, object] | None:
    if len(features) < 62:
        return None
    rows = features[-62:]
    if any(rows[i + 1].minute_msc - rows[i].minute_msc != 60_000 for i in range(61)):
        return None
    closes = np.asarray([r.close_bid for r in rows])
    scale = float(np.std(np.diff(closes[:-1]), ddof=0))
    current = rows[-1]
    high = max(r.high_bid for r in rows[-60:])
    low = min(r.low_bid for r in rows[-60:])
    if scale <= 0 or high <= low or current.first45_move == 0:
        return None
    z = current.first45_move / scale
    reclaim = current.last15_move / abs(current.first45_move)
    location = (current.close_bid - low) / (high - low)
    if z <= -1.0 and reclaim >= 0.5 and location <= 0.15:
        event = pd.Timestamp(current.minute_msc, unit="ms", tz="UTC")
        return {"opportunity_id": f"late-reclaim-h7:{event.isoformat()}",
                "source": "late_reclaim_h7_frozen_v1",
                "event_time": event, "release_time": event + pd.Timedelta(minutes=1),
                "available_time": event + pd.Timedelta(minutes=1),
                "first45_move": current.first45_move,
                "first45_z": z, "reclaim_fraction": reclaim,
                "last15_move": current.last15_move,
                "range_location_60m": location}
    return None
