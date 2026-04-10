# -*- coding: utf-8 -*-
"""
Aggregation utilities for time-series data.

Provides functions to aggregate raw Vizion data (per-day/per-node) into
summary metrics using SUM, AVG, LAST, COUNT strategies.
"""

import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

_logger = logging.getLogger(__name__)


class AggregationType:
    """Supported aggregation types."""
    SUM = 'sum'
    AVG = 'avg'
    LAST = 'last'
    COUNT = 'count'
    MIN = 'min'
    MAX = 'max'


def aggregate_series(
    data_array: List[Dict[str, Any]],
    value_key: str,
    agg_type: str = AggregationType.AVG,
    default_value: Any = 0
) -> Any:
    """
    Aggregate a time-series array of data points.
    
    Args:
        data_array: List of data points (e.g., [{date: '2026-02-03', value: 100}, ...])
        value_key: Key to extract value from each data point (e.g., 'value', 'uptimePct')
        agg_type: Aggregation type ('sum', 'avg', 'last', 'count', 'min', 'max')
        default_value: Value to return if data_array is empty
    
    Returns:
        Aggregated value based on agg_type
    
    Examples:
        >>> data = [{'value': 100}, {'value': 200}, {'value': 150}]
        >>> aggregate_series(data, 'value', AggregationType.SUM)
        450
        >>> aggregate_series(data, 'value', AggregationType.AVG)
        150.0
        >>> aggregate_series(data, 'value', AggregationType.LAST)
        150
    """
    if not data_array:
        return default_value
    
    values = []
    for entry in data_array:
        val = entry.get(value_key)
        if val is not None:
            try:
                values.append(float(val))
            except (ValueError, TypeError):
                _logger.warning(f"Skipping non-numeric value for key '{value_key}': {val}")
                continue
    
    if not values:
        return default_value
    
    if agg_type == AggregationType.SUM:
        return sum(values)
    elif agg_type == AggregationType.AVG:
        return sum(values) / len(values)
    elif agg_type == AggregationType.LAST:
        return values[-1]
    elif agg_type == AggregationType.COUNT:
        return len(values)
    elif agg_type == AggregationType.MIN:
        return min(values)
    elif agg_type == AggregationType.MAX:
        return max(values)
    else:
        _logger.error(f"Unknown aggregation type: {agg_type}")
        return default_value


def aggregate_multiple_series(
    series_dict: Dict[str, List[Dict[str, Any]]],
    value_key: str,
    agg_type: str = AggregationType.SUM
) -> float:
    """
    Aggregate multiple time-series (e.g., multiple methods, multiple nodes).
    
    Args:
        series_dict: Dict of series name -> list of data points
        value_key: Key to extract value from each data point
        agg_type: Aggregation type
    
    Returns:
        Total aggregated value across all series
    
    Example:
        >>> series = {
        ...     'eth_call': [{'value': 100}, {'value': 200}],
        ...     'eth_getBalance': [{'value': 50}, {'value': 75}]
        ... }
        >>> aggregate_multiple_series(series, 'value', AggregationType.SUM)
        425.0
    """
    total = 0.0
    for series_name, data_array in series_dict.items():
        series_total = aggregate_series(data_array, value_key, agg_type, default_value=0)
        total += series_total
    return total


def calculate_percentage_change(current: float, previous: float) -> float:
    """
    Calculate percentage change between two values.
    
    Args:
        current: Current period value
        previous: Previous period value
    
    Returns:
        Percentage change (positive = increase, negative = decrease)
        Returns 0.0 if previous is 0
    
    Example:
        >>> calculate_percentage_change(150, 100)
        50.0
        >>> calculate_percentage_change(80, 100)
        -20.0
    """
    if previous == 0:
        return 0.0 if current == 0 else 100.0
    return ((current - previous) / previous) * 100.0


def bucket_by_date(
    data_array: List[Dict[str, Any]],
    date_key: str = 'date'
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Bucket time-series data by date.
    
    Args:
        data_array: List of data points with date field
        date_key: Key containing the date value
    
    Returns:
        Dict of date string -> list of data points for that date
    
    Example:
        >>> data = [
        ...     {'date': '2026-02-03', 'value': 100},
        ...     {'date': '2026-02-03', 'value': 150},
        ...     {'date': '2026-02-04', 'value': 200}
        ... ]
        >>> bucket_by_date(data)
        {'2026-02-03': [{'date': '2026-02-03', 'value': 100}, ...], ...}
    """
    buckets = {}
    for entry in data_array:
        date_val = entry.get(date_key)
        if date_val:
            date_str = str(date_val)
            if date_str not in buckets:
                buckets[date_str] = []
            buckets[date_str].append(entry)
    return buckets


def aggregate_per_day(
    data_array: List[Dict[str, Any]],
    value_key: str,
    agg_type: str = AggregationType.AVG,
    date_key: str = 'date'
) -> List[Dict[str, Any]]:
    """
    Aggregate time-series data per day.
    
    Args:
        data_array: List of data points with date field
        value_key: Key to aggregate
        agg_type: Aggregation type
        date_key: Key containing the date value
    
    Returns:
        List of {date: str, value: float} aggregated per day
    
    Example:
        >>> data = [
        ...     {'date': '2026-02-03', 'value': 100},
        ...     {'date': '2026-02-03', 'value': 150},
        ...     {'date': '2026-02-04', 'value': 200}
        ... ]
        >>> aggregate_per_day(data, 'value', AggregationType.AVG)
        [{'date': '2026-02-03', 'value': 125.0}, {'date': '2026-02-04', 'value': 200.0}]
    """
    buckets = bucket_by_date(data_array, date_key)
    result = []
    for date_str, entries in sorted(buckets.items()):
        aggregated_value = aggregate_series(entries, value_key, agg_type, default_value=0)
        result.append({
            'date': date_str,
            value_key: aggregated_value
        })
    return result


def calculate_avg_across_nodes(
    nodes_data: List[Dict[str, Any]],
    value_key: str
) -> float:
    """
    Calculate average of a metric across multiple nodes.
    
    Args:
        nodes_data: List of node data dicts
        value_key: Key to average
    
    Returns:
        Average value
    """
    values = []
    for node in nodes_data:
        val = node.get(value_key)
        if val is not None:
            try:
                values.append(float(val))
            except (ValueError, TypeError):
                continue
    
    if not values:
        return 0.0
    return sum(values) / len(values)
