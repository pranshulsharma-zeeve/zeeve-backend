# -*- coding: utf-8 -*-
"""
Email reporting utility functions.

Handles report email context generation, sending, and delivery tracking.
Integrates with existing services.py for report generation.
"""

import logging
from datetime import datetime, timedelta
import pytz
from typing import Dict, Any, List, Optional

from . import services
from . import models as report_models
from . import helpers

_logger = logging.getLogger(__name__)


def format_currency(value: float, decimals: int = 2) -> str:
    """Format value as currency with commas."""
    try:
        if value is None:
            return f'0.{"0" * decimals}'
        return f'{float(value):,.{decimals}f}'
    except (ValueError, TypeError):
        return f'0.{"0" * decimals}'


def format_percentage(value: float, decimals: int = 1) -> str:
    """Format value as percentage."""
    try:
        if value is None:
            return f'0.{"0" * decimals}%'
        return f'{float(value):.{decimals}f}%'
    except (ValueError, TypeError):
        return f'0.{"0" * decimals}%'


def format_count(value: float) -> str:
    """Format large numbers with commas."""
    try:
        if value is None:
            return '0'
        return '{:,}'.format(int(value))
    except (ValueError, TypeError):
        return '0'

def format_wei_value(value: float) -> str:
    """
    Format wei (smallest unit) value for display in emails.
    
    Args:
        value: Value in wei
        
    Returns:
        Formatted string with commas (e.g., "1,945,987,479,269 wei")
    """
    try:
        if value is None or value == 0:
            return "0 wei"
        return f"{int(value):,} wei"
    except (ValueError, TypeError):
        return "0 wei"


def generate_period_string(range_type: str, start_date: datetime, end_date: datetime) -> str:
    """
    Generate human-readable period string.
    
    Args:
        range_type: 'weekly' or 'monthly'
        start_date: Period start
        end_date: Period end
    
    Returns:
        Formatted period string
    """
    try:
        if range_type == 'weekly':
            return f"Week of {start_date.strftime('%B %d')} - {end_date.strftime('%B %d, %Y')}"
        else:  # monthly
            return f"{start_date.strftime('%B %d, %Y')} - {end_date.strftime('%B %d, %Y')}"
    except Exception as e:
        _logger.error(f"Error formatting period string: {e}")
        return "Report Period"


def calculate_percentage_change(current: float, previous: float) -> float:
    """Calculate percentage change between two values."""
    try:
        if previous == 0:
            return 100.0 if current > 0 else 0.0
        
        percentage = ((current - previous) / previous) * 100
        
        # Cap unrealistic percentages at 999.9% to prevent display issues
        # This usually indicates data quality problems
        if percentage > 999.9:
            _logger.warning(f"Capping unrealistic percentage change: {percentage}% (current={current}, previous={previous})")
            return 999.9
        
        return percentage
    except (ValueError, TypeError, ZeroDivisionError):
        return 0.0


def calculate_overall_growth(metric_changes: List[float]) -> float:
    """Calculate overall growth as the average of available metric growth rates."""
    valid_changes = [float(change) for change in metric_changes if change is not None]
    if not valid_changes:
        return 0.0
    return round(sum(valid_changes) / len(valid_changes), 2)


def calculate_improvements(
    current_report: report_models.AccountWeeklyReport,
    previous_report: Optional[report_models.AccountWeeklyReport]
) -> List[str]:
    """
    Generate improvement strings from report comparison.
    
    Args:
        current_report: Current period report
        previous_report: Previous period report (optional)
    
    Returns:
        List of improvement statement strings
    """
    improvements = []
    
    try:
        # Latency improvement
        if previous_report and current_report.rpcSummary and previous_report.rpcSummary:
            current_latency = current_report.rpcSummary.avgLatencyMs
            previous_latency = previous_report.rpcSummary.avgLatencyMs
            
            if previous_latency > 0:
                latency_change = ((previous_latency - current_latency) / previous_latency) * 100
                if latency_change > 0.5:
                    improvements.append(f"Reduced average latency by {latency_change:.1f}%.")
        
        # Delegator growth (from validator highlights)
        if current_report.validatorHighlights:
            total_delegators = sum(
                getattr(v, 'delegatorCount', 0) for v in current_report.validatorHighlights
            )
            if total_delegators > 0:
                improvements.append(f"Managing {total_delegators} active delegators.")
        
        # Zero critical incidents
        if current_report.incidents:
            critical_incidents = len([
                i for i in current_report.incidents 
                if getattr(i, 'severity', '').lower() == 'critical'
            ])
            if critical_incidents == 0:
                improvements.append("Zero critical incidents.")
        else:
            improvements.append("Zero critical incidents.")
        
        # High uptime achievement
        if current_report.overview and current_report.overview.overallUptimePct > 99:
            improvements.append(
                f"Maintained {current_report.overview.overallUptimePct:.1f}% uptime across all nodes."
            )
        
        # Request volume growth
        if previous_report and current_report.rpcSummary and previous_report.rpcSummary:
            request_growth = calculate_percentage_change(
                current_report.rpcSummary.totalRequests,
                previous_report.rpcSummary.totalRequests
            )
            if request_growth > 5:
                improvements.append(f"RPC request volume grew by {request_growth:.1f}%.")
    
    except Exception as e:
        _logger.error(f"Error calculating improvements: {e}")
    
    # Ensure at least one message
    if not improvements:
        improvements = ["Platform metrics remain stable."]
    
    return improvements[:5]  # Top 5 improvements


def prepare_email_context(
    env,
    user,
    range_type: str = 'weekly',
    timezone_str: str = 'UTC'
) -> Dict[str, Any]:
    """
    Generate complete email context by calling existing report services.
    
    Args:
        env: Odoo environment
        user: res.users record
        range_type: 'weekly' or 'monthly'
        timezone_str: User's timezone
    
    Returns:
        Dictionary with report data for email template
    """
    try:
        # Get user's partner ID (customer)
        if not user.partner_id:
            raise ValueError(f"User {user.id} has no associated partner")
        
        # ✅ NEW: Detect node types by searching subscription.node records
        # First, get the subscription IDs for this user's partner
        subscription_node_model = env.get('subscription.node')
        subscription_model = env.get('subscription.subscription')
        
        has_rpc = False
        has_validator = False
        domain = [("subscription_id.customer_name", "=", user.partner_id.id)]
            
        domain.append(("state", "not in", ["deleted", "closed"]))
        print("user.partner_id.id", user.partner_id.id)
        
        if user.partner_id:
            
            # Step 2: Find all nodes for these subscriptions
            nodes = subscription_node_model.sudo().search(domain)
            _logger.info(f"Found {len(nodes)} nodes for user {user.email}")
            
            if nodes:
                # Check node_type field (Selection: 'rpc', 'validator', 'archive', 'other')
                node_types = [node.node_type for node in nodes]
                has_rpc = 'rpc' in node_types
                has_validator = 'validator' in node_types
                _logger.info(f"Node types found: {node_types}")
        
        _logger.info(f"User {user.email} - has_rpc: {has_rpc}, has_validator: {has_validator}")
        
        # Generate current period report using existing service
        # This already includes previous period comparison data in the models
        current_report = services.get_account_report(
            env,
            user.id,
            range_type=range_type,
            timezone_str=timezone_str
        )
        
        # Calculate previous period bounds for period display
        period_start, period_end, prev_start, prev_end = helpers.calculate_period_bounds(
            range_type, timezone_str
        )
        
        # Calculate improvements using current report data
        improvements = calculate_improvements(current_report, None)
        
        # Calculate growth metrics using stored previous period data
        request_growth = 0.0
        rewards_growth = 0.0
        incident_change = 0
        
        # Calculate RPC request growth
        if current_report.rpcSummary:
            prev_requests = current_report.rpcSummary.prevTotalRequests or 0.0
            current_requests = current_report.rpcSummary.totalRequests or 0.0

            if prev_requests > 0:
                request_growth = calculate_percentage_change(
                    current_requests,
                    prev_requests
                )
            elif current_requests > 0:
                request_growth = 100.0
        
        # Calculate validator rewards growth (values already converted to USD)
        if current_report.validatorSummary:
            current_rewards = current_report.validatorSummary.totalRewards or 0.0
            prev_rewards = current_report.validatorSummary.prevTotalRewards or 0.0
            
            # Calculate growth directly in wei (no unit conversion needed)
            if prev_rewards is not None and prev_rewards > 0:
                rewards_growth = calculate_percentage_change(current_rewards, prev_rewards)
                if rewards_growth > 999.9:
                    _logger.warning(f"High rewards growth detected: {rewards_growth}% (current={current_rewards} wei, previous={prev_rewards} wei)")
            elif current_rewards > 0:
                # If previous was 0 but current > 0, show 100%
                rewards_growth = 100.0
                _logger.info(f"Initial rewards period: {current_rewards} wei")
        else:
            # No validator summary, rewards remain 0
            current_rewards = 0.0
        
        current_uptime = current_report.overview.overallUptimePct if current_report.overview else 0.0
        prev_uptime = (
            current_report.overview.prevOverallUptimePct
            if current_report.overview and current_report.overview.prevOverallUptimePct is not None
            else None
        )
        uptime_growth = (
            calculate_percentage_change(current_uptime, prev_uptime)
            if prev_uptime is not None else 0.0
        )

        overall_growth_components = []
        if has_rpc:
            overall_growth_components.append(request_growth)
        if has_validator:
            overall_growth_components.append(rewards_growth)
        if current_report.overview:
            overall_growth_components.append(uptime_growth)

        overall_growth = calculate_overall_growth(overall_growth_components)

        # Incident change: we don't have previous incident count stored
        # This could be added to the report model in the future
        incident_change = 0
        
        # Build email context with node type flags
        email_context = {
            'period_name': generate_period_string(range_type, period_start, period_end),
            'has_rpc': has_rpc,              # ✅ NEW
            'has_validator': has_validator,  # ✅ NEW
            'rpc_requests': current_report.rpcSummary.totalRequests if current_report.rpcSummary else 0,
            'uptime': current_report.overview.overallUptimePct if current_report.overview else 0,
            'validator_rewards': current_rewards,  # Use converted value, not raw
            'overall_growth': overall_growth,
            'request_growth': request_growth,
            'rewards_growth': rewards_growth,
            'incident_change': incident_change,
            'improvements': improvements,
            'avg_latency': current_report.rpcSummary.avgLatencyMs if current_report.rpcSummary else 0,
            'new_delegators': 0,  # Could be calculated from detailed data
            'critical_incidents': len([
                i for i in (current_report.incidents or [])
                if getattr(i, 'severity', '').lower() == 'critical'
            ]),
        }
        
        _logger.info(f"Email context generated for user {user.id}, range: {range_type}")
        return email_context
    
    except Exception as e:
        _logger.error(f"Error generating email context for user {user.id}: {e}", exc_info=True)
        raise


def send_report_email(
    env,
    user,
    range_type: str = 'weekly',
    timezone_str: str = 'UTC'
) -> Dict[str, Any]:
    """
    Send report email to user if they have active nodes.
    
    Args:
        env: Odoo environment
        user: res.users record
        range_type: 'weekly' or 'monthly'
        timezone_str: User's timezone
    
    Returns:
        Dict with {success: bool, message_id: int, error: str, skipped: bool}
    """
    result = {'success': False, 'message_id': None, 'error': None, 'skipped': False}
    
    try:
        # Refresh user record to ensure data is fresh and cursor is valid
        user = user.sudo().browse(user.id)
        if not user.exists():
            result['error'] = f"User {user.id} no longer exists"
            result['skipped'] = True
            _logger.debug(result['error'])
            return result
            
        if not user.email:
            result['error'] = f"User {user.id} has no email address"
            result['skipped'] = True
            _logger.debug(result['error'])
            return result
        
        # Generate email context (this will check if user has nodes)
        try:
            email_context = prepare_email_context(env, user, range_type, timezone_str)
        except ValueError as e:
            # User has no nodes or no data to report
            result['skipped'] = True
            _logger.info(f"Skipping user {user.id}: {e}")
            return result
        
        # Skip ONLY if user has neither RPC nor Validator nodes
        if not email_context.get('has_rpc') and not email_context.get('has_validator'):
            result['skipped'] = True
            _logger.info(f"Skipping user {user.id}: no active node types (has_rpc={email_context.get('has_rpc')}, has_validator={email_context.get('has_validator')})")
            return result
        
        # Get appropriate template
        if range_type == 'weekly':
            template_xml_id = 'zeeve_base.mail_template_zeeve_weekly_report'
        else:
            template_xml_id = 'zeeve_base.mail_template_zeeve_monthly_report'
        
        template = env.ref(template_xml_id, raise_if_not_found=False)
        if not template:
            result['error'] = f"Template {template_xml_id} not found"
            _logger.error(result['error'])
            return result
        
        # ✅ Send email with context dictionary containing node type flags
        mail_id = template.with_context(report=email_context).send_mail(
            user.id,
            force_send=True,
            email_values={'email_to': user.email}
        )
        
        result['success'] = True
        result['message_id'] = mail_id
        _logger.info(f"Report email sent to {user.email} ({user.name}), mail_id: {mail_id}")
        
    except Exception as e:
        result['error'] = str(e)
        _logger.error(f"Failed to send report email to user {user.id}: {e}", exc_info=True)
    
    return result


def log_email_delivery(env, user_id: int, range_type: str, success: bool, error: str = None):
    """
    Log email delivery status.
    
    Args:
        env: Odoo environment
        user_id: User ID
        range_type: 'weekly' or 'monthly'
        success: Whether delivery succeeded
        error: Error message if failed
    """
    try:
        # Log to system logger
        status = "SUCCESS" if success else "FAILED"
        period = datetime.now().strftime('%Y-%m-%d')
        
        log_message = f"Email Report [{range_type.upper()}] - User: {user_id}, Period: {period}, Status: {status}"
        if error:
            log_message += f", Error: {error}"
        
        if success:
            _logger.info(log_message)
        else:
            _logger.error(log_message)
        
    except Exception as e:
        _logger.error(f"Failed to log email delivery: {e}")
