"""Unit tests for Enable Banking transaction normalization."""
from __future__ import annotations

from decimal import Decimal

import django
from django.conf import settings

if not settings.configured:
    settings.configure(
        SECRET_KEY="test-secret-key-for-bank-sync-eb",
        USE_TZ=True,
    )
    django.setup()

from pretix_bank_sync.services.enablebanking_service import EnableBankingService  # noqa: E402


def test_normalize_booked_transaction():
    service = EnableBankingService(app_id="app", private_key_pem="unused")
    raw = {
        "transaction_id": "tx-1",
        "booking_date": "2026-03-01",
        "transaction_amount": {"amount": "42.50", "currency": "EUR"},
        "remittance_information": ["FEST-ABC12", "Ticket"],
        "reference_number": "RF123",
        "debtor": {"name": "Ada Lovelace"},
        "debtor_account": {"iban": "DE89370400440532013000"},
    }
    normalized = service.normalize_transaction(raw, "acct-uid")
    assert normalized["transaction_id"] == "tx-1"
    assert normalized["amount"] == Decimal("42.50")
    assert normalized["currency"] == "EUR"
    assert "FEST-ABC12" in normalized["reference"]
    assert "RF123" in normalized["reference"]
    assert normalized["debtor_name"] == "Ada Lovelace"
    assert normalized["debtor_account_iban"] == "DE89••••3000"


def test_resolve_id_falls_back_to_hash():
    service = EnableBankingService(app_id="app", private_key_pem="unused")
    raw = {
        "booking_date": "2026-03-01",
        "transaction_amount": {"amount": "10.00", "currency": "EUR"},
        "remittance_information": ["hello"],
    }
    tid = service.resolve_transaction_id(raw, "acct-1")
    assert tid.startswith("acct-1:hash:")
