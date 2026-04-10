# -*- coding: utf-8 -*-
from odoo import http, fields
from odoo.http import request
import logging
import requests
import random
import string
from ...auth_module.utils import oauth as oauth_utils

_logger = logging.getLogger(__name__)


class VizionUserController(http.Controller):

    @http.route('/api/create-user', type='http', auth='public', methods=['OPTIONS','GET'], csrf=False)
    def create_vizion_user(self, **kwargs):
        """
        Creates a Vizion user via external API using current user's email.
        Equivalent to Node.js /create-user route.
        """
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            partner = user.partner_id

            result = oauth_utils.create_vizion_user(user)
            return oauth_utils._json_response(
                result.get("success", False),
                data=result.get("data", {}),
                error="Vizion user created successfully" if result.get("success") else result.get("error", ""),
                status=result.get("status", 200)
            )

        except Exception as e:
            _logger.exception("Error while creating Vizion user: %s", str(e))
            return oauth_utils._json_response(False, data={}, error=str(e), status=500)

    @http.route('/api/auth/login-with-email', type='http', auth='public', methods=['OPTIONS','POST'], csrf=False)
    def login_vizion_user(self, **kwargs):
        """
        get a Vizion user via external API using current user's email.
        Equivalent to Node.js /get-user route.
        """
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            partner = user.partner_id
            user_email = partner.email

            vision_base_url = request.env['ir.config_parameter'].sudo().get_param('vision_base_url')
            origin = request.env['ir.config_parameter'].sudo().get_param('backend_url')
            headers = {
                "Content-Type": "application/json",
                "Origin": origin,          # frontend_url from Odoo system params
                "Referer": origin,         # optional, but often useful
            }
            # External API endpoint
            vision_api_url = f"{vision_base_url}/api/auth/login-with-email"

            payload = {
                "username": user_email,
            }

            # Send POST request to Vision API
            response = requests.post(vision_api_url, json=payload, headers=headers, timeout=20)

            if response.status_code not in (200, 201):
                _logger.error("Vision API error: %s - %s", response.status_code, response.text)
                return oauth_utils._json_response(
                    False,
                    data={},
                    error=f"Vision API returned {response.status_code}: {response.text}",
                    status=502,
                )

            result = response.json()

            # data= {
            #     "success": True,
            #     "data": result,
            #     "email": user_email,
            # }
            return oauth_utils._json_response(True, data=result, error="Vizion user fetched successfully")

        except Exception as e:
            _logger.exception("Error while fetching Vizion user: %s", str(e))
            return oauth_utils._json_response(False, data={}, error=str(e), status=500)



    @http.route('/api/vizion/get-user-id', type='http', auth='public', methods=['OPTIONS','GET'], csrf=False)
    def get_vizion_user_id(self, **kwargs):
        """
        get a Vizion user via external API using current user's email.
        Equivalent to Node.js /get-user route.
        """
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            partner = user.partner_id
            user_email = partner.email

            vision_base_url = request.env['ir.config_parameter'].sudo().get_param('vision_base_url')
            # External API endpoint
            vision_api_url = f"{vision_base_url}/api/auth/get-user"

            payload = {
                "username": user_email,
            }
            origin = request.env['ir.config_parameter'].sudo().get_param('backend_url')
            headers = {
                "Content-Type": "application/json",
                "Origin": origin,          # frontend_url from Odoo system params
                "Referer": origin,         # optional, but often useful
            }

            # Send POST request to Vision API
            response = requests.post(vision_api_url, json=payload, headers=headers, timeout=20)

            if response.status_code not in (200, 201):
                _logger.error("Vision API error: %s - %s", response.status_code, response.text)
                return oauth_utils._json_response(
                    False,
                    data={},
                    error=f"Vision API returned {response.status_code}: {response.text}",
                    status=502,
                )

            result = response.json()

            # data= {
            #     "success": True,
            #     "data": result,
            #     "email": user_email,
            # }
            return oauth_utils._json_response(True, data=result, error="Vizion User Id fetched successfully")

        except Exception as e:
            _logger.exception("Error while fetching Vizion user Id: %s", str(e))
            return oauth_utils._json_response(False, data={}, error=str(e), status=500)


    @http.route('/api/test-login-with-email', type='http', auth='public', methods=['OPTIONS','GET'], csrf=False)
    def test_login_with_email(self, **kwargs):
        """
        Test endpoint to call login_with_email method with a specific email and log the response.
        No authentication required - just calls the function and logs the result.
        """
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()
            
            # Call login_with_email with the specified email
            test_email = "hello@optimisticlabsltd.com"
            response = oauth_utils.login_with_email(test_email)
            
            # Log the response
            _logger.info("Test login_with_email response for %s: %s", test_email, response)
            
            # Return the response
            return oauth_utils._json_response(
                True,
                data={"response": response, "email": test_email},
                error="Test completed successfully",
                status=200
            )

        except Exception as e:
            _logger.exception("Error in test_login_with_email: %s", str(e))
            return oauth_utils._json_response(False, data={}, error=str(e), status=500)


    @http.route('/api/v1/generate-token', type='http', auth='public', methods=['OPTIONS','GET'], csrf=False)
    def generate_token_external_method(self, **kwargs):
        """
        Test endpoint to generate an external JWT token for user ID 10.
        This token can be used to test the method count API.
        """
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()
            
            email = "hello@optimisticlabsltd.com"
            # Generate external access token
            token = oauth_utils.generate_external_access_token(email)
            
            # Log the token
            _logger.info("Generated external token for email %s: %s", email, token)
            
            # Return the token and method count response
            return oauth_utils._json_response(
                True,
                data={
                    "token": token,
                },
                error="Token generated",
                status=200
            )

        except Exception as e:
            _logger.exception("Error in test_generate_token: %s", str(e))
            return oauth_utils._json_response(False, data={}, error=str(e), status=500)


    @http.route("/api/method-count", type='http', auth='public', methods=['OPTIONS','GET'], csrf=False)
    def get_method_count(self, **kwargs):
        """
        Get method count trend for a specific node by node name.
        If node_name is not provided or not found, returns method counts for all hosts.
        
        Args:
            node_name: Name of the node to search for (optional)
            
        Returns:
            JSON response with summed method counts for all hosts or detailed per-method data for a specific node
        """
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()
            
            # Verify JWT token
            payload, error, token = oauth_utils.jwt_verification_external()
            if error:
                error_messages = {
                    "missing": "Missing authorization token",
                    "invalid": "Invalid authorization token",
                    "missing_secret": "JWT secret not configured",
                    "error": "Error verifying token"
                }
                _logger.error("JWT verification failed: %s", error)
                return oauth_utils._json_response(
                    False,
                    data={},
                    error=error_messages.get(error, "Unauthorized"),
                    status=401
                )
            
            _logger.info("JWT verified successfully. Payload: %s", payload)
            email = payload.get('email', '')
            user = request.env['res.users'].sudo().search([('login', '=', email)], limit=1)
            if not user:
                _logger.error("User not found for email from token: %s", email)
                return oauth_utils._json_response(
                    False,
                    data={},
                    error="User not found",
                    status=404
                )
  
            # Search for the node in subscription.node model
            node_name = kwargs.get('node_name')
            number_of_days_param = kwargs.get('range')
            try:
                number_of_days = int(number_of_days_param) if number_of_days_param is not None else 1
            except (TypeError, ValueError):
                _logger.warning("Invalid range parameter received: %s", number_of_days_param)
                return oauth_utils._json_response(
                    False,
                    data={},
                    error="Invalid 'range' parameter. Please send a positive integer.",
                    status=400,
                )

            if number_of_days <= 0:
                return oauth_utils._json_response(
                    False,
                    data={},
                    error="'range' must be a positive integer.",
                    status=400,
                )
            node = None
            if node_name:
                node = request.env['subscription.node'].sudo().search([('node_name', '=', node_name)], limit=1)
            
            # If no node_name provided or node not found, get all hosts
            if not node_name or not node:
                _logger.info("Node name not provided or not found, fetching all hosts")
                return oauth_utils.get_all_hosts_method_count(number_of_days)
            
            node_identifier = node.node_identifier
            _logger.info("Found node: %s with identifier: %s", node_name, node_identifier)
            
            # Login with email to get host data
            test_email = "hello@optimisticlabsltd.com"
            login_response = oauth_utils.login_with_email(test_email)
            
            if not login_response or not login_response.get('success'):
                _logger.error("Login failed for email: %s", test_email)
                return oauth_utils._json_response(
                    False,
                    data={},
                    error="Failed to authenticate with Vision API",
                    status=502
                )
            
            # Extract token from login response
            vision_token = login_response.get('token') if login_response else None
            if not vision_token:
                _logger.error("No token received from Vision API")
                return oauth_utils._json_response(
                    False,
                    data={},
                    error="Failed to get authentication token",
                    status=502
                )
            
            # Extract hostData from response
            host_data_list = login_response.get('hostData', [])
            
            # Find matching host by networkId
            primary_host = None
            
            for host in host_data_list:
                _logger.info("Checking host: %s", host.get('networkId'))
                if host.get('networkId') == node_identifier:
                    has_lb = host.get('hasLB')
                    if isinstance(has_lb, str) and has_lb and has_lb.lower() != 'no':
                        primary_host = has_lb.split(',')[0].split()[0]
                    else:
                        primary_host = host.get('primaryHost')
                    _logger.info("Found matching host with primaryHost: %s", primary_host)
                    break
            
            if not primary_host:
                _logger.warning("No matching host found for node identifier: %s", node_identifier)
                return oauth_utils._json_response(
                    False,
                    data={},
                    error=f"No matching host found for node: {node_name}",
                    status=404
                )
            
            # Get detailed method trend for this specific host
            method_count_data = oauth_utils.get_method_trend_for_host(primary_host, vision_token,number_of_days)
            
            if method_count_data is None:
                _logger.error("Failed to get method count for host: %s", primary_host)
                return oauth_utils._json_response(
                    False,
                    data={},
                    error="Failed to get method count from Vision API",
                    status=502
                )
            latest_counts = method_count_data.get('latest_counts', {})
            method_counts = [
                {
                    'method_name': method_name,
                    'count': count
                }
                for method_name, count in latest_counts.items()
            ]
            
            response_data = {
                'node_name': node_name,
                'node_created_date': fields.Datetime.to_string(node.node_created_date or node.create_date) if (node.node_created_date or node.create_date) else None,
                'method_counts': method_counts
            }
            
            return oauth_utils._json_response(
                True,
                data=response_data,
                error="Method count data fetched successfully",
                status=200
            )

        except Exception as e:
            _logger.exception("Error in get_method_count: %s", str(e))
            return oauth_utils._json_response(False, data={}, error=str(e), status=500)
