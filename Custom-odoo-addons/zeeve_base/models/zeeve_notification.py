# -*- coding: utf-8 -*-
"""Persistent user notifications with bus push support."""

from datetime import datetime, timedelta, timezone
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class ZeeveNotification(models.Model):
    _name = 'zeeve.notification'
    _description = 'Zeeve User Notification'
    _order = 'create_date desc, id desc'
    _NOTIFICATION_MAX_PER_PARTNER = 200

    partner_id = fields.Many2one('res.partner', required=True, index=True, ondelete='cascade')
    notification_type = fields.Char(required=True, index=True)
    category = fields.Selection(
        [
            ('info', 'Info'),
            ('success', 'Success'),
            ('warning', 'Warning'),
            ('error', 'Error'),
        ],
        default='info',
        required=True,
    )
    title = fields.Char(required=True)
    message = fields.Text(required=True)
    payload = fields.Json(default=dict)
    action_url = fields.Char()
    is_read = fields.Boolean(default=False, index=True)
    read_at = fields.Datetime()
    expires_at = fields.Datetime(default=lambda self: self._default_expires_at(), index=True)
    reference_model = fields.Char()
    reference_id = fields.Integer()
    dedupe_key = fields.Char(index=True, copy=False)

    @api.model
    def _notification_retention_days(self):
        value = self.env['ir.config_parameter'].sudo().get_param('zeeve.notification_retention_days')
        try:
            days = int(value or 30)
        except (TypeError, ValueError):
            days = 30
        return max(days, 1)

    @api.model
    def _default_expires_at(self):
        return fields.Datetime.now() + timedelta(days=self._notification_retention_days())

    def _get_output_timezone(self):
        """Resolve the target timezone from env context or server local time."""
        tz_name = (self.env.context.get('tz') or '').strip()
        if tz_name:
            try:
                return ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:  # pragma: no cover - defensive fallback
                pass
        return datetime.now().astimezone().tzinfo

    def _serialize_datetime_for_client(self, value):
        """Serialize a UTC-style Odoo datetime into the requested output timezone."""
        if not value:
            return False
        normalized = fields.Datetime.to_datetime(value)
        if not normalized:
            return False
        if normalized.tzinfo is None:
            normalized = normalized.replace(tzinfo=timezone.utc)
        localized = normalized.astimezone(self._get_output_timezone()).replace(tzinfo=None)
        return fields.Datetime.to_string(localized)

    def to_frontend_dict(self):
        self.ensure_one()
        return {
            'id': self.id,
            'type': self.notification_type,
            'category': self.category,
            'title': self.title,
            'message': self.message,
            'payload': self.payload or {},
            'action_url': self.action_url or '',
            'is_read': bool(self.is_read),
            'read_at': self._serialize_datetime_for_client(self.read_at),
            'created_at': self._serialize_datetime_for_client(self.create_date),
            'expires_at': self._serialize_datetime_for_client(self.expires_at),
            'reference_model': self.reference_model or '',
            'reference_id': self.reference_id or False,
        }

    @api.model
    def unread_count_for_partner(self, partner):
        partner_id = partner.id if hasattr(partner, 'id') else int(partner)
        self._cleanup_partner_notifications(partner_id)
        return self.sudo().search_count([
            ('partner_id', '=', partner_id),
            ('is_read', '=', False),
        ])

    @api.model
    def _cleanup_partner_notifications(self, partner):
        partner_id = partner.id if hasattr(partner, 'id') else int(partner)
        notification_env = self.sudo()
        expired = notification_env.search([
            ('partner_id', '=', partner_id),
            ('expires_at', '!=', False),
            ('expires_at', '<=', fields.Datetime.now()),
        ])
        if expired:
            expired.unlink()

        stale_notifications = notification_env.search(
            [('partner_id', '=', partner_id)],
            order='create_date desc, id desc',
            offset=self._NOTIFICATION_MAX_PER_PARTNER,
        )
        if stale_notifications:
            stale_notifications.unlink()

    @api.model
    def notify_partner(
        self,
        partner,
        *,
        notification_type,
        title,
        message,
        category='info',
        payload=None,
        action_url=None,
        reference_model=None,
        reference_id=None,
        dedupe_key=None,
    ):
        partner_record = partner if hasattr(partner, 'id') else self.env['res.partner'].sudo().browse(int(partner))
        if not partner_record or not partner_record.exists():
            return self.browse()

        notification_env = self.sudo()
        notification_env._cleanup_partner_notifications(partner_record.id)
        if dedupe_key:
            existing = notification_env.search([('dedupe_key', '=', dedupe_key)], limit=1)
            if existing:
                _logger.info(
                    "Notification deduped | partner=%s notification_type=%s dedupe_key=%s existing_id=%s",
                    partner_record.id,
                    notification_type,
                    dedupe_key,
                    existing.id,
                )
                return existing

        notification = notification_env.create({
            'partner_id': partner_record.id,
            'notification_type': notification_type,
            'category': category,
            'title': title,
            'message': message,
            'payload': payload or {},
            'action_url': action_url or False,
            'reference_model': reference_model or False,
            'reference_id': reference_id or False,
            'dedupe_key': dedupe_key or False,
        })
        bus_payload = notification.to_frontend_dict()
        bus_payload['unread_count'] = notification_env.unread_count_for_partner(partner_record)
        _logger.info(
            "Notification created and bus push queued | notification_id=%s partner=%s type=%s unread_count=%s dedupe_key=%s",
            notification.id,
            partner_record.id,
            notification_type,
            bus_payload['unread_count'],
            dedupe_key or "",
        )
        self.env['bus.bus']._sendone(partner_record, 'zeeve.notification', bus_payload)
        return notification

    def mark_as_read(self):
        unread = self.filtered(lambda notification: not notification.is_read)
        if unread:
            unread.sudo().write({
                'is_read': True,
                'read_at': fields.Datetime.now(),
            })
        return True
