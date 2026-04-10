# -*- coding: utf-8 -*-
"""
Subscription Management API Endpoints
Provides REST API for subscription operations
"""

import stripe
import logging
import json
from odoo import http, fields
from odoo.http import request, Response
from ...auth_module.utils import oauth as oauth_utils
from ..utils import restake_helper
from ..utils.subscription_helpers import _fetch_bot_wallet_balances
from ...zeeve_base.utils import base_utils
from ..utils.email_utils import send_validator_staking_notification
from ...auth_module.utils import oauth as oauth_utils_import
from ..utils.restake_helper import _get_github_client, _get_repositories
from ...access_rights.utils.access_manager import AccessManager


_logger = logging.getLogger(__name__)
class SubscriptionAPIController(http.Controller):
    @staticmethod
    def _get_subscription_from_node(node_identifier: str):
        node_model = request.env["subscription.node"].sudo()
        subscription_model = request.env["subscription.subscription"].sudo()
        node = node_model.search([("node_identifier", "=", node_identifier)], limit=1)
        if node:
            return node.subscription_id, node
        subscription = subscription_model.search([("subscription_uuid", "=", node_identifier)], limit=1)
        return subscription, node_model.browse()

    @http.route('/api/v1/subscriptions', type='http', auth='user', methods=['GET'], csrf=False)
    def get_subscriptions(self, **kwargs):
        """Get user's subscriptions"""
        try:
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            
            # Module access check
            if not AccessManager.check_module_access(user, 'subscription_management'):
                return oauth_utils._json_response(False, error="Module access denied", status=403)

            domain = AccessManager.get_company_domain(user, 'customer_name')
            record_domain = AccessManager.get_record_domain(user, 'subscription_management')
            if record_domain:
                domain += record_domain

            subscriptions = request.env['subscription.subscription'].sudo().search(domain)
            
            result = []
            for sub in subscriptions:
                nodes_payload = sub.serialize_nodes()
                primary_node = sub.get_primary_node()
                node_identifier = primary_node.node_identifier if primary_node else sub.subscription_id
                result.append({
                    'id': sub.id,
                    'subscription_id': sub.subscription_uuid,
                    'node_id': node_identifier,
                    'nodes': nodes_payload,
                    'name': sub.name,
                    'state': sub.state,
                    'stripe_subscription_id': sub.stripe_subscription_id,
                    'autopay_enabled': sub.autopay_enabled,
                    'stripe_status': sub.stripe_status,
                    'price': sub.price,
                    'currency': sub.currency_id.name,
                    'start_date': sub.start_date.isoformat() if sub.start_date else None,
                    'end_date': sub.end_date.isoformat() if sub.end_date else None,
                    'next_payment_date': sub.next_payment_date.isoformat() if sub.next_payment_date else None,
                    'product_name': sub.product_id.name,
                    'plan_name': sub.sub_plan_id.name,
                })
            
            return oauth_utils._json_response(True, {'subscriptions': result})

        except Exception as e:
            _logger.error("Error getting subscriptions: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v1/subscriptions/<int:subscription_id>', type='http', auth='user', methods=['GET'], csrf=False)
    def get_subscription(self, subscription_id, **kwargs):
        """Get specific subscription details"""
        try:
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            subscription = request.env['subscription.subscription'].sudo().browse(subscription_id)
            
            # Record access check
            domain = [('id', '=', subscription_id)]
            domain += AccessManager.get_company_domain(user, 'customer_name')
            record_domain = AccessManager.get_record_domain(user, 'subscription_management')
            if record_domain:
                domain += record_domain
            
            if not subscription.exists() or not request.env['subscription.subscription'].sudo().search(domain, limit=1):
                return oauth_utils._json_response(False, {'error': 'Subscription not found'}, status=404)
            
            primary_node = subscription.get_primary_node()
            node_identifier = primary_node.node_identifier if primary_node else subscription.subscription_id
            result = {
                'id': subscription.id,
                'subscription_id': subscription.subscription_id,
                'node_id': node_identifier,
                'nodes': subscription.serialize_nodes(),
                'name': subscription.name,
                'state': subscription.state,
                'stripe_subscription_id': subscription.stripe_subscription_id,
                'autopay_enabled': subscription.autopay_enabled,
                'stripe_status': subscription.stripe_status,
                'price': subscription.price,
                'currency': subscription.currency_id.name,
                'start_date': subscription.start_date.isoformat() if subscription.start_date else None,
                'end_date': subscription.end_date.isoformat() if subscription.end_date else None,
                'next_payment_date': subscription.next_payment_date.isoformat() if subscription.next_payment_date else None,
                'product_name': subscription.product_id.name,
                'plan_name': subscription.sub_plan_id.name,
                'payment_count': subscription.payment_count,
                'invoice_count': subscription.invoice_count,
                'payment_log_count': subscription.payment_log_count,
            }
            
            return oauth_utils._json_response(True, {'subscription': result})

        except Exception as e:
            _logger.error("Error getting subscription: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v1/subscriptions/<int:subscription_id>/enable-autopay', type='http', auth='public', methods=['POST'], csrf=False)
    def enable_autopay(self, subscription_id, **kwargs):
        """Enable autopay for subscription"""
        try:
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            subscription = request.env['subscription.subscription'].browse(subscription_id)
            
            if not subscription.exists() or subscription.customer_name.id != request.env.user.partner_id.id:
                return oauth_utils._json_response(False, {'error': 'Subscription not found'}, status=404)
            
            subscription.enable_autopay()
            
            return oauth_utils._json_response(True, {'message': 'Autopay enabled successfully'})

        except Exception as e:
            _logger.error("Error enabling autopay: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v1/subscriptions/<int:subscription_id>/disable-autopay', type='http', auth='public', methods=['POST'], csrf=False)
    def disable_autopay(self, subscription_id, **kwargs):
        """Disable autopay for subscription"""
        try:
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            subscription = request.env['subscription.subscription'].browse(subscription_id)
            
            if not subscription.exists() or subscription.customer_name.id != request.env.user.partner_id.id:
                return oauth_utils._json_response(False, {'error': 'Subscription not found'}, status=404)
            
            subscription.disable_autopay()
            
            return oauth_utils._json_response(True, {'message': 'Autopay disabled successfully'})

        except Exception as e:
            _logger.error("Error disabling autopay: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v1/subscriptions/<string:node_id>/restake/enable', type='http', auth='public', methods=['POST', 'OPTIONS'], csrf=False)
    def enable_restake(self, node_id, **kwargs):
        """Enable Restake for a validator subscription."""
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response(['POST'])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            payload = request.httprequest.get_json(force=True, silent=True) or {}
            minimum_reward = payload.get('minimumReward')
            interval = payload.get('interval')
            host_id = payload.get('hostId')

            required_fields = ["minimumReward", "interval","hostId"]
            is_valid, error_message = base_utils._validate_payload(payload, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, error=error_message, status=400)

            node_identifier = node_id
            subscription, node_record = self._get_subscription_from_node(node_identifier)
            is_admin_user = user.has_group('access_rights.group_admin')
            if not subscription or (subscription.customer_name.id != user.partner_id.id and not is_admin_user):
                return oauth_utils._json_response(False, {'error': 'Subscription not found'}, status=404)
            
            protocol_name = subscription.protocol_id.name if subscription.protocol_id else None
            base_url = request.env['ir.config_parameter'].sudo().get_param('backend_url', '')

            restake_record = restake_helper.enable_restake(
                request.env,
                host_id,
                node_identifier,
                minimum_reward,
                interval,
                partner_id=user.partner_id.id,
                user_email=user.email or user.partner_id.email,
            )
            
            # Send success notification email
            try:
                send_validator_staking_notification(
                    request.env,
                    node_record,
                    action_type='restake',
                    status='success',
                    protocol_name=protocol_name,
                    minimum_reward=str(minimum_reward),
                    interval=str(interval),
                    host_id=str(host_id),
                    base_url=base_url,
                )
            except Exception as mail_error:
                _logger.warning("Failed to send restake success notification email: %s", str(mail_error))

            return oauth_utils._json_response(True, {'restake': restake_record})

        except Exception as e:
            _logger.error("Error enabling restake: %s", str(e))
            
            # Send failure notification email
            try:
                if 'node_record' in locals() and node_record:
                    send_validator_staking_notification(
                        request.env,
                        node_record,
                        action_type='restake',
                        status='failed',
                        protocol_name=protocol_name if 'protocol_name' in locals() else None,
                        error_message=str(e),
                        minimum_reward=str(minimum_reward) if 'minimum_reward' in locals() else None,
                        interval=str(interval) if 'interval' in locals() else None,
                        host_id=str(host_id) if 'host_id' in locals() else None,
                        base_url=base_url if 'base_url' in locals() else None,
                    )
            except Exception as mail_error:
                _logger.warning("Failed to send restake failure notification email: %s", str(mail_error))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)


    @http.route('/api/v1/subscriptions/<string:node_id>/restake/disable', type='http', auth='public', methods=['POST', 'OPTIONS'], csrf=False)
    def disable_restake(self, node_id, **kwargs):
        """Disable Restake for a validator subscription."""
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response(['POST'])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            payload = request.httprequest.get_json(force=True, silent=True) or {}
            host_id = payload.get('hostId')

            required_fields = ["hostId"]
            is_valid, error_message = base_utils._validate_payload(payload, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, error=error_message, status=400)

            node_identifier = node_id
            is_admin_user = user.has_group('access_rights.group_admin')

            subscription, node_record = self._get_subscription_from_node(node_identifier)
            if not subscription or (subscription.customer_name.id != user.partner_id.id and not is_admin_user):
                return oauth_utils._json_response(False, {'error': 'Subscription not found'}, status=404)
            
            # Extract GitHub data BEFORE clearing metadata
            restake_data = {}
            if node_record.metadata_json:
                try:
                    restake_data = json.loads(node_record.metadata_json)
                except Exception:
                    restake_data = {}
            
            github_branch_name = restake_data.get('github_branch_name')
            github_pr_number = restake_data.get('github_pr_number')


            # Extract GitHub data BEFORE clearing metadata
            restake_data = {}
            if node_record.metadata_json:
                try:
                    restake_data = json.loads(node_record.metadata_json)
                except Exception:
                    restake_data = {}
            
            github_branch_name = restake_data.get('github_branch_name')
            github_pr_number = restake_data.get('github_pr_number')


            restake_record = restake_helper._disable_restake(
                request.env,
                host_id
            )
            # ✅ CLEANUP: Close PR and delete GitHub branch to prevent conflicts on re-enable
            if github_branch_name or github_pr_number:
                try:
                    github_client = _get_github_client(request.env)
                    fork_repo, upstream_repo, _, _, _ = _get_repositories(request.env, github_client)
                    restake_helper._cleanup_github_pr_and_branch(upstream_repo, fork_repo, github_pr_number, github_branch_name)
                except Exception as cleanup_error:
                    _logger.warning("GitHub PR/branch cleanup failed (non-blocking): %s", cleanup_error)
                    # Don't fail the disable operation if cleanup fails


            # Update is_active to false in subscription.metaData
            metaData = node_record.metadata_json or "{}"
            try:
                meta_json = json.loads(metaData)
            except Exception:
                meta_json = {}
            # Keep only keys that are NOT in the remove list
            try:
                keys_to_remove = {"github_pr_number", "github_branch_name", "next_run_time", "is_pr_merged"}
                meta_json = {k: v for k, v in meta_json.items() if k not in keys_to_remove}
            except KeyError:
                _logger.debug("key to remove error from disable restake")

            meta_json["is_active"] = False
            node_record.sudo().write({"metadata_json": json.dumps(meta_json)})

            return oauth_utils._json_response(True, {'restake': restake_record})

        except Exception as e:
            _logger.error("Error enabling restake: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)


    @http.route('/api/v1/subscriptions/<string:subscription_id>/cancel', type='http', auth='public', methods=['POST'], csrf=False)
    def cancel_subscription(self, subscription_id, **kwargs):
        """Cancel subscription"""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id','=',128)])
            payload = request.httprequest.get_json(force=True, silent=True) or {}
            reason = payload.get('reason') or payload.get('notes')
            node_identifier = subscription_id
            subscription, node_record = self._get_subscription_from_node(node_identifier)
            rollup_subscription = request.env['rollup.service'].sudo().search([('service_id', '=', node_identifier)])
            if subscription:
                if subscription.customer_name.id != user.partner_id.id:
                    return oauth_utils._json_response(False, {'status':404,'error': 'Subscription not found'}, status=404)
                subscription.notify_unsubscribe_request(requested_by=user, node_identifier=node_identifier, reason=reason)
            elif rollup_subscription:
                if rollup_subscription.customer_id.id != user.partner_id.id:
                    return oauth_utils._json_response(False, {'error': 'Subscription not found'}, status=404)
                rollup_subscription.notify_unsubscribe_request(requested_by=user, reason=reason)
            else :
                return oauth_utils._json_response(False, {'status':404,'error': 'Subscription not found'}, status=404)
            
            return oauth_utils._json_response(True, {'message': 'Cancellation request submitted. Our team will reach out shortly.'})

        except Exception as e:
            _logger.error("Error canceling subscription: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v1/subscriptions/<string:subscription_id>/pause', type='http', auth='public', methods=['POST'], csrf=False)
    def pause_subscription(self, subscription_id, **kwargs):
        """Pause subscription"""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            node_identifier = subscription_id
            rollup_subscription = request.env['rollup.service'].sudo().search([('service_id', '=', node_identifier)])
            if rollup_subscription:
                if rollup_subscription.customer_id.id != user.partner_id.id:
                    return oauth_utils._json_response(False, {'error': 'Subscription not found'}, status=404)
                rollup_subscription.pause_stripe_subscription()
            else:
                return oauth_utils._json_response(False, {'error': 'Subscription not found'}, status=404)
            
            return oauth_utils._json_response(True, {'message': 'Subscription paused successfully'})

        except Exception as e:
            _logger.error("Error pausing subscription: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v1/subscriptions/<string:subscription_id>/resume', type='http', auth='public', methods=['POST'], csrf=False)
    def resume_subscription(self, subscription_id, **kwargs):
        """Resume subscription"""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            node_identifier = subscription_id
            rollup_subscription = request.env['rollup.service'].sudo().search([('service_id', '=', node_identifier)])
            if rollup_subscription:
                if rollup_subscription.customer_id.id != user.partner_id.id:
                    return oauth_utils._json_response(False, {'error': 'Subscription not found'}, status=404)
                rollup_subscription.resume_stripe_subscription()
            else:
                return oauth_utils._json_response(False, {'error': 'Subscription not found'}, status=404)
            
            return oauth_utils._json_response(True, {'message': 'Subscription resumed successfully'})

        except Exception as e:
            _logger.error("Error resuming subscription: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v1/subscriptions/<int:subscription_id>/payment-logs', type='http', auth='public', methods=['GET'], csrf=False)
    def get_payment_logs(self, subscription_id, **kwargs):
        """Get payment logs for subscription"""
        try:
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            subscription = request.env['subscription.subscription'].browse(subscription_id)
            
            if not subscription.exists() or subscription.customer_name.id != request.env.user.partner_id.id:
                return oauth_utils._json_response(False, {'error': 'Subscription not found'}, status=404)
            
            logs = subscription.payment_log_ids
            result = []
            
            for log in logs:
                result.append({
                    'id': log.id,
                    'event_id': log.event_id,
                    'event_type': log.event_type,
                    'amount': log.amount,
                    'currency': log.currency,
                    'payment_status': log.payment_status,
                    'subscription_status': log.subscription_status,
                    'processed': log.processed,
                    'stripe_created': log.stripe_created.isoformat() if log.stripe_created else None,
                    'processed_at': log.processed_at.isoformat() if log.processed_at else None,
                    'description': log.description,
                    'failure_reason': log.failure_reason,
                })
            
            return oauth_utils._json_response(True, {'payment_logs': result})

        except Exception as e:
            _logger.error("Error getting payment logs: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v1/subscriptions/<int:subscription_id>/invoices', type='http', auth='public', methods=['GET'], csrf=False)
    def get_invoices(self, subscription_id, **kwargs):
        """Get invoices for subscription"""
        try:
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            
            # Module access check
            if not AccessManager.check_module_access(user, 'subscription_management'):
                return oauth_utils._json_response(False, error="Module access denied", status=403)

            subscription = request.env['subscription.subscription'].sudo().browse(subscription_id)
            
            # Record access check
            domain = [('id', '=', subscription_id)]
            domain += AccessManager.get_company_domain(user, 'customer_name')
            record_domain = AccessManager.get_record_domain(user, 'subscription_management')
            if record_domain:
                domain += record_domain
            
            if not subscription.exists() or not request.env['subscription.subscription'].sudo().search(domain, limit=1):
                return oauth_utils._json_response(False, {'error': 'Subscription not found'}, status=404)
            
            invoices = subscription.invoice_ids
            result = []
            backend_url = request.env['ir.config_parameter'].sudo().get_param('backend_url')
            for invoice in invoices:
                attachment = request.env['ir.attachment'].sudo().search([('res_id','=', invoice.id)], limit=1)
                if attachment and attachment.id:
                    attachment_url = f"/api/download_invoice/{attachment.id}"
                else:
                    attachment_url = False
                result.append({
                    'id': invoice.id,
                    'name': invoice.name,
                    'amount_total': invoice.amount_total,
                    'amount_residual': invoice.amount_residual,
                    'currency': invoice.currency_id.name,
                    'state': invoice.state,
                    "download_url": (backend_url or '') + (attachment_url or ''),
                    'payment_state': invoice.payment_state,
                    'invoice_date': invoice.invoice_date.isoformat() if invoice.invoice_date else None,
                    'invoice_date_due': invoice.invoice_date_due.isoformat() if invoice.invoice_date_due else None,
                })
            
            return oauth_utils._json_response(True, {'invoices': result})

        except Exception as e:
            _logger.error("Error getting invoices: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v1/subscriptions/<int:subscription_id>/payments', type='http', auth='user', methods=['GET'], csrf=False)
    def get_payments(self, subscription_id, **kwargs):
        """Get payments for subscription"""
        try:
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            
            # Module access check
            if not AccessManager.check_module_access(user, 'subscription_management'):
                return oauth_utils._json_response(False, error="Module access denied", status=403)

            subscription = request.env['subscription.subscription'].sudo().browse(subscription_id)
            
            # Record access check
            domain = [('id', '=', subscription_id)]
            domain += AccessManager.get_company_domain(user, 'customer_name')
            record_domain = AccessManager.get_record_domain(user, 'subscription_management')
            if record_domain:
                domain += record_domain
            
            if not subscription.exists() or not request.env['subscription.subscription'].sudo().search(domain, limit=1):
                return oauth_utils._json_response(False, {'error': 'Subscription not found'}, status=404)
            
            payments = subscription.payment_ids
            result = []
            
            for payment in payments:
                result.append({
                    'id': payment.id,
                    'name': payment.name,
                    'amount': payment.amount,
                    'currency': payment.currency_id.name,
                    'payment_type': payment.payment_type,
                    'state': payment.state,
                    'date': payment.date.isoformat() if payment.date else None,
                    'ref': payment.ref,
                })
            
            return oauth_utils._json_response(True, {'payments': result})

        except Exception as e:
            _logger.error("Error getting payments: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v1/subscriptions/create', type='http', auth='user', methods=['POST'], csrf=False)
    def create_subscription(self, **kwargs):
        """Create new subscription"""
        try:
            data = request.httprequest.get_json(force=True, silent=True) or {}
            
            # Validate required fields
            required_fields = ['product_id', 'sub_plan_id', 'price']
            for field in required_fields:
                if field not in data:
                    return oauth_utils._json_response(False, {'error': f'Missing required field: {field}'}, status=400)
            
            # Create subscription
            # The Super Admin (Company Owner) is the billing owner
            owner_partner_id = request.env.user.company_id.owner_id.partner_id.id or request.env.user.partner_id.id
            
            subscription_vals = {
                'customer_name': owner_partner_id,
                'product_id': data['product_id'],
                'sub_plan_id': data['sub_plan_id'],
                'price': data['price'],
                'source': 'api',
                'state': 'draft',
                'company_id': request.env.user.company_id.id
            }
            
            # Add optional fields
            if 'quantity' in data:
                subscription_vals['quantity'] = data['quantity']
            if 'duration' in data:
                subscription_vals['duration'] = data['duration']
            if 'unit' in data:
                subscription_vals['unit'] = data['unit']
            if 'payment_frequency' in data:
                subscription_vals['payment_frequency'] = data['payment_frequency']
            
            subscription = request.env['subscription.subscription'].sudo().create(subscription_vals)
            
            # Auto-assign access to the operator/admin who created it
            if request.env.user.company_role == 'operator':
                request.env['record.access'].sudo().create({
                    'user_id': request.env.user.id,
                    'module_name': 'subscription_management',
                    'record_id': subscription.id
                })

            node_vals = {
                'node_type': data.get('node_type') or subscription.subscription_type,
                'node_name': data.get('node_name'),
                'network_selection_id': data.get('network_selection_id'),
                'server_location_id': data.get('server_location_id'),
                'software_update_rule': data.get('software_update_rule'),
            }
            filtered_node_vals = {k: v for k, v in node_vals.items() if v}
            subscription.create_primary_node(filtered_node_vals)
            
            return oauth_utils._json_response(True, {
                'message': 'Subscription created successfully',
                'subscription_id': subscription.id
            })

        except Exception as e:
            _logger.error("Error creating subscription: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route("/api/v1/node-validator-details", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False)
    def node_validator_info(self, **_kwargs):
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["GET"])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            
            # -------------------------------
            # Parse and validate payload
            subscription_identifier = _kwargs.get("node_id")
            if not subscription_identifier:
                return oauth_utils._json_response(False, {'error': 'subscription_id (legacy node_id) is required'}, status=400)
            
            # Check if user belongs to access_rights.group_admin
            is_admin_user = user.has_group('access_rights.group_admin')
            subscription, selected_node = self._get_subscription_from_node(subscription_identifier)
            
            if not subscription or (not is_admin_user and subscription.customer_name.id != user.partner_id.id):
                return oauth_utils._json_response(False, {'error': 'Subscription not found'}, status=404)

            # Parse metaData
            restake_data = {}
            if selected_node.metadata_json:
                try:
                    restake_data = json.loads(selected_node.metadata_json)
                except Exception:  # pragma: no cover - corrupted JSON
                    restake_data = {}
            
            # Parse validator_info
            validator_info = {}
            if selected_node.validator_info:
                try:
                    validator_info = json.loads(selected_node.validator_info)
                except Exception:
                    validator_info = {}

            network_type = selected_node.network_selection_id.name.lower() if selected_node.network_selection_id else ""
            bot_addresses = validator_info.get('wallet')
            bot_wallet_balances = _fetch_bot_wallet_balances(network_type, bot_addresses)
            data = {
                "validator_address": validator_info.get('validatorAddress'),
                "delegation_address": validator_info.get('delegationAddress'),
                "protocol": subscription.protocol_id.protocol_id,
                "network_type": network_type,
                "bot_addresses": bot_addresses,
                "bot_wallet_balances": bot_wallet_balances,
                "restake_status": restake_data.get("is_active", False),
                "key": validator_info.get('key'),
                "type": validator_info.get('@type'),
                "email": user.login or user.partner_id.email,
                "node_id": subscription_identifier,
            }
            return oauth_utils._json_response(True, data, status=200)
        except Exception as exc:
            return oauth_utils._json_response(False, error=str(exc), status=500)
