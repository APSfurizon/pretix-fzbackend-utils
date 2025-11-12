import logging
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from pretix.api.serializers.orderchange import OrderPositionInfoPatchSerializer
from pretix.base.models import Item, ItemVariation, Order, OrderPosition
from pretix.base.services import tickets
from pretix.base.signals import order_modified
from pretix.helpers import OF_SELF
from rest_framework import status
from rest_framework.views import APIView
from ..utils import verifyToken

from pretix_fzbackend_utils.fz_utilites.fzOrderChangeManager import FzOrderChangeManager

logger = logging.getLogger(__name__)


@method_decorator(xframe_options_exempt, "dispatch")
@method_decorator(csrf_exempt, "dispatch")
class ApiConvertTicketOnlyOrder(APIView, View):
    permission = "can_change_orders"

    def post(self, request, organizer, event, *args, **kwargs):
        verifyToken(request)
        data = request.data

        if "orderCode" not in data or not isinstance(data["orderCode"], str):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "orderCode"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "rootPositionId" not in data or not isinstance(data["rootPositionId"], int):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "rootPositionId"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "newRootItemId" not in data or not isinstance(data["newRootItemId"], int):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "newRootItemId"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "newRootItemVariationId" in data and data["newRootItemVariationId"] and not isinstance(data["newRootItemVariationId"], int):
            return JsonResponse(
                {"error": 'Invalid parameter "newRootItemVariationId"'}, status=status.HTTP_400_BAD_REQUEST
            )

        orderCode = data["orderCode"]
        currentRootPositionId = data["rootPositionId"]
        newRootItemId = data["newRootItemId"]
        newRootItemVariationId = data.get("newRootItemVariationId", None)

        logger.info(
            f"ApiConvertTicketOnlyOrder [{orderCode}]: "
            f"Got from req rootPosId={currentRootPositionId} newRootItemId={newRootItemId} newRootItemVariationId={newRootItemVariationId}"
        )

        CONTEXT = {"event": request.event, "pdf_data": False, "check_quotas": False}

        with transaction.atomic():
            # OBTAINS OBJECTS FROM DB
            # Original Order
            order: Order = get_object_or_404(
                Order.objects.select_for_update(of=OF_SELF).filter(event=request.event, code=orderCode, event__organizer=request.organizer)
            )
            # root position, item and variation
            rootPosition: OrderPosition = get_object_or_404(
                OrderPosition.objects.select_for_update(of=OF_SELF).filter(pk=currentRootPositionId, order__pk=order.pk)
            )
            rootItem: Item = rootPosition.item
            rootItemVariation: ItemVariation = rootPosition.variation
            logger.debug(
                f"ApiConvertTicketOnlyOrder [{orderCode}]: "
                f"Fetched current rootItem={rootItem.pk} rootItemVariation={rootItemVariation.pk if rootItemVariation else None}"
            )
            # new item and variation
            newRootItem: Item = get_object_or_404(
                Item.objects.select_for_update(of=OF_SELF).filter(pk=newRootItemId, event__pk=request.event.pk)
            )
            newRootItemVariation: ItemVariation = get_object_or_404(
                ItemVariation.objects.select_for_update(of=OF_SELF).filter(pk=newRootItemVariationId, item__pk=newRootItemId)
            ) if newRootItemVariationId is not None else None

            # POSITION SWAP + CREATION
            ocm = FzOrderChangeManager(
                order=order,
                user=self.request.user if self.request.user.is_authenticated else None,
                auth=request.auth,
                notify=False,
                reissue_invoice=False,
            )
            ocm.add_position_no_addon_validation(
                item=rootItem,
                variation=rootItemVariation,
                price=rootPosition.price,
                addon_to=rootPosition,
                subevent=rootPosition.subevent,
                seat=rootPosition.seat,
                # membership=rootPosition.membership,
                valid_from=rootPosition.valid_from,
                valid_until=rootPosition.valid_until,
                is_bundled=True  # IMPORTANT!
            )
            ocm.change_item(
                position=rootPosition,
                item=newRootItem,
                variation=newRootItemVariation
            )
            ocm.change_price(
                position=rootPosition,
                price=0  # newRootItem.default_price if newRootItemVariation is None else newRootItemVariation.default_price
            )
            ocm.commit(check_quotas=False)

            # Possible race condition, however Pretix does this inside their code as well
            # https://github.com/pretix/pretix/issues/5548
            newPosition: OrderPosition = order.positions.order_by('-positionid').first()
            logger.debug(
                f"ApiConvertTicketOnlyOrder [{orderCode}]: Newly added position {newPosition.pk}"
            )

            # We update with the extra data the newly created position
            rootPositionSerializer = OrderPositionInfoPatchSerializer(instance=rootPosition, context=CONTEXT, partial=True)
            tempSerializer = OrderPositionInfoPatchSerializer(context=CONTEXT, data=rootPositionSerializer.data, partial=True)
            tempSerializer.is_valid(raise_exception=False)
            finalData = {k: v for k, v in rootPositionSerializer.data.items() if k not in tempSerializer.errors}
            newPositionSerializer = OrderPositionInfoPatchSerializer(instance=newPosition, context=CONTEXT, data=finalData, partial=True)
            newPositionSerializer.is_valid(raise_exception=True)
            newPositionSerializer.save()
            rootPosition.refresh_from_db()
            rootPositionSerializer = OrderPositionInfoPatchSerializer(instance=rootPosition, context=CONTEXT, data={"answers": []}, partial=True)
            rootPositionSerializer.is_valid(raise_exception=True)
            rootPositionSerializer.save()
            # We log the extra data changes. The position operations are logged inside OCM already
            if 'answers' in finalData:
                for a in finalData['answers']:
                    finalData[f'question_{a["question"]}'] = a["answer"]
                finalData.pop('answers', None)
            order.log_action(
                'pretix.event.order.modified',
                user=self.request.user,
                auth=self.request.auth,
                data={
                    'data': [
                        dict(
                            position=newPosition.pk,
                            **finalData
                        )
                    ]
                }
            )

            tickets.invalidate_cache.apply_async(kwargs={'event': request.event.pk, 'order': order.pk})
            order_modified.send(sender=request.event, order=order)  # Sadly signal has to be sent twice: One after changing the extra info, and one inside ocm

        logger.info(
            f"ApiConvertTicketOnlyOrder [{orderCode}]: Success"
        )

        return HttpResponse("")
