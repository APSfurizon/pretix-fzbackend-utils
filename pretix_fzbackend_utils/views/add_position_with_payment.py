import logging
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.utils.timezone import now
from pretix.api.serializers.order import OrderPaymentCreateSerializer
from pretix.base.models import Item, ItemVariation, Order, OrderPosition, OrderPayment
from pretix.helpers import OF_SELF
from rest_framework import status, serializers
from rest_framework.views import APIView

from pretix_fzbackend_utils.payment import (
    FZ_MANUAL_PAYMENT_PROVIDER_IDENTIFIER,
    FZ_MANUAL_PAYMENT_PROVIDER_ISSUER,
)

from pretix.base.services.orders import OrderChangeManager
#from pretix_fzbackend_utils.fz_utilites.fzOrderChangeManager import FzOrderChangeManager

from ..utils import verifyToken

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


@method_decorator(xframe_options_exempt, "dispatch")
@method_decorator(csrf_exempt, "dispatch")
class ApiAddPositionAndPayment(APIView, View):
    permission = "can_change_orders"

    def post(self, request, organizer, event, *args, **kwargs):
        verifyToken(request)
        data = request.data

        if "orderCode" not in data or not isinstance(data["orderCode"], str):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "orderCode"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "item" not in data or not isinstance(data["item"], int):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "item"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "create_payment" not in data or not isinstance(data["create_payment"], bool):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "create_payment"'}, status=status.HTTP_400_BAD_REQUEST
            )
        
        if "quantity" in data and data["quantity"] and not isinstance(data["quantity"], int):
            return JsonResponse(
                {"error": 'Invalid parameter "quantity"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "addon_to" in data and data["addon_to"] and not isinstance(data["addon_to"], int):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "addon_to"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "variation" in data and data["variation"] and not isinstance(data["variation"], int):
            return JsonResponse(
                {"error": 'Invalid parameter "variation"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "price" in data and data["price"] and not isinstance(data["price"], str):
            return JsonResponse(
                {"error": 'Invalid parameter "price"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "check_quotas" in data and data["check_quotas"] and not isinstance(data["check_quotas"], bool):
            return JsonResponse(
                {"error": 'Invalid parameter "check_quotas"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "notify_user" in data and data["notify_user"] and not isinstance(data["notify_user"], bool):
            return JsonResponse(
                {"error": 'Invalid parameter "notify_user"'}, status=status.HTTP_400_BAD_REQUEST
            )
            
        orderCode = data["orderCode"]
        itemId = data["item"]
        create_payment = data["create_payment"]
        quantity = data.get("quantity", 1)
        addon_to_id = data.get("addon_to", None)
        variationId = data.get("variation", None)
        price = data.get("price", None)
        check_quotas = data.get("check_quotas", True)
        notify_user = data.get("notify_user", True)
        
        logger.info(
            f"ApiAddPositionAndPayment [{orderCode}]: "
            f"Got from req itemId={itemId} addon_to={addon_to_id} variationId={variationId} price={price}"
        )
        
        with transaction.atomic():
            order: Order = get_object_or_404(
                Order.objects.select_for_update(of=OF_SELF).filter(event=request.event, code=orderCode, event__organizer=request.organizer)
            )
            item: Item = get_object_or_404(
                Item.objects.select_for_update(of=OF_SELF).filter(pk=itemId, event__pk=request.event.pk)
            )
            
            addon_to: OrderPosition = get_object_or_404(
                OrderPosition.objects.select_for_update(of=OF_SELF).filter(pk=addon_to_id, order__pk=order.pk)
            ) if addon_to_id is not None else None
            variation: ItemVariation = get_object_or_404(
                ItemVariation.objects.select_for_update(of=OF_SELF).filter(pk=variationId, item__pk=itemId)
            ) if variationId is not None else None
            
            ocm: OrderChangeManager = OrderChangeManager(
                order=order,
                user=self.request.user if self.request.user.is_authenticated else None,
                auth=request.auth,
                notify=notify_user,
                reissue_invoice=False,
            )
            newPositionHandler = ocm.add_position(
                item=item,
                variation=variation,
                price=price,
                addon_to=addon_to,
                subevent=addon_to.subevent if addon_to else None,
                seat=None,
                valid_from=addon_to.valid_from if addon_to else None,
                valid_until=addon_to.valid_until if addon_to else None,
                count=quantity
            )
            ocm.commit(check_quotas=check_quotas)

            newPositions = newPositionHandler.positions
            newPositionIds = [newPosition.pk for newPosition in newPositions]
            logger.debug(
                f"ApiAddPositionAndPayment [{orderCode}]: Newly added position {newPositionIds}"
            )
            
            if (create_payment):
                paymentData = {
                    "state": OrderPayment.PAYMENT_STATE_PENDING,
                    "amount": sum([pos.price for pos in newPositions]),
                    "payment_date": serializers.DateTimeField().to_internal_value(now()),
                    "sendEmail": False,
                    "provider": FZ_MANUAL_PAYMENT_PROVIDER_IDENTIFIER,
                    "info": {
                        "issued_by": FZ_MANUAL_PAYMENT_PROVIDER_ISSUER,
                        "comment": f"Payment created while adding {quantity}x {item.name} to via API",
                    }
                }
                paymentSerializer = OrderPaymentCreateSerializer(data=paymentData, context={"event": request.event, "pdf_data": False, "order": order, "auth": request.auth})
                paymentSerializer.is_valid(raise_exception=True)
                paymentSerializer.save()
                newPayment: OrderPayment = paymentSerializer.instance
                order.log_action(
                    'pretix.event.order.payment.started', {
                        'local_id': newPayment.local_id,
                        'provider': newPayment.provider,
                    },
                    user=request.user if request.user.is_authenticated else None,
                    auth=request.auth
                )
                newPayment.confirm(
                    user=self.request.user if self.request.user.is_authenticated else None,
                    auth=self.request.auth,
                    count_waitinglist=False,
                    ignore_date=True,
                    force=True,
                    send_mail=False,
                )
                logger.info(f"ApiAddPositionAndPayment [{orderCode}]: Payment created")
                
        return JsonResponse({"newPositions": newPositionIds}, status=status.HTTP_200_OK)