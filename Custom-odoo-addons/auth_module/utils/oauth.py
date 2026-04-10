"""Helpers to interact with OAuth2 providers using ``auth.oauth.provider`` records."""

from datetime import timedelta, datetime, timezone
import base64
import json
import requests
import werkzeug.urls
import jwt
from odoo.http import request
from odoo import fields
import logging
from . import jwt_auth
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor, as_completed


ir_config_parameter_table = 'ir.config_parameter'
backend_url_table = 'backend_url'
google_client_secret='google_client_secret'
git_client_secret='git_client_secret'
apple_client_secret='apple_client_secret'
DEFAULT_ALLOWED_METHODS = ('GET', 'POST', 'OPTIONS')
_logger = logging.getLogger(__name__)


def _normalise_origin(origin):
    return (origin or '').strip().rstrip('/')


def _get_allowed_origins():
    """Return the set of allowed origins from system parameters.
    
    SECURITY NOTE: Wildcard '*' is no longer supported in auth_allowed_origins
    to prevent insecure CORS configurations with credentials.
    Only specific origins should be configured.
    """

    icp = request.env['ir.config_parameter'].sudo()
    raw = icp.get_param('auth_allowed_origins') or ''
    if not raw.strip():
        return set()
    entries = []
    has_wildcard = False
    for token in raw.replace('\n', ',').split(','):
        token = _normalise_origin(token)
        if token:
            if token == '*':
                has_wildcard = True
                _logger.warning(
                    'SECURITY WARNING: Wildcard "*" found in auth_allowed_origins. '
                    'This is no longer supported to prevent CORS+Credentials vulnerabilities. '
                    'Configure specific origins instead (e.g., "https://example.com,https://app.example.com")'
                )
                continue  # Skip wildcard, don't add it to allowed origins
            entries.append(token)
    
    if has_wildcard and not entries:
        _logger.error(
            'CORS MISCONFIGURATION: auth_allowed_origins contains only wildcard "*" '
            'and no specific origins. API will reject all cross-origin requests. '
            'Please configure specific allowed origins.'
        )
    
    return set(entries)


def _is_origin_allowed(origin):
    """Check if an origin is in the list of allowed origins.
    
    Note: Wildcard '*' is never allowed to ensure CORS+Credentials security.
    """
    allowed = _get_allowed_origins()
    if not allowed:
        return False
    # Note: Wildcard check removed - '*' is no longer supported
    return _normalise_origin(origin) in allowed


def validate_cors_configuration():
    """Validate CORS configuration and log security warnings if misconfigured.
    
    Should be called during module initialization to ensure proper configuration.
    Checks that auth_allowed_origins contains specific origins (not empty or wildcard).
    """
    try:
        icp = request.env['ir.config_parameter'].sudo()
        raw = (icp.get_param('auth_allowed_origins') or '').strip()
        
        if not raw:
            _logger.warning(
                'CORS SECURITY: auth_allowed_origins is not configured. '
                'API will reject all cross-origin requests. '
                'Please set specific allowed origins in System Parameters.'
            )
            return False
        
        allowed_origins = _get_allowed_origins()
        if not allowed_origins:
            _logger.warning(
                'CORS SECURITY: auth_allowed_origins is configured but empty after parsing. '
                'Please check the configuration format. Expected format: '
                'https://frontend.example.com,https://app.example.com (comma or newline separated)'
            )
            return False
        
        if '*' in allowed_origins:
            _logger.error(
                'CORS SECURITY: Wildcard "*" in auth_allowed_origins is no longer supported. '
                'This configuration has been rejected to prevent CORS+Credentials vulnerabilities. '
                'Please configure specific origins instead.'
            )
            return False
        
        # Log success for audit purposes
        _logger.info(
            'CORS configuration validated successfully. Allowed origins: %s',
            ', '.join(sorted(allowed_origins))
        )
        return True
        
    except Exception as e:
        _logger.exception('Error validating CORS configuration: %s', str(e))
        return False


def jwt_verification_external():
    """Decode and verify a JWT token using the secret key from system parameters."""
    auth_header = request.httprequest.headers.get('Authorization', '')
    if not auth_header:
        return None, 'missing', None
    if not auth_header.lower().startswith('bearer '):
        return None, 'invalid', None
    token = auth_header[7:]

    secret_key = request.env['ir.config_parameter'].sudo().get_param('jwt_secret_external')
    if not secret_key:
        _logger.error('External JWT secret key is not configured in system parameters.')
        return None, 'missing_secret'
    
    try:
        # Verify and decode the token without expiry verification
        payload = jwt_auth._verify_hs256(token, secret_key, verify_exp=False)
        _logger.info('JWT token verified successfully for external use. %s', payload)
        return payload, None , token
    except jwt_auth.InvalidTokenError as e:
        _logger.error('Invalid JWT token: %s', str(e))
        return None, 'invalid', None
    except Exception as e:
        _logger.exception('Error verifying JWT token: %s', str(e))
        return None, 'error', None


def generate_external_access_token(email):
    """Generate a signed JWT access token for external use with jwt_secret_external."""
    secret_key = request.env['ir.config_parameter'].sudo().get_param('jwt_secret_external')
    if not secret_key:
        _logger.error('External JWT secret key is not configured in system parameters.')
        raise ValueError('External JWT secret key is not configured')
    
    payload = jwt_auth.build_access_payload_external(email)
    _logger.info('Generating external access token with payload: %s', payload)
    return jwt_auth._jwt_encode(payload, secret_key)

def jwt_verification_external():
    """Decode and verify a JWT token using the secret key from system parameters."""
    auth_header = request.httprequest.headers.get('Authorization', '')
    if not auth_header:
        return None, 'missing', None
    if not auth_header.lower().startswith('bearer '):
        return None, 'invalid', None
    token = auth_header[7:]

    secret_key = request.env['ir.config_parameter'].sudo().get_param('jwt_secret_external')
    if not secret_key:
        _logger.error('External JWT secret key is not configured in system parameters.')
        return None, 'missing_secret'
    
    try:
        # Verify and decode the token without expiry verification
        payload = jwt_auth._verify_hs256(token, secret_key, verify_exp=False)
        _logger.info('JWT token verified successfully for external use. %s', payload)
        return payload, None , token
    except jwt_auth.InvalidTokenError as e:
        _logger.error('Invalid JWT token: %s', str(e))
        return None, 'invalid', None
    except Exception as e:
        _logger.exception('Error verifying JWT token: %s', str(e))
        return None, 'error', None


def generate_external_access_token(email):
    """Generate a signed JWT access token for external use with jwt_secret_external."""
    secret_key = request.env['ir.config_parameter'].sudo().get_param('jwt_secret_external')
    if not secret_key:
        _logger.error('External JWT secret key is not configured in system parameters.')
        raise ValueError('External JWT secret key is not configured')
    
    payload = jwt_auth.build_access_payload_external(email)
    _logger.info('Generating external access token with payload: %s', payload)
    return jwt_auth._jwt_encode(payload, secret_key)


def create_vizion_user(user):
    """
    Given an Odoo user record, generate a random password, build payload, and create Vizion user via external API.
    Returns dict: {"success": bool, "data": result, "email": user_email, "password": password, "error": str}
    """
    import string, random
    try:
        partner = user.partner_id
        user_email = partner.email
        password_chars = string.ascii_letters + string.digits
        password = ''.join(random.choice(password_chars) for _ in range(10))
        vision_base_url = request.env['ir.config_parameter'].sudo().get_param('vision_base_url')
        if not vision_base_url:
            _logger.error("Vision base URL not configured.")
            return {"success": False, "data": {}, "email": user_email, "password": password, "error": "Vision base URL not configured."}
        vision_api_url = f"{vision_base_url}/api/auth/create-user"
        origin = request.env['ir.config_parameter'].sudo().get_param('backend_url')
        headers = {
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": origin,
        }
        payload = {
            "username": user_email,
            "password": password
        }
        response = requests.post(vision_api_url, json=payload, headers=headers, timeout=20)
        if response.status_code not in (200, 201):
            _logger.error("Vizion API error: %s - %s", response.status_code, response.text)
            return {
                "success": False,
                "data": {},
                "email": user_email,
                "password": password,
                "error": f"Vision API returned {response.status_code}: {response.text}",
                "status": response.status_code
            }
        result = response.json()
        _logger.info("Vizion user created: %s", result)
        return {
            "success": True,
            "data": result,
            "email": user_email,
            "password": password,
            "error": "",
            "status": response.status_code

        }
    except Exception as e:
        _logger.exception("Error creating Vizion user: %s", str(e))
        return {"success": False, "data": {}, "email": None, "password": None, "error": str(e)}
    

def _verify_recaptcha_token(token, expected_action=None):
    """Validate a Google reCAPTCHA v3 token using secret stored in system parameters."""

    secret = request.env['ir.config_parameter'].sudo().get_param('recaptcha_secret')
    if not secret:
        _logger.error('reCAPTCHA secret is not configured in system parameters.')
        return None
    if not token:
        _logger.info('Missing reCAPTCHA token in request payload.')
        return False

    payload = {
        'secret': secret,
        'response': token,
    }
    remote_ip = request.httprequest.environ.get('REMOTE_ADDR')
    if remote_ip:
        payload['remoteip'] = remote_ip

    try:
        response = requests.post(
            'https://www.google.com/recaptcha/api/siteverify',
            data=payload,
            timeout=5,
        )
        response.raise_for_status()
        result = response.json()
    except Exception as exc:
        _logger.exception('Error while verifying reCAPTCHA token: %s', exc)
        return False

    if not result.get('success'):
        _logger.info('reCAPTCHA verification failed: %s', result.get('error-codes'))
        return False

    try:
        min_score = float(
            request.env['ir.config_parameter'].sudo().get_param('recaptcha_min_score', 0.5)
        )
    except (TypeError, ValueError):
        min_score = 0.5

    score = float(result.get('score', 0))
    if score < min_score:
        _logger.info('reCAPTCHA score %.2f below required threshold %.2f', score, min_score)
        return False

    action = result.get('action')
    expected = expected_action or request.env['ir.config_parameter'].sudo().get_param(
        'recaptcha_login_action'
    )
    if expected and action and action != expected:
        _logger.info('reCAPTCHA action mismatch: expected %s got %s', expected, action)
        return False

    return True


def safe_redirect_target(target):
    """Return a sanitized redirect target limited to allowed origins or internal paths."""

    if not target:
        return None

    # Decode the target URL
    target = unquote(target)

    parsed = werkzeug.urls.url_parse(target)
    if parsed.scheme:
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if not _is_origin_allowed(origin):
            _logger.debug("safe_redirect_target: origin %s not allowed", origin)
            return None
        return parsed.to_url()

    if parsed.path and parsed.path.startswith('/'):
        url = parsed.path
        if parsed.query:
            url += f"?{parsed.query}"
        if parsed.fragment:
            url += f"#{parsed.fragment}"
        return url

    _logger.debug("safe_redirect_target: rejected target %s", target)
    return None


def resolve_frontend_redirect(service_url=None):
    """Return a frontend-bound redirect target for the OAuth callback.

    Parameters
    ----------
    service_url: str | None
        Optional URL provided by the frontend to redirect the user after
        authentication.

    Raises
    ------
    ValueError
        If no valid frontend redirect can be computed.
    """
    _logger.info("%s service url---1111-------", service_url)

    icp = request.env['ir.config_parameter'].sudo()
    frontend_base = (icp.get_param('frontend_url') or '').strip().rstrip('/')
    if not frontend_base:
        raise ValueError('frontend_url system parameter is not configured')

    frontend_parsed = werkzeug.urls.url_parse(frontend_base)
    if not frontend_parsed.scheme or not frontend_parsed.netloc:
        raise ValueError('frontend_url must be an absolute URL')

    def _normalise(candidate):
        candidate = (candidate or '').strip()
        if not candidate:
            _logger.debug('resolve_frontend_redirect: blank candidate redirect')
            return None

        parsed_candidate = werkzeug.urls.url_parse(candidate)
        if parsed_candidate.scheme:
            path = parsed_candidate.path or '/'
            if parsed_candidate.query:
                path += f"?{parsed_candidate.query}"
            if parsed_candidate.fragment:
                path += f"#{parsed_candidate.fragment}"
            if parsed_candidate.netloc != frontend_parsed.netloc:
                _logger.debug(
                    'resolve_frontend_redirect: candidate host %s differs from frontend %s, using path %s',
                    parsed_candidate.netloc,
                    frontend_parsed.netloc,
                    path,
                )
            candidate = path

        safe = safe_redirect_target(candidate)
        if not safe:
            _logger.debug('resolve_frontend_redirect: candidate %s rejected by sanitizer', candidate)
            return None

        if safe.startswith('/'):
            return f"{frontend_base}{safe}"
        return safe

    redirect_target = _normalise(service_url)
    if redirect_target:
        _logger.info('resolve_frontend_redirect: redirecting to %s', redirect_target)
        return redirect_target

    fallback_candidates = [
        icp.get_param('auth_module.oauth_default_redirect'),
        frontend_base,
    ]
    _logger.debug('resolve_frontend_redirect: attempting fallbacks %s', fallback_candidates)
    for candidate in fallback_candidates:
        redirect_target = _normalise(candidate)
        if redirect_target:
            _logger.info('resolve_frontend_redirect: using fallback %s', redirect_target)
            return redirect_target

    raise ValueError('No valid frontend redirect target configured')


def make_frontend_redirect_response(url):
    """Return an HTML response that enforces a frontend redirect."""

    body = f"""
    <html>
      <head>
        <meta http-equiv="refresh" content="0;url={url}" />
        <title>Redirecting…</title>
      </head>
      <body>
        <script>window.location.replace({json.dumps(url)});</script>
        <noscript>
          <p>Redirecting to <a href="{url}">{url}</a>…</p>
        </noscript>
      </body>
    </html>
    """.strip()

    response = request.make_response(
        body,
        headers=[
            ('Content-Type', 'text/html; charset=utf-8'),
            ('Location', url),
        ],
        status=303,
    )
    return response


def _decode_jwt(token):
    """Decode a JWT without verifying its signature."""

    try:
        payload_part = token.split('.')[1]
        padded = payload_part + '=' * (-len(payload_part) % 4)
        return json.loads(base64.urlsafe_b64decode(padded).decode())
    except Exception:
        return {}

def cors_headers(methods=None):
    """Return CORS headers based on the current request origin.
    
    SECURITY NOTE: This function enforces strict CORS policies:
    - Only specific allowed origins receive credentials (Access-Control-Allow-Credentials: true)
    - Wildcard '*' is never used as Access-Control-Allow-Origin when credentials are needed
    - Requests without a valid Origin header or from disallowed origins do not receive credential headers
    """

    origin = request.httprequest.headers.get('Origin')
    allow_origin = None
    allow_credentials = False
    
    # SECURITY: Only grant CORS access if origin is explicitly allowed
    if origin and _is_origin_allowed(origin):
        allow_origin = origin
        allow_credentials = True
    
    # If no origin header or origin not allowed, don't set Access-Control-Allow-Origin header
    # This is more secure than using '*' as a fallback
    if not allow_origin:
        _logger.warning('CORS: Blocked request from unauthorized origin: %s. Path: %s. Allowed: %s', 
                        origin, request.httprequest.path, _get_allowed_origins())
        # Return minimal CORS headers without credentials for unauthenticated requests
        allow_origin = None
        allow_credentials = False

    allowed_methods = {m.upper() for m in DEFAULT_ALLOWED_METHODS}
    if methods:
        if isinstance(methods, (list, tuple, set)):
            allowed_methods.update(m.upper() for m in methods)
        else:
            allowed_methods.add(str(methods).upper())
    allowed_methods.add('OPTIONS')

    requested_headers = request.httprequest.headers.get('Access-Control-Request-Headers', '')
    allowed_headers = {'Content-Type', 'Authorization', 'Accept', 'X-Requested-With', 'X-Auth-Token', 'X-CSRF-Token'}
    if requested_headers:
        allowed_headers.update({header.strip() for header in requested_headers.split(',') if header.strip()})

    headers = []
    
    # Only add Access-Control-Allow-Origin if we have a specific allowed origin
    # This prevents using wildcard '*' with credentials
    if allow_origin:
        headers.append(('Access-Control-Allow-Origin', allow_origin))
        headers.append(('Access-Control-Allow-Methods', ', '.join(sorted(allowed_methods))))
        headers.append(('Access-Control-Allow-Headers', ', '.join(sorted(allowed_headers))))
        headers.append(('Access-Control-Expose-Headers', 'Content-Type, Content-Length'))
        headers.append(('Access-Control-Max-Age', '86400'))
        
        # SECURITY: Always set credentials flag when we have a specific origin
        # This is required for requests using cookies (e.g., refresh token)
        if allow_credentials:
            headers.append(('Access-Control-Allow-Credentials', 'true'))
    
    # Always add Vary header to indicate that response varies by Origin
    headers.append(('Vary', 'Origin'))
    if requested_headers:
        headers.append(('Vary', 'Access-Control-Request-Headers'))
    
    return headers


def preflight_response(methods=None):
    """Return an empty HTTP response for OPTIONS requests."""

    return request.make_response('', headers=cors_headers(methods))


def make_json_response(payload, status=200):
    """Return a JSON response applying standard CORS headers."""

    response = request.make_json_response(payload, status=status)
    for header, value in cors_headers():
        response.headers[header] = value
    return response

def _get_provider(provider_name):
    """Return the ``auth.oauth.provider`` record matching ``provider_name``.

    Parameters
    ----------
    provider_name: str
        Provider identifier coming from the route (e.g. ``google``).
    """

    return request.env['auth.oauth.provider'].sudo().search([
        ('enabled', '=', True),
        ('name', 'ilike', provider_name),
    ], limit=1)


def _redirect_uri(provider_name):
    """Compute redirect URI for a provider callback."""
    ICP = request.env[ir_config_parameter_table].sudo()
    base_url = ICP.get_param(backend_url_table)

    return f"{base_url}/api/v1/oauth/{provider_name}/callback"


def build_authorize_url(provider_name, state):
    """Build the provider authorization URL from its configuration."""

    provider = _get_provider(provider_name)
    if not provider:
        raise ValueError('Unknown provider')
    params = {
        'response_type': 'code',
        'client_id': provider.client_id,
        'redirect_uri': _redirect_uri(provider_name),
        'scope': provider.scope or '',
        'state': state,
    }
    return f"{provider.auth_endpoint}?{werkzeug.urls.url_encode(params)}"


def _provider_client_secret(provider_name, provider):
    """Return the configured client secret for ``provider_name``.

    Prefers the value stored on the ``auth.oauth.provider`` record but
    gracefully falls back to legacy system parameters so previous deployments
    keep working.
    """

    if not provider:
        raise ValueError('Unknown provider')

    secret = (getattr(provider, 'client_secret', '') or '').strip()
    if secret:
        return secret

    param_map = {
        'google': google_client_secret,
        'github': git_client_secret,
        'git': git_client_secret,
        'apple': apple_client_secret,
    }
    param_key = param_map.get((provider_name or '').lower())
    if not param_key:
        return ''

    secret = (request.env[ir_config_parameter_table]
                     .sudo()
                     .get_param(param_key) or '')
    return secret.strip()


def _extract_provider_error(resp):
    """Return a human-friendly error message from a failed token response."""

    message = resp.text
    try:
        data = resp.json()
        message = data.get('error_description') or data.get('error') or message
    except ValueError:
        message = resp.text
    return message or 'OAuth provider returned an error'


def exchange_code(provider_name, code):
    """Exchange an authorization ``code`` for tokens.

    Returns
    -------
    tuple
        ``(access_token, refresh_token, expiry, id_token)`` where ``id_token``
        is provided by some providers (e.g. Apple).
    """

    provider = _get_provider(provider_name)
    if not provider:
        raise ValueError('Unknown provider')

    client_secret = _provider_client_secret(provider_name, provider)
    if not client_secret:
        raise ValueError('OAuth provider misconfigured: missing client secret')

    data = {
        'code': code,
        'client_id': provider.client_id,
        'client_secret': client_secret,
        'redirect_uri': _redirect_uri(provider_name),
        'grant_type': 'authorization_code',
    }
    headers = {'Accept': 'application/json'}
    resp = requests.post(provider.data_endpoint, data=data, headers=headers, timeout=10)
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:  # pragma: no cover - network error mapping
        raise ValueError(_extract_provider_error(resp)) from exc

    payload = resp.json()
    expires_in = payload.get('expires_in', 0)
    expiry = fields.Datetime.now() + timedelta(seconds=int(expires_in)) if expires_in else False
    return (
        payload.get('access_token'),
        payload.get('refresh_token'),
        expiry,
        payload.get('id_token'),
    )


def fetch_user_info(provider_name, access_token, id_token=None):
    """Fetch user information from the provider using ``access_token``."""

    provider = _get_provider(provider_name)
    if not provider:
        raise ValueError('Unknown provider')
    headers = {'Authorization': f'Bearer {access_token}', 'Accept': 'application/json'}

    if provider_name.lower() == 'apple':
        info = _decode_jwt(id_token) if id_token else {}
        if not info.get('email'):
            info.update(
                requests.get(
                    'https://appleid.apple.com/auth/userinfo',
                    headers=headers,
                    timeout=10,
                ).json()
            )
        return {
            'id': info.get('sub'),
            'email': info.get('email'),
            'name': info.get('name') or (info.get('email') and info['email'].split('@')[0]),
        }

    info = requests.get(provider.validation_endpoint, headers=headers, timeout=10).json()
    if provider_name.lower() == 'github' and not info.get('email'):
        emails = requests.get('https://api.github.com/user/emails', headers=headers, timeout=10).json()
        if isinstance(emails, list) and emails:
            info['email'] = emails[0].get('email')
    return {
        'id': info.get('sub') or info.get('id'),
        'email': info.get('email'),
        'name': info.get('name') or info.get('login'),
    }


def _json_response(success, data=None, error="", status=200):
    """Utility to build a consistent JSON HTTP response."""

    payload = {
        "success": bool(success),
        "data": data or {},
        "message": error or "",
    }
    return make_json_response(payload, status=status)


def _user_from_token():
    """Return ``(user, error_code)`` based on the JWT access token header."""

    auth_header = request.httprequest.headers.get('Authorization', '')
    if not auth_header:
        return None, 'missing'
    if not auth_header.lower().startswith('bearer '):
        return None, 'invalid'
    token = auth_header[7:]

    try:
        payload = jwt_auth.decode_access_token(token)
    except jwt.ExpiredSignatureError:
        return None, 'expired'
    except jwt.InvalidTokenError:
        return None, 'invalid'
    user = None
    identifier = payload.get('id')
    if identifier:
        try:
            user_id = int(identifier)
            user = request.env['res.users'].sudo().browse(user_id)
            if not user.exists():
                user = None
        except (TypeError, ValueError):
            user = None

    if not user:
        login = payload.get('login') or payload.get('email')
        if login:
            user = request.env['res.users'].sudo().search(
                ['|', ('login', '=', login), ('email', '=', login)],
                limit=1,
            )
    if not user:
        return None, 'invalid'
    return user, ''


def _get_image_url():
    ICP = request.env[ir_config_parameter_table].sudo()
    base_url = ICP.get_param(backend_url_table) or request.httprequest.host_url[:-1]
    return f"{base_url}/web/image"


def require_user():
    """
    Authenticate user from access token.
    Returns (user, None) if valid, (None, resp) if invalid.
    """
    user, token_err = _user_from_token()
    if not user:
        messages = {
            "missing": "Missing access token",
            "expired": "Access token expired",
            "invalid": "Invalid access token",
        }
        resp = _json_response(
            False,
            {"error": messages.get(token_err, "Invalid access token")},
            status=401,
        )
        return None, resp

    # update environment for this request
    request.update_env(user=user.id)
    return user, None


def login_with_email(email):
    """
    Helper function to login Vizion user via external API using email.
    """
    vision_base_url = request.env['ir.config_parameter'].sudo().get_param('vision_base_url')
    origin = request.env['ir.config_parameter'].sudo().get_param('backend_url')
    headers = {
        "Content-Type": "application/json",
        "Origin": origin,
        "Referer": origin,
    }
    # External API endpoint
    vision_api_url = f"{vision_base_url}/api/auth/login-with-email"

    payload = {
        "username": email,
    }

    # Send POST request to Vision API
    response = requests.post(vision_api_url, json=payload, headers=headers, timeout=20)

    if response.status_code not in (200, 201):
        _logger.error("Vision API error: %s - %s", response.status_code, response.text)
        return None

    result = response.json()
    return result


def _parse_entry_date(entry):
    """Return the UTC date for a Vision entry using clock/timestamp values."""
    clock = entry.get('clock')
    if clock is not None:
        try:
            return datetime.fromtimestamp(int(clock), tz=timezone.utc).date()
        except (ValueError, OSError, TypeError):
            pass
    timestamp_str = entry.get('timestamp')
    if not timestamp_str:
        return None
    try:
        return datetime.strptime(timestamp_str, '%a %b %d %Y').date()
    except (ValueError, TypeError):
        return None


def _sum_multi_day_values(data_array):
    """Sum all value_avg entries for multi-day ranges."""
    total = 0
    for entry in data_array:
        total += _safe_int(entry.get('value_avg', 0))
    return total


def _sum_single_day_values(data_array):
    """Calculate count for 1-day ranges based on daily deltas."""
    if not data_array:
        return 0

    # For single day, pick ONLY the last entry's value_avg
    last_entry = data_array[-1]
    return _safe_int(last_entry.get('value_avg', 0))


def sum_last_values(method_data, number_of_days):
    """
    Aggregate method counts based on the requested range.
    
    For a single day, accumulate the deltas from previous dates and append the
    latest value from today. For longer ranges, simply sum the value_avg entries
    returned by the Vision API for each method.
    """
    total_sum = 0
    
    for method_name, data_array in method_data.items():
        if not isinstance(data_array, list) or not data_array:
            continue

        if number_of_days == 1:
            total_sum += _sum_single_day_values(data_array)
        else:
            total_sum += _sum_multi_day_values(data_array)
    
    return {
        'total_sum': int(total_sum),
    }


def _safe_int(value):
    """Convert Vision API value_avg entries to int, fallback to 0."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _utc_timestamp_iso():
    """Return current UTC timestamp in ISO format with trailing Z."""
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def summarize_method_data(method_data, number_of_days):
    """
    Prepare helper aggregates from method count payload received from Vision.
    
    Args:
        method_data: Dictionary containing method count arrays
        number_of_days: Range requested by client
        
    Returns:
        Dictionary with aggregated counts per method along with convenience maps
    """
    total_sum = 0
    latest_counts = {}
    latest_entries = {}
    
    for method_name, data_array in method_data.items():
        if not isinstance(data_array, list) or len(data_array) == 0:
            continue

        if number_of_days == 1:
            aggregated_value = _sum_single_day_values(data_array)
        else:
            aggregated_value = _sum_multi_day_values(data_array)

        last_entry = data_array[-1]
        total_sum += aggregated_value
        latest_counts[method_name] = aggregated_value
        latest_entries[method_name] = last_entry
    
    return {
        'total_sum': int(total_sum),
        'method_data': method_data,
        'latest_counts': latest_counts,
        'latest_entries': latest_entries,
    }


def get_method_count_for_host(primary_host, token, number_of_days, env_config=None):
    """
    Helper function to get method count for a specific host.
    
    Args:
        primary_host: The primary host ID
        token: Authorization token
        
    Returns:
        Sum of method counts or None if error
    """
    try:
        current_time = _utc_timestamp_iso()
        
        if env_config:
            vision_base_url = env_config.get('vision_base_url')
            origin = env_config.get('origin')
        else:
            config_env = request.env['ir.config_parameter'].sudo()
            vision_base_url = config_env.get_param('vision_base_url')
            origin = config_env.get_param('backend_url')

        if not vision_base_url or not origin:
            _logger.error(
                "Missing configuration for method count request: base_url=%s origin=%s",
                vision_base_url,
                origin,
            )
            return None

        vision_api_url = f"{vision_base_url}/api/history/get-eth-method-trend"
        
        headers = {
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": origin,
            "Authorization": f"Bearer {token}",
        }
        
        payload = {
            "numOfDays": number_of_days,
            "primaryHost": primary_host,
            "currentTime": current_time
        }
        
        response = requests.post(vision_api_url, json=payload, headers=headers, timeout=20)
        
        if response.status_code not in (200, 201):
            _logger.error("Vision API error for host %s: %s - %s", primary_host, response.status_code, response.text)
            return None
        
        result = response.json()
        
        if not result.get('success'):
            _logger.error("Vision API returned unsuccessful response for host: %s", primary_host)
            return None
        
        method_data = result.get('data', {})
        summed_data = sum_last_values(method_data, number_of_days)
        
        return summed_data.get('total_sum', 0)
        
    except Exception as e:
        _logger.exception("Error getting method count for host %s: %s", primary_host, str(e))
        return None


def _fetch_method_trend_data(primary_host, token, number_of_days):
    """
    Call Vision API to fetch method trend data for a host.
    
    Returns the raw method data dict or None if the API fails.
    """
    try:
        current_time = _utc_timestamp_iso()
        
        vision_base_url = request.env['ir.config_parameter'].sudo().get_param('vision_base_url')
        vision_api_url = f"{vision_base_url}/api/history/get-eth-method-trend"
        
        origin = request.env['ir.config_parameter'].sudo().get_param('backend_url')
        headers = {
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": origin,
            "Authorization": f"Bearer {token}",
        }
        
        payload = {
            "numOfDays": number_of_days,
            "primaryHost": primary_host,
            "currentTime": current_time
        }
        
        response = requests.post(vision_api_url, json=payload, headers=headers, timeout=20)
        
        if response.status_code not in (200, 201):
            _logger.error("Vision API error for host %s: %s - %s", primary_host, response.status_code, response.text)
            return None
        
        result = response.json()
        
        if not result.get('success'):
            _logger.error("Vision API returned unsuccessful response for host: %s", primary_host)
            return None
        
        return result.get('data', {})
        
    except Exception as e:
        _logger.exception("Error getting method trend for host %s: %s", primary_host, str(e))
        return None


def _normalize_bulk_method_trend_data(raw_data, requested_host_ids):
    """Return a mapping of host identifiers to their method payloads."""

    normalized = {}
    if not raw_data:
        return normalized

    requested_keys = {str(h) for h in requested_host_ids if h not in (None, "")}

    def _store_aliases(payload, *host_keys):
        if not isinstance(payload, dict):
            return
        for host_key in host_keys:
            if host_key in (None, ''):
                continue
            normalized[str(host_key)] = payload

    def _extract_from_list(entries):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            primary_host_key = (
                entry.get("hostId")
                or entry.get("host_id")
                or entry.get("networkId")
                or entry.get("primaryHost")
            )
            payload = (
                entry.get("methodData")
                or entry.get("data")
                or entry.get("methods")
                or entry.get("method_count")
            )
            host_keys = [primary_host_key]
            host_ids_list = entry.get("hostIds") or entry.get("rpc-id") or []
            if isinstance(host_ids_list, str):
                host_ids_list = [item.strip() for item in host_ids_list.split(',')]
            if isinstance(host_ids_list, list):
                host_keys.extend(host_ids_list)
            _store_aliases(payload, *host_keys)

    if isinstance(raw_data, dict):
        keys_matched = False
        for key, value in raw_data.items():
            if not isinstance(value, dict):
                continue
            key_str = str(key)
            if not requested_keys or key_str in requested_keys:
                _store_aliases(value, key_str)
                keys_matched = True

        if not keys_matched:
            for nested_key in ("hosts", "hostData", "items"):
                nested = raw_data.get(nested_key)
                if isinstance(nested, list):
                    _extract_from_list(nested)

            if not normalized and len(requested_keys) == 1:
                only_key = next(iter(requested_keys)) if requested_keys else None
                _store_aliases(raw_data, only_key)
    elif isinstance(raw_data, list):
        _extract_from_list(raw_data)

    return normalized


def _fetch_bulk_method_trend_data(host_ids, token, number_of_days):
    """Fetch method trends for multiple hosts in a single API call."""

    if not host_ids:
        return {}

    try:
        current_time = _utc_timestamp_iso()
        vision_base_url = request.env['ir.config_parameter'].sudo().get_param('vision_base_url')
        vision_api_url = f"{vision_base_url}/api/history/get-eth-method-trend-bulk"

        origin = request.env['ir.config_parameter'].sudo().get_param('backend_url')
        headers = {
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": origin,
            "Authorization": f"Bearer {token}",
        }

        payload = {
            "numOfDays": number_of_days,
            "hostIds": host_ids,
            "currentTime": current_time,
        }

        response = requests.post(vision_api_url, json=payload, headers=headers, timeout=20)
        response.raise_for_status()
        result = response.json()
        if not result.get('success'):
            _logger.error("Vision API returned unsuccessful response for hostIds: %s", host_ids)
            return {}

        raw_data = result.get('data')
        return _normalize_bulk_method_trend_data(raw_data, host_ids)
    except Exception as exc:  # pragma: no cover - defensive log
        _logger.exception("Error fetching bulk method trend data: %s", exc)
        return {}


def get_method_trend_for_host(primary_host, token, number_of_days):
    """
    Helper to fetch the detailed trend data for a specific host.
    
    Returns:
        Dictionary with total sum, raw method data and latest counts, or None if error
    """
    method_data = _fetch_method_trend_data(primary_host, token, number_of_days)
    if method_data is None:
        return None
    return summarize_method_data(method_data, number_of_days)


def _select_host_identifier(host):
    """
    Return the preferred Vision host identifier for bulk requests.
    Prioritize hasLB (if defined and not 'no'), otherwise primaryHost,
    otherwise the first entry from hostIds/rpc-id, or fallback to networkId.
    """
    has_lb = host.get('hasLB')
    if isinstance(has_lb, str):
        has_lb = has_lb.strip()
        if has_lb and has_lb.lower() != 'no':
            return has_lb.split(',')[0].strip()

    primary_host = host.get('primaryHost')
    if primary_host:
        return str(primary_host).strip()

    host_ids = host.get('hostIds') or host.get('rpc-id') or []
    if isinstance(host_ids, str):
        host_ids = [token.strip() for token in host_ids.split(',')]
    for candidate in host_ids:
        candidate_str = str(candidate).strip()
        if candidate_str:
            return candidate_str

    network_id = host.get('networkId')
    if network_id:
        return str(network_id).strip()

    return None


def get_all_hosts_method_count(number_of_days):
    """
    Get method count for all hosts when no specific node is requested.
    
    Returns:
        JSON response with method counts for all hosts
    """
    try:
        # Login with email to get host data
        test_email = "hello@optimisticlabsltd.com"
        login_response = login_with_email(test_email)
        
        if not login_response or not login_response.get('success'):
            _logger.error("Login failed for email: %s", test_email)
            return _json_response(
                False,
                data={},
                error="Failed to authenticate with Vision API",
                status=502
            )
        
        # Extract token from login response
        token = login_response.get('token') if login_response else None
        if not token:
            _logger.error("No token received from Vision API")
            return _json_response(
                False,
                data={},
                error="Failed to get authentication token",
                status=502
            )
        
        # Extract hostData from response
        host_data_list = login_response.get('hostData', [])
        
        # Collect all node identifiers first
        temp_host_mapping = []
        all_node_identifiers = []
        for host in host_data_list:
            network_id = host.get('networkId')
            selected_host_id = _select_host_identifier(host)

            if not selected_host_id:
                _logger.warning("Missing identifiers for host: %s", host)
                continue

            node_identifier = network_id or selected_host_id
            temp_host_mapping.append({
                'host_id': str(selected_host_id),
                'node_identifier': node_identifier,
            })
            all_node_identifiers.append(node_identifier)
        
        # Single batch database query for all node identifiers
        node_records = request.env['subscription.node'].sudo().search([
            ('node_identifier', 'in', all_node_identifiers)
        ])
        
        # Create a lookup dictionary for fast access
        node_lookup = {node.node_identifier: node for node in node_records}
        
        # Build host entries using the lookup
        host_entries = []
        for temp_host in temp_host_mapping:
            node_identifier = temp_host['node_identifier']
            node_rec = node_lookup.get(node_identifier)
            if not node_rec:
                _logger.warning("No matching node found in subscription.node for identifier: %s", node_identifier)
                continue
            
            host_entries.append({
                'host_id': temp_host['host_id'],
                'node_name': node_rec.node_name,
                'node_identifier': node_identifier,
                'node_created_date': fields.Datetime.to_string(node_rec.node_created_date or node_rec.create_date) if (node_rec.node_created_date or node_rec.create_date) else None,
            })

        if not host_entries:
            return _json_response(
                True,
                data={'total_nodes': 0, 'nodes': []},
                error="No hosts available for processing",
                status=200,
            )

        host_ids = []
        seen_host_ids = set()
        for entry in host_entries:
            host_id = entry.get('host_id')
            if not host_id:
                continue
            if host_id in seen_host_ids:
                continue
            seen_host_ids.add(host_id)
            host_ids.append(host_id)
        bulk_method_map = _fetch_bulk_method_trend_data(host_ids, token, number_of_days)

        all_hosts_data = []
        missing_entries = []
        for entry in host_entries:
            method_payload = None
            key = entry.get('host_id')
            if key and key in bulk_method_map:
                method_payload = bulk_method_map[key]

            if method_payload:
                aggregated = sum_last_values(method_payload, number_of_days)
                method_count_sum = aggregated.get('total_sum', 0)
                all_hosts_data.append({
                    'node_name': entry['node_name'],
                    'method_count_sum': method_count_sum,
                    'node_created_date': entry.get('node_created_date'),
                })
            else:
                missing_entries.append(entry)

        if missing_entries:
            all_hosts_data.extend(
                _fetch_missing_method_counts_parallel(
                    missing_entries,
                    token,
                    number_of_days,
                )
            )

        return _json_response(
            True,
            data={
                'total_nodes': len(all_hosts_data),
                'nodes': all_hosts_data
            },
            error="Method count data fetched successfully for all nodes",
            status=200
        )
        
    except Exception as e:
        _logger.exception("Error in get_all_hosts_method_count: %s", str(e))
        return _json_response(False, data={}, error=str(e), status=500)


def _fetch_missing_method_counts_parallel(entries, token, number_of_days):
    """
    Fetch per-host method counts concurrently when bulk payload misses hosts.
    """
    results = []
    if not entries:
        return results

    config_env = request.env['ir.config_parameter'].sudo()
    env_config = {
        'vision_base_url': config_env.get_param('vision_base_url'),
        'origin': config_env.get_param('backend_url'),
    }

    max_workers = min(8, len(entries))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {}
        for entry in entries:
            host_id = entry.get('host_id')
            if not host_id:
                continue
            future = executor.submit(
                get_method_count_for_host,
                host_id,
                token,
                number_of_days,
                env_config,
            )
            future_map[future] = entry
        for future in as_completed(future_map):
            entry = future_map[future]
            try:
                method_count_sum = future.result()
            except Exception as exc:  # pragma: no cover - defensive log
                _logger.exception(
                    "Error getting method count for node %s: %s",
                    entry.get('node_name'),
                    exc,
                )
                continue

            if method_count_sum is None:
                continue

            results.append({
                'node_name': entry['node_name'],
                'method_count_sum': method_count_sum,
                'node_created_date': entry.get('node_created_date'),
            })

    return results
