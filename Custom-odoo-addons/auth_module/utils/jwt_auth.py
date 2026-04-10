"""Utility helpers for issuing and validating JWT access and refresh tokens."""

from datetime import datetime, timedelta, timezone
import base64
import json
import hmac
import hashlib
import logging
import secrets

import jwt

from odoo.http import request
from odoo.tools import config as odoo_config


_logger = logging.getLogger(__name__)

DEFAULT_ACCESS_TOKEN_LIFETIME = timedelta(minutes=15)
DEFAULT_REFRESH_TOKEN_LIFETIME = timedelta(hours=1)
ACCESS_TOKEN_PARAM = 'access_token_minutes'
REFRESH_TOKEN_PARAM = 'refresh_token_hours'

REFRESH_COOKIE_NAME = 'refresh_token'

def _use_secure_cookie():
    httprequest = getattr(request, 'httprequest', None)
    if not httprequest:
        return True  # default for background tasks
    host = httprequest.host or httprequest.environ.get('HTTP_HOST', '')
    return not (host.startswith('localhost') or host.startswith('127.0.0.1'))


try:  # PyJWT-style exceptions
    from jwt import ExpiredSignatureError, InvalidTokenError  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - fallback when PyJWT is absent
    class ExpiredSignatureError(Exception):  # pylint: disable=too-few-public-methods
        """Raised when a JWT is expired."""

    class InvalidTokenError(Exception):  # pylint: disable=too-few-public-methods
        """Raised when a JWT cannot be decoded or signature is invalid."""

    # Expose the fallback exceptions on the imported ``jwt`` module so callers
    # catching ``jwt.InvalidTokenError`` continue to work even without PyJWT.
    if not hasattr(jwt, 'ExpiredSignatureError'):
        jwt.ExpiredSignatureError = ExpiredSignatureError  # type: ignore[attr-defined]
    if not hasattr(jwt, 'InvalidTokenError'):
        jwt.InvalidTokenError = InvalidTokenError  # type: ignore[attr-defined]


def _b64url_encode(raw):
    data = raw.encode('utf-8') if isinstance(raw, str) else raw
    return base64.urlsafe_b64encode(data).rstrip(b'=')


def _b64url_decode(raw):
    if isinstance(raw, str):
        raw = raw.encode('ascii')
    padding = b'=' * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding)


def _jwt_supports_encode():
    return hasattr(jwt, 'encode') and hasattr(jwt, 'decode')


def _utcnow():
    """Return timezone-aware ``datetime`` representing current UTC time."""

    return datetime.now(timezone.utc)


def _get_secret_key():
    """Fetch the JWT secret key from configuration.

    The helper looks for ``auth_module.jwt_secret_key`` first and falls back to
    ``database.secret`` from the Odoo configuration file.
    """

    icp = request.env['ir.config_parameter'].sudo()
    secret = icp.get_param('jwt_secret_key') or odoo_config.get('database.secret')
    if not secret:
        raise ValueError('JWT secret key is not configured')
    return secret


def _get_config_number(param_key):
    """Return a numeric configuration value or ``None`` when unset/invalid."""

    try:
        icp = request.env['ir.config_parameter'].sudo()
    except Exception:  # pragma: no cover - outside HTTP request
        return None

    value = icp.get_param(param_key)
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        _logger.warning('Invalid numeric value for %s: %s', param_key, value)
        return None


def get_access_token_lifetime():
    """Return the configured access token lifetime as ``timedelta``."""

    minutes = _get_config_number(ACCESS_TOKEN_PARAM)
    if minutes and minutes > 0:
        return timedelta(minutes=minutes)
    return DEFAULT_ACCESS_TOKEN_LIFETIME


def get_refresh_token_lifetime():
    """Return the configured refresh token lifetime as ``timedelta``."""

    hours = _get_config_number(REFRESH_TOKEN_PARAM)
    if hours and hours > 0:
        return timedelta(hours=hours)
    return DEFAULT_REFRESH_TOKEN_LIFETIME


def build_access_payload(user, nonce=None, issued_at=None):
    """Create the payload dictionary for an access token."""

    partner = user.partner_id
    now = issued_at or _utcnow()
    lifetime = get_access_token_lifetime()
    nonce = nonce or secrets.token_hex(16)
    email = partner.email or user.login or ''
    identifier =str(user.id) or user.login or email
    domain = request.httprequest.host
    payload = {
        'id': identifier,
        'email': email,
        'account_id': partner.account_id or '',
        'domain': domain,
        'nonce': nonce,
        'iat': int(now.timestamp()),
        'exp': int((now + lifetime).timestamp()),
    }
    return payload

def build_access_payload_external(email):
    now = _utcnow()
    payload={
        'email': email,
        'iat': int(now.timestamp()),
    }
    return payload

def _sign_hs256(payload, secret):
    header = {'alg': 'HS256', 'typ': 'JWT'}
    header_segment = _b64url_encode(json.dumps(header, separators=(',', ':'), ensure_ascii=False).encode('utf-8'))
    payload_segment = _b64url_encode(json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode('utf-8'))
    signing_input = header_segment + b'.' + payload_segment
    key_bytes = secret.encode('utf-8') if isinstance(secret, str) else secret
    signature = hmac.new(key_bytes, signing_input, hashlib.sha256).digest()
    return b'.'.join([header_segment, payload_segment, _b64url_encode(signature)]).decode('ascii')


def _verify_hs256(token, secret, verify_exp):
    try:
        header_segment, payload_segment, signature_segment = token.split('.')
    except ValueError as exc:
        raise InvalidTokenError('Invalid token format') from exc

    signing_input = f'{header_segment}.{payload_segment}'.encode('ascii')
    key_bytes = secret.encode('utf-8') if isinstance(secret, str) else secret
    expected_sig = _b64url_encode(hmac.new(key_bytes, signing_input, hashlib.sha256).digest()).decode('ascii')
    if not hmac.compare_digest(signature_segment, expected_sig):
        raise InvalidTokenError('Invalid signature')

    try:
        payload = json.loads(_b64url_decode(payload_segment))
    except (json.JSONDecodeError, ValueError) as exc:
        raise InvalidTokenError('Invalid payload') from exc

    if verify_exp and payload.get('exp') is not None:
        if int(_utcnow().timestamp()) > int(payload['exp']):
            raise ExpiredSignatureError('Signature has expired')

    return payload


def _jwt_encode(payload, secret):
    if _jwt_supports_encode():
        _logger.info("inside jwt encode")
        token = jwt.encode(payload, secret, algorithm='HS256')
        return token.decode() if isinstance(token, bytes) else token
    return _sign_hs256(payload, secret)


def _jwt_decode(token, secret, options):
    verify_exp = options.get('verify_exp', True)
    if _jwt_supports_encode():
        return jwt.decode(token, secret, algorithms=['HS256'], options=options)
    return _verify_hs256(token, secret, verify_exp)


def generate_access_token(user):
    """Generate a signed JWT access token for ``user``."""
    print('user',user)
    payload = build_access_payload(user)
    return _jwt_encode(payload, _get_secret_key())


def decode_access_token(token, verify_exp=True):
    """Decode an access token and return its payload."""

    options = {'verify_exp': bool(verify_exp)}
    return _jwt_decode(token, _get_secret_key(), options)


def issue_refresh_token(user):
    """Generate and persist a refresh token for ``user``."""

    token, expiry = user._issue_refresh_token(get_refresh_token_lifetime())
    return token, expiry


def get_refresh_cookie():
    """Return the refresh token from the incoming request cookie."""
    print('request.httprequest.cookies',request.httprequest.cookies)
    return request.httprequest.cookies.get(REFRESH_COOKIE_NAME)


def set_refresh_cookie(response, token):
    """Attach the refresh token as an HTTP-only secure cookie.
    
    SECURITY NOTES:
    ===============
    
    1. SameSite=None Configuration:
       - Set to 'None' to allow cross-site requests from configured frontend origins
       - Requires Secure flag (HTTPS) to function, which is enforced via _use_secure_cookie()
       - Combined with strict CORS validation (auth_allowed_origins parameter)
       - Browsers will only send this cookie if:
         a) Request includes credentials: 'include' (on frontend)
         b) Server responds with Access-Control-Allow-Credentials: true
         c) Request Origin matches one of the configured allowed origins
       
    2. Alternative Configurations:
       - If frontend and backend share the same domain/origin: Use SameSite=Lax or Strict
       - For truly cross-origin APIs: Keep SameSite=None (requires Secure flag)
       
    3. HTTPOnly Flag:
       - Prevents JavaScript access (protects against XSS attacks)
       - Refreshing token requires server-to-server call (no JavaScript access)
       
    4. Secure Flag:
       - Enforced automatically via _use_secure_cookie() (HTTPS only, except localhost)
       - Prevents cookie transmission over unsecured connections
    
    CORS Security Integration:
    ==========================
    This cookie implementation works in conjunction with:
    - CORS headers (cors_headers function): Only sends credentials to allowed origins
    - Origin validation (auth_allowed_origins): Only specific origins get CORS credentials header
    - NO WILDCARD: Access-Control-Allow-Origin never uses '*' with credentials
    
    See: oauth.py cors_headers() function for CORS implementation details
    """

    refresh_lifetime = get_refresh_token_lifetime()
    max_age = int(refresh_lifetime.total_seconds())
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        token,
        max_age=max_age,
        httponly=True,
        secure=_use_secure_cookie(),
        samesite='None',
        path='/',
    )
    return response


def clear_refresh_cookie(response):
    """Remove the refresh token cookie from the response."""

    # Browsers require the deletion cookie to mirror the attributes that were
    # used when the cookie was set (same path, SameSite and Secure flags).
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        '',
        max_age=0,
        expires=0,
        httponly=True,
        secure=_use_secure_cookie(),
        samesite='None',
        path='/',
    )
    return response


def invalidate_refresh_token(user):
    """Invalidate any stored refresh token for ``user``."""

    user._clear_refresh_token()


def authenticate_refresh_token(token):
    """Return ``(user, error)`` for the provided refresh token value."""

    if not token:
        return None, 'missing'
    return request.env['res.users'].sudo()._authenticate_refresh_token(token)
