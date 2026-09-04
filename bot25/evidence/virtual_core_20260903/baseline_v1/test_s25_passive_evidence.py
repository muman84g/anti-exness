# -*- coding: utf-8 -*-
"""No-order regression tests for bot25 passive evidence modules."""

from __future__ import annotations

import csv
import tempfile
from pathlib import Path
from unittest.mock import patch

from shadow_opportunity_observer import (
    MARKOUT_FIELDS,
    OPPORTUNITY_FIELDS,
    S25ShadowOpportunityObserver,
)
from shadow_state_tagger import S25ShadowStateTagger, TAG_FIELDS
from passive_evidence_io import append_durable_csv


def _payload(opportunity_id: str, side: str, at: str) -> dict[str, object]:
    return {
        "opportunity_id": opportunity_id,
        "symbol": "XAUUSD",
        "opportunity_type": "frontier_add",
        "side": side,
        "signal_bar_time": "2026-08-28T00:00:00+00:00",
        "registered_at": at,
        "entry_bid": 100.0,
        "entry_ask": 100.2,
        "spread_price": 0.2,
        "spread_points": 200.0,
        "lot": 0.01,
        "contract_size": 100.0,
        "episode_id": "episode-1",
        "active_wave": 1 if side == "LONG" else -1,
        "atr14": 2.0,
        "ema200": 99.0,
        "ema_distance_atr": 0.3,
        "frontier": 99.0 if side == "LONG" else 101.0,
        "frontier_distance_atr": 0.5,
        "long_positions": 1,
        "short_positions": 2,
        "side_imbalance": -1,
        "episode_age_minutes": 30.0,
        "minutes_since_productive_close": 130.0,
        "inventory_mtm_usd": -2.0,
        "core_positions": 2,
        "satellite_positions": 1,
        "capacity_allowed": True,
        "ratio_allowed": True,
        "v23_allowed": False,
        "execution_allowed": True,
        "bar_open": 99.0,
        "bar_high": 101.0,
        "bar_low": 98.0,
        "bar_close": 100.5,
        "bar_volume": 123,
        "break_dir": 1 if side == "LONG" else -1,
    }


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="s25-passive-test-") as temp:
        root = Path(temp)
        logs = root / "logs"
        state = root / "state"
        cfg = {
            "enabled": True,
            "horizons_minutes": [1, 5],
            "completed_id_retention_days": 14,
            "opportunity_csv": "opportunities.csv",
            "markout_csv": "markouts.csv",
            "state_file": "observer.json",
        }
        observer = S25ShadowOpportunityObserver(cfg, log_dir=str(logs), state_dir=str(state))
        long_payload = _payload("opp-long", "LONG", "2026-08-28T00:00:00+00:00")
        assert observer.register_opportunity(long_payload)
        assert not observer.register_opportunity(long_payload)
        assert observer.record_route(
            "opp-long",
            status="unconsumed",
            reason="v23_drought_minority_add_pause",
            at="2026-08-28T00:00:01+00:00",
        )
        assert observer.observe_quote(
            at="2026-08-28T00:01:02+00:00",
            bid=101.0,
            ask=101.2,
        ) == 1
        first_markout = _rows(logs / "markouts.csv")[0]
        assert abs(float(first_markout["pnl_price"]) - 0.8) < 1e-12
        assert abs(float(first_markout["mae_price"]) + 0.2) < 1e-12
        assert first_markout["route_status"] == "unconsumed"
        assert observer.observe_quote(
            at="2026-08-28T00:05:03+00:00",
            bid=99.0,
            ask=99.2,
        ) == 1
        assert len(_rows(logs / "markouts.csv")) == 2

        restarted = S25ShadowOpportunityObserver(cfg, log_dir=str(logs), state_dir=str(state))
        assert not restarted.register_opportunity(long_payload)
        assert restarted.observe_quote(
            at="2026-08-28T00:06:00+00:00",
            bid=102.0,
            ask=102.2,
        ) == 0
        assert len(_rows(logs / "markouts.csv")) == 2

        short_payload = _payload("opp-short", "SHORT", "2026-08-28T01:00:00+00:00")
        assert restarted.register_opportunity(short_payload)
        restarted.record_route("opp-short", status="consumed", reason="frontier_add_confirmed", at="2026-08-28T01:00:01+00:00")
        restarted.observe_quote(at="2026-08-28T01:01:01+00:00", bid=98.8, ask=99.0)
        short_markout = next(row for row in _rows(logs / "markouts.csv") if row["opportunity_id"] == "opp-short")
        assert abs(float(short_markout["pnl_price"]) - 1.0) < 1e-12

        tagger = S25ShadowStateTagger({"enabled": True, "csv": "tags.csv"}, log_dir=str(logs))
        assert tagger.record(long_payload)
        assert not tagger.record(long_payload)
        tag_row = _rows(logs / "tags.csv")[0]
        assert tag_row["signal_bar_time"] == "2026-08-28T00:00:00+00:00"
        assert abs(float(tag_row["bar_range"]) - 3.0) < 1e-12
        assert abs(float(tag_row["mid_to_ema_atr"]) - 0.55) < 1e-12

        crash_cfg = {**cfg, "opportunity_csv": "crash_opportunities.csv", "markout_csv": "crash_markouts.csv", "state_file": "crash_state.json"}
        crash_observer = S25ShadowOpportunityObserver(crash_cfg, log_dir=str(logs), state_dir=str(state))
        crash_payload = _payload("opp-crash", "LONG", "2026-08-28T01:30:00+00:00")
        try:
            with patch("shadow_opportunity_observer.append_durable_csv", side_effect=OSError("registration-write-crash")):
                crash_observer.register_opportunity(crash_payload)
        except OSError as exc:
            assert str(exc) == "registration-write-crash"
        else:
            raise AssertionError("registration write failure must surface to the fail-open runner wrapper")
        recovered_registration = S25ShadowOpportunityObserver(crash_cfg, log_dir=str(logs), state_dir=str(state))
        assert _rows(logs / "crash_opportunities.csv")[0]["event"] == "registered"
        try:
            with patch("shadow_opportunity_observer.append_durable_csv", side_effect=OSError("route-write-crash")):
                recovered_registration.record_route("opp-crash", status="unconsumed", reason="capacity", at="2026-08-28T01:30:01+00:00")
        except OSError as exc:
            assert str(exc) == "route-write-crash"
        else:
            raise AssertionError("route write failure must surface to the fail-open runner wrapper")
        S25ShadowOpportunityObserver(crash_cfg, log_dir=str(logs), state_dir=str(state))
        crash_rows = _rows(logs / "crash_opportunities.csv")
        assert [row["event"] for row in crash_rows] == ["registered", "route_update"]
        assert crash_rows[-1]["route_reason"] == "capacity"

        bad = logs / "bad_opportunities.csv"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("wrong,header\n1,2\n", encoding="utf-8")
        rollover_cfg = {**cfg, "opportunity_csv": bad.name, "markout_csv": "bad_markouts.csv", "state_file": "bad_state.json"}
        rolled = S25ShadowOpportunityObserver(rollover_cfg, log_dir=str(logs), state_dir=str(state))
        assert rolled.register_opportunity(_payload("opp-rollover", "LONG", "2026-08-28T02:00:00+00:00"))
        rollover_rows = _rows(bad)
        assert rollover_rows[0]["event"] == "schema_rollover"
        assert rollover_rows[1]["event"] == "registered"
        assert list((logs / "old").glob("bad_opportunities_schema_retired_*.csv"))

        fsync_failure = logs / "fsync_failure.csv"
        try:
            with patch("passive_evidence_io.os.fsync", side_effect=OSError("disk-sync-failed")):
                append_durable_csv(fsync_failure, {"event": "test"}, ["event"])
        except OSError as exc:
            assert str(exc) == "disk-sync-failed"
        else:
            raise AssertionError("durable append must surface fsync failure")

        assert _rows(logs / "opportunities.csv")[0].keys() == dict.fromkeys(OPPORTUNITY_FIELDS).keys()
        assert _rows(logs / "markouts.csv")[0].keys() == dict.fromkeys(MARKOUT_FIELDS).keys()
        assert _rows(logs / "tags.csv")[0].keys() == dict.fromkeys(TAG_FIELDS).keys()

    print("s25 passive evidence tests ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
