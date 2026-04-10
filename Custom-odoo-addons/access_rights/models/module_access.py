from odoo import fields, models

class ModuleAccess(models.Model):
    _name = 'module.access'
    _description = 'Module-Level Access Control'

    user_id = fields.Many2one('res.users', string='User', required=True, ondelete='cascade')
    module_name = fields.Selection([
        ('subscription_management', 'Subscription Management'),
        ('rollup_management', 'Rollup Management'),
        ('data_importer', 'Data Importer')
    ], string='Module Name', required=True)
    read_access = fields.Boolean(string='Read Access', default=True)

    _sql_constraints = [
        ('user_module_unique', 'unique(user_id, module_name)', 'Module access already exists for this user.')
    ]
