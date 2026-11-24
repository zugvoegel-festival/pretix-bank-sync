"""
GoCardless Bank Account Data rate-limit policy.

Banks (via GoCardless) typically allow as few as 4 successful requests per
day per account access scope (details / balances / transactions). This module
centralizes the plugin's sync budget so we stay within that ceiling.

Policy
------
- Hard cap: ``MAX_SYNCS_PER_DAY`` successful transaction syncs per connection.
- Spacing: periodic syncs must be at least ``MIN_SYNC_INTERVAL`` apart so the
  daily budget is not burned in the first minutes after midnight.
- Manual ("Fetch now") syncs may bypass the spacing interval but not the daily
  cap or an active ``rate_limited_until`` window from a prior HTTP 429.
- Sync tasks must use a lean path (requisition → account IDs → transactions)
  and must not call details/balances endpoints on every sync.
- Connections UI must not live-fetch account details on every page load.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

# Bank / GoCardless default: 4 successful account-scope calls per day.
MAX_SYNCS_PER_DAY = 4

# Spread the daily budget (4 × 6h ≈ 24h). Periodic tasks skip earlier.
MIN_SYNC_INTERVAL = timedelta(hours=6)

# Cache GoCardless access tokens briefly to avoid hammering /token/new/.
TOKEN_CACHE_SECONDS = 50 * 60  # tokens are typically valid ~24h; refresh early


class RateLimitExceeded(Exception):
    """Raised when GoCardless returns HTTP 429 or account success limit is hit."""

    def __init__(
        self,
        message: str,
        *,
        reset_seconds: Optional[int] = None,
        scope: Optional[str] = None,
    ):
        super().__init__(message)
        self.reset_seconds = reset_seconds
        self.scope = scope

    @property
    def retry_after(self) -> timedelta:
        seconds = self.reset_seconds if self.reset_seconds is not None else 24 * 60 * 60
        return timedelta(seconds=max(1, int(seconds)))


def parse_reset_seconds(headers) -> Optional[int]:
    """
    Extract reset wait from GoCardless rate-limit response headers.

    Prefers account-success reset, then general reset. Values are seconds
    remaining in the current window.
    """
    if not headers:
        return None
    for key in (
        "HTTP_X_RATELIMIT_ACCOUNT_SUCCESS_RESET",
        "X-RateLimit-Account-Success-Reset",
        "x-ratelimit-account-success-reset",
        "HTTP_X_RATELIMIT_RESET",
        "X-RateLimit-Reset",
        "x-ratelimit-reset",
        "Retry-After",
        "retry-after",
    ):
        raw = headers.get(key)
        if raw is None:
            continue
        try:
            return max(0, int(float(raw)))
        except (TypeError, ValueError):
            continue
    return None


def syncs_remaining(sync_count_today: int) -> int:
    return max(0, MAX_SYNCS_PER_DAY - max(0, int(sync_count_today)))
