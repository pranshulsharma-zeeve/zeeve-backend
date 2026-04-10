# IOPN Validator Data Flow

Simple flow (text)

- Client/API caller --`node_id`--> `/api/v1/subscriptions/summary/`
  - Calls `_validator_subscription_overview`
    - Calls `_compute_validator_summary`
      - Calls `_iopn_validator_summary`
        - Hits IOPN LCD `staking/v1beta1/validators/VALOPER/`
        - Hits IOPN LCD `staking/v1beta1/validators/VALOPER/delegations?pagination.count_total=true`
        - Hits IOPN LCD `distribution/v1beta1/validators/VALOPER/outstanding_rewards`
        - Hits IOPN LCD `staking/v1beta1/pool`
    - Calls `_compute_validator_delegations`
      - Calls `_iopn_validator_delegations`
        - Hits IOPN LCD `staking/v1beta1/validators/VALOPER/delegations?pagination.count_total=true`
- Response returns to client

Reward snapshots (cron)

- Validator reward snapshot job -> `_iopn_reward_snapshot`
  - Hits IOPN LCD `distribution/v1beta1/validators/VALOPER/outstanding_rewards`
  - Hits IOPN LCD `staking/v1beta1/validators/VALOPER/`
  - Hits IOPN LCD `staking/v1beta1/validators/VALOPER/delegations?pagination.count_total=true`

## Inputs

- `node_id`/`subscription_id` pointing to a validator subscription.
- `protocol.master.web_url` must serve `/blockchain/cosmos/...` (e.g., `https://mainnet-rpc2.iopn.tech`). Automatic fallback from `mainnet-rpc.*` to `mainnet-rpc2.*` is applied in reward snapshots.

## Data Collected (IOPN)

- Validator: tokens, status, commission, jailed, moniker.
- Delegations: list, total delegated, delegator count (pagination.total).
- Rewards: outstanding rewards from distribution (denom `wei/uiopn` converted with 18 decimals).
- Staking pool: bonded total for voting power % (tokens / bonded_total).

## Derived Fields

- `totalStake` = validator tokens (raw).
- `delegatorStake` = sum of delegation balances.
- `ownedStake` = max(tokens - delegatorStake, 0).
- `votingPowerPct` = tokens / bonded_total \* 100 (when pool available).
- `outstandingRewards` = converted rewards amount; `totalRewards` mirrors it. `ownedRewards`/`delegationRewards` are not split (LCD does not expose split data).

## Not Available

- Performance (signed/missed blocks) is not exposed by current IOPN LCD endpoints. Provide a signing-info endpoint if performance tracking is required.

## Viewing the Mermaid Diagram

- In GitLab/GitHub with Mermaid enabled, the fenced `mermaid` block renders automatically in the Markdown view. If you only see code, open in a renderer that supports Mermaid or enable it in repo settings.
- In VS Code, install a Markdown preview with Mermaid support (the built-in preview works in recent versions), then use “Open Preview” to see the diagram.
- CLI/console: keep the block as-is; it remains valid Markdown and will render in any Mermaid-capable viewer later.
