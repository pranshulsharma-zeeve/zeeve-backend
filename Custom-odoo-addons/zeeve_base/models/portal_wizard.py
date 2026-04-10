from odoo import models


class PortalWizard(models.TransientModel):
    _inherit = 'portal.wizard'
    
    def action_apply(self):
        """Completely block all portal emails"""
        # Disable all email sending
        self = self.with_context(
            mail_create_nolog=True,
            mail_notrack=True,
            no_portal_invitation=True,
            tracking_disable=True,
            mail_create_nosubscribe=True,
            no_mail=True
        )
        return super(PortalWizard, self).action_apply()


class PortalWizardUser(models.TransientModel):
    _inherit = 'portal.wizard.user'
    
    def _send_email(self):
        """Never send portal invitation emails"""
        return True
    
    def action_grant_access(self):
        """Block all emails when granting access"""
        self = self.with_context(
            mail_create_nolog=True,
            no_portal_invitation=True,
            tracking_disable=True,
            no_mail=True
        )
        return super(PortalWizardUser, self).action_grant_access()
