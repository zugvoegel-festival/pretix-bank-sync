from django.utils.translation import gettext_lazy
from . import __version__

try:
    from pretix.base.plugins import PluginConfig, PLUGIN_LEVEL_ORGANIZER
except ImportError:
    raise RuntimeError("Please use pretix 2.7 or above to run this plugin!")


class PluginApp(PluginConfig):
    name = "pretix_bank_sync"
    verbose_name = "Bank Sync"

    class PretixPluginMeta:
        name = gettext_lazy("Bank Sync")
        author = "pretix"
        description = gettext_lazy("Sync bank account transactions via GoCardless Bank Account Data and automatically match them to orders")
        visible = True
        version = __version__
        category = "INTEGRATION"
        compatibility = "pretix>=2.7.0"
        # Must live on PretixPluginMeta — pretix defaults missing level to EVENT
        # and the organizer plugins page only enables ORGANIZER / HYBRID plugins.
        level = PLUGIN_LEVEL_ORGANIZER

    def ready(self):
        from . import signals  # NOQA
        self._exempt_gocardless_callback_from_control_auth()

    def _exempt_gocardless_callback_from_control_auth(self):
        """
        GoCardless redirects the browser to our callback without a pretix
        organizer-admin session. Paths under /control/ are gated by
        PermissionMiddleware; allow our callback view names through.
        """
        try:
            from pretix.control.middleware import PermissionMiddleware
        except ImportError:
            return

        if getattr(PermissionMiddleware, '_pretix_bank_sync_callback_patched', False):
            return

        _orig = PermissionMiddleware.__call__
        exempt_views = {
            'plugins:pretix_bank_sync:callback',
            'plugins:pretix_bank_sync:callback_legacy',
        }

        def __call__(self, request):
            # Fast path: only touch bank-sync callback URLs under /control/
            if 'bank-sync/callback' in request.path_info:
                try:
                    from django.urls import resolve
                    match = resolve(request.path_info)
                    if match.view_name in exempt_views:
                        return self.get_response(request)
                except Exception:
                    pass
            return _orig(self, request)

        PermissionMiddleware.__call__ = __call__
        PermissionMiddleware._pretix_bank_sync_callback_patched = True
