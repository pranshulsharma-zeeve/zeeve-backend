import secrets
from datetime import timedelta
from odoo import fields, models, api

class UserInvitation(models.Model):
    _name = 'user.invitation'
    _description = 'User Invitation'

    email = fields.Char(string='Email', required=True)
    role = fields.Selection([
        ('admin', 'Admin'),
        ('operator', 'Operator')
    ], string='Role', required=True)
    node_access_type = fields.Selection([
        ('all', 'All Nodes and Rollups'),
        ('specific', 'Specific Nodes/Rollups')
    ], string='Node/Rollups Access', default='all')
    specific_nodes = fields.Char(string='Specific Nodes (JSON)', help="List of selected nodes if access is specific")
    specific_rollups = fields.Char(string='Specific Rollups (JSON)', help="List of selected rollups if access is specific")
    company_id = fields.Many2one('res.company', string='Company', required=True)
    invited_by = fields.Many2one('res.users', string='Invited By', required=True)
    token = fields.Char(string='Invitation Token', readonly=True, copy=False)
    status = fields.Selection([
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected'),
        ('expired', 'Expired'),
        ('revoked', 'Revoked')
    ], string='Status', default='pending')
    expiry_date = fields.Datetime(string='Expiry Date', readonly=True)
    is_existing_user = fields.Boolean(string='Is Existing User', compute='_compute_is_existing_user', store=True)

    @api.depends('email')
    def _compute_is_existing_user(self):
        for rec in self:
            # Check if a user with this login already exists
            user = self.env['res.users'].sudo().search([('login', '=', rec.email)], limit=1)
            rec.is_existing_user = bool(user)

    @api.model
    def create(self, vals):
        if not vals.get('token'):
            vals['token'] = secrets.token_urlsafe(32)
        if not vals.get('expiry_date'):
            vals['expiry_date'] = fields.Datetime.now() + timedelta(days=7)
        return super(UserInvitation, self).create(vals)

    def action_accept(self):
        self.ensure_one()
        if self.status != 'pending':
            return False
        if self.expiry_date < fields.Datetime.now():
            self.status = 'expired'
            return False
        self.status = 'accepted'
        return True

    def send_invitation_email(self):
        self.ensure_one()
        template = self.env.ref('auth_module.mail_template_invitation_send', raise_if_not_found=False)
        if template:
            template.sudo().send_mail(self.id, force_send=True)

    def send_acceptance_email(self):
        self.ensure_one()
        template = self.env.ref('auth_module.mail_template_invitation_accepted', raise_if_not_found=False)
        if template:
            template.sudo().send_mail(self.id, force_send=True)

    def send_rejection_email(self):
        self.ensure_one()
        template = self.env.ref('auth_module.mail_template_invitation_rejected', raise_if_not_found=False)
        if template:
            template.sudo().send_mail(self.id, force_send=True)

    @api.model
    def invite_user(self, email, role, company_id, invited_by_id, node_access_type='all', specific_nodes=None, specific_rollups=None):
        """
        Create an invitation and send email.
        """
        invitation = self.create({
            'email': email,
            'role': role,
            'company_id': company_id,
            'invited_by': invited_by_id,
            'node_access_type': node_access_type,
            'specific_nodes': specific_nodes,
            'specific_rollups': specific_rollups,
        })
        invitation.send_invitation_email()
        return invitation
