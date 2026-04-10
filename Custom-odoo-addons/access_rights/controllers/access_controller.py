from odoo import http, fields
from odoo.http import request
import logging
from ...auth_module.utils import oauth as oauth_utils

_logger = logging.getLogger(__name__)

class AccessController(http.Controller):
    """APIs for managing operator access."""

    @http.route('/api/operator-access', type='http', auth='public', methods=['OPTIONS', 'POST'], csrf=False)
    def api_operator_access(self, **kwargs):
        """Assign module and record access to an operator."""
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response(methods=['POST'])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            if user.company_role not in ['super_admin', 'admin']:
                return oauth_utils._json_response(False, error="Permission denied", status=403)

            data = request.httprequest.get_json(force=True, silent=True) or {}
            target_user_id = data.get('user_id')
            modules_data = data.get('modules', [])

            if not target_user_id:
                return oauth_utils._json_response(False, error="user_id is required", status=400)

            target_user = request.env['res.users'].sudo().browse(target_user_id)
            if not target_user.exists() or target_user.company_id.id != user.company_id.id:
                return oauth_utils._json_response(False, error="User not found in your company", status=404)
            
            if target_user.company_role != 'operator':
                return oauth_utils._json_response(False, error="Target user is not an operator", status=400)

            # Clear existing access
            request.env['module.access'].sudo().search([('user_id', '=', target_user_id)]).unlink()
            request.env['record.access'].sudo().search([('user_id', '=', target_user_id)]).unlink()

            for mod in modules_data:
                module_name = mod.get('module')
                records = mod.get('records', [])

                # Grant module access
                request.env['module.access'].sudo().create({
                    'user_id': target_user_id,
                    'module_name': module_name,
                    'read_access': True
                })

                # Grant record access
                for rec_id in records:
                    request.env['record.access'].sudo().create({
                        'user_id': target_user_id,
                        'module_name': module_name,
                        'record_id': rec_id
                    })

            return oauth_utils._json_response(True, data={'message': 'Access updated successfully'})
        except Exception as e:
            _logger.error("Error updating operator access: %s", str(e))
            return oauth_utils._json_response(False, error=str(e), status=500)

    @http.route('/api/operator-access', type='http', auth='public', methods=['OPTIONS', 'GET'], csrf=False)
    def get_operator_access(self, **kwargs):
        """Get access details for a specific operator."""
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response(methods=['GET'])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            target_user_id = request.params.get('user_id')
            if not target_user_id:
                 return oauth_utils._json_response(False, error="user_id is required", status=400)
                 
            target_user = request.env['res.users'].sudo().browse(int(target_user_id))
            if not target_user.exists() or target_user.company_id.id != user.company_id.id:
                return oauth_utils._json_response(False, error="User not found in your company", status=404)

            module_access = request.env['module.access'].sudo().search([('user_id', '=', target_user.id)])
            record_access = request.env['record.access'].sudo().search([('user_id', '=', target_user.id)])

            modules = []
            for ma in module_access:
                recs = record_access.filtered(lambda r: r.module_name == ma.module_name).mapped('record_id')
                modules.append({
                    'module': ma.module_name,
                    'records': recs
                })

            return oauth_utils._json_response(True, data={'user_id': target_user.id, 'modules': modules})
        except Exception as e:
            _logger.error("Error getting operator access: %s", str(e))
            return oauth_utils._json_response(False, error=str(e), status=500)
