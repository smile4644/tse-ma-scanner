import inspect
import unittest

import scanner


def build_states(gaps, states=None):
    states = states or {}
    result = {}
    for key, label, _length, _tf in scanner.SPECS:
        gap = gaps.get(key, 20.0)
        state = states.get(key, 8)
        result[key] = {
            "label": label,
            "state": state,
            "state_label": scanner.STATE_LABELS[state],
            "pass": state in (7, 8),
            "ma": 100.0,
            "ma_prev": 99.0,
            "gap_pct": gap,
        }
    return result


class EarlyStageTests(unittest.TestCase):
    def test_download_many_defaults_to_non_dividend_adjusted_ohlc(self):
        default = inspect.signature(scanner.download_many).parameters[
            "auto_adjust"
        ].default
        self.assertIs(default, False)

    def test_tdk_like_setup_is_rank_a(self):
        states = build_states(
            {
                "d5": 4.83,
                "d25": 8.65,
                "d75": 0.22,
                "w13": 5.32,
                "w26": 1.33,
                "m12": 5.72,
            },
            {"d75": 1, "w13": 1, "w26": 7},
        )
        result = scanner.calculate_early_stage_metrics(
            states=states, score=7, full_ma_set=True
        )
        self.assertTrue(result["early_stage_candidate"])
        self.assertEqual(result["early_stage_rank"], "A")
        self.assertEqual(result["ma_proximity_2pct_count"], 2)
        self.assertEqual(result["ma_proximity_5pct_count"], 3)
        self.assertEqual(result["nearest_ma_key"], "d75")

    def test_swcc_like_setup_is_rank_a(self):
        states = build_states(
            {
                "d5": 4.35,
                "d25": 3.11,
                "d75": 8.65,
                "w13": 8.96,
                "w26": 3.24,
                "m12": 1.40,
            },
            {"w26": 7, "m12": 7},
        )
        result = scanner.calculate_early_stage_metrics(
            states=states, score=9, full_ma_set=True
        )
        self.assertTrue(result["early_stage_candidate"])
        self.assertEqual(result["early_stage_rank"], "A")
        self.assertEqual(result["ma_proximity_2pct_count"], 1)
        self.assertEqual(result["ma_proximity_5pct_count"], 4)
        self.assertEqual(result["nearest_ma_key"], "m12")

    def test_valid_b_rank(self):
        states = build_states(
            {
                "d5": 4.5,
                "d25": 4.2,
                "d75": 1.5,
                "w13": 7.0,
                "w26": 6.0,
                "m12": 8.0,
            },
            {"d75": 1},
        )
        result = scanner.calculate_early_stage_metrics(
            states=states, score=7, full_ma_set=True
        )
        self.assertTrue(result["early_stage_candidate"])
        self.assertEqual(result["early_stage_rank"], "B")

    def test_large_day25_or_week13_gap_is_progressed(self):
        states = build_states(
            {
                "d5": 1.0,
                "d25": 12.0,
                "d75": 1.0,
                "w13": 11.0,
                "w26": 1.0,
                "m12": 1.0,
            }
        )
        result = scanner.calculate_early_stage_metrics(
            states=states, score=9, full_ma_set=True
        )
        self.assertFalse(result["early_stage_candidate"])
        self.assertTrue(result["early_stage_progressed"])
        self.assertIsNone(result["early_stage_rank"])

    def test_score_below_seven_is_not_candidate(self):
        states = build_states(
            {
                "d5": 1.0,
                "d25": 1.0,
                "d75": 1.0,
                "w13": 1.0,
                "w26": 1.0,
                "m12": 1.0,
            }
        )
        result = scanner.calculate_early_stage_metrics(
            states=states, score=6, full_ma_set=True
        )
        self.assertFalse(result["early_stage_candidate"])


if __name__ == "__main__":
    unittest.main()
