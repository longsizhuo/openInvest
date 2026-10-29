"""Known-answer, semantic-boundary, leakage, and dependence checks."""

from datetime import date, timedelta
import itertools
import math
import unittest

from experiments.decision_evidence.scoring import (
    brier, interval_score, paired_day_bootstrap, score_records,
)


_RECORD_IDS = itertools.count()


def event_record(**updates):
    row = {
        "record_id": f"test-{next(_RECORD_IDS)}",
        "event_id": "return_negative_v1", "horizon": "30_calendar_days",
        "probability": 0.8, "outcome": True,
        "forecast_at": "2026-01-01T10:00:00Z",
        "information_available_at": "2026-01-01T09:00:00Z",
        "outcome_at": "2026-01-31T10:00:00Z",
        "evaluated_at": "2026-02-01T00:00:00Z",
        "action": "HOLD", "confidence": 0.99,
    }
    row.update(updates)
    return row


def panel(differences, entity_count=1):
    start = date(2026, 1, 1)
    return [
        {
            "date": (start + timedelta(days=index)).isoformat(),
            "entity_id": f"entity_{entity}",
            "loss_a": 1.0 + difference, "loss_b": 1.0,
        }
        for index, difference in enumerate(differences)
        for entity in range(entity_count)
    ]


class ScalarScoresTest(unittest.TestCase):
    def test_brier_known_answers_and_boundary_probabilities(self):
        self.assertAlmostEqual(brier(0.8, True), 0.04)
        self.assertAlmostEqual(brier(0.8, False), 0.64)
        self.assertEqual(brier(1, 1), 0)
        self.assertEqual(brier(0, 0), 0)
        self.assertEqual(brier(1, 0), 1)

    def test_interval_width_and_hand_calculated_tail_penalties(self):
        # [2,5], alpha=.2: width 3; one unit below adds 10, two above add 20.
        self.assertEqual(interval_score(2, 5, 3), 3)
        self.assertEqual(interval_score(2, 5, 1), 13)
        self.assertEqual(interval_score(2, 5, 7), 23)
        # [0,2], alpha=.1: width 2; .5 below adds 10.
        self.assertEqual(interval_score(0, 2, -0.5, alpha=0.1), 12)
        self.assertEqual(interval_score(2, 5, 2), 3)
        self.assertEqual(interval_score(2, 5, 5), 3)

    def test_widening_is_not_a_free_improvement(self):
        self.assertGreater(interval_score(-100, 100, 0), interval_score(-1, 1, 0))

    def test_invalid_scalars_fail_closed(self):
        for probability in (-0.01, 1.01, math.nan, math.inf, True, "0.5"):
            with self.subTest(probability=probability), self.assertRaises(ValueError):
                brier(probability, True)
        with self.assertRaises(ValueError):
            brier(0.5, 0.2)
        for args in ((2, 1, 0, .2), (0, 1, math.nan, .2),
                     (0, math.inf, 0, .2), (0, 1, 0, 0), (0, 1, 0, 1)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                interval_score(*args)


class ExplicitEventTest(unittest.TestCase):
    def score(self, rows, event_id="return_negative_v1", horizon="30_calendar_days"):
        return score_records(rows, event_id=event_id, horizon=horizon)

    def test_known_event_score_never_uses_action_or_confidence(self):
        rows = [event_record(), event_record(outcome=False, confidence=0.01)]
        result = self.score(rows)
        self.assertAlmostEqual(result["mean_brier"], 0.34)
        self.assertEqual(result["n_records"], 2)
        self.assertFalse(result["test_base_rate_used"])
        changed = [dict(row, action="BUY", confidence=0.5) for row in rows]
        self.assertEqual(self.score(changed), result)

    def test_same_hold_action_does_not_merge_different_events(self):
        down = event_record()
        flat = event_record(event_id="return_within_band_v1", outcome=False)
        self.assertAlmostEqual(self.score([down])["mean_brier"], 0.04)
        self.assertAlmostEqual(
            self.score([flat], event_id="return_within_band_v1")["mean_brier"], 0.64
        )
        with self.assertRaisesRegex(ValueError, "event_id"):
            self.score([down, flat])

    def test_action_confidence_cannot_substitute_for_explicit_probability(self):
        row = event_record()
        del row["probability"]
        with self.assertRaisesRegex(ValueError, "probability"):
            self.score([row])

    def test_copied_mature_prediction_cannot_inflate_record_count(self):
        row = event_record(record_id="same-prediction")
        with self.assertRaisesRegex(ValueError, "duplicate record_id"):
            self.score([row, dict(row)])
        for identifier in ("", " ", " has-padding ", None, 4):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                self.score([event_record(record_id=identifier)])
        del row["record_id"]
        with self.assertRaisesRegex(ValueError, "record_id"):
            self.score([row])

    def test_time_offsets_compare_instants_and_dates_in_utc(self):
        rows = [event_record(
            forecast_at="2026-01-01T12:00:00+02:00",
            information_available_at="2026-01-01T10:00:00Z",
        ), event_record(forecast_at="2026-01-01T05:00:00-05:00")]
        self.assertEqual(self.score(rows)["unique_forecast_dates"], 1)

    def test_unknown_unmatured_leaked_and_invalid_rows_rejected(self):
        cases = [
            {"outcome": 1}, {"outcome": "false"}, {"outcome": None},
            {"probability": math.nan}, {"probability": False},
            {"forecast_at": "2026-01-01T10:00:00"},
            {"information_available_at": "2026-01-01T10:00:01Z"},
            {"outcome_at": "2026-01-01T10:00:00Z"},
            {"outcome_at": "2026-02-02T00:00:00Z"},
            {"horizon": "30_trading_days"},
        ]
        for updates in cases:
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.score([event_record(), event_record(**updates)])
        with self.assertRaises(ValueError):
            self.score([])


class DateBlockTest(unittest.TestCase):
    def bootstrap(self, rows, **updates):
        params = {"block_days": 3, "repetitions": 300, "seed": 13}
        params.update(updates)
        return paired_day_bootstrap(rows, **params)

    def test_exact_paired_difference_and_reproducibility(self):
        rows = panel([0.25] * 18)
        result = self.bootstrap(rows)
        self.assertEqual(result["mean_difference"], 0.25)
        self.assertEqual(result["ci95"], [0.25, 0.25])
        self.assertEqual(result, self.bootstrap(list(reversed(rows))))
        self.assertFalse(result["scientific_validity_verified"])

    def test_cross_section_copying_cannot_create_more_market_dates(self):
        differences = [-.3, -.3, -.3, .3, .3, .3] * 3
        sparse = self.bootstrap(panel(differences))
        copied = self.bootstrap(panel(differences, entity_count=100))
        self.assertEqual(sparse["n_dates"], copied["n_dates"])
        self.assertEqual(sparse["n_nonoverlapping_blocks"], copied["n_nonoverlapping_blocks"])
        self.assertAlmostEqual(sparse["mean_difference"], copied["mean_difference"])
        for a, b in zip(sparse["ci95"], copied["ci95"]):
            self.assertAlmostEqual(a, b)
        self.assertEqual(copied["n_rows"], 1800)

    def test_daily_averaging_prevents_dense_day_dominance(self):
        rows = panel([1.0], entity_count=100)
        rows += [{"date": "2026-01-02", "entity_id": "one", "loss_a": 0, "loss_b": 1}]
        result = self.bootstrap(rows, block_days=1)
        self.assertEqual(result["mean_difference"], 0.0)

    def test_same_date_panel_is_not_independent_time_evidence(self):
        result = self.bootstrap(panel([0.3], entity_count=200))
        self.assertEqual(result["n_dates"], 1)
        self.assertIsNone(result["ci95"])
        self.assertFalse(result["resampling_supported"])
        self.assertEqual(result["status"], "insufficient_date_blocks")

    def test_null_and_signal_controls_are_not_p_values(self):
        null = self.bootstrap(panel([0.0] * 30))
        known = self.bootstrap(panel([-0.2] * 30))
        self.assertEqual(null["ci95"], [0.0, 0.0])
        self.assertLess(known["ci95"][1], 0)
        self.assertNotIn("p_value", known)
        self.assertFalse(known["scientific_validity_verified"])

    def test_duplicate_pairs_and_nonfinite_losses_rejected(self):
        rows = panel([0.1] * 9)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.bootstrap(rows + [rows[0]])
        for key in ("loss_a", "loss_b"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.bootstrap([dict(rows[0], **{key: math.nan})])
        with self.assertRaises(ValueError):
            self.bootstrap([])
        with self.assertRaises(ValueError):
            self.bootstrap(rows, block_days=True)
        with self.assertRaises(ValueError):
            self.bootstrap(rows, repetitions=1)
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            self.bootstrap([dict(rows[0], date="2026-W01-1")])


if __name__ == "__main__":
    unittest.main()
