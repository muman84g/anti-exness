"""Validate synthetic QuoteEvents before any native engine state can advance."""

from datetime import datetime, timedelta, timezone
import re
from typing import Any


PRICE_SCALE = 1_000_000
_DATE_RE = re.compile(r"\d{4}\.\d{2}\.\d{2}\Z")
_TIME_RE = re.compile(r"\d{2}:\d{2}:\d{2}\.\d{3}\Z")
_PRICE_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,6})?\Z")


class QuoteContractError(ValueError):
    pass


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise QuoteContractError(f"{name} must be an integer >= {minimum}")
    return value


def _scaled_price(raw: Any, name: str) -> int:
    if type(raw) is not str or len(raw) > 32 or not _PRICE_RE.fullmatch(raw):
        raise QuoteContractError(f"{name} must be a positive decimal with at most six fractional digits")
    whole, _, fractional = raw.partition(".")
    scaled = int(whole) * PRICE_SCALE + int(fractional.ljust(6, "0"))
    if scaled <= 0:
        raise QuoteContractError(f"{name} must be positive at scale {PRICE_SCALE}")
    return scaled


def _raw_utc_epoch_ms(raw_date: str, raw_time: str) -> int:
    if not _DATE_RE.fullmatch(raw_date) or not _TIME_RE.fullmatch(raw_time):
        raise QuoteContractError("raw date/time must use the synthetic UTC canonical format")
    try:
        value = datetime.strptime(f"{raw_date} {raw_time}", "%Y.%m.%d %H:%M:%S.%f")
    except ValueError as exc:
        raise QuoteContractError("raw date/time is invalid") from exc
    aware = value.replace(tzinfo=timezone.utc)
    delta = aware - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def validate_quote_event(
    event: Any,
    quote_event_type: type,
    *,
    previous_context_index: int | None,
    previous_sequence: int | None,
    previous_epoch_ms: int | None,
) -> None:
    """Pure preflight; it does not touch the core, trace, sink, or connection cursor."""
    if type(event) is not quote_event_type:
        raise QuoteContractError("event must be the QuoteEvent from the pinned core")
    context_index = _integer(event.context_row_index, "context_row_index")
    sequence = _integer(event.source_row_sequence, "source_row_sequence", minimum=1)
    epoch_ms = _integer(event.assumed_utc_epoch_ms, "assumed_utc_epoch_ms", minimum=1)
    bid_micro = _integer(event.bid_micro, "bid_micro", minimum=1)
    ask_micro = _integer(event.ask_micro, "ask_micro", minimum=1)
    _integer(event.flags, "flags")

    if type(event.raw_date) is not str or type(event.raw_time) is not str:
        raise QuoteContractError("raw date and time must be strings")
    if _raw_utc_epoch_ms(event.raw_date, event.raw_time) != epoch_ms:
        raise QuoteContractError("raw UTC date/time and epoch milliseconds disagree")
    if type(event.raw_time_label) is not str or not event.raw_time_label.startswith("synthetic-utc:"):
        raise QuoteContractError("time label must mark a synthetic UTC fixture")
    if _scaled_price(event.raw_bid, "raw_bid") != bid_micro:
        raise QuoteContractError("raw_bid and bid_micro disagree")
    if _scaled_price(event.raw_ask, "raw_ask") != ask_micro:
        raise QuoteContractError("raw_ask and ask_micro disagree")
    if bid_micro > ask_micro:
        raise QuoteContractError("bid must not exceed ask")
    if type(event.raw_last) is not str or type(event.raw_volume) is not str:
        raise QuoteContractError("raw last and volume must be strings")
    if type(event.raw_flags) is not str or event.raw_flags != str(event.flags):
        raise QuoteContractError("raw flags and integer flags disagree")
    if type(event.carried_sides) is not tuple or any(side not in {"bid", "ask"} for side in event.carried_sides):
        raise QuoteContractError("carried_sides must be a tuple of bid/ask labels")
    if len(set(event.carried_sides)) != len(event.carried_sides):
        raise QuoteContractError("carried_sides cannot contain duplicates")

    if previous_context_index is not None and context_index <= previous_context_index:
        raise QuoteContractError("context row index did not increase")
    if previous_sequence is not None and sequence <= previous_sequence:
        raise QuoteContractError("source row sequence did not increase")
    if previous_epoch_ms is not None and epoch_ms < previous_epoch_ms:
        raise QuoteContractError("synthetic UTC timestamp moved backward")


class QuoteConnection:
    def __init__(self, *, engine: Any, adapter: Any, quote_event_type: type, sink: Any, trace: Any) -> None:
        self.engine = engine
        self.adapter = adapter
        self.quote_event_type = quote_event_type
        self.sink = sink
        self.trace = trace
        self.last_context_index: int | None = None
        self.last_sequence: int | None = None
        self.last_epoch_ms: int | None = None

    def on_quote(self, event: Any) -> bool:
        validate_quote_event(
            event,
            self.quote_event_type,
            previous_context_index=self.last_context_index,
            previous_sequence=self.last_sequence,
            previous_epoch_ms=self.last_epoch_ms,
        )
        result = self.adapter.on_tick(event)
        self.last_context_index = event.context_row_index
        self.last_sequence = event.source_row_sequence
        self.last_epoch_ms = event.assumed_utc_epoch_ms
        return result
