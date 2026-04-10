"""Extension of ``res.users`` to support JWT refresh token management."""

import hashlib
import secrets
import bcrypt
from odoo import api, fields, models,_
from odoo.exceptions import AccessDenied

class ResUsers(models.Model):
    """Add storage for hashed refresh tokens and expiry timestamps."""

    _inherit = 'res.users'

    jwt_refresh_token_hash = fields.Char(string='Refresh Token Hash', readonly=True, copy=False)
    jwt_refresh_token_expiry = fields.Datetime(string='Refresh Token Expiry', readonly=True, copy=False)
    jwt_refresh_token_issued_at = fields.Datetime(string='Refresh Token Issued At', readonly=True, copy=False)
    password_changed_at = fields.Datetime(string='Password Changed At', readonly=True, copy=False)
    legacy_password_bcrypt = fields.Char(copy=False)

    company_role = fields.Selection([
        ('super_admin', 'Super Admin'),
        ('admin', 'Admin'),
        ('operator', 'Operator')
    ], string='Company Role', tracking=True)
    node_access_type = fields.Selection([
        ('all', 'All Nodes and Rollups'),
        ('specific', 'Specific Nodes/Rollups')
    ], string='Node/Rollups Access', default='all')
    specific_nodes = fields.Char(string='Specific Nodes (JSON)', help="List of selected nodes if access is specific")
    specific_rollups = fields.Char(string='Specific Rollups (JSON)', help="List of selected rollups if access is specific")
    invited_by = fields.Many2one('res.users', string='Invited By', readonly=True)
    is_company_owner = fields.Boolean(string='Is Company Owner', default=False)

    def check_password_with_legacy(self, plain_password: str) -> bool:
        """Try Odoo native hash first; if it fails and a legacy bcrypt hash exists,
        verify with bcrypt, then migrate to Odoo hash and clear the legacy field."""
        self.ensure_one()

        # 1) Odoo's native check
        try:
            self.with_user(self)._check_credentials(
                {'login': self.login, 'password': plain_password, 'type': 'password'},
                {'interactive': True}
            )
            return True
        except AccessDenied:
            pass

        # 2) Legacy bcrypt fallback
        if self.legacy_password_bcrypt:
            try:
                if bcrypt.checkpw(
                    plain_password.encode('utf-8'),
                    self.legacy_password_bcrypt.encode('utf-8')
                ):
                    # Upgrade hash to Odoo scheme (pbkdf2_sha512) and drop legacy
                    self.sudo().write({"password":plain_password})
                    self.sudo().write({'legacy_password_bcrypt': False})
                    return True
            except Exception as exc:
                print("2----------------",exc)
                pass

        raise AccessDenied(_("Invalid credentials"))

    @staticmethod
    def _hash_refresh_token(token):
        """Return a SHA-256 hash of the provided ``token`` value."""

        if not token:
            return False
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    def _issue_refresh_token(self, lifetime):
        """Generate, store and return a new refresh token."""

        self.ensure_one()
        token = secrets.token_urlsafe(48)
        expiry = fields.Datetime.now() + lifetime
        hashed = self._hash_refresh_token(token)
        self.sudo().write({
            'jwt_refresh_token_hash': hashed,
            'jwt_refresh_token_expiry': expiry,
            'jwt_refresh_token_issued_at': fields.Datetime.now(),
        })
        return token, expiry

    def _clear_refresh_token(self):
        """Remove stored refresh token information."""

        self.sudo().write({
            'jwt_refresh_token_hash': False,
            'jwt_refresh_token_expiry': False,
            'jwt_refresh_token_issued_at': False,
        })

    @api.model
    def _authenticate_refresh_token(self, token):
        """Validate the refresh ``token`` and return ``(user, error)``."""

        hashed = self._hash_refresh_token(token)
        if not hashed:
            return None, 'invalid'

        user = self.sudo().search([('jwt_refresh_token_hash', '=', hashed)], limit=1)
        if not user:
            return None, 'invalid'

        expiry = user.jwt_refresh_token_expiry
        if not expiry:
            user._clear_refresh_token()
            return None, 'invalid'

        if expiry < fields.Datetime.now():
            user._clear_refresh_token()
            return None, 'expired'

        return user, ''

    def _notify_security_setting_update(self, subject, content, mail_values=None, **kwargs):
        """Disable the default security-update email spam after bulk user imports."""
        return False
