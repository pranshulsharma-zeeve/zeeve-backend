from odoo import http, fields
from odoo.http import request
import logging
import json
from ..utils import oauth as oauth_utils
from ...access_rights.utils.access_manager import AccessManager
import re

_logger = logging.getLogger(__name__)

class UserManagementController(http.Controller):
    """Controller for managing company users (Admins, Operators)."""

    @http.route('/api/v1/company/users', type='http', auth='public', methods=['OPTIONS', 'GET'], csrf=False)
    def list_company_users(self, **kwargs):
        """List all users and pending invitations belonging to the same company as the authenticated user."""
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            if user.company_role not in ['super_admin', 'admin']:
                return oauth_utils._json_response(False, error="Permission denied", status=403)

            # Fetch all users associated with this company who have an assigned org role
            company_users = request.env['res.users'].sudo().with_context(active_test=False).search([
                ('company_id', '=', user.company_id.id),
                ('company_role', '!=', False),
                ('id', '!=', 1) 
            ])

            # Only fetch pending invitations for this company
            pending_invites = request.env['user.invitation'].sudo().search([
                ('company_id', '=', user.company_id.id),
                ('status', '=', 'pending')
            ])

            # Summary counts
            super_admin_count = 0
            admin_count = 0
            operator_count = 0
            for u in company_users:
                if u.company_role == 'super_admin':
                    super_admin_count += 1
                elif u.company_role == 'admin':
                    admin_count += 1
                elif u.company_role == 'operator':
                    operator_count += 1

            pending_count = len(pending_invites)
            total_count = len(company_users) + pending_count

            result = []
            import json

            # Add users
            for u in company_users:
                assigned_nodes = []
                assigned_rollups = []
                if u.node_access_type == 'all':
                    assigned_nodes = [{"id": "All", "name": "All", "type": "All"}]
                    assigned_rollups = [{"id": "All", "name": "All", "type": "All"}]
                else:
                    try:
                        raw_nodes = json.loads(u.specific_nodes) if u.specific_nodes else []
                        raw_rollups = json.loads(u.specific_rollups) if u.specific_rollups else []

                        # Enrich Nodes
                        for item in raw_nodes:
                            item = (item or "").strip()
                            if not item: continue
                            node_type_key = AccessManager._NODE_NAME_MAP.get(item.lower())
                            if node_type_key:
                                assigned_nodes.append({"id": item, "name": item.title(), "type": "Node Type"})
                            else:
                                node = request.env['subscription.node'].sudo().search([('node_identifier', '=', item)], limit=1)
                                if node:
                                    protocol_name = node.subscription_id.protocol_id.name or ""
                                    node_type_label = dict(node._fields['node_type'].selection).get(node.node_type, node.node_type)
                                    type_name = f"{protocol_name} ({node_type_label})" if protocol_name else node_type_label
                                    assigned_nodes.append({"id": item, "name": node.node_name, "type": type_name})
                                else:
                                    assigned_nodes.append({"id": item, "name": item, "type": "Unknown"})

                        # Enrich Rollups
                        uuid_pattern = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
                        for item in raw_rollups:
                            item = (item or "").strip()
                            if not item: continue
                            if uuid_pattern.match(item):
                                service = request.env['rollup.service'].sudo().search([('service_id', '=', item)], limit=1)
                                if service:
                                    assigned_rollups.append({"id": item, "name": service.name, "type": service.type_id.name or "Rollup"})
                                else:
                                    assigned_rollups.append({"id": item, "name": item, "type": "Unknown Rollup"})
                            else:
                                assigned_rollups.append({"id": item, "name": item, "type": "Rollup Type"})
                    except Exception:
                        pass

                result.append({
                    'id': u.id,
                    'name': u.name,
                    'email': u.login,
                    'role': u.company_role.replace('_', ' ').title() if u.company_role else "User",
                    'is_owner': u.is_company_owner,
                    'active': u.active,
                    'node_access_type': u.node_access_type,
                    'assigned_nodes': assigned_nodes,
                    'assigned_rollups': assigned_rollups,
                    'last_login': u.login_date.isoformat() if u.login_date else None,
                    'status': 'active'
                })

            # Add pending invites
            for inv in pending_invites:
                assigned_nodes = []
                assigned_rollups = []
                if inv.node_access_type == 'all':
                    assigned_nodes = [{"id": "All", "name": "All", "type": "All"}]
                    assigned_rollups = [{"id": "All", "name": "All", "type": "All"}]
                else:
                    try:
                        raw_nodes = json.loads(inv.specific_nodes) if inv.specific_nodes else []
                        raw_rollups = json.loads(inv.specific_rollups) if inv.specific_rollups else []

                        # Enrichment Logic Nodes
                        for item in raw_nodes:
                            item = (item or "").strip()
                            if not item: continue
                            node_type_key = AccessManager._NODE_NAME_MAP.get(item.lower())
                            if node_type_key:
                                assigned_nodes.append({"id": item, "name": item.title(), "type": "Node Type"})
                            else:
                                node = request.env['subscription.node'].sudo().search([('node_identifier', '=', item)], limit=1)
                                if node:
                                    protocol_name = node.subscription_id.protocol_id.name or ""
                                    node_type_label = dict(node._fields['node_type'].selection).get(node.node_type, node.node_type)
                                    type_name = f"{protocol_name} ({node_type_label})" if protocol_name else node_type_label
                                    assigned_nodes.append({"id": item, "name": node.node_name, "type": type_name})
                                else:
                                    assigned_nodes.append({"id": item, "name": item, "type": "Unknown"})

                        # Enrichment Logic Rollups
                        uuid_pattern = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
                        for item in raw_rollups:
                            item = (item or "").strip()
                            if not item: continue
                            if uuid_pattern.match(item):
                                service = request.env['rollup.service'].sudo().search([('service_id', '=', item)], limit=1)
                                if service:
                                    assigned_rollups.append({"id": item, "name": service.name, "type": service.type_id.name or "Rollup"})
                                else:
                                    assigned_rollups.append({"id": item, "name": item, "type": "Unknown Rollup"})
                            else:
                                assigned_rollups.append({"id": item, "name": item, "type": "Rollup Type"})
                    except Exception:
                        pass

                result.append({
                    'id': f"inv_{inv.id}",
                    'name': inv.email.split('@')[0], # Placeholder name
                    'email': inv.email,
                    'role': "Pending invite",
                    'is_owner': False,
                    'active': True,
                    'node_access_type': inv.node_access_type,
                    'assigned_nodes': assigned_nodes,
                    'assigned_rollups': assigned_rollups,
                    'status': 'pending',
                    'invited_on': inv.create_date.isoformat() if inv.create_date else None
                })

            summary = {
                'total': total_count,
                'pending': pending_count,
                'operator': operator_count,
                'super_admin': super_admin_count,
                'admin': admin_count
            }

            return oauth_utils._json_response(True, data={'summary': summary, 'users': result})
        except Exception as e:
            _logger.error("Error listing company users: %s", str(e))
            return oauth_utils._json_response(False, error=str(e), status=500)

    @http.route('/api/v1/company/users/<string:target_user_id>/role', type='http', auth='public', methods=['OPTIONS', 'POST'], csrf=False)
    def update_user_role(self, target_user_id, **kwargs):
        """Update the role of a company user or a pending invitation."""
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            if user.company_role not in ['super_admin', 'admin']:
                return oauth_utils._json_response(False, error="Permission denied", status=403)

            data = request.httprequest.get_json(force=True, silent=True) or {}
            new_role = data.get('role')
            if new_role not in ['admin', 'operator']:
                return oauth_utils._json_response(False, error="Invalid role", status=400)

            node_access_type = data.get('node_access_type')
            specific_nodes = json.dumps(data.get('specific_nodes', [])) if data.get('specific_nodes') is not None else None
            specific_rollups = json.dumps(data.get('specific_rollups', [])) if data.get('specific_rollups') is not None else None

            if target_user_id.startswith('inv_'):
                # Handle invitation update
                inv_id = int(target_user_id.replace('inv_', ''))
                target_inv = request.env['user.invitation'].sudo().browse(inv_id)
                if not target_inv.exists() or target_inv.company_id.id != user.company_id.id:
                    return oauth_utils._json_response(False, error="Invitation not found in your company", status=404)
                
                if target_inv.status != 'pending':
                    return oauth_utils._json_response(False, error=f"Cannot edit an invitation that is {target_inv.status}", status=400)

                if user.company_role == 'admin' and new_role == 'admin':
                    return oauth_utils._json_response(False, error="Admins cannot promote invitations to Admin", status=403)

                vals = {'role': new_role}
                if node_access_type:
                    vals['node_access_type'] = node_access_type
                if specific_nodes is not None:
                    vals['specific_nodes'] = specific_nodes
                if specific_rollups is not None:
                    vals['specific_rollups'] = specific_rollups
                
                target_inv.sudo().write(vals)
                return oauth_utils._json_response(True, data={'message': 'Invitation updated successfully'})
            else:
                # Handle user update
                try:
                    target_user_id_int = int(target_user_id)
                except ValueError:
                    return oauth_utils._json_response(False, error="Invalid user ID format", status=400)

                target_user = request.env['res.users'].sudo().browse(target_user_id_int)
                if not target_user.exists() or target_user.company_id.id != user.company_id.id:
                    return oauth_utils._json_response(False, error="User not found in your company", status=404)

                # Access Control: Admin cannot update Super Admin or other Admins
                if user.company_role == 'admin':
                    if target_user.company_role in ['super_admin', 'admin']:
                        return oauth_utils._json_response(False, error="Admins can only update Operators", status=403)

                if user.company_role == 'admin' and new_role == 'admin':
                    return oauth_utils._json_response(False, error="Admins cannot promote to Admin", status=403)

                vals = {'company_role': new_role}
                if node_access_type:
                    vals['node_access_type'] = node_access_type
                if specific_nodes is not None:
                    vals['specific_nodes'] = specific_nodes
                if specific_rollups is not None:
                    vals['specific_rollups'] = specific_rollups

                target_user.sudo().write(vals)
                return oauth_utils._json_response(True, data={'message': 'User updated successfully'})
        except Exception as e:
            _logger.error("Error updating user role: %s", str(e))
            return oauth_utils._json_response(False, error=str(e), status=500)

    @http.route('/api/v1/company/users/<string:target_user_id>/deactivate', type='http', auth='public', methods=['OPTIONS', 'POST'], csrf=False)
    def deactivate_user(self, target_user_id, **kwargs):
        """Deactivate (archive) a company user or cancel an invitation."""
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            if user.company_role not in ['super_admin', 'admin']:
                return oauth_utils._json_response(False, error="Permission denied", status=403)

            if target_user_id.startswith('inv_'):
                # Handle invitation "deactivation" (cancellation)
                inv_id = int(target_user_id.replace('inv_', ''))
                target_inv = request.env['user.invitation'].sudo().browse(inv_id)
                if not target_inv.exists() or target_inv.company_id.id != user.company_id.id:
                    return oauth_utils._json_response(False, error="Invitation not found in your company", status=404)
                
                if target_inv.status != 'pending':
                    return oauth_utils._json_response(False, error=f"Cannot cancel an invitation that is {target_inv.status}", status=400)

                target_inv.sudo().write({'status': 'rejected'})
                return oauth_utils._json_response(True, data={'message': 'Invitation cancelled successfully'})
            else:
                # Handle user deactivation
                try:
                    target_user_id_int = int(target_user_id)
                except ValueError:
                    return oauth_utils._json_response(False, error="Invalid user ID format", status=400)

                target_user = request.env['res.users'].sudo().browse(target_user_id_int)
                if not target_user.exists() or target_user.company_id.id != user.company_id.id:
                    return oauth_utils._json_response(False, error="User not found in your company", status=404)

                # Cannot deactivate self
                if target_user.id == user.id:
                    return oauth_utils._json_response(False, error="You cannot deactivate yourself", status=400)

                # Access Control: Admin cannot deactivate Super Admin or other Admins
                if user.company_role == 'admin':
                    if target_user.company_role in ['super_admin', 'admin']:
                        return oauth_utils._json_response(False, error="Admins can only deactivate Operators", status=403)

                # Super Admin cannot be deactivated if they are the owner
                if target_user.is_company_owner:
                    return oauth_utils._json_response(False, error="Company owner cannot be deactivated", status=403)

                target_user.sudo().write({'active': False})
                return oauth_utils._json_response(True, data={'message': 'User deactivated successfully'})
        except Exception as e:
            _logger.error("Error deactivating user: %s", str(e))
            return oauth_utils._json_response(False, error=str(e), status=500)

    @http.route('/api/v1/company/invitations/pending', type='http', auth='public', methods=['OPTIONS', 'GET'], csrf=False)
    def list_pending_invitations(self, **kwargs):
        """List all pending invitations belonging to the same company as the authenticated user."""
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            if user.company_role not in ['super_admin', 'admin']:
                return oauth_utils._json_response(False, error="Permission denied", status=403)

            pending_invites = request.env['user.invitation'].sudo().search([
                ('company_id', '=', user.company_id.id),
                ('status', '=', 'pending')
            ])

            import json
            result = []
            for inv in pending_invites:
                assigned_nodes = []
                assigned_rollups = []
                if inv.node_access_type == 'all':
                    assigned_nodes = ["All"]
                    assigned_rollups = ["All"]
                else:
                    try:
                        raw_nodes = json.loads(inv.specific_nodes) if inv.specific_nodes else []
                        raw_rollups = json.loads(inv.specific_rollups) if inv.specific_rollups else []

                        # Enrichment Logic Nodes
                        for item in raw_nodes:
                            item = (item or "").strip()
                            if not item: continue
                            node_type_key = AccessManager._NODE_NAME_MAP.get(item.lower())
                            if node_type_key:
                                assigned_nodes.append({"id": item, "name": item.title(), "type": "Node Type"})
                            else:
                                node = request.env['subscription.node'].sudo().search([('node_identifier', '=', item)], limit=1)
                                if node:
                                    protocol_name = node.subscription_id.protocol_id.name or ""
                                    node_type_label = dict(node._fields['node_type'].selection).get(node.node_type, node.node_type)
                                    type_name = f"{protocol_name} ({node_type_label})" if protocol_name else node_type_label
                                    assigned_nodes.append({"id": item, "name": node.node_name, "type": type_name})
                                else:
                                    assigned_nodes.append({"id": item, "name": item, "type": "Unknown"})

                        # Enrichment Logic Rollups
                        uuid_pattern = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
                        for item in raw_rollups:
                            item = (item or "").strip()
                            if not item: continue
                            if uuid_pattern.match(item):
                                service = request.env['rollup.service'].sudo().search([('service_id', '=', item)], limit=1)
                                if service:
                                    assigned_rollups.append({"id": item, "name": service.name, "type": service.type_id.name or "Rollup"})
                                else:
                                    assigned_rollups.append({"id": item, "name": item, "type": "Unknown Rollup"})
                            else:
                                assigned_rollups.append({"id": item, "name": item, "type": "Rollup Type"})
                    except Exception:
                        pass

                result.append({
                    'id': inv.id,
                    'email': inv.email,
                    'role': inv.role.replace('_', ' ').title() if inv.role else "User",
                    'status': inv.status,
                    'node_access_type': inv.node_access_type,
                    'assigned_nodes': assigned_nodes,
                    'assigned_rollups': assigned_rollups,
                    'invited_by': inv.invited_by.name,
                    'created_at': inv.create_date.isoformat() if inv.create_date else None,
                    'expiry_date': inv.expiry_date.isoformat() if inv.expiry_date else None,
                })

            return oauth_utils._json_response(True, data={'invitations': result})
        except Exception as e:
            _logger.error("Error listing pending invitations: %s", str(e))
            return oauth_utils._json_response(False, error=str(e), status=500)
    @http.route('/api/v1/company/transfer-ownership', type='http', auth='public', methods=['OPTIONS', 'POST'], csrf=False)
    def transfer_ownership(self, **kwargs):
        """Transfer organization ownership to another user."""
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            # Only the current owner can transfer ownership
            if not user.is_company_owner or user.company_role != 'super_admin':
                return oauth_utils._json_response(False, error="Only the company owner can transfer ownership", status=403)

            data = request.httprequest.get_json(force=True, silent=True) or {}
            target_user_id = data.get('target_user_id')
            if not target_user_id:
                return oauth_utils._json_response(False, error="target_user_id is required", status=400)

            try:
                target_user_id_int = int(target_user_id)
            except ValueError:
                return oauth_utils._json_response(False, error="Invalid target user ID format", status=400)

            if target_user_id_int == user.id:
                return oauth_utils._json_response(False, error="You are already the owner", status=400)

            target_user = request.env['res.users'].sudo().browse(target_user_id_int)
            if not target_user.exists() or target_user.company_id.id != user.company_id.id:
                return oauth_utils._json_response(False, error="Target user not found in your company", status=404)

            if not target_user.active:
                return oauth_utils._json_response(False, error="Cannot transfer ownership to an inactive user", status=400)

            # Perform the transfer
            # 1. New owner becomes super_admin and is_company_owner
            target_user.sudo().write({
                'is_company_owner': True,
                'company_role': 'super_admin'
            })
            
            # 2. Old owner is downgraded to admin and loses owner flag
            user.sudo().write({
                'is_company_owner': False,
                'company_role': 'admin'
            })

            _logger.info("Ownership transferred from user %s to %s for company %s", user.id, target_user.id, user.company_id.name)
            
            return oauth_utils._json_response(True, data={'message': f'Ownership transferred to {target_user.name} successfully'})
        except Exception as e:
            _logger.error("Error transferring ownership: %s", str(e))
            return oauth_utils._json_response(False, error=str(e), status=500)

    @http.route('/api/v1/user/invitations/<int:inv_id>/resend', type='http', auth='public', methods=['OPTIONS', 'POST'], csrf=False)
    def resend_invitation(self, inv_id, **kwargs):
        """Resend an invitation, updating its expiry and resetting status to pending."""
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id','=',97)])
            if user.company_role not in ['super_admin', 'admin']:
                return oauth_utils._json_response(False, error="Permission denied", status=403)

            invitation = request.env['user.invitation'].sudo().browse(inv_id)
            if not invitation.exists() or invitation.company_id.id != user.company_id.id:
                return oauth_utils._json_response(False, error="Invitation not found in your company", status=404)

            # Update expiry and status
            from datetime import timedelta
            if invitation.status not in ['pending','expired']:
                return oauth_utils._json_response(False, error="Invitation is already accepted or rejected", status=400)
            invitation.sudo().write({
                'expiry_date': fields.Datetime.now() + timedelta(days=7),
                'status': 'pending'
            })

            # Re-send email
            invitation.send_invitation_email()

            return oauth_utils._json_response(True, data={'message': 'Invitation resent successfully'})
        except Exception as e:
            _logger.error("Error resending invitation: %s", str(e))
            return oauth_utils._json_response(False, error=str(e), status=500)

    @http.route('/api/v1/user/invitations/<int:inv_id>/revoke', type='http', auth='public', methods=['OPTIONS', 'POST'], csrf=False)
    def revoke_invitation(self, inv_id, **kwargs):
        """Revoke a pending invitation."""
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            if user.company_role not in ['super_admin', 'admin']:
                return oauth_utils._json_response(False, error="Permission denied", status=403)

            invitation = request.env['user.invitation'].sudo().browse(inv_id)
            if not invitation.exists() or invitation.company_id.id != user.company_id.id:
                return oauth_utils._json_response(False, error="Invitation not found in your company", status=403)

            if invitation.status != 'pending':
                return oauth_utils._json_response(False, error=f"Cannot revoke an invitation that is {invitation.status}", status=400)

            invitation.sudo().write({'status': 'revoked'})
            return oauth_utils._json_response(True, data={'message': 'Invitation revoked successfully'})
        except Exception as e:
            _logger.error("Error revoking invitation: %s", str(e))
            return oauth_utils._json_response(False, error=str(e), status=500)

    @http.route('/api/v1/user/invitations/<int:inv_id>', type='http', auth='public', methods=['OPTIONS', 'DELETE'], csrf=False)
    def remove_invitation(self, inv_id, **kwargs):
        """Permanently remove an invitation."""
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response(['DELETE'])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            if user.company_role not in ['super_admin', 'admin']:
                return oauth_utils._json_response(False, error="Permission denied", status=403)

            invitation = request.env['user.invitation'].sudo().browse(inv_id)
            if not invitation.exists() or invitation.company_id.id != user.company_id.id:
                return oauth_utils._json_response(False, error="Invitation not found in your company", status=404)

            invitation.sudo().unlink()
            return oauth_utils._json_response(True, data={'message': 'Invitation removed successfully'})
        except Exception as e:
            _logger.error("Error removing invitation: %s", str(e))
            return oauth_utils._json_response(False, error=str(e), status=500)
