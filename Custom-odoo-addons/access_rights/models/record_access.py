from odoo import fields, models

class RecordAccess(models.Model):
    _name = 'record.access'
    _description = 'Record-Level Access Control'

    user_id = fields.Many2one('res.users', string='User', required=True, ondelete='cascade')
    module_name = fields.Selection([
        ('subscription_management', 'Subscription Management'),
        ('rollup_management', 'Rollup Management')
    ], string='Module Name', required=True)
    record_id = fields.Integer(string='Record ID', required=True)

    _sql_constraints = [
        ('user_record_unique', 'unique(user_id, module_name, record_id)', 'Record access already exists for this user.')
    ]
