import logging

from django.dispatch import receiver
from django.urls import resolve
from pretix.multidomain.urlreverse import build_absolute_uri

from pretix.helpers.http import redirect_to_url
from pretix.presale.signals import process_request

logger = logging.getLogger(__name__)

@receiver(process_request, dispatch_uid="fzbackend_utils_process_request")
def returnurl_process_request(sender, request, **kwargs):
    try:
        r = resolve(request.path_info)
    except:
        logging.error("Error while resolving path info")
        return

    if r.url_name == "event.order":
        urlkwargs = r.kwargs

        # backend should listen to calls to /basePath/{organizer}/{event}/order/{code}/{secret}/open/{hmac}/
        # and use them to match the user to the specified order.
        # hmac is used internally by pretix to verify email addresses, for us it's useless
        return redirect_to_url(
            build_absolute_uri(
                request.event, 'presale:event.order.open', kwargs={
                    'order': urlkwargs["order"],
                    'secret': urlkwargs["secret"],
                    'hash': "owo"
                }
            )
        )


