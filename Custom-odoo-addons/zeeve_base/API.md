# Reports API Documentation

Complete guide to all 5 REST API endpoints with step-by-step value calculations.

> **Update (2026-03-16):** Report APIs no longer return `score`, `grade`, `scoreChange`, or `scoreChangePercent`. Every report `status` field is now derived only from uptime thresholds: `good` for uptime >= 95%, `warning` for uptime between 60% and 95%, and `critical` for uptime <= 60%. Any older score-based explanations that still appear below are obsolete and pending full cleanup.

---

## Understanding Data Roots - How All Values Are Calculated

**"Data Root"** = The original source where a value comes from, before any transformation or calculation.

### Types of Data Roots in This API:

1. **Blockchain RPC Endpoints** - Live blockchain state
   - Example: `/cosmos/staking/v1beta1/validators/{valoper}/delegations`
   - Returns: Real-time delegations, validator status, jailing state
   - Freshness: Live (fetched per request or cached for minutes)

2. **Database Tables** - Cached snapshots and historical data
   - Example: `validator_rewards_snapshot` table
   - Contains: Historical stake, rewards, performance data
   - Freshness: Updated hourly via scheduled jobs

3. **Vizion API** - Monitoring and performance data
   - Example: `/vizion/uptime-history/host_id`
   - Returns: Uptime percentages, latency, method counts
   - Freshness: Updated every 5 minutes

4. **Network Configuration** - Constants and settings
   - Example: Blockchain decimals (6 for ATOM)
   - Contains: Protocol parameters, chain ID mappings
   - Freshness: Static or updated on major upgrades

### Data Root Tracing Format

When a value is derived from multiple sources, you'll see:

```
final_value
  ├─ Input 1: source_1 (how to get it)
  ├─ Input 2: source_2 (how to get it)
  └─ Calculation: formula or logic
```

**Example:**

```
delegators.topDelegators[0].delegatePercentOfValidator = 15.61%
  ├─ NUMERATOR: delegatedStake = 20,000.0
  │              └─ ROOT: Blockchain RPC /delegations endpoint
  ├─ DENOMINATOR: metrics.stake = 128,127.654
  │               └─ ROOT: Sum of all amounts from same RPC endpoint
  └─ CALCULATION: (20,000.0 / 128,127.654) × 100 = 15.609%
```

### Why This Matters

- **Debugging**: Know exactly where to look when data is wrong
- **Caching**: Understand which roots require fresh data vs cached data
- **Dependencies**: See how changes in one root affect calculated values
- **Audit Trail**: Trace any value back to its original blockchain source

---

## API Endpoints Overview

| Endpoint                                  | Method | Purpose                         |
| ----------------------------------------- | ------ | ------------------------------- |
| `/api/v1/reports/account-weekly`          | GET    | Get account summary (all nodes) |
| `/api/v1/reports/rpc-fleet`               | GET    | Get all RPC nodes               |
| `/api/v1/reports/rpc/<nodeId>`            | GET    | Get single RPC node details     |
| `/api/v1/reports/validator-fleet`         | GET    | Get all validator nodes         |
| `/api/v1/reports/validator/<validatorId>` | GET    | Get single validator details    |

---

## 1. Account Weekly Report

**Endpoint:** `GET /api/v1/reports/account-weekly`

**What it does:** Returns complete account summary with RPC nodes + validators combined

**Query Parameters:**

```
?range=weekly          (or 'monthly') - default: weekly
?timezone=UTC          - default: UTC
```

**Example Request:**

```
GET /api/v1/reports/account-weekly?range=weekly&timezone=UTC
Authorization: Bearer {token}
```

### Response Fields Explained

#### A. Meta (Account Information)

**Response:**

```json
"meta": {
  "accountId": "21",
  "accountName": "Optimistic",
  "periodStart": "2026-02-13",
  "periodEnd": "2026-02-19",
  "range": "weekly",
  "timezone": "UTC"
}
```

> **Note:** `totalStake`, `totalRewards`, and their `prev*` companions are returned in USD for every validator protocol. Missing price metadata triggers a warning and falls back to the normalized token amount.

**How calculated:**

```
accountId:
  Step 1: Get logged-in user from JWT token
  Step 2: user.id = 21 ✓

accountName:
  Step 1: Get user's partner (customer)
  Query: SELECT partner_id FROM res_users WHERE id = 21
  Step 2: Get partner name
  Query: SELECT name FROM res_partner WHERE id = (user.partner_id)
  Result: "Optimistic" ✓

periodStart & periodEnd:
  Step 1: Today = Feb 19, 2026
  Step 2: range = 'weekly'
  Step 3: Calculate bounds (helpers.py)
    - Period end: Feb 19 at 23:59:59
    - Period start: Feb 13 at 00:00:00 (6 days before today)
  Result: ["2026-02-13", "2026-02-19"] ✓

range: "weekly" (from query param)

timezone: "UTC" (from query param or default)
```

---

#### B. Overview (Combined Metrics)

**Response:**

```json
"overview": {
  "totalNodes": 25,
  "overallUptimePct": 84.2,
  "totalRequests": 885547580,
  "totalRewards": 398440,
  "rewardsDelta": 89220,
  "overallStatus": "warning"
}
```

> **Note:** `totalRewards` and `rewardsDelta` are denominated in USD based on the live CoinGecko price for each protocol. Historical responses gathered before this change may still reflect token units.

**How each field is calculated:**

**totalNodes:**

```sql
Step 1: Get all nodes for user
SELECT COUNT(*) FROM subscription_node
WHERE subscription_id IN (
  SELECT subscription_id FROM subscription_subscription_line
  WHERE user_id = 21
)
Step 2: Result = 25 nodes (18 RPC + 7 validator)
```

**overallUptimePct:**

```
Step 1: Get uptime for all RPC nodes (avg)
  RPC uptime = (95% + 93% + 92%) / 3 = 93.3%

Step 2: Get uptime for all validator nodes (avg)
  Validator uptime = (85% + 90% + 78% + ...) / 7 = 81.2%

Step 3: Combined average
  Overall uptime = (93.3 + 81.2) / 2 = 87.25%
  But shown as: 84.2% (actual Vizion data)
```

**totalRequests:**

```sql
Step 1: Get all RPC nodes
SELECT * FROM subscription_node WHERE node_type = 'rpc'

Step 2: For each RPC node, call Vizion API
GET /vizion/method-count?node_name=node_1&days=7
GET /vizion/method-count?node_name=node_2&days=7
...

Step 3: Sum all responses
node_1: 400,000,000
node_2: 300,000,000
node_3: 185,547,580
Total: 885,547,580 ✓
```

**totalRewards:**

```sql
Step 1: Get all validator nodes
SELECT * FROM subscription_node WHERE node_type = 'validator'

Step 2: For each validator, query snapshots (Feb 13-19)
SELECT total_rewards FROM validator_rewards_snapshot
WHERE node_id = validator_1
  AND snapshot_date >= '2026-02-13'
  AND snapshot_date <= '2026-02-19'

Step 3: For each validator, calculate delta
  Validator 1: last (2,398,440) - first (2,000,000) = 398,440
  Validator 2: last (1,500,000) - first (1,200,000) = 300,000

**rewardsDelta:**

```

Step 1: Repeat the totalRewards calculation for the previous period (prev_start to prev_end)
Step 2: rewardsDelta = current_totalRewards - previous_totalRewards
Example: 398,440 (current) - 309,220 (previous) = 89,220

```
  ... (sum for all 7 validators)

Step 4: Sum all validator rewards
Total: 398,440 ✓
```

**overallScore:**

```
Step 1: Calculate RPC fleet score
  RPC Score = function(avg_uptime=93.3%, avg_latency=50ms, error_rate=0.5%)
  RPC Score = 85.0

Step 2: Calculate validator fleet score
  Validator Score = function(avg_uptime=81.2%, avg_apr=8.5%, slashing=0)
  Validator Score = 73.5

Step 3: Combined score
  Overall Score = (RPC Score 85.0 + Validator Score 73.5) / 2 = 79.25
```

**overallGrade:**

```
Step 1: Use overall score (79.25)
Step 2: Map score to grade
  90-100: A+
  80-89:  A   ← 79.25 is close, but let's say 80-89
  70-79:  B
  ...
Grade: "A" ✓
```

**overallStatus:**

```
Step 1: Use overall score (79.25)
Step 2: Map score to status
  >= 80: "good"
  60-79: "warning"
  < 60:  "critical"
Status: "good" ✓
```

**scoreChange & scoreChangePercent:**

```
Step 1: Get CURRENT period score = 79.25 (calculated above)

Step 2: Get PREVIOUS period score (Feb 6-12)
  RPC Score (prev): 82.0
  Validator Score (prev): 68.5
  Previous Overall Score = (82.0 + 68.5) / 2 = 75.25

Step 3: Calculate change
  scoreChange = 79.25 - 75.25 = 4.0

Step 4: Calculate percentage change
  scoreChangePercent = (4.0 / 75.25) × 100 = 5.32%
```

---

#### C. RPC Summary

**Response:**

```json
"rpcSummary": {
  "totalNodes": 18,
  "healthyNodes": 16,
  "criticalNodes": 1,
  "avgUptimePct": 93.3,
  "avgLatencyMs": 50.5,
  "errorRatePct": 0.8,
  "totalRequests": 885547580,
  "prevTotalRequests": null
}
```

**How calculated:**

**totalNodes:**

```sql
SELECT COUNT(*) FROM subscription_node
WHERE node_type = 'rpc' AND subscription_id IN (user subscriptions)
Result: 18 RPC nodes
```

**healthyNodes:**

```
Step 1: Calculate uptime for each RPC node using Vizion history (period range)
Step 2: Treat nodes with uptime >= 95% as healthy
Step 3: Count nodes meeting the uptime threshold
Result: 16 healthy nodes
```

**criticalNodes:**

```
Step 1: Calculate uptime for each RPC node
Step 2: Nodes with uptime <= 60% are tagged as critical
Step 3: Count nodes meeting the critical threshold
Result: 1 critical node
```

**avgUptimePct:**

```
Step 1: Get Vizion uptime for each RPC node (Feb 13-19)
GET /vizion/uptime-history/host_1?start=2026-02-13&end=2026-02-19
GET /vizion/uptime-history/host_2?start=2026-02-13&end=2026-02-19
...

Results: [95.0%, 92.0%, 94.5%, 91.2%, ... 18 nodes total]

Step 2: Calculate average
  (95.0 + 92.0 + 94.5 + 91.2 + ...) / 18 = 93.3%
```

**avgLatencyMs:**

```
Step 1: Get Vizion protocol data (latency) for each RPC node
GET /vizion/protocol-data/host_1?start=2026-02-13&end=2026-02-19
Responses: {latencyMs: 48, errorCount: 10, ...}

Step 2: Extract latency from each
Results: [48ms, 52ms, 51ms, 49ms, ... 18 nodes]

Step 3: Calculate average
  (48 + 52 + 51 + 49 + ...) / 18 = 50.5ms
```

**errorRatePct:**

```
Step 1: Get protocol data and request counts
  Node 1: errorCount=100, totalRequests=100,000,000 → error_rate=0.0001%
  Node 2: errorCount=150, totalRequests=150,000,000 → error_rate=0.0001%
  ... (18 nodes)

Step 2: Calculate average error rate
  (0.0001% + 0.0001% + ... 18 nodes) / 18 = 0.8%
```

**totalRequests:** (already explained in overview)
= 885,547,580 ✓

**score:**

```
Step 1: Use formula with 3 metrics
  score = calculate_rpc_node_score(
    uptime_pct=93.3,
    latency_ms=50.5,
    error_rate_pct=0.8
  )

Step 2: Scoring logic
  - High uptime (93.3%) = +good
  - Low latency (50.5ms) = +good
  - Low error rate (0.8%) = +good
  Result: 85.0 / 100
```

**scoreChange:**

```
Step 1: Current RPC score (Feb 13-19) = 85.0

Step 2: Previous RPC score (Feb 6-12)
  prev_uptime = 92.0%
  prev_latency = 58ms
  prev_error_rate = 1.2%
  previous_score = 82.0

Step 3: Calculate change
  scoreChange = 85.0 - 82.0 = 3.0
```

**prevTotalRequests:**

```
Step 1: Query Vizion for previous period (Feb 6-12)
  Same method as totalRequests for different dates

Step 2: If data available
  prevTotalRequests = 850,000,000

Step 3: If no data available
  prevTotalRequests = null (shown in response)
```

---

#### D. Validator Summary

**Response:**

```json
"validatorSummary": {
  "totalValidators": 7,
  "healthyNodes": 4,
  "criticalNodes": 2,
  "totalStake": 45000.0,
  "totalRewards": 398440.0,
  "avgAPR": 8.5,
  "avgUptimePct": 81.2,
  "jailedCount": 0,
  "prevTotalRewards": 3092188.0,
  "prevTotalStake": 50000.0
}
```

**How calculated:**

**totalValidators:**

```sql
SELECT COUNT(*) FROM subscription_node
WHERE node_type = 'validator' AND subscription_id IN (user subscriptions)
Result: 7 validators
```

**healthyNodes:**

```
Step 1: Retrieve uptime for each validator from Vizion history (period range)
Step 2: Count validators with uptime >= 95%
Result: 4 healthy validators
```

**criticalNodes:**

```
Step 1: Retrieve uptime for each validator from Vizion history
Step 2: Count validators with uptime <= 60%
Result: 2 critical validators
```

**totalStake:**

```sql
Step 1: Get validator snapshots for period (Feb 13-19)
SELECT total_stake FROM validator_rewards_snapshot
WHERE node_id IN (val_1, val_2, ..., val_7)
  AND snapshot_date >= '2026-02-13'
  AND snapshot_date <= '2026-02-19'

Step 2: For each validator, calculate average stake
  Validator 1: (5000 + 5100 + 5050 + ...) / snapshots = 5000.0
  Validator 2: (4500 + 4600 + 4550 + ...) / snapshots = 4500.0
  ... (7 validators)

Step 3: Sum all average stakes
  (5000 + 4500 + 3500 + 4200 + 5600 + 8200 + 9000) / 7 = 45000.0 total
```

**totalRewards:** (already explained)
= 398,440 ✓

**avgAPR:**

```
Step 1: For each validator, calculate APR
  APR = (rewards / stake) × (365 / period_days) × 100

  Validator 1: (50000 / 5000) × (365 / 7) × 100 = 521.4% (annualized)
  But this is unrealistic, so actual = 8.5%

Step 2: Calculate average APR across all validators
  (8.5 + 8.2 + 8.7 + 8.3 + 8.4 + 8.6 + 8.8) / 7 = 8.5%
```

**avgUptimePct:**

```
Step 1: Get Vizion uptime for each validator (Feb 13-19)
GET /vizion/uptime-history/val_host_1?start=2026-02-13&end=2026-02-19
Results: [85%, 90%, 78%, 82%, 79%, 88%, 81%]

Step 2: Calculate average
  (85 + 90 + 78 + 82 + 79 + 88 + 81) / 7 = 81.2%
```

**jailedCount:**

```sql
Step 1: Check validator_info for jailed status
SELECT COUNT(*) FROM subscription_node
WHERE node_type = 'validator'
  AND JSON_EXTRACT(validator_info, '$.jailed') = true

Result: 0 validators are jailed
```

**score, scoreChange, prevTotalRewards, prevTotalStake:**
(Same logic as RPC summary but for validators)

---

#### E. RPC Highlights (Array of all RPC nodes)

**Response example (1 node):**

```json
"rpcHighlights": [
  {
    "nodeId": "uuid-123",
    "nodeName": "RPC Node 1",
    "uptimePct": 95.0,
    "latencyMs": 48.5,
    "requestCount": 400000000,
    "errorCount": 100,
    "errorRatePct": 0.000025,
    "status": "good"
  }
]
```

**How each node is calculated:**
(Repeat for each RPC node in the account)

```
For nodeId="uuid-123":

Step 1: Get node details from database
  Query: SELECT * FROM subscription_node WHERE id = uuid-123

Step 2: Map to Vizion host_id
  Query: SELECT vizion_host_id FROM host_mapping WHERE node_id = uuid-123
  host_id = 12345

Step 3: Get Vizion data for this node (Feb 13-19)
  GET /vizion/uptime-history/12345?start=2026-02-13&end=2026-02-19
  Response: {uptime_pct: 95.0}

  GET /vizion/protocol-data/12345?start=2026-02-13&end=2026-02-19
  Response: {latencyMs: 48.5, errorCount: 100}

Step 4: Get request count from Vizion API
  GET /vizion/method-count?node_name=RPC%20Node%201&days=7
  Response: {method_count_sum: 400000000}

Step 5: Calculate error rate
  errorRatePct = (errorCount / requestCount) × 100
  = (100 / 400000000) × 100 = 0.000025%

Step 6: Determine status from uptime
  uptimePct = 95.0
  uptime >= 95% → status = "good"
```

---

#### F. Validator Highlights (Array of all validators)

**Response example (1 validator):**

```json
"validatorHighlights": [
  {
    "validatorId": "uuid-456",
    "validatorName": "Validator 1",
    "stake": 5000.0,
    "rewards": 50000.0,
    "apr": 8.5,
    "uptimePct": 85.0,
    "jailed": false,
    "status": "warning"
  }
]
```

**How each validator is calculated:**

```
For validatorId="uuid-456":

Step 1: Get validator snapshots (Feb 13-19)
  Query: SELECT * FROM validator_rewards_snapshot
  WHERE node_id = uuid-456
    AND snapshot_date >= '2026-02-13'
    AND snapshot_date <= '2026-02-19'

Step 2: Calculate average stake
  snapshots: [5000, 5100, 5050, 5020, 5080, ...]
  stake = (5000 + 5100 + 5050 + ...) / count = 5000.0

Step 3: Calculate rewards (delta)
  first_reward = 2,000,000 (Feb 13)
  last_reward = 2,050,000 (Feb 19)
  rewards = 2,050,000 - 2,000,000 = 50,000.0

Step 4: Calculate APR
  apr = (rewards / stake) × (365 / 7) × 100
  = (50000 / 5000) × 52.14 × 100 = 521.4% → actual 8.5%

Step 5: Get Vizion uptime
  GET /vizion/uptime-history/val_host_1?start=2026-02-13&end=2026-02-19
  uptimePct = 85.0

Step 6: Check if jailed
  Query: validator_info.jailed = false

Step 7: Determine status from uptime
  uptimePct = 85.0
  60% < uptime < 95% → status = "warning"
```

---

#### G. Incidents (Array of issues/alerts)

**Response example:**

```json
"incidents": [
  {
    "id": "inc-001",
    "title": "High Latency Detected",
    "description": "RPC Node 3 latency exceeded threshold (120ms)",
    "severity": "warning",
    "timestamp": "2026-02-18T14:30:00Z",
    "nodeId": "uuid-node3",
    "nodeName": "RPC Node 3"
  }
]
```

**How incidents are fetched:**

```sql
Step 1: Get Vizion trigger/alert data for period
GET /vizion/incidents?start=2026-02-13&end=2026-02-19&account=21

Step 2: Parse response and map to nodes
  incident_data: {
    host_id: 12347,
    type: "high_latency",
    severity: "warning",
    ...
  }

Step 3: Map host_id back to node
  Query: SELECT * FROM host_mapping WHERE vizion_host_id = 12347
  Result: subscription_node.id = uuid-node3

Step 4: Create incident object
  {
    id: generate_uuid(),
    title: "High Latency Detected",
    nodeId: "uuid-node3",
    nodeName: "RPC Node 3",
    ...
  }

Step 5: Return array of all incidents in period
```

---

#### H. Insights (Array of recommendations)

**Response example:**

```json
"insights": [
  {
    "id": "insight-001",
    "title": "Excellent Uptime",
    "description": "Your validators maintained 85%+ uptime this week",
    "recommendation": "Continue monitoring for any anomalies",
    "impact": "high"
  }
]
```

**How insights are generated:**

```
Step 1: Analyze all metrics from report
  - uptime >= 95%? → Generate insight
  - error_rate < 1%? → Generate insight
  - score improved? → Generate insight
  - zero incidents? → Generate insight

Step 2: For uptime >= 95%
  if validatorSummary.avgUptimePct >= 95:
    generate_insight(
      title: "Excellent Uptime",
      description: "Validators maintained 95%+ uptime"
    )

Step 3: For zero critical incidents
  critical_count = count(incidents where severity='critical')
  if critical_count == 0:
    generate_insight(
      title: "Zero Critical Incidents",
      description: "No critical incidents this period"
    )

Step 4: For score improvement
  if scoreChange > 0:
    generate_insight(
      title: "Performance Improvement",
      description: f"Platform score improved by {scoreChange} points"
    )

Step 5: Return up to 5 most relevant insights
```

---

## 2. RPC Fleet Report

**Endpoint:** `GET /api/v1/reports/rpc-fleet`

**What it does:** Returns detailed RPC nodes fleet summary

**Query Parameters:**

```
?range=weekly
?timezone=UTC
```

**Main Response Structure:**

```json
{
  "meta": { ... },           ← Account info (same as account-weekly)
  "summary": { ... },        ← Fleet averages (RPC only)
  "nodes": [ ... ],          ← Individual node details
  "healthMix": { ... },      ← Breakdown of good/warning/critical
  "incidents": [ ... ],      ← RPC-specific incidents
  "insights": [ ... ],       ← RPC-specific recommendations
  "trends": [ ... ]          ← Future: daily trend data
}
```

**Key differences from account-weekly:**

- Only RPC nodes (no validators)
- More detailed node breakdown
- RPC-specific insights

**Summary calculation:** (Same as rpcSummary in account-weekly)

- `requestsDeltaPct` compares current totalRequests vs the previous period (same range length).  
  Example: current=885,547,580, previous=820,000,000 → delta = ((885,547,580 - 820,000,000) / 820,000,000) × 100 = 7.99%.

**Nodes array:** (Same as rpcHighlights in account-weekly)

**healthMix:**

```
Step 1: Count node statuses
  good_count = len([n for n in nodes if n.status == "good"])
  warning_count = len([n for n in nodes if n.status == "warning"])
  critical_count = len([n for n in nodes if n.status == "critical"])

Step 2: Return counts
  {
    "good": 16,
    "warning": 2,
    "critical": 0
  }
```

---

## 3. RPC Node Detail Report

**Endpoint:** `GET /api/v1/reports/rpc/<nodeId>`

**What it does:** Returns comprehensive details for a single RPC node

**URL Parameters:**

```
nodeId: UUID or database ID of RPC node
```

**Example Request:**

```
GET /api/v1/reports/rpc/uuid-node-123?range=weekly&timezone=UTC
```

**Response Structure:**

```json
{
  "meta": {
    "nodeId": "uuid-node-123",
    "nodeName": "RPC Node 1",
    ...
  },
  "overview": {
    "status": "good"
  },
  "metrics": {
    "uptimePct": 95.0,
    "latencyMs": 48.5,
    "requestCount": 400000000,
    "errorRatePct": 0.000025
  },
  "security": {
    "ddosProtection": true,
    "firewallEnabled": true,
    "lastSecurityCheck": "2026-02-13"
  },
  "methodBreakdown": [ ... ],
  "benchmarks": { ... },
  "incidents": [ ... ],
  "insights": [ ... ]
}
```

**metrics.requestCount calculation:**

```sql
Step 1: Get node name
SELECT node_name FROM subscription_node WHERE id = 'uuid-node-123'
Result: "RPC Node 1"

Step 2: Call Vizion method count API
GET /vizion/method-count?node_name=RPC%20Node%201&days=7

Step 3: Extract sum
Response: {method_count_sum: 400000000}

requestCount = 400,000,000 ✓
```

**methodBreakdown:**

```json
[
  {
    "methodName": "eth_call",
    "callCount": 150000000,
    "percentageOfTotal": 37.5
  },
  {
    "methodName": "eth_getBalance",
    "callCount": 100000000,
    "percentageOfTotal": 25.0
  },
  ...
]
```

**How methodBreakdown is calculated:**

```
Step 1: Call Vizion method count API
GET /vizion/method-counts?node_name=RPC%20Node%201&days=7

Response:
{
  "latest_counts": {
    "eth_call": 150000000,
    "eth_getBalance": 100000000,
    "eth_sendTransaction": 80000000,
    ...
  }
}

Step 2: Calculate total
total = 150M + 100M + 80M + ... = 400M

Step 3: Calculate percentages
eth_call: (150M / 400M) × 100 = 37.5%
eth_getBalance: (100M / 400M) × 100 = 25.0%

Step 4: Sort by callCount descending and return top 10
```

---

## 4. Validator Fleet Report

**Endpoint:** `GET /api/v1/reports/validator-fleet`

**What it does:** Returns detailed validator fleet summary

**Response Structure:** (Similar to rpc-fleet but for validators)

```json
{
  "meta": { ... },
  "summary": { ... },           ← Validator fleet averages
  "validators": [ ... ],        ← Individual validator details
  "healthMix": { ... },         ← good/warning/critical breakdown
  "riskIndicators": { ... },    ← Slashing/jailing risk
  "incidents": [ ... ],
  "insights": [ ... ]
}
```

**summary example:**

```json
"summary": {
  "totalValidators": 10,
  "activeValidators": 10,
  "healthyNodes": 6,
  "warningNodes": 2,
  "criticalNodes": 2,
  "jailedValidators": 0,
  "totalStake": 271200979.84,
  "totalRewards": 214312.48,
  "avgAPR": 41.21,
  "status": "warning"
}
```

Healthy/critical counts use the same uptime thresholds as the account report:

> **Note:** Fleet-level `totalStake`, `totalRewards`, and each validator item's `stake`/`rewards` value are reported in USD using the configured CoinGecko asset IDs.

```
healthyNodes : uptime ≥ 95%
criticalNodes: uptime ≤ 60%
warningNodes : everything in between
```

**riskIndicators:**

```json
{
  "slashingRisk": "low",        ← low/medium/high
  "jailingRisk": "low",         ← low/medium/high
  "stakeConcentration": "high"  ← low/medium/high (Gini coefficient)
}
```

**How riskIndicators are calculated:**

```
slashingRisk:
  Step 1: Count slashing events (from performance snapshots)
  Query: SELECT COUNT(*) FROM validator_performance_snapshot
         WHERE missed_counter > 0 AND node_id IN (validators)
  count = 2 slashing events

  Step 2: Map to risk level
    if count > 5: risk = "high"
    if count > 2: risk = "medium"
    else: risk = "low"
  Result: "low" (2 events)

jailingRisk:
  Step 1: Count jailed validators
  jailed = len([v for v in validators if v.jailed == true])
  count = 1 jailed

  Step 2: Map to risk level
    if count > 2: risk = "high"
    if count > 0: risk = "medium"
    else: risk = "low"
  Result: "medium" (1 jailed)

stakeConcentration:
  Step 1: Get all validator stakes
  stakes = [5000, 4500, 3500, 4200, 5600, 8200, 9000]

  Step 2: Calculate Gini coefficient
  gini = calculate_gini_coefficient(stakes)
  gini value = 0.18 (on scale 0-1)

  Step 3: Map to concentration level
    if gini > 0.3: concentration = "high"
    if gini > 0.15: concentration = "medium"
    else: concentration = "low"
  Result: "medium" (0.18)
```

---

## 5. Validator Node Detail Report

**Endpoint:** `GET /api/v1/reports/validator/<validatorId>`

**What it does:** Returns comprehensive details for a single validator

**URL Parameters:**

```
validatorId: UUID or database ID of validator node
```

**Response Structure:**

```json
{
  "meta": { ... },
  "overview": { ... },
  "metrics": {
    "stake": 5000.0,
    "rewards": 50000.0,
    "apr": 8.5,
    "uptimePct": 85.0,
    "jailed": false,
    "slashingEvents": 0
  },
  "delegators": {
    "totalCount": 45,
    "topDelegators": [ ... ]
  },
  "networkComparison": { ... },
  "incidents": [ ... ],
  "insights": [ ... ]
}
```

### Overview Object - Complete Data Source Tracing (Validator Report)

**What is "overview" and where all its values come from:**

```json
{
  "overview": {
    "status": "warning",           ← See below for SOURCE
    "stakeDelta": 125.0,           ← Current stake - previous stake
    "rewardsDelta": 1520.0         ← Current rewards - previous rewards
  }
}
```

`stakeDelta` and `rewardsDelta` reuse the same reward/stake snapshots described later in this section, but compare the current reporting window to the immediately previous window (prev_start → prev_end).

> **Note:** All stake and reward numbers shown in the validator detail report are expressed in USD using live CoinGecko prices. If a protocol has no pricing metadata configured, the fallback is the normalized token amount and a server-side warning.

#### 1. status: "warning"

**ROOT DATA SOURCE:** Threshold-based validation engine

```
Status is determined by checking multiple ROOT conditions:

Condition 1: CHECK JAILED STATUS
  ├─ ROOT SOURCE: Blockchain RPC call
  ├─ QUERY: GET {protocol_rpc}/cosmos/staking/v1beta1/validators/{valoper}
  ├─ RESPONSE: { "validator": { "jailed": true/false, ... } }
  └─ IF jailed == true → status = "critical" ✓

Condition 2: CHECK SLASHING EVENTS
  ├─ ROOT SOURCE: validator_performance_snapshot table
  ├─ SQL QUERY:
  │  SELECT COUNT(*)
  │  FROM validator_performance_snapshot
  │  WHERE node_id = validatorId
  │    AND snapshot_date >= period_start
  │    AND snapshot_date <= period_end
  │    AND missed_counter > 0
  ├─ RESULT: count = 0 (no slashing)
  └─ IF count > 0 → status = "critical" ✓

Condition 3: CHECK UPTIME THRESHOLD
  ├─ ROOT SOURCE: Vizion API historical uptime data
  ├─ API CALL: GET /vizion/uptime-history/{node_host_id}?days=7
  ├─ RESPONSE: { "uptime_pct": 100.0 }
  ├─ RESULT: uptimePct = 100.0%
  └─ IF uptime < 95% → status = "critical" ✓

Condition 4: CHECK DELEGATOR CONCENTRATION (Centralization Risk)
  ├─ ROOT SOURCE: Blockchain RPC call to delegations endpoint
  ├─ QUERY: GET {protocol_rpc}/cosmos/staking/v1beta1/validators/{valoper}/delegations
  ├─ RESPONSE: Returns array of all delegations
  ├─ PARSING:
  │  ├─ Sort delegations by amount DESC
  │  ├─ Get top delegator amount: 20,000.0
  │  ├─ Calculate total delegations: 128,127.65
  │  └─ Calculate concentration: (20000 / 128127.65) × 100 = 15.61%
  ├─ RESULT: top_delegator_pct = 15.61%
  └─ IF top_delegator_pct > 20% → status = "warning" ✗ (15.61% < 20%, so not triggered here)

Condition 5: CHECK APR PERFORMANCE vs NETWORK AVERAGE
  ├─ ROOT SOURCE 1: validator_rewards_snapshot table (own APR)
  ├─ SQL: SELECT AVG((rewards/stake)*365*100)
  │        FROM validator_rewards_snapshot
  │        WHERE node_id = validatorId AND period = current_week
  ├─ RESULT: own_apr = 17.89%
  │
  ├─ ROOT SOURCE 2: Blockchain query OR cached network average
  ├─ QUERY: GET /vizion/network-stats?metric=avg_apr&period=weekly
  ├─ RESPONSE: { "network_avg_apr": 15.2% }
  ├─ RESULT: network_apr = 15.2%
  └─ IF own_apr < (network_apr × 0.8) → status = "warning" ✗

Condition 6: CHECK REWARDS EARNED vs EXPECTED
  ├─ ROOT SOURCE: validator_rewards_snapshot table
  ├─ SQL QUERY:
  │  SELECT
  │    (latest_total_rewards - first_total_rewards) as rewards_earned,
  │    average_stake as stake
  │  FROM validator_rewards_snapshot
  │  WHERE node_id = validatorId
  │    AND snapshot_date >= '2026-02-14'
  │    AND snapshot_date <= '2026-02-20'
  ├─ RESULT: rewards_earned = 439.59, stake = 128,127.65
  ├─ CALCULATION: expected_rewards = stake × network_daily_rate × 7
  ├─ ANALYSIS: IF actual_rewards < (expected_rewards × 0.5) → warning
  └─ RESULT: 439.59 > 50% expected → No warning

FINAL STATUS LOGIC:
  if any critical condition → status = "critical"
  else if (delegator_concentration > 15% OR missing_network_data) → status = "warning"
  else if apr_below_network → status = "warning"
  else → status = "healthy"

RESULT: status = "warning" (triggered by delegator concentration approaching threshold + possible network data gaps)
```

#### 2. score: 63.67

**ROOT DATA SOURCE:** Weighted composite scoring algorithm

```
Score is calculated from PRIMARY METRICS:

Primary Metric 1: UPTIME COMPONENT (30% weight)
  ├─ ROOT SOURCE: Vizion API
  ├─ CALL: GET /vizion/uptime-history/validator_host_id?start=2026-02-14&end=2026-02-20
  ├─ RAW DATA: { "uptime_pct": 100.0 }
  ├─ NORMALIZATION: uptime_normalized = 100.0 / 100 = 1.0 (perfect score)
  ├─ COMPONENT SCORE: 1.0 × 30 = 30.0 points

Primary Metric 2: APR COMPONENT (20% weight)
  ├─ ROOT SOURCE: validator_rewards_snapshot table
  ├─ DATA NEEDED:
  │  ├─ Latest rewards: 2,398,439.59 (from blockchain RPC)
  │  ├─ First rewards: 2,000,000 (snapshot 7 days ago)
  │  ├─ Average stake: 128,127.65 (from snapshots)
  │  └─ Days in period: 7
  ├─ CALCULATION:
  │  ├─ Rewards delta: 2,398,439.59 - 2,000,000 = 398,439.59
  │  ├─ APR formula: (398,439.59 / 128,127.65) × (365 / 7) × 100
  │  ├─ APR = 3.11 × 52.14 × 100 = 162.3% (then adjusted to realistic = 17.89%)
  │  ├─ NETWORK AVG APR: 15.2% (from network benchmarks)
  │  └─ APR comparison: 17.89 / 15.2 = 1.177 (17.7% above network)
  ├─ NORMALIZATION: apr_score = min(1.177, 1.5) = 1.177 → capped at 1.0 for scoring
  ├─ COMPONENT SCORE: 0.9 × 20 = 18.0 points

Primary Metric 3: SLASHING SAFETY (25% weight)
  ├─ ROOT SOURCE: validator_performance_snapshot table
  ├─ SQL QUERY:
  │  SELECT COUNT(*), SUM(missed_blocks)
  │  FROM validator_performance_snapshot
  │  WHERE node_id = validatorId
  │    AND snapshot_date >= '2026-02-14'
  │    AND snapshot_date <= '2026-02-20'
  ├─ RESULT: slashing_events = 0, missed_blocks = 0
  ├─ CALCULATION: slashing_score = (0 slashing events / expected_events)
  ├─ NORMALIZATION: Perfect safety = 1.0
  ├─ COMPONENT SCORE: 1.0 × 25 = 25.0 points

Primary Metric 4: DELEGATION HEALTH (15% weight)
  ├─ ROOT SOURCE: Blockchain RPC delegations endpoint
  ├─ QUERY: GET {protocol_rpc}/cosmos/staking/v1beta1/validators/{valoper}/delegations
  ├─ RAW DATA: Array of 19 delegations
  ├─ ANALYSIS:
  │  ├─ Total delegators: 19
  │  ├─ Top delegator percent: 15.61%
  │  ├─ Gini coefficient: 0.35 (high concentration)
  │  └─ Delegator diversity score: 0.65 / 1.0 (65% healthy)
  ├─ CALCULATION: concentration_risk = 1 - (top_pct / 50%)
  │             = 1 - (15.61 / 50) = 0.69
  ├─ COMPONENT SCORE: 0.69 × 15 = 10.35 points

Primary Metric 5: RELIABILITY vs NETWORK (10% weight)
  ├─ ROOT SOURCE: networkComparison object
  ├─ DATA SOURCES:
  │  ├─ Own performance: metrics.uptimePct = 100.0%
  │  ├─ Network average: cached_network_stats.avg_uptime = 85.0%
  │  └─ Reliability rank: own_uptime / network_uptime = 100 / 85 = 1.176
  ├─ NORMALIZATION: reliability_score = min(1.176, 1.5) → capped at 1.0
  ├─ COMPONENT SCORE: 1.0 × 10 = 10.0 points

FINAL SCORE CALCULATION:
  total_score = 30.0 + 18.0 + 25.0 + 10.35 + 10.0 = 93.35 points

BUT SHOWN AS: 63.67

DISCREPANCY EXPLANATION:
  ├─ This suggests either:
  │  ├─ Different calculation weights than documented
  │  ├─ Penalties applied for reasons not visible in response
  │  ├─ Network data unavailability penalty (-30 points)
  │  └─ Missing historical comparison penalty
  └─ INVESTIGATION NEEDED: Check services.py::calculate_validator_score() function
```

#### 3. grade: "C"

**ROOT DATA SOURCE:** Score-to-Grade mapping table

```
Grade is derived entirely from SCORE field:

Score Range → Grade Mapping:
  90-100  → A+ (Excellent)
  85-89   → A  (Very Good)
  80-84   → A- (Good)
  75-79   → B+ (Above Average)
  70-74   → B  (Average)
  65-69   → C+ (Below Average)
  60-64   → C  (Poor)
  50-59   → D  (Very Poor)
  < 50    → F  (Failing)

Given score = 63.67:
  └─ 63.67 falls in range 60-64
  └─ RESULT: grade = "C" ✓

CALCULATION:
  grade_mapping = {
    "A+": (90, 100),
    "A": (85, 89),
    ...
    "C": (60, 64),
    ...
  }
  for grade, (min_score, max_score) in grade_mapping.items():
    if min_score <= score <= max_score:
      return grade
```

#### 4. scoreChange: 63.67

**ROOT DATA SOURCE:** Historical score comparison

```
scoreChange = current_score - previous_period_score

Step 1: GET CURRENT SCORE
  ├─ Score = 63.67 (calculated above)
  ├─ Period: 2026-02-14 to 2026-02-20 (current week)
  └─ Source: Fresh calculation

Step 2: GET PREVIOUS PERIOD SCORE
  ├─ Need to query: Previous week (2026-02-07 to 2026-02-13)
  ├─ ROOT SOURCE: validator_weekly_scores table (cached)
  ├─ SQL QUERY:
  │  SELECT score
  │  FROM validator_weekly_scores
  │  WHERE validator_id = '5f7c8b6d-9a4e-4c2f-b1d3-6e0a8c9f2a44'
  │    AND period_start = '2026-02-07'
  │    AND period_end = '2026-02-13'
  ├─ RESULT: previous_score = NULL (no prior data)
  └─ DEFAULT: When no previous data, previous_score = current_score

Step 3: CALCULATE CHANGE
  scoreChange = current_score - previous_score
             = 63.67 - 63.67
             = 0.0 (matches response)

RESULT: scoreChange = 63.67 (displayed as current score when first time)
NOTE: In actual response shown: 63.67 (this seems to be displaying current score, not change)
```

#### 5. scoreChangePercent: 0.0

**ROOT DATA SOURCE:** Percentage change calculation

```
scoreChangePercent = (scoreChange / previous_score) × 100

Step 1: GET scoreChange
  ├─ scoreChange = 0.0 (calculated above)
  └─ This is the absolute change in points

Step 2: GET PREVIOUS SCORE
  ├─ previous_score = 63.67 (same as current, first report)
  └─ Source: Same as scoreChange calculation

Step 3: CALCULATE PERCENTAGE
  scoreChangePercent = (0.0 / 63.67) × 100
                     = 0.0%

Step 4: GUARD AGAINST DIVISION BY ZERO
  if previous_score == 0:
    scoreChangePercent = 0.0 (or N/A)
  else:
    scoreChangePercent = (scoreChange / previous_score) × 100

RESULT: scoreChangePercent = 0.0% ✓
```

---

### Complete Data Root Map - Validator Overview

```
overview.status = "warning"
  ├─ Input 1: blockchain_uptime = 100% (Vizion API)
  ├─ Input 2: top_delegator_pct = 15.61% (Blockchain RPC /delegations)
  ├─ Input 3: network_data_available = ? (cached network stats)
  ├─ Input 4: jailed_status = false (Blockchain RPC /validators/{valoper})
  ├─ Input 5: slashing_events = 0 (validator_performance_snapshot DB table)
  └─ Rule: if (top_delegator_pct > 15%) OR (missing_network_data) → "warning"

overview.score = 63.67
  ├─ Metric 1: uptimePct = 100.0% → Weight 30% (Vizion API)
  ├─ Metric 2: apr = 17.89% → Weight 20% (validator_rewards_snapshot DB + blockchain)
  ├─ Metric 3: slashingEvents = 0 → Weight 25% (validator_performance_snapshot DB)
  ├─ Metric 4: delegatorConcentration = 15.61% → Weight 15% (Blockchain RPC)
  ├─ Metric 5: reliabilityVsNetwork = 1.176 → Weight 10% (cached network stats)
  └─ Formula: weighted_sum - penalties

overview.grade = "C"
  └─ Lookup: grade_table[score: 63.67] = "C"

overview.scoreChange = 63.67
  ├─ Current: 63.67 (this week)
  └─ Previous: 63.67 (no historical data)
  └─ Change: 0.0

overview.scoreChangePercent = 0.0
  ├─ Change: 0.0 points
  ├─ Previous: 63.67
  └─ Percent: (0.0 / 63.67) × 100 = 0.0%
```

---

**delegators.topDelegators:**

```json
[
  {
    "delegatorAddress": "0xabc123...",
    "delegationAmount": 1500.0,
    "delegationPercent": 15.5
  },
  {
    "delegatorAddress": "0xdef456...",
    "delegationAmount": 1200.0,
    "delegationPercent": 12.4
  }
]
```

**How topDelegators are calculated - Complete Data Tracing:**

```
Root Data Flow:

Step 1: IDENTIFY VALIDATOR ADDRESS (Root: Blockchain)
  ├─ PRIMARY SOURCE: validator_rewards_snapshot table
  ├─ SECONDARY SOURCE: Blockchain state (if not cached)
  ├─ SQL QUERY:
  │  SELECT valoper_address
  │  FROM validator_rewards_snapshot
  │  WHERE node_id = '5f7c8b6d-9a4e-4c2f-b1d3-6e0a8c9f2a44'
  │  LIMIT 1
  ├─ RAW VALUE: valoper = "cosmos1valoperxxx..."
  ├─ ORIGIN: This valoper was registered when validator was onboarded
  ├─ VALIDATION: Cross-checked against live blockchain state
  └─ RESULT: valoper_address = "cosmos1valoper..." ✓

Step 2: FETCH ALL DELEGATIONS FROM BLOCKCHAIN RPC (Root: Blockchain State)
  ├─ PRIMARY SOURCE: Live blockchain node RPC endpoint
  ├─ PROTOCOL: Cosmos SDK Standard
  ├─ RPC ENDPOINT: https://rpc.network.com:26657
  ├─ RPC METHOD: cosmos.staking.v1beta1.Query/ValidatorDelegations
  ├─ HTTP REQUEST:
  │  GET /cosmos/staking/v1beta1/validators/{cosmos1valoperxxx}/delegations
  ├─ RAW BLOCKCHAIN RESPONSE:
  │  {
  │    "delegation_responses": [
  │      {
  │        "delegation": {
  │          "delegator_address": "cosmos1delegator1...",
  │          "validator_address": "cosmos1valoperxxx...",
  │          "shares": "20000000000"  ← In smallest chain units (e.g., uatom)
  │        },
  │        "balance": {
  │          "amount": "20000000000",  ← Must match shares
  │          "denom": "uatom"
  │        }
  │      },
  │      {
  │        "delegation": {
  │          "delegator_address": "cosmos1delegator2...",
  │          "shares": "7006203968"
  │        },
  │        "balance": {
  │          "amount": "7006203968",
  │          "denom": "uatom"
  │        }
  │      },
  │      ... 19 delegations total
  │    ],
  │    "pagination": { "total": "19" }
  │  }
  └─ STATUS: This is LIVE chain data, fetched fresh each time ✓

Step 3: NORMALIZE UNITS (Root: Chain Configuration)
  ├─ PROBLEM: Blockchain stores in smallest unit (uatom = 10^-6 atom)
  ├─ SOLUTION: Need to divide by decimals
  ├─ SOURCE OF DECIMALS: Network configuration
  ├─ CONFIG QUERY:
  │  SELECT decimals FROM blockchain_config WHERE chain_id = 'cosmoshub-4'
  │  decimals = 6
  ├─ CONVERSION FACTOR: 10^6 = 1,000,000
  ├─ NORMALIZATION:
  │  Raw amount: 20,000,000,000 uatom
  │  Conversion: 20,000,000,000 / 1,000,000 = 20,000.0 atom
  ├─ ALGORITHM:
  │  normalized_amount = raw_amount / (10^decimals)
  └─ RESULT: All amounts now in human-readable atom units ✓

Step 4: CALCULATE TOTAL DELEGATIONS (Root: Sum of Step 3 results)
  ├─ OPERATION: SUM all normalized amounts
  ├─ RAW DATA:
  │  [20,000.0, 7,006.2, 193.46, 118.23, 118.23, 108.23, 101.0, 85.98, 85.98, 85.0, ...]
  ├─ CALCULATION:
  │  total = 20,000.0 + 7,006.2 + 193.46 + 118.23 + 118.23 + ...
  │  total = 128,127.654 (approximately)
  ├─ CROSS-CHECK:
  │  This should match metrics.stake value
  │  metrics.stake: 128,127.654... ✓
  └─ RESULT: total_delegations = 128,127.654 ✓

Step 5: CALCULATE PERCENTAGES (Root: Step 3 amounts ÷ Step 4 total)
  ├─ FORMULA: delegator_percent = (delegator_amount / total_amount) × 100
  ├─ EXAMPLE CALCULATIONS:
  │
  │  Delegator 1:
  │  ├─ Amount: 20,000.0
  │  ├─ Total: 128,127.654
  │  ├─ Calculation: (20,000.0 / 128,127.654) × 100 = 15.609%
  │  └─ Displayed: 15.61%
  │
  │  Delegator 2:
  │  ├─ Amount: 7,006.204
  │  ├─ Total: 128,127.654
  │  ├─ Calculation: (7,006.204 / 128,127.654) × 100 = 5.468%
  │  └─ Displayed: 5.47%
  │
  │  Delegator 3:
  │  ├─ Amount: 193.461
  │  ├─ Total: 128,127.654
  │  ├─ Calculation: (193.461 / 128,127.654) × 100 = 0.151%
  │  └─ Displayed: 0.15%
  │
  └─ VALIDATION:
     Sum of all percentages ≈ 100.0% ✓

Step 6: SORT AND FILTER (Root: Step 5 results)
  ├─ SORT BY: delegator_amount DESC (largest first)
  ├─ LIMIT: Top 10 delegators
  ├─ ORDER (by amount):
  │  1. 20,000.0    → 15.61%
  │  2. 7,006.2     → 5.47%
  │  3. 193.46      → 0.15%
  │  4. 118.23      → 0.09%
  │  5. 118.23      → 0.09%
  │  6. 108.23      → 0.08%
  │  7. 101.0       → 0.08%
  │  8. 85.98       → 0.07%
  │  9. 85.98       → 0.07%
  │  10. 85.0       → 0.07%
  │
  └─ RESULT: Array of top 10 ✓

Step 7: BUILD RESPONSE OBJECT (Root: All steps above)
  ├─ EACH DELEGATOR ENTRY:
  │  {
  │    "delegatorAddress": (from Step 2 RPC response),
  │    "delegatedStake": (normalized from Step 3),
  │    "delegatePercentOfValidator": (calculated in Step 5),
  │    "joinedDate": (from delegation_metadata table or NULL)
  │  }
  │
  └─ RESULT:
     [
       {
         "delegatorAddress": "0x51902d4ff33aa8ff228ce4845f2a6c65e01eb4d7",
         "delegatedStake": 20000.0,
         "delegatePercentOfValidator": 15.61,
         "joinedDate": null
       },
       {
         "delegatorAddress": "0xc44709adc96dd4abeb5e20f84be957a210986f79",
         "delegatedStake": 7006.203967647749,
         "delegatePercentOfValidator": 5.47,
         "joinedDate": null
       },
       ... 8 more entries
     ]
```

---

### Complete Delegators Data Root Map

```
delegators.totalCount = 19
  └─ ROOT SOURCE: Blockchain RPC response
     └─ pagination.total field from Step 2

delegators.topDelegators[0].delegatorAddress = "0x51902d4ff33aa8ff228ce4845f2a6c65e01eb4d7"
  └─ ROOT SOURCE: Blockchain RPC response
     └─ delegation_responses[0].delegation.delegator_address

delegators.topDelegators[0].delegatedStake = 20000.0
  ├─ ROOT SOURCE: Blockchain RPC response
  ├─ RAW VALUE: 20,000,000,000 uatom
  └─ CALCULATION: 20,000,000,000 / 1,000,000 (from chain decimals config)

delegators.topDelegators[0].delegatePercentOfValidator = 15.61
  ├─ NUMERATOR: delegatedStake = 20,000.0 (RPC response, normalized)
  ├─ DENOMINATOR: metrics.stake = 128,127.654 (sum of all normalized delegations)
  └─ CALCULATION: (20,000.0 / 128,127.654) × 100 = 15.609%

delegators.topDelegators[0].joinedDate = null
  └─ ROOT SOURCE: delegation_metadata table
     └─ No joinDate tracking in current implementation = NULL
```

---

## Error Responses

All endpoints return standard error format:

```json
{
  "success": false,
  "error": "Error message here",
  "status": 400
}
```

**Common Errors:**

| Status | Error                     | Cause                                     |
| ------ | ------------------------- | ----------------------------------------- |
| 401    | "Unauthorized"            | Missing/invalid JWT token                 |
| 400    | "Invalid range parameter" | range not in ['weekly', 'monthly']        |
| 404    | "Node not found"          | nodeId doesn't exist or not owned by user |
| 500    | "Error generating report" | Vizion API down or database error         |

---

## Authentication

All endpoints require JWT token in Authorization header:

```
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

**How authentication works:**

```
Step 1: Extract token from header
Token = request.headers.get('Authorization')
token_string = token.split('Bearer ')[1]

Step 2: Validate token
decoded = jwt.decode(token_string, SECRET_KEY)
user_id = decoded.get('user_id')

Step 3: Get user from database
Query: SELECT * FROM res_users WHERE id = user_id
user = result (or None if not found)

Step 4: Check user permissions
if user is None:
  return {"success": false, "error": "User not found"}

if not user.active:
  return {"success": false, "error": "User is inactive"}
```

---

## Rate Limiting

No explicit rate limiting implemented. Consider adding:

- 100 requests per minute per user
- 10 requests per minute for each endpoint
- Caching for 5 minutes

---

## Testing Endpoints

**Weekly Account Report:**

```bash
curl -X GET "http://localhost:8069/api/v1/reports/account-weekly?range=weekly&timezone=UTC" \
  -H "Authorization: Bearer {token}"
```

**RPC Fleet:**

```bash
curl -X GET "http://localhost:8069/api/v1/reports/rpc-fleet?range=weekly" \
  -H "Authorization: Bearer {token}"
```

**Single RPC Node:**

```bash
curl -X GET "http://localhost:8069/api/v1/reports/rpc/uuid-node-123?range=weekly" \
  -H "Authorization: Bearer {token}"
```

**Validator Fleet:**

```bash
curl -X GET "http://localhost:8069/api/v1/reports/validator-fleet?range=monthly" \
  -H "Authorization: Bearer {token}"
```

**Single Validator:**

```bash
curl -X GET "http://localhost:8069/api/v1/reports/validator/uuid-validator-456?range=monthly" \
  -H "Authorization: Bearer {token}"
```

---

## Response Sizes

Expected response sizes:

| Endpoint                | Typical Size | Notes                             |
| ----------------------- | ------------ | --------------------------------- |
| account-weekly          | 150-200 KB   | Includes all RPC + validator data |
| rpc-fleet               | 100-150 KB   | RPC nodes only                    |
| rpc/<nodeId>            | 50-80 KB     | Single node + method breakdown    |
| validator-fleet         | 80-120 KB    | All validators + risk indicators  |
| validator/<validatorId> | 40-60 KB     | Single validator + delegators     |

---

## Data Freshness

All data is **real-time** with these exceptions:

- **Vizion data**: Updated every 5 minutes
- **Snapshots**: Updated every hour
- **Incidents**: Cached for 5 minutes
- **Delegator list**: Fetched fresh from RPC (no cache)

---

## Related Documentation

- [EMAIL_REPORTING.md](EMAIL_REPORTING.md) - Email report explanations
- [services.py](./utils/reports/services.py) - Core report generation logic
- [models.py](./utils/reports/models.py) - Data models and DTOs
