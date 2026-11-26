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
   - Enter Enable Banking Application ID and RSA private key (PEM)
   - The private key is encrypted at rest (Fernet via pretix `SECRET_KEY`) and never shown again; leave blank to keep the current key
   - Register the plugin callback URL as redirect URI in the Enable Banking control panel
   - Rotating pretix `SECRET_KEY` will invalidate stored private keys — re-enter them after a key rotation

4. **Connect a bank account**:
   - Organizer → Bank Sync → Connect bank account
   - Select country and bank, authorize at the bank
   - After the bank redirect, select which accounts to sync
   - The connection should show as **Active**

Sign up at https://enablebanking.com/cp/applications/

### Sandbox testing

Enable Banking provides a mock bank. When connecting **Aachener Bank** (country: **DE**), use:

| Field | Value |
|---|---|
| VR NetKey | `VRK1234567890ALL` |
| PIN | `password` |
| OTP | `123456` |

## Dependencies

- `requests`
- `cryptography` (Fernet for private-key encryption)
- `PyJWT` (RS256 JWT for Enable Banking API auth; usually already present via pretix)

## Troubleshooting

### Plugin not appearing in the list

- `pip list | grep pretix-bank-sync`
- Confirm entry point module is the app package (`pretix_bank_sync:…`) in `pyproject.toml`
- Restart pretix web and Celery

### Callback fails after bank authorization

- The callback URL must be reachable without requiring a logged-in session
- Default path: `/control/organizer/<slug>/bank-sync/callback`
- Must be registered as redirect URI for your Enable Banking application (HTTPS)

### Migration errors

```bash
python -m pretix migrate pretix_bank_sync
```

### Sync blocked / rate limited

Banks may limit successful account-data calls (often ~4/day per scope). Wait for the cooldown shown in the UI, or use a later manual fetch.
