"""Unit tests for GoCardless credential encryption helpers."""
from __future__ import annotations

import django
from django.conf import settings

if not settings.configured:
    settings.configure(
        SECRET_KEY="test-secret-key-for-bank-sync-credentials",
        USE_TZ=True,
    )
    django.setup()

from pretix_bank_sync.services.credentials import (  # noqa: E402
    ENC_PREFIX,
    decrypt_secret,
    encrypt_secret,
)


def test_encrypt_decrypt_roundtrip():
    plain = "gc-secret-key-value"
    stored = encrypt_secret(plain)
    assert stored.startswith(ENC_PREFIX)
    assert stored != plain
    assert decrypt_secret(stored) == plain


def test_encrypt_is_idempotent_for_already_encrypted():
    once = encrypt_secret("abc")
    twice = encrypt_secret(once)
    assert once == twice


def test_legacy_plaintext_still_readable():
    assert decrypt_secret("legacy-plain") == "legacy-plain"


def test_empty_values():
    assert encrypt_secret("") == ""
    assert decrypt_secret("") == ""
