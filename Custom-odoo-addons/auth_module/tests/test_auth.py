import json
import base64
from http.cookies import SimpleCookie
from unittest.mock import patch
from odoo.tests.common import HttpCase, TransactionCase


class TestAuthEndpoints(HttpCase):
    """End-to-end tests for authentication REST APIs."""

    def _get_refresh_cookie_header(self, response):
        headers = []
        if hasattr(response.headers, 'getlist'):
            headers = response.headers.getlist('Set-Cookie')
        else:
            header = response.headers.get('Set-Cookie')
            if header:
                headers = [header]
        self.assertTrue(headers)
        cookie = SimpleCookie()
        for header in headers:
            cookie.load(header)
        self.assertIn('refresh_token', cookie)
        return f"refresh_token={cookie['refresh_token'].value}"

    def test_signup_verify_and_login(self):
        headers = {'Content-Type': 'application/json'}
        payload = {
            'first_name': 'Test',
            'last_name': 'User',
            'email': 'test@example.com',
            'password': 'secret',
        }
        resp = self.url_open('/api/v1/signup', data=json.dumps(payload), headers=headers)
        data = json.loads(resp.data.decode())
        self.assertTrue(data['success'])
        self.assertEqual(data['error'], '')

        user = self.env['res.users'].sudo().browse(data['data']['user_id'])
        partner = user.partner_id
        otp = partner.verification_token
        verify_payload = {'token': otp, 'email': 'test@example.com'}
        verify_resp = self.url_open('/api/v1/verify-email', data=json.dumps(verify_payload), headers=headers)
        verify_data = json.loads(verify_resp.data.decode())
        self.assertTrue(verify_data['success'])
        self.assertEqual(verify_data['error'], '')

        login_payload = {'email': 'test@example.com', 'password': 'secret'}
        login_resp = self.url_open('/api/v1/login', data=json.dumps(login_payload), headers=headers)
        login_data = json.loads(login_resp.data.decode())
        self.assertTrue(login_data['success'])
        self.assertEqual(login_data['message'], 'Login successful')
        cookie_header = self._get_refresh_cookie_header(login_resp)

        token_resp = self.url_open('/api/v1/access_token', headers={'Cookie': cookie_header})
        token_data = json.loads(token_resp.data.decode())
        self.assertTrue(token_data['success'])
        self.assertIn('access_token', token_data)

    def test_verify_email_sends_emails_with_utm(self):
        headers = {'Content-Type': 'application/json'}
        # Create config with admin email and social links
        self.env['zeeve.config'].sudo().create({
            'admin_emails': 'admin@example.com',
            'twitter_url': 'https://twitter.com/zeeve',
            'linkedin_url': 'https://linkedin.com/company/zeeve',
            'telegram_url': 'https://t.me/zeeve',
        })
        payload = {
            'first_name': 'Test',
            'last_name': 'User',
            'email': 'test@example.com',
            'password': 'secret',
        }
        resp = self.url_open('/api/v1/signup', data=json.dumps(payload), headers=headers)
        data = json.loads(resp.data.decode())
        self.assertTrue(data['success'])
        self.assertEqual(data['error'], '')
        user = self.env['res.users'].sudo().browse(data['data']['user_id'])
        partner = user.partner_id
        otp = partner.verification_token
        mail_model = self.env['mail.mail'].sudo()
        count_after_signup = mail_model.search_count([])
        utm = {'utm_source': 'some-source', 'utm_medium': 'shardeum', 'SiteTarget': 'app.zeeve.io'}
        utm_encoded = base64.urlsafe_b64encode(json.dumps(utm).encode()).decode().rstrip('=')
        verify_payload = {'token': otp, 'email': 'test@example.com'}
        verify_resp = self.url_open(f'/api/v1/verify-email?utm_info={utm_encoded}', data=json.dumps(verify_payload), headers=headers)
        verify_data = json.loads(verify_resp.data.decode())
        self.assertTrue(verify_data['success'])
        count_after_verify = mail_model.search_count([])
        # Expect three new mails: greeting, shardeum greeting, admin notification
        self.assertEqual(count_after_verify - count_after_signup, 3)
        admin_mail = mail_model.search([('email_to', '=', 'admin@example.com')])
        self.assertTrue(admin_mail)

    def test_forgot_and_reset_password(self):
        headers = {'Content-Type': 'application/json'}
        payload = {
            'first_name': 'Reset',
            'last_name': 'User',
            'email': 'reset@example.com',
            'password': 'initial',
        }
        resp = self.url_open('/api/v1/signup', data=json.dumps(payload), headers=headers)
        data = json.loads(resp.data.decode())
        self.assertTrue(data['success'])
        user = self.env['res.users'].sudo().browse(data['data']['user_id'])
        partner = user.partner_id
        partner.write({'email_verified': True})

        forgot_payload = {'email': payload['email']}
        forgot_resp = self.url_open('/api/v1/forgot-password', data=json.dumps(forgot_payload), headers=headers)
        forgot_data = json.loads(forgot_resp.data.decode())
        self.assertTrue(forgot_data['success'])
        self.assertEqual(forgot_data['error'], '')
        self.assertTrue(partner.password_reset_token)

        reset_payload = {'otp': partner.password_reset_token, 'password': 'changed'}
        reset_resp = self.url_open('/api/v1/reset-password', data=json.dumps(reset_payload), headers=headers)
        reset_data = json.loads(reset_resp.data.decode())
        self.assertTrue(reset_data['success'])
        self.assertEqual(reset_data['error'], '')
        self.assertEqual(reset_data['data']['user_id'], user.id)

        login_payload = {'email': payload['email'], 'password': 'changed'}
        login_resp = self.url_open('/api/v1/login', data=json.dumps(login_payload), headers=headers)
        login_data = json.loads(login_resp.data.decode())
        self.assertTrue(login_data['success'])
        self.assertEqual(login_data['message'], 'Login successful')
        cookie_header = self._get_refresh_cookie_header(login_resp)

        token_resp = self.url_open('/api/v1/access_token', headers={'Cookie': cookie_header})
        token_data = json.loads(token_resp.data.decode())
        self.assertTrue(token_data['success'])
        self.assertIn('access_token', token_data)

    def test_update_user_details_supports_partial_profile_updates(self):
        headers = {'Content-Type': 'application/json'}
        payload = {
            'first_name': 'Before',
            'last_name': 'Name',
            'email': 'before.update@example.com',
            'password': 'secret',
        }
        signup_resp = self.url_open('/api/v1/signup', data=json.dumps(payload), headers=headers)
        signup_data = json.loads(signup_resp.data.decode())
        self.assertTrue(signup_data['success'])

        user = self.env['res.users'].sudo().browse(signup_data['data']['user_id'])
        partner = user.partner_id
        verify_resp = self.url_open(
            '/api/v1/verify-email',
            data=json.dumps({'otp': partner.verification_token, 'email': payload['email']}),
            headers=headers,
        )
        verify_data = json.loads(verify_resp.data.decode())
        self.assertTrue(verify_data['success'])
        access_token = verify_data['data']['access_token']
        cookie_header = self._get_refresh_cookie_header(verify_resp)
        auth_headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {access_token}',
            'Cookie': cookie_header,
        }

        image_payload = base64.b64encode(b'updated-image').decode()
        update_resp = self.url_open(
            '/api/v1/update_user_details',
            data=json.dumps({
                'first_name': 'After',
                'last_name': 'User',
                'email': 'after.update@example.com',
                'image': image_payload,
            }),
            headers=auth_headers,
        )
        update_data = json.loads(update_resp.data.decode())
        self.assertTrue(update_data['success'])
        self.assertTrue(update_data['data']['requires_verification'])

        user.invalidate_recordset()
        partner.invalidate_recordset()
        user = self.env['res.users'].sudo().browse(user.id)
        partner = user.partner_id
        self.assertEqual(user.login, 'after.update@example.com')
        self.assertEqual(user.email, 'after.update@example.com')
        self.assertEqual(user.name, 'After User')
        self.assertEqual(partner.email, 'after.update@example.com')
        self.assertEqual(partner.first_name, 'After')
        self.assertEqual(partner.last_name, 'User')
        self.assertEqual(partner.name, 'After User')
        self.assertTrue(partner.image_1920)
        self.assertFalse(partner.email_verified)
        self.assertTrue(partner.verification_token)

        refresh_resp = self.url_open('/api/v1/access_token', headers={'Cookie': cookie_header})
        refresh_data = json.loads(refresh_resp.data.decode())
        self.assertFalse(refresh_data['success'])

        reverify_resp = self.url_open(
            '/api/v1/verify-email',
            data=json.dumps({'otp': partner.verification_token, 'email': 'after.update@example.com'}),
            headers=headers,
        )
        reverify_data = json.loads(reverify_resp.data.decode())
        self.assertTrue(reverify_data['success'])
        new_access_token = reverify_data['data']['access_token']

        partial_resp = self.url_open(
            '/api/v1/update_user_details',
            data=json.dumps({'first_name': 'Final'}),
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {new_access_token}',
            },
        )
        partial_data = json.loads(partial_resp.data.decode())
        self.assertTrue(partial_data['success'])

        user.invalidate_recordset()
        partner.invalidate_recordset()
        user = self.env['res.users'].sudo().browse(user.id)
        partner = user.partner_id
        self.assertEqual(partner.first_name, 'Final')
        self.assertEqual(partner.last_name, 'User')
        self.assertEqual(user.login, 'after.update@example.com')
        self.assertEqual(user.name, 'Final User')
        self.assertEqual(partner.name, 'Final User')

    def test_reset_link_generation_uses_frontend_base(self):
        config_model = self.env['zeeve.config'].sudo()
        config = config_model.search([], limit=1)
        if config:
            config.write({'auth_frontend_base_url': 'https://frontend.example'})
        else:
            config_model.create({'auth_frontend_base_url': 'https://frontend.example'})

        partner = self.env['res.partner'].sudo().create({
            'name': 'Link Tester',
            'email': 'link.tester@example.com',
        })
        token = partner.generate_password_reset_token()
        link = partner._build_reset_password_link()

        self.assertTrue(link.startswith('https://frontend.example/auth/reset-password?'))
        self.assertIn(f"token={token}", link)

    @patch('auth_module.utils.oauth.requests.get')
    @patch('auth_module.utils.oauth.requests.post')
    def test_oauth_callback_creates_user(self, mock_post, mock_get):
        provider = self.env.ref('auth_module.oauth_provider_google')
        provider.write({'client_id': 'cid', 'client_secret': 'secret'})
        mock_post.return_value.json.return_value = {
            'access_token': 'token',
            'refresh_token': 'refresh',
            'expires_in': 3600,
        }
        mock_post.return_value.raise_for_status = lambda: None
        mock_get.return_value.json.return_value = {
            'sub': 'uid123',
            'email': 'oauth_user@example.com',
            'name': 'OAuth User',
        }
        resp = self.url_open('/api/v1/oauth/google/callback?code=abc')
        data = json.loads(resp.data.decode())
        self.assertTrue(data['success'])
        self.assertEqual(data['message'], 'Login successful')
        user = self.env['res.users'].sudo().search([('login', '=', 'oauth_user@example.com')], limit=1)
        self.assertTrue(user)

    @patch('auth_module.utils.oauth.requests.get')
    @patch('auth_module.utils.oauth.requests.post')
    def test_oauth_callback_apple(self, mock_post, mock_get):
        provider = self.env.ref('auth_module.oauth_provider_apple')
        provider.write({'client_id': 'cid', 'client_secret': 'secret'})
        payload = {'sub': 'appleuid', 'email': 'apple_user@example.com', 'name': 'Apple User'}
        header = base64.urlsafe_b64encode(b'{}').decode().rstrip('=')
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
        id_token = f"{header}.{body}.signature"
        mock_post.return_value.json.return_value = {
            'access_token': 'token',
            'refresh_token': 'refresh',
            'expires_in': 3600,
            'id_token': id_token,
        }
        mock_post.return_value.raise_for_status = lambda: None
        mock_get.return_value.json.return_value = {}
        resp = self.url_open('/api/v1/oauth/apple/callback?code=abc')
        data = json.loads(resp.data.decode())
        self.assertTrue(data['success'])
        self.assertEqual(data['message'], 'Login successful')
        user = self.env['res.users'].sudo().search([('login', '=', 'apple_user@example.com')], limit=1)
        self.assertTrue(user)


class TestAuthMenu(TransactionCase):
    def test_menu_view_accessible(self):
        menu = self.env.ref('auth_module.menu_auth_module_users', raise_if_not_found=False)
        self.assertTrue(menu)
        action = menu.action
        self.assertEqual(action.res_model, 'res.partner')
