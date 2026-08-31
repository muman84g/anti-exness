"""UTC admission blocks resolved from market DST; never an exit clock."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


UTC = timezone.utc
EU_TIMEZONE = ZoneInfo("Europe/London")
US_TIMEZONE = ZoneInfo("America/New_York")

FIXED_START_UTC_MINUTE = 4 * 60
EU_END_UTC_MINUTE = {True: 6 * 60 + 30, False: 7 * 60 + 30}
US_PREOPEN_UTC_MINUTE = {True: 11 * 60 + 30, False: 12 * 60 + 30}
US_LATE_END_UTC_MINUTE = {True: 20 * 60 + 30, False: 21 * 60 + 30}


@dataclass(frozen=True)
class EntryAdmissionBlock:
    id: str
    start_utc: str
    end_utc: str
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


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def classify_entry_admission(at_utc: datetime) -> EntryAdmissionBlock | None:
    """Classify a new-entry instant; this result must not govern open positions."""
    instant = _as_utc(at_utc)
    eu_summer = is_eu_summer_time(instant)
    us_summer = is_us_summer_time(instant)
    minute = instant.hour * 60 + instant.minute
    # Runtime comparison is UTC-only. London and New York are consulted only
    # to select their independently DST-resolved UTC boundaries.
    pre_eu_end = EU_END_UTC_MINUTE[eu_summer]
    us_preopen = US_PREOPEN_UTC_MINUTE[us_summer]
    us_late_end = US_LATE_END_UTC_MINUTE[us_summer]

    if FIXED_START_UTC_MINUTE <= minute < pre_eu_end:
        return EntryAdmissionBlock(
            "jst1300_pre_eu30", _hhmm(FIXED_START_UTC_MINUTE), _hhmm(pre_eu_end),
            eu_summer, us_summer,
        )
    if pre_eu_end <= minute < us_preopen:
        return EntryAdmissionBlock(
            "eu_open_to_us_preopen",
            _hhmm(pre_eu_end),
            _hhmm(us_preopen),
            eu_summer,
            us_summer,
        )
    if us_preopen <= minute < us_late_end:
        return EntryAdmissionBlock(
            "us_to_eu_late",
            _hhmm(us_preopen),
            _hhmm(us_late_end),
            eu_summer,
            us_summer,
        )
    return None
