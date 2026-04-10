"""Helpers to interact with OAuth2 providers using ``auth.oauth.provider`` records."""

from datetime import timedelta, datetime
import base64
import hmac
import hashlib
from odoo.http import request, root
from odoo import fields
from odoo.tools.mimetypes import guess_mimetype
import logging

_logger = logging.getLogger(__name__)

ir_config_parameter_table = 'ir.config_parameter'
web_base_url_table = 'web.base.url'




# ---------------------------
# Helper: validate required payload
# ---------------------------
def _validate_payload(data, required_fields):
    """Validate that all required fields exist in request payload."""
    missing = [field for field in required_fields if not data.get(field)]
    if missing:
        return False, f"Missing required field(s): {', '.join(missing)}"
    return True, None


def _get_jwt_secret():
    """Retrieve JWT secret key from system parameters."""
    try:
        secret = request.env['ir.config_parameter'].sudo().get_param('jwt_secret_key')
        if not secret:
            _logger.warning("JWT secret key not configured in ir.config_parameter")
            secret = 'default-zeeve-secret-key'
        return secret
    except Exception as e:
        _logger.error(f"Error retrieving JWT secret: {e}")
        return 'default-zeeve-secret-key'


def _encode_token(token, secret):
    """Encode token with HMAC-SHA256 signature using secret.
    Uses URL-safe base64 encoding for safe transmission in URLs.
    """
    try:
        # Encode token and signature separately so the serialized format
        # cannot become ambiguous when raw signature bytes contain dots.
        signature = hmac.new(
            secret.encode('utf-8'),
            token.encode('utf-8'),
            hashlib.sha256
        ).digest()

        encoded_token = base64.urlsafe_b64encode(token.encode('utf-8')).decode('utf-8').rstrip('=')
        encoded_signature = base64.urlsafe_b64encode(signature).decode('utf-8').rstrip('=')
        return f"{encoded_token}.{encoded_signature}"
    except Exception as e:
        _logger.error(f"Error encoding token: {e}")
        return None


def _urlsafe_b64decode(value):
    """Decode URL-safe base64 payloads with optional padding stripped."""
    padding = (-len(value)) % 4
    if padding:
        value += '=' * padding
    return base64.urlsafe_b64decode(value.encode('utf-8'))


def _decode_legacy_token(encoded_token, secret):
    """Decode the original token format kept for backward compatibility."""
    try:
        combined = _urlsafe_b64decode(encoded_token)
        parts = combined.rsplit(b'.', 1)
        if len(parts) != 2:
            return None

        token, received_signature = parts
        expected_signature = hmac.new(
            secret.encode('utf-8'),
            token,
            hashlib.sha256
        ).digest()

        if hmac.compare_digest(received_signature, expected_signature):
            return token.decode('utf-8')
    except Exception:
        return None
    return None


def _decode_token(encoded_token, secret):
    """Decode and validate token using HMAC signature."""
    try:
        if '.' in encoded_token:
            token_part, signature_part = encoded_token.split('.', 1)
            token = _urlsafe_b64decode(token_part)
            received_signature = _urlsafe_b64decode(signature_part)
        else:
            decoded_legacy = _decode_legacy_token(encoded_token, secret)
            if decoded_legacy:
                return decoded_legacy
            _logger.warning("Token signature validation failed")
            return None

        expected_signature = hmac.new(
            secret.encode('utf-8'),
            token,
            hashlib.sha256
        ).digest()

        if hmac.compare_digest(received_signature, expected_signature):
            return token.decode('utf-8')

        decoded_legacy = _decode_legacy_token(encoded_token, secret)
        if decoded_legacy:
            return decoded_legacy

        _logger.warning("Token signature validation failed")
        return None
    except Exception as e:
        _logger.error(f"Error decoding token: {e}")
        return None


def public_image_url(record, field_name="image", size=None):
    """Return a public, signed URL for the binary image field attachment.
    Token is encoded with HMAC-SHA256 signature using jwt_secret_key.
    Auto-regenerates tokens with 24-hour expiration.
    
    :param record: Odoo record (e.g., protocol.master browse record)
    :param field_name: the binary field name storing the image
    :param size: optional string like "64x64" to request resized image
    """
    attach = request.env["ir.attachment"].sudo().search([
        ("res_model", "=", record._name),
        ("res_id", "=", record.id),
        ("res_field", "=", field_name),
        ("type", "=", "binary"),
    ], limit=1)

    if not attach:
        return False

    # Auto-regenerate if expired (24 hour TTL)
    needs_regeneration = (
        not attach.access_token or
        (hasattr(attach, 'access_token_created_at') and 
         attach.access_token_created_at and
         attach.access_token_created_at < datetime.now() - timedelta(hours=24))
    )
    
    if needs_regeneration:
        attach.generate_access_token()
        _logger.info(f"Generated new access token for {record._name}:{record.id}")

    # Get secret key and encode token
    secret = _get_jwt_secret()
    encoded_token = _encode_token(attach.access_token, secret)
    
    if not encoded_token:
        _logger.error(f"Failed to encode token for attachment {attach.id}")
        return False

    base = request.httprequest.url_root.rstrip("/")
    # Force HTTPS only in production (not localhost/127.0.0.1)
    if 'localhost' not in base and '127.0.0.1' not in base:
        base = base.replace("http://", "https://")
    path = f"/web/image/{attach.id}"
    if size:
        path += f"/{size}"
    
    # Return URL with encoded token (not plain text)
    return f"{base}{path}?token={encoded_token}"


def public_image_source(record, field_name="image", size=None):
    """Return a browser-safe image source (data URI fallback for inline SVG avatars)."""

    attach = request.env["ir.attachment"].sudo().search([
        ("res_model", "=", record._name),
        ("res_id", "=", record.id),
        ("res_field", "=", field_name),
        ("type", "=", "binary"),
    ], limit=1)
    if not attach:
        return ""

    try:
        bin_value = base64.b64decode(attach.with_context(bin_size=False).datas or b'')
    except Exception:
        bin_value = b""

    mimetype = attach.mimetype
    if not mimetype or mimetype in ("application/octet-stream", "text/plain"):
        mimetype = guess_mimetype(bin_value or b"", default="application/octet-stream")

    if bin_value:
        snippet = bin_value[:128].decode(errors="ignore").lower()
        if "<svg" in snippet:
            mimetype = "image/svg+xml"

    if mimetype == "image/svg+xml" and bin_value:
        encoded = base64.b64encode(bin_value).decode()
        return f"data:image/svg+xml;base64,{encoded}"

    return public_image_url(record, field_name, size)


def _split_emails(values):
    """Normalize a comma separated value or list into distinct emails."""
    if not values:
        return []
    if isinstance(values, (list, tuple, set)):
        result = []
        for value in values:
            result.extend(_split_emails(value))
        return [email for email in result if email]
    if isinstance(values, str):
        return [email.strip() for email in values.split(',') if email.strip()]
    return []


def _get_admin_recipients(env=None, channel_code=None):
    """Return admin recipient configuration (to/cc lists + config record)."""
    env = env or getattr(request, 'env', None)
    if env is None:
        return {"to": [], "cc": [], "config": None, "channel": None}

    config = env['zeeve.config'].sudo().search([], limit=1)
    channel = None
    if channel_code:
        channel_domain = [('code', '=', channel_code)]
        if config:
            channel_domain.append(('config_id', '=', config.id))
        channel = env['zeeve.admin.channel'].sudo().search(channel_domain, limit=1)
        if not channel and config:
            channel = config.admin_channel_ids.filtered(lambda c: c.code == channel_code)[:1]

    email_to = _split_emails(channel.email_to if channel else None)
    email_cc = _split_emails(channel.email_cc if channel else None)

    if not email_to:
        fallback = config.admin_emails if config else ''
        email_to = _split_emails(fallback)

    if not email_to and env.company and env.company.email:
        email_to = _split_emails(env.company.email)

    return {"to": email_to, "cc": email_cc, "config": config, "channel": channel}


def _get_admin_email_list(env=None, channel_code=None):
    """Return admin email addresses configured in Zeeve settings."""
    recipients = _get_admin_recipients(env, channel_code=channel_code)
    return recipients["to"], recipients["config"]
