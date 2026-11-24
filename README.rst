pretix Bank Sync Plugin
========================

This plugin integrates with GoCardless Bank Account Data (formerly Nordigen) to:

- Sync bank account transactions up to 4 times per day
- Automatically match transactions to orders/payments
- Manage bank connection lifecycle (setup, authorization, reauthorization)

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
4. Complete the bank authorization wizard

See ``INSTALLATION.md`` for detailed steps.

License
-------

Copyright 2024–2026 Zugvögel Festival / pretix contributors

Licensed under the Apache License, Version 2.0
