# USD Conversion for Report Metrics

To keep the customer-facing reports consistent, validator stake and reward
figures are now expressed in **USD** across every REST API response and the
account summary emails. Internal calculations (APR, scores, etc.) still use the
raw values written by the snapshot cron, but we normalize and price-convert the
numbers at the serialization layer.

## Protocol Metadata

Each `protocol.master` record exposes the following pricing fields:

| Field | Description |
| --- | --- |
| `token_symbol` | Optional label used in logs/UI while troubleshooting. |
| `price_coingecko_id` | CoinGecko slug (e.g., `ethereum`, `coreum`). Required for fetching USD prices. |
| `reward_decimals` | Number of decimals that must be removed from `validator.rewards.snapshot.total_rewards` to reach whole tokens. Defaults to 18 (wei-style data). |
| `stake_decimals` | Decimals to remove from `total_stake`. Defaults to 0 because stake snapshots are usually already stored in token units. |

Populate these values from the UI (Protocols > Pricing group) or via data
migration. Missing `price_coingecko_id` values will trigger warnings and cause
the API to fall back to returning the normalized token amount rather than USD.

## Price Fetching

`TokenPriceService` batches CoinGecko `simple/price` requests, caches the USD
values in-process for five minutes, and gracefully falls back to the most recent
price if CoinGecko is rate-limited. Override the base URL via the
`coingecko_base_url` config parameter if you proxy outbound traffic.

## Conversion Rules

1. Normalize raw snapshot values to token units using the configured decimals.
2. Multiply by the cached USD price when available.
3. If a price is unavailable, retain the normalized token amount and log a
   warning that identifies the missing protocol.

Outputs affected:

- `overview.totalRewards`, `overview.rewardsDelta`
- `validatorSummary.totalStake`, `totalRewards`, and their `prev*` counterparts
- Validator highlights (`stake`, `rewards`)
- Validator fleet summary/items
- Validator detail metrics (`stake`, `rewards`, deltas)
- Validator reward trends (`trends[].rewards`)
- Email context fields derived from the above services

RPC metrics (uptime, latency, request counts) remain unchanged—they are already
reported in human-readable units by Vizion.
