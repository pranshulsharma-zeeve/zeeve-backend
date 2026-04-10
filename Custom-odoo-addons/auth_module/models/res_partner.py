"""Extension of ``res.partner`` to support OTP based email verification.

The model adds helper methods used by the authentication controller to
generate and validate one-time passwords (OTP) sent to the user's e-mail
address.  OTP codes expire after a short period of time and are stored in the
partner record to keep :class:`res.users` free of custom fields.
"""

import base64
import json
import secrets
from datetime import timedelta
from urllib.parse import urlencode
from ...zeeve_base.utils import base_utils
from ...auth_module.utils import opn_util
from odoo import api, fields, models
import logging

res_company = 'res.company'
zeeve_config = 'zeeve.config'

RESET_PASSWORD_PATH_PARAM = 'reset_password_path'

_logger = logging.getLogger(__name__)

class ResPartner(models.Model):
    """Partner model extended with authentication helpers."""

    _inherit = 'res.partner'

    oauth_provider = fields.Selection([
        ('google', 'Google'),
        ('github', 'GitHub'),
        ('apple', 'Apple'),
        ('email', 'Email'),
        ('quicknode', 'QuickNode'),
    ], string='OAuth Provider')
    oauth_uid = fields.Char('OAuth UID')
    oauth_access_token = fields.Char('OAuth Access Token')
    oauth_refresh_token = fields.Char('OAuth Refresh Token')
    oauth_token_expires = fields.Datetime('OAuth Token Expiry')
    # ``verification_token`` is used as a numeric OTP code.  Keeping the
    # existing field name avoids the need to update mail templates and other
    # pieces of code that already rely on it.
    verification_token = fields.Char('Email Verification Token')
    verification_token_expiry = fields.Datetime('Verification Token Expiry')
    password_reset_token = fields.Char('Password Reset Token')
    password_reset_token_expiry = fields.Datetime('Password Reset Token Expiry')
    email_verified = fields.Boolean('Email Verified', default=False)
    first_name = fields.Char('First Name')
    middle_name = fields.Char('Middle Name')
    last_name = fields.Char('Last Name')
    account_id = fields.Char(string="Account Id", readonly=True)
    utm_info = fields.Json(string="UTM Info")
    stripe_customer_id = fields.Char(
        string="Stripe Customer ID",
        help="Stripe customer identifier saved after the first rollup checkout.",
    )
    multi_tenant_host = fields.Selection([
        ('zeeve', 'Zeeve'),
        ('iopn', 'IOPN'),
        ('ew', 'EW'),
    ], string='Multi-Tenant Host', default='zeeve', help="Platform from which the user comes from")
    utm_info_pretty = fields.Text(
        string="Metadata (JSON)",
        compute="_compute_pretty_json",
        readonly=True,
    )
    max_opn_subscriptions = fields.Integer(
        string='Max OPN Subscriptions',
        default=3,
        help='Maximum number of active OPN protocol subscriptions allowed for this user',
        tracking=True,
    )

    def _compute_pretty_json(self):
        for partner in self:
            metadata = partner.utm_info
            try:
                partner.utm_info_pretty = json.dumps(metadata, indent=2, sort_keys=True) if metadata else "{}"
            except (TypeError, ValueError):
                partner.utm_info_pretty = str(metadata or "{}")

    def generate_verification_token(self):
        """Generate and store a six digit OTP for e-mail verification.

        The OTP is valid for 10 minutes.  Returning the value allows callers to
        reuse it if needed (e.g., tests).
        """

        otp = ''.join(secrets.choice('0123456789') for _ in range(6))
        self.write({
            'verification_token': otp,
            'verification_token_expiry': fields.Datetime.now() + timedelta(minutes=10),
        })
        return otp

    @api.model
    def verify_email_token(self, token):
        """Validate an OTP and mark the corresponding partner as verified."""

        partner = self.sudo().search([('verification_token', '=', token)], limit=1)
        if not partner:
            return False
        if partner.verification_token_expiry < fields.Datetime.now():
            return False
        partner.write({
            'email_verified': True,
            'verification_token': False,
            'verification_token_expiry': False,
        })
        return True
    
    def send_welcome_email(self):
        try:
            admin_recipients = base_utils._get_admin_recipients(self.env, channel_code='new_user_admin')
            admin_emails = admin_recipients.get('to') or []
            ctx = {
                'config': admin_recipients.get('config'),
                'admin_emails': ','.join(admin_emails),
            }
            template = self.env.ref('auth_module.mail_template_welcome_user', raise_if_not_found=False)
            admin_template = self.env.ref('auth_module.mail_template_user_onboarded', raise_if_not_found=False)
            if not template:
                _logger.warning("Welcome email template missing; skipping welcome email for partners %s", self.ids)
                return

            template = template.sudo()
            admin_template = admin_template and admin_template.sudo()

            for partner in self:
                if not partner.email:
                    _logger.info("Welcome email skipped for partner %s due to missing email", partner.id)
                    continue

                try:
                    if self.multi_tenant_host == 'iopn':
                        opn_util.send_opn_email_by_type(self.env,self,'welcome')
                    else:
                        template.send_mail(
                            partner.id,
                            email_values={'email_to': partner.email},
                            force_send=True,
                        )
                    _logger.info("Welcome email queued for partner %s", partner.id)
                except Exception:
                    _logger.exception("Failed to send welcome email to partner %s", partner.id)

                if not admin_template or not admin_emails:
                    continue

                try:
                    admin_template.with_context(**ctx).send_mail(
                        partner.id,
                        email_values={'email_to': ','.join(admin_emails) ,'email_cc': ','.join(admin_recipients.get('cc', []))},
                        force_send=True,
                    )
                except Exception:
                    _logger.exception("Failed to send admin onboarding email for partner %s", partner.id)
        except Exception:
            _logger.exception("Unexpected error while sending welcome emails for partners %s", self.ids)

    def send_shardeum_welcome_email(self):
        """Send the Shardeum welcome email to all partners in the recordset."""

        template = self.env.ref('auth_module.mail_template_welcome_shardeum', raise_if_not_found=False)
        if not template:
            return False

        partners = self.filtered(lambda partner: partner.email)
        if not partners:
            return False

        template = template.sudo()
        sent = False

        for partner in partners:
            try:
                template.send_mail(
                    partner.id,
                    email_values={'email_to': partner.email},
                    force_send=True,
                )
                sent = True
            except Exception:  # pragma: no cover - avoid blocking flows on mail failures
                _logger.exception("Shardeum welcome email failed for partner %s", partner.id)

        return sent

    def send_verification_email(self):
        """Send the OTP by e-mail using Odoo's mail template system."""
        try:
            self.send_security_email('otp_verification')
        except Exception as exc:
            print("Error sending Otp verification email", str(exc))

    def generate_password_reset_token(self):
        """Generate and store a six digit OTP for password reset."""

        token = ''.join(secrets.choice('0123456789') for _ in range(6))
        self.write({
            'password_reset_token': token,
            'password_reset_token_expiry': fields.Datetime.now() + timedelta(minutes=10),
        })
        return token

    def clear_password_reset_token(self):
        """Invalidate an existing password reset token."""

        self.write({
            'password_reset_token': False,
            'password_reset_token_expiry': False,
        })

    def send_password_reset_email(self):
        """Send the password reset OTP using the configured template."""

        try:
            self.send_security_email('forget_password')
        except Exception as exc:
            print("Error sending password reset email", str(exc))

    def send_security_email(self, mail_type, extra_context=None, email_values=None, force_send=True):
        """Send Zeeve authentication e-mails (OTP verification or password reset).

        Parameters
        ----------
        mail_type: str
            Either ``'forget_password'`` or ``'otp_verification'`` to choose the template.
        extra_context: dict | None
            Extra rendering context (e.g., ``{'reset_link': 'https://...'}``).
        email_values: dict | None
            Optional overrides for the outgoing mail (``email_to`` is set automatically).
        force_send: bool
            Forward to :meth:`mail.template.send_mail` ``force_send`` parameter.
        """
        if self.multi_tenant_host == 'iopn' and mail_type == 'otp_verification':
            return opn_util.send_opn_email_by_type(self.env, self, 'otp')
        elif self.multi_tenant_host == 'iopn' and mail_type == 'forget_password':
            # Build reset link for IOPN users
            reset_token = (extra_context or {}).get('reset_token') or self.password_reset_token or self.verification_token
            reset_link = (
                (extra_context or {}).get('reset_link')
                or (extra_context or {}).get('reset_password_link')
                or self._build_iopn_reset_password_link(reset_token)
            )
            return opn_util.send_opn_email_by_type(self.env, self, 'forgot_password', reset_link=reset_link)
        
        template_map = {
            'forget_password': 'auth_module.mail_template_forget_password_zeeve',
            'otp_verification': 'auth_module.mail_template_otp_verification_zeeve',
        }

        xml_id = template_map.get(mail_type)
        if not xml_id:
            raise ValueError("Unsupported mail_type %s" % mail_type)

        template = self.env.ref(xml_id, raise_if_not_found=False)
        if not template:
            return

        extra_context = dict(extra_context or {})
        email_values = dict(email_values or {})

        partners = self.filtered(lambda partner: partner.email)
        if not partners:
            return

        template = template.sudo()
        today = fields.Date.context_today(self)

        for partner in partners:
            ctx = dict(extra_context)
            ctx.setdefault('current_year', today.year)
            ctx.setdefault('partner', partner)

            if mail_type == 'forget_password':
                reset_token = ctx.get('reset_token') or partner.password_reset_token or partner.verification_token
                if reset_token:
                    ctx.setdefault('reset_token', reset_token)
                reset_link = (
                    ctx.get('reset_link')
                    or ctx.get('reset_password_link')
                    or partner._build_reset_password_link(reset_token)
                )
                if reset_link:
                    ctx['reset_link'] = reset_link
            elif mail_type == 'otp_verification':
                ctx.setdefault('otp_code', ctx.get('otp_code') or partner.verification_token or partner.password_reset_token or '000000')

            partner_email_values = dict(email_values)
            partner_email_values.setdefault('email_to', partner.email)

            try:
                template.with_context(**ctx).send_mail(partner.id, email_values=partner_email_values, force_send=force_send)
            except Exception as exc:  # pragma: no cover - avoid crashing flows due to e-mail problems
                print("Error sending %s email" % mail_type, str(exc))

    @api.model
    def _decode_utm_info(self, utm_info):
        """Decode a base64 encoded UTM info query parameter."""

        try:
            padded = utm_info + '=' * (-len(utm_info) % 4)
            return json.loads(base64.urlsafe_b64decode(padded).decode())
        except Exception:
            return {}

    @staticmethod
    def _utm_contains_keyword(utm_data, keyword):
        if not utm_data or not keyword:
            return False
        keyword = keyword.lower()
        return any(keyword in str(value or '').lower() for value in utm_data.values())

    def send_signup_emails(self, utm_info=None):
        """Send greeting and admin notification e-mails after signup."""
        try:
            utm = self._decode_utm_info(utm_info) if utm_info else {}
            if utm:
                _logger.info("Signup emails: storing UTM %s for partners %s", utm, self.ids)
                self.sudo().write({'utm_info': utm})

            shardeum_partners = self.browse()
            if utm:
                if self._utm_contains_keyword(utm, 'shardeum'):
                    shardeum_partners = self
            else:
                shardeum_partners = self.filtered(
                    lambda partner: self._utm_contains_keyword(partner.utm_info or {}, 'shardeum')
                )
            if shardeum_partners:
                _logger.info(
                    "Signup emails: sending Shardeum welcome emails to partners %s (utm=%s, stored=%s)",
                    shardeum_partners.ids,
                    utm,
                    shardeum_partners.mapped('utm_info'),
                )
                print("Sending Shardeum welcome emails to partners %s" % shardeum_partners.ids)
                shardeum_partners.send_shardeum_welcome_email()

            _logger.info("Signup emails: sending welcome emails to partners %s", self.ids)
            self.send_welcome_email()
        except Exception as exc:
            _logger.exception("Signup emails: unexpected error for partners %s", self.ids)

    def store_utm_info(self, utm_info):
        """Persist UTM metadata without sending any e-mails."""

        if not utm_info:
            return
        utm = self._decode_utm_info(utm_info)
        if not utm:
            return
        _logger.info("Signup emails: updating UTM %s for partners %s", utm, self.ids)
        self.sudo().write({'utm_info': utm})

    def _get_auth_frontend_base_url(self):
        """Return the configured base URL for the external auth frontend."""

        ICP = self.env['ir.config_parameter'].sudo()

        base_url = (
            ICP.get_param('frontend_url')
            or ''
        )

        base_url = base_url.strip()
        return base_url.rstrip('/') if base_url else ''

    def _get_iopn_frontend_base_url(self):
        """Return the configured base URL for the IOPN auth frontend."""

        ICP = self.env['ir.config_parameter'].sudo()

        base_url = (
            ICP.get_param('iopn_frontend_url')
            or ''
        )

        base_url = base_url.strip()
        return base_url.rstrip('/') if base_url else ''

    def _build_reset_password_link(self, token=None):
        """Construct a password reset link for the auth frontend.

        Parameters
        ----------
        token: str | None
            Explicit token value. Falls back to the partner's stored reset/verification tokens.
        """

        self.ensure_one()

        token = token or self.password_reset_token or self.verification_token
        if not token:
            return False

        ICP = self.env['ir.config_parameter'].sudo()
        base_url = self._get_auth_frontend_base_url()
        reset_path = ICP.get_param(RESET_PASSWORD_PATH_PARAM)
        reset_path = reset_path.strip()
        if not reset_path.startswith('/'):
            reset_path = '/' + reset_path

        base_url = base_url.rstrip('/')
        query = urlencode({'token': token})
        return f"{base_url}{reset_path}?{query}"

    def _build_iopn_reset_password_link(self, token=None):
        """Construct a password reset link for the IOPN auth frontend.

        Parameters
        ----------
        token: str | None
            Explicit token value. Falls back to the partner's stored reset/verification tokens.
        """

        self.ensure_one()

        token = token or self.password_reset_token or self.verification_token
        if not token:
            return False

        ICP = self.env['ir.config_parameter'].sudo()
        base_url = self._get_iopn_frontend_base_url()
        reset_path = '/account/reset'
        reset_path = reset_path.strip()
        if not reset_path.startswith('/'):
            reset_path = '/' + reset_path

        base_url = base_url.rstrip('/')
        query = urlencode({'token': token})
        return f"{base_url}{reset_path}?{query}"
