# -*- coding: utf-8 -*-
"""
HTTP controller for reports endpoints.

Provides 5 REST API endpoints for generating reports:
- GET /api/v1/reports/account-weekly
- GET /api/v1/reports/rpc-fleet
- GET /api/v1/reports/rpc/<nodeId>
- GET /api/v1/reports/validator-fleet
- GET /api/v1/reports/validator/<validatorId>
"""

import logging
from odoo import http
from odoo.http import request
from ...auth_module.utils import oauth as oauth_utils
from ..utils.reports import services, models

_logger = logging.getLogger(__name__)


class ReportsController(http.Controller):
    """HTTP controller for report generation endpoints."""
    
    @http.route(
        '/api/v1/reports/account-weekly',
        type='http',
        auth='none',
        methods=['OPTIONS', 'GET'],
        csrf=False
    )
    def account_weekly_report(self, **kwargs):
        """
        Generate account weekly/monthly report.
        
        Query params:
            range: 'weekly' or 'monthly' (default: weekly)
            timezone: User's timezone (default: UTC)
        
        Returns:
            JSON response with success flag and report data
        """
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()
            
            # Authenticate user
            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            # user = request.env['res.users'].sudo().search([('id', '=', 21)],limit=1)
            # Parse query parameters
            range_type = kwargs.get('range', 'weekly')
            timezone_str = kwargs.get('timezone', 'UTC')
            
            # Validate range parameter
            if range_type not in ('weekly', 'monthly'):
                return oauth_utils._json_response(
                    False,
                    error="Invalid range parameter. Must be 'weekly' or 'monthly'.",
                    status=400
                )
            
            # Generate report
            account_id = user.id
            report = services.get_account_report(
                request.env,
                account_id,
                range_type,
                timezone_str
            )
            
            # Convert to dict
            report_dict = models.dataclass_to_dict(report)
            
            return oauth_utils._json_response(True, report_dict)
        
        except Exception as e:
            _logger.exception(f"Error generating account weekly report: {e}")
            return oauth_utils._json_response(False, error=str(e), status=500)
    
    @http.route(
        '/api/v1/reports/rpc-fleet',
        type='http',
        auth='none',
        methods=['OPTIONS', 'GET'],
        csrf=False
    )
    def rpc_fleet_report(self, **kwargs):
        """
        Generate RPC fleet report.
        
        Query params:
            range: 'weekly' or 'monthly' (default: weekly)
            timezone: User's timezone (default: UTC)
        
        Returns:
            JSON response with success flag and report data
        """
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()
            
            # Authenticate user
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            
            # user = request.env['res.users'].sudo().search([('id', '=', 21)],limit=1)

            
            # Parse query parameters
            range_type = kwargs.get('range', 'weekly')
            timezone_str = kwargs.get('timezone', 'UTC')
            
            # Validate range parameter
            if range_type not in ('weekly', 'monthly'):
                return oauth_utils._json_response(
                    False,
                    error="Invalid range parameter. Must be 'weekly' or 'monthly'.",
                    status=400
                )
            
            # Generate report
            account_id = user.id
            report = services.get_rpc_fleet_report(
                request.env,
                account_id,
                range_type,
                timezone_str
            )
            
            # Convert to dict
            report_dict = models.dataclass_to_dict(report)
            
            return oauth_utils._json_response(True, report_dict)
        
        except Exception as e:
            _logger.exception(f"Error generating RPC fleet report: {e}")
            return oauth_utils._json_response(False, error=str(e), status=500)
    
    @http.route(
        '/api/v1/reports/rpc/<string:nodeId>',
        type='http',
        auth='none',
        methods=['OPTIONS', 'GET'],
        csrf=False
    )
    def rpc_node_detail_report(self, nodeId, **kwargs):
        """
        Generate RPC node detail report.
        
        URL params:
            nodeId: Node identifier (UUID or database ID)
        
        Query params:
            range: 'weekly' or 'monthly' (default: weekly)
            timezone: User's timezone (default: UTC)
        
        Returns:
            JSON response with success flag and report data
        """
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()
            
            # Authenticate user
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            
            # user = request.env['res.users'].sudo().search([('id', '=', 30)],limit=1)

            
            # Validate nodeId
            if not nodeId:
                return oauth_utils._json_response(
                    False,
                    error='Node ID is required',
                    status=400
                )
            
            # Parse query parameters
            range_type = kwargs.get('range', 'weekly')
            timezone_str = kwargs.get('timezone', 'UTC')
            
            # Validate range parameter
            if range_type not in ('weekly', 'monthly'):
                return oauth_utils._json_response(
                    False,
                    error="Invalid range parameter. Must be 'weekly' or 'monthly'.",
                    status=400
                )
            
            # Generate report
            report = services.get_rpc_node_report(
                request.env,
                nodeId,
                range_type,
                timezone_str
            )
            
            # Convert to dict
            report_dict = models.dataclass_to_dict(report)
            
            return oauth_utils._json_response(True, report_dict)
        
        except ValueError as e:
            # Node not found or invalid ID
            return oauth_utils._json_response(False, error=str(e), status=404)
        except Exception as e:
            _logger.exception(f"Error generating RPC node detail report: {e}")
            return oauth_utils._json_response(False, error=str(e), status=500)
    
    @http.route(
        '/api/v1/reports/validator-fleet',
        type='http',
        auth='none',
        methods=['OPTIONS', 'GET'],
        csrf=False
    )
    def validator_fleet_report(self, **kwargs):
        """
        Generate validator fleet report.
        
        Query params:
            range: 'weekly' or 'monthly' (default: weekly)
            timezone: User's timezone (default: UTC)
        
        Returns:
            JSON response with success flag and report data
        """
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()
            
            # Authenticate user
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            
            # user = request.env['res.users'].sudo().search([('id', '=', 30)],limit=1)

            
            # Parse query parameters
            range_type = kwargs.get('range', 'weekly')
            timezone_str = kwargs.get('timezone', 'UTC')
            
            # Validate range parameter
            if range_type not in ('weekly', 'monthly'):
                return oauth_utils._json_response(
                    False,
                    error="Invalid range parameter. Must be 'weekly' or 'monthly'.",
                    status=400
                )
            
            # Generate report
            account_id = user.id
            report = services.get_validator_fleet_report(
                request.env,
                account_id,
                range_type,
                timezone_str
            )
            
            # Convert to dict
            report_dict = models.dataclass_to_dict(report)
            
            return oauth_utils._json_response(True, report_dict)
        
        except Exception as e:
            _logger.exception(f"Error generating validator fleet report: {e}")
            return oauth_utils._json_response(False, error=str(e), status=500)
    
    @http.route(
        '/api/v1/reports/validator/<string:validatorId>',
        type='http',
        auth='none',
        methods=['OPTIONS', 'GET'],
        csrf=False
    )
    def validator_node_detail_report(self, validatorId, **kwargs):
        """
        Generate validator node detail report.
        
        URL params:
            validatorId: Validator node identifier (UUID or database ID)
        
        Query params:
            range: 'weekly' or 'monthly' (default: weekly)
            timezone: User's timezone (default: UTC)
        
        Returns:
            JSON response with success flag and report data
        """
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()
            
            # Authenticate user
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            
            # user = request.env['res.users'].sudo().search([('id', '=', 30)],limit=1)

            
            # Validate validatorId
            if not validatorId:
                return oauth_utils._json_response(
                    False,
                    error='Validator ID is required',
                    status=400
                )
            
            # Parse query parameters
            range_type = kwargs.get('range', 'weekly')
            timezone_str = kwargs.get('timezone', 'UTC')
            
            # Validate range parameter
            if range_type not in ('weekly', 'monthly'):
                return oauth_utils._json_response(
                    False,
                    error="Invalid range parameter. Must be 'weekly' or 'monthly'.",
                    status=400
                )
            
            # Generate report
            report = services.get_validator_node_report(
                request.env,
                validatorId,
                range_type,
                timezone_str
            )
            
            # Convert to dict
            report_dict = models.dataclass_to_dict(report)
            
            return oauth_utils._json_response(True, report_dict)
        
        except ValueError as e:
            # Validator not found or invalid ID
            return oauth_utils._json_response(False, error=str(e), status=404)
        except Exception as e:
            _logger.exception(f"Error generating validator node detail report: {e}")
            return oauth_utils._json_response(False, error=str(e), status=500)
