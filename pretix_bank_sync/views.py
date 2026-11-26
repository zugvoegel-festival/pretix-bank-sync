"""
Views for Bank Sync plugin.

Control panel views for setup, authorization, and transaction management.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView, View, ListView, DeleteView
from django.db.models import Count, Q

from pretix.base.models import Organizer, Event
from pretix.control.permissions import OrganizerPermissionRequiredMixin, EventPermissionRequiredMixin
from pretix.control.views import PaginationMixin
from pretix.helpers.urls import build_absolute_uri

from .models import BankAccount, BankConnection, BankTransaction, TransactionMatchSuggestion
from .services.enablebanking_service import EnableBankingService
from .services.credentials import (
    get_enablebanking_credentials,
    has_enablebanking_credentials,
    set_enablebanking_credentials,
    private_key_is_configured,
    get_default_country,
)
from .services.rate_limit import MAX_SYNCS_PER_DAY, RateLimitExceeded
from .services.redact import mask_iban
from .tasks import approve_match_suggestion, _handle_transaction

logger = logging.getLogger(__name__)

EU_COUNTRIES = [
    ('AT', _('Austria')),
    ('BE', _('Belgium')),
    ('BG', _('Bulgaria')),
    ('HR', _('Croatia')),
    ('CY', _('Cyprus')),
    ('CZ', _('Czech Republic')),
    ('DK', _('Denmark')),
    ('EE', _('Estonia')),
    ('FI', _('Finland')),
    ('FR', _('France')),
    ('DE', _('Germany')),
    ('GR', _('Greece')),
    ('HU', _('Hungary')),
    ('IS', _('Iceland')),
    ('IE', _('Ireland')),
    ('IT', _('Italy')),
    ('LV', _('Latvia')),
    ('LI', _('Liechtenstein')),
    ('LT', _('Lithuania')),
    ('LU', _('Luxembourg')),
    ('MT', _('Malta')),
    ('NL', _('Netherlands')),
    ('NO', _('Norway')),
    ('PL', _('Poland')),
    ('PT', _('Portugal')),
    ('RO', _('Romania')),
    ('SK', _('Slovakia')),
    ('SI', _('Slovenia')),
    ('ES', _('Spain')),
    ('SE', _('Sweden')),
]


def _normalize_aspsps(aspsps) -> list:
    """Reduce ASPSP payloads to {id, name, country, maximum_consent_validity}."""
    result = []
    for inst in aspsps or []:
        if not isinstance(inst, dict):
            continue
        name = inst.get('name') or ''
        country = inst.get('country') or ''
        if not name:
            continue
        mcv = inst.get('maximum_consent_validity')
        try:
            mcv = int(mcv) if mcv is not None else None
        except (TypeError, ValueError):
            mcv = None
        result.append({
            'id': f"{name}|{country}",
            'name': str(name),
            'country': str(country),
            'bic': '',
            'maximum_consent_validity': mcv,
        })
    result.sort(key=lambda x: x['name'].lower())
    return result


def _get_eb_service(organizer) -> EnableBankingService:
    app_id, private_key = get_enablebanking_credentials(organizer)
    if not app_id or not private_key:
        raise ValueError('Enable Banking credentials not configured')
    return EnableBankingService(app_id=app_id, private_key_pem=private_key)


def _fetch_aspsps_for_organizer(organizer, country: str) -> list:
    service = _get_eb_service(organizer)
    return _normalize_aspsps(service.list_aspsps(country=country))


def get_bank_setup_steps(current_step_key=None, connection=None):
    steps = [
        {
            'key': 'credentials',
            'label': _('Credentials'),
            'description': _('Configure Enable Banking App ID and private key'),
            'status': 'pending',
        },
        {
            'key': 'select_country',
            'label': _('Select Country'),
            'description': _('Choose your country'),
            'status': 'pending',
        },
        {
            'key': 'select_bank',
            'label': _('Select Bank'),
            'description': _('Choose your bank from the list'),
            'status': 'pending',
        },
        {
            'key': 'authorize',
            'label': _('Authorize'),
            'description': _('Authorize access to your bank account'),
            'status': 'pending',
        },
        {
            'key': 'verify',
            'label': _('Verify'),
            'description': _('Verify the connection and select accounts'),
            'status': 'pending',
        },
    ]

    if current_step_key:
        found_current = False
        for step in steps:
            if step['key'] == current_step_key:
                step['status'] = 'active'
                found_current = True
            elif not found_current:
                step['status'] = 'completed'
            else:
                step['status'] = 'pending'

    if connection and connection.status == BankConnection.STATUS_ACTIVE:
        for step in steps:
            if step['key'] in ('authorize', 'verify'):
                step['status'] = 'completed'

    return steps


class BankConnectionsListView(OrganizerPermissionRequiredMixin, ListView):
    permission = 'can_change_organizer_settings'
    template_name = 'pretix_bank_sync/control/connections_list.html'
    context_object_name = 'connections'
    model = BankConnection

    def get_queryset(self):
        organizer = self.request.organizer
        return BankConnection.objects.filter(
            organizer=organizer
        ).annotate(
            transaction_count=Count('transactions')
        ).order_by('-created')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        organizer = self.request.organizer
        has_credentials = has_enablebanking_credentials(organizer)
        connections = self.get_queryset()
        stats = {
            'total': connections.count(),
            'active': connections.filter(status=BankConnection.STATUS_ACTIVE).count(),
            'expired': connections.filter(status=BankConnection.STATUS_EXPIRED).count(),
            'pending': connections.filter(status=BankConnection.STATUS_PENDING).count(),
            'error': connections.filter(status=BankConnection.STATUS_ERROR).count(),
        }

        primary = connections.first()
        linked_accounts = []
        expiring_soon = False
        can_manual_sync = False
        sync_block_reason = ""
        sandbox_mode = False
        if primary:
            primary.reset_daily_counter()
            sandbox_mode = primary.is_sandbox()
            if primary.status == BankConnection.STATUS_ACTIVE:
                expiring_soon = primary.is_expiring_soon(within_days=7)
                can_manual_sync = primary.can_sync(force=True)
                sync_block_reason = primary.sync_block_reason(force=True)
            for account in primary.accounts.all():
                linked_accounts.append({
                    'id': account.account_uid,
                    'name': account.account_name,
                    'iban_masked': account.masked_iban,
                    'is_active': account.is_active,
                    'pk': account.pk,
                })

        pending_review = BankTransaction.objects.filter(
            connection__organizer=organizer,
            state__in=[
                BankTransaction.STATE_PENDING_APPROVAL,
                BankTransaction.STATE_NOMATCH,
                BankTransaction.STATE_UNCHECKED,
            ],
        ).count()

        ctx.update({
            'organizer': organizer,
            'has_credentials': has_credentials,
            'stats': stats,
            'now': now(),
            'primary_connection': primary,
            'has_connection': bool(primary),
            'linked_accounts': linked_accounts,
            'expiring_soon': expiring_soon,
            'pending_review_count': pending_review,
            'single_connection_mode': True,
            'can_manual_sync': can_manual_sync,
            'sync_block_reason': sync_block_reason,
            'max_syncs_per_day': MAX_SYNCS_PER_DAY,
            'sandbox_mode': sandbox_mode,
            'countries': EU_COUNTRIES,
        })
        return ctx

    def post(self, request, *args, **kwargs):
        """Handle account activation toggles from the connections list."""
        organizer = request.organizer
        action = request.POST.get('action')
        connection = BankConnection.objects.filter(organizer=organizer).first()
        if not connection:
            return redirect('plugins:pretix_bank_sync:connections_list', organizer=organizer.slug)

        if action == 'update_accounts':
            active_ids = set(request.POST.getlist('active_accounts'))
            accounts = list(connection.accounts.all())
            for account in accounts:
                account.is_active = str(account.pk) in active_ids
            BankAccount.objects.bulk_update(accounts, ['is_active'])
            messages.success(request, _('Account selection saved.'))
        return redirect('plugins:pretix_bank_sync:connections_list', organizer=organizer.slug)


class BankConnectionDeleteView(OrganizerPermissionRequiredMixin, DeleteView):
    permission = 'can_change_organizer_settings'
    model = BankConnection
    template_name = 'pretix_bank_sync/control/connection_delete.html'
    context_object_name = 'connection'

    def get_queryset(self):
        return BankConnection.objects.filter(organizer=self.request.organizer)

    def get_success_url(self):
        return reverse('plugins:pretix_bank_sync:connections_list', kwargs={
            'organizer': self.request.organizer.slug
        })

    def form_valid(self, form):
        messages.success(self.request, _('Bank connection deleted successfully.'))
        return super().form_valid(form)


class BankConnectionRenewView(OrganizerPermissionRequiredMixin, View):
    permission = 'can_change_organizer_settings'

    def post(self, request, *args, **kwargs):
        organizer = request.organizer
        connection_id = kwargs.get('connection_id')
        try:
            connection = BankConnection.objects.get(pk=connection_id, organizer=organizer)
        except BankConnection.DoesNotExist:
            messages.error(request, _('Connection not found.'))
            return redirect('plugins:pretix_bank_sync:connections_list', organizer=organizer.slug)

        if not has_enablebanking_credentials(organizer):
            messages.error(request, _('Enable Banking credentials not configured. Please configure them first.'))
            return redirect('plugins:pretix_bank_sync:settings', organizer=organizer.slug)

        if not connection.aspsp_name or not connection.aspsp_country:
            messages.error(request, _('Could not determine bank. Please create a new connection.'))
            return redirect('plugins:pretix_bank_sync:connections_list', organizer=organizer.slug)

        try:
            service = _get_eb_service(organizer)
            callback_url = build_absolute_uri(
                'plugins:pretix_bank_sync:callback',
                kwargs={'organizer': organizer.slug},
            )
            auth_data = service.create_auth(
                connection.aspsp_name,
                connection.aspsp_country,
                redirect_url=callback_url,
            )
            auth_url = auth_data.get('url', '')
            connection.status = BankConnection.STATUS_PENDING
            connection.auth_link = auth_url
            connection.session_id = ''
            connection.last_error = ''
            connection.save(update_fields=['status', 'auth_link', 'session_id', 'last_error'])
            request.session['eb_auth_link'] = auth_url
            messages.success(request, _('Reauthorization started. Please authorize the connection.'))
            return redirect(
                f"{reverse('plugins:pretix_bank_sync:bank_setup_wizard', kwargs={'organizer': organizer.slug})}?step=authorize"
            )
        except Exception as e:
            logger.exception("Error renewing Enable Banking connection")
            messages.error(request, _('An error occurred: {}').format(str(e)))
            return redirect('plugins:pretix_bank_sync:connections_list', organizer=organizer.slug)


class BankSetupWizardView(OrganizerPermissionRequiredMixin, TemplateView):
    permission = 'can_change_organizer_settings'
    template_name = 'pretix_bank_sync/control/bank_setup_wizard.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        organizer = self.request.organizer
        step = self.request.GET.get('step', 'credentials')
        connection_id = self.request.GET.get('connection_id')
        connection = None
        if connection_id:
            connection = BankConnection.objects.filter(pk=connection_id, organizer=organizer).first()

        steps = get_bank_setup_steps(current_step_key=step, connection=connection)
        current_step = next((s for s in steps if s['status'] == 'active'), steps[0])

        session_creds = self.request.session.get('wizard_credentials', {})
        stored_id, stored_key = get_enablebanking_credentials(organizer)
        app_id = session_creds.get('app_id', stored_id)
        has_key = private_key_is_configured(organizer)
        callback_url = build_absolute_uri(
            'plugins:pretix_bank_sync:callback',
            kwargs={'organizer': organizer.slug},
        )

        ctx.update({
            'organizer': organizer,
            'step': step,
            'steps': steps,
            'current_step': current_step,
            'connection': connection,
            'app_id': app_id,
            'client_id': app_id,  # template compat
            'callback_url': callback_url,
            'has_credentials': bool(app_id and has_key),
            'secret_configured': has_key,
            'client_secret': '',
            'private_key': '',
            'provider_name': 'Enable Banking',
            'countries': EU_COUNTRIES,
        })

        if step == 'credentials' and app_id and stored_key and self.request.GET.get('tested'):
            ctx['credentials_valid'] = True
            ctx['credentials_test_message'] = _('Credentials verified successfully. API connection working.')

        elif step == 'select_country':
            ctx['selected_country'] = self.request.session.get(
                'eb_selected_country', get_default_country(organizer)
            )

        elif step == 'select_bank':
            selected_country = self.request.session.get(
                'eb_selected_country', get_default_country(organizer)
            )
            ctx['selected_country'] = selected_country
            institutions = self.request.session.get('eb_aspsps', [])
            if selected_country and not institutions and app_id and has_key:
                try:
                    institutions = _fetch_aspsps_for_organizer(organizer, selected_country)
                    self.request.session['eb_aspsps'] = institutions
                except Exception:
                    logger.exception("Error loading banks on select_bank GET")
                    ctx['institutions_error'] = _(
                        'Could not load banks from Enable Banking. Go back and select the country again.'
                    )
            ctx['institutions'] = institutions
            ctx['selected_institution'] = self.request.session.get('eb_selected_aspsp', '')

        elif step == 'authorize':
            auth_link = self.request.session.get('eb_auth_link') or (
                connection.auth_link if connection else ''
            )
            ctx['has_auth_link'] = bool(auth_link)
            ctx['auth_link'] = auth_link

        elif step == 'verify':
            connection = connection or BankConnection.objects.filter(organizer=organizer).first()
            ctx['connection'] = connection
            if connection:
                ctx['accounts'] = list(connection.accounts.all())
                ctx['sandbox_mode'] = connection.is_sandbox()

        return ctx

    def post(self, request, *args, **kwargs):
        organizer = request.organizer
        step = request.POST.get('step', 'credentials')
        action = request.POST.get('action', '')
        wizard_url = reverse(
            'plugins:pretix_bank_sync:bank_setup_wizard',
            kwargs={'organizer': organizer.slug},
        )

        if step == 'credentials':
            app_id = request.POST.get('client_id', request.POST.get('app_id', '')).strip()
            private_key = request.POST.get('client_secret', request.POST.get('private_key', '')).strip()
            _aid, existing_key = get_enablebanking_credentials(organizer)
            key_for_use = private_key or existing_key

            if action == 'test':
                if not app_id or not key_for_use:
                    messages.error(
                        request,
                        _('Please enter Application ID and private key (or keep an existing key) to test.'),
                    )
                    return redirect(f"{wizard_url}?step=credentials")
                try:
                    service = EnableBankingService(app_id=app_id, private_key_pem=key_for_use)
                    service.list_aspsps(country='DE')
                    set_enablebanking_credentials(
                        organizer,
                        app_id=app_id,
                        private_key=private_key if private_key else None,
                    )
                    messages.success(
                        request,
                        _('Credentials test successful! Your credentials can connect to Enable Banking.'),
                    )
                except Exception as e:
                    messages.warning(request, _('Credentials test failed: {}').format(str(e)))
                    logger.exception("Error testing Enable Banking credentials")
                request.session['wizard_credentials'] = {'app_id': app_id}
                return redirect(f"{wizard_url}?step=credentials&tested=1")

            if not app_id:
                messages.error(request, _('Application ID is required.'))
                return redirect(f"{wizard_url}?step=credentials")
            if not key_for_use:
                messages.error(request, _('Private key is required.'))
                return redirect(f"{wizard_url}?step=credentials")

            try:
                set_enablebanking_credentials(
                    organizer,
                    app_id=app_id,
                    private_key=private_key if private_key else None,
                )
            except Exception:
                logger.exception("Failed to save Enable Banking credentials")
                messages.error(request, _('Could not save credentials. Please try again.'))
                return redirect(f"{wizard_url}?step=credentials")
            request.session.pop('wizard_credentials', None)
            messages.success(request, _('Credentials saved successfully.'))
            return redirect(f"{wizard_url}?step=select_country")

        if step == 'select_country':
            country = request.POST.get('country', '').strip()
            if not country:
                messages.error(request, _('Please select a country.'))
                return redirect(f"{wizard_url}?step=select_country")
            request.session['eb_selected_country'] = country
            set_enablebanking_credentials(organizer, app_id=get_enablebanking_credentials(organizer)[0], country=country)
            try:
                institutions = _fetch_aspsps_for_organizer(organizer, country)
                request.session['eb_aspsps'] = institutions
                messages.success(
                    request,
                    _('Country selected. {} banks found. Now choose your bank...').format(len(institutions)),
                )
            except Exception as e:
                logger.exception("Error loading banks for country")
                request.session.pop('eb_aspsps', None)
                messages.warning(request, _('Country selected, but failed to load banks: {}').format(str(e)))
            return redirect(f"{wizard_url}?step=select_bank")

        if step == 'select_bank':
            aspsp_id = request.POST.get('institution_id', '').strip()
            country = request.session.get('eb_selected_country', '')
            if not country:
                messages.error(request, _('Please select a country first.'))
                return redirect(f"{wizard_url}?step=select_country")
            if not aspsp_id:
                messages.error(request, _('Please select a bank.'))
                return redirect(f"{wizard_url}?step=select_bank")

            if '|' in aspsp_id:
                aspsp_name, aspsp_country = aspsp_id.split('|', 1)
            else:
                aspsp_name, aspsp_country = aspsp_id, country

            mcv_str = request.POST.get('maximum_consent_validity', '')
            maximum_consent_validity = int(mcv_str) if mcv_str.isdigit() else None
            # Prefer ASPSP metadata from session list
            for inst in request.session.get('eb_aspsps', []):
                if inst.get('id') == aspsp_id and inst.get('maximum_consent_validity'):
                    maximum_consent_validity = inst['maximum_consent_validity']
                    break

            try:
                service = _get_eb_service(organizer)
                callback_url = build_absolute_uri(
                    'plugins:pretix_bank_sync:callback',
                    kwargs={'organizer': organizer.slug},
                )
                auth_data = service.create_auth(
                    aspsp_name,
                    aspsp_country,
                    redirect_url=callback_url,
                    maximum_consent_validity=maximum_consent_validity,
                )
                auth_url = auth_data.get('url', '')
                if not auth_url:
                    messages.error(request, _('Failed to create bank authorization. Please try again.'))
                    return redirect(f"{wizard_url}?step=select_bank")

                connection, created = BankConnection.objects.get_or_create(
                    organizer=organizer,
                    defaults={
                        'requisition_id': None,
                        'status': BankConnection.STATUS_PENDING,
                        'aspsp_name': aspsp_name,
                        'aspsp_country': aspsp_country,
                        'auth_link': auth_url,
                    },
                )
                if not created:
                    connection.status = BankConnection.STATUS_PENDING
                    connection.aspsp_name = aspsp_name
                    connection.aspsp_country = aspsp_country
                    connection.auth_link = auth_url
                    connection.session_id = ''
                    connection.save(update_fields=[
                        'status', 'aspsp_name', 'aspsp_country', 'auth_link', 'session_id',
                    ])

                request.session['eb_selected_aspsp'] = aspsp_id
                request.session['eb_auth_link'] = auth_url
                messages.success(request, _('Authorization ready. Continue to your bank.'))
                return redirect(f"{wizard_url}?step=authorize")
            except Exception as e:
                logger.exception("Error creating Enable Banking auth")
                messages.error(request, _('An error occurred: {}').format(str(e)))
                return redirect(f"{wizard_url}?step=select_bank")

        if step == 'authorize':
            auth_link = request.session.get('eb_auth_link')
            if not auth_link:
                connection = BankConnection.objects.filter(organizer=organizer).first()
                auth_link = connection.auth_link if connection else ''
            if auth_link:
                return redirect(auth_link)
            messages.error(request, _('No authorization link found. Please start over.'))
            return redirect(f"{wizard_url}?step=select_bank")

        if step == 'verify':
            connection = BankConnection.objects.filter(organizer=organizer).first()
            if connection and action == 'update_accounts':
                active_ids = set(request.POST.getlist('active_accounts'))
                accounts = list(connection.accounts.all())
                for account in accounts:
                    account.is_active = str(account.pk) in active_ids
                BankAccount.objects.bulk_update(accounts, ['is_active'])
                messages.success(request, _('Account selection saved.'))
                return redirect('plugins:pretix_bank_sync:connections_list', organizer=organizer.slug)
            return redirect(f"{wizard_url}?step=verify")

        return redirect(wizard_url)


class BankSyncSettingsView(OrganizerPermissionRequiredMixin, TemplateView):
    permission = 'can_change_organizer_settings'
    template_name = 'pretix_bank_sync/control/settings.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        organizer = self.request.organizer
        connection = BankConnection.objects.filter(organizer=organizer).first()
        recent_transactions = []
        if connection:
            recent_transactions = BankTransaction.objects.filter(
                connection=connection
            ).order_by('-booking_date', '-created')[:10]

        app_id, _key = get_enablebanking_credentials(organizer)
        auto_confirm = organizer.settings.get('pretix_bank_sync_auto_confirm', True)
        from . import __version__ as plugin_version

        ctx.update({
            'organizer': organizer,
            'connection': connection,
            'accounts': list(connection.accounts.all()) if connection else [],
            'recent_transactions': recent_transactions,
            'enablebanking_app_id': app_id,
            'gocardless_client_id': app_id,  # template compat during rename
            'secret_configured': private_key_is_configured(organizer),
            'auto_confirm': auto_confirm,
            'active_tab': self.request.GET.get('tab', 'enablebanking'),
            'bank_sync_version': plugin_version,
            'provider_name': 'Enable Banking',
            'sandbox_mode': connection.is_sandbox() if connection else False,
            'default_country': get_default_country(organizer),
            'countries': EU_COUNTRIES,
        })
        return ctx

    def post(self, request, *args, **kwargs):
        organizer = request.organizer
        settings_url = (
            f"{reverse('plugins:pretix_bank_sync:settings', kwargs={'organizer': organizer.slug})}"
            f"?tab=enablebanking"
        )
        try:
            app_id = request.POST.get(
                'enablebanking_app_id',
                request.POST.get('gocardless_client_id', ''),
            ).strip()
            private_key = request.POST.get(
                'enablebanking_private_key',
                request.POST.get('gocardless_client_secret', ''),
            ).strip()
            country = request.POST.get('enablebanking_country', '').strip()
            auto_confirm = request.POST.get('auto_confirm') == 'on'

            set_enablebanking_credentials(
                organizer,
                app_id=app_id,
                private_key=private_key if private_key else None,
                country=country or None,
            )
            organizer.settings.set('pretix_bank_sync_auto_confirm', bool(auto_confirm))
            messages.success(request, _('Enable Banking settings saved successfully.'))
        except Exception:
            logger.exception("Failed to save Enable Banking credentials")
            messages.error(request, _('Could not save credentials. Please try again.'))
        return redirect(settings_url)


class BankSyncCallbackView(View):
    """
    Handle OAuth callback from Enable Banking.

    Intentionally unauthenticated: the bank redirect returns here without a
    pretix session. Connection is activated by authorization ``code``.
    """

    def get(self, request, *args, **kwargs):
        organizer = get_object_or_404(Organizer, slug=kwargs['organizer'])
        list_url = reverse(
            'plugins:pretix_bank_sync:connections_list',
            kwargs={'organizer': organizer.slug},
        )
        code = request.GET.get('code', '')
        if not code:
            messages.error(request, _('Invalid callback. Missing authorization code.'))
            return redirect(list_url)

        connection = BankConnection.objects.filter(
            organizer=organizer,
            status=BankConnection.STATUS_PENDING,
        ).first()
        if not connection:
            messages.error(request, _('No pending bank authorization found.'))
            return redirect(list_url)

        try:
            service = _get_eb_service(organizer)
            session = service.create_session(code)
        except Exception:
            logger.exception('Failed to create Enable Banking session after callback')
            connection.status = BankConnection.STATUS_ERROR
            connection.last_error = 'Failed to create bank session'
            connection.last_error_at = now()
            connection.save(update_fields=['status', 'last_error', 'last_error_at'])
            messages.error(request, _('Failed to create bank session. Please try again.'))
            return redirect(list_url)

        connection.session_id = session.get('session_id', '')
        connection.status = BankConnection.STATUS_ACTIVE
        connection.auth_link = ''
        connection.last_error = ''
        connection.rate_limited_until = None

        valid_until_str = (session.get('access') or {}).get('valid_until')
        if valid_until_str:
            connection.consent_expires_at = parse_datetime(valid_until_str) or (
                now() + timedelta(days=90)
            )
        else:
            connection.consent_expires_at = now() + timedelta(days=90)

        account_uids = []
        for acct in session.get('accounts', []):
            uid = acct.get('uid', '')
            iban = (acct.get('account_id') or {}).get('iban') or ''
            if not uid:
                continue
            # Skip accounts without IBAN (EB issue #3) but keep those with uid
            BankAccount.objects.update_or_create(
                connection=connection,
                account_uid=uid,
                defaults={
                    'account_name': ' – '.join(
                        filter(None, [acct.get('name', ''), acct.get('details', '')])
                    ),
                    'iban': iban,
                    'currency': acct.get('currency', 'EUR') or 'EUR',
                    'is_active': bool(iban),
                },
            )
            account_uids.append(uid)

        connection.account_ids = account_uids
        connection.save(update_fields=[
            'session_id', 'status', 'auth_link', 'last_error', 'rate_limited_until',
            'consent_expires_at', 'account_ids',
        ])

        request.session.pop('eb_auth_link', None)
        request.session.pop('eb_selected_aspsp', None)
        messages.success(request, _('Bank account connected successfully!'))
        return redirect(
            f"{reverse('plugins:pretix_bank_sync:bank_setup_wizard', kwargs={'organizer': organizer.slug})}?step=verify"
        )


class BankSyncFetchTransactionsView(OrganizerPermissionRequiredMixin, View):
    permission = 'can_change_organizer_settings'

    def post(self, request, *args, **kwargs):
        organizer = request.organizer
        connection = BankConnection.objects.filter(organizer=organizer).first()
        if not connection or connection.status != BankConnection.STATUS_ACTIVE:
            messages.error(request, _('No active bank connection found.'))
            return redirect('plugins:pretix_bank_sync:connections_list', organizer=organizer.slug)

        connection.reset_daily_counter()
        if not connection.can_sync(force=True):
            messages.warning(
                request,
                connection.sync_block_reason(force=True)
                or _('Sync is not available right now (rate limit).'),
            )
            return redirect('plugins:pretix_bank_sync:connections_list', organizer=organizer.slug)

        if not has_enablebanking_credentials(organizer):
            messages.error(request, _('Enable Banking credentials not configured.'))
            return redirect('plugins:pretix_bank_sync:settings', organizer=organizer.slug)

        date_from = request.POST.get('date_from', '').strip() or None
        try:
            from .tasks import sync_bank_transactions
            sync_bank_transactions(connection.id, force=True, date_from=date_from)
            connection.refresh_from_db()
            if connection.is_rate_limited():
                messages.warning(
                    request,
                    _('Bank API rate limit hit. Sync paused until {when}.').format(
                        when=connection.rate_limited_until
                    ),
                )
            else:
                remaining = connection.syncs_remaining_today()
                messages.success(
                    request,
                    _('Transaction sync completed. {remaining} of {limit} syncs remaining today.').format(
                        remaining=remaining,
                        limit=MAX_SYNCS_PER_DAY,
                    ),
                )
            return redirect('plugins:pretix_bank_sync:transactions', organizer=organizer.slug)
        except RateLimitExceeded as e:
            until = now() + e.retry_after
            connection.mark_rate_limited(until, message=str(e))
            messages.warning(
                request,
                _('Bank API rate limit exceeded. Try again after {when}.').format(when=until),
            )
            return redirect('plugins:pretix_bank_sync:connections_list', organizer=organizer.slug)
        except Exception as e:
            logger.exception("Error manually fetching transactions")
            messages.error(request, _('Error fetching transactions: {}').format(str(e)))
            return redirect('plugins:pretix_bank_sync:connections_list', organizer=organizer.slug)


class BankInstitutionsAjaxView(OrganizerPermissionRequiredMixin, View):
    permission = 'can_change_organizer_settings'

    def get(self, request, *args, **kwargs):
        organizer = request.organizer
        country = request.GET.get('country', '')
        query = request.GET.get('query', '').lower()

        if not has_enablebanking_credentials(organizer):
            return JsonResponse({'results': [], 'error': 'Credentials not configured'}, status=400)
        if not country:
            return JsonResponse({'results': [], 'error': 'Country required'}, status=400)

        try:
            institutions = _fetch_aspsps_for_organizer(organizer, country)
            results = []
            for inst in institutions:
                if query and query not in inst['name'].lower():
                    continue
                results.append({
                    'id': inst['id'],
                    'text': inst['name'],
                    'name': inst['name'],
                    'bic': '',
                    'maximum_consent_validity': inst.get('maximum_consent_validity'),
                })
            return JsonResponse({'results': results, 'pagination': {'more': False}})
        except Exception as e:
            logger.exception("Error fetching ASPSPs")
            return JsonResponse({'results': [], 'error': str(e)}, status=500)


class BankSyncTransactionsView(OrganizerPermissionRequiredMixin, PaginationMixin, ListView):
    """List synced transactions."""
    permission = 'can_change_organizer_settings'
    template_name = 'pretix_bank_sync/control/transactions.html'
    context_object_name = 'transactions'
    model = BankTransaction

    def get_queryset(self):
        organizer = self.request.organizer
        connection = BankConnection.objects.filter(organizer=organizer).first()
        
        if not connection:
            return BankTransaction.objects.none()
        
        qs = BankTransaction.objects.filter(connection=connection).select_related('order', 'payment')

        # Filter by state if provided
        state = self.request.GET.get('state')
        if state:
            qs = qs.filter(state=state)

        return qs.order_by('-booking_date', '-created')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        organizer = self.request.organizer
        connection = BankConnection.objects.filter(organizer=organizer).first()

        ctx.update({
            'organizer': organizer,
            'connection': connection,
            'state_filter': self.request.GET.get('state', ''),
        })
        return ctx


class OrganizerTransactionReviewView(OrganizerPermissionRequiredMixin, PaginationMixin, ListView):
    """Organizer-level queue for unclear / pending bank transactions."""
    permission = 'can_change_organizer_settings'
    template_name = 'pretix_bank_sync/control/review_queue.html'
    context_object_name = 'transactions'
    model = BankTransaction
    paginate_by = 50

    def get_queryset(self):
        organizer = self.request.organizer
        qs = BankTransaction.objects.filter(
            connection__organizer=organizer,
        ).select_related(
            'connection', 'order', 'order__event', 'payment'
        ).prefetch_related(
            'match_suggestions', 'match_suggestions__order', 'match_suggestions__order__event'
        )

        state = self.request.GET.get('state', 'needs_review')
        if state == 'needs_review':
            qs = qs.filter(state__in=[
                BankTransaction.STATE_PENDING_APPROVAL,
                BankTransaction.STATE_NOMATCH,
                BankTransaction.STATE_UNCHECKED,
            ])
        elif state:
            qs = qs.filter(state=state)

        return qs.order_by('-date', '-created')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        organizer = self.request.organizer
        base = BankTransaction.objects.filter(connection__organizer=organizer)
        ctx.update({
            'organizer': organizer,
            'state_filter': self.request.GET.get('state', 'needs_review'),
            'events': organizer.events.all().order_by('name'),
            'counts': {
                'needs_review': base.filter(state__in=[
                    BankTransaction.STATE_PENDING_APPROVAL,
                    BankTransaction.STATE_NOMATCH,
                    BankTransaction.STATE_UNCHECKED,
                ]).count(),
                'pending_approval': base.filter(state=BankTransaction.STATE_PENDING_APPROVAL).count(),
                'nomatch': base.filter(state=BankTransaction.STATE_NOMATCH).count(),
                'matched': base.filter(state=BankTransaction.STATE_MATCHED).count(),
                'discarded': base.filter(state=BankTransaction.STATE_DISCARDED).count(),
            },
        })
        return ctx


class OrganizerTransactionDiscardView(OrganizerPermissionRequiredMixin, View):
    permission = 'can_change_organizer_settings'

    def post(self, request, *args, **kwargs):
        organizer = request.organizer
        trans = get_object_or_404(
            BankTransaction,
            pk=kwargs['transaction_id'],
            connection__organizer=organizer,
        )
        trans.state = BankTransaction.STATE_DISCARDED
        trans.order = None
        trans.payment = None
        trans.error_message = ''
        trans.save(update_fields=['state', 'order', 'payment', 'error_message', 'updated'])
        TransactionMatchSuggestion.objects.filter(
            transaction=trans, is_approved__isnull=True
        ).update(is_approved=False, reviewed_at=now(), reviewed_by=request.user)
        messages.success(request, _('Transaction discarded.'))
        return redirect(request.POST.get('next') or reverse(
            'plugins:pretix_bank_sync:review_queue',
            kwargs={'organizer': organizer.slug},
        ))


class OrganizerTransactionAssignView(OrganizerPermissionRequiredMixin, View):
    """Manually assign a transaction to an order (event slug + order code)."""
    permission = 'can_change_organizer_settings'

    def post(self, request, *args, **kwargs):
        organizer = request.organizer
        trans = get_object_or_404(
            BankTransaction,
            pk=kwargs['transaction_id'],
            connection__organizer=organizer,
        )
        event_slug = request.POST.get('event_slug', '').strip()
        order_code = request.POST.get('order_code', '').strip().upper()
        next_url = request.POST.get('next') or reverse(
            'plugins:pretix_bank_sync:review_queue',
            kwargs={'organizer': organizer.slug},
        )

        if not event_slug or not order_code:
            messages.error(request, _('Event and order code are required.'))
            return redirect(next_url)

        event = Event.objects.filter(organizer=organizer, slug=event_slug).first()
        if not event:
            messages.error(request, _('Event not found.'))
            return redirect(next_url)

        from pretix.base.models import Order
        order = Order.objects.filter(event=event, code=order_code).first()
        if not order:
            messages.error(request, _('Order not found.'))
            return redirect(next_url)

        try:
            TransactionMatchSuggestion.objects.filter(
                transaction=trans, is_approved__isnull=True
            ).update(is_approved=False, reviewed_at=now(), reviewed_by=request.user)

            TransactionMatchSuggestion.objects.create(
                transaction=trans,
                order=order,
                match_type=TransactionMatchSuggestion.MATCH_TYPE_MANUAL,
                confidence_score=1.0,
                match_reason=_('Manually assigned by {user}').format(user=request.user),
                amount_match=abs(trans.amount - order.pending_sum) < Decimal('0.01'),
                amount_difference=trans.amount - order.pending_sum,
                is_approved=True,
                reviewed_at=now(),
                reviewed_by=request.user,
            )

            trans.order = order
            trans.state = BankTransaction.STATE_MATCHED
            if trans.amount < order.pending_sum:
                trans.is_partial_payment = True
                trans.payment_group_id = f"{order.code}_{trans.date.isoformat()}"
            _handle_transaction(
                trans,
                [(event.slug, order.code)],
                {},
                organizer,
                amount=trans.amount,
            )
            messages.success(
                request,
                _('Transaction assigned to {event}-{code}.').format(
                    event=event.slug, code=order.code
                ),
            )
        except Exception as e:
            logger.exception("Manual assign failed")
            messages.error(request, _('Could not assign transaction: {}').format(str(e)))

        return redirect(next_url)


class OrganizerMatchApproveView(OrganizerPermissionRequiredMixin, View):
    permission = 'can_change_organizer_settings'

    def post(self, request, *args, **kwargs):
        organizer = request.organizer
        suggestion = get_object_or_404(
            TransactionMatchSuggestion,
            pk=kwargs['suggestion_id'],
            transaction__connection__organizer=organizer,
            is_approved__isnull=True,
        )
        try:
            approve_match_suggestion(suggestion.pk, request.user)
            messages.success(request, _('Match approved and transaction processed successfully.'))
        except Exception as e:
            logger.exception("Error approving match suggestion")
            messages.error(request, _('Error approving match: {}').format(str(e)))
        return redirect(request.POST.get('next') or reverse(
            'plugins:pretix_bank_sync:review_queue',
            kwargs={'organizer': organizer.slug},
        ))


class OrganizerMatchRejectView(OrganizerPermissionRequiredMixin, View):
    permission = 'can_change_organizer_settings'

    def post(self, request, *args, **kwargs):
        organizer = request.organizer
        suggestion = get_object_or_404(
            TransactionMatchSuggestion,
            pk=kwargs['suggestion_id'],
            transaction__connection__organizer=organizer,
            is_approved__isnull=True,
        )
        suggestion.is_approved = False
        suggestion.reviewed_at = now()
        suggestion.reviewed_by = request.user
        suggestion.save()
        messages.success(request, _('Match suggestion rejected.'))
        return redirect(request.POST.get('next') or reverse(
            'plugins:pretix_bank_sync:review_queue',
            kwargs={'organizer': organizer.slug},
        ))


class TransactionMatchReviewView(EventPermissionRequiredMixin, PaginationMixin, ListView):
    """View for reviewing and approving transaction matches (event-level)."""
    permission = 'can_change_orders'
    template_name = 'pretix_bank_sync/control/match_review.html'
    context_object_name = 'transactions'
    model = BankTransaction
    paginate_by = 50

    def get_queryset(self):
        event = get_object_or_404(Event, slug=self.kwargs['event'], organizer__slug=self.kwargs['organizer'])
        
        # Get all transactions that either:
        # 1. Are matched to an order in this event, OR
        # 2. Have pending suggestions for orders in this event, OR
        # 3. Are unmatched but belong to a connection for this organizer (might match later)
        # We'll filter by organizer connection and then show those with orders in this event or pending suggestions
        
        # First get transactions from connections for this organizer
        transactions = BankTransaction.objects.filter(
            connection__organizer=event.organizer
        ).select_related(
            'connection',
            'order',
            'order__event',
            'payment'
        ).prefetch_related(
            'match_suggestions',
            'match_suggestions__order'
        )
        
        # Event view: only this event's matches / suggestions.
        # Organizer-wide unclear txs live in the organizer review queue.
        event_transaction_ids = set()

        matched = transactions.filter(order__event=event).values_list('id', flat=True)
        event_transaction_ids.update(matched)

        pending_suggestions = TransactionMatchSuggestion.objects.filter(
            transaction__connection__organizer=event.organizer,
            order__event=event,
            is_approved__isnull=True
        ).values_list('transaction_id', flat=True)
        event_transaction_ids.update(pending_suggestions)

        return transactions.filter(id__in=event_transaction_ids).order_by('-date', '-created')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        event = get_object_or_404(Event, slug=self.kwargs['event'], organizer__slug=self.kwargs['organizer'])
        
        event_transaction_ids = set(
            BankTransaction.objects.filter(
                connection__organizer=event.organizer,
                order__event=event,
            ).values_list('id', flat=True)
        )
        event_transaction_ids.update(
            TransactionMatchSuggestion.objects.filter(
                transaction__connection__organizer=event.organizer,
                order__event=event,
                is_approved__isnull=True,
            ).values_list('transaction_id', flat=True)
        )

        all_transactions = BankTransaction.objects.filter(id__in=event_transaction_ids)
        ctx['total_transactions'] = all_transactions.count()
        ctx['pending_suggestions'] = TransactionMatchSuggestion.objects.filter(
            transaction__connection__organizer=event.organizer,
            order__event=event,
            is_approved__isnull=True,
        ).count()
        ctx['matched_transactions'] = all_transactions.filter(
            state=BankTransaction.STATE_MATCHED
        ).count()
        ctx['pending_approval'] = all_transactions.filter(
            state=BankTransaction.STATE_PENDING_APPROVAL
        ).count()
        ctx['unmatched_transactions'] = 0
        ctx['event'] = event
        ctx['organizer'] = event.organizer
        return ctx


class TransactionMatchApproveView(EventPermissionRequiredMixin, View):
    """Approve a match suggestion."""
    permission = 'can_change_orders'

    def post(self, request, *args, **kwargs):
        event = get_object_or_404(Event, slug=kwargs['event'], organizer__slug=kwargs['organizer'])
        suggestion = get_object_or_404(
            TransactionMatchSuggestion,
            pk=kwargs['suggestion_id'],
            order__event=event,
            is_approved__isnull=True
        )
        
        try:
            approve_match_suggestion(suggestion.pk, request.user)
            messages.success(request, _('Match approved and transaction processed successfully.'))
        except Exception as e:
            logger.exception("Error approving match suggestion")
            messages.error(request, _('Error approving match: {}').format(str(e)))
        
        return redirect('plugins:pretix_bank_sync:match_review', organizer=event.organizer.slug, event=event.slug)


class TransactionMatchRejectView(EventPermissionRequiredMixin, View):
    """Reject a match suggestion."""
    permission = 'can_change_orders'

    def post(self, request, *args, **kwargs):
        event = get_object_or_404(Event, slug=kwargs['event'], organizer__slug=kwargs['organizer'])
        suggestion = get_object_or_404(
            TransactionMatchSuggestion,
            pk=kwargs['suggestion_id'],
            order__event=event,
            is_approved__isnull=True
        )
        
        suggestion.is_approved = False
        suggestion.reviewed_at = now()
        suggestion.reviewed_by = request.user
        suggestion.save()
        
        messages.success(request, _('Match suggestion rejected.'))
        return redirect('plugins:pretix_bank_sync:match_review', organizer=event.organizer.slug, event=event.slug)
