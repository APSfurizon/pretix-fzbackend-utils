import json
import logging
import re
from fzutils.FzOrderChangeManager import FzOrderChangeManager
from rest_framework.views import APIView
from django import forms
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from pretix.base.forms import SettingsForm
from pretix.base.settings import GlobalSettingsObject
from pretix.base.models import (
    Item, ItemVariation, Event, Order,
    OrderPosition
)
from pretix.control.views.event import EventSettingsFormView, EventSettingsViewMixin
from pretix.base.services.orders import OrderChangeManager


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
                {"error": 'Missing or invalid parameter "position"'}, status=400
            )
        if "is_bundle" not in data or not isinstance(data["is_bundle"], bool):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "is_bundle"'}, status=400
            )
        logger.info(
            f"Backend is trying to set is_bundle for position {data['position']} to {data['is_bundle']}"
        )

        position: OrderPosition = get_object_or_404(
            OrderPosition.objects.filter(id=data["position"])
        )

        position.is_bundled = data["is_bundle"]
        position.save(update_fields=["is_bundled"])
        logger.info(
            f"Backend successfully set is_bundle for position {data['position']} to {data['is_bundle']}"
        )

        return HttpResponse("")


@method_decorator(xframe_options_exempt, "dispatch")
@method_decorator(csrf_exempt, "dispatch")
class ApiConvertTicketOnlyOrder(APIView, View):
    permission = "can_change_orders"
    def post(self, request, organizer, event, *args, **kwargs):
        #TODO header check
        data = request.data
        #TODO input validation
        #logger.info(
        #    f"Backend is trying to set is_bundle for position {data['position']} to {data['is_bundle']}"
        #)
        orderCode = "907E7"
        itemId = 1
        itemVariationId = None

        order: Order = get_object_or_404(
            Order.objects.filter(event=request.event, code=orderCode, event__organizer=request.organizer)
        )
        item: Item = get_object_or_404(
            Item.objects.filter(pk=itemId)
        )
        itemVariation: ItemVariation = get_object_or_404(
            ItemVariation.objects.filter(pk=itemVariationId)
        ) if itemVariationId is not None else None
    

        ocm = FzOrderChangeManager(
            order=order,
            user=None,
            auth=request.auth,
            notify=False,
            reissue_invoice=False,
        )
        
        ocm.add_position_no_addon_validation(
            item=item,
            variation=itemVariation,
            price=None,
            addon_to=validated_data.get('addon_to'),
            subevent=validated_data.get('subevent'),
            seat=validated_data.get('seat'),
            valid_from=validated_data.get('valid_from'),
            valid_until=validated_data.get('valid_until'),
        )
        
        nextposid = order.all_positions.aggregate(m=Max('positionid'))['m'] + 1
        pos = OrderPosition.objects.create(
            item=op.item, variation=op.variation, addon_to=op.addon_to,
            price=op.price.gross, order=self.order, tax_rate=op.price.rate, tax_code=op.price.code,
            tax_value=op.price.tax, tax_rule=op.item.tax_rule,
            positionid=nextposid, subevent=op.subevent, seat=op.seat,
            used_membership=op.membership, valid_from=op.valid_from, valid_until=op.valid_until,
            is_bundled=op.is_bundled,
        )
        nextposid += 1
        self.order.log_action('pretix.event.order.changed.add', user=self.user, auth=self.auth, data={
            'position': pos.pk,
            'item': op.item.pk,
            'variation': op.variation.pk if op.variation else None,
            'addon_to': op.addon_to.pk if op.addon_to else None,
            'price': op.price.gross,
            'positionid': pos.positionid,
            'membership': pos.used_membership_id,
            'subevent': op.subevent.pk if op.subevent else None,
            'seat': op.seat.pk if op.seat else None,
            'valid_from': op.valid_from.isoformat() if op.valid_from else None,
            'valid_until': op.valid_until.isoformat() if op.valid_until else None,
        })

        #logger.info(
        #    f"Backend successfully set is_bundle for position {data['position']} to {data['is_bundle']}"
        #)
        print(order)

        return HttpResponse("")
