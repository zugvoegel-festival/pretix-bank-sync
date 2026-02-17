"""
Notifications for bank connection status (expiry, reauthorization).
"""
import logging
from django.utils.translation import gettext_lazy as _

from pretix.base.models import User
from pretix.base.services.mail import SendMailException, mail
from pretix.base.i18n import LazyI18nString

logger = logging.getLogger(__name__)


def _get_organizer_notification_emails(connection):
    """Collect email addresses of organizer team members (with organizer settings permission)."""
    emails = list(
        User.objects.filter(
            teams__organizer=connection.organizer,
            is_active=True,
        )
        .exclude(email__isnull=True)
        .exclude(email='')
        .values_list('email', flat=True)
        .distinct()
    )
    return emails


def send_connection_notification(connection, notification_type):
    """
    Send an email notification about the bank connection to organizer team members.

    :param connection: BankConnection instance
    :param notification_type: One of 'needs_reauthorization', 'expiring_soon'
    """
    emails = _get_organizer_notification_emails(connection)
    if not emails:
        logger.warning(
            "No recipient emails for bank connection notification "
            "(organizer=%s, type=%s)",
            connection.organizer.slug,
            notification_type,
        )
        return

    organizer_name = str(connection.organizer.name)
    context = {'organizer': organizer_name}

    if notification_type == 'needs_reauthorization':
        subject = LazyI18nString.from_gettext(_('Bank connection needs reauthorization: {organizer}'))
        body = _(
            'Hello,\n\n'
            'The bank connection for organizer "{organizer}" needs to be reauthorized. '
            'Please go to the bank sync settings and reconnect the account.\n\n'
            'Best regards,\n'
            'Your pretix system'
        )
    elif notification_type == 'expiring_soon':
        subject = LazyI18nString.from_gettext(_('Bank connection expiring soon: {organizer}'))
        body = _(
            'Hello,\n\n'
            'The bank connection for organizer "{organizer}" will expire within 7 days. '
            'Please reauthorize the connection in the bank sync settings to avoid interruption.\n\n'
            'Best regards,\n'
            'Your pretix system'
        )
    else:
        logger.warning("Unknown bank connection notification type: %s", notification_type)
        return

    try:
        mail(
            email=emails,
            subject=subject,
            template=LazyI18nString.from_gettext(body),
            context=context,
            organizer=connection.organizer,
            plain_text_only=True,
            auto_email=True,
        )
    except SendMailException as e:
        logger.exception("Failed to send bank connection notification: %s", e)
        raise
