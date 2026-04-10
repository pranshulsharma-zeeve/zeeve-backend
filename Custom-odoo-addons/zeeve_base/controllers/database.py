import secrets

from odoo import http
from odoo.http import request
from odoo.addons.web.controllers.database import Database
from odoo.tools import config


class SecureDatabase(Database):
    """Limit database management routes to technical administrators only."""

    _TOKEN_HEADER = 'X-Backup-Token'

    def _token_is_valid(self):
        """Return True if the request carries the configured backup token."""
        token = request.httprequest.headers.get(self._TOKEN_HEADER)
        if not token:
            return False

        param_token = request.env['ir.config_parameter'].sudo().get_param('backup_api_token')
        config_token = config.get('backup_api_token')
        expected = param_token or config_token
        if not expected:
            return False

        return secrets.compare_digest(token, expected)

    def _has_manager_access(self):
        user = request.env.user
        return bool(user and user.has_group('base.group_system'))

    def _deny(self):
        return request.redirect('/web/login')

    @http.route('/web/database/manager', type='http', auth='user')
    def manager(self, **kw):
        if not self._has_manager_access():
            return self._deny()
        return super().manager(**kw)

    @http.route('/web/database/selector', type='http', auth='user')
    def selector(self, **kw):
        if not self._has_manager_access():
            return self._deny()
        return super().selector(**kw)

    @http.route('/web/database/create', type='http', auth='user', methods=['POST'], csrf=False)
    def create(self, master_pwd, name, lang, password, **post):
        if not self._has_manager_access():
            return self._deny()
        return super().create(master_pwd, name, lang, password, **post)

    @http.route('/web/database/duplicate', type='http', auth='user', methods=['POST'], csrf=False)
    def duplicate(self, master_pwd, name, new_name, neutralize_database=False):
        if not self._has_manager_access():
            return self._deny()
        return super().duplicate(master_pwd, name, new_name, neutralize_database)

    @http.route('/web/database/drop', type='http', auth='user', methods=['POST'], csrf=False)
    def drop(self, master_pwd, name):
        if not self._has_manager_access():
            return self._deny()
        return super().drop(master_pwd, name)

    @http.route('/web/database/backup', type='http', auth='public', methods=['POST'], csrf=False)
    def backup(self, master_pwd, name, backup_format='zip'):
        if not (self._token_is_valid() or self._has_manager_access()):
            return self._deny()
        return super().backup(master_pwd, name, backup_format)

    @http.route('/web/database/restore', type='http', auth='user', methods=['POST'], csrf=False, max_content_length=None)
    def restore(self, master_pwd, backup_file, name, copy=False, neutralize_database=False):
        if not self._has_manager_access():
            return self._deny()
        return super().restore(master_pwd, backup_file, name, copy=copy, neutralize_database=neutralize_database)

    @http.route('/web/database/change_password', type='http', auth='user', methods=['POST'], csrf=False)
    def change_password(self, master_pwd, master_pwd_new):
        if not self._has_manager_access():
            return self._deny()
        return super().change_password(master_pwd, master_pwd_new)
