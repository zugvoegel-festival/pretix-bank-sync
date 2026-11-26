"""
Secure handling of Enable Banking API credentials.

The RSA private key is encrypted at rest with Fernet, keyed from Django's
SECRET_KEY. If Fernet is unavailable, the value is stored as plaintext rather
than failing the request with HTTP 500. Legacy plaintext values remain readable
and are re-encrypted on the next save when Fernet works.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from typing import Optional, Tuple

from django.conf import settings

logger = logging.getLogger(__name__)

ENC_PREFIX = "enc:v1:"
APP_ID_KEY = "pretix_bank_sync_enablebanking_app_id"
PRIVATE_KEY_KEY = "pretix_bank_sync_enablebanking_private_key"
COUNTRY_KEY = "pretix_bank_sync_enablebanking_country"
FETCH_INTERVAL_KEY = "pretix_bank_sync_enablebanking_fetch_interval"

# Legacy GoCardless keys (read-only for migration notice; no longer written)
LEGACY_CLIENT_ID_KEY = "pretix_bank_sync_gocardless_client_id"
LEGACY_CLIENT_SECRET_KEY = "pretix_bank_sync_gocardless_client_secret"
LEGACY_REDIRECT_URI_KEY = "pretix_bank_sync_gocardless_redirect_uri"


def _secret_key_material() -> bytes:
    key = settings.SECRET_KEY
    if isinstance(key, bytes):
        return key
    return str(key).encode("utf-8")


def _fernet():
    from cryptography.fernet import Fernet

    digest = hashlib.sha256(_secret_key_material()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    if plaintext.startswith(ENC_PREFIX):
        return plaintext
    try:
        token = _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
        return ENC_PREFIX + token
    except Exception:
        logger.exception(
            "Fernet encrypt failed; storing Enable Banking private key in plaintext"
        )
        return plaintext


def decrypt_secret(stored: str) -> str:
    if not stored:
        return ""
    if not stored.startswith(ENC_PREFIX):
        return stored
    try:
        return _fernet().decrypt(stored[len(ENC_PREFIX):].encode("ascii")).decode("utf-8")
    except Exception:
        logger.exception("Failed to decrypt Enable Banking private key")
        return ""


def get_enablebanking_credentials(organizer) -> Tuple[str, str]:
    """Return (app_id, private_key_plaintext)."""
    app_id = organizer.settings.get(APP_ID_KEY, "") or ""
    private_key = decrypt_secret(organizer.settings.get(PRIVATE_KEY_KEY, "") or "")
    return str(app_id), str(private_key)


def has_enablebanking_credentials(organizer) -> bool:
    app_id, private_key = get_enablebanking_credentials(organizer)
    return bool(app_id and private_key)


def set_enablebanking_credentials(
    organizer,
    app_id: str,
    private_key: Optional[str] = None,
    country: Optional[str] = None,
):
    """
    Persist credentials. Pass private_key=None to leave the existing key unchanged.
    Empty string for private_key clears it.
    """
    organizer.settings.set(APP_ID_KEY, (app_id or "").strip())
    if private_key is not None:
        key = private_key.strip()
        organizer.settings.set(PRIVATE_KEY_KEY, encrypt_secret(key) if key else "")
    if country is not None:
        organizer.settings.set(COUNTRY_KEY, (country or "DE").strip() or "DE")


def private_key_is_configured(organizer) -> bool:
    stored = organizer.settings.get(PRIVATE_KEY_KEY, "") or ""
    return bool(decrypt_secret(stored))


def get_default_country(organizer) -> str:
    return organizer.settings.get(COUNTRY_KEY, "DE") or "DE"


def clear_enablebanking_settings(organizer):
    for key in (APP_ID_KEY, PRIVATE_KEY_KEY, COUNTRY_KEY, FETCH_INTERVAL_KEY):
        organizer.settings.delete(key)
