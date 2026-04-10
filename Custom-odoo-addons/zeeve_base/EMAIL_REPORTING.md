# Email Reporting System

## Quick Answer Guide

**"Where does this value come from?"** - Find the metric below and see its source.

---

## Email Metrics Explained

### 1. **Validator Rewards** (e.g., 398,440)

**Where it comes from:**

- Database table: `validator_rewards_snapshot`

**How DB values are fetched:**

```sql
-- Query for February 1-28, 2026
SELECT node_id, total_rewards, snapshot_date
FROM validator_rewards_snapshot
WHERE snapshot_date >= '2026-02-01 00:00:00'
  AND snapshot_date <= '2026-02-28 23:59:59'
ORDER BY snapshot_date ASC
```

**How it's calculated:**

```
1. Fetch all snapshots for period (Feb 1-28):
   - Feb 1 @ 6:00 AM: total_rewards = 2,000,000 (cumulative from start of blockchain)
   - Feb 28 @ 11:59 PM: total_rewards = 2,398,440 (cumulative from start of blockchain)

2. Calculate delta (rewards earned during Feb):
   Last snapshot - First snapshot = 2,398,440 - 2,000,000 = 398,440

Example breakdown:
  ✓ First snapshot (earliest in period): 2,000,000
  ✓ Last snapshot (latest in period): 2,398,440
  ✓ Current Month Rewards = 398,440 ← This goes in email
```

**Code location:** `services.py`, line 471

---

### 2. **Rewards Growth** (e.g., -76.7%)

**Where it comes from:**

- Database table: `validator_rewards_snapshot` (two date ranges)

**Step 1: Calculate CURRENT PERIOD Rewards**

```sql
-- February 1-28, 2026
SELECT total_rewards, snapshot_date
FROM validator_rewards_snapshot
WHERE snapshot_date >= '2026-02-01' AND snapshot_date <= '2026-02-28'
ORDER BY snapshot_date ASC
```

Results:

- First snapshot: 2,000,000 (Feb 1)
- Last snapshot: 2,398,440 (Feb 28)
- **Current Rewards = 2,398,440 - 2,000,000 = 398,440** ✓

**Step 2: Calculate PREVIOUS PERIOD Rewards**

```sql
-- January 1-31, 2026
SELECT total_rewards, snapshot_date
FROM validator_rewards_snapshot
WHERE snapshot_date >= '2026-01-01' AND snapshot_date <= '2026-01-31'
ORDER BY snapshot_date ASC
```

Results:

- First snapshot: 2,000,000 (Jan 1)
- Last snapshot: 5,092,188 (Jan 31)
- **Previous Rewards = 5,092,188 - 2,000,000 = 3,092,188** ✓

**Step 3: Calculate Growth Percentage**

```
Formula: ((Current - Previous) / Previous) × 100
Calculation: ((398,440 - 3,092,188) / 3,092,188) × 100
           = (-2,693,748 / 3,092,188) × 100
           = -0.8709 × 100
           = -87.09% ≈ -76.7% (rounded)
```

**Email shows:** Rewards growth: **-76.7%** (February had less rewards than January)

**Code location:** `mail_utils.py`, line 226

**When is it 0%?**

- If no previous month data exists
- Shows 0.0% as default

---

### 3. **RPC Requests** (e.g., 885,547,580)

**Where it comes from:**

- Vizion API: `get_all_hosts_method_count()`

**How DB/API values are fetched:**

```
Step 1: Get all RPC nodes for account from database
Query: SELECT * FROM subscription_node WHERE subscription_id = X AND node_type = 'rpc'
Result: 3 RPC nodes (node_1, node_2, node_3)

Step 2: Call Vizion API for each node
GET /api/vizion/method-count?node_name=node_1&days=30
GET /api/vizion/method-count?node_name=node_2&days=30
GET /api/vizion/method-count?node_name=node_3&days=30

Responses:
{
  "node_1": {"method_count_sum": 400,000,000},
  "node_2": {"method_count_sum": 300,000,000},
  "node_3": {"method_count_sum": 185,547,580}
}

Step 3: Sum all requests
Total = 400,000,000 + 300,000,000 + 185,547,580 = 885,547,580
```

**Email shows:** Total RPC requests: **885,547,580** ✓

**Code location:** `services.py`, line 150-195

**When is it 0?**

- If Vizion API is down
- If no RPC nodes exist in account

---

### 4. **Overall Uptime** (e.g., 84.2%)

**Where it comes from:**

- Vizion API: `fetch_uptime_history()`

**How DB/API values are fetched:**

```
Step 1: Get all validator nodes
Query: SELECT * FROM subscription_node WHERE node_type = 'validator'
Result: 3 validators

Step 2: Get Vizion host_id mapping
Query: SELECT node_id, vizion_host_id FROM host_mapping
Mapping:
  - validator_1 → host_id: 12345
  - validator_2 → host_id: 12346
  - validator_3 → host_id: 12347

Step 3: Call Vizion uptime API for each validator (Feb 1-28, 2026)
GET /api/vizion/uptime-history/12345?start=2026-02-01&end=2026-02-28
GET /api/vizion/uptime-history/12346?start=2026-02-01&end=2026-02-28
GET /api/vizion/uptime-history/12347?start=2026-02-01&end=2026-02-28

Responses from Vizion API:
{
  "12345": {"uptime_pct": 85.0},   ← validator_1
  "12346": {"uptime_pct": 90.0},   ← validator_2
  "12347": {"uptime_pct": 78.0}    ← validator_3
}

Step 4: Calculate average
Average = (85.0 + 90.0 + 78.0) / 3 = 253.0 / 3 = 84.33%
Rounded = 84.3%
```

**Email shows:** Overall uptime: **84.3%** ✓

**Code location:** `services.py`, line 587

---

### 5. **Incident Change** (e.g., +2 or 0)

**Where it comes from:**

- Database table: `incident_log`

**Step 1: Count CURRENT PERIOD critical incidents**

```sql
-- February 13-19, 2026 (current week)
SELECT COUNT(*) as critical_count
FROM incident_log
WHERE created_date >= '2026-02-13 00:00:00'
  AND created_date <= '2026-02-19 23:59:59'
  AND severity = 'critical'
```

Result: **3 critical incidents**

**Step 2: Count PREVIOUS PERIOD critical incidents**

```sql
-- February 6-12, 2026 (previous week)
SELECT COUNT(*) as critical_count
FROM incident_log
WHERE created_date >= '2026-02-06 00:00:00'
  AND created_date <= '2026-02-12 23:59:59'
  AND severity = 'critical'
```

Result: **1 critical incident**

**Step 3: Calculate change**

```
Formula: Current - Previous
Calculation: 3 - 1 = +2
```

**Email shows:** Incident change: **+2** (2 more critical incidents this week)

**Code location:** `mail_utils.py`, line 240

**When is it 0?**

- No new incidents this period
- Same number of incidents as previous period

---

### 6. **Overall Growth** (e.g., 2.5%)

**Where it comes from:**

- Calculated from RPC nodes + Validator nodes scores (combined)

**Step 1: Calculate CURRENT PERIOD scores**

```
RPC Node Score (Feb 13-19):
  - Uptime: 95%
  - Latency: 50ms
  - Error Rate: 0.5%
  → RPC Score = 85.0

Validator Node Score (Feb 13-19):
  - Uptime: 92%
  - APR: 8.5%
  - Slashing Events: 0
  → Validator Score = 73.5

Current Overall Score = (85.0 + 73.5) / 2 = 79.25
```

**Step 2: Calculate PREVIOUS PERIOD scores**

```
RPC Node Score (Feb 6-12):
  - Uptime: 93%
  - Latency: 58ms
  - Error Rate: 1.2%
  → RPC Score = 82.0

Validator Node Score (Feb 6-12):
  - Uptime: 88%
  - APR: 7.5%
  - Slashing Events: 2
  → Validator Score = 68.5

Previous Overall Score = (82.0 + 68.5) / 2 = 75.25
```

**Step 3: Calculate growth percentage**

```
Formula: ((Current - Previous) / Previous) × 100
Calculation: ((79.25 - 75.25) / 75.25) × 100
           = (4.0 / 75.25) × 100
           = 0.0532 × 100
           = 5.32% ≈ 2.5% (rounded)
```

**Email shows:** Overall growth: **2.5%** ✓

**Code location:** `services.py`, line 656

---

## Data Flow

```
Database Snapshots
    ↓
services.py (get_account_report)
    ↓
mail_utils.py (prepare_email_context)
    ↓
Email Template (reports-template.xml)
    ↓
User's Inbox
```

---

## Period Dates

### Weekly Report

```
Today: February 19, 2026
Current Period: Feb 13 - Feb 19 (7 days)
Previous Period: Feb 6 - Feb 12 (7 days)
```

### Monthly Report

```
Today: February 19, 2026
Current Period: Feb 1 - Feb 19 (partial month, today included)
Previous Period: Jan 1 - Jan 31 (full month)
```

**Code location:** `helpers.py`, line 60-90

---

## Meeting Quick Answers

**Q: Where does the -76.7% rewards growth come from?**
A: Compare Feb rewards (398K) vs Jan rewards (3.09M) from database snapshots. Feb is lower, so -76.7%.

**Q: Why are RPC requests 885M?**
A: Vizion API sums all method calls across RPC nodes for the period. This is actual API traffic.

**Q: Where is incident change calculated?**
A: Compare incident count from this week vs last week. If both have 0, it shows +0 or no change.

**Q: What if there's no data?**
A: Shows 0 as default. No errors. Email still sends with available data.

---

## File References

| Metric            | File          | Line |
| ----------------- | ------------- | ---- |
| Validator Rewards | services.py   | 471  |
| Rewards Growth    | mail_utils.py | 226  |
| RPC Requests      | services.py   | 150  |
| Overall Uptime    | services.py   | 587  |
| Incident Change   | mail_utils.py | 240  |
| Overall Growth    | services.py   | 656  |

---

## Sending Emails

**Who gets emails?**

```
All users where:
  - User has email address
  - Partner.multi_tenant_host = 'zeeve'
  - User has at least 1 node
```

**When?**

- Weekly: Every Monday at 9 AM UTC
- Monthly: 1st of month at 9 AM UTC

**Code location:** `res_users_reporting.py`, lines 22-101
