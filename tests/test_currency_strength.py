# tests/test_currency_strength.py
import unittest
import numpy as np
from unittest.mock import patch, MagicMock

# Import your actual code
from utils.calculate_currency_strength import calculate_atr, calculate_currency_strength
from config import CURRENCIES


# --------------------------
# Mock data: fixed candles for testing
# --------------------------
MOCK_CANDLES = [
    {"mid": {"h": "1.2500", "l": "1.2400", "c": "1.2450"}},
    {"mid": {"h": "1.2520", "l": "1.2420", "c": "1.2480"}},
    {"mid": {"h": "1.2550", "l": "1.2440", "c": "1.2510"}},
    {"mid": {"h": "1.2580", "l": "1.2470", "c": "1.2540"}},
    {"mid": {"h": "1.2600", "l": "1.2490", "c": "1.2570"}},
]


class TestCurrencyStrength(unittest.TestCase):

    # --------------------------
    # Test 1: ATR calculation
    # --------------------------
    def test_calculate_atr(self):
        atr = calculate_atr(MOCK_CANDLES, period=3)
        self.assertIsInstance(atr, float)
        self.assertGreater(atr, 0)
        print(f"\n✅ ATR Test: {atr:.5f}")

    # --------------------------
    # Test 2: Full strength function with mock data
    # --------------------------
    # @patch("utils.calculate_currency_strength.get_candles")
    # def test_strength_output_format(self, mock_get_candles):
    #     # Set mock return value
    #     mock_get_candles.return_value = MOCK_CANDLES

    #     test_pairs = ["EUR_USD", "GBP_USD", "USD_JPY"]
    #     scores = calculate_currency_strength(
    #         pairs=test_pairs,
    #         timeframes=["H1", "H4"],
    #         weights=[1, 3],
    #         lookback=5
    #     )

    #     # Validate output
    #     self.assertEqual(set(scores.keys()), set(CURRENCIES))
    #     for score in scores.values():
    #         self.assertIsInstance(score, float)
    #         self.assertGreaterEqual(score, -5)
    #         self.assertLessEqual(score, 5)

    #     print("\n✅ Strength Test Output:")
    #     for curr, s in sorted(scores.items(), key=lambda x: -x[1]):
    #         print(f"  {curr}: {s:.4f}")

    @patch("utils.get_candles")  # <-- patch where it's defined/exported
    def test_strength_output_format(self, mock_get_candles):
        mock_get_candles.return_value = MOCK_CANDLES

        test_pairs = ["EUR_USD", "GBP_USD", "USD_JPY"]
        scores = calculate_currency_strength(
            pairs=test_pairs,
            timeframes=["H1", "H4"],
            weights=[1, 3],
            lookback=5
        )

        self.assertEqual(set(scores.keys()), set(CURRENCIES))
        for score in scores.values():
            self.assertIsInstance(score, float)
            self.assertGreaterEqual(score, -5)
            self.assertLessEqual(score, 5)

        print("\n✅ Strength Test Output:")
        for curr, s in sorted(scores.items(), key=lambda x: -x[1]):
            print(f"  {curr}: {s:.4f}")
    # --------------------------
    # Test 3: Run with real live data
    # --------------------------
    def test_strength_with_real_data(self):
        from utils.currency_strength import get_currency_strength

        ranking, scores = get_currency_strength()
        self.assertIsInstance(ranking, list)
        self.assertEqual(len(ranking), len(CURRENCIES))

        print("\n📊 NEW ALGORITHM LIVE OUTPUT:")
        for curr, s in ranking[:4]:
            print(f"  {curr}: {s:.4f}")


if __name__ == "__main__":
    print("=== RUNNING UNIT TESTS ===")
    unittest.main(verbosity=2)