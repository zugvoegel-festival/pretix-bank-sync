"""
GoCardless Bank Account Data API service.

Handles OAuth flow, API calls, and transaction fetching with rate-limit awareness.
"""
import hashlib
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional

import requests
from django.core.cache import cache
from django.utils.timezone import now

from .rate_limit import TOKEN_CACHE_SECONDS, RateLimitExceeded, parse_reset_seconds

logger = logging.getLogger(__name__)


class GoCardlessService:
    """
    Service for interacting with GoCardless Bank Account Data API.
    """

    # API endpoints
    BASE_URL = "https://bankaccountdata.gocardless.com/api/v2"

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        """
        Initialize GoCardless service.

        Args:
            client_id: GoCardless client ID
            client_secret: GoCardless client secret
            redirect_uri: OAuth redirect URI
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.base_url = self.BASE_URL
        self._access_token: Optional[str] = None

    def _token_cache_key(self) -> str:
        return f"pretix_bank_sync:gc_token:{self.client_id}"

    def _get_access_token(self) -> Optional[str]:
        """
        Get access token using client credentials (cached).

        Returns:
            Access token or None if authentication fails
        """
        if self._access_token:
            return self._access_token

        cached = cache.get(self._token_cache_key())
        if cached:
            self._access_token = cached
            return cached

        url = f"{self.base_url}/token/new/"
        data = {
            "secret_id": self.client_id,
            "secret_key": self.client_secret,
        }

        try:
            # Keep below gunicorn's default 30s worker timeout so a hung API
            # returns an error page instead of an opaque worker-kill 500.
            response = requests.post(url, json=data, timeout=12)
            response.raise_for_status()
            token_data = response.json()
            access_token = token_data.get("access")
            if not access_token:
                logger.error("GoCardless API returned no access token in response")
                return None
            self._access_token = access_token
            # Prefer API-reported expiry when present; otherwise use conservative TTL.
            expires_in = token_data.get("access_expires") or token_data.get("expires_in")
            try:
                ttl = min(TOKEN_CACHE_SECONDS, max(60, int(expires_in) - 60)) if expires_in else TOKEN_CACHE_SECONDS
            except (TypeError, ValueError):
                ttl = TOKEN_CACHE_SECONDS
            cache.set(self._token_cache_key(), access_token, timeout=ttl)
            return access_token
        except requests.HTTPError as e:
            error_detail = ""
            try:
                error_data = response.json()
                error_detail = error_data.get("detail", error_data.get("error", ""))
            except Exception:
                error_detail = response.text[:200] if hasattr(response, 'text') else str(e)
            logger.error(f"Failed to get GoCardless access token (HTTP {response.status_code}): {error_detail}")
            return None
        except requests.RequestException as e:
            logger.error(f"Failed to get GoCardless access token: {e}")
            return None

    def _raise_for_rate_limit(self, response: requests.Response, scope: Optional[str] = None):
        if response.status_code != 429:
            return
        reset_seconds = parse_reset_seconds(response.headers)
        detail = ""
        try:
            payload = response.json()
            detail = payload.get("detail") or payload.get("summary") or ""
        except Exception:
            detail = response.text[:300] if response.text else "Rate limit exceeded"
        raise RateLimitExceeded(
            detail or "Rate limit exceeded",
            reset_seconds=reset_seconds,
            scope=scope,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params=None,
        json_body=None,
        auth: bool = True,
        scope: Optional[str] = None,
        timeout: int = 12,
    ) -> requests.Response:
        """Perform an API request with auth and 429 handling."""
        headers = {}
        if auth:
            access_token = self._get_access_token()
            if not access_token:
                raise Exception("Failed to authenticate with GoCardless")
            headers["Authorization"] = f"Bearer {access_token}"
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        url = f"{self.base_url}{path}"
        response = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
            timeout=timeout,
        )
        self._raise_for_rate_limit(response, scope=scope)
        response.raise_for_status()
        return response

    def list_institutions(self, country: Optional[str] = None) -> List[Dict]:
        """
        List available institutions (banks) for a country.

        Args:
            country: ISO country code (e.g., 'GB', 'DE', 'FR'). If None, returns all.

        Returns:
            List of institution data
        """
        params = {}
        if country:
            params["country"] = country

        try:
            response = self._request("GET", "/institutions/", params=params)
            data = response.json()
            # Handle both response formats: list directly or dict with "results" key
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                return data.get("results", [])
            else:
                logger.warning(f"Unexpected response format from institutions API: {type(data)}")
                return []
        except RateLimitExceeded:
            raise
        except requests.RequestException as e:
            logger.error(f"Failed to list institutions: {e}")
            raise Exception(f"Failed to list institutions: {e}")

    def create_requisition_link(
        self,
        redirect_url: str,
        institution_id: Optional[str] = None,
        agreement: Optional[str] = None
    ) -> Dict:
        """
        Create a requisition link for bank authorization.

        Args:
            redirect_url: URL to redirect to after authorization
            institution_id: Optional institution ID (if user already selected bank)
            agreement: Optional agreement ID for reauthorization

        Returns:
            Dict with 'id' (requisition_id) and 'link' (authorization URL)
        """
        data = {
            "redirect": redirect_url,
        }
        if institution_id:
            data["institution_id"] = institution_id
        if agreement:
            data["agreement"] = agreement

        try:
            response = self._request("POST", "/requisitions/", json_body=data)
            return response.json()
        except RateLimitExceeded:
            raise
        except requests.RequestException as e:
            logger.error(f"Failed to create requisition: {e}")
            raise Exception(f"Failed to create requisition: {e}")

    def get_requisition(self, requisition_id: str) -> Dict:
        """
        Get requisition details.

        Args:
            requisition_id: The requisition ID

        Returns:
            Requisition data
        """
        try:
            response = self._request("GET", f"/requisitions/{requisition_id}/")
            return response.json()
        except RateLimitExceeded:
            raise
        except requests.RequestException as e:
            logger.error(f"Failed to get requisition {requisition_id}: {e}")
            raise Exception(f"Failed to get requisition: {e}")

    def list_account_ids(self, requisition_id: str) -> List[str]:
        """
        Return account IDs for a requisition without calling the details endpoint.

        Prefer this for periodic sync — account details have a separate daily
        bank rate-limit budget.
        """
        requisition = self.get_requisition(requisition_id)
        account_ids = requisition.get("accounts") or []
        return [str(aid) for aid in account_ids if aid]

    def get_agreement(self, agreement_id: str) -> Optional[Dict]:
        """Fetch an end-user agreement (includes access_valid_for_days)."""
        if not agreement_id:
            return None
        try:
            response = self._request("GET", f"/agreements/enduser/{agreement_id}/")
            return response.json()
        except RateLimitExceeded:
            raise
        except requests.RequestException as e:
            logger.warning(f"Failed to get agreement {agreement_id}: {e}")
            return None

    def consent_valid_days_from_requisition(self, requisition: Dict, default: int = 90) -> int:
        """Resolve access validity days from requisition → agreement, else default."""
        agreement_id = requisition.get('agreement') or ''
        if isinstance(agreement_id, dict):
            days = agreement_id.get('access_valid_for_days')
            if days:
                return int(days)
            agreement_id = agreement_id.get('id') or ''
        agreement = self.get_agreement(agreement_id) if agreement_id else None
        if agreement and agreement.get('access_valid_for_days'):
            return int(agreement['access_valid_for_days'])
        return default

    def get_accounts(self, requisition_id: str) -> List[Dict]:
        """
        Get accounts for a requisition (includes details — costly for rate limits).

        Prefer ``list_account_ids`` for sync. Use this only for setup/wizard UX.
        """
        try:
            account_ids = self.list_account_ids(requisition_id)
            accounts = []
            for account_id in account_ids:
                account_data = self.get_account_details(account_id)
                if account_data:
                    accounts.append(account_data)
            return accounts
        except RateLimitExceeded:
            raise
        except requests.RequestException as e:
            logger.error(f"Failed to get accounts for requisition {requisition_id}: {e}")
            raise Exception(f"Failed to get accounts: {e}")

    def get_account_details(self, account_id: str) -> Optional[Dict]:
        """
        Get account details.

        Args:
            account_id: The account ID

        Returns:
            Account data or None
        """
        try:
            response = self._request(
                "GET",
                f"/accounts/{account_id}/",
                scope="details",
            )
            return response.json()
        except RateLimitExceeded:
            raise
        except requests.RequestException as e:
            logger.error(f"Failed to get account {account_id}: {e}")
            return None

    def get_balances(self, account_id: str) -> List[Dict]:
        """
        Get balances for an account.

        Args:
            account_id: The account ID

        Returns:
            List of balance data with fields like:
            - balanceAmount: Dict with 'amount' and 'currency'
            - balanceType: 'current', 'available', 'interimAvailable', etc.
            - referenceDate: Date of the balance
            - creditLimitIncluded: Boolean indicating if credit limit is included
        """
        try:
            response = self._request(
                "GET",
                f"/accounts/{account_id}/balances/",
                scope="balances",
            )
            data = response.json()
            return data.get("balances", [])
        except RateLimitExceeded:
            raise
        except requests.RequestException as e:
            logger.error(f"Failed to get balances for account {account_id}: {e}")
            raise Exception(f"Failed to get balances: {e}")

    def get_transactions(
        self,
        account_id: str,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        include_pending: bool = False,
    ) -> List[Dict]:
        """
        Get transactions for an account.

        Args:
            account_id: The account ID
            date_from: Start date (default: 90 days ago)
            date_to: End date (default: today)
            include_pending: Also return pending txs (may lack stable IDs)

        Returns:
            List of transaction data (booked first; pending tagged with _pending)
        """
        if date_from is None:
            date_from = now() - timedelta(days=90)
        if date_to is None:
            date_to = now()

        params = {
            "date_from": date_from.strftime("%Y-%m-%d"),
            "date_to": date_to.strftime("%Y-%m-%d"),
        }

        try:
            response = self._request(
                "GET",
                f"/accounts/{account_id}/transactions/",
                params=params,
                scope="transactions",
            )
            data = response.json()
            tx_block = data.get("transactions") or {}
            booked = list(tx_block.get("booked") or [])
            pending = list(tx_block.get("pending") or [])
            logger.info(
                "GoCardless account %s: %s booked, %s pending transactions",
                account_id,
                len(booked),
                len(pending),
            )
            if include_pending:
                for p in pending:
                    if isinstance(p, dict):
                        p = dict(p)
                        p["_pending"] = True
                        booked.append(p)
            return booked
        except RateLimitExceeded:
            raise
        except requests.RequestException as e:
            logger.error(f"Failed to get transactions for account {account_id}: {e}")
            raise Exception(f"Failed to get transactions: {e}")

    def get_consent_status(self, requisition_id: str) -> Dict:
        """
        Get consent status for a requisition.

        Args:
            requisition_id: The requisition ID

        Returns:
            Consent status data
        """
        requisition = self.get_requisition(requisition_id)
        return {
            "status": requisition.get("status"),
            "consent_id": requisition.get("consent_id"),
            "expires_at": requisition.get("expires_at"),
        }

    @staticmethod
    def _as_text(value) -> str:
        """Coerce GoCardless remittance / ID fields that may be str, list, or blank."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (list, tuple)):
            parts = [GoCardlessService._as_text(v) for v in value]
            return " ".join(p for p in parts if p)
        return str(value).strip()

    @staticmethod
    def _parse_date(value) -> Optional[date]:
        text = GoCardlessService._as_text(value)
        if not text:
            return None
        # bookingDateTime / valueDateTime → take date part
        if "T" in text:
            text = text.split("T", 1)[0]
        try:
            return datetime.strptime(text, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None

    def resolve_transaction_id(self, transaction_data: Dict, account_id: str) -> str:
        """
        Stable unique id for a booked transaction.

        Many banks (esp. DE) omit ``transactionId``. Prefer bank id, then
        GoCardless ``internalTransactionId`` (scoped to account), then
        ``entryReference``, then a deterministic content hash.
        """
        bank_id = self._as_text(transaction_data.get("transactionId"))
        if bank_id:
            return bank_id

        internal = self._as_text(transaction_data.get("internalTransactionId"))
        if internal:
            # internalTransactionId is only unique within one account
            return f"{account_id}:gc:{internal}"

        entry = self._as_text(transaction_data.get("entryReference"))
        if entry:
            return f"{account_id}:entry:{entry}"

        # Last resort — avoid skipping the row entirely
        amount = (transaction_data.get("transactionAmount") or {}).get("amount", "")
        currency = (transaction_data.get("transactionAmount") or {}).get("currency", "")
        date = (
            self._as_text(transaction_data.get("bookingDate"))
            or self._as_text(transaction_data.get("valueDate"))
        )
        remittance = self._as_text(
            transaction_data.get("remittanceInformationUnstructured")
        ) or self._as_text(transaction_data.get("remittanceInformationStructured"))
        blob = f"{account_id}|{date}|{amount}|{currency}|{remittance}"
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]
        return f"{account_id}:hash:{digest}"

    def normalize_transaction(self, transaction_data: Dict, account_id: str) -> Dict:
        """
        Normalize transaction data from GoCardless format.

        Args:
            transaction_data: Raw transaction data from GoCardless
            account_id: Account ID

        Returns:
            Normalized transaction data
        """
        # Extract amount
        amount_obj = transaction_data.get("transactionAmount") or {}
        amount_str = amount_obj.get("amount", "0") or "0"
        amount = Decimal(str(amount_str))

        # Extract currency
        currency = self._as_text(amount_obj.get("currency"))

        booking_date = self._parse_date(transaction_data.get("bookingDate"))
        value_date = self._parse_date(transaction_data.get("valueDate"))
        # date is required on the model — never leave it None
        date = booking_date or value_date or now().date()

        remittance_unstructured = self._as_text(
            transaction_data.get("remittanceInformationUnstructured")
        ) or self._as_text(transaction_data.get("remittanceInformationUnstructuredArray"))
        remittance_structured = self._as_text(
            transaction_data.get("remittanceInformationStructured")
        ) or self._as_text(transaction_data.get("remittanceInformationStructuredArray"))
        reference = " ".join(
            p for p in (remittance_structured, remittance_unstructured) if p
        )

        from .redact import mask_iban, redact_sensitive_payload

        debtor_account = transaction_data.get("debtorAccount", {}) or {}
        creditor_account = transaction_data.get("creditorAccount", {}) or {}
        debtor_iban = debtor_account.get("iban", "") or ""
        creditor_iban = creditor_account.get("iban", "") or ""

        return {
            "transaction_id": self.resolve_transaction_id(transaction_data, account_id),
            "account_id": account_id,
            "amount": amount,
            "currency": currency,
            "date": date,
            "booking_date": booking_date,
            "remittance_information_unstructured": remittance_unstructured,
            "remittance_information_structured": remittance_structured,
            "reference": reference,
            "debtor_name": self._as_text(transaction_data.get("debtorName")),
            # Store masked IBANs only — full values stay at GoCardless
            "debtor_account_iban": mask_iban(debtor_iban),
            "creditor_name": self._as_text(transaction_data.get("creditorName")),
            "creditor_account_iban": mask_iban(creditor_iban),
            "raw_data": redact_sensitive_payload(transaction_data),
        }
