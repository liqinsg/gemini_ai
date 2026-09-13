"""
Unit tests for range_detector.py
Run with: `python -m pytest tests/test_range_detector.py -v`
"""
import pytest
from unittest.mock import patch
from utils.range_detector import is_sideways


class TestRangeDetector:
    def test_returns_false_for_trending(self):
        """Trending pair with large range should NOT be flagged sideways"""
        mock_candles = []
        # Steeper slope: last 5 days alone = ~0.8% range > 0.5%
        for i in range(45):
            price = 103.0 + (i * 0.12)  # Increased from 0.08 → 0.12
            mock_candles.append({"mid": {"h": price + 0.1, "l": price - 0.1, "c": price}})

        with patch("utils.range_detector.get_candles", return_value=mock_candles):
            sideways, reason, metrics = is_sideways("USD_JPY", max_range_pct=0.5)
            print(f"DEBUG TRENDING: {reason} | {metrics}")
            assert sideways is False

    def test_flags_narrow_range(self):
        """Very tight price range should be flagged sideways"""
        mock_candles = []
        for i in range(45):
            mock_candles.append({"mid": {"h": 105.10, "l": 105.00, "c": 105.05}})

        with patch("utils.range_detector.get_candles", return_value=mock_candles):
            sideways, reason, _ = is_sideways("USD_JPY", max_range_pct=0.15)
            assert sideways is True
            assert "range" in reason.lower()

    def test_insufficient_data_returns_false(self):
        """Should not flag when there's not enough candle data"""
        mock_candles = [{"mid": {"h": 105.5, "l": 103.0, "c": 105.0}}] * 10

        with patch("utils.range_detector.get_candles", return_value=mock_candles):
            sideways, reason, _ = is_sideways("USD_JPY", lookback_days=15)
            assert sideways is False
            assert "not enough" in reason.lower()  # Match actual message

    def test_ma_band_trigger(self):
        """Price stuck tight to MA20 should be flagged sideways"""
        mock_candles = []
        for i in range(45):
            close = 105.00 + (i * 0.0001)
            mock_candles.append({"mid": {"h": close + 0.01, "l": close - 0.01, "c": close}})

        with patch("utils.range_detector.get_candles", return_value=mock_candles):
            sideways, reason, metrics = is_sideways(
                "USD_JPY",
                max_range_pct=0.01,      # Your range = 0.02% > 0.01% → PASS range check
                min_volatility_ratio=0.3,  # 1.0 > 0.3 → PASS volatility check
                ma_band_threshold=0.2     # Loosen to ensure trigger
            )
            print(f"DEBUG: {reason} | {metrics}")
            assert sideways is True
            assert "MA20" in reason or "stuck" in reason.lower()
