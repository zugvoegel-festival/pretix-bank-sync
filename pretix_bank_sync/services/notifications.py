"""
Notifications for bank connection status (expiry, reauthorization).
"""
import logging
from django.utils.translation import gettext_lazy as _

from pretix.base.models import User
from pretix.base.services.mail import SendMailException, mail
from pretix.base.i18n import LazyI18nString
from pretix.helpers.urls import build_absolute_uri

logger = logging.getLogger(__name__)


def _get_organizer_notification_emails(connection):
    """
    Emails of active users who can change organizer settings for this organizer.
    """
    emails = list(
        User.objects.filter(
            teams__organizer=connection.organizer,
            teams__can_change_organizer_settings=True,
            is_active=True,
        )
        .exclude(email__isnull=True)
        .exclude(email='')
        .values_list('email', flat=True)
        .distinct()
    )
    return emails


def _settings_url(connection):
    return build_absolute_uri(
        'plugins:pretix_bank_sync:connections_list',
        kwargs={'organizer': connection.organizer.slug},
    )


def send_connection_notification(connection, notification_type):
    """
    Send an email notification about the bank connection to organizer admins.

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
    settings_url = _settings_url(connection)
    days = connection.days_until_consent_expiry()
    context = {
        'organizer': organizer_name,
        'settings_url': settings_url,
        'days': days if days is not None else '?',
    }

    if notification_type == 'needs_reauthorization':
        subject = LazyI18nString.from_gettext(_('Bank connection needs reauthorization: {organizer}'))
        body = _(
            'Hello,\n\n'
            'The bank connection for organizer "{organizer}" needs to be reauthorized.\n\n'
            'Open Bank Sync and click Renew:\n'
            '{settings_url}\n\n'
            'Best regards,\n'
            'Your pretix system'
        )
    elif notification_type == 'expiring_soon':
        subject = LazyI18nString.from_gettext(_('Bank connection expiring soon: {organizer}'))
        body = _(
            'Hello,\n\n'
            'The bank connection for organizer "{organizer}" will expire in about {days} day(s).\n\n'
            'Please renew the authorization before it expires:\n'
            '{settings_url}\n\n'
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
