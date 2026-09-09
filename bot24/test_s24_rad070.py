from __future__ import annotations

import json
from pathlib import Path
import unittest
from types import SimpleNamespace

import pandas as pd

import live_s24_bot as s24


ROOT = Path(__file__).resolve().parent


class S24RAD070Tests(unittest.TestCase):
    def test_frozen_rad_identity_is_independent(self):
        params = json.loads((ROOT / "s24_params.json").read_text(encoding="utf-8"))
        self.assertIsNone(s24.S24NoAdverseRunner._config_error(params))
        rad = params["strategies"][1]
        self.assertEqual((rad["lane_id"], rad["magic"], rad["comment_prefix"]), (207, 240207, "s24_rad070"))
        self.assertNotEqual(rad["magic"], params["strategies"][0]["magic"])
        self.assertNotEqual(rad["magic"], params["v206_strategy"]["magic"])

    def test_rad_signal_exists_only_on_completed_m30_boundary(self):
        index = pd.date_range("2026-01-01T00:00:00Z", periods=12 * 30, freq="min")
        rows = []
        for minute in range(len(index)):
            block = minute // 30
            price = 2000.0 + block * 4.0 + (minute % 30) * 0.05
            radius = 1.0 + block * 0.2
            rows.append({"Open": price, "High": price + radius, "Low": price - radius, "Close": price + 0.02, "Volume": 1.0})
        enriched = s24.add_features(pd.DataFrame(rows, index=index), 0.001)
        possible = enriched.index[(enriched["rad_long"] | enriched["rad_short"])]
        self.assertTrue(all(ts.minute in {29, 59} for ts in possible))
        non_boundary = ~enriched.index.minute.isin([29, 59])
        self.assertFalse(bool(enriched.loc[non_boundary, ["rad_long", "rad_short"]].to_numpy().any()))

    def test_rad_threshold_and_direction_contract(self):
        row = pd.Series({"spread_points": 100.0, "rad_score": 0.70, "rad_long": True, "rad_short": False})
        side, reason = s24.S24NoAdverseRunner._strategy_signal_decision(
            SimpleNamespace(params={"max_entry_spread_points": 300.0}), row, {"mode": "rad070_m30"}
        )
        self.assertEqual((side, reason), ("LONG", "rad070_long_signal"))


if __name__ == "__main__":
    unittest.main()
