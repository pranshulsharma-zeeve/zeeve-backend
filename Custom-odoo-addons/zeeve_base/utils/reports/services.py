# -*- coding: utf-8 -*-
"""
Report service functions.

Provides the 5 main report generation functions that combine data from
Vizion, Odoo ORM, and RPC sources. Designed to be reusable by both
HTTP API controllers and email generation workflows.
"""

import logging
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta
import pytz
import requests

from . import helpers
from . import aggregation
from . import scoring
from . import clients
from . import models
from .pricing import TokenPriceService, convert_raw_value
from ....auth_module.utils import oauth as oauth_utils

_logger = logging.getLogger(__name__)

HEALTHY_UPTIME_THRESHOLD = 95.0  # % uptime required to treat a node as healthy
CRITICAL_UPTIME_THRESHOLD = 60.0  # % uptime at or below which a node is considered critical


def _classify_uptime_health(uptime_pct: float) -> str:
    """Classify uptime percentage into healthy/warning/critical buckets."""
    if uptime_pct >= HEALTHY_UPTIME_THRESHOLD:
        return 'healthy'
    if uptime_pct <= CRITICAL_UPTIME_THRESHOLD:
        return 'critical'
    return 'warning'


def _status_from_uptime(uptime_pct: float) -> str:
    """Map uptime buckets to public API status labels."""
    uptime_health = _classify_uptime_health(uptime_pct)
    if uptime_health == 'healthy':
        return 'good'
    return uptime_health


def _fetch_daily_rpc_trends_from_vizion(
    env,
    rpc_host_ids: List[str],
    num_days: int,
    user_email: Optional[str] = None
) -> tuple:
    """
    Fetch daily RPC request trends directly from Vizion API using VizionClient.
    
    Calls the Vision API to get method trend data with daily breakdowns.
    Returns both aggregated daily request counts and per-host daily counts.
    
    Args:
        env: Odoo environment
        rpc_host_ids: List of RPC host IDs to fetch trends for
        num_days: Number of days to fetch data for
        user_email: Email of the user for Vizion authentication (uses current user if not provided)
        
    Returns:
        Tuple of (daily_totals, per_host_daily):
        - daily_totals: Dict[date (YYYY-MM-DD) -> aggregated request count for that day]
        - per_host_daily: Dict[host_id -> Dict[date -> request count]]
    """
    if not rpc_host_ids:
        _logger.info("No RPC host IDs provided, returning empty trends")
        return {}, {}
    
    try:
        # Get user email if not provided
        if not user_email:
            try:
                user = env['res.users'].sudo().search([('id', '=', env.uid)], limit=1)
                user_email = user.login if user else None
            except Exception as e:
                _logger.warning(f"Could not get current user email: {e}")
                user_email = None
        
        if not user_email:
            _logger.warning("Cannot authenticate with Vizion: no user email available")
            return {}, {}
        
        # Create VizionClient with user email
        vizion_client = clients.VizionClient(env, user_email=user_email)
        
        # Fetch daily method trends (now returns tuple)
        daily_trends, per_host_daily = vizion_client.fetch_daily_method_trends(rpc_host_ids, num_days)
        
        _logger.info(f"Vizion trends fetched for user {user_email}: {len(daily_trends)} days with data")
        return daily_trends, per_host_daily
        
    except Exception as e:
        _logger.warning(f"Error fetching daily RPC trends from Vizion: {e}", exc_info=True)
        return {}, {}


def validate_account_report(report: 'models.AccountWeeklyReport') -> List[str]:
    """
    Validate account report data for anomalies and suspicious values.
    
    Args:
        report: AccountWeeklyReport object to validate
        
    Returns:
        List of validation warnings/errors (empty if all OK)
    """
    warnings = []
    
    try:
        # Validate overview metrics
        if report.overview:
            ov = report.overview
            if ov.overallUptimePct < 0 or ov.overallUptimePct > 100:
                warnings.append(f"Overall uptime anomaly: {ov.overallUptimePct}%")
            if ov.prevOverallUptimePct is not None and (ov.prevOverallUptimePct < 0 or ov.prevOverallUptimePct > 100):
                warnings.append(f"Previous overall uptime anomaly: {ov.prevOverallUptimePct}%")
                
        # Validate RPC summary
        if report.rpcSummary:
            rs = report.rpcSummary
            if rs.errorRatePct and rs.errorRatePct > 100:
                warnings.append(f"RPC error rate exceeds 100%: {rs.errorRatePct}%")
            if rs.avgLatencyMs and rs.avgLatencyMs > 100000:
                warnings.append(f"RPC latency anomaly: {rs.avgLatencyMs}ms (>100s)")
                
        # Validate validator summary  
        if report.validatorSummary:
            vs = report.validatorSummary
            if vs.totalRewards and vs.totalRewards > 1_000_000_000_000_000:
                warnings.append(
                    f"Validator rewards anomaly: {vs.totalRewards} (may be in wei, not tokens). "
                    "Consider unit conversion."
                )
            if vs.avgAPR and vs.avgAPR > 10000:
                warnings.append(f"Validator APR anomaly: {vs.avgAPR}% (unrealistic)")
    except AttributeError as e:
        # Silently catch attribute errors - don't break if model structure changes
        _logger.debug(f"Validation skipped due to attribute error: {e}")
            
    return warnings


def _validate_and_log_warnings(report: models.AccountWeeklyReport):
    """Helper to validate report and log any warnings."""
    warnings = validate_account_report(report)
    if warnings:
        for warning in warnings:
            _logger.warning(f"Report validation warning: {warning}")
    return warnings


def _aggregate_daily_rpc_requests(
    daily_vizion_data: Dict[str, float],
    period_start: datetime,
    period_end: datetime
) -> Dict[str, float]:
    """
    Filter daily RPC request counts to reporting period.
    
    Args:
        daily_vizion_data: Dict mapping date (YYYY-MM-DD) -> total requests from Vizion
        period_start: Start of reporting period
        period_end: End of reporting period
    
    Returns:
        Dict mapping date (YYYY-MM-DD) -> requests for dates in period
    """
    filtered_requests = {}
    
    try:
        _logger.info(f"Filtering {len(daily_vizion_data)} days of RPC data to reporting period")
        
        for date_str, request_count in daily_vizion_data.items():
            try:
                # Parse the date
                parsed_date = datetime.strptime(date_str, '%Y-%m-%d')
                parsed_date = pytz.UTC.localize(parsed_date)
                
                # Check if within reporting period
                if period_start <= parsed_date <= period_end:
                    filtered_requests[date_str] = float(request_count)
                    _logger.debug(f"Included {date_str}: {request_count} requests")
                else:
                    _logger.debug(f"Excluded {date_str}: outside period [{period_start.date()}, {period_end.date()}]")
            except Exception as e:
                _logger.warning(f"Error processing date {date_str}: {e}")
                continue
    
    except Exception as e:
        _logger.warning(f"Error filtering RPC requests: {e}", exc_info=True)
    
    _logger.info(f"RPC requests filtered to {len(filtered_requests)} days within period")
    return filtered_requests


def _collect_protocols_from_nodes(nodes: List[Any]) -> List[Any]:
    """Return unique protocol.master records referenced by the given nodes."""
    protocols = []
    seen = set()
    for node in nodes:
        subscription = getattr(node, 'subscription_id', None)
        protocol = getattr(subscription, 'protocol_id', None) if subscription else None
        if protocol and protocol.id not in seen:
            seen.add(protocol.id)
            protocols.append(protocol)
    return protocols


def _build_protocol_metadata(protocols: List[Any], price_service: TokenPriceService) -> Dict[int, Dict[str, Any]]:
    """Build metadata map keyed by protocol ID (price, decimals, etc.)."""
    metadata = {}
    if not protocols:
        return metadata
    prices = price_service.get_prices(protocols)
    for protocol in protocols:
        metadata[protocol.id] = {
            'protocol': protocol,
            'price': prices.get(protocol.id),
            'reward_decimals': protocol.reward_decimals or 0,
            'stake_decimals': protocol.stake_decimals or 0,
        }
    return metadata


def _map_nodes_to_protocol_meta(nodes: List[Any], protocol_metadata: Dict[int, Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """Map node IDs to their protocol metadata."""
    mapping = {}
    for node in nodes:
        subscription = getattr(node, 'subscription_id', None)
        protocol = getattr(subscription, 'protocol_id', None) if subscription else None
        if protocol and protocol.id in protocol_metadata:
            mapping[node.id] = protocol_metadata[protocol.id]
    return mapping


def _convert_amount_with_metadata(
    raw_value: Optional[float],
    protocol_meta: Optional[Dict[str, Any]],
    decimals_key: str,
    missing_price_protocols: Optional[set] = None
) -> Tuple[float, Optional[float]]:
    """Normalize and convert a raw amount using metadata for stake/reward decimals."""
    decimals = 0
    price = None
    protocol_name = None
    if protocol_meta:
        decimals = protocol_meta.get(decimals_key, 0) or 0
        price = protocol_meta.get('price')
        protocol = protocol_meta.get('protocol')
        protocol_name = protocol.name if protocol else None
    tokens, usd = convert_raw_value(raw_value, decimals, price)
    if usd is None and price is None and protocol_name and missing_price_protocols is not None:
        missing_price_protocols.add(protocol_name)
    return tokens, usd


def _aggregate_daily_uptime_from_history(
    uptime_data_points: Dict[str, Any],
    period_start: datetime,
    period_end: datetime
) -> Dict[str, float]:
    """
    Aggregate uptime data points by day to calculate daily uptime percentages.
    
    Args:
        uptime_data_points: Dict from uptime history API with port status arrays
            Format: {'Port 8545 Status': [{'timestamp': ..., 'value_avg': 1/0}, ...]}
        period_start: Start of reporting period
        period_end: End of reporting period
    
    Returns:
        Dict mapping date (YYYY-MM-DD) -> uptime percentage for that day (0-100)
    """
    daily_uptime = {}
    
    try:
        # Collect all data points with timestamps
        all_points = []
        for port_name, data_points in uptime_data_points.items():
            if isinstance(data_points, list):
                for point in data_points:
                    if isinstance(point, dict):
                        all_points.append(point)
        
        if not all_points:
            _logger.debug("No uptime data points found")
            return daily_uptime
        
        _logger.debug(f"Found {len(all_points)} uptime data points to process")
        
        # Group points by day
        points_by_day = {}
        for point in all_points:
            try:
                # Parse timestamp - prefer 'clock' field (Unix timestamp in seconds)
                clock = point.get('clock')
                timestamp = point.get('timestamp')
                dt = None
                
                # Priority 1: Use 'clock' field (Unix timestamp in seconds)
                if clock is not None:
                    try:
                        if isinstance(clock, str):
                            clock = int(clock)
                        dt = datetime.fromtimestamp(clock, tz=pytz.UTC)
                    except Exception as e:
                        _logger.debug(f"Could not parse clock field {clock}: {e}")
                
                # Priority 2: Try timestamp field
                if dt is None and timestamp is not None:
                    if isinstance(timestamp, str):
                        # Try various parsing methods
                        try:
                            # Try ISO format
                            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                        except:
                            try:
                                # Try parsing date strings like "Sat Feb 21 2026"
                                dt = datetime.strptime(timestamp, '%a %b %d %Y')
                                dt = pytz.UTC.localize(dt)
                            except:
                                try:
                                    # Try as millisecond timestamp
                                    dt = datetime.fromtimestamp(int(timestamp) / 1000, tz=pytz.UTC)
                                except:
                                    _logger.debug(f"Could not parse timestamp string: {timestamp}")
                    elif isinstance(timestamp, (int, float)):
                        # Assume millisecond timestamp
                        try:
                            dt = datetime.fromtimestamp(timestamp / 1000, tz=pytz.UTC)
                        except:
                            _logger.debug(f"Could not parse timestamp number: {timestamp}")
                
                if dt is None:
                    _logger.debug(f"Could not parse any timestamp from point: {point}")
                    continue
                
                if dt is None:
                    continue
                
                # Ensure timezone aware
                if dt.tzinfo is None:
                    dt = pytz.UTC.localize(dt)
                
                # Filter to period
                if dt < period_start or dt > period_end:
                    _logger.debug(f"Point {dt} outside period [{period_start}, {period_end}]")
                    continue
                
                date_key = dt.strftime('%Y-%m-%d')
                value_avg = float(point.get('value_avg', 0))
                
                if date_key not in points_by_day:
                    points_by_day[date_key] = {'up': 0, 'total': 0}
                
                points_by_day[date_key]['total'] += 1
                if value_avg >= 1:
                    points_by_day[date_key]['up'] += 1
            except Exception as e:
                _logger.debug(f"Error processing uptime data point {point}: {e}")
                continue
        
        # Calculate daily uptime percentages
        for date_key, counts in points_by_day.items():
            if counts['total'] > 0:
                uptime_pct = (counts['up'] / counts['total']) * 100
                daily_uptime[date_key] = round(uptime_pct, 2)
        
        _logger.info(f"Aggregated uptime to {len(daily_uptime)} days: {list(daily_uptime.keys())}")
    except Exception as e:
        _logger.warning(f"Error aggregating daily uptime: {e}", exc_info=True)
    
    return daily_uptime


def _aggregate_daily_validator_rewards(
    snapshot_repo: clients.SnapshotRepository,
    validator_node_ids: List[int],
    period_start: datetime,
    period_end: datetime,
    node_protocol_meta: Optional[Dict[int, Dict[str, Any]]] = None,
    missing_price_protocols: Optional[set] = None
) -> Dict[str, float]:
    """
    Aggregate daily validator EARNED rewards from snapshots (matching overview calculation).
    
    Calculates earned rewards as cumulative delta per validator per day (last - first),
    then sums across all validators for each day. This matches the validator_total_rewards
    calculation in the overview section.
    
    Args:
        snapshot_repo: SnapshotRepository instance
        validator_node_ids: List of validator node IDs
        period_start: Start of reporting period
        period_end: End of reporting period
    
        node_protocol_meta: Optional mapping of node_id -> protocol metadata (for price conversion)
        missing_price_protocols: Optional set to record protocols that lack pricing

    Returns:
        Dict mapping date (YYYY-MM-DD) -> total earned rewards for that day
    """
    daily_rewards = {}
    
    try:
        _logger.info(f"Starting validator rewards aggregation for {len(validator_node_ids)} validator nodes")
        if not validator_node_ids:
            _logger.info(f"No validator node IDs provided, returning empty")
            return daily_rewards
        
        # Fetch all snapshots for the period
        snapshots = snapshot_repo.get_validator_reward_snapshots(
            validator_node_ids, period_start, period_end
        )
        _logger.info(f"Fetched {len(snapshots)} snapshots for trends calculation")
        
        if not snapshots:
            _logger.info(f"No snapshots found, returning empty")
            return daily_rewards
        
        # Group snapshots by node AND day to calculate daily deltas
        snapshots_by_node_and_day = {}
        for snapshot in snapshots:
            if not hasattr(snapshot, 'snapshot_date') or not hasattr(snapshot, 'total_rewards'):
                _logger.debug(f"Snapshot missing required attributes")
                continue
            
            snapshot_date = snapshot.snapshot_date
            if snapshot_date is None:
                continue
            
            # Ensure timezone aware
            if snapshot_date.tzinfo is None:
                snapshot_date = pytz.UTC.localize(snapshot_date)
            
            date_key = snapshot_date.strftime('%Y-%m-%d')
            node_id = snapshot.node_id.id if hasattr(snapshot.node_id, 'id') else snapshot.node_id
            
            key = (node_id, date_key)
            if key not in snapshots_by_node_and_day:
                snapshots_by_node_and_day[key] = []
            
            snapshots_by_node_and_day[key].append(snapshot)
        
        _logger.info(f"Grouped snapshots into {len(snapshots_by_node_and_day)} node-day combinations")
        
        # Calculate daily earned rewards using cumulative delta per validator
        for (node_id, date_key), day_snapshots in snapshots_by_node_and_day.items():
            # Sort by date to get first and last for delta calculation
            sorted_snaps = sorted(day_snapshots, key=lambda s: s.snapshot_date)
            
            if len(sorted_snaps) == 1:
                # Single snapshot for this day - use total_rewards as daily earned
                daily_earned = float(sorted_snaps[0].total_rewards or 0)
            else:
                # Multiple snapshots - calculate delta (last - first)
                first_total = float(sorted_snaps[0].total_rewards or 0)
                last_total = float(sorted_snaps[-1].total_rewards or 0)
                daily_earned = last_total - first_total
                if daily_earned < 0:
                    _logger.warning(
                        f"Negative daily reward delta for validator {node_id} on {date_key}: "
                        f"first={first_total}, last={last_total}. Treating as 0."
                    )
                    daily_earned = 0
            
            if date_key not in daily_rewards:
                daily_rewards[date_key] = 0.0

            protocol_meta = node_protocol_meta.get(node_id) if node_protocol_meta else None
            normalized_tokens, usd_amount = _convert_amount_with_metadata(
                daily_earned,
                protocol_meta,
                'reward_decimals',
                missing_price_protocols
            )
            aggregate_value = usd_amount if usd_amount is not None else normalized_tokens
            daily_rewards[date_key] += round(aggregate_value, 2)
            _logger.info(
                f"Validator {node_id} on {date_key}: earned {aggregate_value} (USD fallback={usd_amount is None}), "
                f"cumulative day total: {daily_rewards[date_key]}"
            )
    
    except Exception as e:
        _logger.warning(f"Error aggregating daily validator rewards: {e}", exc_info=True)
    
    _logger.info(f"Validator rewards aggregation complete. Found {len(daily_rewards)} days with data: {daily_rewards}")
    return daily_rewards


def _build_daily_trends(
    daily_requests: Dict[str, float],
    daily_rewards: Dict[str, float],
    period_start: datetime,
    period_end: datetime
) -> List[models.TrendDataPoint]:
    """
    Build trends array from daily request and reward data.
    
    Creates a TrendDataPoint for each day in the period, filling in requests and rewards.
    
    Args:
        daily_requests: Dict mapping date (YYYY-MM-DD) -> requests
        daily_rewards: Dict mapping date (YYYY-MM-DD) -> rewards
        period_start: Start of reporting period
        period_end: End of reporting period
    
    Returns:
        List of TrendDataPoint objects sorted by date
    """
    trends = []
    
    try:
        _logger.info(f"Building daily trends. Daily requests: {daily_requests}, Daily rewards: {daily_rewards}")
        _logger.info(f"Period: {period_start} to {period_end}")
        
        # Generate all dates in the period
        current_date = period_start.replace(hour=0, minute=0, second=0, microsecond=0)
        
        while current_date <= period_end:
            date_key = current_date.strftime('%Y-%m-%d')
            
            # Get requests and rewards for this day (0 if not present)
            requests = daily_requests.get(date_key, 0.0)
            rewards = daily_rewards.get(date_key, 0.0)
            
            _logger.debug(f"Date {date_key}: requests={requests}, rewards={rewards}")
            
            # Include day if it has ANY data (requests > 0 OR rewards > 0)
            if requests > 0 or rewards > 0:
                trend_point = models.TrendDataPoint(
                    date=date_key,
                    requestCount=requests,
                    rewards=rewards
                )
                trends.append(trend_point)
                _logger.info(f"Added trend for {date_key}: requests={requests}, rewards={rewards}")
            else:
                _logger.debug(f"Skipping {date_key}: no data (requests={requests}, rewards={rewards})")
            
            # Move to next day
            current_date += timedelta(days=1)
    
    except Exception as e:
        _logger.warning(f"Error building daily trends: {e}", exc_info=True)
    
    _logger.info(f"Trends building complete. Total trend points: {len(trends)}")
    return trends


def get_account_report(
    env,
    account_id: int,
    range_type: str = 'weekly',
    timezone_str: str = 'UTC'
) -> models.AccountWeeklyReport:
    """
    Generate account weekly/monthly report.
    
    Aggregates data from all RPC nodes and validators belonging to the account.
    
    Args:
        env: Odoo environment
        account_id: User/account ID
        range_type: 'weekly' or 'monthly'
        timezone_str: User's timezone
    
    Returns:
        AccountWeeklyReport data object
    """

    
    # Calculate period bounds
    period_start, period_end, prev_start, prev_end = helpers.calculate_period_bounds(
        range_type, timezone_str
    )
    
    # Get user email for Vizion authentication
    user = env['res.users'].sudo().search([('id', '=', account_id)], limit=1)
    user_email = user.login if user else None
    
    # Initialize clients/repositories
    node_repo = clients.NodeRepository(env)
    vizion_client = clients.VizionClient(env, user_email=user_email)
    snapshot_repo = clients.SnapshotRepository(env)
    
    # Fetch all nodes for account
    all_nodes = node_repo.get_nodes_by_account(account_id)
    rpc_nodes = [n for n in all_nodes if n.node_type == 'rpc']
    validator_nodes = [n for n in all_nodes if n.node_type == 'validator']

    price_service = TokenPriceService(env)
    protocol_metadata = _build_protocol_metadata(
        _collect_protocols_from_nodes(all_nodes),
        price_service
    )
    validator_protocol_meta = _map_nodes_to_protocol_meta(validator_nodes, protocol_metadata)
    missing_price_protocols: set = set()
    

    
    # Get account name (from first node's subscription customer)
    account_name = "Unknown Account"
    if all_nodes:
        subscription = all_nodes[0].subscription_id
        if subscription and subscription.customer_name:
            account_name = subscription.customer_name.name or "Unknown"
    
    # Build meta
    meta = models.ReportMeta(
        accountId=str(account_id),
        accountName=account_name,
        periodStart=helpers.format_date_for_response(period_start),
        periodEnd=helpers.format_date_for_response(period_end),
        range=range_type,
        timezone=timezone_str
    )
    
    # Fetch login data once and build host ID mapping to avoid repeated API calls
    host_id_mapping = {}
    if all_nodes:
        try:
            subscription = all_nodes[0].subscription_id
            if subscription and subscription.customer_name:
                customer_email = subscription.customer_name.email
                login_response = oauth_utils.login_with_email(customer_email)
                if login_response and login_response.get('success'):
                    host_data_list = login_response.get('hostData', [])
                    host_id_mapping = node_repo.build_host_id_mapping(host_data_list)
        except Exception as e:
            pass
    
    # Process RPC nodes - batch fetch protocol data and uptime history for all nodes
    rpc_highlights = []
    rpc_total_requests = 0.0
    rpc_uptime_sum = 0.0
    rpc_latency_sum = 0.0
    rpc_error_rate_sum = 0.0
    rpc_healthy_count = 0
    rpc_critical_count = 0
    
    # Collect all host IDs using cached mapping
    rpc_host_ids = []
    rpc_host_id_to_node = {}
    for node in rpc_nodes:
        host_id = node_repo.get_vizion_host_id(node, host_id_mapping)
        if host_id:
            rpc_host_ids.append(host_id)
            rpc_host_id_to_node[host_id] = node
    
    # Fetch protocol data and uptime history for all RPC nodes in batch
    current_time = datetime.now(pytz.UTC).isoformat()
    rpc_protocol_data_batch = {}
    rpc_uptime_data_batch = {}  # Uptime available for all node types (RPC, validator, archive)
    rpc_method_count_data = {}  # Will store method counts for RPC nodes
    rpc_prev_method_count_data = {}  # Will store method counts for previous period
    
    prev_total_requests_fleet = 0.0
    prev_total_requests_fleet = 0.0
    if rpc_host_ids:
        # Define protocol_name before use (default to 'ethereum' if no nodes)
        protocol_name = 'ethereum'
        if rpc_nodes:
            protocol = rpc_nodes[0].subscription_id.protocol_id
            protocol_name = protocol.name.lower().replace(' ', '') if protocol else 'ethereum'
        
        # Fetch uptime history from dedicated API (available for all node types)
        rpc_uptime_data_batch = vizion_client.fetch_uptime_history(
            rpc_host_ids, protocol_name, current_time, range_type
        ) or {}
        rpc_protocol_data_batch = vizion_client.fetch_protocol_data(rpc_host_ids, current_time, range_type, protocol_name) or {}
        
        
        # Fetch method counts for RPC nodes using Vision API
        # Fetch bulk data for both current and previous periods in one API call
        rpc_method_count_data = {}
        rpc_prev_method_count_data = {}
        per_host_daily_data = {}  # Per-host daily request counts for individual node assignment
        per_host_prev_data = {}   # Per-host previous period data
        try:
            # Fetch data for double the period to get both current and previous periods
            # For weekly: 14 days (7 current + 7 previous)
            # For monthly: 60 days (30 current + 30 previous)
            num_days = helpers.get_days_in_range(range_type)
            bulk_num_days = num_days * 2
            
            # Fetch daily RPC trends directly from Vision API (now returns tuple)
            daily_rpc_data, per_host_daily = _fetch_daily_rpc_trends_from_vizion(env, rpc_host_ids, bulk_num_days, user_email)
            _logger.info(f"Fetched daily RPC trends from Vision API: {len(daily_rpc_data)} days, {len(per_host_daily)} hosts with data")
            _logger.info(f"Date range check: period={helpers.format_date_for_response(period_start)} to {helpers.format_date_for_response(period_end)}, prev={helpers.format_date_for_response(prev_start)} to {helpers.format_date_for_response(prev_end)}")
            _logger.info(f"Daily RPC data dates: {sorted(daily_rpc_data.keys())}")
            
            # Show per-host data structure
            for host_id in list(per_host_daily.keys())[:2]:  # Show first 2 hosts
                host_dates = list(per_host_daily[host_id].keys())
                _logger.info(f"Per-host {host_id}: {len(host_dates)} dates, sample: {host_dates[:3]}")
            
            # Separate into current and previous periods (both totals and per-host)
            for date_str, total_requests in daily_rpc_data.items():
                try:
                    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                    date_obj = pytz.UTC.localize(date_obj)
                    
                    if period_start <= date_obj <= period_end:
                        # Current period - aggregate to a single entry for all RPC nodes
                        # (We get total requests per day, not per-node)
                        rpc_method_count_data['_total'] = rpc_method_count_data.get('_total', 0) + total_requests
                    elif prev_start <= date_obj <= prev_end:
                        # Previous period
                        rpc_prev_method_count_data['_total'] = rpc_prev_method_count_data.get('_total', 0) + total_requests
                except Exception as e:
                    _logger.debug(f"Error processing daily RPC data for date {date_str}: {e}")
                    continue
            
            _logger.info(f"Daily RPC totals - Current: {rpc_method_count_data.get('_total', 0)}, Previous: {rpc_prev_method_count_data.get('_total', 0)}")
            
            # Build per-host mappings: host_id -> total requests for the period
            # This allows individual nodes to get their own counts instead of equal distribution
            for host_id, host_daily_data in per_host_daily.items():
                host_current_total = 0
                host_prev_total = 0
                
                for date_str, request_count in host_daily_data.items():
                    try:
                        date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                        date_obj = pytz.UTC.localize(date_obj)
                        
                        if period_start <= date_obj <= period_end:
                            host_current_total += request_count
                        elif prev_start <= date_obj <= prev_end:
                            host_prev_total += request_count
                    except Exception as e:
                        _logger.debug(f"Error processing host {host_id} data for date {date_str}: {e}")
                        continue
                
                if host_current_total > 0:
                    per_host_daily_data[host_id] = host_current_total
                    _logger.debug(f"Host {host_id}: current={host_current_total}, prev={host_prev_total}")
                if host_prev_total > 0:
                    per_host_prev_data[host_id] = host_prev_total
            
            _logger.info(f"Separated RPC data: {rpc_method_count_data}, previous: {rpc_prev_method_count_data}")
            _logger.info(f"Per-host data: {len(per_host_daily_data)} hosts with current period data, {len(per_host_prev_data)} with previous data")
            
            # Log per-host aggregation details
            per_host_current_sum = sum(per_host_daily_data.values())
            per_host_prev_sum = sum(per_host_prev_data.values())
            _logger.info(f"Per-host sums: current={per_host_current_sum}, prev={per_host_prev_sum}")
            _logger.info(f"Per-host current data (top 5): {dict(list(per_host_daily_data.items())[:5])}")
        except Exception as e:
            _logger.warning(f"Error fetching RPC method counts from Vision API: {e}")
            rpc_method_count_data = {}
            rpc_prev_method_count_data = {}
            per_host_daily_data = {}
            per_host_prev_data = {}
    
    # Fetch previous period RPC data for request and uptime comparisons
    rpc_prev_uptime_data_batch = {}
    rpc_prev_protocol_data_batch = {}
    if rpc_host_ids and vizion_client:
        try:
            protocol = rpc_nodes[0].subscription_id.protocol_id
            protocol_name = protocol.name.lower().replace(' ', '') if protocol else 'ethereum'
            prev_time = prev_end.isoformat()
            
            rpc_prev_uptime_data_batch = vizion_client.fetch_uptime_history(
                rpc_host_ids, protocol_name, prev_time, range_type
            ) or {}
            rpc_prev_protocol_data_batch = vizion_client.fetch_protocol_data(
                rpc_host_ids, prev_time, range_type, protocol_name
            ) or {}
        except Exception as e:
            _logger.warning(f"Error fetching previous period RPC data: {e}")
    
    # Calculate RPC node count upfront (needed for fallback request count distribution)
    rpc_node_count = len(rpc_nodes)
    
    # Process each node with batch data
    for node in rpc_nodes:
        try:
            host_id = node_repo.get_vizion_host_id(node, host_id_mapping)
            
            # If no host_id, use default values instead of skipping
            if not host_id:
                uptime_pct = 0.0
                protocol_metrics = {'latencyMs': 0.0, 'errorCount': 0.0, 'errorRatePct': 0.0}
                request_count = 0.0
                error_rate_pct = 0.0
            else:
                # Get metrics from protocol data
                protocol_metrics = _parse_rpc_protocol_data(rpc_protocol_data_batch.get(host_id, {}))
                
                # Get uptime from uptime history API
                uptime_data = rpc_uptime_data_batch.get(host_id, {})
                uptime_pct = uptime_data.get('uptime_pct', 0.0)
                # Get request count from method count data (RPC nodes only)
                # Priority: 1) Per-host data 2) Node name lookup 3) Equal distribution of total
                request_count = 0.0
                if host_id in per_host_daily_data:
                    # Use per-host aggregated request count (BEST - specific to this host)
                    request_count = per_host_daily_data[host_id]
                elif node.node_name in rpc_method_count_data:
                    # Try node name lookup (in case of different mapping)
                    request_count = rpc_method_count_data[node.node_name]
                elif '_total' in rpc_method_count_data and rpc_node_count > 0:
                    # Fallback: Distribute total requests equally across nodes if per-node data not available
                    request_count = rpc_method_count_data['_total'] / rpc_node_count
                
                # Calculate error rate with request count
                error_rate_pct = (protocol_metrics['errorCount'] / request_count * 100) if request_count > 0 else 0.0
            
            node_status = _status_from_uptime(uptime_pct)
            
            uptime_health = _classify_uptime_health(uptime_pct)
            if uptime_health == 'healthy':
                rpc_healthy_count += 1
            elif uptime_health == 'critical':
                rpc_critical_count += 1

            rpc_highlights.append(models.RpcHighlight(
                nodeId=node.node_identifier or str(node.id),
                nodeName=node.node_name or "Unknown Node",
                uptimePct=uptime_pct,
                latencyMs=protocol_metrics['latencyMs'],
                requestCount=request_count,
                errorCount=protocol_metrics['errorCount'],
                errorRatePct=error_rate_pct,
                status=node_status
            ))
            
            rpc_total_requests += request_count
            rpc_uptime_sum += uptime_pct
            rpc_latency_sum += protocol_metrics['latencyMs']
            rpc_error_rate_sum += protocol_metrics['errorCount']  # Sum error COUNTS, not percentages
        except Exception as e:
            _logger.exception(f"Error processing RPC node {node.id}: {e}")
    
    # Calculate RPC averages
    rpc_avg_uptime = rpc_uptime_sum / rpc_node_count if rpc_node_count > 0 else 0.0
    rpc_avg_latency = rpc_latency_sum / rpc_node_count if rpc_node_count > 0 else 0.0
    # CRITICAL FIX: Calculate error rate from aggregated totals, not average of percentages
    # Formula: (total_errors / total_requests * 100)
    rpc_avg_error_rate = (rpc_error_rate_sum / rpc_total_requests * 100) if rpc_total_requests > 0 else 0.0
    
    prev_rpc_uptime_sum = 0.0
    prev_rpc_total_requests = 0.0
    if rpc_host_ids:
        for node in rpc_nodes:
            host_id = node_repo.get_vizion_host_id(node, host_id_mapping)
            if host_id:
                prev_uptime_data = rpc_prev_uptime_data_batch.get(host_id, {})
                prev_rpc_uptime_sum += prev_uptime_data.get('uptime_pct', 0.0)
            if host_id and host_id in per_host_prev_data:
                prev_rpc_total_requests += per_host_prev_data[host_id]
            elif node.node_name in rpc_prev_method_count_data:
                prev_rpc_total_requests += rpc_prev_method_count_data[node.node_name]
            elif '_total' in rpc_prev_method_count_data and rpc_node_count > 0:
                prev_rpc_total_requests += rpc_prev_method_count_data['_total'] / rpc_node_count
    
    rpc_summary = models.RpcSummary(
        totalNodes=rpc_node_count,
        healthyNodes=rpc_healthy_count,
        criticalNodes=rpc_critical_count,
        avgUptimePct=round(rpc_avg_uptime, 2),
        avgLatencyMs=round(rpc_avg_latency, 2),
        errorRatePct=round(rpc_avg_error_rate, 2),
        totalRequests=round(rpc_total_requests, 0),
        prevTotalRequests=round(prev_rpc_total_requests, 0) if prev_rpc_total_requests > 0 else None
    )
    
    # Process validator nodes
    validator_highlights = []
    validator_total_stake = 0.0
    validator_total_rewards = 0.0
    validator_apr_sum = 0.0
    validator_uptime_sum = 0.0
    validator_jailed_count = 0
    validator_total_rewards_usd = 0.0
    validator_total_stake_usd = 0.0
    validator_healthy_count = 0
    validator_critical_count = 0
    
    # Collect validator host IDs for batch uptime fetching
    validator_host_ids = []
    for node in validator_nodes:
        host_id = node_repo.get_vizion_host_id(node, host_id_mapping)
        if host_id:
            validator_host_ids.append(host_id)
    
    # Batch fetch uptime history for validators (available for all node types)
    validator_uptime_data_batch = {}
    if validator_host_ids and validator_nodes:
        protocol = validator_nodes[0].subscription_id.protocol_id
        protocol_name = protocol.name.lower().replace(' ', '') if protocol else 'ethereum'
        validator_uptime_data_batch = vizion_client.fetch_uptime_history(
            validator_host_ids, protocol_name, current_time, range_type
        ) or {}
    
    # Fetch previous period uptime data for validators
    validator_prev_uptime_data_batch = {}
    if validator_host_ids and validator_nodes:
        try:
            protocol = validator_nodes[0].subscription_id.protocol_id
            protocol_name = protocol.name.lower().replace(' ', '') if protocol else 'ethereum'
            prev_time = prev_end.isoformat()
            validator_prev_uptime_data_batch = vizion_client.fetch_uptime_history(
                validator_host_ids, protocol_name, prev_time, range_type
            ) or {}
        except Exception as e:
            _logger.warning(f"Error fetching previous period validator uptime: {e}")
    
    # Fetch validator snapshots
    validator_node_ids = [n.id for n in validator_nodes]
    reward_snapshots = snapshot_repo.get_validator_reward_snapshots(
        validator_node_ids, period_start, period_end
    )
    
    # Group snapshots by node
    snapshots_by_node = {}
    for snapshot in reward_snapshots:
        node_id = snapshot.node_id.id
        if node_id not in snapshots_by_node:
            snapshots_by_node[node_id] = []
        snapshots_by_node[node_id].append(snapshot)
    
    # Fetch performance snapshots (contains missed_counter, signed_blocks data)
    performance_snapshots = snapshot_repo.get_validator_performance_snapshots(
        validator_node_ids, period_start, period_end
    )
    
    # Group performance snapshots by node
    perf_snapshots_by_node = {}
    for snapshot in performance_snapshots:
        node_id = snapshot.node_id.id
        if node_id not in perf_snapshots_by_node:
            perf_snapshots_by_node[node_id] = []
        perf_snapshots_by_node[node_id].append(snapshot)
    
    # Fetch previous period validator snapshots for comparison data
    prev_reward_snapshots = snapshot_repo.get_validator_reward_snapshots(
        validator_node_ids, prev_start, prev_end
    )
    prev_snapshots_by_node = {}
    for snapshot in prev_reward_snapshots:
        node_id = snapshot.node_id.id
        if node_id not in prev_snapshots_by_node:
            prev_snapshots_by_node[node_id] = []
        prev_snapshots_by_node[node_id].append(snapshot)
    
    for node in validator_nodes:
        try:
            node_snapshots = snapshots_by_node.get(node.id, [])
            avg_stake = 0.0
            total_rewards = 0.0

            # Aggregate rewards and stake (or use zero defaults if no snapshots)
            if node_snapshots:
                avg_stake = aggregation.aggregate_series(
                    [{'value': s.total_stake} for s in node_snapshots],
                    'value',
                    aggregation.AggregationType.AVG
                )
                # Calculate rewards earned as delta: last cumulative - first cumulative
                sorted_snapshots = sorted(node_snapshots, key=lambda s: s.snapshot_date)
                if len(sorted_snapshots) == 1:
                    total_rewards = sorted_snapshots[0].total_rewards
                else:
                    reward_delta = sorted_snapshots[-1].total_rewards - sorted_snapshots[0].total_rewards
                    if reward_delta < 0:
                        # CRITICAL: Log negative deltas which indicate slashing/unjailing events
                        _logger.warning(
                            f"Negative reward delta detected for validator node {node.id}: "
                            f"prev={sorted_snapshots[0].total_rewards}, current={sorted_snapshots[-1].total_rewards}, "
                            f"delta={reward_delta}. This may indicate slashing or unjailing events."
                        )
                    total_rewards = max(0, reward_delta)
            else:
                _logger.warning(f"No snapshots found for validator node {node.id}, using zero defaults")

            protocol_meta = validator_protocol_meta.get(node.id)
            stake_tokens_display, stake_usd_value = _convert_amount_with_metadata(
                avg_stake,
                protocol_meta,
                'stake_decimals',
                missing_price_protocols
            )
            reward_tokens_display, reward_usd_value = _convert_amount_with_metadata(
                total_rewards,
                protocol_meta,
                'reward_decimals',
                missing_price_protocols
            )
            validator_total_stake_usd += stake_usd_value if stake_usd_value is not None else stake_tokens_display
            validator_total_rewards_usd += reward_usd_value if reward_usd_value is not None else reward_tokens_display
            
            # Calculate APR
            apr = _calculate_apr(total_rewards, avg_stake, range_type)
            
            # Check if jailed (from latest snapshot or metadata)
            jailed = _is_validator_jailed(node)
            if jailed:
                validator_jailed_count += 1
            
            # Get uptime from uptime history API
            host_id = node_repo.get_vizion_host_id(node, host_id_mapping)
            uptime_pct = 0.0
            if host_id:
                uptime_data = validator_uptime_data_batch.get(host_id, {})
                uptime_pct = uptime_data.get('uptime_pct', 0.0)
            
            uptime_health = _classify_uptime_health(uptime_pct)
            if uptime_health == 'healthy':
                validator_healthy_count += 1
            elif uptime_health == 'critical':
                validator_critical_count += 1

            # Calculate slashing events from performance snapshot data
            slashing_events = 0
            perf_snaps = perf_snapshots_by_node.get(node.id, [])
            if perf_snaps:
                sorted_perf = sorted(perf_snaps, key=lambda s: s.snapshot_date)
                total_missed = 0
                total_signed = 0
                for snap in sorted_perf:
                    missed = getattr(snap, 'missed_counter', 0) or 0
                    signed = getattr(snap, 'signed_blocks', 0) or 0
                    total_missed += missed
                    total_signed += signed
                
                # Calculate miss rate and determine slashing indicator
                total_blocks = total_signed + total_missed
                if total_blocks > 0:
                    miss_rate = total_missed / total_blocks
                    # If miss rate > 10%, mark as slashing event
                    if miss_rate > 0.1:
                        slashing_events = 1
            
            val_status = _status_from_uptime(uptime_pct)
            
            validator_highlights.append(models.ValidatorHighlight(
                validatorId=node.node_identifier or str(node.id),
                validatorName=node.node_name or "Unknown Validator",
                stake=stake_usd_value if stake_usd_value is not None else stake_tokens_display,
                rewards=reward_usd_value if reward_usd_value is not None else reward_tokens_display,
                apr=apr,
                uptimePct=uptime_pct,
                jailed=jailed,
                status=val_status
            ))
            
            validator_total_stake += avg_stake
            validator_total_rewards += total_rewards
            validator_apr_sum += apr
            validator_uptime_sum += uptime_pct
        except Exception as e:
            _logger.exception(f"Error processing validator node {node.id}: {e}")
    
    # Calculate validator averages
    validator_count = len(validator_nodes)
    # CRITICAL FIX: Calculate APR from aggregated stake and rewards, not average of individual APRs
    # Formula: APR = (total_rewards / average_total_stake) * (365 / period_days) * 100
    validator_avg_apr = _calculate_apr(validator_total_rewards, (validator_total_stake / validator_count if validator_count > 0 else 0.0), range_type) if validator_count > 0 else 0.0
    validator_avg_uptime = validator_uptime_sum / validator_count if validator_count > 0 else 0.0
    
    # Accumulate previous period totals
    prev_validator_total_rewards = 0.0
    prev_validator_total_stake = 0.0
    prev_validator_uptime_sum = 0.0
    prev_validator_total_rewards_usd = 0.0
    prev_validator_total_stake_usd = 0.0
    for node in validator_nodes:
        prev_node_snapshots = prev_snapshots_by_node.get(node.id, [])
        prev_avg_stake = 0.0
        prev_total_rewards = 0.0
        if prev_node_snapshots:
            prev_avg_stake = aggregation.aggregate_series(
                [{'value': s.total_stake} for s in prev_node_snapshots],
                'value',
                aggregation.AggregationType.AVG
            )
            sorted_prev = sorted(prev_node_snapshots, key=lambda s: s.snapshot_date)
            if len(sorted_prev) == 1:
                prev_total_rewards = sorted_prev[0].total_rewards
            else:
                prev_total_rewards = max(0, sorted_prev[-1].total_rewards - sorted_prev[0].total_rewards)
        
        prev_apr = _calculate_apr(prev_total_rewards, prev_avg_stake, range_type)
        # Accumulate previous period totals
        prev_validator_total_rewards += prev_total_rewards
        prev_validator_total_stake += prev_avg_stake
        protocol_meta = validator_protocol_meta.get(node.id)
        prev_stake_tokens_display, prev_stake_usd = _convert_amount_with_metadata(
            prev_avg_stake,
            protocol_meta,
            'stake_decimals',
            missing_price_protocols
        )
        prev_reward_tokens_display, prev_reward_usd = _convert_amount_with_metadata(
            prev_total_rewards,
            protocol_meta,
            'reward_decimals',
            missing_price_protocols
        )
        prev_validator_total_stake_usd += prev_stake_usd if prev_stake_usd is not None else prev_stake_tokens_display
        prev_validator_total_rewards_usd += prev_reward_usd if prev_reward_usd is not None else prev_reward_tokens_display
        
        
        # Get previous period uptime
        prev_uptime_pct = 0.0
        host_id = node_repo.get_vizion_host_id(node, host_id_mapping)
        if host_id:
            prev_uptime_data = validator_prev_uptime_data_batch.get(host_id, {})
            prev_uptime_pct = prev_uptime_data.get('uptime_pct', 0.0)
        prev_validator_uptime_sum += prev_uptime_pct
        
    validator_summary = models.ValidatorSummary(
        totalValidators=validator_count,
        healthyNodes=validator_healthy_count,
        criticalNodes=validator_critical_count,
        totalStake=round(validator_total_stake_usd, 2),
        totalRewards=round(validator_total_rewards_usd, 2),
        avgAPR=round(validator_avg_apr, 2),
        avgUptimePct=round(validator_avg_uptime, 2),
        jailedCount=validator_jailed_count,
        prevTotalRewards=round(prev_validator_total_rewards_usd, 2) if prev_validator_total_rewards_usd > 0 else None,
        prevTotalStake=round(prev_validator_total_stake_usd, 2) if prev_validator_total_stake_usd > 0 else None
    )
    
    # Calculate overall metrics
    total_tracked_nodes = rpc_node_count + validator_count
    prev_total_tracked_nodes = rpc_node_count + validator_count
    overall_uptime = (
        (rpc_uptime_sum + validator_uptime_sum) / total_tracked_nodes
        if total_tracked_nodes > 0 else 0.0
    )
    prev_overall_uptime = (
        (prev_rpc_uptime_sum + prev_validator_uptime_sum) / prev_total_tracked_nodes
        if prev_total_tracked_nodes > 0 else 0.0
    )
    overall_status = _status_from_uptime(overall_uptime)
    
    rewards_delta = helpers.calculate_change(
        round(validator_total_rewards_usd, 2),
        round(prev_validator_total_rewards_usd, 2)
    )

    overview = models.AccountOverview(
        totalNodes=len(all_nodes),
        overallUptimePct=round(overall_uptime, 2),
        totalRequests=round(rpc_total_requests, 0),
        totalRewards=round(validator_total_rewards_usd, 2),
        rewardsDelta=rewards_delta,
        overallStatus=overall_status,
        prevOverallUptimePct=round(prev_overall_uptime, 2)
    )
    
    # Generate insights
    insights = _generate_account_insights(
        rpc_summary, validator_summary, rpc_highlights, validator_highlights
    )
    
    # Fetch incidents from Vizion trigger data using cached host ID mapping
    incidents = _fetch_incidents_from_vizion(
        all_nodes, vizion_client, node_repo, range_type, host_id_mapping
    )
    
    # Aggregate daily trends from RPC request counts and validator rewards
    try:
        _logger.info(f"Starting trends aggregation")
        
        # Fetch daily RPC request counts directly from Vision API
        daily_requests_raw = {}
        try:
            num_days = helpers.get_days_in_range(range_type)
            # Fetch returns tuple now: (daily_totals, per_host_daily)
            daily_requests_raw, _ = _fetch_daily_rpc_trends_from_vizion(env, rpc_host_ids, num_days, user_email)
            _logger.info(f"Fetched {len(daily_requests_raw)} days of daily RPC trends from Vision API")
        except Exception as e:
            _logger.warning(f"Error fetching daily RPC trends from Vision API: {e}")
            daily_requests_raw = {}
        
        # Aggregate daily RPC request counts (filter to period)
        daily_requests = _aggregate_daily_rpc_requests(
            daily_requests_raw,
            period_start,
            period_end
        )
        _logger.info(f"Daily requests aggregated: {daily_requests}")
        
        # CRITICAL FIX: Verify and correct rpc_total_requests using the daily trends data
        # The daily_requests is the authoritative source (directly from Vision API, 7 days only)
        trends_total_requests = sum(daily_requests.values())
        _logger.info(f"RPC totalRequests check: calculated_from_nodes={rpc_total_requests}, trends_total={trends_total_requests}")
        if abs(rpc_total_requests - trends_total_requests) > 1.0:
            _logger.warning(f"RPC totalRequests mismatch: difference={abs(rpc_total_requests - trends_total_requests)} ({abs(rpc_total_requests - trends_total_requests) / trends_total_requests * 100:.1f}%)")
            # Always use trends_total as it's the authoritative source
            rpc_total_requests = trends_total_requests
        
        # Update both rpc_summary and overview with the corrected value
        rpc_summary.totalRequests = round(rpc_total_requests, 0)
        overview.totalRequests = round(rpc_total_requests, 0)
        
        # Aggregate daily validator rewards
        daily_rewards = _aggregate_daily_validator_rewards(
            snapshot_repo,
            [n.id for n in validator_nodes],
            period_start,
            period_end,
            validator_protocol_meta,
            missing_price_protocols
        )
        _logger.info(f"Daily rewards aggregated: {daily_rewards}")
        
        # Build trends array
        trends = _build_daily_trends(daily_requests, daily_rewards, period_start, period_end)
        _logger.info(f"Trends built with {len(trends)} data points")
    except Exception as e:
        _logger.warning(f"Error building account trends: {e}", exc_info=True)
        trends = []

    if missing_price_protocols:
        _logger.warning(
            "USD conversion skipped for protocols without pricing metadata: %s",
            ", ".join(sorted(missing_price_protocols))
        )
    
    # Validate report before returning
    # CRITICAL FIX: Recalculate rpc_total_requests to match trends data
    # This ensures totalRequests equals sum of daily trend requestCounts
    if trends:
        trends_total = sum(t.requestCount or 0 for t in trends)
        _logger.info(f"Trends array sum: {trends_total}, current rpc_total_requests: {rpc_total_requests}")
        if trends_total > 0 and abs(rpc_total_requests - trends_total) > 1.0:
            _logger.warning(f"RPC totalRequests mismatch: trends_total={trends_total}, current={rpc_total_requests}")
            # Update to match trends (which is authoritative)
            rpc_total_requests = trends_total
            rpc_summary.totalRequests = round(rpc_total_requests, 0)
            overview.totalRequests = round(rpc_total_requests, 0)
    
    # Validate report before returning
    _validate_and_log_warnings(models.AccountWeeklyReport(
        meta=meta,
        overview=overview,
        rpcSummary=rpc_summary,
        validatorSummary=validator_summary,
        rpcHighlights=rpc_highlights,
        validatorHighlights=validator_highlights,
        incidents=incidents,
        insights=insights,
        trends=trends
    ))
    
    return models.AccountWeeklyReport(
        meta=meta,
        overview=overview,
        rpcSummary=rpc_summary,
        validatorSummary=validator_summary,
        rpcHighlights=rpc_highlights,
        validatorHighlights=validator_highlights,
        incidents=incidents,
        insights=insights,
        trends=trends
    )


def get_rpc_fleet_report(
    env,
    account_id: int,
    range_type: str = 'weekly',
    timezone_str: str = 'UTC'
) -> models.RpcFleetReport:
    """
    Generate RPC fleet report.
    
    Args:
        env: Odoo environment
        account_id: User/account ID
        range_type: 'weekly' or 'monthly'
        timezone_str: User's timezone
    
    Returns:
        RpcFleetReport data object
    """

    
    # Calculate period bounds
    period_start, period_end, prev_start, prev_end = helpers.calculate_period_bounds(
        range_type, timezone_str
    )
    
    # Get user email for Vizion authentication
    user = env['res.users'].sudo().search([('id', '=', account_id)], limit=1)
    user_email = user.login if user else None
    
    # Initialize repositories
    node_repo = clients.NodeRepository(env)
    vizion_client = clients.VizionClient(env, user_email=user_email)
    
    # Fetch RPC nodes
    rpc_nodes = node_repo.get_nodes_by_account(account_id, node_type='rpc')
    
    # Get account name
    account_name = "Unknown Account"
    if rpc_nodes:
        subscription = rpc_nodes[0].subscription_id
        if subscription and subscription.customer_name:
            account_name = subscription.customer_name.name or "Unknown"
    
    # Build meta
    meta = models.ReportMeta(
        accountId=str(account_id),
        accountName=account_name,
        periodStart=helpers.format_date_for_response(period_start),
        periodEnd=helpers.format_date_for_response(period_end),
        range=range_type,
        timezone=timezone_str
    )
    
    # Build host ID mapping from login response to avoid repeated API calls
    host_id_mapping = {}
    if rpc_nodes and user_email:
        try:
            login_response = oauth_utils.login_with_email(user_email)
            if login_response and login_response.get('success'):
                host_data_list = login_response.get('hostData', [])
                host_id_mapping = node_repo.build_host_id_mapping(host_data_list)
        except Exception as e:
            pass
    
    # Collect all host IDs for batch fetching
    rpc_host_ids = []
    rpc_host_id_to_node = {}
    for node in rpc_nodes:
        host_id = node_repo.get_vizion_host_id(node, host_id_mapping)
        if host_id:
            rpc_host_ids.append(host_id)
            rpc_host_id_to_node[host_id] = node
    
    # Batch fetch protocol data and uptime history for all nodes
    current_time = datetime.now(pytz.UTC).isoformat()
    rpc_protocol_data_batch = {}
    rpc_uptime_data_batch = {}  # Uptime available for all node types (RPC, validator, archive)
    rpc_method_count_data = {}
    per_host_rpc_requests = {}  # Initialize before if block
    prev_total_requests_fleet = 0.0
    
    if rpc_host_ids:
        # Define protocol_name before use (default to 'ethereum' if no nodes)
        protocol_name = 'ethereum'
        if rpc_nodes:
            protocol = rpc_nodes[0].subscription_id.protocol_id
            protocol_name = protocol.name.lower().replace(' ', '') if protocol else 'ethereum'
        
        # Fetch uptime history (available for all node types)
        rpc_uptime_data_batch = vizion_client.fetch_uptime_history(rpc_host_ids, protocol_name, current_time, range_type) or {}
        
        rpc_protocol_data_batch = vizion_client.fetch_protocol_data(rpc_host_ids, current_time, range_type, protocol_name) or {}
        
        # Fetch daily RPC request trends directly from Vision API
        # This provides daily granularity needed for trends calculation
        # Fetch bulk data for both current and previous periods (consistent with account-weekly)
        daily_rpc_requests = {}
        try:
            num_days = helpers.get_days_in_range(range_type)
            bulk_num_days = num_days * 2  # Fetch 2x for current + previous period (consistent with account-weekly)
            # Fetch returns tuple now: (daily_totals, per_host_daily)
            daily_rpc_requests, per_host_daily = _fetch_daily_rpc_trends_from_vizion(env, rpc_host_ids, bulk_num_days, user_email)
            _logger.info(f"Fetched daily RPC trends: {len(daily_rpc_requests)} days with data, {len(per_host_daily)} hosts")
            
            # Build per-host aggregates for the reporting period
            for host_id, host_daily_data in per_host_daily.items():
                host_total = 0
                for date_str, request_count in host_daily_data.items():
                    try:
                        date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                        date_obj = pytz.UTC.localize(date_obj)
                        if period_start <= date_obj <= period_end:
                            host_total += request_count
                    except Exception as e:
                        _logger.debug(f"Error processing host {host_id} date {date_str}: {e}")
                        continue
                if host_total > 0:
                    per_host_rpc_requests[host_id] = host_total
        except Exception as e:
            _logger.warning(f"Error fetching daily RPC trends from Vision API: {e}")
            daily_rpc_requests = {}
            per_host_rpc_requests = {}
        
        # Filter to reporting period and aggregate
        rpc_method_count_data = _aggregate_daily_rpc_requests(
            daily_rpc_requests,
            period_start,
            period_end
        )
        prev_rpc_request_data = _aggregate_daily_rpc_requests(
            daily_rpc_requests,
            prev_start,
            prev_end
        )
        prev_total_requests_fleet = sum(prev_rpc_request_data.values()) if prev_rpc_request_data else 0.0
        _logger.info(f"Aggregated RPC requests to {len(rpc_method_count_data)} days in reporting period, {len(per_host_rpc_requests)} hosts with data")
    
    # Fetch previous period RPC data for traffic comparison in fleet report
    rpc_prev_uptime_data_batch_fleet = {}
    rpc_prev_protocol_data_batch_fleet = {}
    if rpc_host_ids and vizion_client:
        try:
            protocol = rpc_nodes[0].subscription_id.protocol_id
            protocol_name = protocol.name.lower().replace(' ', '') if protocol else 'ethereum'
            prev_time = prev_end.isoformat()
            
            rpc_prev_uptime_data_batch_fleet = vizion_client.fetch_uptime_history(
                rpc_host_ids, protocol_name, prev_time, range_type
            ) or {}
            rpc_prev_protocol_data_batch_fleet = vizion_client.fetch_protocol_data(
                rpc_host_ids, prev_time, range_type, protocol_name
            ) or {}
        except Exception as e:
            _logger.warning(f"Error fetching previous period RPC data for fleet: {e}")
    
    # Process each RPC node
    node_items = []
    total_requests = 0.0
    total_errors = 0.0
    uptime_sum = 0.0
    latency_sum = 0.0
    healthy_count = 0
    warning_count = 0
    critical_count = 0
    
    for node in rpc_nodes:
        try:
            host_id = node_repo.get_vizion_host_id(node, host_id_mapping)
            
            # If no host_id, show node with default values instead of skipping
            if not host_id:
                node_data = {
                    'uptimePct': 0.0,
                    'latencyMs': 0.0,
                    'requestCount': 0.0,
                    'errorCount': 0.0,
                    'errorRatePct': 0.0
                }
            else:
                # Get metrics from batch responses
                protocol_metrics = _parse_rpc_protocol_data(rpc_protocol_data_batch.get(host_id, {}))
                uptime_data = rpc_uptime_data_batch.get(host_id, {})
                uptime_pct = uptime_data.get('uptime_pct', 0.0)
                
                # Get request count from per-host data (RPC nodes only)
                # Priority: 1) Per-host aggregated data 2) Node name lookup
                request_count = 0.0
                if host_id in per_host_rpc_requests:
                    request_count = per_host_rpc_requests[host_id]
                elif node.node_name in rpc_method_count_data:
                    request_count = rpc_method_count_data[node.node_name]
                
                # Calculate error rate with request count
                error_rate_pct = (protocol_metrics['errorCount'] / request_count * 100) if request_count > 0 else 0.0
                
                # Build node data dict with uptime from history API
                node_data = {
                    'uptimePct': uptime_pct,
                    'latencyMs': protocol_metrics['latencyMs'],
                    'requestCount': request_count,
                    'errorCount': protocol_metrics['errorCount'],
                    'errorRatePct': error_rate_pct
                }
            
            node_status = _status_from_uptime(node_data['uptimePct'])
            
            uptime_value = node_data['uptimePct']
            uptime_health = _classify_uptime_health(uptime_value)
            if uptime_health == 'healthy':
                healthy_count += 1
            elif uptime_health == 'critical':
                critical_count += 1
            else:
                warning_count += 1

            node_items.append(models.RpcNodeItem(
                nodeId=node.node_identifier or str(node.id),
                nodeName=node.node_name or "Unknown Node",
                status=node_status,
                uptimePct=node_data['uptimePct'],
                latencyMs=node_data['latencyMs'],
                requestCount=node_data['requestCount'],
                errorCount=node_data['errorCount'],
                errorRatePct=node_data['errorRatePct']
            ))
            
            total_requests += node_data['requestCount']
            total_errors += node_data['errorCount']
            uptime_sum += node_data['uptimePct']
            latency_sum += node_data['latencyMs']
        except Exception as e:
            _logger.exception(f"Error processing RPC node {node.id}: {e}")
    
    # Calculate fleet averages
    node_count = len(rpc_nodes)
    avg_uptime = uptime_sum / node_count if node_count > 0 else 0.0
    avg_latency = latency_sum / node_count if node_count > 0 else 0.0
    error_rate_pct = (total_errors / total_requests * 100) if total_requests > 0 else 0.0
    
    requests_delta_pct = helpers.calculate_change_percent(total_requests, prev_total_requests_fleet)
    fleet_status = _status_from_uptime(avg_uptime)
    
    summary = models.RpcFleetSummary(
        totalNodes=node_count,
        healthyNodes=healthy_count,
        warningNodes=warning_count,
        criticalNodes=critical_count,
        avgUptimePct=round(avg_uptime, 2),
        avgLatencyMs=round(avg_latency, 2),
        totalRequests=round(total_requests, 0),
        totalErrors=round(total_errors, 0),
        errorRatePct=round(error_rate_pct, 2),
        requestsDeltaPct=requests_delta_pct,
        status=fleet_status
    )
    
    health_mix = models.HealthMix(
        good=healthy_count,
        warning=warning_count,
        critical=critical_count
    )
    
    # Generate insights
    insights = _generate_rpc_fleet_insights(summary, node_items)
    
    # Fetch incidents from Vizion trigger data using cached host ID mapping
    incidents = _fetch_incidents_from_vizion(
        rpc_nodes, vizion_client, node_repo, range_type, host_id_mapping
    )
    
    # Build trends with daily request counts (usage trend)
    trends = []
    try:
        if rpc_host_ids:
            # Fetch daily RPC request trends from Vision API
            num_days = helpers.get_days_in_range(range_type)
            daily_requests_raw, _ = _fetch_daily_rpc_trends_from_vizion(env, rpc_host_ids, num_days, user_email)
            
            # Filter to reporting period
            daily_requests = _aggregate_daily_rpc_requests(
                daily_requests_raw,
                period_start,
                period_end
            )
            
            # Build trends array with request counts
            current_date = period_start.replace(hour=0, minute=0, second=0, microsecond=0)
            while current_date <= period_end:
                date_key = current_date.strftime('%Y-%m-%d')
                requests = daily_requests.get(date_key, 0.0)
                
                if requests > 0:
                    trend_point = models.TrendDataPoint(
                        date=date_key,
                        requestCount=requests
                    )
                    trends.append(trend_point)
                
                current_date += timedelta(days=1)
            
            _logger.info(f"Built RPC fleet trends with {len(trends)} data points")
    except Exception as e:
        _logger.warning(f"Error building RPC fleet trends: {e}", exc_info=True)
        trends = []
    
    return models.RpcFleetReport(
        meta=meta,
        summary=summary,
        nodes=node_items,
        healthMix=health_mix,
        incidents=incidents,
        insights=insights,
        trends=trends
    )


def get_rpc_node_report(
    env,
    node_id: str,
    range_type: str = 'weekly',
    timezone_str: str = 'UTC'
) -> models.RpcNodeDetailReport:
    """
    Generate RPC node detail report.
    
    Args:
        env: Odoo environment
        node_id: Node identifier (UUID or database ID)
        range_type: 'weekly' or 'monthly'
        timezone_str: User's timezone
    
    Returns:
        RpcNodeDetailReport data object
    """

    
    # Calculate period bounds
    period_start, period_end, prev_start, prev_end = helpers.calculate_period_bounds(
        range_type, timezone_str
    )
    
    # Initialize repositories
    node_repo = clients.NodeRepository(env)
    
    # Get node first to determine user email from subscription
    node = node_repo.get_node_by_id(node_id)
    host_id = node_repo.get_vizion_host_id(node)
    if not node:
        raise ValueError(f"Node not found: {node_id}")
    
    # Get user email from subscription's customer for Vizion authentication
    user_email = None
    if node.subscription_id and node.subscription_id.customer_name:
        user_email = node.subscription_id.customer_name.email
    
    vizion_client = clients.VizionClient(env, user_email=user_email)
    rpc_repo = clients.RpcDataRepository(env)
    
    # Get account info from subscription's customer
    subscription = node.subscription_id
    account_id = str(subscription.customer_name.id) if subscription and subscription.customer_name else "Unknown"
    account_name = subscription.customer_name.name or "Unknown" if subscription and subscription.customer_name else "Unknown"
    protocol = subscription.protocol_id if subscription else None

    price_service = TokenPriceService(env)
    protocol_metadata = _build_protocol_metadata(
        [protocol] if protocol else [],
        price_service
    )
    node_protocol_meta = protocol_metadata.get(protocol.id) if protocol else None
    detail_missing_price_protocols: set = set()
    
    # Build meta
    meta = models.ReportMeta(
        accountId=account_id,
        accountName=account_name,
        periodStart=helpers.format_date_for_response(period_start),
        periodEnd=helpers.format_date_for_response(period_end),
        range=range_type,
        timezone=timezone_str,
        nodeId=node.node_identifier or str(node.id),
        nodeName=node.node_name or "Unknown Node"
    )
    
    # Build host ID mapping from login response to avoid repeated API calls
    host_id_mapping = {}
    if user_email:
        try:
            login_response = oauth_utils.login_with_email(user_email)
            if login_response and login_response.get('success'):
                host_data_list = login_response.get('hostData', [])
                host_id_mapping = node_repo.build_host_id_mapping(host_data_list)
        except Exception as e:
            _logger.warning(f"Error building host ID mapping: {e}")
    
    # Fetch node data
    host_id = node_repo.get_vizion_host_id(node, host_id_mapping)
    node_data = _fetch_rpc_node_data(node, vizion_client, range_type, host_id_mapping)
    
    # Fetch previous period uptime for comparison
    prev_uptime_pct = 0.0
    prev_protocol_metrics = {'latencyMs': 0.0, 'errorCount': 0.0}
    if host_id:
        try:
            protocol = node.subscription_id.protocol_id if node.subscription_id else None
            protocol_name = protocol.name.lower().replace(' ', '') if protocol else 'ethereum'
            prev_time = prev_end.isoformat()
            
            prev_uptime_data_batch = vizion_client.fetch_uptime_history(
                [host_id], protocol_name, prev_time, range_type
            )
            prev_uptime_data = prev_uptime_data_batch.get(host_id, {}) if prev_uptime_data_batch else {}
            prev_uptime_pct = prev_uptime_data.get('uptime_pct', 0.0)
            
            prev_protocol_data_batch = vizion_client.fetch_protocol_data(
                [host_id], prev_time, range_type, protocol_name
            )
            prev_protocol_metrics = _parse_rpc_protocol_data(prev_protocol_data_batch.get(host_id, {})) if prev_protocol_data_batch else {}
        except Exception as e:
            _logger.warning(f"Error fetching previous period RPC data: {e}")
    
    uptime_change_percent = helpers.calculate_change_percent(node_data['uptimePct'], prev_uptime_pct)
    
    node_status = _status_from_uptime(node_data['uptimePct'])
    
    overview = models.RpcNodeOverview(
        status=node_status
    )
    
    # Build metrics
    metrics = models.RpcNodeMetrics(
        uptimePct=node_data['uptimePct'],
        uptimeChangePercent=uptime_change_percent,
        latencyMs=node_data['latencyMs'],
        latencyChangePercent=0.0,
        requestCount=node_data['requestCount'],
        requestChangePercent=0.0,
        errorCount=node_data['errorCount'],
        errorRatePct=node_data['errorRatePct'],
        errorChangePercent=0.0
    )
    
    # Build security info from node creation date using periodic intervals
    security_info = {}
    try:
        if node.create_date:
            create_date = node.create_date
            if create_date.tzinfo is None:
                create_date = pytz.UTC.localize(create_date)
            interval_dates = helpers.get_last_interval_date(create_date)
            last_security_check_date = interval_dates['weekly'].isoformat()
            
            security_info = {
                'ddosProtection': True,
                'firewallEnabled': True,
                'lastSecurityCheck': last_security_check_date
            }
    except Exception as e:
        _logger.warning(f"Error building security info from node creation date: {e}")
        security_info = {
            'ddosProtection': True,
            'firewallEnabled': True,
            'lastSecurityCheck': None
        }
    
    security = models.SecurityInfo(
        ddosProtection=security_info.get('ddosProtection', True),
        firewallEnabled=security_info.get('firewallEnabled', True),
        lastSecurityCheck=security_info.get('lastSecurityCheck')
    )
    
    # Fetch method breakdown
    method_breakdown = _fetch_method_breakdown(node, vizion_client, rpc_repo, range_type)
    
    # Calculate benchmarks (vs user's own nodes)
    benchmarks = models.BenchmarksInfo(
        uptimeVsNetwork=0.0,  # TODO: calculate vs network average
        latencyVsNetwork=0.0,
        reliabilityVsNetwork=0.0
    )
    
    # Generate insights
    insights = _generate_rpc_node_insights(node_data, security)
    
    # Fetch incidents from Vizion trigger data for this node using cached host ID mapping
    incidents = _fetch_incidents_from_vizion(
        [node], vizion_client, node_repo, range_type, host_id_mapping
    )
    
    # Build trends with daily uptime percentages
    trends = []
    try:
        if host_id:
            # Fetch uptime history for the node
            protocol = node.subscription_id.protocol_id if node.subscription_id else None
            protocol_name = protocol.name.lower().replace(' ', '') if protocol else 'ethereum'
            current_time = datetime.now(pytz.UTC).isoformat()
            
            uptime_data_batch = vizion_client.fetch_uptime_history(
                [host_id], protocol_name, current_time, range_type
            )
            
            uptime_data = uptime_data_batch.get(host_id, {})
            uptime_data_points = uptime_data.get('data_points', {})
            
            # Debug: log structure of uptime data
            _logger.info(f"Uptime data structure: keys={list(uptime_data_points.keys())}")
            for port_name, points in uptime_data_points.items():
                if isinstance(points, list) and len(points) > 0:
                    _logger.info(f"Port {port_name}: {len(points)} points, sample: {points[0] if points else 'none'}")
            
            # Aggregate uptime data by day
            daily_uptime = _aggregate_daily_uptime_from_history(
                uptime_data_points,
                period_start,
                period_end
            )
            
            # Build trends array with uptime percentages
            current_date = period_start.replace(hour=0, minute=0, second=0, microsecond=0)
            while current_date <= period_end:
                date_key = current_date.strftime('%Y-%m-%d')
                uptime_pct = daily_uptime.get(date_key, 0.0)
                
                if uptime_pct > 0 or date_key in daily_uptime:
                    trend_point = models.TrendDataPoint(
                        date=date_key,
                        uptimePct=uptime_pct
                    )
                    trends.append(trend_point)
                
                current_date += timedelta(days=1)
            
            _logger.info(f"Built RPC node trends with {len(trends)} data points")
    except Exception as e:
        _logger.warning(f"Error building RPC node trends: {e}", exc_info=True)
        trends = []
    
    return models.RpcNodeDetailReport(
        meta=meta,
        overview=overview,
        metrics=metrics,
        security=security,
        methodBreakdown=method_breakdown,
        benchmarks=benchmarks,
        incidents=incidents,
        insights=insights,
        trends=trends
    )


def get_validator_fleet_report(
    env,
    account_id: int,
    range_type: str = 'weekly',
    timezone_str: str = 'UTC'
) -> models.ValidatorFleetReport:
    """
    Generate validator fleet report.
    
    Args:
        env: Odoo environment
        account_id: User/account ID
        range_type: 'weekly' or 'monthly'
        timezone_str: User's timezone
    
    Returns:
        ValidatorFleetReport data object
    """

    
    # Calculate period bounds
    period_start, period_end, prev_start, prev_end = helpers.calculate_period_bounds(
        range_type, timezone_str
    )
    
    # Get user email for Vizion authentication
    user = env['res.users'].sudo().search([('id', '=', account_id)], limit=1)
    user_email = user.login if user else None
    
    # Initialize repositories and clients
    node_repo = clients.NodeRepository(env)
    snapshot_repo = clients.SnapshotRepository(env)
    rpc_repo = clients.RpcDataRepository(env)
    
    # Try to initialize Vizion client (may fail if no auth available)
    try:
        vizion_client = clients.VizionClient(env, user_email=user_email)
    except Exception as e:
        _logger.warning(f"Could not initialize Vizion client: {e}")
        vizion_client = None
    
    # Fetch validator nodes
    validator_nodes = node_repo.get_nodes_by_account(account_id, node_type='validator')
    price_service = TokenPriceService(env)
    protocol_metadata = _build_protocol_metadata(
        _collect_protocols_from_nodes(validator_nodes),
        price_service
    )
    validator_protocol_meta = _map_nodes_to_protocol_meta(validator_nodes, protocol_metadata)
    validator_missing_price_protocols: set = set()
    
    # Get account name
    account_name = "Unknown Account"
    if validator_nodes:
        subscription = validator_nodes[0].subscription_id
        if subscription and subscription.customer_name:
            account_name = subscription.customer_name.name or "Unknown"
    
    # Build host ID mapping from login response to avoid repeated API calls
    host_id_mapping = {}
    if validator_nodes and user_email:
        try:
            login_response = oauth_utils.login_with_email(user_email)
            if login_response and login_response.get('success'):
                host_data_list = login_response.get('hostData', [])
                host_id_mapping = node_repo.build_host_id_mapping(host_data_list)
        except Exception as e:
            pass
    
    # Build meta
    meta = models.ReportMeta(
        accountId=str(account_id),
        accountName=account_name,
        periodStart=helpers.format_date_for_response(period_start),
        periodEnd=helpers.format_date_for_response(period_end),
        range=range_type,
        timezone=timezone_str
    )
    
    # Fetch validator snapshots
    validator_node_ids = [n.id for n in validator_nodes]
    reward_snapshots = snapshot_repo.get_validator_reward_snapshots(
        validator_node_ids, period_start, period_end
    )
    
    # Group snapshots by node
    snapshots_by_node = {}
    for snapshot in reward_snapshots:
        node_id = snapshot.node_id.id
        if node_id not in snapshots_by_node:
            snapshots_by_node[node_id] = []
        snapshots_by_node[node_id].append(snapshot)
    
    # Fetch performance snapshots (contains missed_counter, signed_blocks data)
    performance_snapshots = snapshot_repo.get_validator_performance_snapshots(
        validator_node_ids, period_start, period_end
    )
    
    # Group performance snapshots by node
    perf_snapshots_by_node = {}
    for snapshot in performance_snapshots:
        node_id = snapshot.node_id.id
        if node_id not in perf_snapshots_by_node:
            perf_snapshots_by_node[node_id] = []
        perf_snapshots_by_node[node_id].append(snapshot)
    
    
    # Collect all validator host IDs for batch uptime history fetching
    validator_host_ids = []
    validator_host_id_to_node = {}
    for node in validator_nodes:
        host_id = node_repo.get_vizion_host_id(node, host_id_mapping)
        if host_id:
            validator_host_ids.append(host_id)
            validator_host_id_to_node[host_id] = node
    
    # Batch fetch uptime history for all validators (uptime available for all node types)
    validator_uptime_data_batch = {}
    current_time = datetime.now(pytz.UTC).isoformat()
    if validator_host_ids and vizion_client:
        try:
            protocol = validator_nodes[0].subscription_id.protocol_id
            protocol_name = protocol.name.lower().replace(' ', '') if protocol else 'ethereum'
            validator_uptime_data_batch = vizion_client.fetch_uptime_history(
                validator_host_ids, protocol_name, current_time, range_type
            ) or {}
        except Exception as e:
            _logger.warning(f"Error fetching validator uptime history: {e}")
    
    # Fetch previous period uptime data for validators
    validator_prev_uptime_data_batch_fleet = {}
    if validator_host_ids and vizion_client:
        try:
            protocol = validator_nodes[0].subscription_id.protocol_id
            protocol_name = protocol.name.lower().replace(' ', '') if protocol else 'ethereum'
            prev_time = prev_end.isoformat()
            validator_prev_uptime_data_batch_fleet = vizion_client.fetch_uptime_history(
                validator_host_ids, protocol_name, prev_time, range_type
            ) or {}
        except Exception as e:
            _logger.warning(f"Error fetching previous period validator uptime for fleet: {e}")
    
    # Fetch previous period snapshots for validators
    prev_reward_snapshots_fleet = snapshot_repo.get_validator_reward_snapshots(
        validator_node_ids, prev_start, prev_end
    )
    prev_snapshots_by_node_fleet = {}
    for snapshot in prev_reward_snapshots_fleet:
        node_id = snapshot.node_id.id
        if node_id not in prev_snapshots_by_node_fleet:
            prev_snapshots_by_node_fleet[node_id] = []
        prev_snapshots_by_node_fleet[node_id].append(snapshot)
    
    prev_performance_snapshots_fleet = snapshot_repo.get_validator_performance_snapshots(
        validator_node_ids, prev_start, prev_end
    )
    prev_perf_snapshots_by_node_fleet = {}
    for snapshot in prev_performance_snapshots_fleet:
        node_id = snapshot.node_id.id
        if node_id not in prev_perf_snapshots_by_node_fleet:
            prev_perf_snapshots_by_node_fleet[node_id] = []
        prev_perf_snapshots_by_node_fleet[node_id].append(snapshot)
    
    # Process each validator
    validator_items = []
    total_stake = 0.0
    total_rewards = 0.0
    total_stake_usd = 0.0
    total_rewards_usd = 0.0
    uptime_sum = 0.0
    gini_token_stakes: List[float] = []
    active_count = 0
    jailed_count = 0
    total_slashing_events = 0
    healthy_count = 0
    warning_count = 0
    critical_count = 0
    
    for node in validator_nodes:
        try:
            node_snapshots = snapshots_by_node.get(node.id, [])
            
            # Aggregate stake and rewards (use defaults if no snapshots)
            if node_snapshots:
                avg_stake = aggregation.aggregate_series(
                    [{'value': s.total_stake} for s in node_snapshots],
                    'value',
                    aggregation.AggregationType.AVG
                )
                # Calculate rewards earned as delta: last cumulative - first cumulative
                sorted_snapshots = sorted(node_snapshots, key=lambda s: s.snapshot_date)
                if len(sorted_snapshots) == 1:
                    sum_rewards = sorted_snapshots[0].total_rewards
                else:
                    sum_rewards = max(0, sorted_snapshots[-1].total_rewards - sorted_snapshots[0].total_rewards)
            else:
                _logger.warning(f"No snapshots found for validator node {node.id}, using zero defaults")
                avg_stake = 0.0
                sum_rewards = 0.0
            
            protocol_meta = validator_protocol_meta.get(node.id)
            stake_tokens_display, stake_usd_value = _convert_amount_with_metadata(
                avg_stake,
                protocol_meta,
                'stake_decimals',
                validator_missing_price_protocols
            )
            reward_tokens_display, reward_usd_value = _convert_amount_with_metadata(
                sum_rewards,
                protocol_meta,
                'reward_decimals',
                validator_missing_price_protocols
            )
            total_stake_usd += stake_usd_value if stake_usd_value is not None else stake_tokens_display
            total_rewards_usd += reward_usd_value if reward_usd_value is not None else reward_tokens_display
            gini_token_stakes.append(stake_tokens_display)

            # Calculate APR
            apr = _calculate_apr(sum_rewards, avg_stake, range_type)
            
            # Check jailed status
            jailed = _is_validator_jailed(node)
            if jailed:
                jailed_count += 1
            else:
                active_count += 1
            
            # Calculate slashing events from performance snapshot data
            slashing_events = 0
            perf_snaps = perf_snapshots_by_node.get(node.id, [])
            if perf_snaps:
                sorted_perf = sorted(perf_snaps, key=lambda s: s.snapshot_date)
                total_missed = 0
                total_signed = 0
                for snap in sorted_perf:
                    missed = getattr(snap, 'missed_counter', 0) or 0
                    signed = getattr(snap, 'signed_blocks', 0) or 0
                    total_missed += missed
                    total_signed += signed
                
                # Calculate miss rate and determine slashing indicator
                total_blocks = total_signed + total_missed
                if total_blocks > 0:
                    miss_rate = total_missed / total_blocks
                    # If miss rate > 10%, mark as slashing event
                    if miss_rate > 0.1:
                        slashing_events = 1
            total_slashing_events += slashing_events
            
            # Get uptime from history API
            host_id = node_repo.get_vizion_host_id(node, host_id_mapping)
            uptime_pct = 0.0
            if host_id:
                uptime_data = validator_uptime_data_batch.get(host_id, {})
                uptime_pct = uptime_data.get('uptime_pct', 0.0)
            
            val_status = _status_from_uptime(uptime_pct)
            
            uptime_health = _classify_uptime_health(uptime_pct)
            if uptime_health == 'healthy':
                healthy_count += 1
            elif uptime_health == 'critical':
                critical_count += 1
            else:
                warning_count += 1

            validator_items.append(models.ValidatorNodeItem(
                validatorId=node.node_identifier or str(node.id),
                validatorName=node.node_name or "Unknown Validator",
                status=val_status,
                stake=stake_usd_value if stake_usd_value is not None else stake_tokens_display,
                rewards=reward_usd_value if reward_usd_value is not None else reward_tokens_display,
                apr=apr,
                uptimePct=uptime_pct,
                jailed=jailed,
                slashingEvents=slashing_events,
            ))
            
            total_stake += avg_stake
            total_rewards += sum_rewards
            uptime_sum += uptime_pct
        except Exception as e:
            _logger.exception(f"Error processing validator node {node.id}: {e}")
    
    # Calculate fleet averages
    validator_count = len(validator_nodes)
    avg_apr = _calculate_apr(
        total_rewards,
        (total_stake / validator_count if validator_count > 0 else 0.0),
        range_type
    ) if validator_count > 0 else 0.0
    avg_uptime = uptime_sum / validator_count if validator_count > 0 else 0.0

    fleet_status = _status_from_uptime(avg_uptime)
    
    summary = models.ValidatorFleetSummary(
        totalValidators=validator_count,
        activeValidators=active_count,
        healthyNodes=healthy_count,
        warningNodes=warning_count,
        criticalNodes=critical_count,
        jailedValidators=jailed_count,
        totalStake=round(total_stake_usd, 2),
        totalRewards=round(total_rewards_usd, 2),
        avgAPR=round(avg_apr, 2),
        status=fleet_status
    )
    
    health_mix = models.HealthMix(
        good=healthy_count,
        warning=warning_count,
        critical=critical_count
    )
    
    # Calculate risk indicators
    # Calculate stake concentration using Gini coefficient
    validator_stakes_tokens = gini_token_stakes or [helpers.safe_float(v.stake, 0.0) for v in validator_items]
    gini_coefficient = helpers.calculate_gini_coefficient(validator_stakes_tokens)
    stake_concentration = helpers.gini_to_concentration_level(gini_coefficient)
    
    risk_indicators = models.RiskIndicators(
        slashingRisk=scoring.determine_risk_level(total_slashing_events, 3, 5),
        jailingRisk=scoring.determine_risk_level(jailed_count, 1, 2),
        stakeConcentration=stake_concentration
    )
    
    # Generate insights
    insights = _generate_validator_fleet_insights(summary, validator_items, risk_indicators)
    
    # Fetch incidents from Vizion trigger data using cached host ID mapping (if available for validators)
    incidents = _fetch_incidents_from_vizion(
        validator_nodes, vizion_client, node_repo, range_type, host_id_mapping
    ) if vizion_client is not None else []
    
    # Build trends from daily validator rewards
    daily_rewards = _aggregate_daily_validator_rewards(
        snapshot_repo,
        validator_node_ids,
        period_start,
        period_end,
        validator_protocol_meta,
        validator_missing_price_protocols
    )
    trends = _build_daily_trends(
        daily_requests={}, daily_rewards=daily_rewards,
        period_start=period_start, period_end=period_end
    )
    
    if validator_missing_price_protocols:
        _logger.warning(
            "USD conversion skipped for protocols without pricing metadata: %s",
            ", ".join(sorted(validator_missing_price_protocols))
        )

    return models.ValidatorFleetReport(
        meta=meta,
        summary=summary,
        validators=validator_items,
        healthMix=health_mix,
        riskIndicators=risk_indicators,
        incidents=incidents,
        insights=insights,
        trends=trends
    )


def get_validator_node_report(
    env,
    validator_id: str,
    range_type: str = 'weekly',
    timezone_str: str = 'UTC'
) -> models.ValidatorNodeDetailReport:
    """
    Generate validator node detail report.
    
    Args:
        env: Odoo environment
        validator_id: Validator node identifier (UUID or database ID)
        range_type: 'weekly' or 'monthly'
        timezone_str: User's timezone
    
    Returns:
        ValidatorNodeDetailReport data object
    """

    
    # Calculate period bounds
    period_start, period_end, prev_start, prev_end = helpers.calculate_period_bounds(
        range_type, timezone_str
    )
    
    # Initialize repositories
    node_repo = clients.NodeRepository(env)
    snapshot_repo = clients.SnapshotRepository(env)
    rpc_repo = clients.RpcDataRepository(env)
    
    # Fetch node
    node = node_repo.get_node_by_id(validator_id)
    if not node:
        raise ValueError(f"Validator node not found: {validator_id}")
    
    # Get account info from subscription's customer
    subscription = node.subscription_id
    account_id = str(subscription.customer_name.id) if subscription and subscription.customer_name else "Unknown"
    account_name = subscription.customer_name.name or "Unknown" if subscription and subscription.customer_name else "Unknown"
    
    # Get user email for Vizion authentication
    user_email = None
    if subscription and subscription.customer_name:
        user_email = subscription.customer_name.email

    protocol = subscription.protocol_id if subscription else None
    price_service = TokenPriceService(env)
    protocol_metadata = _build_protocol_metadata(
        [protocol] if protocol else [],
        price_service
    )
    node_protocol_meta = protocol_metadata.get(protocol.id) if protocol else None
    detail_missing_price_protocols: set = set()
    
    vizion_client = clients.VizionClient(env, user_email=user_email)
    
    # Build host ID mapping from login response
    host_id_mapping = {}
    if user_email:
        try:
            login_response = oauth_utils.login_with_email(user_email)
            if login_response and login_response.get('success'):
                host_data_list = login_response.get('hostData', [])
                host_id_mapping = node_repo.build_host_id_mapping(host_data_list)
        except Exception as e:
            _logger.warning(f"Error building host ID mapping: {e}")
    
    # Build meta
    meta = models.ReportMeta(
        accountId=account_id,
        accountName=account_name,
        periodStart=helpers.format_date_for_response(period_start),
        periodEnd=helpers.format_date_for_response(period_end),
        range=range_type,
        timezone=timezone_str,
        validatorId=node.node_identifier or str(node.id),
        validatorName=node.node_name or "Unknown Validator"
    )
    
    # Fetch validator snapshots
    reward_snapshots = snapshot_repo.get_validator_reward_snapshots(
        [node.id], period_start, period_end
    )
    
    if not reward_snapshots:
        _logger.warning(f"No snapshots found for validator node {node.id}")
    
    # Aggregate stake and rewards
    avg_stake = aggregation.aggregate_series(
        [{'value': s.total_stake} for s in reward_snapshots],
        'value',
        aggregation.AggregationType.AVG
    ) if reward_snapshots else 0.0
    
    # Calculate rewards earned as delta: last cumulative - first cumulative
    sum_rewards = 0.0
    if reward_snapshots:
        sorted_snapshots = sorted(reward_snapshots, key=lambda s: s.snapshot_date)
        if len(sorted_snapshots) == 1:
            sum_rewards = sorted_snapshots[0].total_rewards
        else:
            sum_rewards = max(0, sorted_snapshots[-1].total_rewards - sorted_snapshots[0].total_rewards)
    
    stake_tokens_display, stake_usd_value = _convert_amount_with_metadata(
        avg_stake,
        node_protocol_meta,
        'stake_decimals',
        detail_missing_price_protocols
    )
    reward_tokens_display, reward_usd_value = _convert_amount_with_metadata(
        sum_rewards,
        node_protocol_meta,
        'reward_decimals',
        detail_missing_price_protocols
    )

    # Calculate APR
    apr = _calculate_apr(sum_rewards, avg_stake, range_type)
    
    # Check jailed status
    jailed = _is_validator_jailed(node)
    
    # Fetch performance snapshots for slashing events calculation
    performance_snapshots = snapshot_repo.get_validator_performance_snapshots(
        [node.id], period_start, period_end
    )
    
    # Calculate slashing events from performance snapshot data
    slashing_events = 0
    if performance_snapshots:
        sorted_perf = sorted(performance_snapshots, key=lambda s: s.snapshot_date)
        total_missed = 0
        total_signed = 0
        for snap in sorted_perf:
            missed = getattr(snap, 'missed_counter', 0) or 0
            signed = getattr(snap, 'signed_blocks', 0) or 0
            total_missed += missed
            total_signed += signed
        
        # Calculate miss rate and determine slashing indicator
        total_blocks = total_signed + total_missed
        if total_blocks > 0:
            miss_rate = total_missed / total_blocks
            # If miss rate > 10%, mark as slashing event
            if miss_rate > 0.1:
                slashing_events = 1
    
    # Fetch uptime from history API
    uptime_pct = 0.0
    try:
        host_id = node_repo.get_vizion_host_id(node, host_id_mapping)
        if host_id:
            current_time = datetime.now(pytz.UTC).isoformat()
            protocol = node.subscription_id.protocol_id
            protocol_name = protocol.name.lower().replace(' ', '') if protocol else 'ethereum'
            
            uptime_data_batch = vizion_client.fetch_uptime_history([host_id], protocol_name, current_time, range_type)
            uptime_data = uptime_data_batch.get(host_id, {})
            uptime_pct = uptime_data.get('uptime_pct', 0.0)
    except Exception as e:
        _logger.warning(f"Error fetching validator uptime: {e}")
    
    prev_reward_snapshots_detail = snapshot_repo.get_validator_reward_snapshots(
        [node.id], prev_start, prev_end
    )
    prev_avg_stake_detail = 0.0
    prev_total_rewards_detail = 0.0
    if prev_reward_snapshots_detail:
        prev_avg_stake_detail = aggregation.aggregate_series(
            [{'value': s.total_stake} for s in prev_reward_snapshots_detail],
            'value',
            aggregation.AggregationType.AVG
        )
        sorted_prev_detail = sorted(prev_reward_snapshots_detail, key=lambda s: s.snapshot_date)
        if len(sorted_prev_detail) == 1:
            prev_total_rewards_detail = sorted_prev_detail[0].total_rewards
        else:
            prev_total_rewards_detail = max(0, sorted_prev_detail[-1].total_rewards - sorted_prev_detail[0].total_rewards)
    
    prev_stake_tokens_display, prev_stake_usd_value = _convert_amount_with_metadata(
        prev_avg_stake_detail,
        node_protocol_meta,
        'stake_decimals',
        detail_missing_price_protocols
    )
    prev_reward_tokens_display, prev_reward_usd_value = _convert_amount_with_metadata(
        prev_total_rewards_detail,
        node_protocol_meta,
        'reward_decimals',
        detail_missing_price_protocols
    )
    val_status = _status_from_uptime(uptime_pct)
    
    # Calculate stake changes from previous period
    prev_reward_snapshots = snapshot_repo.get_validator_reward_snapshots(
        [node.id], prev_start, prev_end
    )
    prev_avg_stake = 0.0
    if prev_reward_snapshots:
        prev_avg_stake = aggregation.aggregate_series(
            [{'value': s.total_stake} for s in prev_reward_snapshots],
            'value',
            aggregation.AggregationType.AVG
        )
    
    current_stake_value = stake_usd_value if stake_usd_value is not None else stake_tokens_display
    previous_stake_value = prev_stake_usd_value if prev_stake_usd_value is not None else prev_stake_tokens_display
    current_reward_value = reward_usd_value if reward_usd_value is not None else reward_tokens_display
    previous_reward_value = prev_reward_usd_value if prev_reward_usd_value is not None else prev_reward_tokens_display

    stake_change = helpers.calculate_change(current_stake_value, previous_stake_value)
    stake_change_percent = helpers.calculate_change_percent(current_stake_value, previous_stake_value)
    rewards_change = helpers.calculate_change(current_reward_value, previous_reward_value)
    rewards_change_percent = helpers.calculate_change_percent(current_reward_value, previous_reward_value)
    
    overview = models.ValidatorNodeOverview(
        status=val_status,
        stakeDelta=stake_change,
        rewardsDelta=rewards_change
    )
    
    # Build metrics
    metrics = models.ValidatorMetrics(
        stake=current_stake_value,
        stakeChange=stake_change,
        stakeChangePercent=stake_change_percent,
        rewards=current_reward_value,
        rewardsChangePercent=rewards_change_percent,
        apr=apr,
        aprChange=0.0,
        uptimePct=uptime_pct,
        jailed=jailed,
        slashingEvents=slashing_events
    )
    
    # Fetch delegators
    delegators_info = _fetch_delegators_info(node, rpc_repo, reward_snapshots)
    
    # Calculate network comparison (placeholder)
    network_comparison = models.NetworkComparison(
        uptimeVsNetwork=0.0,  # TODO: calculate vs network average
        rewardsVsNetwork=0.0,
        aprVsNetwork=0.0,
        reliabilityVsNetwork=0.0
    )
    
    # Generate insights
    insights = _generate_validator_node_insights(metrics, delegators_info)
    
    # Fetch incidents from Vizion trigger data for this validator (if available)
    incidents = _fetch_incidents_from_vizion([node], vizion_client, node_repo, range_type, host_id_mapping)
    
    # Build trends with daily uptime percentages
    trends = []
    try:
        host_id = node_repo.get_vizion_host_id(node, host_id_mapping)
        if host_id:
            # Fetch uptime history for the validator
            protocol = node.subscription_id.protocol_id if node.subscription_id else None
            protocol_name = protocol.name.lower().replace(' ', '') if protocol else 'ethereum'
            current_time = datetime.now(pytz.UTC).isoformat()
            
            uptime_data_batch = vizion_client.fetch_uptime_history(
                [host_id], protocol_name, current_time, range_type
            )
            
            uptime_data = uptime_data_batch.get(host_id, {})
            uptime_data_points = uptime_data.get('data_points', {})
            
            # Debug: log structure of uptime data
            _logger.info(f"Validator uptime data structure: keys={list(uptime_data_points.keys())}")
            for port_name, points in uptime_data_points.items():
                if isinstance(points, list) and len(points) > 0:
                    _logger.info(f"Port {port_name}: {len(points)} points, sample: {points[0] if points else 'none'}")
            
            # Aggregate uptime data by day
            daily_uptime = _aggregate_daily_uptime_from_history(
                uptime_data_points,
                period_start,
                period_end
            )
            
            # Build trends array with uptime percentages
            current_date = period_start.replace(hour=0, minute=0, second=0, microsecond=0)
            while current_date <= period_end:
                date_key = current_date.strftime('%Y-%m-%d')
                uptime_pct = daily_uptime.get(date_key, 0.0)
                
                if uptime_pct > 0 or date_key in daily_uptime:
                    trend_point = models.TrendDataPoint(
                        date=date_key,
                        uptimePct=uptime_pct
                    )
                    trends.append(trend_point)
                
                current_date += timedelta(days=1)
            
            _logger.info(f"Built validator node trends with {len(trends)} data points")
    except Exception as e:
        _logger.warning(f"Error building validator node trends: {e}", exc_info=True)
        trends = []
    
    if detail_missing_price_protocols:
        _logger.warning(
            "USD conversion skipped for protocols without pricing metadata: %s",
            ", ".join(sorted(detail_missing_price_protocols))
        )

    return models.ValidatorNodeDetailReport(
        meta=meta,
        overview=overview,
        metrics=metrics,
        delegators=delegators_info,
        networkComparison=network_comparison,
        incidents=incidents,
        insights=insights,
        trends=trends
    )


# ============================================================================
# Helper functions
# ============================================================================

def _fetch_incidents_from_vizion(
    nodes: List[Any],
    vizion_client,
    node_repo,
    range_type: str,
    host_id_mapping: Optional[Dict[str, str]] = None
) -> List[models.Incident]:
    """
    Fetch historical incidents/alerts from Vizion for a list of nodes using batch API.
    
    Collects all host IDs, makes a single batch API call, then parses results.
    Uses cached host_id_mapping if provided to avoid repeated API calls.
    
    Response format is an array of events with tags containing node identifiers.
    Maps events back to nodes using networkId from tags.
    
    Args:
        nodes: List of subscription.node records
        vizion_client: Initialized VizionClient
        node_repo: Initialized NodeRepository
        range_type: 'weekly' or 'monthly'
        host_id_mapping: Pre-built mapping of node_identifier -> host_id (optional)
    
    Returns:
        List of Incident objects
    """
    incidents = []
    
    try:
        # Collect all host IDs
        host_ids = []
        node_identifier_to_node = {}
        
        for node in nodes:
            host_id = node_repo.get_vizion_host_id(node, host_id_mapping)
            if host_id:
                host_ids.append(host_id)
            # Also map by node_identifier for tag-based lookup
            node_identifier_to_node[node.node_identifier] = node
        
        if not host_ids:
            return incidents
        
        # Fetch trigger data for all hosts in batch
        current_time = datetime.now(pytz.UTC).isoformat()
        trigger_data_response = vizion_client.fetch_trigger_data(host_ids, current_time, range_type)
        
        if not trigger_data_response:
            return incidents
        
        # Response is a flat array of events (not nested by hostId)
        trigger_data_response = vizion_client.fetch_trigger_data(host_ids, current_time, range_type)
        
        if not trigger_data_response:
            return incidents
        
        def _flatten_trigger_events(payload):
            if isinstance(payload, list):
                return [event for event in payload if isinstance(event, dict)]
            
            if isinstance(payload, dict):
                data_section = payload.get('data', payload)
                
                if isinstance(data_section, list):
                    return [event for event in data_section if isinstance(event, dict)]
                
                if isinstance(data_section, dict):
                    flat_events = []
                    for host_events in data_section.values():
                        if isinstance(host_events, list):
                            flat_events.extend(
                                event for event in host_events if isinstance(event, dict)
                            )
                    return flat_events
            return []
        
        events = _flatten_trigger_events(trigger_data_response)
        
        if not isinstance(events, list):
            _logger.warning(f"Unexpected trigger data format: {type(events)}")
            return incidents
        
        # Parse events array
        for event in events:
            try:
                # Extract networkId from tags to map back to node
                tags = event.get('tags', []) if isinstance(event.get('tags'), list) else []
                node_identifier = None
                severity_str = None
                
                for tag in tags:
                    tag_name = tag.get('tag', '')
                    tag_value = tag.get('value', '')
                    
                    if tag_name == 'networkId':
                        node_identifier = tag_value
                    elif tag_name == 'host-name':
                        host_name = tag_value
                
                # Get the node from mapping
                node = None
                if node_identifier:
                    node = node_identifier_to_node.get(node_identifier)
                
                # Map severity number to label
                severity_map = {
                    '0': 'not_classified',
                    '1': 'info',
                    '2': 'warning',
                    '3': 'average',
                    '4': 'high',
                    '5': 'disaster'
                }
                severity_num = event.get('severity', '2')
                severity = severity_map.get(severity_num, 'medium')
                
                # Create incident
                incident = models.Incident(
                    id=event.get('eventid', helpers.generate_incident_id()),
                    severity=severity,
                    title=event.get('name', 'Alert'),
                    description=event.get('opdata', ''),
                    nodeName=node.node_name if node else "Unknown",
                    startTime=event.get('clock'),
                    endTime=None,  # Not provided in current response format
                    duration=None,  # Not provided in current response format
                    type='alert'  # Default type
                )
                incidents.append(incident)
            except Exception as e:
                _logger.warning(f"Error parsing event {event.get('eventid', '?')}: {e}")
                continue
    except Exception as e:
        _logger.exception(f"Error fetching incidents from Vizion: {e}")
    
    return incidents


def _parse_rpc_protocol_data(protocol_data: Dict[str, Any]) -> Dict[str, float]:
    """
    Parse and aggregate RPC protocol data from Vizion API response.
    
    NOTE: 
    - Uptime is fetched separately from fetch_uptime_history() API (available for all node types)
    - Request count is fetched separately via method count APIs (RPC nodes only)
    This function extracts latency and error metrics only.
    
    Args:
        protocol_data: Single host's data from Vizion protocol API batch response
    
    Returns:
        Dict with latencyMs, errorCount, errorRatePct (no uptime, no requestCount)
    """
    if not protocol_data:
        return {
            'latencyMs': 0.0,
            'errorCount': 0.0,
            'errorRatePct': 0.0
        }
    
    # Parse data from Vizion response (excluding uptime and requestCount)
    latency_ms = helpers.safe_float(protocol_data.get('latencyMs', 0))
    error_count = helpers.safe_float(protocol_data.get('errorCount', 0))
    # Note: Can't calculate error rate without request count from Vizion
    # Will need to fetch request count from method count APIs for RPC nodes
    error_rate_pct = 0.0
    
    return {
        'latencyMs': latency_ms,
        'errorCount': error_count,
        'errorRatePct': error_rate_pct
    }


def _fetch_rpc_node_data(node, vizion_client, range_type: str, host_id_mapping: Optional[Dict[str, str]] = None) -> Dict[str, float]:
    """
    Fetch and aggregate RPC node data from Vizion and method count APIs.
    
    Combines:
    - Protocol data (latency, errors) from Vizion protocol API
    - Uptime percentage from Vizion uptime history API (available for all node types)
    - Request count from method count API (RPC nodes only)
    
    Returns:
        Dict with uptimePct, latencyMs, requestCount, errorCount, errorRatePct
    """
    # Get Vizion host ID
    node_repo = clients.NodeRepository(vizion_client.env)
    host_id = node_repo.get_vizion_host_id(node, host_id_mapping)
    
    if not host_id:
        _logger.warning(f"No Vizion host ID found for node {node.id}, returning default values")
        return {
            'uptimePct': 0.0,
            'latencyMs': 0.0,
            'requestCount': 0.0,
            'errorCount': 0.0,
            'errorRatePct': 0.0
        }
    
    current_time = datetime.now(pytz.UTC).isoformat()
    # Fetch uptime from dedicated history API (available for all node types)
    protocol = node.subscription_id.protocol_id
    protocol_name = protocol.name.lower().replace(' ', '') if protocol else 'ethereum'
    
    # Fetch protocol data (latency, errors only - no uptime, no requestCount)
    protocol_data_batch = vizion_client.fetch_protocol_data([host_id], current_time, range_type, protocol_name)
    
    if not protocol_data_batch:
        _logger.warning(f"No protocol data returned from Vizion for node {node.id}")
        return {
            'uptimePct': 0.0,
            'latencyMs': 0.0,
            'requestCount': 0.0,
            'errorCount': 0.0,
            'errorRatePct': 0.0
        }
    
    # Get protocol metrics from batch response
    node_protocol_data = protocol_data_batch.get(host_id, {})
    protocol_metrics = _parse_rpc_protocol_data(node_protocol_data)
    
   
    
    uptime_data_batch = vizion_client.fetch_uptime_history([host_id], protocol_name, current_time, range_type)
    uptime_data = uptime_data_batch.get(host_id, {})
    uptime_pct = uptime_data.get('uptime_pct', 0.0)
    
    # Fetch request count from method count API (RPC nodes only)
    request_count = 0.0
    try:
        vizion_token = vizion_client._get_auth_token()
        num_days = helpers.get_days_in_range(range_type)
        
        # Get method count for this specific host
        method_data = oauth_utils.get_method_trend_for_host(host_id, vizion_token, num_days)
        if method_data:
            method_counts = method_data.get('latest_counts', {})
            request_count = sum(method_counts.values()) if method_counts else 0.0
    except Exception as e:
        _logger.warning(f"Error fetching method counts for node {node.id}: {e}")
    
    # Calculate error rate
    error_rate_pct = (protocol_metrics['errorCount'] / request_count * 100) if request_count > 0 else 0.0
    
    return {
        'uptimePct': uptime_pct,
        'latencyMs': protocol_metrics['latencyMs'],
        'requestCount': request_count,
        'errorCount': protocol_metrics['errorCount'],
        'errorRatePct': error_rate_pct
    }


def _calculate_apr(rewards: float, stake: float, range_type: str) -> float:
    """
    Calculate annualized percentage rate (APR).
    
    Args:
        rewards: Total rewards for the period
        stake: Average stake for the period
        range_type: 'weekly' or 'monthly'
    
    Returns:
        APR as percentage (e.g., 12.5 for 12.5%)
    """
    if stake == 0:
        return 0.0
    
    # Calculate period days
    period_days = 7 if range_type == 'weekly' else 30
    
    # APR = (rewards / stake) * (365 / period_days) * 100
    apr = (rewards / stake) * (365 / period_days) * 100
    return round(apr, 2)


def _is_validator_jailed(node) -> bool:
    """
    Check if validator is jailed.
    
    Args:
        node: subscription.node record
    
    Returns:
        True if jailed, False otherwise
    """
    # Try to parse validator_info JSON
    try:
        import json
        if node.validator_info:
            validator_info = json.loads(node.validator_info)
            return validator_info.get('jailed', False)
    except Exception as e:
        _logger.warning(f"Error parsing validator_info for node {node.id}: {e}")
    
    return False


def _fetch_method_breakdown(
    node,
    vizion_client,
    rpc_repo,
    range_type: str
) -> List[models.MethodBreakdownItem]:
    """
    Fetch method breakdown using existing get_all_hosts_method_count function.
    
    Returns:
        List of MethodBreakdownItem objects
    """
    try:
        # Get Vizion token and host ID
        token = vizion_client._get_auth_token()
        node_repo = clients.NodeRepository(vizion_client.env)
        host_id = node_repo.get_vizion_host_id(node)
        
        if not token or not host_id:
            return []
        
        # Get number of days
        num_days = helpers.get_days_in_range(range_type)
        
        # Fetch method counts
        method_data = rpc_repo.get_method_counts(host_id, token, num_days)
        
        if not method_data:
            return []
        
        # Parse method counts
        latest_counts = method_data.get('latest_counts', {})
        total_calls = sum(latest_counts.values()) if latest_counts else 0
        
        method_items = []
        for method_name, call_count in latest_counts.items():
            call_percent = (call_count / total_calls * 100) if total_calls > 0 else 0.0
            
            method_items.append(models.MethodBreakdownItem(
                method=method_name,
                callCount=call_count,
                callPercent=round(call_percent, 2),
                avgLatencyMs=0.0,  # TODO: fetch from detailed method data
                errorCount=0.0,  # TODO: fetch from error data
                errorRatePct=0.0
            ))
        
        # Sort by call count descending
        method_items.sort(key=lambda x: x.callCount, reverse=True)
        
        return method_items
    except Exception as e:
        _logger.exception(f"Error fetching method breakdown: {e}")
        return []


def _fetch_delegators_info(node, rpc_repo, reward_snapshots=None) -> models.DelegatorsInfo:
    """
    Fetch delegator information: count from snapshots, top delegators from RPC.
    
    Args:
        node: The validator node
        rpc_repo: RPC repository for fetching delegations
        reward_snapshots: Historical reward snapshots containing delegator_count
    
    Returns:
        DelegatorsInfo object with totalCount and topDelegators list
    """
    try:
        import json
        
        # Get valoper and protocol from reward snapshots (most reliable source)
        valoper = None
        protocol_key = None
        rpc_base_url = None
        total_count = 0
        
        if reward_snapshots:
            latest_snapshot = max(reward_snapshots, key=lambda s: s.snapshot_date)
            
            # Get valoper from snapshot
            if hasattr(latest_snapshot, 'valoper') and latest_snapshot.valoper:
                valoper = latest_snapshot.valoper
            
            # Get protocol info from snapshot
            if hasattr(latest_snapshot, 'protocol_key') and latest_snapshot.protocol_key:
                protocol_key = latest_snapshot.protocol_key
            
            # Get protocol record for RPC URL
            if hasattr(latest_snapshot, 'protocol_id') and latest_snapshot.protocol_id:
                protocol = latest_snapshot.protocol_id
                # Check network_selection_id to choose correct RPC endpoint for Coreum
                network_selection = node.network_selection_id
                network_name = (network_selection.name or "").strip().lower() if network_selection else "mainnet"
                
                if protocol.name.lower().replace(' ', '') == "coreum" and network_name == "testnet":
                    rpc_base_url = (protocol.web_url_testnet or "").strip()
                else:
                    rpc_base_url = protocol.web_url if protocol else ''
            
            # Get delegator count from snapshot
            if hasattr(latest_snapshot, 'delegator_count') and latest_snapshot.delegator_count:
                total_count = int(latest_snapshot.delegator_count)
        
        # Fallback to node.validator_info if snapshot didn't have the data
        if not valoper or not protocol_key or not rpc_base_url:
            if node.validator_info:
                validator_info = json.loads(node.validator_info)
                if not valoper:
                    valoper = validator_info.get('valoper') or validator_info.get('validator_address')
            
            # Get protocol from node if still missing
            if not protocol_key or not rpc_base_url:
                protocol = node.subscription_id.protocol_id
                if not protocol_key:
                    protocol_key = protocol.name.lower().replace(' ', '') if protocol else ''
                if not rpc_base_url:
                    # Check network_selection_id to choose correct RPC endpoint
                    network_selection = node.network_selection_id
                    network_name = (network_selection.name or "").strip().lower() if network_selection else "mainnet"
                    
                    if protocol_key == "coreum" and network_name == "testnet":
                        rpc_base_url = (protocol.web_url_testnet or "").strip() if protocol else ''
                    else:
                        rpc_base_url = protocol.web_url if protocol else ''
        
        # Validate we have required data
        if not valoper:
            _logger.warning(f"No valoper found for node {node.id}")
            return models.DelegatorsInfo(totalCount=0, topDelegators=[])
        
        if not protocol_key or not rpc_base_url:
            _logger.warning(f"Missing protocol info for node {node.id}: protocol_key={protocol_key}, rpc_base_url={rpc_base_url}")
            return models.DelegatorsInfo(totalCount=0, topDelegators=[])
        
        # Fetch top delegators from RPC
        top_delegators = []
        delegations_data = rpc_repo.get_validator_delegations(
            valoper, protocol_key, rpc_base_url
        )
        
        if delegations_data and 'items' in delegations_data:
            # If we didn't get count from snapshot, use RPC count as fallback
            if total_count == 0:
                total_count = len(delegations_data['items'])
            
            # Sort delegators by stake amount (descending) to get actual top delegators
            sorted_delegators = sorted(
                delegations_data['items'],
                key=lambda x: helpers.safe_float(x.get('amount', 0)),
                reverse=True
            )

            # Get top 10 delegators by stake
            for item in sorted_delegators[:10]:
                top_delegators.append(models.TopDelegator(
                    delegatorAddress=item.get('delegatorAddress', ''),
                    delegatedStake=helpers.safe_float(item.get('amount', 0)),
                    delegatePercentOfValidator=helpers.safe_float(item.get('pctOfValidator', 0)),

                ))
        else:
            pass
        
        return models.DelegatorsInfo(
            totalCount=total_count,
            topDelegators=top_delegators
        )
    except Exception as e:
        _logger.exception(f"Error fetching delegators info: {e}")
        return models.DelegatorsInfo(totalCount=0, topDelegators=[])


# ============================================================================
# Insight generation functions
# ============================================================================

def _generate_account_insights(
    rpc_summary,
    validator_summary,
    rpc_highlights,
    validator_highlights
) -> List[models.Insight]:
    """Generate insights for account weekly report."""
    insights = []
    
    # RPC insights
    if rpc_summary.avgUptimePct < 95:
        insights.append(models.Insight(
            id=helpers.generate_insight_id(),
            title="RPC Uptime Below Target",
            description=f"Your RPC fleet uptime is {rpc_summary.avgUptimePct:.1f}%, below the recommended 95%.",
            recommendation="Review nodes with critical status and consider infrastructure improvements.",
            impact="high"
        ))
    
    # Validator insights
    if validator_summary.jailedCount > 0:
        insights.append(models.Insight(
            id=helpers.generate_insight_id(),
            title="Jailed Validators Detected",
            description=f"{validator_summary.jailedCount} validator(s) are currently jailed.",
            recommendation="Check validator performance and unjail if eligible.",
            impact="high"
        ))
    
    # APR insights
    if validator_summary.avgAPR < 10:
        insights.append(models.Insight(
            id=helpers.generate_insight_id(),
            title="Low Average APR",
            description=f"Your validator APR of {validator_summary.avgAPR:.1f}% is below typical network rates.",
            recommendation="Review commission rates and validator performance.",
            impact="medium"
        ))
    
    return insights


def _generate_rpc_fleet_insights(summary, node_items) -> List[models.Insight]:
    """Generate insights for RPC fleet report."""
    insights = []
    
    if summary.criticalNodes > 0:
        insights.append(models.Insight(
            id=helpers.generate_insight_id(),
            title="Critical Nodes Detected",
            description=f"{summary.criticalNodes} node(s) are in critical status.",
            recommendation="Investigate and resolve issues with critical nodes immediately.",
            impact="high"
        ))
    
    if summary.avgLatencyMs > 500:
        insights.append(models.Insight(
            id=helpers.generate_insight_id(),
            title="High Average Latency",
            description=f"Fleet latency of {summary.avgLatencyMs:.0f}ms exceeds recommended threshold.",
            recommendation="Consider upgrading infrastructure or optimizing network routing.",
            impact="medium"
        ))
    
    return insights


def _generate_rpc_node_insights(node_data, security) -> List[models.Insight]:
    """Generate insights for RPC node detail report."""
    insights = []
    
    if node_data['errorRatePct'] > 5:
        insights.append(models.Insight(
            id=helpers.generate_insight_id(),
            title="High Error Rate",
            description=f"Error rate of {node_data['errorRatePct']:.1f}% is above acceptable levels.",
            recommendation="Investigate error logs and consider node restart or configuration review.",
            impact="high"
        ))
    
    # TODO: TLS certificate monitoring - will be implemented in future
    
    return insights


def _generate_validator_fleet_insights(summary, validator_items, risk_indicators) -> List[models.Insight]:
    """Generate insights for validator fleet report."""
    insights = []
    
    if risk_indicators.slashingRisk == 'high':
        insights.append(models.Insight(
            id=helpers.generate_insight_id(),
            title="High Slashing Risk",
            description="Multiple slashing events detected across your validator fleet.",
            recommendation="Review validator operations and implement double-sign protection.",
            impact="high"
        ))
    
    if summary.jailedValidators > 0:
        insights.append(models.Insight(
            id=helpers.generate_insight_id(),
            title="Validators Jailed",
            description=f"{summary.jailedValidators} validator(s) are currently jailed.",
            recommendation="Check missed blocks and unjail validators if possible.",
            impact="high"
        ))
    
    return insights


def _generate_validator_node_insights(metrics, delegators_info) -> List[models.Insight]:
    """Generate insights for validator node detail report."""
    insights = []
    
    if metrics.jailed:
        insights.append(models.Insight(
            id=helpers.generate_insight_id(),
            title="Validator is Jailed",
            description="Your validator is currently jailed and not earning rewards.",
            recommendation="Review missed blocks and unjail the validator.",
            impact="high"
        ))
    
    if delegators_info.totalCount < 10:
        insights.append(models.Insight(
            id=helpers.generate_insight_id(),
            title="Low Delegator Count",
            description=f"Only {delegators_info.totalCount} delegators, which may impact visibility.",
            recommendation="Consider marketing efforts to attract more delegators.",
            impact="low"
        ))
    
    return insights
