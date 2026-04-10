"""Tests for pricing utilities."""

import importlib.util
import sys
import types
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

BASE_DIR = Path(__file__).resolve().parents[1]
REPORTS_DIR = BASE_DIR / "utils" / "reports"

zeeve_base_pkg = types.ModuleType("zeeve_base")
zeeve_base_pkg.__path__ = [str(BASE_DIR)]
sys.modules.setdefault("zeeve_base", zeeve_base_pkg)

utils_pkg = types.ModuleType("zeeve_base.utils")
utils_pkg.__path__ = [str(BASE_DIR / "utils")]
sys.modules.setdefault("zeeve_base.utils", utils_pkg)

reports_pkg = types.ModuleType("zeeve_base.utils.reports")
reports_pkg.__path__ = [str(REPORTS_DIR)]
sys.modules.setdefault("zeeve_base.utils.reports", reports_pkg)

helpers_spec = importlib.util.spec_from_file_location(
    "zeeve_base.utils.reports.helpers",
    REPORTS_DIR / "helpers.py"
)
helpers_module = importlib.util.module_from_spec(helpers_spec)
sys.modules["zeeve_base.utils.reports.helpers"] = helpers_module
helpers_spec.loader.exec_module(helpers_module)

pricing_spec = importlib.util.spec_from_file_location(
    "zeeve_base.utils.reports.pricing",
    REPORTS_DIR / "pricing.py"
)
pricing = importlib.util.module_from_spec(pricing_spec)
sys.modules["zeeve_base.utils.reports.pricing"] = pricing
pricing_spec.loader.exec_module(pricing)


class _DummyConfig:
    def sudo(self):
        return self

    def get_param(self, name, default=None):
        return None


class _DummyEnv(dict):
    def __getitem__(self, item):
        if item == "ir.config_parameter":
            return _DummyConfig()
        return super().__getitem__(item)


class _DummyProtocol:
    def __init__(self, proto_id, asset_id):
        self.id = proto_id
        self.price_coingecko_id = asset_id
        self.reward_decimals = 18
        self.stake_decimals = 0
        self.token_symbol = "TEST"


class TestPricingHelpers(unittest.TestCase):
    """Validate helper utilities for USD conversion."""

    def setUp(self):
        pricing.TokenPriceService.clear_cache()

    def test_normalize_amount_with_decimals(self):
        self.assertEqual(pricing.normalize_amount(10 ** 20, 18), 100.0)
        self.assertEqual(pricing.normalize_amount(None, 18), 0.0)

    def test_convert_raw_value_returns_tokens_and_usd(self):
        tokens, usd = pricing.convert_raw_value(5 * (10 ** 18), 18, 2.0)
        self.assertEqual(tokens, 5.0)
        self.assertEqual(usd, 10.0)

    @patch("zeeve_base.utils.reports.pricing.requests.get")
    def test_price_service_fetches_and_caches(self, mock_get):
        response = MagicMock()
        response.json.return_value = {"ethereum": {"usd": 3200.0}}
        response.status_code = 200
        response.raise_for_status.return_value = None
        mock_get.return_value = response

        env = _DummyEnv()
        protocol = _DummyProtocol(1, "ethereum")
        service = pricing.TokenPriceService(env, ttl_seconds=60)

        prices = service.get_prices([protocol])
        self.assertEqual(prices[1], 3200.0)
        # Second call should reuse cache
        prices = service.get_prices([protocol])
        self.assertEqual(prices[1], 3200.0)
        self.assertEqual(mock_get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
