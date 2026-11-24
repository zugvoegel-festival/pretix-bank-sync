"""
Signals for Bank Sync plugin.

Handles settings, navigation, and periodic tasks.
"""
import logging
from django.dispatch import receiver
from django.urls import resolve, reverse
from django.utils.translation import gettext_lazy as _
from django.utils.timezone import now

from pretix.base.settings import settings_hierarkey
from pretix.base.signals import periodic_task
from pretix.control.signals import nav_organizer, nav_event

from .models import BankConnection

logger = logging.getLogger(__name__)

# Register plugin settings defaults (keys must match views/tasks)
settings_hierarkey.add_default("pretix_bank_sync_gocardless_client_id", "", str)
settings_hierarkey.add_default("pretix_bank_sync_gocardless_client_secret", "", str)
settings_hierarkey.add_default("pretix_bank_sync_gocardless_redirect_uri", "", str)
# hierarkey expects a *serialized* default (string); bool True breaks some paths
settings_hierarkey.add_default("pretix_bank_sync_auto_confirm", "True", bool)


@receiver(nav_organizer, dispatch_uid="bank_sync_nav_organizer")
def nav_organizer_settings(sender, request, organizer, **kwargs):
    """Add Bank Sync to organizer settings navigation."""
    if not request.user.has_organizer_permission(organizer, 'can_change_organizer_settings'):
        return []

    url = resolve(request.path_info)
    active_names = (
        'connections_list', 'bank_setup_wizard', 'connection_delete',
        'connection_renew', 'settings', 'transactions', 'review_queue',
        'transaction_discard', 'transaction_assign',
        'organizer_match_approve', 'organizer_match_reject',
    )
    return [{
        'label': _('Bank Sync'),
        'url': reverse('plugins:pretix_bank_sync:connections_list', kwargs={
            'organizer': organizer.slug
        }),
        'parent': reverse('control:organizer.edit', kwargs={
            'organizer': organizer.slug
        }),
        'active': url.namespace == 'plugins:pretix_bank_sync' and url.url_name in active_names,
    }]


@receiver(periodic_task, dispatch_uid="bank_sync_periodic_sync")
def periodic_sync_transactions(sender, **kwargs):
    """
    Periodic task to sync bank transactions.

    Respects the GoCardless/bank budget (max 4 syncs/day, spaced ~6h apart).
    The sync task itself atomically claims the attempt; we only pre-check here
    to avoid flooding the queue.
    """
    from django.core.cache import cache

    from .tasks import sync_bank_transactions
    from .services.rate_limit import MAX_SYNCS_PER_DAY, MIN_SYNC_INTERVAL

    # Get all active connections that can sync
    connections = BankConnection.objects.filter(status=BankConnection.STATUS_ACTIVE)

    for connection in connections:
        # Reset daily counter if needed
        connection.reset_daily_counter()

        # Pre-check without claiming — task calls try_begin_sync
        if not connection.can_sync(force=False):
            logger.debug(
                f"Skipping sync for connection {connection.id}: "
                f"{connection.sync_block_reason(force=False) or 'not eligible'} "
                f"(count={connection.sync_count_today}/{MAX_SYNCS_PER_DAY})"
            )
            continue

        # Debounce queueing until spacing window elapses (task also claims a slot)
        queue_key = f'bank_sync_queued_{connection.id}'
        if not cache.add(queue_key, True, timeout=int(MIN_SYNC_INTERVAL.total_seconds())):
            logger.debug(f"Sync already queued recently for connection {connection.id}")
            continue

        # Trigger async sync task
        try:
            sync_bank_transactions.apply_async(
                kwargs={'connection_id': connection.id, 'force': False}
            )
            logger.info(f"Queued sync task for connection {connection.id}")
        except Exception as e:
            cache.delete(queue_key)
            logger.error(f"Failed to queue sync task for connection {connection.id}: {e}")
            connection.status = BankConnection.STATUS_ERROR
            connection.last_error = str(e)
            connection.last_error_at = now()
            connection.save(update_fields=['status', 'last_error', 'last_error_at'])


@receiver(periodic_task, dispatch_uid="bank_sync_periodic_expiry_check")
def periodic_check_expiry_warnings(sender, **kwargs):
    """Periodic task to check for connections expiring soon and send warnings."""
    from .tasks import check_connection_expiry_warnings
    
    # Queue expiry check task (runs daily)
    try:
        check_connection_expiry_warnings.apply_async()
        logger.debug("Queued expiry warning check task")
    except Exception as e:
        logger.error(f"Failed to queue expiry warning check task: {e}")


@receiver(nav_event, dispatch_uid="bank_sync_nav_event")
def nav_event_transactions(sender, request, **kwargs):
    """Add Bank Transaction Matches to event navigation."""
    if not request.user.has_event_permission(request.organizer, request.event, 'can_change_orders', request=request):
        return []
    
    url = resolve(request.path_info)
    return [{
        'label': _('Bank Transactions'),
        'url': reverse('plugins:pretix_bank_sync:match_review', kwargs={
            'organizer': request.organizer.slug,
            'event': request.event.slug
        }),
        'parent': reverse('control:event.orders', kwargs={
            'organizer': request.organizer.slug,
            'event': request.event.slug
        }),
        'active': url.namespace == 'plugins:pretix_bank_sync' and url.url_name in ('match_review', 'match_approve', 'match_reject'),
    }]
