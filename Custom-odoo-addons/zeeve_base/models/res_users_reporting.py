# -*- coding: utf-8 -*-
"""
Cron methods for automated report email delivery.
"""

import logging
from odoo import models, api

_logger = logging.getLogger(__name__)


class ResUsersReporting(models.Model):
    _inherit = 'res.users'

    @api.model
    def _send_weekly_report_emails(self):
        """
        Cron job method to send weekly reports to all users with active nodes.
        Called every Monday at 9 AM UTC.
        """
        _logger.info("Starting weekly report email batch send...")
        
        # Find all users with email addresses
        all_users = self.search([('email', '!=', False),('partner_id.multi_tenant_host', '=', 'zeeve')])
        
        sent_count = 0
        failed_count = 0
        skipped_count = 0
        
        for user in all_users:
            try:
                # Import mail_utils here to avoid circular import
                from ..utils.reports import mail_utils
                
                # Check if user has any nodes (will be validated in send function)
                result = mail_utils.send_report_email(
                    self.env,
                    user,
                    range_type='weekly',
                    timezone_str='UTC'
                )
                
                if result['success']:
                    sent_count += 1
                    mail_utils.log_email_delivery(self.env, user.id, 'weekly', True)
                elif result.get('skipped'):
                    skipped_count += 1
                else:
                    failed_count += 1
                    mail_utils.log_email_delivery(
                        self.env, user.id, 'weekly', False, result.get('error')
                    )
                
                # Commit after each user to prevent losing progress on errors
                self.env.cr.commit()
                
            except Exception as e:
                failed_count += 1
                _logger.error(f"Error sending weekly report to user {user.id}: {e}", exc_info=True)
                # Rollback the failed transaction
                try:
                    self.env.cr.rollback()
                except Exception as rollback_error:
                    _logger.error(f"Error during rollback for user {user.id}: {rollback_error}")
                # Continue with other users
                continue
        
        _logger.info(
            f"Weekly report batch complete. Sent: {sent_count}, Skipped: {skipped_count}, Failed: {failed_count}"
        )
        
        return True

    @api.model
    def _send_monthly_report_emails(self):
        """
        Cron job method to send monthly reports to all users with active nodes.
        Called on the 1st of each month at 9 AM UTC.
        """
        _logger.info("Starting monthly report email batch send...")
        
        # Find all users with email addresses
        all_users = self.search([('email', '!=', False),('partner_id.multi_tenant_host', '=', 'zeeve')])
        
        sent_count = 0
        failed_count = 0
        skipped_count = 0
        
        for user in all_users:
            try:
                # Import mail_utils here to avoid circular import
                from ..utils.reports import mail_utils
                
                # Check if user has any nodes (will be validated in send function)
                result = mail_utils.send_report_email(
                    self.env,
                    user,
                    range_type='monthly',
                    timezone_str='UTC'
                )
                
                if result['success']:
                    sent_count += 1
                    mail_utils.log_email_delivery(self.env, user.id, 'monthly', True)
                elif result.get('skipped'):
                    skipped_count += 1
                else:
                    failed_count += 1
                    mail_utils.log_email_delivery(
                        self.env, user.id, 'monthly', False, result.get('error')
                    )
                
                # Commit after each user to prevent losing progress on errors
                self.env.cr.commit()
                
            except Exception as e:
                failed_count += 1
                _logger.error(f"Error sending monthly report to user {user.id}: {e}", exc_info=True)
                # Rollback the failed transaction
                try:
                    self.env.cr.rollback()
                except Exception as rollback_error:
                    _logger.error(f"Error during rollback for user {user.id}: {rollback_error}")
                # Continue with other users
                continue
        
        _logger.info(
            f"Monthly report batch complete. Sent: {sent_count}, Skipped: {skipped_count}, Failed: {failed_count}"
        )
        
        return True
