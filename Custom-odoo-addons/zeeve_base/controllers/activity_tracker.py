# -*- coding: utf-8 -*-
"""HTTP controller exposing customer activity tracker data."""

import logging

from odoo import http
from odoo.http import request

from ...auth_module.utils import oauth as oauth_utils
from ..utils import activity_tracker

_logger = logging.getLogger(__name__)


class ActivityTrackerController(http.Controller):
    """Authenticated API endpoints for customer activity data."""

    def _apply_client_timezone(self, timezone_name=None):
        tz_name = (
            request.httprequest.headers.get("X-Timezone")
            or timezone_name
            or getattr(request.env.user, "tz", False)
            or False
        )
        if tz_name:
            request.update_context(tz=tz_name)

    @http.route(
        "/api/v1/account/activity-tracker",
        type="http",
        auth="none",
        methods=["OPTIONS", "GET"],
        csrf=False,
    )
    def get_activity_tracker(self, **kwargs):
        """Return activity summary for the logged-in user."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["GET"])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id', '=', 21)],limit=1)
        

            limit = kwargs.get("limit", 100)
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                return oauth_utils._json_response(
                    False,
                    error="limit must be an integer.",
                    status=400,
                )

            include_activities = str(kwargs.get("include_activities", "")).strip().lower() in (
                "1", "true", "yes",
            )
            self._apply_client_timezone(kwargs.get("timezone"))
            payload = activity_tracker.get_user_activity_payload(
                request.env,
                user,
                limit=limit,
                include_activities=include_activities,
            )
            return oauth_utils._json_response(True, payload)
        except Exception as exc:  # pragma: no cover - defensive fallback
            _logger.exception("Failed to fetch activity tracker: %s", exc)
            return oauth_utils._json_response(False, error=str(exc), status=500)
