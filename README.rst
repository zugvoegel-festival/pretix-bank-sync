pretix Bank Sync Plugin
========================

This plugin integrates with GoCardless Bank Account Data (formerly Nordigen) to:

- Sync bank account transactions up to 4 times per day (bank API rate limit)
- Automatically match transactions to orders/payments
- Manage bank connection lifecycle (setup, authorization, reauthorization)

Rate limiting: banks often allow only ~4 successful account-data requests per day
per access scope. The plugin spaces automatic syncs (~6 hours apart), caches
access tokens and account IDs, uses a lean sync path (transactions only), handles
HTTP 429 with a cooldown, and blocks overlapping syncs. Manual "Fetch now" may
skip spacing but still respects the daily cap and any active cooldown.

Installation
------------

Install the plugin using pip::

    pip install pretix-bank-sync

Or in development mode from a checkout::

    pip install -e .

Then enable the plugin for your organizer in pretix control, run migrations,
and configure GoCardless credentials in **Organizer → Bank Sync**.

Configuration
-------------

1. Create a free Bank Account Data account at
   https://bankaccountdata.gocardless.com/
2. Copy your Secret ID and Secret Key into the plugin settings / setup wizard
3. Register the plugin callback URL as a redirect URI if required
   (``/_bank_sync/organizer/<slug>/callback``)
4. Complete the bank authorization wizard

One bank connection per organizer (multiple accounts under that consent are OK).
Matching is organizer-wide; ask buyers to use ``EVENTSLUG-ORDERCODE`` as the
transfer reference. Unclear matches appear in **Organizer → Bank Sync → Review queue**.
Consent expiry warnings are emailed to users with organizer settings permission;
renew from the connections page while still active if expiry is within 14 days.

Security notes: the GoCardless Secret Key is Fernet-encrypted using the pretix
``SECRET_KEY`` and is never re-displayed in the UI. IBANs are masked in stored
``raw_data`` and in the control panel. Bank Sync control pages require
organizer settings permission (event match review requires order change permission).

See ``INSTALLATION.md`` for detailed steps.

License
-------

Copyright 2024–2026 Zugvögel Festival / pretix contributors

Licensed under the Apache License, Version 2.0
