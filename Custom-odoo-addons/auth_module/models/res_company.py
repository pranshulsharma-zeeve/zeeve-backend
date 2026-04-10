from odoo import fields, models

class ResCompany(models.Model):
    _inherit = 'res.company'

    owner_id = fields.Many2one('res.users', string='Company Owner')
