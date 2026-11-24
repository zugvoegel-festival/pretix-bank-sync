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
