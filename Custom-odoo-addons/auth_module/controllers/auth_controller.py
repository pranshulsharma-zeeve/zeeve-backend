from odoo import http, fields
from odoo.http import request
from odoo.exceptions import AccessDenied
import secrets
import logging
import requests
import json
from ..utils import oauth as oauth_utils
from ..utils import jwt_auth
from ...zeeve_base.utils import base_utils as base_utils
import re

ALLOWED_MULTI_TENANT_HOSTS = {'zeeve', 'iopn', 'ew'}

_logger = logging.getLogger(__name__)


class AuthController(http.Controller):
    """REST controllers handling sign up, login and OTP verification."""

    @http.route('/api/v1/signup', type='http', auth="public", csrf=False, methods=['OPTIONS', 'POST'])
    def api_signup(self, **kwargs):
        """Register a new user and send an OTP for e-mail verification."""

        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            data = request.httprequest.get_json(force=True, silent=True) or {}
            required_fields = ['first_name', 'last_name', 'email', 'password']
            is_valid, error_msg = base_utils._validate_payload(data, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, {'error': error_msg}, status=400)
            first_name = (data.get('first_name') or '').strip()
            middle_name = (data.get('middle_name') or '').strip()
            last_name = (data.get('last_name') or '').strip()
            email = (data.get('email') or '').strip()
            password = data.get('password')
            utm_info = (
                request.httprequest.args.get('utm_info')
                or data.get('utm_info')
                or kwargs.get('utm_info')
            )
            multi_tenant_host = data.get('multi_tenant_host')


            

            env = request.env['res.users'].sudo()
            if env.search([('login', '=', email)], limit=1):
                return oauth_utils._json_response(False, error="User already exists", status=409)

            name_parts = [part for part in [first_name, middle_name, last_name] if part]
            full_name = ' '.join(name_parts) or email
            # --- Make the user a PORTAL user ---
            portal_group = request.env.ref('base.group_portal')
            user = env.with_context(no_reset_password=True).create({
                'name': full_name,
                'login': email,
                'email': email,
                'password': password,
            })
            user.sudo().write({
                'groups_id': [(6, 0, [portal_group.id])]
            })
            partner = user.partner_id
            partner.write({
                'email_verified': False,
                'oauth_provider': 'email',
                'first_name': first_name or full_name,
                'middle_name': middle_name or False,
                'last_name': last_name or False,
            })
            if multi_tenant_host == 'iopn':
                partner.write({'multi_tenant_host': multi_tenant_host})
            partner.store_utm_info(utm_info)
            partner.generate_verification_token()
            partner.send_verification_email()

            # --- Create Vizion user via external API helper ---
            oauth_utils.create_vizion_user(user)
            return oauth_utils._json_response(True, {"user_id": user.id}, status=201)
        except Exception as exc:  # pragma: no cover - unexpected messages
            return oauth_utils._json_response(False, error=str(exc), status=500)

    @http.route('/api/v1/login', type='http', auth='public', methods=['OPTIONS', 'POST'], csrf=False)
    def api_login(self, **kwargs):
        """Authenticate a user with email/password and set a refresh token cookie."""

        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            data = request.httprequest.get_json(force=True, silent=True) or {}
            multi_tenant_host = (
                request.httprequest.args.get('multi_tenant_host')
                or data.get('multi_tenant_host')
                or kwargs.get('multi_tenant_host')
                or 'zeeve'
            )
            multi_tenant_host = (multi_tenant_host or '').strip().lower() or 'zeeve'
            if multi_tenant_host not in ALLOWED_MULTI_TENANT_HOSTS:
                _logger.info('Rejected login due to unknown multi-tenant host: %s', multi_tenant_host)
                return oauth_utils.make_json_response(
                    {'success': False, 'message': 'Invalid credentials'}, status=401
                )
            required_fields = ['email', 'password']
            is_valid, error_msg = base_utils._validate_payload(data, required_fields)
            if not is_valid:
                return oauth_utils.make_json_response(
                    {'success': False, 'message': error_msg}, status=400
                )

            email = (data.get('email') or '').strip()
            password = data.get('password')

            if not email or not password:
                return oauth_utils.make_json_response(
                    {'success': False, 'message': 'Missing credentials'}, status=400
                )

            recaptcha_token = data.get('recaptcha_token') or data.get('recaptcha')
            recaptcha_ok = oauth_utils._verify_recaptcha_token(recaptcha_token)
            if recaptcha_ok is None:
                return oauth_utils.make_json_response(
                    {'success': False, 'message': 'Login temporarily unavailable. Contact support.'},
                    status=500,
                )
            if recaptcha_ok is False:
                return oauth_utils.make_json_response(
                    {'success': False, 'message': 'reCAPTCHA verification failed'},
                    status=400,
                )
            user = request.env['res.users'].sudo().search([('login', '=', email)], limit=1)
            if not user:
                return oauth_utils.make_json_response(
                    {'success': False, 'message': 'Invalid credentials'}, status=401
                )

            partner = user.partner_id
            partner_host = (partner.multi_tenant_host or 'zeeve').lower()
            if multi_tenant_host != partner_host:
                _logger.warning(
                    'Multi-tenant host mismatch for user %s: requested %s, partner %s',
                    user.id,
                    multi_tenant_host,
                    partner_host,
                )
                return oauth_utils.make_json_response(
                    {'success': False, 'message': 'Invalid credentials'}, status=401
                )

            credentials = {'login': user.login, 'password': password, 'type': 'password'}
            # Native or legacy bcrypt (auto-upgrades on success)
            try:
                user.check_password_with_legacy(password)
            except AccessDenied:
                _logger.exception('Invalid Cred %s f', user.id)
                return oauth_utils.make_json_response({'success': False, 'message': 'Invalid credentials'}, status=401)
            if not partner.email_verified:
                partner.generate_verification_token()
                partner.send_verification_email()
                response = oauth_utils.make_json_response(
                    {
                        'success': False,
                        'message': 'Email not verified. OTP sent to email',
                        'requires_verification': True,
                    },
                    status=403,
                )
                jwt_auth.clear_refresh_cookie(response)
                return response

            request.session.logout(keep_db=True)
            refresh_token, _ = jwt_auth.issue_refresh_token(user)
            response = oauth_utils.make_json_response(
                {'success': True, 'message': 'Login successful'}
            )
            jwt_auth.set_refresh_cookie(response, refresh_token)
            return response

        except Exception as exc:  # pragma: no cover - unexpected messages
            return oauth_utils.make_json_response(
                {'success': False, 'message': str(exc)}, status=500
            )


    @http.route(
        '/api/v1/oauth/<string:provider>/authorize',
        type='http',
        auth='public',
        methods=['OPTIONS', 'GET'],
        csrf=False,
    )
    def oauth_authorize(self, provider, **kwargs):
        """Return the provider authorization URL for the frontend."""

        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response(methods=['GET'])
            data = request.httprequest.get_json(force=True, silent=True) or {}
            utm_info = (
                request.httprequest.args.get('utm_info')
                or data.get('utm_info')
                or kwargs.get('utm_info')
            )
            service_url = (
                request.httprequest.args.get('serviceURL')
                or data.get('serviceURL')
                or kwargs.get('serviceURL')
            )
            state = secrets.token_urlsafe(16)
            request.session['oauth_state'] = state
            # Include service_url in the state parameter
            state_data = {
                'state': state,
                'utm_info': utm_info,
                'service_url': service_url,  # Add service_url here
            }
            state = json.dumps(state_data)
            if service_url:
                state_data['service_url'] = service_url

            url = oauth_utils.build_authorize_url(provider, state)
            return oauth_utils._json_response(True, {'authorization_url': url})
        except Exception as exc:  # pragma: no cover - unexpected messages
            _logger.error("Error in oauth_authorize: %s", str(exc))
            return oauth_utils._json_response(False, error=str(exc), status=500)

    @http.route('/api/v1/oauth/<string:provider>/callback', type='http', auth='public', methods=['GET'], csrf=False)
    def oauth_callback(self, provider, **kwargs):
        """Handle OAuth2 callback, extracting UTM info from state."""

        try:
            code = request.params.get('code')
            state = request.params.get('state')
            if not code:
                return oauth_utils._json_response(False, error="Missing code", status=400)
            if not state:
                return oauth_utils._json_response(False, error="Missing state", status=400)

            try:
                state_data = json.loads(state)
                utm_info = state_data.get('utm_info')
                service_url = state_data.get('service_url')
            except json.JSONDecodeError:
                return oauth_utils._json_response(False, error="Invalid state format", status=400)

            access_token, refresh_token, expiry, id_token = oauth_utils.exchange_code(provider, code)
            info = oauth_utils.fetch_user_info(provider, access_token, id_token)
            # Log the raw user info payload returned by the OAuth provider for debugging/inspection
            try:
                _logger.info("OAuth user info (%s): %s", provider, json.dumps(info))
            except Exception:
                # Fallback in case of non-serializable values
                _logger.info("OAuth user info (%s): %s", provider, info)
            email = info.get('email')
            if not email:
                return oauth_utils._json_response(False, error='Email not provided by provider', status=422)
            uid = info.get('id')
            name = info.get('name') or email

            env_users = request.env['res.users'].sudo()
            user = env_users.search([('login', '=', email)], limit=1)
            portal_group = request.env.ref('base.group_portal')

            is_new_user = bool(not user)
            if not user:
                user = env_users.with_context(no_reset_password=True).create({'name': name, 'login': email, 'email': email})

            if is_new_user:
                user.sudo().write({'groups_id': [(6, 0, [portal_group.id])]})
            partner = user.partner_id
            # Build base partner values
            partner_vals = {
                'oauth_provider': provider,
                'oauth_uid': uid,
                'oauth_access_token': access_token,
                'oauth_refresh_token': refresh_token,
                'oauth_token_expires': expiry,
                'email_verified': True,
            }
            # If new user coming via Google, attempt to split name into first/last
            if is_new_user and (provider or '').lower() == 'google':
                raw_name = (info.get('name') or '').strip()
                first_name = False
                last_name = False
                if raw_name:
                    parts = raw_name.split()
                    if parts:
                        first_name = parts[0]
                        if len(parts) > 1:
                            last_name = parts[-1]
                    partner_vals.update({
                        'first_name': first_name or False,
                        'last_name': last_name or False,
                    })
                    _logger.info("OAuth (google) name parsed -> first: %s last: %s", first_name, last_name)
            # If new user coming via GitHub, attempt to split username into first/last
            elif is_new_user and (provider or '').lower() == 'github':
                raw_name = (info.get('name') or '').strip()

                cleaned = re.sub(r'[\-_]+', ' ', raw_name).strip()
                parts = cleaned.split()

                first_name = parts[0] if parts else False
                last_name = parts[-1] if len(parts) > 1 else False

                partner_vals.update({
                    'first_name': first_name or False,
                    'last_name': last_name or False,
                })

                _logger.info("OAuth (github) name parsed -> raw: %s | first: %s | last: %s",raw_name, first_name, last_name)

            partner.write(partner_vals) 
            if utm_info:
                partner.store_utm_info(utm_info)
            if is_new_user:
                _logger.info("OAuth signup: new user %s (%s) created via %s – sending welcome email", user.id, email, provider)
                partner.send_signup_emails(utm_info)
            elif utm_info:
                _logger.info("OAuth signup: existing user %s (%s) updated UTM info", user.id, email)

            request.session.logout(keep_db=True)
            refresh_token, _ = jwt_auth.issue_refresh_token(user)
            _logger.info("OAuth signup: existing user %s (%s) service url-------", request.params.get('serviceURL'), state_data.get('service_url'))
            service_url = state_data.get('service_url') or request.session.pop('oauth_service_url', False) or request.params.get('serviceURL')
            try:
                _logger.info("%s service url-------", service_url)

                redirect_target = oauth_utils.resolve_frontend_redirect(service_url)
            except ValueError as err:
                _logger.error("OAuth callback: %s", err)
                return oauth_utils._json_response(False, error=str(err), status=400)
            _logger.info("OAuth callback: redirecting user %s to %s", user.id, redirect_target)
            response = oauth_utils.make_frontend_redirect_response(redirect_target)
            jwt_auth.set_refresh_cookie(response, refresh_token)
            return response
        except Exception as exc:  # pragma: no cover - unexpected messages
            _logger.error("Error in oauth_callback: %s", str(exc))
            return oauth_utils._json_response(False, error=str(exc), status=500)

    @http.route('/api/v1/verify-email', type='http', auth='public', methods=['OPTIONS', 'POST'], csrf=False)
    def api_verify_email(self, **kwargs):
        """Verify signup via OTP and trigger welcome emails."""

        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            data = request.httprequest.get_json(force=True, silent=True) or {}
            required_fields = ['otp', 'email']
            is_valid, error_msg = base_utils._validate_payload(data, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, {'error': error_msg}, status=400)
            token = (data.get('otp') or '').strip()
            email = (data.get('email') or '').strip()
            if not token or not email:
                return oauth_utils._json_response(False, error="Token and email are required", status=400)
            env = request.env['res.partner'].sudo()
            partner = env.search([('email', '=', email), ('verification_token', '=', token)], limit=1)
            if not partner:
                return oauth_utils._json_response(False, error="Invalid OTP", status=404)
            if partner.verification_token_expiry and partner.verification_token_expiry < fields.Datetime.now():
                return oauth_utils._json_response(False, error="Token expired", status=422)
            partner.write({
                'email_verified': True,
                'verification_token': False,
                'verification_token_expiry': False,
            })
            utm_info = request.httprequest.args.get('utm_info')
            partner.send_signup_emails(utm_info)
            request.env['zeeve.notification'].sudo().notify_partner(
                partner,
                notification_type='welcome',
                title='Welcome to Zeeve',
                message='Your account is verified and ready to use.',
                category='success',
                payload={
                    'partner_id': partner.id,
                    'email': partner.email or '',
                },
                action_url='/dashboard',
                reference_model='res.partner',
                reference_id=partner.id,
                dedupe_key='welcome:%s' % partner.id,
            )
            user = partner.user_ids[:1]
            if not user:
                return oauth_utils._json_response(False, error="No user associated with account", status=422)
            request.session.logout(keep_db=True)
            refresh_token, _ = jwt_auth.issue_refresh_token(user)
            access_token = jwt_auth.generate_access_token(user)
            response = oauth_utils._json_response(
                True,
                {
                    "message": "Email verified successfully.",
                    "user_id": user.id,
                    "access_token": access_token,
                },
            )
            jwt_auth.set_refresh_cookie(response, refresh_token)
            return response
        except Exception as exc:  # pragma: no cover - unexpected messages
            return oauth_utils._json_response(False, error=str(exc), status=500)

    @http.route('/api/v1/resend-verification', type='http', auth='public', methods=['OPTIONS', 'POST'], csrf=False)
    def api_resend_verification(self, **kwargs):
        """Regenerate and send the verification OTP to the user."""

        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            data = request.httprequest.get_json(force=True, silent=True) or {}
            required_fields = ['email']
            is_valid, error_msg = base_utils._validate_payload(data, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, {'error': error_msg}, status=400)
            email = (data.get('email') or '').strip()
            if not email:
                return oauth_utils._json_response(False, error="Email is required", status=400)

            user = request.env['res.users'].sudo().search([('login', '=', email)], limit=1)
            if not user:
                return oauth_utils._json_response(
                    True,
                    {'message': 'If the account exists, a verification email has been sent.'},
                )

            partner = user.partner_id
            partner.generate_verification_token()
            partner.send_verification_email()
            return oauth_utils._json_response(True, {'message': 'Verification email sent'})
        except Exception as exc:  # pragma: no cover - unexpected messages
            return oauth_utils._json_response(False, error=str(exc), status=500)

    @http.route('/api/v1/forgot-password', type='http', auth='public', methods=['OPTIONS', 'POST'], csrf=False)
    def api_forgot_password(self, **kwargs):
        """Create a password reset OTP and notify the user."""

        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            data = request.httprequest.get_json(force=True, silent=True) or {}
            required_fields = ['email']
            is_valid, error_msg = base_utils._validate_payload(data, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, {'error': error_msg}, status=400)
            email = (data.get('email') or '').strip()
            if not email:
                return oauth_utils._json_response(False, error="Email is required", status=400)

            user = request.env['res.users'].sudo().search([('login', '=', email)], limit=1)
            if not user:
                _logger.info('user with provided email does not exist, email=%s', email)
                return oauth_utils._json_response(
                    False,
                    error="User with provided email does not exist",
                    status=404
                )
            partner = user.partner_id
            partner.generate_password_reset_token()
            partner.send_password_reset_email()
            return oauth_utils._json_response(True, {'message': 'Password reset OTP sent'})
        except Exception as exc:  # pragma: no cover - unexpected messages
            return oauth_utils._json_response(False, error=str(exc), status=500)

    @http.route('/api/v1/reset-password', type='http', auth='public', methods=['OPTIONS', 'POST'], csrf=False)
    def api_reset_password(self, **kwargs):
        """Validate the OTP and update the user's password."""

        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            data = request.httprequest.get_json(force=True, silent=True) or {}

            required_fields = ['otp', 'password']
            is_valid, error_msg = base_utils._validate_payload(data, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, {'error': error_msg}, status=400)
            otp = (data.get('otp') or '').strip()
            password = data.get('password')
            if not otp or not password:
                return oauth_utils._json_response(False, error="OTP and password are required", status=400)

            env = request.env['res.partner'].sudo()
            partner = env.search([('password_reset_token', '=', otp)], limit=1)
            if not partner:
                return oauth_utils._json_response(False, error="Invalid or expired token", status=422)
            if partner.password_reset_token_expiry and partner.password_reset_token_expiry < fields.Datetime.now():
                partner.clear_password_reset_token()
                return oauth_utils._json_response(False, error="Invalid or expired token", status=422)

            user = partner.user_ids[:1]
            if not user:
                return oauth_utils._json_response(False, error="No user associated with token", status=422)

            user.sudo().write({'password': password})
            partner.clear_password_reset_token()
            return oauth_utils._json_response(
                True,
                {'message': 'Password updated successfully.', 'user_id': user.id},
            )
        except Exception as exc:  # pragma: no cover - unexpected messages
            return oauth_utils._json_response(False, error=str(exc), status=500)
        
    @http.route('/api/v1/user-info', type='http', auth='public', methods=['OPTIONS', 'GET'], csrf=False)
    def api_user_info(self, **kwargs):
        """Retrieve the authenticated user's profile information and access details."""

        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            partner = user.partner_id
            # Build response payload
            image_src = ""
            if partner.image_1920:
                image_src = base_utils.public_image_source(partner, "image_1920", size="128x128")

            assigned_nodes = []
            assigned_rollups = []

            # Super Admin or node_access_type 'all' gets "All" access
            if user.company_role == 'super_admin' or user.node_access_type == 'all':
                assigned_nodes = ["All"]
                assigned_rollups = ["All"]
            else:
                try:
                    assigned_nodes = json.loads(user.specific_nodes) if user.specific_nodes else []
                    assigned_rollups = json.loads(user.specific_rollups) if user.specific_rollups else []
                except Exception:
                    pass

            data = {
                "first_name": partner.first_name or "",
                "last_name": partner.last_name or "",
                "company_name": user.company_id.name or "",
                "usercred": user.login or partner.email or "",
                "provider": partner.oauth_provider,
                "image_url": image_src,
                "access_details": {
                    "role": user.company_role or "super_admin",
                    "node_access_type": user.node_access_type or "all",
                    "assigned_nodes": assigned_nodes,
                    "assigned_rollups": assigned_rollups,
                }
            }

            return oauth_utils._json_response(True, data=data)

        except Exception as exc:  # pragma: no cover - unexpected messages
            return oauth_utils._json_response(False, error=str(exc), status=500)

    @http.route('/api/v1/update_user_details', type='http', auth='public', methods=['OPTIONS', 'POST'], csrf=False)
    def api_update_user(self, **kwargs):
        """Update the authenticated user's profile details."""
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response(methods=['POST'])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            payload = request.httprequest.get_json(force=True, silent=True) or {}
            allowed_fields = ('first_name', 'last_name', 'email', 'image')
            if not any(field in payload for field in allowed_fields):
                return oauth_utils._json_response(
                    False,
                    error='At least one of first_name, last_name, email, or image is required',
                    status=400,
                )

            partner = user.partner_id.sudo()
            user = user.sudo()
            partner_vals = {}
            user_vals = {}
            email_changed = False

            if 'first_name' in payload:
                partner_vals['first_name'] = (payload.get('first_name') or '').strip() or False

            if 'last_name' in payload:
                partner_vals['last_name'] = (payload.get('last_name') or '').strip() or False

            if 'image' in payload:
                image_data = payload.get('image')
                partner_vals['image_1920'] = image_data or False
                user_vals['image_1920'] = image_data or False

            if 'email' in payload:
                new_email = (payload.get('email') or '').strip()
                if not new_email:
                    return oauth_utils._json_response(False, error='Email is required', status=400)

                existing_user = request.env['res.users'].sudo().search(
                    ['&', ('id', '!=', user.id), '|', ('login', '=', new_email), ('email', '=', new_email)],
                    limit=1,
                )
                if existing_user:
                    return oauth_utils._json_response(False, error='User already exists', status=409)

                current_email = (user.login or partner.email or '').strip()
                email_changed = new_email != current_email
                user_vals.update({
                    'login': new_email,
                    'email': new_email,
                })
                partner_vals['email'] = new_email

            should_sync_name = (
                'first_name' in payload
                or 'last_name' in payload
                or email_changed
            )
            if should_sync_name:
                first_name = partner_vals.get('first_name', partner.first_name)
                last_name = partner_vals.get('last_name', partner.last_name)
                display_email = user_vals.get('login') or user.login or partner.email
                name_parts = [part for part in [first_name, partner.middle_name, last_name] if part]
                full_name = ' '.join(name_parts) or display_email
                partner_vals['name'] = full_name
                user_vals['name'] = full_name

            if partner_vals:
                partner.write(partner_vals)
            if user_vals:
                user.write(user_vals)

            if email_changed:
                partner.write({'email_verified': False})
                partner.generate_verification_token()
                partner.send_verification_email()
                jwt_auth.invalidate_refresh_token(user)
                request.session.logout(keep_db=True)
                response = oauth_utils._json_response(
                    True,
                    data={
                        'message': 'Profile updated successfully. Verification email sent to the new email address.',
                        'requires_verification': True,
                    },
                )
                jwt_auth.clear_refresh_cookie(response)
                return response

            return oauth_utils._json_response(
                True,
                data={'message': 'Profile updated successfully.'},
            )
        except Exception as exc:
            return oauth_utils._json_response(False, error=str(exc), status=500)

    @http.route('/api/v1/access_token', type='http', auth='public', methods=['OPTIONS', 'GET'], csrf=False)
    def api_access_token(self, **kwargs):
        """Return a short-lived JWT access token using the refresh token cookie."""

        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response(methods=['GET'])

            refresh_token = jwt_auth.get_refresh_cookie()
            user, error = jwt_auth.authenticate_refresh_token(refresh_token)
            if not user:
                error_map = {
                    'missing': 'Missing refresh token',
                    'expired': 'Refresh token expired',
                    'invalid': 'Invalid refresh token',
                }
                response = oauth_utils.make_json_response(
                    {
                        'success': False,
                        'message': error_map.get(error, 'Invalid refresh token'),
                    },
                    status=401,
                )
                jwt_auth.clear_refresh_cookie(response)
                return response

            access_token = jwt_auth.generate_access_token(user)
            return oauth_utils.make_json_response(
                {'success': True, 'access_token': access_token}
            )

        except Exception as exc:  # pragma: no cover - unexpected messages
            return oauth_utils.make_json_response(
                {'success': False, 'message': str(exc)}, status=500
            )

    @http.route('/api/v1/logout', type='http', auth='public', methods=['OPTIONS', 'POST'], csrf=False)
    def api_logout(self, **kwargs):
        """Invalidate the refresh token and clear the cookie."""

        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            refresh_token = jwt_auth.get_refresh_cookie()
            user, _ = jwt_auth.authenticate_refresh_token(refresh_token)
            if user:
                jwt_auth.invalidate_refresh_token(user)

            response = oauth_utils.make_json_response(
                {'success': True, 'message': 'Logged out successfully'}
            )
            jwt_auth.clear_refresh_cookie(response)
            request.session.logout(keep_db=True)
            return response

        except Exception as exc:  # pragma: no cover - unexpected messages
            return oauth_utils.make_json_response(
                {'success': False, 'message': str(exc)}, status=500
            )

    @http.route('/api/v1/change-password', type='http', auth='public', methods=['OPTIONS', 'POST'], csrf=False)
    def api_change_password(self, **kwargs):
        """Allow users to change their password after validating the current one."""

        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()
            user, token_err = oauth_utils._user_from_token()
            if not user:
                messages = {
                    "missing": "Missing access token",
                    "expired": "Access token expired",
                    "invalid": "Invalid access token",
                }
                return oauth_utils._json_response(
                    False,
                    error=messages.get(token_err, "Invalid access token"),
                    status=401,
                )

            request.update_env(user=user.id)

            data = request.httprequest.get_json(force=True, silent=True) or {}
            required_fields = ['email', 'current_password', 'new_password']
            is_valid, error_msg = base_utils._validate_payload(data, required_fields)
            if not is_valid:
                return oauth_utils.make_json_response(
                    {'success': False, 'message': error_msg}, status=400
                )

            email = (data.get('email') or '').strip()
            current_password = data.get('current_password') or ''
            new_password = data.get('new_password') or ''

            if len(new_password) < 8:
                return oauth_utils.make_json_response(
                    {'success': False, 'message': 'New password must be at least 8 characters long'}, status=400
                )
            if not user:
                return oauth_utils.make_json_response(
                    {'success': False, 'message': 'Invalid credentials'}, status=401
                )

            try:
                user.check_password_with_legacy(current_password)
            except AccessDenied:
                _logger.info('Password change failed for user %s: invalid current password', user.id)
                return oauth_utils.make_json_response(
                    {'success': False, 'message': 'Invalid credentials'}, status=401
                )

            user.sudo().write({
                'password': new_password,
                'password_changed_at': fields.Datetime.now(),
            })
            _logger.info('Password updated for user %s', user.id)
            return oauth_utils.make_json_response(
                {'success': True, 'message': 'Password updated successfully'}
            )

        except Exception as exc:  # pragma: no cover - unexpected messages
            return oauth_utils.make_json_response(
                {'success': False, 'message': str(exc)}, status=500
            )

    @http.route('/api/v1/invite-user', type='http', auth='public', methods=['OPTIONS', 'POST'], csrf=False)
    def api_invite_user(self, **kwargs):
        """Invite a new user (Admin or Operator) to the company."""
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id','=',131)])
            if user.company_role not in ['super_admin', 'admin']:
                return oauth_utils._json_response(False, error="Permission denied", status=403)

            data = request.httprequest.get_json(force=True, silent=True) or {}
            required_fields = ['email', 'role']
            is_valid, error_msg = base_utils._validate_payload(data, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, {'error': error_msg}, status=400)

            email = (data.get('email') or '').strip()
            role = data.get('role')
            
            # Check if user already exists and belongs to a company
            existing_user = request.env['res.users'].sudo().search([('login', '=', email)], limit=1)
            if existing_user and existing_user.company_id and existing_user.company_id.id != request.env.ref('base.main_company').id:
                return oauth_utils._json_response(False, error="User is already associated with an organization", status=400)
            
            # Check for existing pending invitation
            existing_inv = request.env['user.invitation'].sudo().search([
                ('email', '=', email),
                ('company_id', '=', user.company_id.id),
                ('status', '=', 'pending')
            ], limit=1)
            if existing_inv:
                return oauth_utils._json_response(False, error="An invitation has already been sent to this email", status=400)

            # Enforce company name update before inviting
            default_company_name = request.env['ir.config_parameter'].sudo().get_param('zeeve.default_company_name', 'Zeeve Dev')
            if user.company_id.name == default_company_name:
                return oauth_utils._json_response(False, error="Default company name detected. Please update your company name before inviting users.", status=403)

            node_access_type = data.get('node_access_type', 'all')
            specific_nodes = json.dumps(data.get('specific_nodes', [])) if data.get('specific_nodes') else None
            specific_rollups = json.dumps(data.get('specific_rollups', [])) if data.get('specific_rollups') else None

            if role not in ['admin', 'operator']:
                return oauth_utils._json_response(False, error="Invalid role", status=400)


            invitation = request.env['user.invitation'].sudo().invite_user(
                email=email,
                role=role,
                company_id=user.company_id.id,
                invited_by_id=user.id,
                node_access_type=node_access_type,
                specific_nodes=specific_nodes,
                specific_rollups=specific_rollups
            )

            return oauth_utils._json_response(True, data={
                'message': 'Invitation sent successfully',
                'token': invitation.token # In a real system, this would be in the email link
            })
        except Exception as exc:
            return oauth_utils._json_response(False, error=str(exc), status=500)

    @http.route('/api/v1/setup-password', type='http', auth='public', methods=['OPTIONS', 'POST'], csrf=False)
    def api_setup_password(self, **kwargs):
        """Setup password for an invited user using the invitation token."""
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            data = request.httprequest.get_json(force=True, silent=True) or {}
            required_fields = ['token', 'password']
            is_valid, error_msg = base_utils._validate_payload(data, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, {'error': error_msg}, status=400)

            token = data.get('token')
            password = data.get('password')

            invitation = request.env['user.invitation'].sudo().search([
                ('token', '=', token),
                ('status', '=', 'pending')
            ], limit=1)

            if not invitation:
                return oauth_utils._json_response(False, error="Invalid or expired invitation token", status=404)

            if invitation.expiry_date < fields.Datetime.now():
                invitation.sudo().write({'status': 'expired'})
                return oauth_utils._json_response(False, error="Invitation token expired", status=410)

            # Create the user
            env_users = request.env['res.users'].sudo()
            portal_group = request.env.ref('base.group_portal')
            
            user = env_users.with_context(
                no_reset_password=True,
                allowed_company_ids=[invitation.company_id.id],
                default_company_id=invitation.company_id.id,
            ).create({
                'name': invitation.email,
                'login': invitation.email,
                'email': invitation.email,
                'password': password,
                'company_ids': [(6, 0, [invitation.company_id.id])],
                'company_id': invitation.company_id.id,
                'company_role': invitation.role,
                'invited_by': invitation.invited_by.id,
                'groups_id': [(6, 0, [portal_group.id])],
                'node_access_type': invitation.node_access_type,
                'specific_nodes': invitation.specific_nodes,
                'specific_rollups': invitation.specific_rollups
            })

            # invitation.sudo().write({'status': 'accepted'})
            
            # Auto-login or return success
            return oauth_utils._json_response(True, data={'message': 'Password set successfully', 'user_id': user.id})
        except Exception as exc:
            return oauth_utils._json_response(False, error=str(exc), status=500)

    @http.route('/api/v1/register-company', type='http', auth='public', methods=['OPTIONS', 'POST'], csrf=False)
    def api_register_company(self, **kwargs):
        """Register a new company and make the current user the Super Admin."""
        try:
            if request.httprequest.method == 'OPTIONS':
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id','=',130)])
            data = request.httprequest.get_json(force=True, silent=True) or {}
            required_fields = ['company_name']
            is_valid, error_msg = base_utils._validate_payload(data, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, {'error': error_msg}, status=400)

            company_name = data.get('company_name').strip()
            existing_company = request.env['res.company'].sudo().search(
                [('name', '=', company_name)],
                limit=1
            )

            if existing_company:
                return oauth_utils._json_response(
                    False,
                    {'error': 'Company already exists'},
                    status=400
                )
            # Create Company
            company = request.env['res.company'].sudo().create({'name': company_name})

            # Update Current User
            user.sudo().write({
                'company_ids': [(4, company.id)],
                'company_id': company.id,
                'company_role': 'super_admin',
                'is_company_owner': True,
            })

            company.sudo().write({'owner_id': user.id})

            return oauth_utils._json_response(True, data={'message': 'Company registered successfully', 'company_id': company.id})
        except Exception as exc:
            return oauth_utils._json_response(False, error=str(exc), status=500)
