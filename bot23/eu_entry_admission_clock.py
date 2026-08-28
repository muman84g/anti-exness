"""Market-DST-aware admission blocks; never a position-exit clock."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


UTC = timezone.utc
JST = timezone(timedelta(hours=9), name="JST")
EU_TIMEZONE = ZoneInfo("Europe/London")
US_TIMEZONE = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class EntryAdmissionBlock:
    id: str
    start_jst: str
    end_jst: str
    eu_summer_time: bool
    us_summer_time: bool


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("session clock requires a timezone-aware datetime")
    return value.astimezone(UTC)


def is_eu_summer_time(at_utc: datetime) -> bool:
    """Return whether London observes daylight-saving time at this instant."""
    london = _as_utc(at_utc).astimezone(EU_TIMEZONE)
    return bool(london.dst())


def is_us_summer_time(at_utc: datetime) -> bool:
    """Return whether New York observes daylight-saving time at this instant."""
    new_york = _as_utc(at_utc).astimezone(US_TIMEZONE)
    return bool(new_york.dst())


def classify_entry_admission(at_utc: datetime) -> EntryAdmissionBlock | None:
    """Classify a new-entry instant; this result must not govern open positions."""
    instant = _as_utc(at_utc)
    eu_summer = is_eu_summer_time(instant)
    us_summer = is_us_summer_time(instant)
    minute = instant.astimezone(JST).hour * 60 + instant.astimezone(JST).minute
    # London governs the European boundary. New York independently governs
    # the US boundaries because their DST change dates differ for several
    # weeks each year.
    pre_eu_end = 15 * 60 + 30 + (0 if eu_summer else 60)
    us_preopen = 20 * 60 + 30 + (0 if us_summer else 60)
    us_late_end = 5 * 60 + 30 + (0 if us_summer else 60)

    if 13 * 60 <= minute < pre_eu_end:
        return EntryAdmissionBlock(
            "jst1300_pre_eu30", "13:00", "15:30" if eu_summer else "16:30",
            eu_summer, us_summer,
        )
    if pre_eu_end <= minute < us_preopen:
        return EntryAdmissionBlock(
            "eu_open_to_us_preopen",
            "15:30" if eu_summer else "16:30",
            "20:30" if us_summer else "21:30",
            eu_summer,
            us_summer,
        )
    if minute >= us_preopen or minute < us_late_end:
        return EntryAdmissionBlock(
            "us_to_eu_late",
            "20:30" if us_summer else "21:30",
            "05:30" if us_summer else "06:30",
            eu_summer,
            us_summer,
        )
    return None
