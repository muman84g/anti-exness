"""Position-owned deadlines that are independent of entry-session calendars."""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd


def fixed_hold_due_at(entry_times_utc: Iterable[pd.Timestamp], hold_minutes: int) -> pd.Timestamp:
    """Return an absolute UTC deadline from confirmed fills and elapsed minutes."""
    entries: list[pd.Timestamp] = []
    for value in entry_times_utc:
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is None:
            raise ValueError("position entry time must be timezone-aware")
        entries.append(stamp.tz_convert("UTC"))
    if not entries:
        raise ValueError("at least one confirmed position entry time is required")
    if int(hold_minutes) < 0:
        raise ValueError("hold_minutes must be non-negative")
    return min(entries) + pd.Timedelta(minutes=int(hold_minutes))
