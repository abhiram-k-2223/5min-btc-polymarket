#!/usr/bin/env python3
"""Unit tests for scripts/btc5m_favorite_band.py (stdlib only)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import btc5m_favorite_band as b


class AllowEntryTest(unittest.TestCase):
    def test_reject_below_band(self):
        self.assertFalse(b.allow_entry(side_gap_usd=80.0, fav_ask=0.49, min_gap_usd=70.0))

    def test_allow_low_edge(self):
        self.assertTrue(b.allow_entry(side_gap_usd=80.0, fav_ask=0.50, min_gap_usd=70.0))

    def test_prefer_zone(self):
        self.assertTrue(b.prefer_zone(0.55))
        self.assertFalse(b.prefer_zone(0.60))

    def test_allow_high_edge(self):
        self.assertTrue(b.allow_entry(side_gap_usd=-90.0, fav_ask=0.70, min_gap_usd=70.0))

    def test_reject_above_band(self):
        self.assertFalse(b.allow_entry(side_gap_usd=90.0, fav_ask=0.71, min_gap_usd=70.0))

    def test_reject_small_gap(self):
        self.assertFalse(b.allow_entry(side_gap_usd=10.0, fav_ask=0.55, min_gap_usd=70.0))

    def test_reject_none_inputs(self):
        self.assertFalse(b.allow_entry(side_gap_usd=None, fav_ask=0.55, min_gap_usd=70.0))
        self.assertFalse(b.allow_entry(side_gap_usd=80.0, fav_ask=None, min_gap_usd=70.0))

    def test_gap_at_threshold_allows(self):
        self.assertTrue(b.allow_entry(side_gap_usd=70.0, fav_ask=0.55, min_gap_usd=70.0))


class SizingTest(unittest.TestCase):
    def test_fixed_ten_default(self):
        self.assertEqual(b.size_for_band(equity_usd=100.0), 10.0)

    def test_capped_at_5pct_above_200(self):
        self.assertEqual(b.size_for_band(equity_usd=1000.0), 10.0)  # 5% = 50, capped at $10
        self.assertEqual(b.size_for_band(equity_usd=100.0, stake_usd=50.0), 10.0)  # <=200: fixed $10
        self.assertAlmostEqual(b.size_for_band(equity_usd=300.0, stake_usd=50.0), 15.0)  # >200: 5% cap binds

    def test_floored_one(self):
        self.assertEqual(b.size_for_band(equity_usd=0.0), 1.0)
        self.assertEqual(b.size_for_band(equity_usd=-50.0), 1.0)
        self.assertEqual(b.size_for_band(equity_usd=None), 10.0)


class NoStopInvariantTest(unittest.TestCase):
    def test_band_has_no_stop_key(self):
        d = b.decision_json(mode="band", side="UP", gap_usd=80.0, fav_ask=0.55, prefer=True, reason="ok")
        self.assertEqual(d["mode"], "band")
        self.assertNotIn("stop_loss_pct", d)
        self.assertNotIn("stop", d)
        self.assertEqual(d["prefer_zone"], True)


class SelectModeTest(unittest.TestCase):
    def test_base_only_never_bands(self):
        self.assertEqual(b.select_mode(entry_mode="base", fav_ask=0.55, side_ask=0.90, band_max_ask=0.70, base_threshold=0.60), "base")

    def test_band_only_skips_when_over_band(self):
        self.assertEqual(b.select_mode(entry_mode="band", fav_ask=0.85, side_ask=0.85, band_max_ask=0.70, base_threshold=0.60), "skip")

    def test_both_prefers_band(self):
        self.assertEqual(b.select_mode(entry_mode="both", fav_ask=0.55, side_ask=0.90, band_max_ask=0.70, base_threshold=0.60), "band")

    def test_both_falls_back_to_base(self):
        self.assertEqual(b.select_mode(entry_mode="both", fav_ask=0.85, side_ask=0.90, band_max_ask=0.70, base_threshold=0.60), "base")


class BandLaneTest(unittest.TestCase):
    """band_lane() gates the threshold exemption (I2) and band candidacy."""

    def test_band_mode_in_band_allows(self):
        self.assertTrue(b.band_lane(entry_mode="band", side_gap_usd=80.0, fav_ask=0.55, min_gap_usd=70.0))

    def test_both_mode_allows(self):
        self.assertTrue(b.band_lane(entry_mode="both", side_gap_usd=-80.0, fav_ask=0.55, min_gap_usd=70.0))

    def test_base_mode_never(self):
        self.assertFalse(b.band_lane(entry_mode="base", side_gap_usd=80.0, fav_ask=0.55, min_gap_usd=70.0))

    def test_gap_failure_closes_lane(self):
        self.assertFalse(b.band_lane(entry_mode="both", side_gap_usd=10.0, fav_ask=0.55, min_gap_usd=70.0))

    def test_out_of_band_closes_lane(self):
        self.assertFalse(b.band_lane(entry_mode="both", side_gap_usd=80.0, fav_ask=0.85, min_gap_usd=70.0))

    def test_loosened_max_ask_opens_lane(self):
        # M1: --band-max-ask above 0.70 must take effect, not silently skip.
        self.assertTrue(b.band_lane(entry_mode="both", side_gap_usd=80.0, fav_ask=0.73, min_gap_usd=70.0, band_max_ask=0.75))
        self.assertFalse(b.band_lane(entry_mode="both", side_gap_usd=80.0, fav_ask=0.73, min_gap_usd=70.0, band_max_ask=0.70))

    def test_none_inputs_close_lane(self):
        self.assertFalse(b.band_lane(entry_mode="both", side_gap_usd=None, fav_ask=0.55, min_gap_usd=70.0))
        self.assertFalse(b.band_lane(entry_mode=None, side_gap_usd=80.0, fav_ask=0.55, min_gap_usd=70.0))


class HedgeSuppressedTest(unittest.TestCase):
    """Band leg never hedges (spec 3a, review I1)."""

    def test_band_suppresses_hedge(self):
        self.assertTrue(b.hedge_suppressed(mode="band"))

    def test_base_and_unknown_do_not(self):
        self.assertFalse(b.hedge_suppressed(mode="base"))
        self.assertFalse(b.hedge_suppressed(mode=None))


if __name__ == "__main__":
    unittest.main()
