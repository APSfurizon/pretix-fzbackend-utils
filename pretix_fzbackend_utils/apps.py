from django.utils.translation import gettext_lazy

from . import __version__

try:
    from pretix.base.plugins import PluginConfig
except ImportError:
    raise RuntimeError("Please use pretix 2.7 or above to run this plugin!")


class PluginApp(PluginConfig):
    default = True
    name = "pretix_fzbackend_utils"
    verbose_name = "Pretix fz-backend utils"

    class PretixPluginMeta:
        name = gettext_lazy("Pretix fz-backend utils")
        author = "APSfurizon"
        description = gettext_lazy(
            "Pretix utils plugin to work together with fz-backend"
        )
        visible = True
        version = __version__
        category = "API"
        compatibility = "pretix>=2024.9.0"

    def ready(self):
        from . import signals  # NOQA
