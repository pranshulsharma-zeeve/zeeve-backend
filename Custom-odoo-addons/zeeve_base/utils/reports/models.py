# -*- coding: utf-8 -*-
"""
Response data models (DTOs) for report endpoints.

Defines dataclass structures matching the exact API response schemas
for all 5 report types. These models are used by both API controllers
and email templates.
"""

from dataclasses import dataclass, field
from typing import List, Optional


# ============================================================================
# Common nested structures
# ============================================================================

@dataclass
class ReportMeta:
    """Metadata common to all reports."""
    accountId: str
    accountName: str
    periodStart: str  # ISO date
    periodEnd: str  # ISO date
    range: str  # 'weekly' | 'monthly'
    timezone: str
    # Optional fields for specific reports
    nodeId: Optional[str] = None
    nodeName: Optional[str] = None
    validatorId: Optional[str] = None
    validatorName: Optional[str] = None


@dataclass
class Incident:
    """Incident data structure."""
    id: str
    severity: str  # 'low' | 'medium' | 'high'
    title: str
    description: Optional[str] = None
    nodeName: Optional[str] = None
    validatorName: Optional[str] = None
    startTime: Optional[str] = None  # ISO datetime
    endTime: Optional[str] = None  # ISO datetime
    duration: Optional[float] = None  # hours or minutes
    type: Optional[str] = None  # 'slashing' | 'jailing' | 'downtime'


@dataclass
class Insight:
    """Insight/recommendation data structure."""
    id: str
    title: str
    recommendation: str
    impact: str  # 'high' | 'medium' | 'low'
    description: Optional[str] = None


@dataclass
class TrendDataPoint:
    """Daily trend data point."""
    date: str  # ISO date
    totalRequests: Optional[float] = None
    totalRewards: Optional[float] = None
    totalStake: Optional[float] = None
    overallUptimePct: Optional[float] = None
    avgUptimePct: Optional[float] = None
    avgLatencyMs: Optional[float] = None
    avgAPR: Optional[float] = None
    errorRatePct: Optional[float] = None
    uptimePct: Optional[float] = None
    latencyMs: Optional[float] = None
    requestCount: Optional[float] = None
    stake: Optional[float] = None
    rewards: Optional[float] = None
    apr: Optional[float] = None


# ============================================================================
# Account Weekly Report
# ============================================================================

@dataclass
class AccountOverview:
    """Overview section for account weekly report."""
    totalNodes: int
    overallUptimePct: float
    totalRequests: float
    totalRewards: float
    rewardsDelta: float
    overallStatus: str  # 'good' | 'warning' | 'critical'
    prevOverallUptimePct: Optional[float] = None


@dataclass
class RpcSummary:
    """RPC summary for account weekly report."""
    totalNodes: int
    healthyNodes: int
    criticalNodes: int
    avgUptimePct: float
    avgLatencyMs: float
    errorRatePct: float
    totalRequests: float
    prevTotalRequests: Optional[float] = None  # Previous period total for growth calculation


@dataclass
class ValidatorSummary:
    """Validator summary for account weekly report."""
    totalValidators: int
    healthyNodes: int
    criticalNodes: int
    totalStake: float
    totalRewards: float
    avgAPR: float
    avgUptimePct: float
    jailedCount: int
    prevTotalRewards: Optional[float] = None  # Previous period total for growth calculation
    prevTotalStake: Optional[float] = None  # Previous period total for growth calculation


@dataclass
class RpcHighlight:
    """RPC node highlight for account weekly report."""
    nodeId: str
    nodeName: str
    uptimePct: float
    latencyMs: float
    requestCount: float
    errorCount: float
    errorRatePct: float
    status: str  # 'good' | 'warning' | 'critical'


@dataclass
class ValidatorHighlight:
    """Validator highlight for account weekly report."""
    validatorId: str
    validatorName: str
    stake: float
    rewards: float
    apr: float
    uptimePct: float
    jailed: bool
    status: str  # 'good' | 'warning' | 'critical'


@dataclass
class AccountWeeklyReport:
    """Complete account weekly report response."""
    meta: ReportMeta
    overview: AccountOverview
    rpcSummary: RpcSummary
    validatorSummary: ValidatorSummary
    rpcHighlights: List[RpcHighlight]
    validatorHighlights: List[ValidatorHighlight]
    incidents: List[Incident]
    insights: List[Insight]
    trends: List[TrendDataPoint]


# ============================================================================
# RPC Fleet Report
# ============================================================================

@dataclass
class RpcFleetSummary:
    """Summary section for RPC fleet report."""
    totalNodes: int
    healthyNodes: int
    warningNodes: int
    criticalNodes: int
    avgUptimePct: float
    avgLatencyMs: float
    totalRequests: float
    totalErrors: float
    errorRatePct: float
    requestsDeltaPct: float
    status: str  # 'good' | 'warning' | 'critical'


@dataclass
class RpcNodeItem:
    """Individual RPC node for fleet report."""
    nodeId: str
    nodeName: str
    status: str  # 'good' | 'warning' | 'critical'
    uptimePct: float
    latencyMs: float
    requestCount: float
    errorCount: float
    errorRatePct: float


@dataclass
class HealthMix:
    """Health distribution for fleet reports."""
    good: int
    warning: int
    critical: int


@dataclass
class RpcFleetReport:
    """Complete RPC fleet report response."""
    meta: ReportMeta
    summary: RpcFleetSummary
    nodes: List[RpcNodeItem]
    healthMix: HealthMix
    incidents: List[Incident]
    insights: List[Insight]
    trends: List[TrendDataPoint]


# ============================================================================
# RPC Node Detail Report
# ============================================================================

@dataclass
class RpcNodeOverview:
    """Overview section for RPC node detail report."""
    status: str  # 'good' | 'warning' | 'critical'


@dataclass
class RpcNodeMetrics:
    """Metrics section for RPC node detail report."""
    uptimePct: float
    uptimeChangePercent: float
    latencyMs: float
    latencyChangePercent: float
    requestCount: float
    requestChangePercent: float
    errorCount: float
    errorRatePct: float
    errorChangePercent: float


@dataclass
class SecurityInfo:
    """Security information for RPC node detail report."""
    ddosProtection: Optional[bool] = None
    firewallEnabled: bool = True  # Default to True
    lastSecurityCheck: Optional[str] = None  # ISO date from weekly interval


@dataclass
class MethodBreakdownItem:
    """Method breakdown item for RPC node detail report."""
    method: str
    callCount: float
    callPercent: float
    avgLatencyMs: float
    errorCount: float
    errorRatePct: float


@dataclass
class BenchmarksInfo:
    """Benchmarks vs network for RPC node detail report."""
    uptimeVsNetwork: float  # percentage difference
    latencyVsNetwork: float  # percentage difference
    reliabilityVsNetwork: float  # percentage difference


@dataclass
class RpcNodeDetailReport:
    """Complete RPC node detail report response."""
    meta: ReportMeta
    overview: RpcNodeOverview
    metrics: RpcNodeMetrics
    security: SecurityInfo
    methodBreakdown: List[MethodBreakdownItem]
    benchmarks: BenchmarksInfo
    incidents: List[Incident]
    insights: List[Insight]
    trends: List[TrendDataPoint]


# ============================================================================
# Validator Fleet Report
# ============================================================================

@dataclass
class ValidatorFleetSummary:
    """Summary section for validator fleet report."""
    totalValidators: int
    activeValidators: int
    healthyNodes: int
    warningNodes: int
    criticalNodes: int
    jailedValidators: int
    totalStake: float
    totalRewards: float
    avgAPR: float
    status: str  # 'good' | 'warning' | 'critical'


@dataclass
class ValidatorNodeItem:
    """Individual validator for fleet report."""
    validatorId: str
    validatorName: str
    status: str  # 'good' | 'warning' | 'critical'
    stake: float
    rewards: float
    apr: float
    uptimePct: float
    jailed: bool
    slashingEvents: int


@dataclass
class RiskIndicators:
    """Risk indicators for validator fleet report."""
    slashingRisk: str  # 'low' | 'medium' | 'high'
    jailingRisk: str  # 'low' | 'medium' | 'high'
    stakeConcentration: str  # 'low' | 'medium' | 'high'


@dataclass
class ValidatorFleetReport:
    """Complete validator fleet report response."""
    meta: ReportMeta
    summary: ValidatorFleetSummary
    validators: List[ValidatorNodeItem]
    healthMix: HealthMix
    riskIndicators: RiskIndicators
    incidents: List[Incident]
    insights: List[Insight]
    trends: List[TrendDataPoint]


# ============================================================================
# Validator Node Detail Report
# ============================================================================

@dataclass
class ValidatorNodeOverview:
    """Overview section for validator node detail report."""
    status: str  # 'good' | 'warning' | 'critical'
    stakeDelta: float
    rewardsDelta: float


@dataclass
class ValidatorMetrics:
    """Metrics section for validator node detail report."""
    stake: float
    stakeChange: float
    stakeChangePercent: float
    rewards: float
    rewardsChangePercent: float
    apr: float
    aprChange: float
    uptimePct: float
    jailed: bool
    slashingEvents: int


@dataclass
class TopDelegator:
    """Top delegator item for validator node detail report."""
    delegatorAddress: str
    delegatedStake: float
    delegatePercentOfValidator: float
    joinedDate: Optional[str] = None  # ISO date


@dataclass
class DelegatorsInfo:
    """Delegators section for validator node detail report."""
    totalCount: int
    topDelegators: List[TopDelegator]


@dataclass
class NetworkComparison:
    """Network comparison for validator node detail report."""
    uptimeVsNetwork: float  # percentage difference
    rewardsVsNetwork: float  # percentage difference
    aprVsNetwork: float  # percentage difference
    reliabilityVsNetwork: float  # percentage difference


@dataclass
class ValidatorNodeDetailReport:
    """Complete validator node detail report response."""
    meta: ReportMeta
    overview: ValidatorNodeOverview
    metrics: ValidatorMetrics
    delegators: DelegatorsInfo
    networkComparison: NetworkComparison
    incidents: List[Incident]
    insights: List[Insight]
    trends: List[TrendDataPoint]


# ============================================================================
# Utility functions for dataclass to dict conversion
# ============================================================================

def dataclass_to_dict(obj):
    """
    Convert a dataclass instance to a dictionary recursively.
    
    Handles nested dataclasses, lists, and None values.
    """
    if obj is None:
        return None
    
    if isinstance(obj, list):
        return [dataclass_to_dict(item) for item in obj]
    
    if not hasattr(obj, '__dataclass_fields__'):
        return obj
    
    result = {}
    for field_name in obj.__dataclass_fields__:
        value = getattr(obj, field_name)
        if isinstance(value, list):
            result[field_name] = [dataclass_to_dict(item) for item in value]
        elif hasattr(value, '__dataclass_fields__'):
            result[field_name] = dataclass_to_dict(value)
        else:
            result[field_name] = value
    
    return result
