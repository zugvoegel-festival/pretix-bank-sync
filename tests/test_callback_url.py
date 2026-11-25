"""Callback URL must live outside /control/ (no PermissionMiddleware)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URLS = (ROOT / "pretix_bank_sync" / "urls.py").read_text(encoding="utf-8")


def test_callback_route_is_outside_control():
    match = re.search(
        r"re_path\(\s*r'\^([^']+)'\s*,\s*BankSyncCallbackView",
        URLS,
    )
    assert match, "BankSyncCallbackView route not found in urls.py"
    pattern = match.group(1)
    assert not pattern.startswith("control/"), pattern
    assert pattern.startswith("_bank_sync/"), pattern


def test_legacy_control_callback_route_removed():
    assert "bank-sync/callback" not in URLS
