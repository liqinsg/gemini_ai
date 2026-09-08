import unittest
from unittest.mock import patch, MagicMock
from utils.mc_loader import get_latest_mc


class TestMCLoader(unittest.TestCase):

    @patch("utils.mc_loader.requests.get")
    def test_get_latest_mc_returns_latest_payload(self, mock_get):
        """Tests that get_latest_mc accurately parses directory listings and returns the correct payload."""
        # Mock GitHub directory response
        mock_dir_res = MagicMock()
        mock_dir_res.json.return_value = [
            {"name": "daily_mc_EURUSD_20260907_0315.json", "download_url": "http://mock/1.json"},
            {"name": "daily_mc_EURUSD_20260908_1842.json", "download_url": "http://mock/latest.json"},
            {"name": "daily_mc_EURUSD_20260908_0105.json", "download_url": "http://mock/2.json"},
        ]
        mock_dir_res.raise_for_status.return_value = None

        # Mock JSON payload response for the target file
        mock_payload_res = MagicMock()
        expected_payload = {
            "pair": "EURUSD=X",
            "timeframe": "D",
            "p_up": 61.2,
            "p_down": 38.8,
            "regime": "⚡ D STRONG MOMENTUM"
        }
        mock_payload_res.json.return_value = expected_payload
        mock_payload_res.raise_for_status.return_value = None

        mock_get.side_effect = [mock_dir_res, mock_payload_res]

        # Call the loader function
        result = get_latest_mc(instrument="EURUSD=X", day=True)

        # Assert function returns the expected dictionary
        self.assertEqual(result, expected_payload)
        self.assertEqual(result["p_up"], 61.2)
        
        # Verify get_latest_mc called the download_url for the newest timestamp
        self.assertEqual(mock_get.call_args_list[1][0][0], "http://mock/latest.json")

    @patch("utils.mc_loader.requests.get")
    def test_get_latest_mc_returns_none_when_no_match(self, mock_get):
        """Tests that get_latest_mc returns None if no matching files exist."""
        mock_dir_res = MagicMock()
        mock_dir_res.json.return_value = [
            {"name": "daily_mc_GBPUSD_20260908_1200.json", "download_url": "http://mock/gbp.json"}
        ]
        mock_dir_res.raise_for_status.return_value = None
        mock_get.return_value = mock_dir_res

        result = get_latest_mc(instrument="USDJPY", day=True)

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()