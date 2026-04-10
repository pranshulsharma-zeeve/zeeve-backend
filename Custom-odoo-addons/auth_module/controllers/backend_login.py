"""Custom web login controller to block portal-only accounts."""

from odoo import _, http
from odoo.http import request
from odoo.addons.auth_signup.controllers.main import AuthSignupHome
from odoo.addons.web.controllers.home import SIGN_UP_REQUEST_PARAMS
import odoo


class PortalRestrictedLogin(AuthSignupHome):
    """Extend the default login to reject portal-only users."""

    @http.route()
    def web_login(self, redirect=None, **kw):
        response = super().web_login(redirect=redirect, **kw)
        if request.httprequest.method == 'POST':
            user = self._portal_only_user()
            if user:
                return self._deny_portal_access(redirect)
        return response

    def _portal_only_user(self):
        uid = request.session.uid
        if not uid:
            return None
        user = request.env['res.users'].sudo().browse(uid)
        if not user.exists():
            return None
        if user.has_group('base.group_user'):
            return None
        return user if user.has_group('base.group_portal') else None

    def _prepare_login_values(self, redirect=None):
        values = {k: v for k, v in request.params.items() if k in SIGN_UP_REQUEST_PARAMS}
        try:
            values['databases'] = http.db_list()
        except odoo.exceptions.AccessDenied:
            values['databases'] = None

        if request.params.get('login'):
            values['login'] = request.params.get('login')
        elif request.session.get('auth_login'):
            values['login'] = request.session.get('auth_login')

        if not odoo.tools.config['list_db']:
            values['disable_database_manager'] = True

        values.update(self.get_auth_signup_config())
        if redirect:
            values['redirect'] = redirect
        return values

    def _deny_portal_access(self, redirect=None):
        request.session.logout(keep_db=True)
        frontend_url = (
            request.env['ir.config_parameter'].sudo().get_param('frontend_url') or ''
        ).strip()
        if frontend_url:
            return request.redirect(frontend_url, local=False)
        values = self._prepare_login_values(redirect=redirect)
        values['error'] = _(
            "Portal accounts cannot access the Zeeve backend. "
            "Please use the customer portal instead."
        )
        return request.render('web.login', values)
