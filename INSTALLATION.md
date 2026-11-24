# Installation Instructions

## Development Mode Installation (Recommended)

If you're developing or testing the plugin locally:

1. **Activate your pretix virtual environment** (if you have one):
   ```bash
   source /path/to/pretix/venv/bin/activate
   ```

2. **Install the plugin in development mode**:
   ```bash
   cd /path/to/pretix-bank-sync
   pip install -e .
   ```

   This will:
   - Install the plugin and its dependency (`requests`)
   - Make the plugin discoverable by pretix
   - Allow you to edit the code without reinstalling

## Production Installation

```bash
pip install pretix-bank-sync
# or from a local checkout:
pip install .
```

## Enable the Plugin

1. **Run migrations**:
   ```bash
   python -m pretix migrate pretix_bank_sync
   ```

2. **Enable the plugin in pretix**:
   - Organizer settings → Plugins → enable **Bank Sync**
   - Restart the pretix web process and Celery workers

3. **Configure credentials**:
   - Organizer → Bank Sync → Settings (or the setup wizard)
   - Enter GoCardless Bank Account Data Secret ID and Secret Key
   - The Secret Key is encrypted at rest (Fernet via pretix `SECRET_KEY`) and never shown again; leave blank to keep the current key
   - Optional: set Redirect URI; otherwise the plugin callback URL is used
   - Rotating pretix `SECRET_KEY` will invalidate stored GoCardless secrets — re-enter them after a key rotation

4. **Connect a bank account**:
   - Organizer → Bank Sync → Set up connection
   - Select country and bank, create a requisition, authorize at the bank
   - After the bank redirect, the connection should show as **Active**

Sign up for Bank Account Data at https://bankaccountdata.gocardless.com/

## Dependencies

- `requests` (usually already installed with pretix)

## Troubleshooting

### Plugin not appearing in the list

- `pip list | grep pretix-bank-sync`
- Confirm entry point: `pretix_bank_sync.apps:PluginApp` in `pyproject.toml`
- Restart pretix web and Celery

### Callback fails after bank authorization

- The callback URL must be reachable without requiring a logged-in session
- Default path: `/control/organizer/<slug>/bank-sync/callback`

### Migration errors

```bash
python -m pretix migrate pretix_bank_sync
```

## Behaviour

1. One bank connection per organizer (multiple accounts under that consent OK)
2. Periodic sync runs up to 4 times per day per active connection
3. Prefer payment references `EVENTSLUG-ORDERCODE`; also matches order code / sender / amount
4. High-confidence EUR event+order or exact matches auto-confirm when `pretix_bank_sync_auto_confirm` is enabled (default)
5. Unclear matches: **Organizer → Bank Sync → Review queue** (approve / assign / discard); event view shows that event’s matches
6. Consent expiry: email (~7 days) to users with organizer settings permission; **Renew** available while active if ≤14 days left
