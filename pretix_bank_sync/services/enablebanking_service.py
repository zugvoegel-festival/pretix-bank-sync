"""
Enable Banking API client and transaction normalization.

JWT (RS256) auth with App ID + RSA private key. Ported/adapted from
nicoknoll/pretix-enablebanking, with our redaction + stable ID helpers.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional

import jwt as pyjwt
import requests
from django.utils.timezone import now

from .redact import mask_iban, redact_sensitive_payload

logger = logging.getLogger(__name__)


class EnableBankingService:
    BASE_URL = "https://api.enablebanking.com"

    def __init__(self, app_id: str, private_key_pem: str):
        self.app_id = app_id
        self.private_key_pem = private_key_pem

    def _create_jwt(self) -> str:
        iat = int(datetime.now(tz=timezone.utc).timestamp())
        payload = {
            "iss": "enablebanking.com",
            "aud": "api.enablebanking.com",
            "iat": iat,
            "exp": iat + 3600,
        }
        return pyjwt.encode(
            payload,
            self.private_key_pem,
            algorithm="RS256",
            headers={"kid": self.app_id},
        )

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._create_jwt()}"}

    def list_aspsps(self, country: Optional[str] = None) -> List[Dict]:
        params = {}
        if country:
            params["country"] = country
        resp = requests.get(
            f"{self.BASE_URL}/aspsps",
            params=params,
            headers=self._headers(),
            timeout=30,
        )
        if not resp.ok:
            logger.error(
                "Enable Banking GET /aspsps failed %s: %s",
                resp.status_code,
                resp.text[:300],
            )
        resp.raise_for_status()
        return resp.json().get("aspsps", [])

    def create_auth(
        self,
        aspsp_name: str,
        aspsp_country: str,
        redirect_url: str,
        maximum_consent_validity: Optional[int] = None,
    ) -> Dict:
        seconds = maximum_consent_validity if maximum_consent_validity else 90 * 24 * 3600
        valid_until = (
            datetime.now(tz=timezone.utc) + timedelta(seconds=seconds)
        ).strftime("%Y-%m-%dT%H:%M:%S.000000+00:00")

        body = {
            "access": {"valid_until": valid_until},
            "aspsp": {"name": aspsp_name, "country": aspsp_country},
            "state": str(uuid.uuid4()),
            "redirect_url": redirect_url,
            "psu_type": "personal",
        }
        resp = requests.post(
            f"{self.BASE_URL}/auth",
            json=body,
            headers=self._headers(),
            timeout=30,
        )
        if not resp.ok:
            logger.error(
                "Enable Banking POST /auth failed %s: %s",
                resp.status_code,
                resp.text[:300],
            )
        resp.raise_for_status()
        return resp.json()

    def create_session(self, code: str) -> Dict:
        resp = requests.post(
            f"{self.BASE_URL}/sessions",
            json={"code": code},
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def get_transactions(
        self,
        account_uid: str,
        date_from: Optional[date] = None,
    ) -> List[Dict]:
        """Return booked transactions (paginated via continuation_key)."""
        params: Dict[str, str] = {}
        if date_from:
            params["date_from"] = str(date_from)

        transactions: List[Dict] = []
        while True:
            resp = requests.get(
                f"{self.BASE_URL}/accounts/{account_uid}/transactions",
                params=params,
                headers=self._headers(),
                timeout=60,
            )
            if resp.status_code == 429:
                from .rate_limit import RateLimitExceeded, parse_reset_seconds

                raise RateLimitExceeded(
                    "Enable Banking rate limit exceeded",
                    reset_seconds=parse_reset_seconds(resp.headers),
                    scope="transactions",
                )
            resp.raise_for_status()
            data = resp.json()
            transactions.extend(data.get("transactions", []))
            continuation_key = data.get("continuation_key")
            if not continuation_key:
                break
            params["continuation_key"] = continuation_key

        return transactions

    @staticmethod
    def _as_text(value) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (list, tuple)):
            parts = [EnableBankingService._as_text(v) for v in value]
            return " ".join(p for p in parts if p)
        if isinstance(value, dict):
            return EnableBankingService._as_text(value.get("name") or value.get("iban") or "")
        return str(value).strip()

    @staticmethod
    def _parse_date(value) -> Optional[date]:
        text = EnableBankingService._as_text(value)
        if not text:
            return None
        if "T" in text:
            text = text.split("T", 1)[0]
        try:
            return datetime.strptime(text, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None

    def resolve_transaction_id(self, transaction_data: Dict, account_uid: str) -> str:
        bank_id = self._as_text(
            transaction_data.get("transaction_id")
            or transaction_data.get("transactionId")
        )
        if bank_id:
            return bank_id

        entry = self._as_text(
            transaction_data.get("entry_reference")
            or transaction_data.get("entryReference")
        )
        if entry:
            return f"{account_uid}:entry:{entry}"

        amount = (transaction_data.get("transaction_amount") or {}).get("amount", "")
        currency = (transaction_data.get("transaction_amount") or {}).get("currency", "")
        tx_date = (
            self._as_text(transaction_data.get("booking_date"))
            or self._as_text(transaction_data.get("value_date"))
        )
        remittance = self._as_text(transaction_data.get("remittance_information"))
        blob = f"{account_uid}|{tx_date}|{amount}|{currency}|{remittance}"
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]
        return f"{account_uid}:hash:{digest}"

    def normalize_transaction(self, transaction_data: Dict, account_uid: str) -> Dict:
        """Map Enable Banking transaction JSON to BankTransaction field kwargs."""
        amount_obj = transaction_data.get("transaction_amount") or {}
        amount = Decimal(str(amount_obj.get("amount", "0") or "0"))
        currency = self._as_text(amount_obj.get("currency")) or "EUR"

        booking_date = self._parse_date(transaction_data.get("booking_date"))
        value_date = self._parse_date(transaction_data.get("value_date"))
        tx_date = booking_date or value_date or now().date()

        remittance = self._as_text(transaction_data.get("remittance_information"))
        ref_number = self._as_text(transaction_data.get("reference_number"))
        if ref_number and ref_number not in remittance:
            remittance = f"{remittance} {ref_number}".strip()

        debtor = transaction_data.get("debtor") or {}
        creditor = transaction_data.get("creditor") or {}
        debtor_account = transaction_data.get("debtor_account") or {}
        creditor_account = transaction_data.get("creditor_account") or {}

        debtor_name = self._as_text(debtor.get("name") if isinstance(debtor, dict) else debtor)
        creditor_name = self._as_text(
            creditor.get("name") if isinstance(creditor, dict) else creditor
        )
        debtor_iban = self._as_text(
            debtor_account.get("iban") if isinstance(debtor_account, dict) else ""
        )
        creditor_iban = self._as_text(
            creditor_account.get("iban") if isinstance(creditor_account, dict) else ""
        )

        return {
            "transaction_id": self.resolve_transaction_id(transaction_data, account_uid),
            "account_id": account_uid,
            "amount": amount,
            "currency": currency,
            "date": tx_date,
            "booking_date": booking_date,
            "remittance_information_unstructured": remittance,
            "remittance_information_structured": "",
            "reference": remittance,
            "debtor_name": debtor_name,
            "debtor_account_iban": mask_iban(debtor_iban),
            "creditor_name": creditor_name,
            "creditor_account_iban": mask_iban(creditor_iban),
            "raw_data": redact_sensitive_payload(transaction_data),
        }
