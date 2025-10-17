import logging
import re
from typing import List
from pretix_fzbackend_utils.payment import FZ_MANUAL_PAYMENT_PROVIDER_IDENTIFIER, FZ_MANUAL_PAYMENT_PROVIDER_ISSUER
from pretix_fzbackend_utils.fz_utilites.fzOrderChangeManager import FzOrderChangeManager
from pretix_fzbackend_utils.fz_utilites.fzException import FzException
from rest_framework.views import APIView
from rest_framework import status, serializers
from django import forms
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.timezone import get_current_timezone, make_aware, now
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from pretix.api.serializers.orderchange import OrderPositionInfoPatchSerializer
from pretix.api.serializers.order import OrderRefundCreateSerializer, OrderPaymentCreateSerializer
from pretix.helpers import OF_SELF
from pretix.base.forms import SettingsForm
from pretix.base.settings import GlobalSettingsObject
from pretix.base.models import (
    Item, ItemVariation, Event, Order,
    OrderPosition, OrderPayment, OrderRefund,
    QuestionAnswer, Question
)
from pretix.control.views.event import EventSettingsFormView, EventSettingsViewMixin
from pretix.base.services.locking import lock_objects
from pretix.base.services import tickets
from pretix.base.signals import (
    order_modified, order_paid,
)


logger = logging.getLogger(__name__)


class FznackendutilsSettingsForm(SettingsForm):
    fzbackendutils_redirect_url = forms.RegexField(
        label=_("Order redirect url"),
        help_text=_(
            "When an user has done, has modified or has paid an order, pretix will redirect him to this spacified url, "
            "with the order code and secret appended as query parameters (<code>?c={orderCode}&s={orderSecret}&m={statusMessages}</code>). "
            "This page should call <code>/api/v1/orders-workflow/link-order</code> of the backend to link this order "
            "to the logged in user."
        ),
        required=False,
        widget=forms.TextInput,
        regex=re.compile(r"^(https://.*/.*|http://localhost[:/].*)*$"),
    )


class FznackendutilsSettings(EventSettingsViewMixin, EventSettingsFormView):
    model = Event
    form_class = FznackendutilsSettingsForm
    template_name = "pretix_fzbackend_utils/settings.html"
    permission = "can_change_settings"

    def get_success_url(self) -> str:
        return reverse(
            "plugins:pretix_fzbackend_utils:settings",
            kwargs={
                "organizer": self.request.event.organizer.slug,
                "event": self.request.event.slug,
            },
        )


@method_decorator(xframe_options_exempt, "dispatch")
@method_decorator(csrf_exempt, "dispatch")
class ApiSetItemBundle(APIView, View):
    permission = "can_change_orders"
    def post(self, request, organizer, event, *args, **kwargs):
        token = request.headers.get("fz-backend-api")
        settings = GlobalSettingsObject().settings
        if settings.fzbackendutils_internal_endpoint_token and (
            not token or token != settings.fzbackendutils_internal_endpoint_token
        ):
            return JsonResponse({"error": "Invalid token"}, status=403)

        data = request.data
        if "position" not in data or not isinstance(data["position"], int):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "position"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "is_bundle" not in data or not isinstance(data["is_bundle"], bool):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "is_bundle"'}, status=status.HTTP_400_BAD_REQUEST
            )
        logger.info(
            f"FzBackend is trying to set is_bundle for position {data['position']} to {data['is_bundle']}"
        )

        position: OrderPosition = get_object_or_404(
            OrderPosition.objects.filter(id=data["position"])
        )

        position.is_bundled = data["is_bundle"]
        position.save(update_fields=["is_bundled"])
        logger.info(
            f"FzBackend successfully set is_bundle for position {data['position']} to {data['is_bundle']}"
        )

        return HttpResponse("")

