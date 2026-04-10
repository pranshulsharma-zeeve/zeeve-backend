from odoo import http, fields
from odoo.http import request
import logging
import json
from ..utils import oauth as oauth_utils
from ...access_rights.utils.access_manager import AccessManager
import re

_logger = logging.getLogger(__name__)

class InvitationController(http.Controller):
    """Controller for managing user invitations (receive, accept, reject)."""

    @http.route('/api/v1/user/invitations', type='http', auth='public', methods=['OPTIONS', 'GET'], csrf=False)
    def list_user_invitations(self, **kwargs):
        """List all pending invitations for the authenticated user."""
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id','=',98)])
            # print(user,'---------23')
            # Search by user email
            invitations = request.env['user.invitation'].sudo().search([
                ('email', '=', user.login),
                ('status', '=', 'pending')
            ])
            result = []
            for inv in invitations:
                assigned_nodes = []
                assigned_rollups = []
                if inv.node_access_type == 'all':
                    assigned_nodes = [{"id": "All", "name": "All", "type": "All"}]
                    assigned_rollups = [{"id": "All", "name": "All", "type": "All"}]
                else:
                    try:
                        raw_nodes = json.loads(inv.specific_nodes) if inv.specific_nodes else []
                        raw_rollups = json.loads(inv.specific_rollups) if inv.specific_rollups else []
                        
                        # Enrich Nodes
                        for item in raw_nodes:
                            item = (item or "").strip()
                            if not item: continue
                            
                            # Check if it's a type shorthand
                            node_type_key = AccessManager._NODE_NAME_MAP.get(item.lower())
                            if node_type_key:
                                assigned_nodes.append({
                                    "id": item,
                                    "name": item.title(),
                                    "type": "Node Type"
                                })
                            else:
                                # Search for specific node
                                node = request.env['subscription.node'].sudo().search([('node_identifier', '=', item)], limit=1)
                                if node:
                                    protocol_name = node.subscription_id.protocol_id.name or ""
                                    node_type_label = dict(node._fields['node_type'].selection).get(node.node_type, node.node_type)
                                    type_name = f"{protocol_name} ({node_type_label})" if protocol_name else node_type_label
                                    assigned_nodes.append({
                                        "id": item,
                                        "name": node.node_name,
                                        "type": type_name
                                    })
                                else:
                                    assigned_nodes.append({"id": item, "name": item, "type": "Unknown"})

                        # Enrich Rollups
                        uuid_pattern = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
                        for item in raw_rollups:
                            item = (item or "").strip()
                            if not item: continue
                            
                            if uuid_pattern.match(item):
                                # Search for specific rollup service
                                service = request.env['rollup.service'].sudo().search([('service_id', '=', item)], limit=1)
                                if service:
                                    assigned_rollups.append({
                                        "id": item,
                                        "name": service.name,
                                        "type": service.type_id.name or "Rollup"
                                    })
                                else:
                                    assigned_rollups.append({"id": item, "name": item, "type": "Unknown Rollup"})
                            else:
                                # It's a type name
                                assigned_rollups.append({
                                    "id": item,
                                    "name": item,
                                    "type": "Rollup Type"
                                })
                    except Exception as e:
                        _logger.error("Error enriching invitation assets: %s", str(e))
                        pass

                result.append({
                    'id': inv.id,
                    'organization': inv.company_id.name,
                    'role': inv.role.replace('_', ' ').title() if inv.role else "User",
                    'status': inv.status,
                    'node_access_type': inv.node_access_type,
                    'assigned_nodes': assigned_nodes,
                    'assigned_rollups': assigned_rollups,
                    'invited_by': inv.invited_by.name,
                    'invited_on': inv.create_date.isoformat() if inv.create_date else None,
                    'expiry_date': inv.expiry_date.isoformat() if inv.expiry_date else None,
                })

            return oauth_utils._json_response(True, data={'invitations': result})
        except Exception as e:
            _logger.error("Error listing user invitations: %s", str(e))
            return oauth_utils._json_response(False, error=str(e), status=500)

    @http.route('/api/v1/user/invitations/<int:inv_id>/accept', type='http', auth='public', methods=['OPTIONS', 'POST'], csrf=False)
    def accept_invitation(self, inv_id, **kwargs):
        """Accept an invitation and join the company."""
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id','=',98)])
            invitation = request.env['user.invitation'].sudo().browse(inv_id)
            if not invitation.exists() or invitation.email != user.login:
                return oauth_utils._json_response(False, error="Invitation not found", status=404)

            if invitation.status != 'pending':
                return oauth_utils._json_response(False, error=f"Invitation is already {invitation.status}", status=400)

            if invitation.expiry_date < fields.Datetime.now():
                invitation.sudo().write({'status': 'expired'})
                return oauth_utils._json_response(False, error="Invitation has expired", status=400)

            # Accept the invitation
            invitation.sudo().action_accept()

            # Update user's company and role
            user.sudo().write({
                'company_ids': [(4, invitation.company_id.id)],
                'company_id': invitation.company_id.id,
                'company_role': invitation.role,
                'node_access_type': invitation.node_access_type,
                'specific_nodes': invitation.specific_nodes,
                'specific_rollups': invitation.specific_rollups,
                'invited_by': invitation.invited_by.id
            })

            # Send acceptance notification
            invitation.send_acceptance_email()

            return oauth_utils._json_response(True, data={'message': 'Invitation accepted successfully'})
        except Exception as e:
            _logger.error("Error accepting invitation: %s", str(e))
            return oauth_utils._json_response(False, error=str(e), status=500)

    @http.route('/api/v1/user/invitations/<int:inv_id>/reject', type='http', auth='public', methods=['OPTIONS', 'POST'], csrf=False)
    def reject_invitation(self, inv_id, **kwargs):
        """Reject an invitation."""
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            invitation = request.env['user.invitation'].sudo().browse(inv_id)
            if not invitation.exists() or invitation.email != user.login:
                return oauth_utils._json_response(False, error="Invitation not found", status=404)

            if invitation.status != 'pending':
                return oauth_utils._json_response(False, error=f"Invitation is already {invitation.status}", status=400)

            # Reject the invitation
            invitation.sudo().write({'status': 'rejected'})
            
            # Send rejection notification
            invitation.send_rejection_email()

            return oauth_utils._json_response(True, data={'message': 'Invitation rejected successfully'})
        except Exception as e:
            _logger.error("Error rejecting invitation: %s", str(e))
            return oauth_utils._json_response(False, error=str(e), status=500)

    @http.route('/api/v1/user/organization/leave', type='http', auth='public', methods=['OPTIONS', 'POST'], csrf=False)
    def leave_organization(self, **kwargs):
        """Allow a user to leave their current organization."""
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            current_company = user.company_id
            if not current_company:
                return oauth_utils._json_response(False, error="You do not belong to any organization", status=400)

            # Ownership check
            if user.is_company_owner:
                return oauth_utils._json_response(False, error="Only the company owner can leave after transferring ownership", status=400)

            # Check if user has other companies
            other_companies = user.company_ids.filtered(lambda c: c.id != current_company.id)
            if not other_companies:
                return oauth_utils._json_response(False, error="You must belong to at least one organization. You cannot leave your only organization.", status=400)

            # Perform the leave
            # 1. Remove from company_ids and reset org-specific role/access fields
            user.sudo().write({
                'company_ids': [(3, current_company.id)],
                'company_id': other_companies[0].id,
                'company_role': False,
                'node_access_type': False,
                'specific_nodes': False,
                'specific_rollups': False,
                'invited_by': False,
            })

            _logger.info("User %s left company %s", user.id, current_company.name)
            return oauth_utils._json_response(True, data={'message': f"Successfully left organization {current_company.name}"})
        except Exception as e:
            _logger.error("Error leaving organization: %s", str(e))
            return oauth_utils._json_response(False, error=str(e), status=500)
