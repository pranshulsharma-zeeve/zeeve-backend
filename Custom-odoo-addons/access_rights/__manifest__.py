# -*- coding: utf-8 -*-
{
    'name': 'Access Rights Management',
    'version': '1.0',
    'category': 'Administration',
    'summary': 'Custom access rights and user groups for Odoo modules',
    'description': """
        Access Rights Management
        ========================
        This module provides custom user groups with specific access rights:
        
        * Admin: Full access except settings
        * Technical Manager: Full access including settings
        * Support Staff Manager: Read/Write access to subscriptions and invoicing
        * Support Staff: Read-only access to subscriptions and invoicing
    """,
    'author': 'Zeeve',
    'website': 'https://www.zeeve.io',
    'depends': [
        'base',
        'account',
    ],
    'data': [
        'security/access_rights_groups.xml',
        # 'security/ir.model.access.csv',
        # 'views/menu_access.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
