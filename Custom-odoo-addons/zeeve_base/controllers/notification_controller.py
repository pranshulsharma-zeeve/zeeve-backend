# -*- coding: utf-8 -*-
"""Notification APIs for external frontends."""

import time

import odoo

from odoo import http
from odoo.http import request
from odoo.addons.bus.websocket import WebsocketConnectionHandler

from ...auth_module.utils import oauth as oauth_utils


class NotificationController(http.Controller):
    def _external_server_base_url(self):
        httprequest = request.httprequest
        forwarded_proto = (
            httprequest.headers.get('X-Forwarded-Proto')
            or httprequest.environ.get('HTTP_X_FORWARDED_PROTO')
            or ''
        )
        forwarded_host = (
            httprequest.headers.get('X-Forwarded-Host')
            or httprequest.environ.get('HTTP_X_FORWARDED_HOST')
            or ''
        )
        scheme = (forwarded_proto.split(',')[0].strip() or httprequest.scheme or 'http').lower()
        host = forwarded_host.split(',')[0].strip() or httprequest.host
        return '%s://%s' % (scheme, host)

    def _external_websocket_base_url(self):
        server_base_url = self._external_server_base_url()
        if server_base_url.startswith('https://'):
            return 'wss://' + server_base_url[len('https://'):]
        if server_base_url.startswith('http://'):
            return 'ws://' + server_base_url[len('http://'):]
        return server_base_url

    def _client_timezone(self):
        """Resolve timezone passed by the frontend for client-local serialization."""
        return (
            request.httprequest.headers.get('X-Timezone')
            or request.params.get('timezone')
            or request.env.user.tz
            or False
        )

    @http.route('/api/v1/notifications/websocket-bootstrap', type='http', auth='none', methods=['OPTIONS', 'GET'], csrf=False)
    def websocket_bootstrap(self, **kwargs):
        if request.httprequest.method == 'OPTIONS':
            return oauth_utils.preflight_response(methods=['GET'])

        user, resp = oauth_utils.require_user()
        if not user:
            return resp

        request.session.logout(keep_db=True)
        user_context = dict(user.context_get())
        request.session.should_rotate = True
        request.session.update({
            'db': request.db,
            'login': user.login,
            'uid': user.id,
            'context': user_context,
            'session_token': user._compute_session_token(request.session.sid),
        })

        env = request.env(user=user.id)
        odoo.http.root.session_store.rotate(request.session, env)
        request.future_response.set_cookie(
            'session_id',
            request.session.sid,
            max_age=http.get_session_max_inactivity(env),
            httponly=True,
            path='/',
        )

        session_info = env['ir.http'].session_info()
        websocket_base_url = self._external_websocket_base_url()
        server_base_url = self._external_server_base_url()
        return oauth_utils._json_response(
            True,
            data={
                'websocket_url': '%s/websocket?version=%s' % (
                    websocket_base_url,
                    session_info.get('websocket_worker_version') or '',
                ),
                'websocket_worker_version': session_info.get('websocket_worker_version'),
                'server_url': server_base_url,
                'last_bus_id': request.env['bus.bus'].sudo()._bus_last_id(),
                'subscribe_payload': {
                    'event_name': 'subscribe',
                    'data': {
                        'channels': [],
                        'last': request.env['bus.bus'].sudo()._bus_last_id(),
                    },
                },
                'session_authenticated': True,
            },
        )

    @http.route('/api/v1/notifications', type='http', auth='none', methods=['OPTIONS', 'GET'], csrf=False)
    def list_notifications(self, **kwargs):
        if request.httprequest.method == 'OPTIONS':
            return oauth_utils.preflight_response(methods=['GET'])

        user, resp = oauth_utils.require_user()
        if not user:
            return resp

        limit = min(max(int(kwargs.get('limit', 20) or 20), 1), 100)
        offset = max(int(kwargs.get('offset', 0) or 0), 0)
        unread_only_raw = kwargs.get('unread_only')
        if unread_only_raw is None:
            unread_only = True
        else:
            unread_only = str(unread_only_raw).lower() in {'1', 'true', 'yes'}
        client_tz = self._client_timezone()
        notification_env = request.env['zeeve.notification'].with_context(tz=client_tz).sudo()
        notification_env._cleanup_partner_notifications(user.partner_id.id)
        domain = [('partner_id', '=', user.partner_id.id)]
        if unread_only:
            domain.append(('is_read', '=', False))
        notifications = notification_env.search(domain, offset=offset, limit=limit, order='create_date desc, id desc')
        return oauth_utils._json_response(
            True,
            data={
                'notifications': [notification.to_frontend_dict() for notification in notifications],
                'unread_count': notification_env.unread_count_for_partner(user.partner_id),
                'last_bus_id': request.env['bus.bus'].sudo()._bus_last_id(),
            },
        )

    @http.route('/api/v1/notifications/mark-read', type='http', auth='none', methods=['OPTIONS', 'POST'], csrf=False)
    def mark_notifications_read(self, **kwargs):
        if request.httprequest.method == 'OPTIONS':
            return oauth_utils.preflight_response(methods=['POST'])

        user, resp = oauth_utils.require_user()
        if not user:
            return resp

        payload = request.httprequest.get_json(force=True, silent=True) or {}
        ids = payload.get('notification_ids') or []
        if payload.get('notification_id'):
            ids.append(payload['notification_id'])
        ids = [int(notification_id) for notification_id in ids if str(notification_id).isdigit()]
        if not ids:
            return oauth_utils._json_response(False, error='notification_ids is required', status=400)

        notifications = request.env['zeeve.notification'].sudo().search([
            ('id', 'in', ids),
            ('partner_id', '=', user.partner_id.id),
        ])
        notifications.mark_as_read()
        return oauth_utils._json_response(
            True,
            data={
                'updated_ids': notifications.ids,
                'unread_count': request.env['zeeve.notification'].sudo().unread_count_for_partner(user.partner_id),
            },
        )

    @http.route('/api/v1/notifications/mark-all-read', type='http', auth='none', methods=['OPTIONS', 'POST'], csrf=False)
    def mark_all_notifications_read(self, **kwargs):
        if request.httprequest.method == 'OPTIONS':
            return oauth_utils.preflight_response(methods=['POST'])

        user, resp = oauth_utils.require_user()
        if not user:
            return resp

        request.env['zeeve.notification'].sudo()._cleanup_partner_notifications(user.partner_id.id)
        notifications = request.env['zeeve.notification'].sudo().search([
            ('partner_id', '=', user.partner_id.id),
            ('is_read', '=', False),
        ])
        notifications.mark_as_read()
        return oauth_utils._json_response(True, data={'updated_ids': notifications.ids, 'unread_count': 0})

    @http.route('/api/v1/notifications/live', type='http', auth='none', methods=['OPTIONS', 'GET'], csrf=False)
    def poll_live_notifications(self, **kwargs):
        if request.httprequest.method == 'OPTIONS':
            return oauth_utils.preflight_response(methods=['GET'])

        user, resp = oauth_utils.require_user()
        if not user:
            return resp

        try:
            last_bus_id = max(int(kwargs.get('last_bus_id', 0) or 0), 0)
        except (TypeError, ValueError):
            last_bus_id = 0
        try:
            timeout_seconds = int(kwargs.get('timeout', 25) or 25)
        except (TypeError, ValueError):
            timeout_seconds = 25
        timeout_seconds = min(max(timeout_seconds, 5), 25)

        bus_env = request.env['bus.bus'].sudo()
        deadline = time.time() + timeout_seconds
        raw_notifications = []
        while time.time() < deadline and not raw_notifications:
            raw_notifications = bus_env._poll([user.partner_id], last=last_bus_id)
            if raw_notifications:
                break
            time.sleep(1)

        live_notifications = []
        max_bus_id = last_bus_id
        notification_env = request.env['zeeve.notification'].with_context(tz=self._client_timezone()).sudo()
        for raw_notification in raw_notifications:
            max_bus_id = max(max_bus_id, raw_notification.get('id', 0))
            message = raw_notification.get('message') or {}
            if message.get('type') != 'zeeve.notification':
                continue
            payload = dict(message.get('payload') or {})
            notification_id = payload.get('id')
            if notification_id:
                notification = notification_env.browse(notification_id)
                if notification.exists():
                    payload = notification.to_frontend_dict()
            payload['bus_id'] = raw_notification.get('id')
            live_notifications.append(payload)

        return oauth_utils._json_response(
            True,
            data={
                'notifications': live_notifications,
                'last_bus_id': max_bus_id,
                'timed_out': not bool(live_notifications),
            },
        )
