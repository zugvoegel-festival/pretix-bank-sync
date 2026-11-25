"""Primary callback is outside /control/; legacy control path remains for old requisitions."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URLS = (ROOT / "pretix_bank_sync" / "urls.py").read_text(encoding="utf-8")
APPS = (ROOT / "pretix_bank_sync" / "apps.py").read_text(encoding="utf-8")


def test_primary_callback_route_is_outside_control():
    match = re.search(
        r"re_path\(\s*r'\^([^']+)'\s*,\s*BankSyncCallbackView\.as_view\(\),\s*name='callback'",
        URLS,
        flags=re.DOTALL,
    )
    assert match, "named callback route not found"
    pattern = match.group(1)
    assert pattern.startswith("_bank_sync/"), pattern


def test_legacy_control_callback_route_kept():
    assert "bank-sync/callback$" in URLS
    assert "name='callback_legacy'" in URLS


def test_permission_middleware_patch_mentions_legacy_view():
    assert "callback_legacy" in APPS
    assert "PermissionMiddleware" in APPS
