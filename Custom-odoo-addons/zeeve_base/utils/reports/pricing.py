# -*- coding: utf-8 -*-
"""
Utility helpers for converting on-chain metrics to USD.

Provides a cached CoinGecko-backed price service plus helpers to normalize
raw snapshot values into token units before applying USD conversion.
"""

import logging
import time
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import urlparse

import requests

from . import helpers

_LOGGER = logging.getLogger(__name__)

_DEFAULT_COINGECKO_BASE = "https://api.coingecko.com/api/v3"
_PRICE_CACHE: Dict[str, Dict[str, float]] = {"entries": {}}
_CACHE_TTL_SECONDS = 300  # 5 minutes


class TokenPriceService:
    """Fetch and cache USD prices for protocol tokens."""

    def __init__(self, env, ttl_seconds: int = _CACHE_TTL_SECONDS):
        self.env = env
        self.ttl_seconds = ttl_seconds or _CACHE_TTL_SECONDS
        self._config = env["ir.config_parameter"].sudo()

    def get_prices(self, protocols: Iterable) -> Dict[int, float]:
        """
        Return USD prices keyed by protocol ID.

        Args:
            protocols: Iterable of protocol.master records

        Returns:
            Dict[protocol_id -> usd_price]
        """
        records = [p for p in protocols if getattr(p, "price_coingecko_id", False)]
        if not records:
            return {}

        now = time.time()
        cache = _PRICE_CACHE.setdefault("entries", {})
        prices: Dict[int, float] = {}
        missing_asset_ids = []

        for protocol in records:
            asset_id_raw = (protocol.price_coingecko_id or "").strip()
            asset_id = _normalize_asset_id(asset_id_raw)
            if not asset_id:
                _LOGGER.warning(
                    "Price lookup skipped because CoinGecko ID is not configured correctly for protocol %s (raw value: %s)",
                    getattr(protocol, "name", protocol.id),
                    asset_id_raw or "<empty>",
                )
                continue
            cached_entry = cache.get(asset_id)
            if (
                cached_entry
                and (now - cached_entry["timestamp"]) < self.ttl_seconds
                and cached_entry["price"] is not None
            ):
                prices[protocol.id] = cached_entry["price"]
            else:
                missing_asset_ids.append(asset_id)

        if missing_asset_ids:
            fetched = self._fetch_prices_from_coingecko(missing_asset_ids)
            now = time.time()
            for asset_id, price in fetched.items():
                cache[asset_id] = {"price": price, "timestamp": now}
            for protocol in records:
                asset_id = _normalize_asset_id((protocol.price_coingecko_id or "").strip())
                if not asset_id:
                    continue
                entry = cache.get(asset_id)
                if entry and entry["price"] is not None:
                    prices[protocol.id] = entry["price"]

        return prices

    def _fetch_prices_from_coingecko(self, asset_ids: Iterable[str]) -> Dict[str, float]:
        """Fetch USD prices for the given CoinGecko asset IDs."""
        base_url = (
            self._config.get_param("coingecko_base_url", "") or _DEFAULT_COINGECKO_BASE
        ).rstrip("/")
        results: Dict[str, float] = {}

        ids = [asset_id for asset_id in asset_ids if asset_id]
        if not ids:
            return results

        chunk_size = 50  # Stay well below the 250 id limit
        for index in range(0, len(ids), chunk_size):
            chunk = ids[index : index + chunk_size]
            try:
                response = requests.get(
                    f"{base_url}/simple/price",
                    params={"ids": ",".join(chunk), "vs_currencies": "usd"},
                    timeout=10,
                )
                response.raise_for_status()
                data = response.json()
                for asset_id in chunk:
                    usd_price = data.get(asset_id, {}).get("usd")
                    if usd_price is not None:
                        results[asset_id] = float(usd_price)
                    else:
                        results[asset_id] = None
                        _LOGGER.warning(
                            "CoinGecko price missing for %s (chunk %s)", asset_id, chunk
                        )
            except Exception as exc:
                _LOGGER.exception(
                    "CoinGecko price fetch failed for assets %s: %s", chunk, exc
                )
        return results

    @classmethod
    def clear_cache(cls):
        """Utility for tests to reset the shared cache."""
        _PRICE_CACHE["entries"] = {}


def normalize_amount(raw_value: Optional[float], decimals: int) -> float:
    """Normalize a raw integer-like value into token units."""
    value = helpers.safe_float(raw_value, 0.0)
    if decimals and decimals > 0:
        value /= float(10 ** decimals)
    return value


def convert_raw_value(
    raw_value: Optional[float], decimals: int, usd_price: Optional[float]
) -> Tuple[float, Optional[float]]:
    """
    Normalize a raw value and convert it to USD if a price is available.

    Returns:
        (token_amount, usd_amount_or_none)
    """
    tokens = normalize_amount(raw_value, decimals)
    usd = round(tokens * usd_price, 2) if usd_price is not None else None
    return tokens, usd


def _normalize_asset_id(asset_id: str) -> str:
    """Return a CoinGecko slug even if the configuration stored a full URL."""
    value = (asset_id or "").strip()
    if not value:
        return ""
    if "://" in value:
        try:
            parsed = urlparse(value)
            path = (parsed.path or "").rstrip("/")
            slug = path.split("/")[-1] if path else ""
            return slug.lower()
        except Exception:  # pragma: no cover - defensive
            return value.lower()
    return value.lower()
