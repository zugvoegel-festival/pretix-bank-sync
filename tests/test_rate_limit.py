"""Unit tests for rate-limit helpers (no pretix required)."""
import unittest
from datetime import timedelta

from pretix_bank_sync.services.rate_limit import (
    MAX_SYNCS_PER_DAY,
    MIN_SYNC_INTERVAL,
    RateLimitExceeded,
    parse_reset_seconds,
    syncs_remaining,
)


class RateLimitHelpersTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(MAX_SYNCS_PER_DAY, 4)
        self.assertEqual(MIN_SYNC_INTERVAL, timedelta(hours=6))

    def test_syncs_remaining(self):
        self.assertEqual(syncs_remaining(0), 4)
        self.assertEqual(syncs_remaining(3), 1)
        self.assertEqual(syncs_remaining(4), 0)
        self.assertEqual(syncs_remaining(10), 0)
        self.assertEqual(syncs_remaining(-1), 4)

    def test_parse_reset_seconds_account_success(self):
        headers = {"HTTP_X_RATELIMIT_ACCOUNT_SUCCESS_RESET": "3600"}
        self.assertEqual(parse_reset_seconds(headers), 3600)

    def test_parse_reset_seconds_prefers_account_over_general(self):
        headers = {
            "X-RateLimit-Account-Success-Reset": "120",
            "X-RateLimit-Reset": "999",
        }
        self.assertEqual(parse_reset_seconds(headers), 120)

    def test_parse_reset_seconds_retry_after(self):
        self.assertEqual(parse_reset_seconds({"Retry-After": "45"}), 45)

    def test_parse_reset_seconds_invalid(self):
        self.assertIsNone(parse_reset_seconds({"Retry-After": "soon"}))
        self.assertIsNone(parse_reset_seconds(None))
        self.assertIsNone(parse_reset_seconds({}))

    def test_rate_limit_exceeded_retry_after(self):
        exc = RateLimitExceeded("slow down", reset_seconds=90, scope="transactions")
        self.assertEqual(exc.retry_after, timedelta(seconds=90))
        self.assertEqual(exc.scope, "transactions")

    def test_rate_limit_exceeded_default_retry(self):
        exc = RateLimitExceeded("slow down")
        self.assertEqual(exc.retry_after, timedelta(hours=24))


if __name__ == "__main__":
    unittest.main()
