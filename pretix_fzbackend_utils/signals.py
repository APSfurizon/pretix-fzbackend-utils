import logging
from collections import OrderedDict
from django import forms
from django.contrib.messages import constants as messages, get_messages
from django.core.exceptions import PermissionDenied
from django.dispatch import receiver
from django.urls import resolve, reverse
from django.utils.translation import gettext_lazy as _
from pretix.base.signals import register_global_settings, register_payment_providers
from pretix.control.signals import nav_event_settings
from pretix.helpers.http import redirect_to_url
from pretix.presale.signals import process_request
from urllib.parse import urlencode

from pretix_fzbackend_utils.payment import FzbackendManualPaymentProvider

logger = logging.getLogger(__name__)


@receiver(process_request, dispatch_uid="fzbackendutils_process_request")
def returnurl_process_request(sender, request, **kwargs):
    try:
        r = resolve(request.path_info)
    except Exception as e:
        logger.error("Error while resolving path info:", e)
        return

    if r.url_name == "event.order":
        urlkwargs = r.kwargs

        if not sender.settings.fzbackendutils_redirect_url:
            raise PermissionDenied("fz-backend-utils: no order redirect url set")

        #  Fetch order status messages
        query = []
        storage = get_messages(request)
        for message in storage:
            if message.level == messages.ERROR:
                query.append(("error", str(message)))
            elif message.level == messages.WARNING:
                query.append(("warning", str(message)))
            if message.level == messages.INFO:
                query.append(("info", str(message)))
            if message.level == messages.SUCCESS:
                query.append(("success", str(message)))

        order = urlkwargs["order"]
        secret = urlkwargs["secret"]
        url = (
            sender.settings.fzbackendutils_redirect_url
            + f"?c={order}&s={secret}&m={urlencode(query)}"
        )
        logger.info(f"Redirecting to {url}")
        return redirect_to_url(url)


@receiver(nav_event_settings, dispatch_uid="fzbackendutils_nav")
def navbar_info(sender, request, **kwargs):
    url = resolve(request.path_info)
    if not request.user.has_event_permission(
        request.organizer, request.event, "can_change_event_settings", request=request
    ):
        return []
    return [
        {
            "label": _("Fz-backend settings"),
            "url": reverse(
                "plugins:pretix_fzbackend_utils:settings",
                kwargs={
                    "event": request.event.slug,
                    "organizer": request.organizer.slug,
                },
            ),
            "active": url.namespace == "plugins:pretix_fzbackend_utils",
        }
    ]


@receiver(register_global_settings, dispatch_uid="autocart_global_setting")
def globalSettings(**kwargs):
    return OrderedDict(
        [
            (
                "fzbackendutils_internal_endpoint_token",
                forms.CharField(
                    label=_("[FZBACKEND] Internal endpoint token"),
                    help_text=_(
                        "This plugin exposes some api for extra access to the fz-backend. This token needs to be specified in the "
                        "<code>fz-backend-api</code> header to access these endpoints."
                    ),
                    required=False,
                ),
            )
        ]
    )


@receiver(register_payment_providers, dispatch_uid="payment_fzbackend_manual")
def register_payment_provider(sender, **kwargs):
    return [FzbackendManualPaymentProvider]
