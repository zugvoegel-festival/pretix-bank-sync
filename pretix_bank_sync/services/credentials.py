"""
Secure handling of GoCardless API credentials.

The Secret Key is encrypted at rest with Fernet, keyed from Django's SECRET_KEY.
If Fernet is unavailable, the value is stored as plaintext (same as pretix
payment-provider secrets) rather than failing the request with HTTP 500.
Legacy plaintext values remain readable and are re-encrypted on the next save
when Fernet works.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from typing import Optional, Tuple

from django.conf import settings

logger = logging.getLogger(__name__)

ENC_PREFIX = "enc:v1:"
CLIENT_ID_KEY = "pretix_bank_sync_gocardless_client_id"
CLIENT_SECRET_KEY = "pretix_bank_sync_gocardless_client_secret"
REDIRECT_URI_KEY = "pretix_bank_sync_gocardless_redirect_uri"


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
        # Never fail the HTTP request because of crypto — fall back to plaintext.
        logger.exception(
            "Fernet encrypt failed; storing GoCardless secret in plaintext"
        )
        return plaintext


def decrypt_secret(stored: str) -> str:
    if not stored:
        return ""
    if not stored.startswith(ENC_PREFIX):
        # Legacy / fallback plaintext
        return stored
    try:
        return _fernet().decrypt(stored[len(ENC_PREFIX) :].encode("ascii")).decode("utf-8")
    except Exception:
        logger.exception("Failed to decrypt GoCardless secret")
        return ""


def get_gocardless_credentials(organizer) -> Tuple[str, str, str]:
    """Return (client_id, client_secret_plaintext, redirect_uri)."""
    client_id = organizer.settings.get(CLIENT_ID_KEY, "") or ""
    client_secret = decrypt_secret(organizer.settings.get(CLIENT_SECRET_KEY, "") or "")
    redirect_uri = organizer.settings.get(REDIRECT_URI_KEY, "") or ""
    return str(client_id), str(client_secret), str(redirect_uri)


def has_gocardless_credentials(organizer) -> bool:
    client_id, client_secret, _redirect = get_gocardless_credentials(organizer)
    return bool(client_id and client_secret)


def set_gocardless_credentials(
    organizer,
    client_id: str,
    client_secret: Optional[str] = None,
    redirect_uri: Optional[str] = None,
):
    """
    Persist credentials. Pass client_secret=None to leave the existing secret unchanged.
    Empty string for client_secret clears it.
    """
    organizer.settings.set(CLIENT_ID_KEY, (client_id or "").strip())
    if client_secret is not None:
        secret = client_secret.strip()
        organizer.settings.set(CLIENT_SECRET_KEY, encrypt_secret(secret) if secret else "")
    if redirect_uri is not None:
        organizer.settings.set(REDIRECT_URI_KEY, (redirect_uri or "").strip())


def secret_is_configured(organizer) -> bool:
    stored = organizer.settings.get(CLIENT_SECRET_KEY, "") or ""
    return bool(decrypt_secret(stored))
