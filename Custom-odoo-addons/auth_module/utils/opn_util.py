"""OPN Mail Template Utilities.

This module provides helper functions to send email templates
specific to the OPN (Open Validator Network) platform.
"""

import logging

_logger = logging.getLogger(__name__)


def send_opn_welcome_email(env, partner):
    """Send the OPN welcome email to a partner.

    Args:
        env: Odoo environment
        partner: res.partner record

    Returns:
        bool: True if email was sent successfully, False otherwise
    """
    try:
        template = env.ref('auth_module.mail_template_welcome_user_opn', raise_if_not_found=False)
        if not template:
            _logger.error("OPN Welcome email template not found")
            return False

        if not partner.email:
            _logger.warning(f"Partner {partner.id} has no email address")
            return False

        template.send_mail(partner.id, force_send=True)
        _logger.info(f"OPN Welcome email sent to {partner.email}")
        return True

    except Exception as e:
        _logger.error(f"Error sending OPN welcome email to {partner.email}: {str(e)}")
        return False


def send_opn_otp_verification_email(env, partner, otp_code=None):
    """Send the OPN OTP verification email to a partner.

    Args:
        env: Odoo environment
        partner: res.partner record
        otp_code: Optional OTP code to include in the email context

    Returns:
        bool: True if email was sent successfully, False otherwise
    """
    try:
        template = env.ref('auth_module.mail_template_otp_verification_opn', raise_if_not_found=False)
        if not template:
            _logger.error("OPN OTP Verification email template not found")
            return False

        if not partner.email:
            _logger.warning(f"Partner {partner.id} has no email address")
            return False

        # Build context with OTP code
        ctx = {'otp_code': otp_code or partner.verification_token}
        
        template.with_context(**ctx).send_mail(partner.id, force_send=True)
        _logger.info(f"OPN OTP verification email sent to {partner.email}")
        return True

    except Exception as e:
        _logger.error(f"Error sending OPN OTP verification email to {partner.email}: {str(e)}")
        return False


def send_opn_forgot_password_email(env, partner, reset_link):
    """Send the OPN forgot password email to a partner.

    Args:
        env: Odoo environment
        partner: res.partner record
        reset_link: Password reset link to include in the email

    Returns:
        bool: True if email was sent successfully, False otherwise
    """
    try:
        template = env.ref('auth_module.mail_template_forget_password_opn', raise_if_not_found=False)
        if not template:
            _logger.error("OPN Forgot Password email template not found")
            return False

        if not partner.email:
            _logger.warning(f"Partner {partner.id} has no email address")
            return False

        # Build context with reset link
        ctx = {
            'reset_link': reset_link,
            'reset_password_link': reset_link,
        }
        
        template.with_context(**ctx).send_mail(partner.id, force_send=True)
        _logger.info(f"OPN Forgot password email sent to {partner.email}")
        return True

    except Exception as e:
        _logger.error(f"Error sending OPN forgot password email to {partner.email}: {str(e)}")
        return False


def send_opn_reset_password_confirmation_email(env, partner):
    """Send the OPN password reset confirmation email to a partner.

    Args:
        env: Odoo environment
        partner: res.partner record

    Returns:
        bool: True if email was sent successfully, False otherwise
    """
    try:
        template = env.ref('auth_module.mail_template_reset_password_opn', raise_if_not_found=False)
        if not template:
            _logger.error("OPN Reset Password Confirmation email template not found")
            return False

        if not partner.email:
            _logger.warning(f"Partner {partner.id} has no email address")
            return False

        template.send_mail(partner.id, force_send=True)
        _logger.info(f"OPN Password reset confirmation email sent to {partner.email}")
        return True

    except Exception as e:
        _logger.error(f"Error sending OPN password reset confirmation email to {partner.email}: {str(e)}")
        return False


def send_opn_email_by_type(env, partner, email_type, **kwargs):
    """Send an OPN email based on the specified type.

    This is a convenience function that routes to the appropriate email sender.

    Args:
        env: Odoo environment
        partner: res.partner record
        email_type: Type of email to send ('welcome', 'otp', 'forgot_password', 'reset_confirmation')
        **kwargs: Additional arguments specific to each email type
            - otp_code: For OTP verification emails
            - reset_link: For forgot password emails

    Returns:
        bool: True if email was sent successfully, False otherwise
    """
    email_type_map = {
        'welcome': lambda: send_opn_welcome_email(env, partner),
        'otp': lambda: send_opn_otp_verification_email(env, partner, kwargs.get('otp_code')),
        'forgot_password': lambda: send_opn_forgot_password_email(env, partner, kwargs.get('reset_link')),
        'reset_confirmation': lambda: send_opn_reset_password_confirmation_email(env, partner),
    }

    if email_type not in email_type_map:
        _logger.error(f"Unknown OPN email type: {email_type}")
        return False

    return email_type_map[email_type]()
