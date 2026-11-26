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
        description = gettext_lazy(
            "Sync bank account transactions via Enable Banking (PSD2/Open Banking) "
            "and automatically match them to orders"
        )
        visible = True
        version = __version__
        category = "INTEGRATION"
        compatibility = "pretix>=2.7.0"
        # Must live on PretixPluginMeta — pretix defaults missing level to EVENT
        # and the organizer plugins page only enables ORGANIZER / HYBRID plugins.
        level = PLUGIN_LEVEL_ORGANIZER

    def uninstalled(self, organizer):
        from .models import BankConnection
        from .services.credentials import clear_enablebanking_settings

        clear_enablebanking_settings(organizer)
        organizer.settings.delete('pretix_bank_sync_auto_confirm')
        BankConnection.objects.filter(organizer=organizer).delete()

    def ready(self):
        from . import signals  # NOQA
