import unittest

import scanner


def item(score, all_ma_above=False, price=100.0, ma_offset=0.0):
    states = {}
    candle = {"day": {"low": 99.0, "high": 100.0},
              "week": {"low": 99.0, "high": 100.0},
              "month": {"low": 99.0, "high": 100.0}}
    for key, label, _length, _tf in scanner.SPECS:
        states[key] = {
            "label": label,
            "state": 7 if score == 8 else 6,
            "state_label": scanner.STATE_LABELS[7 if score == 8 else 6],
            "ma": price * (1 + ma_offset / 100),
            "ma_prev": price * (1 + ma_offset / 100),
        }
    return {
        "price": price,
        "score": score,
        "available_ma_count": 9,
        "all_ma_above": all_ma_above,
        "states": states,
        "candle": candle,
    }


class NearCandidateTests(unittest.TestCase):
    def test_near_9_uses_fully_scored_without_score_floor(self):
        candidate = scanner.near_9_of_9_candidate(item(6))
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["near_9_of_9_from_score"], 6)

    def test_near_9_excludes_existing_9_of_9(self):
        self.assertIsNone(scanner.near_9_of_9_candidate(item(9)))

    def test_near_9_uses_day_week_month_candle_keys(self):
        candidate = scanner.near_9_of_9_candidate(item(6))
        self.assertIsNotNone(candidate)
        malformed = item(6)
        malformed["candle"] = {"D": malformed["candle"]["day"]}
        self.assertIsNone(scanner.near_9_of_9_candidate(malformed))

    def test_near_all_allows_low_score_and_excludes_formal_all_above(self):
        self.assertIsNotNone(scanner.near_all_ma_above_candidate(item(4, ma_offset=0.5)))
        self.assertIsNone(scanner.near_all_ma_above_candidate(item(5, all_ma_above=True)))

    def test_near_all_rejects_more_than_one_percent(self):
        self.assertIsNone(scanner.near_all_ma_above_candidate(item(4, ma_offset=1.01)))


if __name__ == "__main__":
    unittest.main()
