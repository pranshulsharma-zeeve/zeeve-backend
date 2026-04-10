# API Field Documentation - Exact Formulas from Code

## `/api/v1/reports/account-weekly` Response Fields

All calculations reference exact code implementations. No assumptions.

---

## Meta Fields

### `accountId`

- **Source**: Account identifier passed to `get_account_report()`
- **Formula**: `str(account_id)`
- **Code**: [services.py](services.py#L521)

### `accountName`

- **Source**: Customer name from first node's subscription
- **Formula**: `subscription.customer_name.name` (first node)
- **Fallback**: `"Unknown Account"`
- **Code**: [services.py](services.py#L512-L519)

### `periodStart`

- **Source**: Report period start date (timezone-aware)
- **Formula**: `helpers.format_date_for_response(period_start)`
- **Code**: [services.py](services.py#L525)

### `periodEnd`

- **Source**: Report period end date (timezone-aware)
- **Formula**: `helpers.format_date_for_response(period_end)`
- **Code**: [services.py](services.py#L526)

### `range`

- **Source**: Period type parameter
- **Values**: `"weekly"` or `"monthly"`
- **Code**: [services.py](services.py#L527)

### `timezone`

- **Source**: User timezone string
- **Formula**: User-provided timezone string
- **Code**: [services.py](services.py#L528)

---

## Overview Section

### `totalNodes`

- **Source**: All RPC + all validator nodes
- **Formula**: `len(all_nodes)`
- **Code**: [services.py](services.py#L1189)

### `overallUptimePct`

- **Source**: Blended uptime across RPC and validator nodes
- **Formula**:
  ```
  IF total_tracked_nodes > 0:
    (rpc_uptime_sum + validator_uptime_sum) / total_tracked_nodes
  ELSE:
    0.0
  ```
- **Where**:
  - `rpc_uptime_sum` = sum of individual RPC node uptime percentages
  - `validator_uptime_sum` = sum of individual validator node uptime percentages
  - `total_tracked_nodes` = count(RPC nodes) + count(validator nodes)
- **Rounding**: `round(result, 2)`
- **Code**: [services.py](services.py#L1182-L1186)

### `totalRequests`

- **Source**: Sum of all RPC method calls in period
- **Data Source**: Vizion API daily RPC trends
- **Formula**: Sum of all method counts across all RPC nodes
- **Rounding**: `round(result, 0)`
- **Code**: [services.py](services.py#L1189)

### `totalRewards`

- **Source**: Validator rewards (cumulative snapshots)
- **Formula**: Sum across all validators of: `last_reward_snapshot - first_reward_snapshot`
- **Per-Validator Calculation**:
  - If 1 snapshot: `total_rewards = snapshot.total_rewards`
  - If 2+ snapshots: `total_rewards = max(0, last_snapshot - first_snapshot)`
- **Rounding**: `round(result, 2)`
- **Code**: [services.py](services.py#L930-L946)

### `rewardsDelta`

- **Source**: Change in total validator rewards from previous period
- **Formula**: `calculate_change(current_total_rewards, previous_total_rewards)`
  - Where: `current - previous` (absolute difference)
- **Implementation**: [helpers.py](helpers.py#L260-L273)
  ```python
  round(float(current) - float(previous), 2)
  ```
- **Code**: [services.py](services.py#L1211)

### `overallScore`

- **Source**: Fleet health score (0-100)
- **Formula**:
  ```
  IF validator_count > 0:
    (rpc_score + validator_score) / 2
  ELSE:
    rpc_score
  ```
- **Where**:
  - `rpc_score` = calculated from RPC uptime, latency, error rate
  - `validator_score` = calculated from fleet validator health
- **Code**: [services.py](services.py#L1193)

### `overallGrade`

- **Source**: Letter grade based on overall score
- **Formula**: `scoring.determine_grade(overall_score)`
- **Fallback**: Grade based on score buckets (A, B, C, D, F)
- **Code**: [services.py](services.py#L1194)

### `overallStatus`

- **Source**: Status string based on overall score
- **Formula**: `scoring.determine_status(overall_score)`
- **Fallback**: Status text (e.g., "Healthy", "At Risk", "Critical")
- **Code**: [services.py](services.py#L1195)

### `scoreChange`

- **Source**: Absolute change from previous period score
- **Formula**: `calculate_change(current_overall_score, previous_overall_score)`
  ```python
  round(float(current) - float(previous), 2)
  ```
- **Code**: [services.py](services.py#L1196)

### `scoreChangePercent`

- **Source**: Percentage change from previous period score
- **Formula**:
  ```
  IF previous_score == 0:
    0.0
  ELSE:
    ((current_score - previous_score) / previous_score) * 100
  ```
- **Rounding**: `round(result, 2)`
- **Implementation**: [helpers.py](helpers.py#L276-L300)
- **Code**: [services.py](services.py#L1197-L1202)

### `prevOverallUptimePct`

- **Source**: Overall uptime from previous period (for comparison)
- **Formula**: Same as `overallUptimePct` but calculated for previous period
  ```
  IF prev_total_tracked_nodes > 0:
    (prev_rpc_uptime_sum + prev_validator_uptime_sum) / prev_total_tracked_nodes
  ELSE:
    0.0
  ```
- **Rounding**: `round(result, 2)`
- **Code**: [services.py](services.py#L1189-L1191)

---

## RPC Summary Section

### `totalNodes`

- **Source**: Count of RPC nodes in account
- **Formula**: `len(rpc_nodes)`
- **Code**: [services.py](services.py#L633)

### `healthyNodes`

- **Source**: RPC nodes with uptime >= 95%
- **Formula**: Count of nodes where `uptime_pct >= 95`
- **Code**: [services.py](services.py#L700-L705)

### `criticalNodes`

- **Source**: RPC nodes with uptime < 60%
- **Formula**: Count of nodes where `uptime_pct < 60`
- **Code**: [services.py](services.py#L706-L711)

### `avgUptimePct`

- **Source**: Average uptime across all RPC nodes
- **Formula**:
  ```
  IF rpc_node_count > 0:
    rpc_uptime_sum / rpc_node_count
  ELSE:
    0.0
  ```
- **Rounding**: `round(result, 2)`
- **Code**: [services.py](services.py#L745)

### `avgLatencyMs`

- **Source**: Average latency across all RPC nodes (from Vizion)
- **Formula**:
  ```
  IF rpc_node_count > 0:
    rpc_latency_sum / rpc_node_count
  ELSE:
    0.0
  ```
- **Rounding**: `round(result, 2)`
- **Code**: [services.py](services.py#L746)

### `errorRatePct`

- **Source**: Error rate across all RPC requests
- **Formula**:

  ```
  IF total_requests > 0:
    (total_errors / total_requests) * 100
  ELSE:
    0.0
  ```

  - Where: `total_errors` = sum of error counts per node

- **Rounding**: `round(result, 2)`
- **Code**: [services.py](services.py#L748)

### `totalRequests`

- **Source**: Total RPC method calls in period
- **Data Source**: Vizion API daily trends
- **Formula**: Sum of all method counts across all RPC nodes and all days
- **Rounding**: `round(result, 0)`
- **Code**: [services.py](services.py#L749)

### `score`

- **Source**: RPC node health score
- **Formula**: `calculate_rpc_node_score(avg_uptime_pct, avg_latency_ms, error_rate_pct)`
- **Fallback**: Score based on component weights (uptime dominates)
- **Code**: [services.py](services.py#L753)

### `scoreChange`

- **Source**: Absolute change from previous period RPC score
- **Formula**: `calculate_change(current_rpc_score, previous_rpc_score)`
- **Code**: [services.py](services.py#L754)

### `prevTotalRequests`

- **Source**: Total RPC requests from previous period (for comparison)
- **Formula**: Same as `totalRequests` but for previous period
- **Fallback**: `None` if previous period had 0 requests
- **Code**: [services.py](services.py#L755-L757)

---

## Validator Summary Section

### `totalValidators`

- **Source**: Count of validator nodes in account
- **Formula**: `len(validator_nodes)`
- **Code**: [services.py](services.py#L1088)

### `healthyNodes`

- **Source**: Validators with uptime >= 95%
- **Formula**: Count of nodes where `uptime_pct >= 95`
- **Code**: [services.py](services.py#L1030-L1035)

### `criticalNodes`

- **Source**: Validators with uptime < 60%
- **Formula**: Count of nodes where `uptime_pct < 60`
- **Code**: [services.py](services.py#L1036-L1041)

### `totalStake`

- **Source**: Total delegated stake across all validators
- **Formula**: Sum of all validators: `average(validator_stake_snapshots)`
- **Aggregation**: Average of stake snapshots per validator
- **Rounding**: `round(result, 2)`
- **Code**: [services.py](services.py#L930-L946)

### `totalRewards`

- **Source**: Total earned rewards across all validators
- **Formula**: Sum across all validators of: `last_reward - first_reward` (delta)
- **Per-Validator**:
  - If 1 snapshot: `total_rewards = snapshot.total_rewards`
  - If 2+ snapshots: `total_rewards = max(0, last - first)`
- **Rounding**: `round(result, 2)`
- **Code**: [services.py](services.py#L930-L946)

### `avgAPR`

- **Source**: Annualized percentage rate (APR)
- **Formula**:

  ```
  IF validator_count > 0:
    (total_rewards / (total_stake / validator_count)) * (365 / period_days) * 100
  ELSE:
    0.0
  ```

  - Where: `period_days` = 7 (weekly) or 30 (monthly)

- **Rounding**: `round(result, 2)`
- **Implementation**: [services.py](services.py#L2993-3013)
  ```python
  apr = (rewards / stake) * (365 / period_days) * 100
  ```
- **Code**: [services.py](services.py#L1091-1092)

### `avgUptimePct`

- **Source**: Average uptime across all validators
- **Formula**:
  ```
  IF validator_count > 0:
    validator_uptime_sum / validator_count
  ELSE:
    0.0
  ```
- **Rounding**: `round(result, 2)`
- **Code**: [services.py](services.py#L1093)

### `jailedCount`

- **Source**: Number of jailed validators
- **Formula**: Count of validators where `jailed == True`
- **Code**: [services.py](services.py#L1097)

### `score`

- **Source**: Validator fleet health score
- **Formula**: `calculate_fleet_score(list_of_individual_validator_scores)`
- **Fallback**: 0.0 if no validators
- **Code**: [services.py](services.py#L1099)

### `scoreChange`

- **Source**: Absolute change from previous period validator score
- **Formula**: `calculate_change(current_validator_score, previous_validator_score)`
- **Code**: [services.py](services.py#L1100)

### `prevTotalRewards`

- **Source**: Total validator rewards from previous period
- **Formula**: Same as `totalRewards` but for previous period
- **Fallback**: `None` if previous period had 0 rewards
- **Code**: [services.py](services.py#L1101-L1102)

### `prevTotalStake`

- **Source**: Total validator stake from previous period
- **Formula**: Same as `totalStake` but for previous period
- **Fallback**: `None` if previous period had 0 stake
- **Code**: [services.py](services.py#L1103-L1104)

---

## RPC Highlights (Per-Node Details)

Each RPC highlight object represents individual node metrics:

### `nodeId`

- **Source**: Unique node identifier
- **Formula**: `node.id`

### `nodeName`

- **Source**: Human-readable node name
- **Formula**: `node.node_name`

### `status`

- **Source**: Health classification
- **Values**: `"healthy"` (uptime >= 95%) or `"critical"` (uptime < 60%) or `"degraded"` (between)
- **Code**: [services.py](services.py#L700-L718)

### `uptimePct`

- **Source**: Individual node uptime percentage
- **Data Source**: Vizion API
- **Fallback**: `0.0` if no Vizion data

### `methodCalls`

- **Source**: Total RPC method calls for this node
- **Formula**: Sum of daily method counts from Vizion API
- **Fallback**: `0` if no data

### `latencyMs`

- **Source**: Average response latency
- **Data Source**: Vizion API protocol metrics
- **Fallback**: `0.0` if no data

### `score`

- **Source**: Individual RPC node score
- **Formula**: `calculate_rpc_node_score(uptime_pct, latency_ms, error_rate_pct)`

### `scoreChange`

- **Source**: Change from previous period
- **Formula**: `calculate_change(current_score, previous_score)`

---

## Validator Highlights (Per-Validator Details)

Each validator highlight object represents individual validator metrics:

### `validatorId`

- **Source**: Unique validator identifier
- **Formula**: `node.id`

### `validatorName`

- **Source**: Human-readable validator name
- **Formula**: `node.node_name`

### `status`

- **Source**: Health classification
- **Values**: `"healthy"`, `"critical"`, `"jailed"`, `"degraded"`
- **Logic**:
  - If `jailed == True`: `"jailed"`
  - Elif `uptime >= 95`: `"healthy"`
  - Elif `uptime < 60`: `"critical"`
  - Else: `"degraded"`

### `stake`

- **Source**: Individual validator stake
- **Formula**: `average(stake_snapshots)` for this validator
- **Fallback**: `0.0`

### `rewards`

- **Source**: Individual validator rewards earned
- **Formula**:
  - If 1 snapshot: `snapshot.total_rewards`
  - If 2+ snapshots: `max(0, last - first)`
- **Fallback**: `0.0`

### `apr`

- **Source**: Individual validator APR
- **Formula**:
  ```
  IF stake > 0:
    (rewards / stake) * (365 / period_days) * 100
  ELSE:
    0.0
  ```
- **Where**: `period_days` = 7 (weekly) or 30 (monthly)

### `uptimePct`

- **Source**: Individual validator uptime
- **Data Source**: Vizion API
- **Fallback**: `0.0`

### `score`

- **Source**: Individual validator score
- **Formula**: `calculate_validator_score(uptime_pct, apr, slashing_events, jailed)`

### `scoreChange`

- **Source**: Change from previous period
- **Formula**: `calculate_change(current_score, previous_score)`

---

## Incidents Array

### `incidentId`

- **Source**: Unique incident identifier
- **Data Source**: Vizion API incident logs

### `timestamp`

- **Source**: When incident occurred
- **Format**: ISO 8601 datetime

### `severity`

- **Source**: Incident severity level
- **Values**: `"critical"`, `"warning"`, `"info"`

### `description`

- **Source**: Incident details
- **Data Source**: Vizion API

---

## Insights Array

### `title`

- **Source**: Insight heading
- **Examples**: `"RPC Uptime Below Target"`, `"Low Average APR"`, `"Jailed Validators Detected"`
- **Code**: [services.py](services.py#L3214-3250)

### `description`

- **Source**: Detailed explanation
- **Examples**: `"Your RPC nodes have dropped below the 95% uptime target"`, etc.

### `type`

- **Source**: Insight category
- **Values**: `"warning"`, `"info"`, `"opportunity"`

### `impact`

- **Source**: Business impact assessment
- **Examples**: `"High"`, `"Medium"`, `"Low"`

### Insight Generation Logic

**RPC Uptime Below Target**:

- **Trigger**: `current_report.rpcSummary.avgUptimePct < 95` AND `previous_report.rpcSummary.avgUptimePct >= 95`
- **Code**: [services.py](services.py#L3225-3227)

**Validator Jailing**:

- **Trigger**: `current_report.validatorSummary.jailedCount > 0`
- **Code**: [services.py](services.py#L3232-3237)

**Low APR**:

- **Trigger**: `current_report.validatorSummary.avgAPR < 10`
- **Code**: [services.py](services.py#L3239-3244)

---

## Trends Array

### `date`

- **Source**: Date of trend data point
- **Format**: ISO 8601 date string (YYYY-MM-DD)

### `requestCount`

- **Source**: Total RPC method calls for this day
- **Data Source**: Vizion API daily trends
- **Formula**: Sum of all method counts for all RPC nodes on this date
- **Fallback**: `0`

### `rewards`

- **Source**: Cumulative validator rewards for this day
- **Data Source**: Validator snapshots
- **Formula**: Sum of `total_rewards` snapshot value for all validators on this date
- **Fallback**: `0.0`
- **Note**: Values are cumulative snapshots, not daily earned amounts
- **Code**: [services.py](services.py#L310-407)

---

## Email Context Fields (Bonus)

These fields are NOT in API response but used in email templates:

### `overall_growth` (Email Only)

- **Source**: Email template context
- **Formula**: Average of available metric growth rates
  ```python
  def calculate_overall_growth(metric_changes: List[float]) -> float:
      valid_changes = [change for change in metric_changes if change is not None]
      if not valid_changes:
          return 0.0
      return round(sum(valid_changes) / len(valid_changes), 2)
  ```
- **Metrics Included**:
  - RPC-only: `avg(request_growth%, uptime_growth%)`
  - Validator-only: `avg(rewards_growth%, uptime_growth%)`
  - Mixed: `avg(request_growth%, rewards_growth%, uptime_growth%)`
- **Code**: [mail_utils.py](mail_utils.py#L109-115)

### `period_string` (Email Only)

- **Source**: Formatted period display
- **For Weekly**: `"Feb 17, 2026 - Feb 23, 2026"`
- **For Monthly**: `"Feb 01, 2026 - Feb 28, 2026"`
- **Code**: [mail_utils.py](mail_utils.py#L68-85)

---

## Summary of Key Calculation Patterns

### Pattern 1: Node Aggregation

All per-node fields are aggregated using sum or average:

```
SUM for counts: healthyNodes, criticalNodes, jailedCount, totalRequests
AVG for rates: avgUptimePct, avgLatencyMs, avgAPR, errorRatePct
BLENDED for overall: (rpc_sum + validator_sum) / total_count
```

### Pattern 2: Snapshot Deltas

Validator rewards use cumulative snapshots:

```
IF snapshots_count == 1:
    rewards = snapshot.total_rewards
ELSE:
    rewards = max(0, last_snapshot.total_rewards - first_snapshot.total_rewards)
```

### Pattern 3: Previous Period

All change calculations reference previous period data:

```
scoreChangePercent = ((current - previous) / previous) * 100
scoreChange = current - previous
```

### Pattern 4: Fallbacks

Missing data defaults to:

```
Percentages/Averages: 0.0
Counts: 0
Optional fields: None
```

---

## Data Quality Notes

1. **Uptime Data**: From Vizion API, available for all node types
2. **Request Counts**: From Vizion API for RPC nodes only
3. **Validator Rewards/Stake**: From database snapshots (daily)
4. **Latency/Errors**: From Vizion API protocol metrics
5. **All values**: Timezone-aware using user's timezone
6. **Rounding**: Consistent to 2 decimal places (except counts, which are 0 decimals)

---

## `/api/v1/reports/rpc-fleet` Response Fields

Same format as account-weekly. Only exact code behavior.  
Note: As requested, score/grade formulas are not explained here.

---

## Meta Fields

### `accountId`

- **Source**: Account identifier from authenticated user
- **Formula**: `str(account_id)`

### `accountName`

- **Source**: First RPC node subscription customer name
- **Formula**: `rpc_nodes[0].subscription_id.customer_name.name`
- **Fallback**: `"Unknown Account"`

### `periodStart`

- **Source**: Report period start
- **Formula**: `helpers.format_date_for_response(period_start)`

### `periodEnd`

- **Source**: Report period end
- **Formula**: `helpers.format_date_for_response(period_end)`

### `range`

- **Source**: Query parameter
- **Values**: `"weekly"` or `"monthly"`

### `timezone`

- **Source**: Query parameter
- **Fallback**: `"UTC"`

### `nodeId`, `nodeName`, `validatorId`, `validatorName`

- **Source**: Meta model supports multiple endpoints
- **Value in rpc-fleet**: `null`

---

## Summary Fields

### `totalNodes`

- **Source**: RPC node list for account
- **Formula**: `len(rpc_nodes)`

### `healthyNodes`, `warningNodes`, `criticalNodes`

- **Source**: Uptime bucket per node (`_classify_uptime_health`)
- **Formula**:
  - healthy if `uptimePct >= 95.0`
  - critical if `uptimePct <= 60.0`
  - otherwise warning

### `avgUptimePct`

- **Source**: Uptime of all RPC nodes
- **Formula**:
  ```
  IF node_count > 0:
    uptime_sum / node_count
  ELSE:
    0.0
  ```
- **Rounding**: `round(result, 2)`

### `avgLatencyMs`

- **Source**: Latency of all RPC nodes
- **Formula**:
  ```
  IF node_count > 0:
    latency_sum / node_count
  ELSE:
    0.0
  ```
- **Rounding**: `round(result, 2)`

### `totalRequests`

- **Source**: Sum of all node request counts
- **Formula**: `sum(node.requestCount)`
- **Rounding**: `round(result, 0)`

### `totalErrors`

- **Source**: Sum of all node error counts
- **Formula**: `sum(node.errorCount)`
- **Rounding**: `round(result, 0)`

### `errorRatePct`

- **Source**: Fleet-level requests and errors
- **Formula**:
  ```
  IF total_requests > 0:
    (total_errors / total_requests) * 100
  ELSE:
    0.0
  ```
- **Rounding**: `round(result, 2)`

### `requestsDeltaPct`

- **Source**: Current period total requests vs previous period total requests
- **Formula**: `helpers.calculate_change_percent(total_requests, prev_total_requests_fleet)`
- **Helper formula used**:
  ```
  IF previous == 0:
    0.0
  ELSE:
    round(((current - previous) / previous) * 100, 2)
  ```

### `status`

- **Source**: Status function from summary score
- **Formula used in code**: `scoring.determine_status(fleet_score)`

### `score`, `grade`, `scoreChange`, `scoreChangePercent`

- **Note**: Included in API response model.
- **As requested**: formula details intentionally skipped.

---

## Nodes[] Fields

### `nodeId`

- **Source**: Node identifier
- **Formula**: `node.node_identifier or str(node.id)`

### `nodeName`

- **Source**: Node display name
- **Formula**: `node.node_name or "Unknown Node"`

### `uptimePct`

- **Source**: Uptime history API by host id
- **Formula**: `uptime_data.get('uptime_pct', 0.0)`
- **Fallback**: `0.0`

### `latencyMs`

- **Source**: Parsed protocol metrics
- **Formula**: `protocol_metrics['latencyMs']`
- **Fallback**: `0.0`

### `requestCount`

- **Source**: Daily request trend aggregation
- **Priority used in code**:
  1. `per_host_rpc_requests[host_id]`
  2. `rpc_method_count_data[node.node_name]`
  3. `0.0`

### `errorCount`

- **Source**: Parsed protocol metrics
- **Formula**: `protocol_metrics['errorCount']`
- **Fallback**: `0.0`

### `errorRatePct`

- **Source**: Node errors and node request count
- **Formula**:
  ```
  IF requestCount > 0:
    (errorCount / requestCount) * 100
  ELSE:
    0.0
  ```

### `status`

- **Source**: Node status function
- **Formula used in code**: `scoring.determine_status(node_score)`

### `score`, `scoreChange`

- **Note**: Present in response model.
- **As requested**: formula details intentionally skipped.

---

## healthMix

### `good`

- **Formula**: `healthyNodes`

### `warning`

- **Formula**: `warningNodes`

### `critical`

- **Formula**: `criticalNodes`

---

## incidents

- **Source**: `_fetch_incidents_from_vizion(...)`
- **Behavior**: if no matching events in period, returns `[]`

---

## insights

- **Source**: `_generate_rpc_fleet_insights(summary, node_items)`
- **Rules**:
  - add `"Critical Nodes Detected"` when `summary.criticalNodes > 0`
  - add `"High Average Latency"` when `summary.avgLatencyMs > 500`

---

## trends

- **Source**: Daily RPC trends from Vizion, filtered to report period
- **Behavior**:
  - each trend item is built from day-level `requestCount`
  - day is added only when `requestCount > 0`
  - if all days are zero, `trends = []`

---

## Consistency Check (Code Review Notes)

These are exact consistency observations from current code:

1. `summary.status` is score-based, but `healthyNodes/warningNodes/criticalNodes` are uptime-bucket based.  
   So status and health counts can disagree in edge cases.

2. `summary.scoreChangePercent` is currently hardcoded `0.0` for rpc-fleet.

3. Previous period fleet "error rate" input for score uses `prev_total_errors / node_count` (not error percentage by requests).

4. Previous period node error rate uses previous errors divided by **current** request count.

If you want, I can do a separate fix pass to make these four areas consistent across current/previous period calculations.

---

## `/api/v1/reports/rpc/<nodeId>` Response Fields

Same style as above. Only exact code behavior.  
As requested, score/grade formula details are not explained.

---

## Meta Fields

### `accountId`

- **Source**: Node subscription customer
- **Formula**: `str(subscription.customer_name.id)`
- **Fallback**: `"Unknown"`

### `accountName`

- **Source**: Node subscription customer name
- **Formula**: `subscription.customer_name.name`
- **Fallback**: `"Unknown"`

### `periodStart`

- **Source**: Period start from range + timezone
- **Formula**: `helpers.format_date_for_response(period_start)`

### `periodEnd`

- **Source**: Period end from range + timezone
- **Formula**: `helpers.format_date_for_response(period_end)`

### `range`

- **Source**: Query param
- **Values**: `"weekly"` or `"monthly"`

### `timezone`

- **Source**: Query param
- **Fallback**: `"UTC"`

### `nodeId`

- **Source**: Requested node
- **Formula**: `node.node_identifier or str(node.id)`

### `nodeName`

- **Source**: Requested node
- **Formula**: `node.node_name or "Unknown Node"`

### `validatorId`, `validatorName`

- **Value in RPC node endpoint**: `null`

---

## Overview Fields

### `status`

- **Source**: status function from node score
- **Formula used in code**: `scoring.determine_status(node_score)`

### `score`, `grade`, `scoreChange`, `scoreChangePercent`

- **Note**: Present in API response model.
- **As requested**: formula details intentionally skipped.
- **Current behavior**: `scoreChangePercent` is set to `0.0` in this endpoint.

---

## Metrics Fields

### `uptimePct`

- **Source**: Uptime history API
- **Formula**: `uptime_data.get('uptime_pct', 0.0)`
- **Fallback**: `0.0`

### `uptimeChangePercent`

- **Source**: Current vs previous period uptime
- **Formula**: `helpers.calculate_change_percent(current_uptime, prev_uptime)`
- **Helper formula used**:
  ```
  IF previous == 0:
    0.0
  ELSE:
    round(((current - previous) / previous) * 100, 2)
  ```

### `latencyMs`

- **Source**: Protocol data API (`latencyMs`)
- **Formula**: `protocol_metrics['latencyMs']`
- **Fallback**: `0.0`

### `latencyChangePercent`

- **Current behavior**: hardcoded `0.0`

### `requestCount`

- **Source**: Method trend API for this host
- **Formula**: `sum(method_data['latest_counts'].values())`
- **Fallback**: `0.0`

### `requestChangePercent`

- **Current behavior**: hardcoded `0.0`

### `errorCount`

- **Source**: Protocol data API (`errorCount`)
- **Formula**: `protocol_metrics['errorCount']`
- **Fallback**: `0.0`

### `errorRatePct`

- **Source**: Current node errors and current node requests
- **Formula**:
  ```
  IF requestCount > 0:
    (errorCount / requestCount) * 100
  ELSE:
    0.0
  ```

### `errorChangePercent`

- **Current behavior**: hardcoded `0.0`

---

## security

### `ddosProtection`

- **Current behavior**: set to `True`

### `firewallEnabled`

- **Current behavior**: set to `True`

### `lastSecurityCheck`

- **Source**: Node `create_date`
- **Formula**:
  - Get interval dates from `helpers.get_last_interval_date(create_date)`
  - Use weekly value: `interval_dates['weekly'].isoformat()`
- **Fallback**: `None` on exception

---

## methodBreakdown[]

For each method item:

### `method`

- **Source**: key in `latest_counts`

### `callCount`

- **Source**: value in `latest_counts[method]`

### `callPercent`

- **Formula**:
  ```
  IF total_calls > 0:
    (callCount / total_calls) * 100
  ELSE:
    0.0
  ```
- **Rounding**: `round(result, 2)`

### `avgLatencyMs`, `errorCount`, `errorRatePct`

- **Current behavior**: hardcoded `0.0` (TODO in code)

### Empty array condition

`methodBreakdown = []` when any of these happen:

- no Vizion token
- no host id
- no method data returned
- exception while fetching/parsing

---

## benchmarks

### `uptimeVsNetwork`, `latencyVsNetwork`, `reliabilityVsNetwork`

- **Current behavior**: all hardcoded `0.0` (TODO in code)

---

## incidents

- **Source**: `_fetch_incidents_from_vizion([node], ...)`
- **Behavior**: returns `[]` when no incidents in period

---

## insights

- **Source**: `_generate_rpc_node_insights(node_data, security)`
- **Rule**:
  - adds `"High Error Rate"` only when `errorRatePct > 5`
- otherwise `[]`

---

## trends

- **Source**: Daily uptime aggregation from uptime history `data_points`
- **Build logic**:
  - aggregate raw points per day into uptime%
  - add trend row when day exists in aggregated data (or uptime > 0)
- **Result**:
  - if no daily uptime points in period, `trends = []`

---

## Why your sample values are all zero (exact code behavior)

With no host-level usage/uptime data returned for this node during period:

- `uptimePct = 0.0`
- `latencyMs = 0.0`
- `requestCount = 0.0`
- `errorCount = 0.0`
- `errorRatePct = 0.0`
- `methodBreakdown = []`
- `incidents = []`
- `insights = []` (because error rate is not > 5)
- `trends = []` (no daily uptime points)

This exactly matches your response payload.

---

## Consistency Check (Code Review Notes)

1. `overview.scoreChangePercent` is hardcoded `0.0`.
2. `metrics.latencyChangePercent`, `requestChangePercent`, `errorChangePercent` are hardcoded `0.0`.
3. Previous score comparison uses previous errors divided by **current** request count.
4. `security` values are fixed defaults (`True/True`) + derived date; not fetched from live security API in this endpoint.
5. `benchmarks` are placeholders (`0.0` for all three fields).

If you want, I can now do a code fix pass for this endpoint to make these fields fully consistent and computed.

---

## `/api/v1/reports/validator-fleet` Response Fields

Same style as above. Only exact code behavior.  
As requested, score/grade/APR formula details are not explained.

---

## Meta Fields

### `accountId`

- **Source**: Authenticated user account passed to report service
- **Formula**: `str(account_id)`

### `accountName`

- **Source**: First validator node subscription customer name
- **Formula**: `validator_nodes[0].subscription_id.customer_name.name`
- **Fallback**: `"Unknown Account"`

### `periodStart`

- **Source**: Report period start
- **Formula**: `helpers.format_date_for_response(period_start)`

### `periodEnd`

- **Source**: Report period end
- **Formula**: `helpers.format_date_for_response(period_end)`

### `range`

- **Source**: Query param
- **Values**: `"weekly"` or `"monthly"`

### `timezone`

- **Source**: Query param
- **Fallback**: `"UTC"`

### `nodeId`, `nodeName`, `validatorId`, `validatorName`

- **Value in validator-fleet**: `null`

---

## Summary Fields

### `totalValidators`

- **What this means**: Total number of validator nodes under this account.
- **How we calculate it**: We fetch all validator nodes for the account and count them.
- **Formula used in code**: `len(validator_nodes)`

### `activeValidators`

- **What this means**: Validators that are currently active (not jailed).
- **How we calculate it**: For each validator, we read jailed status. If jailed is false, we count it as active.
- **Formula used in code**: count of validators where `jailed == False`

### `jailedValidators`

- **What this means**: Validators that are currently jailed.
- **How we calculate it**: For each validator, if jailed is true, it is counted here.
- **Formula used in code**: count of validators where `jailed == True`

### `healthyNodes`, `warningNodes`, `criticalNodes`

- **What this means**: Health split of validators based on uptime.
- **How we calculate it**: Every validator is put into one bucket using its uptime percentage.
- **Rules used in code**:
  - healthy if `uptimePct >= 95.0`
  - critical if `uptimePct <= 60.0`
  - otherwise warning

### `totalStake`

- **What this means**: Total stake managed by all validators in the selected period.
- **How we calculate it**:
  1. For each validator, collect stake snapshots in the period.
  2. Compute that validator’s average stake for the period.
  3. Add all validator averages.
- **Formula used in code**: sum of each validator `avg_stake`
- **Rounding**: `round(result, 2)`

### `totalRewards`

- **What this means**: Total rewards earned by all validators during the selected period.
- **How we calculate it per validator**:
  - If only one reward snapshot exists in period, use that snapshot value.
  - If multiple snapshots exist, use delta: `last.total_rewards - first.total_rewards`.
  - If delta is negative, clamp to 0.
- **Fleet formula**: sum of each validator’s period reward value
- **Rounding**: `round(result, 2)`

### `avgAPR`

- **What this means**: Average annualized return metric for the validator fleet.
- **Note**: Included in API response.
- **As requested**: APR formula details intentionally skipped.

### `status`

- **What this means**: Overall health label for the fleet (`good` / `warning` / `critical`).
- **How we calculate it**: Derived from fleet score by status mapping function.
- **Formula used in code**: `scoring.determine_status(fleet_score)`

### `score`, `grade`, `scoreChange`, `scoreChangePercent`

- **What this means**:
  - `score`: overall fleet score
  - `grade`: score bucket label
  - `scoreChange`: current score minus previous period score
  - `scoreChangePercent`: percentage score change field
- **Note**: Included in API response model.
- **As requested**: formula details intentionally skipped.
- **Current behavior**: `scoreChangePercent` is set to `0.0` in this endpoint.

---

## validators[] Fields

### `validatorId`

- **Formula**: `node.node_identifier or str(node.id)`

### `validatorName`

- **Formula**: `node.node_name or "Unknown Validator"`

### `stake`

- **Source**: Stake snapshots for this validator
- **Formula**: average of `total_stake` snapshots in period
- **Fallback**: `0.0` when no snapshots

### `rewards`

- **Source**: Reward snapshots for this validator
- **Formula**:
  - if one snapshot: snapshot `total_rewards`
  - if multiple snapshots: `max(0, last.total_rewards - first.total_rewards)`
- **Fallback**: `0.0` when no snapshots

### `apr`

- **Note**: Included in API response.
- **As requested**: APR formula details intentionally skipped.

### `uptimePct`

- **Source**: Uptime history API by host id
- **Formula**: `uptime_data.get('uptime_pct', 0.0)`
- **Fallback**: `0.0`

### `jailed`

- **Source**: Parsed `validator_info` JSON
- **Formula**: `validator_info.get('jailed', False)`

### `slashingEvents`

- **Source**: Performance snapshots (`missed_counter`, `signed_blocks`)
- **Formula**:
  ```
  total_blocks = total_signed + total_missed
  IF total_blocks > 0 AND (total_missed / total_blocks) > 0.1:
    slashingEvents = 1
  ELSE:
    slashingEvents = 0
  ```

### `status`

- **Source**: Status function from validator score
- **Formula used in code**: `scoring.determine_status(val_score)`

### `score`, `scoreChange`

- **Note**: Included in API response.
- **As requested**: formula details intentionally skipped.

---

## healthMix

### `good`

- **Formula**: `healthyNodes`

### `warning`

- **Formula**: `warningNodes`

### `critical`

- **Formula**: `criticalNodes`

---

## riskIndicators

### `slashingRisk`

- **Source**: Total slashing events across fleet
- **Formula**: `scoring.determine_risk_level(total_slashing_events, 3, 5)`
- **Mapping**:
  - `high` if value >= 5
  - `medium` if value >= 3
  - else `low`

### `jailingRisk`

- **Source**: `jailed_count`
- **Formula**: `scoring.determine_risk_level(jailed_count, 1, 2)`
- **Mapping**:
  - `high` if value >= 2
  - `medium` if value >= 1
  - else `low`

### `stakeConcentration`

- **Source**: Gini coefficient of validator stakes
- **Steps**:
  1. `validator_stakes = [v.stake for v in validator_items if v.stake is not None]`
  2. `gini = helpers.calculate_gini_coefficient(validator_stakes)`
  3. `helpers.gini_to_concentration_level(gini)`
- **Level mapping**:
  - `low` if gini < 0.3
  - `medium` if 0.3 <= gini < 0.5
  - `high` if gini >= 0.5

---

## incidents

- **Source**: `_fetch_incidents_from_vizion(validator_nodes, ...)`
- **Behavior**: `[]` when no incidents in period (or Vizion unavailable)

---

## insights

- **Source**: `_generate_validator_fleet_insights(summary, validator_items, risk_indicators)`
- **Rules in code**:
  - add `"High Slashing Risk"` when `risk_indicators.slashingRisk == 'high'`
  - add `"Validators Jailed"` when `summary.jailedValidators > 0`
- otherwise `[]`

---

## trends

- **Source**: `_build_daily_trends(daily_requests={}, daily_rewards=...)`
- **Important behavior**:
  - `daily_requests` is passed as empty dict in validator-fleet, so `requestCount` is always `0.0`
  - rewards come from `_aggregate_daily_validator_rewards(...)`
  - trend row is added when `requests > 0 OR rewards > 0`

### `date`

- Day key in `YYYY-MM-DD`

### `requestCount`

- In this endpoint: always `0.0` (because `daily_requests={}`)

### `rewards`

- Daily value from `_aggregate_daily_validator_rewards(...)`

Other trend fields remain `null` because `TrendDataPoint` has optional fields and this endpoint only sets `date`, `requestCount`, `rewards`.

---

## Why your sample looks like this (exact code behavior)

1. `healthMix` shows `good=1`, `critical=9` because counts are uptime-bucket based (`>=95` good, `<=60` critical).
2. Many validators have `uptimePct=0.0`, so they fall into critical bucket.
3. `insights` is empty because:
   - `slashingRisk` is `low` (not high)
   - `jailedValidators` is `0`
4. `trends[].requestCount` is `0.0` for every day by design in this endpoint.
5. `trends[].rewards` are populated from daily reward aggregation logic.

---

## Consistency Check (Code Review Notes)

1. `summary.status` is score-based, while health counts are uptime-bucket based.
2. `summary.scoreChangePercent` is hardcoded `0.0`.
3. `riskIndicators.stakeConcentration` can be `high`, but no insight is generated for it in current logic.
4. In daily rewards aggregation, if a validator has only one snapshot in a day, code uses full cumulative `total_rewards` as that day's reward.
5. Because of point 4, `trends[].rewards` can look much larger than summary period delta values.

If you want, I can do a fix pass to make validator-fleet trend rewards strictly delta-based and align consistency across summary/trends.

---

## `/api/v1/reports/validator/<validatorId>` Response Fields

Team-friendly explanation (for people outside the project).  
Only exact code behavior is documented.

---

## Meta Fields

### `accountId`

- **What this means**: Which customer account owns this validator.
- **How we calculate it**: Taken from validator node subscription customer.
- **Formula used in code**: `str(subscription.customer_name.id)`
- **Fallback**: `"Unknown"`

### `accountName`

- **What this means**: Customer name for the account.
- **How we calculate it**: Taken from validator node subscription customer name.
- **Formula used in code**: `subscription.customer_name.name`
- **Fallback**: `"Unknown"`

### `periodStart`, `periodEnd`

- **What this means**: Report start and end dates based on selected range.
- **How we calculate it**: Format calculated period bounds.
- **Formula used in code**:
  - `helpers.format_date_for_response(period_start)`
  - `helpers.format_date_for_response(period_end)`

### `range`

- **What this means**: Selected report window.
- **Values**: `weekly` or `monthly`

### `timezone`

- **What this means**: Timezone used while calculating period bounds.
- **Fallback**: `UTC`

### `validatorId`, `validatorName`

- **What this means**: The specific validator requested in API path.
- **Formula used in code**:
  - `validatorId = node.node_identifier or str(node.id)`
  - `validatorName = node.node_name or "Unknown Validator"`

### `nodeId`, `nodeName`

- **Value in validator detail endpoint**: `null`

---

## Overview Fields

### `status`

- **What this means**: Overall health label for this validator (`good` / `warning` / `critical`).
- **How we calculate it**: Derived from validator score using status mapping function.
- **Formula used in code**: `scoring.determine_status(val_score)`

### `score`, `grade`, `scoreChange`, `scoreChangePercent`

- **What this means**:
  - `score`: current overall validator score
  - `grade`: score bucket label
  - `scoreChange`: current score minus previous period score
  - `scoreChangePercent`: percentage score change vs previous period
- **Note**: Included in API response.
- **As requested**: score formula details are intentionally skipped.

### `stakeDelta`

- **What this means**: How much average stake changed vs previous period.
- **How we calculate it**:
  - current stake = avg stake in current period
  - previous stake = avg stake in previous period
  - delta = current - previous
- **Formula used in code**: `helpers.calculate_change(avg_stake, prev_avg_stake)`

### `rewardsDelta`

- **What this means**: How much rewards changed vs previous period.
- **How we calculate it**:
  - current rewards = current period rewards delta
  - previous rewards = previous period rewards delta
  - delta = current - previous
- **Formula used in code**: `helpers.calculate_change(sum_rewards, prev_total_rewards_detail)`

---

## Metrics Fields

### `stake`

- **What this means**: Average stake of this validator during the selected period.
- **How we calculate it**: Average of `total_stake` from reward snapshots in period.
- **Fallback**: `0.0` when no snapshots

### `stakeChange`

- **What this means**: Absolute stake change vs previous period.
- **Formula used in code**: `helpers.calculate_change(avg_stake, prev_avg_stake)`

### `stakeChangePercent`

- **What this means**: Percentage stake change vs previous period.
- **Formula used in code**: `helpers.calculate_change_percent(avg_stake, prev_avg_stake)`

### `rewards`

- **What this means**: Rewards earned in selected period.
- **How we calculate it**:
  - if one snapshot: use `snapshot.total_rewards`
  - if multiple snapshots: `max(0, last.total_rewards - first.total_rewards)`

### `rewardsChangePercent`

- **What this means**: Percentage rewards change vs previous period.
- **Formula used in code**: `helpers.calculate_change_percent(sum_rewards, prev_total_rewards_detail)`

### `apr`

- **What this means**: Annualized rewards metric for this validator.
- **Note**: Included in API response.
- **As requested**: APR formula details are intentionally skipped.

### `aprChange`

- **Current behavior**: hardcoded `0.0`

### `uptimePct`

- **What this means**: Validator uptime percentage in selected period.
- **How we calculate it**: Read from uptime history API by host id.
- **Formula used in code**: `uptime_data.get('uptime_pct', 0.0)`

### `jailed`

- **What this means**: Whether validator is jailed currently.
- **How we calculate it**: Parse `validator_info` JSON and read `jailed` flag.
- **Formula used in code**: `validator_info.get('jailed', False)`

### `slashingEvents`

- **What this means**: Slashing risk indicator for this period (0 or 1).
- **How we calculate it**:
  - Sum `missed_counter` and `signed_blocks` from performance snapshots.
  - Compute miss rate = missed / (missed + signed).
  - If miss rate > 10%, mark 1 else 0.
- **Formula used in code**:
  - `total_blocks = total_signed + total_missed`
  - if `total_blocks > 0` and `(total_missed / total_blocks) > 0.1` then `slashingEvents = 1`, else `0`

---

## delegators

### `totalCount`

- **What this means**: Number of delegators for this validator.
- **How we calculate it**:
  1. Prefer latest reward snapshot `delegator_count`.
  2. If missing, fallback to count of RPC delegations response items.

### `topDelegators[]`

- **What this means**: Top 10 delegators by delegated amount.
- **How we calculate it**:
  - Fetch delegations from RPC using validator valoper address.
  - Sort by `amount` descending.
  - Take first 10.

For each item:

### `delegatorAddress`

- Delegator wallet address from RPC item.

### `delegatedStake`

- Delegated amount from RPC item.

### `delegatePercentOfValidator`

- Percentage of validator stake from RPC item (`pctOfValidator`).

### `joinedDate`

- **Current behavior**: not set by code, remains `null`.

---

## networkComparison

### `uptimeVsNetwork`, `rewardsVsNetwork`, `aprVsNetwork`, `reliabilityVsNetwork`

- **Current behavior**: all hardcoded `0.0` (placeholder TODO in code)

---

## incidents

- **What this means**: Alert/incident events mapped to this validator.
- **How we calculate it**: `_fetch_incidents_from_vizion([node], ...)`
- **Behavior**: `[]` when no events in selected period.

---

## insights

- **How we calculate it**: `_generate_validator_node_insights(metrics, delegators_info)`
- **Rules in code**:
  - add `"Validator is Jailed"` when `jailed == true`
  - add `"Low Delegator Count"` when `delegators.totalCount < 10`
- Otherwise insights remain empty.

---

## trends

- **What this means**: Daily uptime timeline for this validator.
- **How we calculate it**:
  1. Fetch uptime history data points.
  2. Group by date.
  3. Daily uptime% = `(up_points / total_points) * 100`.
  4. Add trend row for days where that date exists in aggregated uptime data.

### `date`

- Day key in `YYYY-MM-DD`

### `uptimePct`

- Daily uptime percentage for that day.

All other trend fields remain `null` in this endpoint because only `date` and `uptimePct` are set here.

---

## Why your sample looks like this (exact code behavior)

1. `uptimePct` is `100.0`, so trend rows show `100.0` for days where uptime data points exist.
2. `rewards = 0` and `rewardsChangePercent = -100.0` means previous period had rewards but current period reward delta is zero.
3. `stakeDelta` and `stakeChange` are positive because current average stake is higher than previous period average.
4. `insights` is empty because validator is not jailed and delegator count is high (`533`, not below 10).
5. `networkComparison` values are all `0.0` by current placeholder logic.

---

## Consistency Check (Code Review Notes)

1. `aprChange` is hardcoded `0.0`.
2. `networkComparison.*` fields are placeholders (`0.0`).
3. `joinedDate` in `topDelegators` is not populated by current code.
4. Trend rows are added only for dates that have uptime data points; missing days are not included.

If you want, I can do the same outsider-friendly rewrite for the earlier `rpc-fleet` and `rpc-node` sections too, so the full doc has one consistent tone.
