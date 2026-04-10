from odoo import models, fields


class ZeeveConfig(models.Model):
    """Stores configuration for Zeeve such as admin e-mails and social links."""

    _name = 'zeeve.config'
    _description = 'Zeeve Configuration'

    admin_emails = fields.Char('Admin Emails', help='Comma separated list of admin emails')
    admin_channel_ids = fields.One2many(
        'zeeve.admin.channel',
        'config_id',
        string='Admin Notification Channels',
        help='Optional per-product/channel admin recipient lists.',
    )
    support_email = fields.Char('Support Email')
    twitter_url = fields.Char('Twitter URL')
    linkedin_url = fields.Char('LinkedIn URL')
    telegram_url = fields.Char('Telegram URL')
    network_type_ids = fields.Many2many(
        'zeeve.network.type',
        'zeeve_config_network_type_rel',
        'config_id',
        'network_type_id',
        string='Network Types',
        help='Network types available to protocols.',
    )


class ZeeveNetworkType(models.Model):
    """Network categories (e.g. Mainnet, Testnet) selectable on protocols."""

    _name = 'zeeve.network.type'
    _description = 'Zeeve Network Type'
    _order = 'name'

    name = fields.Char(string='Name', required=True)
    code = fields.Char(string='Code', help='Optional unique code for integrations.')
    description = fields.Text(string='Description')

    _sql_constraints = [
        ('zeeve_network_type_name_uniq', 'unique(name)', 'Network type name must be unique.'),
        ('zeeve_network_type_code_uniq', 'unique(code)', 'Network type code must be unique.'),
    ]


class ZeeveAdminChannel(models.Model):
    """Store named e-mail recipient lists for admin notifications."""

    _name = 'zeeve.admin.channel'
    _description = 'Admin Notification Channel'
    _order = 'name'

    name = fields.Char(string='Channel Name', required=True)
    code = fields.Char(
        string='Technical Code',
        required=True,
        help='Unique identifier used in code to reference this channel.',
    )
    config_id = fields.Many2one(
        'zeeve.config',
        string='Configuration',
        ondelete='cascade',
        required=True,
    )
    email_to = fields.Char(
        string='To Recipients',
        help='Comma separated list of primary recipients.',
    )
    email_cc = fields.Char(
        string='CC Recipients',
        help='Comma separated list of CC recipients.',
    )
    description = fields.Text(string='Description')
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('zeeve_admin_channel_code_unique', 'unique(code)', 'Channel code must be unique.'),
    ]
