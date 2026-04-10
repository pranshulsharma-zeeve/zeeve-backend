# -*- coding: utf-8 -*-
"""
Discount API Controller
Provides endpoints for frontend to interact with discount functionality
"""
import logging
from odoo import http, fields
from odoo.http import request, Response
from odoo.addons.auth_module.utils import oauth as oauth_utils

_logger = logging.getLogger(__name__)


class DiscountAPIController(http.Controller):

    @http.route('/api/v1/discounts/available', type='http', auth='public', methods=['POST'], csrf=False)
    def get_available_discounts(self, **kwargs):
        """
        Get available discounts for a subscription plan and protocol
        """
        try:
            # Authenticate user
            user, token_err = oauth_utils._user_from_token()
            if not user:
                messages = {
                    "missing": "Missing access token",
                    "expired": "Access token expired",
                    "invalid": "Invalid access token",
                }
                return oauth_utils._json_response(
                    False,
                    error=messages.get(token_err, "Invalid access token"),
                    status=401,
                )

            request.update_env(user=user.id)
            
            # Parse request data
            data = request.httprequest.get_json(force=True, silent=True) or {}
            
            # Validate required fields
            if 'subscription_plan_id' not in data:
                return oauth_utils._json_response(False, {'error': 'Missing required field: subscription_plan_id'}, status=400)
            
            subscription_plan_id = data.get('subscription_plan_id')
            protocol_id = data.get('protocol_id')
            amount = data.get('amount', 0)
            
            # Get available discounts
            discounts = request.env['subscription.discount'].sudo().get_available_discounts(
                subscription_plan_id, protocol_id, amount
            )
            
            # Format response
            discount_list = []
            for discount in discounts:
                discount_list.append({
                    'id': discount.id,
                    'name': discount.name,
                    'code': discount.code,
                    'description': discount.description,
                    'discount_type': discount.discount_type,
                    'discount_value': discount.discount_value,
                    'minimum_amount': discount.minimum_amount,
                    'maximum_discount_amount': discount.maximum_discount_amount,
                    'valid_from': discount.valid_from.isoformat() if discount.valid_from else None,
                    'valid_until': discount.valid_until.isoformat() if discount.valid_until else None,
                    'usage_limit': discount.usage_limit,
                    'usage_count': discount.usage_count,
                    'remaining_usage': discount.remaining_usage,
                    'is_valid': discount.is_valid,
                })
            
            return oauth_utils._json_response(True, {
                'discounts': discount_list,
                'count': len(discount_list)
            })

        except Exception as e:
            _logger.error("Error getting available discounts: %s", str(e))
            return oauth_utils._json_response(False, {'error': f'Internal Server Error: {str(e)}'}, status=500)

    @http.route('/api/v1/discounts/validate', type='http', auth='public', methods=['POST'], csrf=False)
    def validate_discount_code(self, **kwargs):
        """
        Validate a discount code for a specific subscription
        """
        try:
            # Authenticate user
            user, token_err = oauth_utils._user_from_token()
            if not user:
                messages = {
                    "missing": "Missing access token",
                    "expired": "Access token expired",
                    "invalid": "Invalid access token",
                }
                return oauth_utils._json_response(
                    False,
                    error=messages.get(token_err, "Invalid access token"),
                    status=401,
                )

            request.update_env(user=user.id)
            
            # Parse request data
            data = request.httprequest.get_json(force=True, silent=True) or {}
            
            # Validate required fields
            required_fields = ['discount_code', 'subscription_plan_id']
            for field in required_fields:
                if field not in data:
                    return oauth_utils._json_response(False, {'error': f'Missing required field: {field}'}, status=400)
            
            discount_code = data.get('discount_code')
            subscription_plan_id = data.get('subscription_plan_id')
            protocol_id = data.get('protocol_id')
            amount = data.get('amount', 0)
            
            # Validate discount code
            discount, message = request.env['subscription.discount'].sudo().validate_discount_code(
                discount_code, subscription_plan_id, protocol_id, amount
            )
            
            if not discount:
                return oauth_utils._json_response(False, {'error': message}, status=422)
            
            # Calculate discount amount
            discount_amount = discount.calculate_discount_amount(amount)
            final_amount = amount - discount_amount
            
            return oauth_utils._json_response(True, {
                'valid': True,
                'discount': {
                    'id': discount.id,
                    'name': discount.name,
                    'code': discount.code,
                    'description': discount.description,
                    'discount_type': discount.discount_type,
                    'discount_value': discount.discount_value,
                    'discount_amount': discount_amount,
                    'final_amount': final_amount,
                    'original_amount': amount,
                }
            })

        except Exception as e:
            _logger.error("Error validating discount code: %s", str(e))
            return oauth_utils._json_response(False, {'error': f'Internal Server Error: {str(e)}'}, status=500)

    @http.route('/api/v1/discounts/calculate', type='http', auth='public', methods=['POST'], csrf=False)
    def calculate_discount(self, **kwargs):
        """
        Calculate discount amount for a given discount and amount
        """
        try:
            # Authenticate user
            user, token_err = oauth_utils._user_from_token()
            if not user:
                messages = {
                    "missing": "Missing access token",
                    "expired": "Access token expired",
                    "invalid": "Invalid access token",
                }
                return oauth_utils._json_response(
                    False,
                    error=messages.get(token_err, "Invalid access token"),
                    status=401,
                )

            request.update_env(user=user.id)
            
            # Parse request data
            data = request.httprequest.get_json(force=True, silent=True) or {}
            
            # Validate required fields
            required_fields = ['discount_id', 'amount']
            for field in required_fields:
                if field not in data:
                    return oauth_utils._json_response(False, {'error': f'Missing required field: {field}'}, status=400)
            
            discount_id = data.get('discount_id')
            amount = data.get('amount')
            
            # Get discount
            discount = request.env['subscription.discount'].sudo().browse(discount_id)
            if not discount.exists():
                return oauth_utils._json_response(False, {'error': 'Discount not found'}, status=404)

            if not discount.is_valid:
                return oauth_utils._json_response(False, {'error': 'Discount is not currently valid'}, status=409)
            
            # Calculate discount amount
            discount_amount = discount.calculate_discount_amount(amount)
            final_amount = amount - discount_amount
            
            return oauth_utils._json_response(True, {
                'discount_amount': discount_amount,
                'final_amount': final_amount,
                'original_amount': amount,
                'discount_percentage': (discount_amount / amount * 100) if amount > 0 else 0
            })

        except Exception as e:
            _logger.error("Error calculating discount: %s", str(e))
            return oauth_utils._json_response(False, {'error': f'Internal Server Error: {str(e)}'}, status=500)

    @http.route('/api/v1/discounts/plans/<int:plan_id>', type='http', auth='public', methods=['GET'], csrf=False)
    def get_plan_discounts(self, plan_id, **kwargs):
        """
        Get all discounts available for a specific subscription plan
        """
        try:
            # Authenticate user
            user, token_err = oauth_utils._user_from_token()
            if not user:
                messages = {
                    "missing": "Missing access token",
                    "expired": "Access token expired",
                    "invalid": "Invalid access token",
                }
                return oauth_utils._json_response(
                    False,
                    error=messages.get(token_err, "Invalid access token"),
                    status=401,
                )

            request.update_env(user=user.id)
            
            # Get subscription plan
            plan = request.env['subscription.plan'].sudo().browse(plan_id)
            if not plan.exists():
                return oauth_utils._json_response(False, {'error': 'Subscription plan not found'}, status=404)
            
            # Get available discounts for this plan
            discounts = request.env['subscription.discount'].sudo().get_available_discounts(
                plan_id, None, 0
            )
            
            # Format response
            discount_list = []
            for discount in discounts:
                discount_list.append({
                    'id': discount.id,
                    'name': discount.name,
                    'code': discount.code,
                    'description': discount.description,
                    'discount_type': discount.discount_type,
                    'discount_value': discount.discount_value,
                    'minimum_amount': discount.minimum_amount,
                    'maximum_discount_amount': discount.maximum_discount_amount,
                    'valid_from': discount.valid_from.isoformat() if discount.valid_from else None,
                    'valid_until': discount.valid_until.isoformat() if discount.valid_until else None,
                    'usage_limit': discount.usage_limit,
                    'usage_count': discount.usage_count,
                    'remaining_usage': discount.remaining_usage,
                    'is_valid': discount.is_valid,
                })
            
            return oauth_utils._json_response(True, {
                'plan_id': plan_id,
                'plan_name': plan.name,
                'discounts': discount_list,
                'count': len(discount_list)
            })

        except Exception as e:
            _logger.error("Error getting plan discounts: %s", str(e))
            return oauth_utils._json_response(False, {'error': f'Internal Server Error: {str(e)}'}, status=500)

    @http.route('/api/v1/discounts/protocols/<int:protocol_id>', type='http', auth='public', methods=['GET'], csrf=False)
    def get_protocol_discounts(self, protocol_id, **kwargs):
        """
        Get all discounts available for a specific protocol
        """
        try:
            # Authenticate user
            user, token_err = oauth_utils._user_from_token()
            if not user:
                messages = {
                    "missing": "Missing access token",
                    "expired": "Access token expired",
                    "invalid": "Invalid access token",
                }
                return oauth_utils._json_response(
                    False,
                    error=messages.get(token_err, "Invalid access token"),
                    status=401,
                )

            request.update_env(user=user.id)
            
            # Get protocol
            protocol = request.env['protocol.master'].sudo().browse(protocol_id)
            if not protocol.exists():
                return oauth_utils._json_response(False, {'error': 'Protocol not found'}, status=404)
            
            # Get available discounts for this protocol
            discounts = request.env['subscription.discount'].sudo().search([
                ('is_valid', '=', True),
                '|', ('protocol_ids', '=', False),
                ('protocol_ids', 'in', [protocol_id])
            ])
            
            # Format response
            discount_list = []
            for discount in discounts:
                discount_list.append({
                    'id': discount.id,
                    'name': discount.name,
                    'code': discount.code,
                    'description': discount.description,
                    'discount_type': discount.discount_type,
                    'discount_value': discount.discount_value,
                    'minimum_amount': discount.minimum_amount,
                    'maximum_discount_amount': discount.maximum_discount_amount,
                    'valid_from': discount.valid_from.isoformat() if discount.valid_from else None,
                    'valid_until': discount.valid_until.isoformat() if discount.valid_until else None,
                    'usage_limit': discount.usage_limit,
                    'usage_count': discount.usage_count,
                    'remaining_usage': discount.remaining_usage,
                    'is_valid': discount.is_valid,
                })
            
            return oauth_utils._json_response(True, {
                'protocol_id': protocol_id,
                'protocol_name': protocol.name,
                'discounts': discount_list,
                'count': len(discount_list)
            })

        except Exception as e:
            _logger.error("Error getting protocol discounts: %s", str(e))
            return oauth_utils._json_response(False, {'error': f'Internal Server Error: {str(e)}'}, status=500)
