# -*- coding: utf-8 -*-
"""
Data client wrappers for report generation.

Provides interfaces to fetch data from:
- Vizion API (uptime, latency, requests, errors, incidents, security)
- Odoo ORM (validator snapshots, node records)
- RPC functions (validator summaries, delegations, method counts)
"""

import logging
import requests
import pytz
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from odoo.http import request
from ....auth_module.utils import oauth as oauth_utils
from ....subscription_management.utils.subscription_helpers import (
                _compute_validator_summary
)
from ....subscription_management.utils.subscription_helpers import (
                _compute_validator_delegations
)

_logger = logging.getLogger(__name__)


class VizionClient:
    """
    Client wrapper for Vizion API calls.
    
    Handles authentication, request formatting, and response parsing
    for Vizion endpoints used in reports.
    """
    
    def __init__(self, env, user_email: Optional[str] = None):
        """
        Initialize Vizion client.
        
        Args:
            env: Odoo environment (request.env)
            user_email: Email of user for Vizion authentication (optional, will use current user if not provided)
        """
        self.env = env
        self.user_email = user_email
        self.base_url = env['ir.config_parameter'].sudo().get_param('vision_base_url')
        self.backend_url = env['ir.config_parameter'].sudo().get_param('backend_url')
        self._token = None
    
    def _get_auth_token(self) -> Optional[str]:
        """
        Get Vizion authentication token by logging in with user email.
        
        Returns:
            JWT token string or None if login fails
        """
        if self._token:
            return self._token
        
        try:
            # Use user email for Vizion authentication
            if not self.user_email:
                _logger.error("Cannot authenticate with Vizion: no user email provided")
                return None
            
            login_response = oauth_utils.login_with_email(self.user_email)
            
            if login_response and login_response.get('success'):
                self._token = login_response.get('token')
                return self._token
            else:
                _logger.error(f"Vizion login failed for user {self.user_email}")
                return None
        except Exception as e:
            _logger.exception(f"Error getting Vizion token for {self.user_email}: {e}")
            return None
    
    def _normalize_vizion_protocol_name(self, protocol_name: str) -> str:
        """
        Map protocol names to Vizion API format.
        
        Vizion supports: coreum, avalanche, evm, ethereum, polygon-cdk, parachain, flow, near, subsquid, injective, ewx, theta, etherlink
        
        Args:
            protocol_name: Protocol name (potentially with spaces, mixed case)
        
        Returns:
            Protocol name in Vizion format
        """
        normalized = protocol_name.strip().lower().replace(' ', '')
        protocol_mapping = {
            'energyweb': 'ewx',
            'ewx': 'ewx',
            'ethereum': 'ethereum',
            'polygon': 'polygon-cdk',
            'polygoncdk': 'polygon-cdk',
            'avalanche': 'avalanche',
            'coreum': 'coreum',
            'near': 'near',
            'flow': 'flow',
            'theta': 'theta',
            'etherlink': 'etherlink',
            'evm': 'evm',
            'subsquid': 'subsquid',
            'injective': 'injective',
            'parachain': 'parachain',
        }
        return protocol_mapping.get(normalized, normalized)
    
    def fetch_protocol_data(
        self,
        host_ids: List[str],
        current_time: str,
        range_type: str,
        protocol_name: str
    ) -> Dict[str, Any]:
        """
        Fetch protocol data from Vizion /api/item/get-latest-protocol-data endpoint.
        
        Supports batch requests with multiple hostIds for efficiency.
        
        Args:
            host_ids: List of host IDs (e.g., ["node-1-primary", "node-2-primary"])
            current_time: Current timestamp in ISO format
            range_type: 'weekly' or 'monthly'
            protocol_name: Protocol name (e.g., 'ethereum', 'polygon', etc.)
        
        Returns:
            Dict with raw Vizion response data or empty dict if failed
        """
        token = self._get_auth_token()
        if not token:
            _logger.error("Cannot fetch protocol data without Vizion token")
            return {}
        
        if not host_ids:
            _logger.warning("No host IDs provided for protocol data fetch")
            return {}
        
        try:
            # Map range_type to Vizion range format (1h, 24h, 7d)
            range_map = {
                'weekly': '7d',
                'monthly': '30d'
            }
            vizion_range = range_map.get(range_type, '7d')
            
            # Normalize protocol name for Vizion API
            vizion_protocol_name = self._normalize_vizion_protocol_name(protocol_name)
            vision_base_url = request.env['ir.config_parameter'].sudo().get_param('vision_base_url')
            url = f"{vision_base_url}/api/item/get-latest-protocol-data?protocol={vizion_protocol_name}"
            headers = {
                "Content-Type": "application/json",
                "Origin": self.backend_url,
                "Referer": self.backend_url,
                "Authorization": f"Bearer {token}",
            }
            # Use primary host for request, batch hostIds in payload
            primary_host = host_ids[0]
            payload = {
                "primaryHost": primary_host,
                "hostIds": host_ids,
                "currentTime": current_time,
                "range": vizion_range,
                "token": token,
            }
            _logger.info(f"DEBUG Uptime API Payload - hostIds: {host_ids}, primaryHost: {primary_host}, range: {range_type}")
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            if response.status_code in (200, 201):
                result = response.json()
                if result.get('success'):
                    return result.get('data', {})
                else:
                    _logger.warning(f"Vizion protocol data request unsuccessful: {result}")
                    return {}
            else:
                _logger.error(
                    f"Vizion protocol data API error {response.status_code}: {response.text}"
                )
                return {}
        except Exception as e:
            _logger.exception(f"Error fetching protocol data from Vizion: {e}")
            return {}
    
    def fetch_trigger_data(
        self,
        host_ids: List[str],
        current_time: str,
        range_type: str
    ) -> Dict[str, Any]:
        """
        Fetch trigger/incident data from Vizion /api/item/get-trigger-data endpoint.
        
        Supports batch requests with multiple hostIds for efficiency.
        
        Args:
            host_ids: List of host IDs (e.g., ["node-1-primary", "node-2-primary"])
            current_time: Current timestamp in ISO format
            range_type: 'weekly' or 'monthly'
        
        Returns:
            Dict with trigger/incident data or empty dict if failed
        """
        token = self._get_auth_token()
        if not token:
            _logger.error("Cannot fetch trigger data without Vizion token")
            return {}
        
        try:
            # Map range_type to Vizion range format (1h, 24h, 7d)
            range_map = {
                'weekly': '7d',
                'monthly': '30d'
            }
            vizion_range = range_map.get(range_type, '7d')
            
            url = f"{self.base_url}/api/history/historical-alerts"
            headers = {
                "Content-Type": "application/json",
                "Origin": self.backend_url,
                "Referer": self.backend_url,
                "Authorization": f"Bearer {token}",
            }
            # Always use hostIds array format
            payload = {
                "hostIds": host_ids,
                "currentTime": current_time,
                "range": vizion_range,
            }
            
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            print("response alert", response.json())
            if response.status_code in (200, 201):
                result = response.json()
                if result.get('success'):
                    return result.get('data', {})
                else:
                    _logger.warning(f"Vizion trigger data request unsuccessful: {result}")
                    return {}
            else:
                _logger.error(
                    f"Vizion trigger data API error {response.status_code}: {response.text}"
                )
                return {}
        except Exception as e:
            _logger.exception(f"Error fetching trigger data from Vizion: {e}")
            return {}
    
    def fetch_security_data(
        self,
        host_ids: List[str],
        current_time: str,
        range_type: str
    ) -> Dict[str, Any]:
        """
        Fetch security monitor data from Vizion /api/item/get-security-monitor-data endpoint.
        
        Supports batch requests with multiple hostIds for efficiency.
        
        Args:
            host_ids: List of host IDs (e.g., ["node-1-primary", "node-2-primary"])
            current_time: Current timestamp in ISO format
            range_type: 'weekly' or 'monthly'
        
        Returns:
            Dict with security data or empty dict if failed
        """
        token = self._get_auth_token()
        if not token:
            _logger.error("Cannot fetch security data without Vizion token")
            return {}
        
        try:
            # Map range_type to Vizion range format (1h, 24h, 7d)
            range_map = {
                'weekly': '7d',
                'monthly': '30d'
            }
            vizion_range = range_map.get(range_type, '7d')
            
            url = f"{self.base_url}/api/item/get-security-monitor-data"
            headers = {
                "Content-Type": "application/json",
                "Origin": self.backend_url,
                "Referer": self.backend_url,
                "Authorization": f"Bearer {token}",
            }
            # Always use hostIds array format
            payload = {
                "hostIds": host_ids,
                "currentTime": current_time,
                "range": vizion_range,
            }
            
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            if response.status_code in (200, 201):
                result = response.json()
                if result.get('success'):
                    return result.get('data', {})
                else:
                    _logger.warning(f"Vizion security data request unsuccessful: {result}")
                    return {}
            else:
                _logger.error(
                    f"Vizion security data API error {response.status_code}: {response.text}"
                )
                return {}
        except Exception as e:
            _logger.exception(f"Error fetching security data from Vizion: {e}")
            return {}
    
    def fetch_uptime_history(
        self,
        host_ids: List[str],
        protocol_name: str,
        current_time: str,
        range_type: str
    ) -> Dict[str, Dict[str, Any]]:
        """
        Fetch uptime history data from Vizion /api/history/get-port-uptime-history-generic endpoint.
        
        Supports batch requests with multiple hostIds for efficiency.
        Calculates uptime percentage from historical data points where 1 = up, 0 = down.
        
        Args:
            host_ids: List of host IDs (e.g., ["node-1-primary", "node-2-primary"])
            protocol_name: Protocol name (e.g., 'ethereum', 'coreum', 'near')
            current_time: Current timestamp in ISO format
            range_type: 'weekly' (7 days) or 'monthly' (28-31 days)
        
        Returns:
            Dict mapping host_id -> {'uptime_pct': float, 'data_points': dict}
            Example: {
                "host-1": {"uptime_pct": 95.5, "data_points": {...}},
                "host-2": {"uptime_pct": 98.2, "data_points": {...}}
            }
        """
        token = self._get_auth_token()
        if not token:
            _logger.error("Cannot fetch uptime history without Vizion token")
            return {host_id: {'uptime_pct': 0.0, 'data_points': {}} for host_id in host_ids}
        
        if not host_ids:
            return {}
        
        try:
            # Map range_type to Vizion range format (1h, 24h, 7d)
            range_map = {
                'weekly': '7d',
                'monthly': '30d'  # Use 7d for monthly as well
            }
            vizion_range = range_map.get(range_type, '7d')
            
            # Normalize protocol name for Vizion API
            vizion_protocol_name = self._normalize_vizion_protocol_name(protocol_name)
            url = f"{self.base_url}/api/history/get-port-uptime-history-generic?protocol={vizion_protocol_name}"
            headers = {
                "Content-Type": "application/json",
                "Origin": self.backend_url,
                "Referer": self.backend_url,
                "Authorization": f"Bearer {token}",
            }
            # Use primary host for request, but Vizion backend will fetch data for all hostIds
            str_host_ids = [str(h) for h in host_ids]
            primary_host = str_host_ids[0]
            payload = {
                "hostIds": str_host_ids,
                "primaryHost": primary_host,
                "range": vizion_range,
                "currentTime": current_time,
                "token": token,
            }
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            if response.status_code in (200, 201):
                result = response.json()
                if result.get('success'):
                    data = result.get('data', {})

                    
                    # Process response: response structure differs for single vs multi-host
                    # Single host: {'Port 8545 Status': [...], 'Port 9000 Status': [...]}
                    # Multi host: {'11658': {'Port 8545 Status': [...]}, '11659': {'Port 8545 Status': [...]}}
                    uptime_results = {}
                    
                    # Check if this is a multi-host response by checking if host IDs are keys
                    # For multi-host, at least one key should match a host_id (as string)
                    str_host_ids_set = set(str(hid) for hid in host_ids)
                    response_keys = set(data.keys())
                    is_multi_host_response = bool(str_host_ids_set & response_keys)
                    
                    
                    if is_multi_host_response and len(host_ids) > 1:
                        # Multi-host format: each host ID has its own port data dict
                        for host_id in host_ids:
                            str_host_id = str(host_id)
                            port_data = data.get(str_host_id, {})
                            uptime_pct = self._calculate_uptime_percentage(port_data)
                            uptime_results[host_id] = {
                                'uptime_pct': uptime_pct,
                                'data_points': port_data
                            }
                    else:
                        # Single-host format: data is flat with port names as keys
                        # {'Port 8545 Status': [data_points], 'Port 9000 Status': [data_points]}
                        for host_id in host_ids:
                            uptime_pct = self._calculate_uptime_percentage(data)
                            uptime_results[host_id] = {
                                'uptime_pct': uptime_pct,
                                'data_points': data
                            }
                    
                    _logger.info(f"Fetched uptime history for {len(uptime_results)} hosts")
                    return uptime_results
                else:
                    _logger.warning(f"Vizion uptime history request unsuccessful: {result}")
                    return {host_id: {'uptime_pct': 0.0, 'data_points': {}} for host_id in host_ids}
            else:
                _logger.error(
                    f"Vizion uptime history API error {response.status_code}: {response.text}"
                )
                return {host_id: {'uptime_pct': 0.0, 'data_points': {}} for host_id in host_ids}
        except Exception as e:
            _logger.exception(f"Error fetching uptime history from Vizion: {e}")
            return {host_id: {'uptime_pct': 0.0, 'data_points': {}} for host_id in host_ids}
    
    def _calculate_uptime_percentage(self, uptime_data: Dict[str, Any]) -> float:
        """
        Calculate uptime percentage from Vizion uptime history data.
        
        Vizion returns data points where value_avg = 1 (up) or value_avg = 0 (down).
        This calculates the percentage of time the node was up.
        
        Args:
            uptime_data: Dict from uptime history API response with port status arrays
        
        Returns:
            Uptime percentage (0-100)
        """
        try:
            total_points = 0
            up_points = 0
            
            # Iterate through all port status arrays
            for port_name, data_points in uptime_data.items():
                if isinstance(data_points, list):
                    for point in data_points:
                        if isinstance(point, dict):
                            try:
                                # Extract value_avg and convert to float
                                value_avg = float(point.get('value_avg', 0))
                                total_points += 1
                                
                                # value_avg = 1 means up, 0 means down
                                if value_avg >= 1:
                                    up_points += 1
                            except (ValueError, TypeError):
                                _logger.warning(f"Invalid value_avg in data point: {point}")
                                continue
            
            # Calculate percentage
            if total_points == 0:
                _logger.warning("No uptime data points found")
                return 0.0
            
            uptime_pct = (up_points / total_points) * 100
            _logger.info(
                f"Calculated uptime: {up_points}/{total_points} points up = {uptime_pct:.2f}%"
            )
            return round(uptime_pct, 2)
        except Exception as e:
            _logger.exception(f"Error calculating uptime percentage: {e}")
            return 0.0

    def fetch_daily_method_trends(
        self,
        host_ids: List[str],
        num_days: int
    ) -> tuple:
        """
        Fetch daily RPC method request trends from Vision API.
        
        Calls /api/history/get-eth-method-trend-bulk endpoint to get daily
        request counts aggregated across all hosts AND per-host.
        
        Args:
            host_ids: List of host IDs to fetch trends for
            num_days: Number of days of historical data to fetch
        
        Returns:
            Tuple of (daily_totals, per_host_daily):
            - daily_totals: Dict[date (YYYY-MM-DD) -> total request count (float)]
              Example: {'2026-02-19': 12500000, '2026-02-25': 14300000}
            - per_host_daily: Dict[host_id -> Dict[date -> request_count]]
              Example: {'11228': {'2026-02-19': 1250000, '2026-02-25': 1430000}, ...}
        """
        token = self._get_auth_token()
        if not token:
            _logger.error("Cannot fetch method trends without Vizion token")
            return {}, {}
        
        if not host_ids:
            _logger.warning("No host IDs provided for method trends fetch")
            return {}, {}
        
        try:
            current_time = datetime.now(pytz.UTC).isoformat()
            url = f"{self.base_url}/api/history/get-eth-method-trend-bulk"
            headers = {
                "Content-Type": "application/json",
                "Origin": self.backend_url,
                "Referer": self.backend_url,
                "Authorization": f"Bearer {token}",
            }
            
            payload = {
                "numOfDays": num_days,
                "hostIds": host_ids,
                "currentTime": current_time,
            }
            
            _logger.info(f"Fetching method trends for {len(host_ids)} hosts, {num_days} days")
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            
            if response.status_code in (200, 201):
                result = response.json()
                if result.get('success'):
                    data = result.get('data', {})
                    _logger.debug(f"DEBUG: Vision API method trends response data type: {type(data)}, keys: {list(data.keys()) if isinstance(data, dict) else 'N/A'}")
                    if isinstance(data, dict):
                        for k, v in list(data.items())[:2]:  # Log first 2 entries
                            _logger.debug(f"DEBUG: data[{k}] = {type(v)}, {v if not isinstance(v, (dict, list)) else '...'}")
                    return self._aggregate_method_trend_data(data)
                else:
                    _logger.warning(f"Vision method trends request unsuccessful: {result}")
                    return {}, {}
            else:
                _logger.error(
                    f"Vision method trends API error {response.status_code}: {response.text}"
                )
                return {}, {}
        except Exception as e:
            _logger.exception(f"Error fetching method trends from Vision API: {e}")
            return {}
    
    def _aggregate_method_trend_data(self, raw_data: Dict[str, Any]) -> tuple:
        """
        Parse and aggregate method trend data by date from Vision API response.
        
        Handles flexible response formats from Vision API:
        - Direct host mapping: {host_id: {method_name: [data_points]}}
        - Nested format: {hosts: [...]}, {hostData: [...]}, etc.
        
        Handles both Unix timestamp (in 'clock' field) and ISO format dates.
        Returns both total aggregates and per-host aggregates for individual node request counts.
        
        Args:
            raw_data: Raw response data from Vision API
        
        Returns:
            Tuple of (daily_totals_dict, per_host_daily_dict):
            - daily_totals_dict: {date_YYYY-MM-DD: total_requests_float}
            - per_host_daily_dict: {host_id: {date_YYYY-MM-DD: requests_float}}
        """
        daily_aggregates = {}
        per_host_daily = {}  # Track per-host aggregates for individual node counts
        
        try:
            if not isinstance(raw_data, dict):
                _logger.warning(f"Expected dict raw_data, got {type(raw_data)}, value: {raw_data}")
                return daily_aggregates, per_host_daily
            
            _logger.info(f"DEBUG: Aggregating method trends. Raw data type: {type(raw_data)}, keys: {list(raw_data.keys())[:5]}")
            
            # First, try to extract method data from the response
            # Vision API might return in different formats
            method_data_map = {}
            
            # Format 1: Direct host mapping {host_id: {method: [...]}}
            has_direct_format = False
            for host_key, host_value in raw_data.items():
                if isinstance(host_value, dict) and any(isinstance(v, list) for v in host_value.values()):
                    has_direct_format = True
                    method_data_map[host_key] = host_value
            
            # Format 2: Nested in 'hosts', 'hostData', 'items', etc.
            if not has_direct_format:
                for nested_key in ("hosts", "hostData", "items", "data"):
                    nested_data = raw_data.get(nested_key)
                    if isinstance(nested_data, list):
                        _logger.info(f"DEBUG: Found nested data under '{nested_key}': {len(nested_data)} items")
                        # Each item should be {hostId: ..., methodData/data/methods: [...]}
                        for item in nested_data:
                            if not isinstance(item, dict):
                                continue
                            host_id = item.get("hostId") or item.get("host_id") or item.get("networkId") or item.get("primaryHost")
                            methods = item.get("methodData") or item.get("data") or item.get("methods") or item.get("method_count")
                            if host_id and methods:
                                method_data_map[host_id] = methods
                        break
            
            # Format 3: If raw_data itself has only one key and its value is method data
            if not method_data_map and len(raw_data) == 1:
                only_key = next(iter(raw_data.keys()))
                only_value = raw_data[only_key]
                if isinstance(only_value, dict) and any(isinstance(v, list) for v in only_value.values()):
                    method_data_map[only_key] = only_value
            
            _logger.info(f"DEBUG: Extracted method data map for {len(method_data_map)} hosts")
            
            # Now aggregate by date, tracking both totals and per-host
            for host_id, host_methods in method_data_map.items():
                if not isinstance(host_methods, dict):
                    _logger.debug(f"Host {host_id} methods is not dict (type: {type(host_methods)}), skipping")
                    continue
                
                # Initialize per-host daily aggregates
                if host_id not in per_host_daily:
                    per_host_daily[host_id] = {}
                
                for method_name, data_points in host_methods.items():
                    if not isinstance(data_points, list):
                        _logger.debug(f"Host {host_id} method {method_name} data is not list (type: {type(data_points)}), skipping")
                        continue
                    
                    _logger.debug(f"Host {host_id}, method {method_name}: {len(data_points)} data points")
                    
                    for point in data_points:
                        if not isinstance(point, dict):
                            continue
                        
                        # Vision API returns 'clock' field with Unix timestamp, NOT 'date'
                        date_str = point.get('clock') or point.get('date')
                        # Vision API returns 'value_avg' as string, need to convert
                        value_avg_str = point.get('value_avg', 0)
                        
                        if not date_str:
                            continue
                        
                        try:
                            # Convert value_avg to float (it comes as string from API)
                            value_avg = float(value_avg_str)
                            
                            # Parse the date (could be Unix timestamp or ISO format)
                            date_str_val = str(date_str).strip()
                            
                            if date_str_val.isdigit() and len(date_str_val) >= 10:
                                # Unix timestamp (from 'clock' field)
                                timestamp = int(date_str_val)
                                parsed_date = datetime.fromtimestamp(timestamp, tz=pytz.UTC)
                            else:
                                # Try ISO format
                                parsed_date = datetime.fromisoformat(date_str_val.replace('Z', '+00:00'))
                                if parsed_date.tzinfo is None:
                                    parsed_date = pytz.UTC.localize(parsed_date)
                            
                            # Format as YYYY-MM-DD
                            date_key = parsed_date.strftime('%Y-%m-%d')
                            
                            # Aggregate the request count (both total and per-host)
                            daily_aggregates[date_key] = daily_aggregates.get(date_key, 0) + value_avg
                            per_host_daily[host_id][date_key] = per_host_daily[host_id].get(date_key, 0) + value_avg
                            
                        except (ValueError, TypeError) as e:
                            _logger.debug(f"Error parsing date {date_str} or value {value_avg_str}: {e}")
                            continue
            
            _logger.info(f"DEBUG: Aggregated method trends to {len(daily_aggregates)} days: {daily_aggregates}")
            _logger.info(f"DEBUG: Per-host data available for {len(per_host_daily)} hosts")
            return daily_aggregates, per_host_daily
            
        except Exception as e:
            _logger.exception(f"Error aggregating method trend data: {e}")
            return daily_aggregates, per_host_daily




class SnapshotRepository:
    """
    Repository for fetching validator snapshot data from Odoo ORM.
    
    Provides batch queries for validator rewards and performance snapshots.
    """
    
    def __init__(self, env):
        """
        Initialize snapshot repository.
        
        Args:
            env: Odoo environment (request.env)
        """
        self.env = env
    
    def get_validator_reward_snapshots(
        self,
        node_ids: List[int],
        start_date: datetime,
        end_date: datetime
    ) -> List[Any]:
        """
        Fetch validator reward snapshots for given nodes and date range.
        
        Args:
            node_ids: List of subscription.node IDs
            start_date: Start of period (inclusive)
            end_date: End of period (inclusive)
        
        Returns:
            List of validator.rewards.snapshot records
        """
        if not node_ids:
            return []
        
        try:
            domain = [
                ('node_id', 'in', node_ids),
                ('snapshot_date', '>=', start_date),
                ('snapshot_date', '<=', end_date),
            ]
            snapshots = self.env['validator.rewards.snapshot'].sudo().search(
                domain,
                order='snapshot_date desc'
            )
            _logger.info(
                f"Fetched {len(snapshots)} reward snapshots for {len(node_ids)} nodes"
            )
            return snapshots
        except Exception as e:
            _logger.exception(f"Error fetching reward snapshots: {e}")
            return []
    
    def get_validator_performance_snapshots(
        self,
        node_ids: List[int],
        start_date: datetime,
        end_date: datetime
    ) -> List[Any]:
        """
        Fetch validator performance snapshots for given nodes and date range.
        
        Args:
            node_ids: List of subscription.node IDs
            start_date: Start of period (inclusive)
            end_date: End of period (inclusive)
        
        Returns:
            List of validator.performance.snapshot records
        """
        if not node_ids:
            return []
        
        try:
            domain = [
                ('node_id', 'in', node_ids),
                ('snapshot_date', '>=', start_date),
                ('snapshot_date', '<=', end_date),
            ]
            snapshots = self.env['validator.performance.snapshot'].sudo().search(
                domain,
                order='snapshot_date desc'
            )
            _logger.info(
                f"Fetched {len(snapshots)} performance snapshots for {len(node_ids)} nodes"
            )
            return snapshots
        except Exception as e:
            _logger.exception(f"Error fetching performance snapshots: {e}")
            return []
    
    def get_latest_reward_snapshot(self, node_id: int) -> Optional[Any]:
        """
        Get the most recent reward snapshot for a node.
        
        Args:
            node_id: subscription.node ID
        
        Returns:
            Latest validator.rewards.snapshot record or None
        """
        try:
            snapshot = self.env['validator.rewards.snapshot'].sudo().search(
                [('node_id', '=', node_id)],
                order='snapshot_date desc',
                limit=1
            )
            return snapshot if snapshot else None
        except Exception as e:
            _logger.exception(f"Error fetching latest reward snapshot: {e}")
            return None
    
    def get_latest_performance_snapshot(self, node_id: int) -> Optional[Any]:
        """
        Get the most recent performance snapshot for a node.
        
        Args:
            node_id: subscription.node ID
        
        Returns:
            Latest validator.performance.snapshot record or None
        """
        try:
            snapshot = self.env['validator.performance.snapshot'].sudo().search(
                [('node_id', '=', node_id)],
                order='snapshot_date desc',
                limit=1
            )
            return snapshot if snapshot else None
        except Exception as e:
            _logger.exception(f"Error fetching latest performance snapshot: {e}")
            return None


class RpcDataRepository:
    """
    Repository for fetching data via existing RPC helper functions.
    
    Wraps existing functions like _compute_validator_summary(),
    _compute_validator_delegations(), and get_all_hosts_method_count().
    """
    
    def __init__(self, env):
        """
        Initialize RPC data repository.
        
        Args:
            env: Odoo environment (request.env)
        """
        self.env = env
    
    def get_validator_summary(
        self,
        valoper: str,
        protocol_key: str,
        rpc_base_url: str
    ) -> Dict[str, Any]:
        """
        Get validator summary using existing _compute_validator_summary() function.
        
        Args:
            valoper: Validator operator address
            protocol_key: Protocol identifier (e.g., 'coreum', 'avalanche')
            rpc_base_url: RPC endpoint URL
        
        Returns:
            Dict with validator summary data
        """
        try:
            
            summary = _compute_validator_summary(valoper, protocol_key, rpc_base_url)
            return summary
        except Exception as e:
            _logger.exception(f"Error fetching validator summary: {e}")
            return {}
    
    def get_validator_delegations(
        self,
        valoper: str,
        protocol_key: str,
        rpc_base_url: str,
        cursor: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get validator delegations using existing _compute_validator_delegations() function.
        
        Args:
            valoper: Validator operator address
            protocol_key: Protocol identifier
            rpc_base_url: RPC endpoint URL
            cursor: Optional pagination cursor
        
        Returns:
            Dict with {items: [...], nextCursor: ...}
        """
        try:
            _logger.info(f"Calling _compute_validator_delegations with valoper={valoper}, protocol={protocol_key}, rpc={rpc_base_url}")
            delegations = _compute_validator_delegations(
                valoper, protocol_key, rpc_base_url, cursor=cursor
            )
            _logger.info(f"_compute_validator_delegations returned: {type(delegations)}, items count: {len(delegations.get('items', [])) if isinstance(delegations, dict) else 'N/A'}")
            return delegations
        except Exception as e:
            _logger.exception(f"Error fetching validator delegations: {e}")
            return {'items': [], 'nextCursor': None}
    
    def get_method_counts(
        self,
        host_id: str,
        token: str,
        number_of_days: int
    ) -> Dict[str, Any]:
        """
        Get method count breakdown using existing method count functions.
        
        Args:
            host_id: Vizion host identifier
            token: Vizion auth token
            number_of_days: Number of days to aggregate
        
        Returns:
            Dict with method count data
        """
        try:
            # Use the existing get_method_trend_for_host function
            method_data = oauth_utils.get_method_trend_for_host(
                host_id, token, number_of_days
            )
            return method_data if method_data else {}
        except Exception as e:
            _logger.exception(f"Error fetching method counts: {e}")
            return {}


class NodeRepository:
    """
    Repository for fetching subscription.node records from Odoo ORM.
    """
    
    def __init__(self, env):
        """
        Initialize node repository.
        
        Args:
            env: Odoo environment (request.env)
        """
        self.env = env
    
    def get_nodes_by_account(
        self,
        account_id: int,
        node_type: Optional[str] = None
    ) -> List[Any]:
        """
        Fetch all nodes for a given account.
        
        Args:
            account_id: User ID (res.users) who owns the subscription
            node_type: Optional filter by node type ('rpc', 'validator', etc.)
        
        Returns:
            List of subscription.node records
        """
        try:
            # Get user and their partner
            user = self.env['res.users'].sudo().search([('id', '=', account_id)], limit=1)
            if not user or not user.partner_id:
                _logger.warning(f"User {account_id} not found or has no partner")
                return []
            
            partner_id = user.partner_id.id
            
            # Filter nodes by customer_name (which points to res.partner)
            domain = [
                ('subscription_id.customer_name.id', '=', partner_id),
                ('state', '=', 'ready'),
            ]
            if node_type:
                domain.append(('node_type', '=', node_type))
            
            nodes = self.env['subscription.node'].sudo().search(domain)
            _logger.info(
                f"Fetched {len(nodes)} nodes for account {account_id} "
                f"(partner_id: {partner_id}, type filter: {node_type})"
            )
            return nodes
        except Exception as e:
            _logger.exception(f"Error fetching nodes by account: {e}")
            return []
    
    def get_node_by_id(self, node_id: str) -> Optional[Any]:
        """
        Fetch a single node by node_identifier or ID.
        
        Args:
            node_id: node_identifier (UUID) or database ID
        
        Returns:
            subscription.node record or None
        """
        try:
            # Try by node_identifier first
            node = self.env['subscription.node'].sudo().search(
                [('node_identifier', '=', node_id)],
                limit=1
            )
            if node:
                return node
            
            # Try by database ID
            try:
                node_id_int = int(node_id)
                node = self.env['subscription.node'].sudo().browse(node_id_int)
                if node.exists():
                    return node
            except (ValueError, TypeError):
                pass
            
            return None
        except Exception as e:
            _logger.exception(f"Error fetching node by ID: {e}")
            return None
    
    def get_vizion_host_id(self, node: Any, host_data_mapping: Optional[Dict[str, str]] = None) -> Optional[str]:
        """
        Extract Vizion host identifier from node record.
        
        Uses pre-built host_data_mapping if provided to avoid repeated API calls.
        If mapping not provided, queries Vizion API (not recommended for multiple nodes).
        
        Args:
            node: subscription.node record
            host_data_mapping: Pre-built mapping of node_identifier -> host_id from cached login response
        
        Returns:
            Vizion host identifier (primaryHost or hasLB) or None
        """
        try:
            # If mapping provided, use it (preferred)
            if host_data_mapping is not None:
                node_identifier = node.node_identifier
                host_id = host_data_mapping.get(node_identifier)
                if not host_id:
                    _logger.warning(f"No host ID mapping found for node {node_identifier}")
                return host_id
            
            # Fallback: query API (for single node scenarios)
            # Get customer email from subscription
            subscription = node.subscription_id
            if not subscription or not subscription.customer_name or not subscription.customer_name.email:
                _logger.warning(f"Cannot get Vizion host ID: no customer email for node {node.id}")
                return None

            login_response = oauth_utils.login_with_email(subscription.customer_name.email)
            
            if not login_response or not login_response.get('success'):
                _logger.warning(f"Vizion login failed for node {node.id}")
                return None
            
            host_data_list = login_response.get('hostData', [])
            node_identifier = node.node_identifier
            
            # Find matching host by networkId
            for host in host_data_list:
                if host.get('networkId') == node_identifier:
                    # Use hasLB if available, otherwise primaryHost
                    host_id = oauth_utils._select_host_identifier(host)
                    return host_id
            
            _logger.warning(
                f"No matching Vizion host found for node {node_identifier}"
            )
            return None
        except Exception as e:
            _logger.exception(f"Error getting Vizion host ID: {e}")
            return None
    
    def build_host_id_mapping(self, host_data_list: List[Dict[str, Any]]) -> Dict[str, str]:
        """
        Build a mapping of node_identifier (networkId) -> host_id from Vizion hostData.
        
        This mapping is used to avoid repeated API calls when looking up host IDs for multiple nodes.
        
        Args:
            host_data_list: List of hostData dicts from login_with_email response
        
        Returns:
            Dict mapping node_identifier -> primary_host_id
        """
        mapping = {}
        try:
            for host in host_data_list:
                network_id = host.get('networkId')
                if network_id:
                    # Use hasLB if available and not 'no', otherwise primaryHost
                    host_id = oauth_utils._select_host_identifier(host)
                    if host_id:
                        mapping[network_id] = host_id
                        _logger.debug(f"Mapped {network_id} -> {host_id}")
            
            _logger.info(f"Built host ID mapping for {len(mapping)} nodes")
        except Exception as e:
            _logger.exception(f"Error building host ID mapping: {e}")
        
        return mapping
