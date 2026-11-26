"""
Tasks for Bank Sync plugin.

Handles periodic syncing and transaction matching.
"""
import logging
import operator
import re
from decimal import Decimal
from functools import reduce
from typing import List, Optional

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q, Min, Max
from django.db.models.functions import Length
from django.utils.timezone import now
from datetime import timedelta
from django.utils.translation import gettext_noop
from django_scopes import scope

from pretix.base.models import Order, Invoice, OrderPayment, OrderRefund, Quota
from pretix.base.payment import PaymentException
from pretix.base.services.mail import SendMailException
from pretix.base.services.orders import change_payment_provider
from pretix.base.services.tasks import TransactionAwareTask
from pretix.celery_app import app

from .models import BankAccount, BankConnection, BankTransaction, TransactionMatchSuggestion
from .services.enablebanking_service import EnableBankingService
from .services.notifications import send_connection_notification
from .services.credentials import get_enablebanking_credentials
from .services.rate_limit import RateLimitExceeded

logger = logging.getLogger(__name__)


def _find_order_for_code(base_qs, code):
    """Find order by code with variations."""
    try_codes = [
        code,
        Order.normalize_code(code, is_fallback=True),
        code[:settings.ENTROPY['order_code']],
        Order.normalize_code(code[:settings.ENTROPY['order_code']], is_fallback=True)
    ]
    for c in try_codes:
        try:
            return base_qs.get(code=c)
        except Order.DoesNotExist:
            pass
    return None


def _find_order_for_invoice_id(base_qs, prefixes, number):
    """Find order by invoice number."""
    try:
        r = [
            Q(
                prefix__istartswith=prefix,
                full_invoice_no__iregex=prefix + r'[\- ]*0*' + number
            )
            for prefix in set(prefixes)
        ]
        return base_qs.select_related('order').get(
            reduce(operator.or_, r)
        ).order
    except (Invoice.DoesNotExist, Invoice.MultipleObjectsReturned):
        return None


@transaction.atomic
def _handle_transaction(trans: BankTransaction, matches: tuple, regex_match_to_slug, organizer, amount=None):
    """
    Handle a matched transaction - create payment and confirm.
    
    Args:
        trans: The bank transaction
        matches: Tuple of (slug, code) matches
        regex_match_to_slug: Mapping of regex matches to actual slugs
        organizer: The organizer
        amount: Optional amount to use (for partial payments). If None, uses trans.amount
    """
    orders = []
    
    if amount is None:
        amount = trans.amount

    # Filter by currency first (primary matching criterion)
    qs = Order.objects.filter(
        event__organizer=organizer,
        event__currency=trans.currency
    )

    for slug, code in matches:
        original_slug = regex_match_to_slug.get(slug, slug)
        order = _find_order_for_code(
            qs.filter(Q(event__slug__iexact=slug) | Q(event__slug__iexact=original_slug)),
            code
        )
        if order:
            if order.code not in {o.code for o in orders}:
                orders.append(order)
        else:
            # Try invoice number match
            order = _find_order_for_invoice_id(
                Invoice.objects.filter(event__organizer=organizer),
                (slug, original_slug),
                code
            )
            if order and order.code not in {o.code for o in orders}:
                orders.append(order)

    if not orders:
        # No match
        trans.state = BankTransaction.STATE_NOMATCH
        trans.save()
        return

    trans.order = orders[0]
    
    # Check if this is a partial payment
    if amount < orders[0].pending_sum:
        trans.is_partial_payment = True
        # Generate a payment group ID based on order code and date
        trans.payment_group_id = f"{orders[0].code}_{trans.date.isoformat()}"

    if len(orders) > 1:
        # Multi-match! Can we split this automatically?
        order_pending_sum = sum(o.pending_sum for o in orders)
        if order_pending_sum != amount:
            # we can't :( this needs to be dealt with by a human
            trans.state = BankTransaction.STATE_NOMATCH
            trans.error_message = gettext_noop('Automatic split to multiple orders not possible.')
            trans.save()
            return

        # we can!
        splits = [(o, o.pending_sum) for o in orders]
    else:
        splits = [(orders[0], amount)]

    for o in orders:
        if o.status == Order.STATUS_PAID and o.pending_sum <= Decimal('0.00'):
            trans.state = BankTransaction.STATE_DUPLICATE
            trans.save()
            return
        elif o.status == Order.STATUS_CANCELED:
            trans.state = BankTransaction.STATE_ERROR
            trans.error_message = gettext_noop('The order has already been canceled.')
            trans.save()
            return

        if trans.currency != o.event.currency:
            trans.state = BankTransaction.STATE_ERROR
            trans.error_message = gettext_noop('Currencies do not match.')
            trans.save()
            return

    trans.state = BankTransaction.STATE_MATCHED
    for order, amount in splits:
        info_data = {
            'reference': trans.reference,
            'date': trans.date.isoformat() if trans.date else '',
            'debtor_name': trans.debtor_name,
            'creditor_name': trans.creditor_name,
            'iban': trans.debtor_account_iban or trans.creditor_account_iban,
            'full_amount': str(trans.amount),
            'trans_id': trans.pk,
            'transaction_id': trans.transaction_id,
        }

        if amount < Decimal("0.00"):
            # Handle refund
            pending_refund = order.refunds.filter(
                amount=-amount,
                provider__in=('manual', 'banktransfer'),
                state__in=(OrderRefund.REFUND_STATE_CREATED, OrderRefund.REFUND_STATE_TRANSIT),
            ).first()
            existing_payment = order.payments.filter(
                provider='banktransfer',
                state__in=(OrderPayment.PAYMENT_STATE_CONFIRMED,),
            ).first()
            if pending_refund:
                pending_refund.provider = "banktransfer"
                pending_refund.info_data = {
                    **pending_refund.info_data,
                    **info_data,
                }
                pending_refund.done()
            elif existing_payment:
                existing_payment.create_external_refund(
                    amount=-amount,
                    info=str(info_data)
                )
            else:
                r = order.refunds.create(
                    state=OrderRefund.REFUND_STATE_EXTERNAL,
                    source=OrderRefund.REFUND_SOURCE_EXTERNAL,
                    amount=-amount,
                    order=order,
                    execution_date=now(),
                    provider='banktransfer',
                    info=str(info_data)
                )
                order.log_action('pretix.event.order.refund.created.externally', {
                    'local_id': r.local_id,
                    'provider': r.provider,
                })
            continue

        try:
            p, created = order.payments.get_or_create(
                amount=amount,
                provider='banktransfer',
                state__in=(OrderPayment.PAYMENT_STATE_CREATED, OrderPayment.PAYMENT_STATE_PENDING),
                defaults={
                    'state': OrderPayment.PAYMENT_STATE_CREATED,
                }
            )
        except OrderPayment.MultipleObjectsReturned:
            created = False
            p = order.payments.filter(
                amount=amount,
                provider='banktransfer',
                state__in=(OrderPayment.PAYMENT_STATE_CREATED, OrderPayment.PAYMENT_STATE_PENDING),
            ).last()

        p.info_data = {
            **p.info_data,
            **info_data,
        }

        if created:
            # Perform payment method switching on-demand
            old_fee, new_fee, fee, p, new_invoice_created = change_payment_provider(
                order, p.payment_provider, p.amount, new_payment=p, create_log=False
            )
            if fee:
                p.fee = fee
                p.save(update_fields=['fee'])

        try:
            p.confirm()
            trans.payment = p
        except Quota.QuotaExceededException:
            logger.warning(f"Quota exceeded when confirming payment for order {order.code}")
        except SendMailException:
            logger.warning(f"Email send failed when confirming payment for order {order.code}")
        except Exception as e:
            logger.error(f"Error confirming payment for order {order.code}: {e}")
            trans.state = BankTransaction.STATE_ERROR
            trans.error_message = str(e)

    trans.save()


@transaction.atomic
def approve_match_suggestion(suggestion_id, user):
    """Approve a match suggestion and process the transaction."""
    from .models import TransactionMatchSuggestion
    
    suggestion = TransactionMatchSuggestion.objects.select_related('transaction', 'order').get(pk=suggestion_id)
    
    if suggestion.is_approved is not None:
        raise ValueError("Suggestion has already been reviewed")
    
    trans = suggestion.transaction
    
    # Check if this is a multi-order payment
    # Look for other pending suggestions for the same transaction that mention multiple orders
    is_multi_order = "Multiple order codes found" in suggestion.match_reason
    
    if is_multi_order:
        # Find all related suggestions for this transaction that mention multiple orders
        related_suggestions = TransactionMatchSuggestion.objects.filter(
            transaction=trans,
            is_approved__isnull=True,
            match_reason__contains="Multiple order codes found"
        ).select_related('order')
        
        # Get all orders from related suggestions
        orders = [s.order for s in related_suggestions]
        total_pending = sum(o.pending_sum for o in orders)
        
        # Verify the sum matches the transaction amount
        if abs(trans.amount - total_pending) < Decimal("0.01"):
            # This is a valid multi-order payment - approve all related suggestions
            for sug in related_suggestions:
                sug.is_approved = True
                sug.reviewed_at = now()
                sug.reviewed_by = user
                sug.save()
            
            # Process the transaction with all orders
            trans.order = orders[0]  # Set primary order (first one)
            trans.state = BankTransaction.STATE_MATCHED
            
            # Build matches list for _handle_transaction
            matches = [(o.event.slug, o.code) for o in orders]
            regex_match_to_slug = {}
            
            # Use the existing _handle_transaction logic which handles multi-order splits
            _handle_transaction(
                trans,
                matches,
                regex_match_to_slug,
                trans.connection.organizer,
                amount=trans.amount
            )
            
            # Reject any other suggestions for this transaction
            TransactionMatchSuggestion.objects.filter(
                transaction=trans,
                is_approved__isnull=True
            ).update(
                is_approved=False,
                reviewed_at=now(),
                reviewed_by=user
            )
            
            return trans
        else:
            # Amounts don't match - treat as single order payment
            is_multi_order = False
    
    # Single order payment (original logic)
    if not is_multi_order:
        order = suggestion.order
        
        # Mark suggestion as approved
        suggestion.is_approved = True
        suggestion.reviewed_at = now()
        suggestion.reviewed_by = user
        suggestion.save()
        
        # Process the transaction
        trans.order = order
        trans.state = BankTransaction.STATE_MATCHED
        
        # Check if this is a partial payment
        if trans.amount < order.pending_sum:
            trans.is_partial_payment = True
            trans.payment_group_id = f"{order.code}_{trans.date.isoformat()}"
        
        # Use the existing _handle_transaction logic
        _handle_transaction(
            trans,
            [(order.event.slug, order.code)],
            {},
            trans.connection.organizer,
            amount=trans.amount
        )
        
        # Reject other suggestions for this transaction
        TransactionMatchSuggestion.objects.filter(
            transaction=trans,
            is_approved__isnull=True
        ).update(
            is_approved=False,
            reviewed_at=now(),
            reviewed_by=user
        )
    
    return trans


def _normalize_name(name):
    """Normalize name for comparison (remove extra spaces, convert to uppercase)."""
    if not name:
        return ""
    return " ".join(name.upper().split())


def _as_decimal(value) -> Decimal:
    """Coerce money-like values to Decimal (avoids Decimal/float TypeError)."""
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _currency_code(value) -> str:
    return (value or "").strip().upper()


def _calculate_name_similarity(name1, name2):
    """Calculate similarity between two names (simple word-based matching)."""
    if not name1 or not name2:
        return 0.0
    
    name1_norm = _normalize_name(name1)
    name2_norm = _normalize_name(name2)
    
    if name1_norm == name2_norm:
        return 1.0
    
    # Split into words and check for common words
    words1 = set(name1_norm.split())
    words2 = set(name2_norm.split())
    
    if not words1 or not words2:
        return 0.0
    
    # Calculate Jaccard similarity (intersection over union)
    intersection = len(words1 & words2)
    union = len(words1 | words2)
    
    if union == 0:
        return 0.0
    
    return intersection / union


def _find_fuzzy_code_matches(reference_text, order_codes):
    """Find order codes that appear within the reference text (fuzzy matching)."""
    matches = []
    reference_upper = (reference_text or "").upper()
    
    for code in order_codes:
        if not code:
            continue
        code_upper = code.upper()
        # Check if code appears in reference (with or without separators)
        if code_upper in reference_upper:
            matches.append(code)
        # Also check normalized versions (remove separators)
        code_normalized = re.sub(r'[^A-Z0-9]', '', code_upper)
        ref_normalized = re.sub(r'[^A-Z0-9]', '', reference_upper)
        if code_normalized in ref_normalized:
            matches.append(code)
    
    return matches


# Prefer classic pretix bank-transfer style references: EVENTSLUG-ORDERCODE
EVENT_ORDER_PATTERN = re.compile(
    r'(?i)\b([a-z0-9][a-z0-9._-]{0,62})\s*[-_/]\s*([a-z0-9]{3,20})\b'
)


def _suggestion_for_order(trans, order, match_type, match_reason, base_confidence):
    amount = _as_decimal(trans.amount)
    pending = _as_decimal(order.pending_sum)
    amount_match = abs(amount - pending) < Decimal("0.01")
    amount_diff = amount - pending
    currency_match = _currency_code(trans.currency) == _currency_code(order.event.currency)
    is_eur = _currency_code(trans.currency) == 'EUR'

    confidence = base_confidence
    if amount_match and currency_match and is_eur:
        confidence = min(1.0, base_confidence + 0.05)
    elif not amount_match:
        confidence = max(0.5, confidence - 0.1)
    if not currency_match or not is_eur:
        confidence = max(0.5, confidence - 0.1)

    reason = match_reason
    if not currency_match:
        reason += f" (currency mismatch: {trans.currency} vs {order.event.currency})"
    if not is_eur:
        reason += f" (non-EUR currency: {trans.currency})"

    return {
        'order': order,
        'match_type': match_type,
        'confidence_score': confidence,
        'match_reason': reason,
        'amount_match': amount_match,
        'amount_difference': amount_diff,
    }


def _find_event_order_matches(trans, pending_orders):
    """Match EVENTSLUG-ORDERCODE (or EVENTSLUG/ORDERCODE) in remittance text."""
    reference_text = trans.get_reference_text()
    if not reference_text:
        return []

    by_key = {(o.event.slug.upper(), o.code.upper()): o for o in pending_orders}
    seen = set()
    suggestions = []

    for match in EVENT_ORDER_PATTERN.finditer(reference_text):
        slug = match.group(1).upper()
        code = match.group(2).upper()
        key = (slug, code)
        if key in seen:
            continue
        order = by_key.get(key)
        if not order:
            continue
        seen.add(key)
        suggestions.append(
            _suggestion_for_order(
                trans,
                order,
                TransactionMatchSuggestion.MATCH_TYPE_EVENT_ORDER,
                f"Event+order reference '{order.event.slug}-{order.code}' found in remittance",
                base_confidence=1.0,
            )
        )
    return suggestions


def _generate_match_suggestions(trans: BankTransaction, organizer):
    """Generate match suggestions using multi-step fuzzy logic."""
    suggestions = []
    amount = _as_decimal(trans.amount)
    tx_currency = _currency_code(trans.currency)
    
    # Get all pending orders for this organizer
    pending_orders = Order.objects.filter(
        event__organizer=organizer,
        status=Order.STATUS_PENDING
    ).select_related('event', 'customer')
    
    if not pending_orders.exists():
        return suggestions
    
    reference_text = trans.get_reference_text() or ""
    reference_upper = reference_text.upper()
    sender_name = trans.get_sender_name()
    
    # Get all order codes
    order_codes = list(pending_orders.values_list('code', flat=True))

    # Step 0: EVENTSLUG-ORDERCODE (preferred, organizer-wide)
    event_order_suggestions = _find_event_order_matches(trans, pending_orders)
    if event_order_suggestions:
        suggestions.extend(event_order_suggestions)
        suggestions.sort(key=lambda x: x['confidence_score'], reverse=True)
        return suggestions[:10]
    
    # Step 1: Exact order code match
    matched_orders = []
    for order in pending_orders:
        order_code_upper = order.code.upper()
        
        # Check exact match in reference
        if order_code_upper in reference_upper or order_code_upper.replace("-", "") in reference_upper.replace(" ", "").replace("-", ""):
            matched_orders.append(order)
    
    # Check if multiple orders were found - this might be a multi-order payment
    if len(matched_orders) > 1:
        # Calculate total pending sum for all matched orders
        total_pending = sum((_as_decimal(o.pending_sum) for o in matched_orders), Decimal("0"))
        total_amount_match = abs(amount - _as_decimal(total_pending)) < Decimal("0.01")
        total_amount_diff = amount - _as_decimal(total_pending)
        
        # Check currency match (all orders should have same currency)
        currency_match = all(
            tx_currency == _currency_code(o.event.currency) 
            for o in matched_orders
        )
        is_eur = tx_currency == 'EUR'
        
        if total_amount_match and currency_match:
            # Multiple orders found and total amount matches - likely multi-order payment
            # Create suggestions for all orders with a special flag
            for order in matched_orders:
                confidence = 0.95 if is_eur else 0.85
                match_reason = f"Multiple order codes found in reference. This transaction appears to pay {len(matched_orders)} orders. Order '{order.code}' is one of them. Total: {trans.amount} {trans.currency} = sum of {len(matched_orders)} orders."
                
                suggestions.append({
                    'order': order,
                    'match_type': TransactionMatchSuggestion.MATCH_TYPE_EXACT_CODE,
                    'confidence_score': confidence,
                    'match_reason': match_reason,
                    'amount_match': True,  # Individual amount doesn't match, but total does
                    'amount_difference': _as_decimal(order.pending_sum) - amount,  # Difference from total
                    'is_multi_order': True,  # Flag to indicate this is part of a multi-order payment
                    'related_orders': [o.code for o in matched_orders],  # List of all orders in this payment
                })
        else:
            # Multiple orders found but amount doesn't match - create individual suggestions
            for order in matched_orders:
                amount_match = abs(amount - _as_decimal(order.pending_sum)) < Decimal("0.01")
                amount_diff = amount - _as_decimal(order.pending_sum)
                currency_match = tx_currency == _currency_code(order.event.currency)
                is_eur = tx_currency == 'EUR'
                
                confidence = 1.0
                if amount_match and currency_match and is_eur:
                    confidence = 1.0
                elif amount_match and currency_match:
                    confidence = 0.85
                elif amount_match and is_eur:
                    confidence = 0.85
                elif amount_match:
                    confidence = 0.8
                else:
                    confidence = 0.9
                    if not is_eur:
                        confidence = 0.85
                
                match_reason = f"Exact order code '{order.code}' found in transaction reference"
                if len(matched_orders) > 1:
                    match_reason += f" (Note: {len(matched_orders)} order codes found, but amounts don't sum correctly)"
                if not currency_match:
                    match_reason += f" (currency mismatch: {trans.currency} vs {order.event.currency})"
                if not is_eur:
                    match_reason += f" (non-EUR currency: {trans.currency})"
                
                suggestions.append({
                    'order': order,
                    'match_type': TransactionMatchSuggestion.MATCH_TYPE_EXACT_CODE,
                    'confidence_score': confidence,
                    'match_reason': match_reason,
                    'amount_match': amount_match,
                    'amount_difference': amount_diff,
                })
    else:
        # Single order match (original logic)
        for order in matched_orders:
            amount_match = abs(amount - _as_decimal(order.pending_sum)) < Decimal("0.01")
            amount_diff = amount - _as_decimal(order.pending_sum)
            currency_match = tx_currency == _currency_code(order.event.currency)
            is_eur = tx_currency == 'EUR'
            
            confidence = 1.0  # Highest confidence for exact code match
            if amount_match and currency_match and is_eur:
                confidence = 1.0
            elif amount_match and currency_match:
                # Currency matches but not EUR - needs review
                confidence = 0.85
            elif amount_match and is_eur:
                # Amount matches, EUR, but currency mismatch - needs review
                confidence = 0.85
            elif amount_match:
                # Amount matches but currency issues - needs review
                confidence = 0.8
            else:
                # Slightly lower if amount doesn't match (might be partial payment)
                confidence = 0.9
                if not is_eur:
                    confidence = 0.85  # Lower if not EUR
            
            match_reason = f"Exact order code '{order.code}' found in transaction reference"
            if not currency_match:
                match_reason += f" (currency mismatch: {trans.currency} vs {order.event.currency})"
            if not is_eur:
                match_reason += f" (non-EUR currency: {trans.currency})"
            
            suggestions.append({
                'order': order,
                'match_type': TransactionMatchSuggestion.MATCH_TYPE_EXACT_CODE,
                'confidence_score': confidence,
                'match_reason': match_reason,
                'amount_match': amount_match,
                'amount_difference': amount_diff,
            })
    
    # Step 2: Fuzzy order code match (code within other words)
    if not suggestions:  # Only do fuzzy if no exact matches
        fuzzy_matches = _find_fuzzy_code_matches(reference_text, order_codes)
        for order in pending_orders:
            if order.code in fuzzy_matches:
                amount_match = abs(amount - _as_decimal(order.pending_sum)) < Decimal("0.01")
                amount_diff = amount - _as_decimal(order.pending_sum)
                currency_match = tx_currency == _currency_code(order.event.currency)
                is_eur = tx_currency == 'EUR'
                
                confidence = 0.7  # Lower confidence for fuzzy match
                if amount_match and currency_match and is_eur:
                    confidence = 0.75
                elif amount_match:
                    confidence = 0.7
                else:
                    confidence = 0.65
                
                # Lower confidence if not EUR
                if not is_eur:
                    confidence = max(0.6, confidence - 0.1)
                
                match_reason = f"Order code '{order.code}' found within transaction reference text"
                if not currency_match:
                    match_reason += f" (currency mismatch: {trans.currency} vs {order.event.currency})"
                if not is_eur:
                    match_reason += f" (non-EUR currency: {trans.currency})"
                
                suggestions.append({
                    'order': order,
                    'match_type': TransactionMatchSuggestion.MATCH_TYPE_FUZZY_CODE,
                    'confidence_score': confidence,
                    'match_reason': match_reason,
                    'amount_match': amount_match,
                    'amount_difference': amount_diff,
                })
    
    # Step 3: Sender name match
    if sender_name:
        for order in pending_orders:
            # Try to match against customer name
            customer_name = None
            if order.customer:
                customer_name = order.customer.name_cached
            elif order.email:
                # Could also match against email, but name is more reliable
                pass
            
            if customer_name:
                name_similarity = _calculate_name_similarity(sender_name, customer_name)
                if name_similarity > 0.5:  # Threshold for name matching
                    amount_match = abs(amount - _as_decimal(order.pending_sum)) < Decimal("0.01")
                    amount_diff = amount - _as_decimal(order.pending_sum)
                    currency_match = tx_currency == _currency_code(order.event.currency)
                    is_eur = tx_currency == 'EUR'
                    
                    confidence = name_similarity * 0.6  # Max 0.6 for name-only matches
                    if amount_match and currency_match and is_eur:
                        confidence = min(0.8, name_similarity * 0.7 + 0.3)
                    elif amount_match:
                        confidence = min(0.75, name_similarity * 0.65 + 0.25)
                    
                    # Lower confidence if not EUR
                    if not is_eur:
                        confidence = max(0.5, confidence - 0.1)
                    
                    match_reason = f"Sender name '{sender_name}' matches customer name '{customer_name}' (similarity: {name_similarity:.2f})"
                    if not currency_match:
                        match_reason += f" (currency mismatch: {trans.currency} vs {order.event.currency})"
                    if not is_eur:
                        match_reason += f" (non-EUR currency: {trans.currency})"
                    
                    suggestions.append({
                        'order': order,
                        'match_type': TransactionMatchSuggestion.MATCH_TYPE_SENDER_NAME,
                        'confidence_score': confidence,
                        'match_reason': match_reason,
                        'amount_match': amount_match,
                        'amount_difference': amount_diff,
                    })
    
    # Step 4: Amount-only match (fallback when no code or name matches)
    # Only create suggestions if no other matches were found
    if not suggestions:
        for order in pending_orders:
            currency_match = tx_currency == _currency_code(order.event.currency)
            amount_match = abs(amount - _as_decimal(order.pending_sum)) < Decimal("0.01")
            amount_diff = abs(amount - _as_decimal(order.pending_sum))
            is_eur = tx_currency == 'EUR'
            
            # Only suggest if currency matches and amount is close (within 10% or €10)
            threshold = max(abs(_as_decimal(order.pending_sum)) * Decimal("0.1"), Decimal("10.00"))
            if currency_match and amount_diff <= threshold:
                # Very low confidence for amount-only matches
                if amount_match and is_eur:
                    confidence = 0.4
                elif amount_match:
                    confidence = 0.35
                elif is_eur:
                    # Close amount match in EUR
                    confidence = 0.3
                else:
                    confidence = 0.25
                
                match_reason = f"Amount match only (no order code or name found). Amount: {trans.amount} {trans.currency}, Order pending: {order.pending_sum} {order.event.currency}"
                if not amount_match:
                    match_reason += f" (difference: {amount_diff:.2f})"
                
                suggestions.append({
                    'order': order,
                    'match_type': TransactionMatchSuggestion.MATCH_TYPE_AMOUNT_ONLY,
                    'confidence_score': confidence,
                    'match_reason': match_reason,
                    'amount_match': amount_match,
                    'amount_difference': amount_diff,
                })
    
    # Sort by confidence score (highest first)
    suggestions.sort(key=lambda x: x['confidence_score'], reverse=True)
    
    # Limit to top 10 suggestions to avoid overwhelming the review interface
    return suggestions[:10]


def match_transactions(connection: BankConnection):
    """Match unchecked transactions to orders using fuzzy logic."""
    transactions = BankTransaction.objects.filter(
        connection=connection,
        state=BankTransaction.STATE_UNCHECKED,
    )

    if not transactions.exists():
        return

    organizer = connection.organizer
    auto_confirm = organizer.settings.get('pretix_bank_sync_auto_confirm', True)
    matched = 0
    nomatch = 0
    review = 0
    errors = 0

    for trans in transactions.iterator(chunk_size=100):
        try:
            amount = _as_decimal(trans.amount)
            tx_currency = _currency_code(trans.currency)

            if amount == Decimal("0.00"):
                trans.state = BankTransaction.STATE_DISCARDED
                trans.save(update_fields=['state'])
                continue

            suggestions = _generate_match_suggestions(trans, organizer)

            if not suggestions:
                trans.state = BankTransaction.STATE_NOMATCH
                trans.save(update_fields=['state'])
                nomatch += 1
                continue

            best_suggestion = suggestions[0]
            is_eur = tx_currency == 'EUR'
            currency_match = tx_currency == _currency_code(best_suggestion['order'].event.currency)
            is_multi_order = "Multiple order codes found" in best_suggestion.get('match_reason', '')

            if is_multi_order and auto_confirm:
                multi_order_suggestions = [
                    s for s in suggestions
                    if "Multiple order codes found" in s.get('match_reason', '')
                ]

                if len(multi_order_suggestions) > 1:
                    orders = [s['order'] for s in multi_order_suggestions]
                    total_pending = sum((_as_decimal(o.pending_sum) for o in orders), Decimal("0"))
                    if (
                        abs(amount - total_pending) < Decimal("0.01")
                        and is_eur
                        and all(tx_currency == _currency_code(o.event.currency) for o in orders)
                        and best_suggestion['confidence_score'] >= 0.95
                    ):
                        trans.order = orders[0]
                        trans.state = BankTransaction.STATE_MATCHED
                        matches = [(o.event.slug, o.code) for o in orders]
                        _handle_transaction(trans, matches, {}, organizer, amount=amount)
                        matched += 1
                    else:
                        trans.state = BankTransaction.STATE_PENDING_APPROVAL
                        trans.save(update_fields=['state'])
                        TransactionMatchSuggestion.objects.filter(transaction=trans).delete()
                        for sug in suggestions[:10]:
                            TransactionMatchSuggestion.objects.create(
                                transaction=trans,
                                order=sug['order'],
                                match_type=sug['match_type'],
                                confidence_score=sug['confidence_score'],
                                match_reason=sug['match_reason'],
                                amount_match=sug['amount_match'],
                                amount_difference=_as_decimal(
                                    sug.get('amount_difference', Decimal('0'))
                                ),
                            )
                        review += 1
                else:
                    trans.state = BankTransaction.STATE_PENDING_APPROVAL
                    trans.save(update_fields=['state'])
                    TransactionMatchSuggestion.objects.filter(transaction=trans).delete()
                    for sug in suggestions[:10]:
                        TransactionMatchSuggestion.objects.create(
                            transaction=trans,
                            order=sug['order'],
                            match_type=sug['match_type'],
                            confidence_score=sug['confidence_score'],
                            match_reason=sug['match_reason'],
                            amount_match=sug['amount_match'],
                            amount_difference=_as_decimal(
                                sug.get('amount_difference', Decimal('0'))
                            ),
                        )
                    review += 1
            elif (
                auto_confirm
                and best_suggestion['match_type'] in (
                    TransactionMatchSuggestion.MATCH_TYPE_EXACT_CODE,
                    TransactionMatchSuggestion.MATCH_TYPE_EVENT_ORDER,
                )
                and best_suggestion['confidence_score'] >= 0.95
                and best_suggestion['amount_match']
                and is_eur
                and currency_match
            ):
                order = best_suggestion['order']
                trans.order = order
                trans.state = BankTransaction.STATE_MATCHED

                if amount < _as_decimal(order.pending_sum):
                    trans.is_partial_payment = True
                    trans.payment_group_id = f"{order.code}_{trans.date.isoformat()}"

                _handle_transaction(
                    trans,
                    [(order.event.slug, order.code)],
                    {},
                    organizer,
                    amount=amount,
                )
                matched += 1
            else:
                trans.state = BankTransaction.STATE_PENDING_APPROVAL
                trans.save(update_fields=['state'])
                TransactionMatchSuggestion.objects.filter(transaction=trans).delete()
                for sug in suggestions[:5]:
                    TransactionMatchSuggestion.objects.create(
                        transaction=trans,
                        order=sug['order'],
                        match_type=sug['match_type'],
                        confidence_score=sug['confidence_score'],
                        match_reason=sug['match_reason'],
                        amount_match=sug['amount_match'],
                        amount_difference=_as_decimal(sug['amount_difference']),
                    )
                review += 1
        except Exception:
            errors += 1
            logger.exception(
                "Error matching transaction %s (connection %s)",
                getattr(trans, 'pk', '?'),
                connection.pk,
            )
            try:
                if trans.state == BankTransaction.STATE_UNCHECKED:
                    trans.state = BankTransaction.STATE_ERROR
                    trans.save(update_fields=['state'])
            except Exception:
                logger.exception("Failed to mark transaction as error after match failure")

    logger.info(
        "Match connection %s: matched=%s review=%s nomatch=%s errors=%s",
        connection.pk,
        matched,
        review,
        nomatch,
        errors,
    )



@app.task(base=TransactionAwareTask, bind=True, max_retries=3, default_retry_delay=60)
def sync_bank_transactions(self, connection_id: int, force: bool = False, date_from: str = None):
    """
    Sync transactions from Enable Banking for a bank connection.

    Fetches only active BankAccount rows, uses last_fetch_date (or date_from)
    for incremental pulls, and respects the plugin rate-limit budget.
    Pass force=True for manual fetches (bypasses spacing interval, not the
    daily cap / 429 cooldown).
    """
    from datetime import date as date_cls

    with scope(organizer=None):
        try:
            connection = BankConnection.objects.get(pk=connection_id)
        except BankConnection.DoesNotExist:
            logger.error(f"Bank connection {connection_id} not found")
            return

    with scope(organizer=connection.organizer):
        if not connection.try_begin_sync(force=force):
            reason = connection.sync_block_reason(force=force)
            logger.warning(
                f"Cannot sync connection {connection_id}: {reason or 'limit reached or not active'}"
            )
            return

        organizer = connection.organizer
        app_id, private_key = get_enablebanking_credentials(organizer)

        if not app_id or not private_key:
            logger.error(f"Enable Banking credentials not configured for organizer {organizer.slug}")
            old_status = connection.status
            connection.status = BankConnection.STATUS_ERROR
            connection.last_error = "Enable Banking credentials not configured"
            connection.last_error_at = now()
            connection.save(update_fields=['status', 'last_error', 'last_error_at'])

            if old_status != BankConnection.STATUS_ERROR:
                cache_key = f'bank_connection_{connection_id}_status_error_sent'
                if not cache.get(cache_key):
                    try:
                        send_connection_notification(connection, 'needs_reauthorization')
                        cache.set(cache_key, True, timeout=30 * 24 * 60 * 60)
                    except Exception as e:
                        logger.error(f"Failed to send error notification for connection {connection_id}: {e}")
            return

        try:
            service = EnableBankingService(app_id=app_id, private_key_pem=private_key)

            accounts = list(connection.accounts.filter(is_active=True))
            if not accounts and connection.account_ids:
                # Fallback for connections created before BankAccount rows existed
                accounts = [
                    BankAccount(
                        connection=connection,
                        account_uid=uid,
                        is_active=True,
                    )
                    for uid in connection.account_ids
                    if uid
                ]

            if not accounts:
                logger.warning(f"No active accounts found for connection {connection_id}")
                return

            if connection.consent_expires_at:
                days_until_expiry = (connection.consent_expires_at - now()).days
                if 0 < days_until_expiry <= 7:
                    cache_key = f'bank_connection_{connection_id}_warning_sent'
                    last_warning = cache.get(cache_key)
                    should_send = not last_warning or (now() - last_warning).days >= 7
                    if should_send:
                        try:
                            send_connection_notification(connection, 'expiring_soon')
                            cache.set(cache_key, now(), timeout=7 * 24 * 60 * 60)
                        except Exception as e:
                            logger.error(f"Failed to send expiry warning for connection {connection_id}: {e}")

            override_from = None
            if date_from:
                try:
                    override_from = date_cls.fromisoformat(date_from)
                except ValueError:
                    logger.warning("Invalid date_from %s; ignoring", date_from)

            new_transactions = []
            fetched_any = False
            total_api = 0
            total_skipped_no_id = 0
            total_duplicates = 0
            total_errors = 0

            for account in accounts:
                account_uid = account.account_uid
                if not account_uid:
                    continue

                if override_from:
                    fetch_from = override_from
                elif getattr(account, 'last_fetch_date', None):
                    fetch_from = account.last_fetch_date
                else:
                    fetch_from = now().date() - timedelta(days=30)

                try:
                    transactions = service.get_transactions(account_uid, date_from=fetch_from)
                    fetched_any = True
                    total_api += len(transactions)
                    for tx_data in transactions:
                        try:
                            normalized = service.normalize_transaction(tx_data, account_uid)
                        except Exception:
                            total_errors += 1
                            logger.exception(
                                "Failed to normalize transaction for account %s",
                                account_uid,
                            )
                            continue

                        transaction_id = (normalized.get('transaction_id') or '').strip()
                        if not transaction_id:
                            total_skipped_no_id += 1
                            continue

                        if BankTransaction.objects.filter(transaction_id=transaction_id).exists():
                            total_duplicates += 1
                            continue

                        try:
                            trans = BankTransaction(connection=connection, **normalized)
                            trans.save()
                            new_transactions.append(trans)
                        except Exception:
                            total_errors += 1
                            logger.exception(
                                "Failed to save transaction %s for account %s",
                                transaction_id,
                                account_uid,
                            )
                            continue

                    if getattr(account, 'pk', None):
                        account.last_fetched = now()
                        account.last_fetch_date = now().date()
                        account.save(update_fields=['last_fetched', 'last_fetch_date'])

                except RateLimitExceeded as e:
                    until = now() + e.retry_after
                    connection.mark_rate_limited(
                        until,
                        message=f"Rate limit on transactions for account {account_uid}: {e}",
                    )
                    logger.warning(
                        f"Rate limited syncing connection {connection_id} "
                        f"account {account_uid}; paused until {until}"
                    )
                    if fetched_any:
                        connection.record_sync_success()
                    return
                except Exception as e:
                    logger.error(f"Error fetching transactions for account {account_uid}: {e}")
                    continue

            if fetched_any:
                connection.record_sync_success()
            else:
                logger.warning(f"No transaction fetches succeeded for connection {connection_id}")

            logger.info(
                "Sync connection %s: api=%s new=%s duplicates=%s skipped_no_id=%s errors=%s",
                connection_id,
                total_api,
                len(new_transactions),
                total_duplicates,
                total_skipped_no_id,
                total_errors,
            )

            if new_transactions:
                logger.info(f"Synced {len(new_transactions)} new transactions for connection {connection_id}")
                try:
                    match_transactions(connection)
                except Exception:
                    logger.exception(
                        "Matching failed after sync for connection %s; transactions are saved",
                        connection_id,
                    )

            if connection.consent_expires_at and connection.consent_expires_at < now():
                old_status = connection.status
                connection.status = BankConnection.STATUS_EXPIRED
                connection.save(update_fields=['status'])
                logger.warning(f"Connection {connection_id} consent has expired")

                if old_status != BankConnection.STATUS_EXPIRED:
                    cache_key = f'bank_connection_{connection_id}_status_expired_sent'
                    if not cache.get(cache_key):
                        try:
                            send_connection_notification(connection, 'needs_reauthorization')
                            cache.set(cache_key, True, timeout=30 * 24 * 60 * 60)
                        except Exception as e:
                            logger.error(f"Failed to send expiry notification for connection {connection_id}: {e}")

        except RateLimitExceeded as e:
            until = now() + e.retry_after
            connection.mark_rate_limited(until, message=str(e))
            logger.warning(f"Rate limited before sync for connection {connection_id}: {e}")
            return
        except Exception as e:
            logger.error(f"Error syncing transactions for connection {connection_id}: {e}")
            old_status = connection.status
            connection.status = BankConnection.STATUS_ERROR
            connection.last_error = str(e)
            connection.last_error_at = now()
            connection.save(update_fields=['status', 'last_error', 'last_error_at'])

            if old_status != BankConnection.STATUS_ERROR:
                cache_key = f'bank_connection_{connection_id}_status_error_sent'
                if not cache.get(cache_key):
                    try:
                        send_connection_notification(connection, 'needs_reauthorization')
                        cache.set(cache_key, True, timeout=30 * 24 * 60 * 60)
                    except Exception as e2:
                        logger.error(f"Failed to send error notification for connection {connection_id}: {e2}")
            raise


@app.task(base=TransactionAwareTask, bind=True)
def check_connection_expiry_warnings(self):
    """
    Periodic task to check for connections expiring soon and send warning emails.
    
    This runs independently of sync tasks to ensure warnings are sent even
    if sync doesn't run frequently.
    """
    with scope(organizer=None):
        # Get all active connections with expiration dates
        connections = BankConnection.objects.filter(
            status=BankConnection.STATUS_ACTIVE,
            consent_expires_at__isnull=False
        )
        
        for connection in connections:
            with scope(organizer=connection.organizer):
                # Check if expires within 7 days
                days_until_expiry = (connection.consent_expires_at - now()).days
                
                if 0 < days_until_expiry <= 7:
                    # Check if we need to send warning using cache
                    cache_key = f'bank_connection_{connection.id}_warning_sent'
                    last_warning = cache.get(cache_key)
                    should_send = not last_warning or (now() - last_warning).days >= 7
                    
                    if should_send:
                        try:
                            send_connection_notification(connection, 'expiring_soon')
                            # Store in cache for 7 days
                            cache.set(cache_key, now(), timeout=7 * 24 * 60 * 60)
                            logger.info(f"Sent expiry warning for connection {connection.id}")
                        except Exception as e:
                            logger.error(f"Failed to send expiry warning for connection {connection.id}: {e}")
