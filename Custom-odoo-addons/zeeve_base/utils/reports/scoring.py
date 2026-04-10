# -*- coding: utf-8 -*-
"""Shared report classification helpers."""


def determine_risk_level(metric_value: float, threshold_medium: float, threshold_high: float) -> str:
    """
    Determine risk level based on metric value and thresholds.
    
    Args:
        metric_value: Value to evaluate
        threshold_medium: Threshold for medium risk
        threshold_high: Threshold for high risk
    
    Returns:
        Risk level: 'low', 'medium', 'high'
    
    Example:
        >>> determine_risk_level(2, 3, 5)  # 2 slashing events
        'low'
        >>> determine_risk_level(4, 3, 5)
        'medium'
        >>> determine_risk_level(6, 3, 5)
        'high'
    """
    if metric_value >= threshold_high:
        return 'high'
    elif metric_value >= threshold_medium:
        return 'medium'
    else:
        return 'low'
