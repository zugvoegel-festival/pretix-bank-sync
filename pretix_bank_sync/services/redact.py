"""
Redaction helpers for IBANs and other sensitive banking fields.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Dict


IBAN_KEYS = {
    "iban",
    "bban",
    "account",
    "accountnumber",
    "account_number",
    "debtoraccount",
    "creditoraccount",
}


def mask_iban(iban: str) -> str:
    """Mask an IBAN, keeping country code + last 4 characters."""
    if not iban:
        return ""
    cleaned = re.sub(r"\s+", "", str(iban)).upper()
    if len(cleaned) <= 8:
        return "••••"
    return f"{cleaned[:4]}••••{cleaned[-4:]}"


def redact_value_for_key(key: str, value: Any) -> Any:
    key_l = (key or "").lower().replace("_", "")
    if key_l in IBAN_KEYS or key_l.endswith("iban"):
        if isinstance(value, str):
            return mask_iban(value)
        if isinstance(value, dict) and "iban" in value:
            redacted = copy.deepcopy(value)
            redacted["iban"] = mask_iban(value.get("iban", ""))
            return redacted
    return value


def redact_sensitive_payload(data: Any) -> Any:
    """
    Deep-copy and redact IBAN-like fields from API payloads before persistence.
    """
    if isinstance(data, dict):
        out: Dict[str, Any] = {}
        for k, v in data.items():
            key_l = k.lower().replace("_", "")
            if key_l in IBAN_KEYS or key_l.endswith("iban"):
                out[k] = redact_value_for_key(k, v)
            else:
                out[k] = redact_sensitive_payload(v)
        return out
    if isinstance(data, list):
        return [redact_sensitive_payload(item) for item in data]
    return data
