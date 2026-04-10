{
    'name': 'Authentication Module',
    'version': '1.0.1',
    'category': 'Authentication',
    'summary': 'Custom user authentication with OAuth and email verification',
    'depends': ['base', 'mail', 'auth_signup', 'web', 'auth_oauth', 'zeeve_base'],
    'data': [
        'security/ir.model.access.csv',
        'data/oauth_provider.xml',
        'data/zeeve_template.xml',
        'data/opn_templates.xml',
        'views/web_login_hide_oauth.xml',
        'views/res_partner_views.xml',
    ],
    'installable': True,
    'application': False,
}
