# -*- coding: utf-8 -*-
"""Redirect portal-only users away from Odoo customer portal pages."""

from odoo import http
from odoo.http import request
from odoo.addons.portal.controllers.portal import CustomerPortal


class ZeevePortalRedirect(CustomerPortal):
    """Prevent portal-only users from landing on Odoo portal home/account pages."""

    def _portal_frontend_redirect(self):
        frontend_url = (request.env['ir.config_parameter'].sudo().get_param('frontend_url') or '').strip()
        if frontend_url:
            return request.redirect(frontend_url, local=False)
        return request.redirect('/', local=False)

    def _is_portal_only_user(self):
        user = request.env.user
        return bool(
            user
            and user.has_group('base.group_portal')
            and not user.has_group('base.group_user')
        )

    @http.route()
    def home(self, **kw):
        if self._is_portal_only_user():
            return self._portal_frontend_redirect()
        return super().home(**kw)

    @http.route()
    def account(self, redirect=None, **post):
        if self._is_portal_only_user():
            return self._portal_frontend_redirect()
        return super().account(redirect=redirect, **post)
