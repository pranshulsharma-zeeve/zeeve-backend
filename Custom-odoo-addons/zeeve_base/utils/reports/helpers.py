# -*- coding: utf-8 -*-
"""
Helper utilities for report generation.

Provides date range calculation, timezone handling, and common utility functions.
"""

import logging
from datetime import datetime, timedelta
from typing import Tuple, Optional
import pytz

_logger = logging.getLogger(__name__)


def calculate_period_bounds(
    range_type: str,
    timezone_str: str,
    current_time: Optional[datetime] = None
) -> Tuple[datetime, datetime, datetime, datetime]:
    """
    Calculate period boundaries for weekly/monthly ranges in user's timezone.
    
    Args:
        range_type: 'weekly' or 'monthly'
        timezone_str: User's timezone (e.g., 'America/New_York', 'UTC')
        current_time: Optional current datetime (defaults to now in UTC)
    
    Returns:
        Tuple of (periodStart, periodEnd, previousPeriodStart, previousPeriodEnd)
        All timestamps are returned as timezone-aware datetime objects in the user's timezone.
    
    Weekly logic:
        - Period covers 7 days BEFORE today (excluding today)
        - If today is Feb 2, period is Jan 26 00:00:00 to Feb 1 23:59:59
        - periodEnd = end of yesterday
        - periodStart = start of 7 days before yesterday
    
    Monthly logic:
        - Period covers 30 days BEFORE today (excluding today)
        - If today is Feb 2, period is 30 days ending on Feb 1 23:59:59
        - periodEnd = end of yesterday
        - periodStart = start of 30 days before yesterday
    """
    if not current_time:
        current_time = datetime.now(pytz.UTC)
    
    # Convert to user's timezone
    try:
        user_tz = pytz.timezone(timezone_str)
    except pytz.UnknownTimeZoneError:
        _logger.warning(f"Unknown timezone '{timezone_str}', falling back to UTC")
        user_tz = pytz.UTC
    
    # Convert current time to user timezone
    current_in_tz = current_time.astimezone(user_tz)
    
    if range_type == 'weekly':
        # Use last 7 days BEFORE today (excluding today)
        # If today is Feb 2, period is Jan 26 to Feb 1
        yesterday = current_in_tz - timedelta(days=1)
        period_end = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)
        
        # Period start is 6 days before period_end (7 days total)
        period_start = (period_end - timedelta(days=6)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        
        # Previous period is 7 days before current period
        previous_period_end = period_start - timedelta(microseconds=1)
        previous_period_start = (previous_period_end - timedelta(days=6)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    
    elif range_type == 'monthly':
        # Use last 30 days BEFORE today (excluding today)
        # If today is Feb 2, period is 30 days ending on Feb 1
        yesterday = current_in_tz - timedelta(days=1)
        period_end = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)
        
        # Period start is 29 days before period_end (30 days total)
        period_start = (period_end - timedelta(days=29)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        
        # Previous period is 30 days before current period
        previous_period_end = period_start - timedelta(microseconds=1)
        previous_period_start = (previous_period_end - timedelta(days=29)).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
    
    else:
        raise ValueError(f"Invalid range_type: {range_type}. Must be 'weekly' or 'monthly'")
    
    _logger.info(
        f"Calculated period bounds for {range_type} in {timezone_str}: "
        f"period=[{period_start} to {period_end}], "
        f"previous=[{previous_period_start} to {previous_period_end}]"
    )
    
    return period_start, period_end, previous_period_start, previous_period_end


def _get_last_day_of_month(year: int, month: int) -> int:
    """Return the last day number of the given month."""
    if month == 12:
        next_month = datetime(year + 1, 1, 1)
    else:
        next_month = datetime(year, month + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    return last_day


def format_date_for_response(dt: datetime) -> str:
    """
    Format datetime as ISO 8601 date string for API response.
    
    Args:
        dt: Timezone-aware datetime
    
    Returns:
        ISO date string (YYYY-MM-DD)
    """
    return dt.strftime('%Y-%m-%d')


def format_datetime_for_response(dt: datetime) -> str:
    """
    Format datetime as ISO 8601 datetime string for API response.
    
    Args:
        dt: Timezone-aware datetime
    
    Returns:
        ISO datetime string (YYYY-MM-DDTHH:MM:SSZ)
    """
    # Convert to UTC for API response
    dt_utc = dt.astimezone(pytz.UTC)
    return dt_utc.strftime('%Y-%m-%dT%H:%M:%SZ')


def safe_float(value, default=0.0) -> float:
    """Safely convert value to float, returning default if conversion fails."""
    try:
        return float(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def safe_int(value, default=0) -> int:
    """Safely convert value to int, returning default if conversion fails."""
    try:
        return int(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def generate_insight_id() -> str:
    """Generate a unique insight ID."""
    import uuid
    return f"insight_{uuid.uuid4().hex[:8]}"


def generate_incident_id() -> str:
    """Generate a unique incident ID."""
    import uuid
    return f"incident_{uuid.uuid4().hex[:8]}"


def get_days_in_range(range_type: str) -> int:
    """
    Get number of days in the given range type.
    
    Args:
        range_type: 'weekly' or 'monthly'
    
    Returns:
        Number of days (7 for weekly, 30 for monthly)
    """
    if range_type == 'weekly':
        return 7
    elif range_type == 'monthly':
        return 30  # Approximate for monthly
    else:
        raise ValueError(f"Invalid range_type: {range_type}")


def get_last_interval_date(creation_date):
    """
    Get the last occurrence of weekly, monthly, and quarterly intervals based on creation date.
    
    Calculates the most recent interval boundary dates relative to today,
    considering the node's creation date as the starting point.
    
    Args:
        creation_date: Node creation date (datetime object or string ISO format)
    
    Returns:
        Dict with keys 'weekly', 'monthly', 'quarterly' containing date objects
    """
    # Parse creation date if string
    if isinstance(creation_date, str):
        try:
            creation = datetime.fromisoformat(creation_date.replace('Z', '+00:00'))
        except Exception:
            creation = datetime.fromisoformat(creation_date)
    else:
        creation = creation_date
    
    # Ensure datetime objects
    if not hasattr(creation, 'time'):
        creation = datetime.combine(creation, datetime.min.time())
    
    # Normalize to midnight UTC
    creation = creation.replace(hour=0, minute=0, second=0, microsecond=0)
    
    today = datetime.now(pytz.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    
    def get_last_date(interval_days: int) -> datetime:
        """Get last occurrence of N-day interval."""
        if today < creation:
            return creation
        diff_days = (today - creation).days
        if diff_days < interval_days:
            return creation
        intervals_passed = diff_days // interval_days
        last_date = creation + timedelta(days=intervals_passed * interval_days)
        return last_date if last_date <= today else creation
    
    def get_last_monthly_date(months: int) -> datetime:
        """Get last occurrence of N-month interval."""
        if today < creation:
            return creation
        last = creation
        original_day = creation.day
        while True:
            next_date = last
            for _ in range(months):
                if next_date.month == 12:
                    next_date = next_date.replace(year=next_date.year + 1, month=1)
                else:
                    next_date = next_date.replace(month=next_date.month + months)
            try:
                if next_date.day < original_day:
                    next_date = next_date.replace(day=original_day)
            except ValueError:
                next_date = (next_date.replace(day=1) - timedelta(days=1))
            if next_date > today:
                break
            last = next_date
        return last if last >= creation else creation
    
    return {
        'weekly': get_last_date(7),
        'monthly': get_last_monthly_date(1),
        'quarterly': get_last_monthly_date(3)
    }


def calculate_change(current: float, previous: float) -> float:
    """
    Calculate absolute change between current and previous values.
    
    Args:
        current: Current period value
        previous: Previous period value
    
    Returns:
        Absolute change (current - previous), rounded to 2 decimal places
    """
    if current is None or previous is None:
        return 0.0
    return round(float(current) - float(previous), 2)


def calculate_change_percent(current: float, previous: float) -> float:
    """
    Calculate percentage change between current and previous values.
    
    Safely handles cases where previous is zero.
    
    Args:
        current: Current period value
        previous: Previous period value
    
    Returns:
        Percentage change ((current - previous) / previous * 100), rounded to 2 decimal places
        Returns 0.0 if previous is zero
    """
    if current is None or previous is None:
        return 0.0
    
    current = float(current)
    previous = float(previous)
    
    if previous == 0:
        # If previous was 0 and current is positive, represent as growth
        # If both 0, no change
        return 0.0
    
    return round(((current - previous) / previous) * 100, 2)


def calculate_gini_coefficient(values: list) -> float:
    """
    Calculate Gini coefficient (inequality measure) for a list of values.
    
    Gini coefficient ranges from 0 (perfect equality) to 1 (perfect inequality).
    Used to measure stake concentration across validators.
    
    Formula: G = (2 * Σ(i * x_i)) / (n * Σ(x_i)) - (n + 1) / n
    where x_i are sorted values in ascending order, i is 1-indexed position
    
    Args:
        values: List of numeric values (e.g., validator stakes)
    
    Returns:
        Gini coefficient as float (0.0 to 1.0), rounded to 3 decimal places
    """
    if not values or len(values) == 0:
        return 0.0
    
    # Convert to float and sort ascending
    float_values = [float(v) for v in values if v is not None and float(v) >= 0]
    
    if len(float_values) == 0:
        return 0.0
    
    # If only one value, no inequality
    if len(float_values) == 1:
        return 0.0
    
    float_values.sort()
    n = len(float_values)
    sum_values = sum(float_values)
    
    # If all values are zero, no inequality
    if sum_values == 0:
        return 0.0
    
    # Calculate Gini coefficient
    sum_weighted = sum((i + 1) * float_values[i] for i in range(n))
    gini = (2 * sum_weighted) / (n * sum_values) - (n + 1) / n
    
    # Clamp to [0, 1] range and round
    return round(max(0.0, min(1.0, gini)), 3)


def gini_to_concentration_level(gini: float) -> str:
    """
    Map Gini coefficient to concentration risk level.
    
    Thresholds:
    - low: Gini < 0.3 (good distribution)
    - medium: 0.3 <= Gini < 0.5 (moderate concentration)
    - high: Gini >= 0.5 (high concentration)
    
    Args:
        gini: Gini coefficient (0.0 to 1.0)
    
    Returns:
        Risk level string: 'low', 'medium', or 'high'
    """
    if gini is None or gini < 0:
        return 'low'
    
    if gini < 0.3:
        return 'low'
    elif gini < 0.5:
        return 'medium'
    else:
        return 'high'
