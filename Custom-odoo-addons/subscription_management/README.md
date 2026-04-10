# Subscription Management Module

## Restake Flow Overview

- **Endpoint:** `POST /api/v1/subscriptions/<node_id>/restake/enable`
- **Required fields:**
  - `reward` – minimum reward threshold as an integer value.
  - `interval` – restake cadence expressed in hours (integer).
- **Side effects:**
  - Persists the configuration under `subscription.subscription.metaData['restake']`.
  - Issues a single Zabbix `host.update` request that sets the `{$RESTAKE_INTERVAL}` and `{$RESTAKE_MIN_REWARD}` macros.
- **Automation:**
  - After the initial update, Zabbix performs all recurring restake operations—no Odoo cron job is necessary.

Refer to `utils/restake_helper.py` for the helper implementation that coordinates GitHub metadata, subscription storage, and the Zabbix update.

## Validator Metrics Architecture

The validator insights surface three public APIs — `/api/v1/validator/rewards`, `/api/v1/validator/performance`, and `/api/v1/validator/stake-delegator-chart`. Each relies on the same heartbeat: queue builders run daily, snapshotters run every six hours, and the APIs read from those persisted snapshots (falling back to live RPC only when necessary). Today only Coreum and Avalanche validators are scheduled because they expose the telemetry our helpers expect.

### Flow at a Glance

```
subscription.node (ready validators)
        │
        │  daily 00:00 UTC
        ▼
┌──────────────────────────┐
│ populate_*_queue        │
│ - collapse duplicates   │
│ - refresh RPC targets   │
└──────────┬──────────────┘
           │ 6-hour cadence (00,06,12,18 UTC)
           ▼
┌──────────────────────────┐
│ snapshot_all_*          │
│ - fetch via helpers     │
│ - persist 60-day window │
└──────────┬──────────────┘
           │ on-demand
           ▼
┌──────────────────────────┐
│ /validator/* APIs       │
│ - query snapshots       │
│ - optional live RPC     │
└──────────────────────────┘
```

### Phase Breakdown

| Step                                   | Cadence                            | Inputs                                                                   | Core actions                                                                                                                                           | Outputs                                                                                        |
| -------------------------------------- | ---------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------- |
| 1. Queue build (`populate_*_queue`)    | Daily cron                         | `subscription.node` records with `node_type=validator` and `state=ready` | Resolve valoper/nodeID, choose RPC base (mainnet vs testnet), ensure protocol is supported, upsert a `pending` queue row, deduplicate stale entries    | `validator.rewards.queue` and `validator.performance.queue` rows marked `pending`              |
| 2. Snapshot capture (`snapshot_all_*`) | Every 6 hours, max 20 rows per run | Pending queue rows                                                       | Flip row to `processing`, call `_collect_validator_*_snapshot`, enforce a 90-second budget, persist normalized payloads, purge rows older than 60 days | Fresh `validator.rewards.snapshot` and `validator.performance.snapshot` rows keyed by protocol |
| 3. Chart APIs (`/validator/*`)         | On demand                          | Snapshot tables (+ live RPC fallback)                                    | Guard access, normalize request, load historical window, calculate any derived metrics (e.g., Coreum missed deltas, Avalanche uptime projection)       | JSON series powering dashboards                                                                |

### Data Ingestion Jobs (Detail)

- **Queue population (daily):** `validator.rewards.queue.populate_rewards_queue()` and `validator.performance.queue.populate_performance_queue()` scan all ready validator subscriptions, map their RPC endpoints, verify they belong to `SUPPORTED_VALIDATOR_HISTORY_PROTOCOLS`/`SUPPORTED_VALIDATOR_PERFORMANCE_PROTOCOLS`, and create or refresh queue entries while collapsing duplicates and rewriting stale RPC URLs.

- **Snapshot processors (every 6 hours):** `validator.rewards.snapshot.snapshot_all_validators_rewards()` and `validator.performance.snapshot.snapshot_all_validator_performance()` pop up to 20 pending rows, switch them to `processing`, call the helpers, and commit each result independently so partial progress survives RPC failures. Rows older than 60 days are pruned on each pass.

- **External data sources:**
  - **Coreum LCD (REST):**
    - Rewards & stake: `GET {rpc_base_url}/cosmos/distribution/v1beta1/validators/{valoper}/outstanding_rewards`, `.../cosmos/staking/v1beta1/validators/{valoper}`, and `.../cosmos/staking/v1beta1/validators/{valoper}/delegations?pagination.count_total=true`.
    - Performance: `GET .../cosmos/base/tendermint/v1beta1/blocks/latest`, `.../cosmos/slashing/v1beta1/params`, `.../cosmos/staking/v1beta1/validators/{valoper}` (consensus keys), `.../cosmos/base/tendermint/v1beta1/validatorsets/latest`, plus paginated `.../cosmos/slashing/v1beta1/signing_infos` to find the `missed_blocks_counter`.
  - **Avalanche RPC:** `_avalanche_fetch_validator` wraps `platform.getCurrentValidators` (`includeDelegators=True`). The payload feeds rewards (stake + potential rewards) and performance (uptime projected onto a 100-block window). `_fetch_avalanche_chain_height` adds `platform.getHeight` so snapshots store real P-Chain height.
  - **Flow REST API:** Flow validators rely entirely on Cadence scripts executed via the Flow Access API. All endpoints are resolved dynamically from `protocol.master` (`web_url` for mainnet, `web_url_testnet` for testnet). When no protocol URL is configured, the helper falls back to public Flow endpoints (`rest-mainnet.onflow.org` / `rest-testnet.onflow.org`).
    - **Node ID extraction:** Flow uses 64 or 128-character hex node IDs (e.g., `6c6af0933b710655ec553f4bead3b01c5e0a3ffd1194ee536efb926b356c54aa`). The queue population logic extracts this from `subscription.node.validator_info` using `_extract_validator_address()`.
    - **Network resolution:** The snapshot processor calls `_resolve_flow_context()` to locate the matching subscription node, infer `mainnet` or `testnet` from `network_selection_id`, and extract the owner wallet address from `validator_info` (optional, reserved for future wallet balance queries).
    - **Rewards & stake collection:** `fetch_flow_validator_metrics()` executes three Cadence scripts in parallel:
      1. **Aggregated metrics script:** Returns `nodeTokensRewarded`, `delegatorRewardTotal`, `delegatorCountAll`, and `delegatorCountActive` by iterating the node's delegator list on-chain and summing `DelegatorInfo.tokensRewarded` for each entry. Active delegators are those with non-zero `tokensCommitted`, `tokensStaked`, or `tokensUnstaking`.
      2. **Total stake with delegators:** `FlowIDTableStaking.NodeInfo(nodeID).totalCommittedWithDelegators()` returns the full validator stake including all delegations.
      3. **Total stake without delegators:** `FlowIDTableStaking.NodeInfo(nodeID).totalCommittedWithoutDelegators()` returns only the validator's self-bonded stake.
    - The helper computes `delegator_stake = total_with - total_without` and stores `outstanding_rewards` (node rewards), `tokens` (total stake), and `delegator_count` (active count matching Flow Explorer logic).
    - **Block height:** Fetched via `GET {rest_base}/v1/blocks?height=sealed&limit=1` to anchor each snapshot to the chain tip.
    - **ID table addresses:** Mainnet uses `0x8624b52f9ddcd04a`, testnet uses `0x9eca2b38b18b5dfe`. These are hardcoded as constants in `flow_validator_metrics.py` since they never change for the public Flow chain.
    - **Fallback for older Cadence syntax:** If the aggregated metrics script fails (Flow nodes running outdated Cadence), the helper retries with `pub` instead of `access(all)`, then falls back to the simpler `NodeInfo` script and counts delegators from the raw array length.
    - **Performance:** Flow validators do not expose slashing windows or missed block counters, so performance snapshots are not scheduled for Flow. Only rewards and stake history are persisted.

### Storage Models & Retention

| Model                            | Purpose                                                      | Key fields                                                                                                           |
| -------------------------------- | ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------- |
| `validator.rewards.snapshot`     | Outstanding rewards, stake, and delegator count per protocol | `valoper`, `outstanding_rewards`, `total_stake`, `delegator_count`, `protocol_id`, `protocol_key`, `snapshot_date`   |
| `validator.performance.snapshot` | Signed/missed block windows per protocol                     | `valoper`, `valcons_addr`, `height`, `missed_counter`, `window_size`, `protocol_id`, `protocol_key`, `snapshot_date` |

Each snapshot routine enforces `unique(valoper, protocol, snapshot_date)` so history grows append-only.

### API Routing Pattern

Every chart route in `controllers/subscription_controller.py` follows the same guardrails:

1. Validate the session via `oauth_utils.require_user()` and confirm the user owns the subscription or belongs to `access_rights.group_admin`.
2. Normalize the requested `nodeId`, load the `subscription.node`, and extract the validator address with `_extract_validator_address()`.
3. Resolve the protocol and reject unsupported chains early to avoid missing LCD/RPC endpoints.
4. Derive the correct RPC base URL (testnet vs mainnet) from the protocol record, trimming trailing `/` characters.
5. Delegate to `_fetch_validator_*_with_period`, which queries the snapshot tables and falls back to a live RPC fetch when history is missing for the requested window.

## API Deep-Dive

### `/api/v1/validator/rewards`

**Workflow**

1. Controller validation (steps above) ensures the caller can view the subscription and that the protocol belongs to `SUPPORTED_VALIDATOR_HISTORY_PROTOCOLS`.
2. `_fetch_validator_rewards_with_period(valoper, protocol_id, period_days)` loads all `validator.rewards.snapshot` rows newer than `now - period_days` for that protocol.
3. Each snapshot already contains `outstanding_rewards` (Coreum: converted from micro CORE using `_micro_to_core_number`; Avalanche: normalized via `_nano_to_avax_number`), plus `total_stake` and `delegator_count`.
4. The helper converts `snapshot_date` to ISO strings and emits a `series` list. No snapshots? The API returns an empty series and the note “No historical rewards found.”
5. Data freshness is governed by the 6-hour snapshot cron; retries are not attempted here because the ingestion path already performs single-attempt RPC fetches with logging and queue-level retries.

**External Calls During Ingestion**

- Coreum rewards helper `_fetch_validator_outstanding_rewards()` hits the LCD URLs listed in the “External data sources” section above. It sums every denom returned by `outstanding_rewards`, converts tokens from micro units, and counts delegators using pagination metadata.
- Avalanche rewards helper `_build_avalanche_reward_snapshot()` digests the `platform.getCurrentValidators` payload: `stakeAmount` + `delegatorWeight` produce `total_stake`, `potentialReward` + nested `delegators[].potentialReward` produce `outstanding_rewards`, and the delegator array length becomes `delegator_count`.

### `/api/v1/validator/performance`

**Workflow**

1. The controller confirms the protocol is in `SUPPORTED_VALIDATOR_PERFORMANCE_PROTOCOLS` and, for Coreum, enforces the `valoper` checksum format via `_is_valoper_address()`.
2. `_fetch_validator_performance_with_period(valoper, protocol_key, rpc_base_url, period_days, protocol_id)` loads `validator.performance.snapshot` rows in chronological order.
3. For Coreum snapshots, the helper treats `missed_counter` as a cumulative metric; it computes deltas between consecutive rows to derive per-window `signed` vs `missed` counts. For Avalanche snapshots, values are already per-window because uptime is projected directly onto a 100-block synthetic window.
4. When there are no rows for the requested period, the helper calls `_collect_validator_performance_snapshot()`, which dispatches to the protocol-specific collector:
   - Coreum collector chains the LCD requests described earlier to discover the validator’s consensus key, resolve its valcons address from `validatorsets/latest`, and finally read the slashing module’s `missed_blocks_counter`.
   - Avalanche collector reuses `_avalanche_fetch_validator()` and converts uptime percentages into synthetic `window_size`, `signed`, and `missed` values.
5. The API responds with `series`, `latestHeight`, `valconsAddr`, and `windowSize`, enabling the UI to plot recent windows or fall back to live data seamlessly.

**Avalanche signed/missed derivation**

- Avalanche’s `platform.getCurrentValidators` response exposes `uptime` (a 0–1 float or 0–100 percentage) but not a slashing history. To keep the charts comparable to Coreum, `_fetch_avalanche_performance_data()` treats each uptime sample as a 100-block window:
  - `window_size = 100`
  - `signed = round(window_size * (uptime_pct / 100))`
  - `missed = window_size - signed`
- This produces a ratio that mirrors “signed vs missed” even though Avalanche does not emit the raw counters. The helper also queries `platform.getHeight` before saving the snapshot so `height` matches the actual P-Chain block height rather than a timestamp.
- **How to validate:**
  1. Call `platform.getCurrentValidators` yourself (`curl -X POST https://<rpc>/ext/bc/P -H 'content-type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"platform.getCurrentValidators","params":{"nodeIDs":["NodeID-XYZ"],"includeDelegators":true}}'`). Confirm the `uptime` in the payload equals the percentage shown in our API.
  2. Plug that percentage into the formula above to verify the `signed`/`missed` numbers stored in `validator.performance.snapshot`.
  3. Call `platform.getHeight` (same RPC path, method `platform.getHeight`) and check that the returned hex height matches `latestHeight` in the `/validator/performance` JSON and in Avalanche’s Explorer (https://subnets.avax.network → Primary Network → P-Chain → “Latest Block”).

### `/api/v1/validator/stake-delegator-chart`

**Workflow**

1. Authentication and protocol validation mirror the rewards endpoint because both charts rely on the same `validator.rewards.snapshot` table.
2. `_fetch_validator_stake_delegator_with_period(valoper, protocol_id, period_days)` queries snapshots for the time window and builds two parallel series: `tokens` (total stake) and `delegatorCount`.
3. Each point reuses `snapshot.snapshot_date` as the x-axis. No extra RPC requests are fired during read time—the heavy lifting already happened during the rewards snapshot ingestion.
4. When no snapshots exist, the helper returns empty lists and the note “No historical stake data found.”

### Testing & Monitoring

- `tests/test_validator_metrics.py` asserts that helper queries respect `protocol_id` filters so Coreum and Avalanche data never leak across tenants even when validators share the same operator string.
- Cron executions log success/error counts plus elapsed runtime; operators can monitor the Odoo log stream or the `ir.cron` dashboard to confirm snapshot freshness.

### Presenting to Stakeholders

- Emphasize that every API response is backed by persisted history, not ad-hoc RPC calls, which keeps response times predictable and protects on-chain endpoints from spikes.
- Highlight the modular protocol adapters (Coreum LCD vs Avalanche RPC) so product managers understand how the team can onboard additional chains by adding new collectors and including them in the supported sets.
- Reference the ASCII flow above during reviews—it mirrors the production cron schedule (daily queue fills, six-hour snapshots, on-demand reads) and illustrates the single-source-of-truth models powering all validator dashboards.
