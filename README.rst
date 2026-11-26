pretix Bank Sync Plugin
========================

This plugin integrates with `Enable Banking <https://enablebanking.com/>`_
(PSD2 / Open Banking) to:

- Sync bank account transactions periodically (respecting bank API rate limits)
- Automatically match transactions to orders/payments
- Manage bank connection lifecycle (setup, authorization, reauthorization)
- Let you activate/deactivate individual accounts under one consent

Rate limiting: banks often allow only a small number of successful account-data
requests per day per access scope. The plugin spaces automatic syncs, caches
credentials, uses a lean sync path (transactions only), handles HTTP 429 with a
cooldown, and blocks overlapping syncs. Manual "Fetch now" may skip spacing but
still respects the daily cap and any active cooldown. You can optionally set a
start date for manual fetches.

Installation
------------

Install the plugin using pip::

    pip install pretix-bank-sync

Or in development mode from a checkout::

    pip install -e .

Then enable the plugin for your organizer in pretix control, run migrations,
and configure Enable Banking credentials in **Organizer → Bank Sync**.

Configuration
-------------

1. Create an application at https://enablebanking.com/cp/applications/
2. Generate an RSA key pair, register the public key, keep the private key (PEM)
3. Copy your Application ID and private key into the plugin settings / setup wizard
4. Register the plugin callback URL as the redirect URI, e.g.
   ``https://your.domain/control/organizer/ORGANIZER/bank-sync/callback``
5. Complete the bank authorization wizard and select which accounts to sync

For evaluation / personal use, Enable Banking's restricted production mode
allows linking your own accounts without a commercial contract. Public /
third-party account access requires a contract and KYB.

One bank connection per organizer (multiple accounts under that consent are OK).
Matching is organizer-wide; ask buyers to use ``EVENTSLUG-ORDERCODE`` as the
transfer reference. Unclear matches appear in **Organizer → Bank Sync → Review queue**.
Consent expiry warnings are emailed to users with organizer settings permission;
renew from the connections page while still active if expiry is within 14 days.

Sandbox: Enable Banking's mock ASPSP (e.g. "Aachener Bank" with demo credentials)
shows a sandbox warning in the control panel.

Security notes: the RSA private key is Fernet-encrypted using the pretix
``SECRET_KEY`` and is never re-displayed in the UI. IBANs are masked in stored
``raw_data`` and in the control panel. Bank Sync control pages require
organizer settings permission (event match review requires order change permission).
Disabling the plugin for an organizer clears credentials and connection data.

See ``INSTALLATION.md`` for detailed steps.

License
-------

Copyright 2024–2026 Zugvögel Festival / pretix contributors

Licensed under the Apache License, Version 2.0
