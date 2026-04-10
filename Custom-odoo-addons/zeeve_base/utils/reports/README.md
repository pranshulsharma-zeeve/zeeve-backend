# Reports Module

Comprehensive reporting system for monitoring RPC nodes, validators, and account performance with aggregated metrics, uptime-based health classification, and insights.

> **Update (2026-03-16):** Report API payloads no longer expose `score`, `grade`, `scoreChange`, or `scoreChangePercent`. Public `status` fields now depend only on uptime thresholds: `good` for uptime >= 95%, `warning` for uptime between 60% and 95%, and `critical` for uptime <= 60%. Any score-based notes further down are obsolete until the remaining documentation cleanup lands.

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     HTTP Controller Layer                        │
│  (5 REST Endpoints: /api/v1/reports/*)                          │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ↓
┌─────────────────────────────────────────────────────────────────┐
│                      Services Layer                              │
│  (get_account_report, get_rpc_fleet_report, etc.)              │
│  - Data Fetching        - Aggregation                           │
│  - Uptime Classification - Insight Generation                   │
└────────────┬──────────────────────┬─────────────┬───────────────┘
             │                      │             │
  ┌────────▼─────────┐  ┌────────▼─────────────┐  ┌──▼──────────────┐
  │ Aggregation      │  │ Classification       │  │ Helpers         │
  │ (time-series)    │  │ (uptime/risk logic)  │  │ (date/format)   │
  └─────────────────┘  └──────────────────────┘  └─────────────────┘
             │                     │                    │
    ┌────────▼──────────────────────▼────────────────────▼────────┐
    │                      Clients Layer                           │
    │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
    │  │VizionClient  │  │Snapshot      │  │RpcData           │  │
    │  │(HTTP API)    │  │Repository    │  │Repository        │  │
    │  │              │  │(ORM queries) │  │(RPC wrappers)    │  │
    │  └──────────────┘  └──────────────┘  └──────────────────┘  │
    └──────────────────────────────────────────────────────────────┘
             │                     │                    │
    ┌────────▼──────────┐ ┌───────▼──────────┐ ┌─────▼──────────┐
    │ Vizion APIs       │ │ Odoo Database    │ │ RPC Functions  │
    │ - Protocol Data   │ │ - validator.*    │ │ - Validator    │
    │ - Trigger Data    │ │ - subscription.* │ │   methods      │
    │ - Security Data   │ │                  │ │ - Method count │
    └───────────────────┘ └──────────────────┘ └────────────────┘
```

## Data Flow per Endpoint

---

## 1. Account Weekly Report

**Endpoint:** `GET /api/v1/reports/account-weekly?range=weekly&timezone=UTC`

```
┌────────────────────────────────────────────────────────────────────┐
│ Client Request with JWT Token                                      │
│ GET /api/v1/reports/account-weekly?range=weekly&timezone=UTC      │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ↓
                 ┌──────────────────────┐
                 │ Authentication       │
                 │ (JWT verify + user)  │
                 └──────────┬───────────┘
                            │
                ┌───────────▼────────────┐
                │ Fetch all RPC nodes    │
                │ for this account       │
                │ (subscription_node)    │
                └───────────┬────────────┘
                            │
          ┌─────────────────▼──────────────────┐
          │ For each RPC node:                 │
          │ _fetch_rpc_node_data()             │
          │                                    │
          │ 1. Get Vizion Host ID              │
          │    ↓ login_with_email()            │
          │    ↓ Returns host data             │
          │                                    │
          │ 2. Fetch Protocol Data (Vizion)    │
          │    ↓ /api/item/get-latest-...      │
          │    ↓ Returns: uptime%, latency,    │
          │      request count, error count    │
          │                                    │
          │ 3. Calculate metrics               │
          │    ↓ Error rate % calculation      │
          │    ↓ Score calculation             │
          │    ↓ Grade & Status                │
          └────────────┬──────────────────────┘
                       │
        ┌──────────────▼──────────────────┐
        │ Fetch all validator nodes       │
        │ for this account                │
        │ (subscription_node)             │
        └──────────┬──────────────────────┘
                   │
    ┌──────────────▼──────────────────┐
    │ Batch fetch reward snapshots     │
    │ validator.reward.snapshot        │
    │ (within period range)            │
    └──────────┬───────────────────────┘
               │
    ┌──────────▼───────────────────────┐
    │ For each validator:               │
    │                                   │
    │ 1. Aggregate snapshots            │
    │    ↓ SUM: total_stake, rewards    │
    │    ↓ AVG: stake per snapshot      │
    │    ↓ LAST: latest state           │
    │                                   │
    │ 2. Calculate APR                  │
    │    ↓ (rewards/stake) * 365/days   │
    │                                   │
    │ 3. Check if jailed                │
    │    ↓ Parse validator_info JSON    │
    │                                   │
    │ 4. Calculate score & status       │
    │    ↓ Scoring formula              │
    │    ↓ Grade & Status               │
    └────────────┬──────────────────────┘
                 │
        ┌────────▼───────────────────────┐
        │ Aggregate RPC metrics           │
        │ Calculate fleet totals/averages │
        │ (SUM, AVG across all nodes)     │
        └────────┬──────────────────────┘
                 │
        ┌────────▼───────────────────────┐
        │ Aggregate validator metrics     │
        │ Calculate fleet totals/averages │
        │ (SUM, AVG across validators)    │
        └────────┬──────────────────────┘
                 │
        ┌────────▼───────────────────────┐
        │ Generate insights               │
        │ _generate_account_insights()    │
        │ - Uptime warnings               │
        │ - Jailed validator alerts       │
        │ - APR anomalies                 │
        └────────┬──────────────────────┘
                 │
        ┌────────▼───────────────────────┐
        │ Build AccountWeeklyReport DTO   │
        │ - Report metadata               │
        │ - Account overview              │
        │ - RPC & validator summaries     │
        │ - Incidents & insights          │
        │ - Trends (placeholder)          │
        └────────┬──────────────────────┘
                 │
        ┌────────▼───────────────────────┐
        │ Convert DTO to dict             │
        │ Return JSON response            │
        └────────────────────────────────┘
```

**Data Sources:**

- **Odoo DB**: `subscription_node` (node list), `validator.reward.snapshot` (stake/rewards)
- **Vizion API**: `/api/item/get-latest-protocol-data` (uptime, latency, requests)
- **RPC Functions**: Node identifier resolution

---

## 2. RPC Fleet Report

**Endpoint:** `GET /api/v1/reports/rpc-fleet?range=weekly&timezone=UTC`

```
┌────────────────────────────────────────────────────────────────────┐
│ Client Request with JWT Token                                      │
│ GET /api/v1/reports/rpc-fleet?range=weekly&timezone=UTC           │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ↓
                 ┌──────────────────────┐
                 │ Authentication       │
                 │ (JWT verify + user)  │
                 └──────────┬───────────┘
                            │
                ┌───────────▼────────────────┐
                │ Fetch all RPC nodes        │
                │ for this account           │
                │ (node_type='rpc')          │
                │ (subscription_node)        │
                └────────────┬───────────────┘
                             │
          ┌──────────────────▼──────────────┐
          │ For each RPC node:              │
          │ _fetch_rpc_node_data()          │
          │                                 │
          │ Vizion API Call:                │
          │ /api/item/get-latest-protocol.. │
          │ Returns:                        │
          │ - uptime_pct                    │
          │ - latency_ms                    │
          │ - request_count                 │
          │ - error_count                   │
          │                                 │
          │ Calculate:                      │
          │ - error_rate_pct                │
          │ - rpc_node_score                │
          │ - grade (A/B/C)                 │
          │ - status (good/warn/critical)   │
          └──────────┬──────────────────────┘
                     │
          ┌──────────▼──────────────────┐
          │ Classify nodes by status:    │
          │ - Healthy (status=good)      │
          │ - Warning (status=warning)   │
          │ - Critical (status=critical) │
          │ - Count each category        │
          └──────────┬──────────────────┘
                     │
          ┌──────────▼──────────────────┐
          │ Calculate fleet averages:    │
          │ - Avg uptime %               │
          │ - Avg latency ms             │
          │ - Total requests             │
          │ - Total errors               │
          │ - HealthMix distribution     │
          └──────────┬──────────────────┘
                     │
          ┌──────────▼──────────────────┐
          │ Generate insights:           │
          │ _generate_rpc_fleet_insights │
          │ - Critical nodes alert       │
          │ - High latency alert         │
          │ - Error rate spike           │
          └──────────┬──────────────────┘
                     │
          ┌──────────▼──────────────────┐
          │ Build RpcFleetReport DTO     │
          │ - Report metadata            │
          │ - Fleet summary              │
          │ - Node list with scores      │
          │ - Health distribution        │
          │ - Incidents & insights       │
          │ - Trends (placeholder)       │
          └──────────┬──────────────────┘
                     │
          ┌──────────▼──────────────────┐
          │ Convert DTO to dict          │
          │ Return JSON response         │
          └──────────────────────────────┘
```

**Data Sources:**

- **Odoo DB**: `subscription_node` (RPC node list)
- **Vizion API**: `/api/item/get-latest-protocol-data` (uptime, latency, metrics)

---

## 3. RPC Node Detail Report

**Endpoint:** `GET /api/v1/reports/rpc/{nodeId}?range=weekly&timezone=UTC`

```
┌────────────────────────────────────────────────────────────────────┐
│ Client Request with JWT Token                                      │
│ GET /api/v1/reports/rpc/{nodeId}?range=weekly&timezone=UTC        │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ↓
                 ┌──────────────────────┐
                 │ Authentication       │
                 │ (JWT verify + user)  │
                 └──────────┬───────────┘
                            │
                ┌───────────▼────────────────┐
                │ Find node by ID            │
                │ (UUID or database ID)      │
                │ (subscription_node)        │
                └────────────┬───────────────┘
                             │
          ┌──────────────────▼──────────────────────┐
          │ Fetch RPC node core metrics             │
          │ _fetch_rpc_node_data()                  │
          │                                          │
          │ 1. Get Vizion Host ID                   │
          │    ↓ login_with_email()                 │
          │    ↓ Returns host identifier            │
          │                                          │
          │ 2. Vizion API: Protocol Data            │
          │    /api/item/get-latest-protocol-data   │
          │    Returns:                             │
          │    - uptime_pct                         │
          │    - latency_ms                         │
          │    - total_requests                     │
          │    - error_count                        │
          │    - error_rate_pct (calculated)        │
          │                                          │
          │ 3. Calculate score/grade/status         │
          └──────────┬───────────────────────────────┘
                     │
          ┌──────────▼──────────────────────────┐
          │ Fetch security metrics (Vizion)     │
          │ /api/item/get-security-monitor-data │
          │                                      │
          │ Returns:                             │
          │ - TLS cert status                    │
          │ - TLS cert expiry date               │
          │ - DDoS protection status             │
          │ - Firewall enabled                   │
          │ - Last security check timestamp      │
          └──────────┬──────────────────────────┘
                     │
          ┌──────────▼──────────────────────────┐
          │ Fetch method breakdown               │
          │ _fetch_method_breakdown()            │
          │                                      │
          │ 1. Get method counts                 │
          │    ↓ get_all_hosts_method_count()   │
          │                                      │
          │ 2. Parse latest counts dict          │
          │    ↓ Extract method names & counts   │
          │                                      │
          │ 3. Sort by call count (descending)   │
          │    ↓ Top methods by usage            │
          │                                      │
          │ 4. Build MethodBreakdownItem list    │
          │    ↓ method, callCount, percentage   │
          └──────────┬──────────────────────────┘
                     │
          ┌──────────▼──────────────────────────┐
          │ Build RpcNodeDetailReport DTO        │
          │ - Report metadata                    │
          │ - Node overview (name, account)      │
          │ - Metrics (uptime, latency, etc)     │
          │ - Change percentages                 │
          │ - Security info                      │
          │ - Method breakdown                   │
          │ - Benchmarks (placeholder)           │
          │ - Incidents & insights               │
          │ - Trends (placeholder)               │
          └──────────┬──────────────────────────┘
                     │
          ┌──────────▼──────────────────────────┐
          │ Convert DTO to dict                  │
          │ Return JSON response                 │
          └──────────────────────────────────────┘
```

**Data Sources:**

- **Odoo DB**: `subscription_node` (node details)
- **Vizion APIs**:
  - `/api/item/get-latest-protocol-data` (uptime, latency, requests, errors)
  - `/api/item/get-security-monitor-data` (TLS, DDoS, firewall status)
- **RPC Functions**: `get_all_hosts_method_count()` (method breakdown)

---

## 4. Validator Fleet Report

**Endpoint:** `GET /api/v1/reports/validator-fleet?range=weekly&timezone=UTC`

```
┌────────────────────────────────────────────────────────────────────┐
│ Client Request with JWT Token                                      │
│ GET /api/v1/reports/validator-fleet?range=weekly&timezone=UTC     │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ↓
                 ┌──────────────────────┐
                 │ Authentication       │
                 │ (JWT verify + user)  │
                 └──────────┬───────────┘
                            │
                ┌───────────▼────────────────────┐
                │ Fetch all validator nodes      │
                │ for this account               │
                │ (node_type='validator')        │
                │ (subscription_node)            │
                └────────────┬──────────────────┘
                             │
          ┌──────────────────▼──────────────────┐
          │ Batch fetch reward snapshots        │
          │ validator.reward.snapshot           │
          │ (within period range)               │
          │ (all validator node_ids)            │
          └──────────┬───────────────────────────┘
                     │
          ┌──────────▼───────────────────────┐
          │ Group snapshots by validator      │
          │ (one validator = multiple snaps)  │
          └──────────┬──────────────────────┘
                     │
          ┌──────────▼──────────────────────────┐
          │ For each validator:                 │
          │                                     │
          │ 1. Aggregate snapshots              │
          │    ↓ SUM: total_stake               │
          │    ↓ SUM: total_rewards             │
          │    ↓ AVG: stake per snapshot        │
          │                                     │
          │ 2. Calculate APR                    │
          │    ↓ Formula: (rewards/stake) *     │
          │      (365/period_days) * 100        │
          │                                     │
          │ 3. Check if jailed                  │
          │    ↓ Parse validator_info JSON      │
          │    ↓ field: jailed (bool)           │
          │                                     │
          │ 4. Count slashing events            │
          │    ↓ snapshot.slashing_events       │
          │                                     │
          │ 5. Calculate validator score        │
          │    ↓ Scoring formula (see docs)     │
          │    ↓ Grade & Status                 │
          └──────────┬───────────────────────────┘
                     │
          ┌──────────▼──────────────────────┐
          │ Classify validators by status:   │
          │ Count:                           │
          │ - Active validators              │
          │ - Jailed validators              │
          │ - Total stake                    │
          │ - Total rewards                  │
          │ - Average APR                    │
          └──────────┬──────────────────────┘
                     │
          ┌──────────▼──────────────────────┐
          │ Calculate risk indicators:       │
          │                                  │
          │ 1. Slashing risk                 │
          │    ↓ Avg slashing events         │
          │    ↓ Level: low/medium/high      │
          │                                  │
          │ 2. Jailing risk                  │
          │    ↓ % jailed validators         │
          │    ↓ Level: low/medium/high      │
          │                                  │
          │ 3. Stake concentration          │
          │    ↓ Largest stake vs avg        │
          │    ↓ Risk assessment             │
          └──────────┬──────────────────────┘
                     │
          ┌──────────▼──────────────────────┐
          │ Generate insights:               │
          │ _generate_validator_fleet_..     │
          │ - Slashing alerts                │
          │ - Jailing alerts                 │
          │ - APR anomalies                  │
          └──────────┬──────────────────────┘
                     │
          ┌──────────▼──────────────────────┐
          │ Build ValidatorFleetReport DTO   │
          │ - Report metadata                │
          │ - Fleet summary                  │
          │ - Validator list with scores     │
          │ - Risk indicators                │
          │ - Incidents & insights           │
          │ - Trends (placeholder)           │
          └──────────┬──────────────────────┘
                     │
          ┌──────────▼──────────────────────┐
          │ Convert DTO to dict              │
          │ Return JSON response             │
          └──────────────────────────────────┘
```

**Data Sources:**

- **Odoo DB**:
  - `subscription_node` (validator node list)
  - `validator.reward.snapshot` (stake, rewards, slashing events)
  - `validator.performance.snapshot` (validator metadata)

---

## 5. Validator Node Detail Report

**Endpoint:** `GET /api/v1/reports/validator/{validatorId}?range=weekly&timezone=UTC`

```
┌────────────────────────────────────────────────────────────────────┐
│ Client Request with JWT Token                                      │
│ GET /api/v1/reports/validator/{validatorId}?range=weekly          │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ↓
                 ┌──────────────────────┐
                 │ Authentication       │
                 │ (JWT verify + user)  │
                 └──────────┬───────────┘
                            │
                ┌───────────▼────────────────┐
                │ Find validator by ID       │
                │ (UUID or database ID)      │
                │ (subscription_node)        │
                └────────────┬───────────────┘
                             │
          ┌──────────────────▼──────────────────┐
          │ Fetch reward snapshots              │
          │ validator.reward.snapshot           │
          │ (within period range)               │
          │ (for this validator_id)             │
          └──────────┬───────────────────────────┘
                     │
          ┌──────────▼──────────────────────────┐
          │ Aggregate snapshot data             │
          │                                     │
          │ 1. SUM: total_stake                 │
          │    ↓ Sum of all snapshots.stake     │
          │                                     │
          │ 2. SUM: total_rewards               │
          │    ↓ Sum of all snapshots.rewards   │
          │                                     │
          │ 3. AVG: stake per snapshot          │
          │    ↓ Average stake value            │
          │                                     │
          │ 4. LAST: jailed status              │
          │    ↓ Latest snapshot jailed field   │
          │                                     │
          │ 5. SUM: slashing_events             │
          │    ↓ Total slashing events          │
          └──────────┬───────────────────────────┘
                     │
          ┌──────────▼──────────────────────────┐
          │ Calculate APR                       │
          │ Formula:                            │
          │ (total_rewards / total_stake) *     │
          │ (365 / period_days) * 100           │
          └──────────┬──────────────────────────┘
                     │
          ┌──────────▼──────────────────────────┐
          │ Check if validator jailed           │
          │ Parse validator_info JSON           │
          │ field: jailed (bool)                │
          └──────────┬──────────────────────────┘
                     │
          ┌──────────▼──────────────────────────┐
          │ Calculate validator score           │
          │ Scoring formula:                    │
          │ - Uptime: 40% (always high)         │
          │ - APR: 30% (normalized)             │
          │ - Slashing: -15% per event (max)    │
          │ - Jailed: -30% if true              │
          │                                     │
          │ Grade & Status determination        │
          └──────────┬──────────────────────────┘
                     │
          ┌──────────▼──────────────────────────┐
          │ Fetch delegators info               │
          │ _fetch_delegators_info()            │
          │                                     │
          │ 1. Parse validator_info JSON        │
          │    ↓ Extract valoper field          │
          │    ↓ Example: cosmosvaloper...      │
          │                                     │
          │ 2. RPC call: get_validator_...      │
          │    ↓ _compute_validator_delegations │
          │    ↓ Returns: delegator list        │
          │                                     │
          │ 3. Count total delegators           │
          │    ↓ delegator_count from snapshot  │
          │                                     │
          │ 4. Build top 10 delegators list     │
          │    ↓ Each: address, stake, rewards  │
          │    ↓ Sorted by stake (descending)   │
          └──────────┬──────────────────────────┘
                     │
          ┌──────────▼──────────────────────────┐
          │ Build network comparison            │
          │ (placeholder for future)             │
          │ - Network average APR                │
          │ - Network avg validator stake       │
          │ - Percentile ranks                  │
          └──────────┬──────────────────────────┘
                     │
          ┌──────────▼──────────────────────────┐
          │ Generate insights:                  │
          │ _generate_validator_node_insights   │
          │ - Jailing alert                     │
          │ - Low delegators warning            │
          │ - APR anomaly                       │
          │ - High slashing alert               │
          └──────────┬──────────────────────────┘
                     │
          ┌──────────▼──────────────────────────┐
          │ Build ValidatorNodeDetailReport DTO │
          │ - Report metadata                   │
          │ - Validator overview                │
          │ - Metrics (stake, rewards, APR)     │
          │ - Change percentages                │
          │ - Delegators info (top 10)          │
          │ - Network comparison                │
          │ - Incidents & insights              │
          │ - Trends (placeholder)              │
          └──────────┬──────────────────────────┘
                     │
          ┌──────────▼──────────────────────────┐
          │ Convert DTO to dict                 │
          │ Return JSON response                │
          └──────────────────────────────────────┘
```

**Data Sources:**

- **Odoo DB**:
  - `subscription_node` (validator details)
  - `validator.reward.snapshot` (stake, rewards, slashing, jailed status)
  - `validator.performance.snapshot` (validator metadata)
- **RPC Functions**: `_compute_validator_delegations()` (delegators list)

---

## Aggregation Strategy

| Metric            | Aggregation Type | Purpose                      |
| ----------------- | ---------------- | ---------------------------- |
| `uptime_pct`      | AVG              | Average uptime across period |
| `latency_ms`      | AVG              | Average latency              |
| `request_count`   | SUM              | Total requests in period     |
| `error_count`     | SUM              | Total errors in period       |
| `stake`           | AVG / SUM        | Stake values                 |
| `rewards`         | SUM              | Total rewards earned         |
| `apr`             | AVG              | Average APR                  |
| `jailed`          | LAST             | Most recent jailed status    |
| `slashing_events` | COUNT / SUM      | Total slashing events        |

---

## Scoring Logic

### RPC Node Score (0-100)

```
score = (uptime_pct * 0.50) +
        (latency_score * 0.30) +
        (error_rate_score * 0.20)

where:
  latency_score = max(0, 100 - (latency_ms / 10)) [capped at 100]
  error_rate_score = max(0, 100 - (error_rate_pct * 10)) [capped at 100]
```

### Validator Score (0-100)

```
score = (uptime_pct * 0.40) +
        (apr_score * 0.30) +
        slashing_penalty +
        jailing_penalty

where:
  apr_score = normalized 10-20% range
  slashing_penalty = -15% per slashing event (max -45%)
  jailing_penalty = -30% if jailed
```

### Grade Determination

- **A Grade**: Score ≥ 90
- **B Grade**: Score ≥ 75
- **C Grade**: Score < 75

### Status Determination

- **Good**: Score ≥ 80
- **Warning**: Score ≥ 60
- **Critical**: Score < 60

---

## Period Calculation

### Weekly Report

- **Range**: Monday 00:00 to Sunday 23:59 (in user's timezone)
- **Duration**: 7 days

### Monthly Report

- **Range**: 1st day 00:00 to last day 23:59 (in user's timezone)
- **Duration**: 28-31 days

All timestamps are calculated with timezone awareness using `pytz` library.

---

## API Response Format

All endpoints return standardized JSON response:

```json
{
  "success": true,
  "data": {
    "meta": {
      "accountId": 123,
      "accountName": "Acme Inc",
      "periodStart": "2026-02-02T00:00:00Z",
      "periodEnd": "2026-02-08T23:59:59Z",
      "range": "weekly",
      "timezone": "UTC",
      "nodeId": "uuid-or-id (optional)",
      "nodeName": "Node Name (optional)",
      "validatorId": "uuid-or-id (optional)",
      "validatorName": "Validator Name (optional)"
    },
    "overview": {
      /* varies by endpoint */
    },
    "incidents": [],
    "insights": [],
    "trends": []
  }
}
```

---

## Error Handling

| Status | Error                         | Cause                               |
| ------ | ----------------------------- | ----------------------------------- |
| 400    | Invalid range parameter       | range must be 'weekly' or 'monthly' |
| 400    | Node/Validator ID is required | Missing URL parameter               |
| 401    | Authentication required       | Missing/invalid JWT token           |
| 404    | Node/Validator not found      | Invalid node or validator ID        |
| 500    | Internal server error         | Unexpected exception                |

---

## Module Files

```
zeeve_base/utils/reports/
├── __init__.py           # Module initialization
├── helpers.py            # Date range calculation, formatting utilities
├── aggregation.py        # Time-series aggregation logic
├── scoring.py            # Scoring and grade determination
├── clients.py            # Data fetching (Vizion, ORM, RPC)
├── models.py             # Response DTOs (dataclasses)
├── services.py           # Core business logic (5 report functions)
└── README.md             # This file
```

---

## Future Enhancements

- [ ] Daily trend data aggregation from Vizion time-series
- [ ] Incident data parsing from Vizion trigger events
- [ ] Network-wide benchmarks for comparison
- [ ] Slashing event details and analysis
- [ ] Email template rendering using same service functions
- [ ] Report caching for repeated requests
- [ ] Historical trend analysis and predictions
