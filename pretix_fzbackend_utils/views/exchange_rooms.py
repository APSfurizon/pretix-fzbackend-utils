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

@method_decorator(xframe_options_exempt, "dispatch")
@method_decorator(csrf_exempt, "dispatch")
class ApiExchangeRooms(APIView, View):
    permission = "can_change_orders"
    def post(self, request, organizer, event, *args, **kwargs):
        data = request.data
        
        # Source info
        if "sourceOrderCode" not in data or not isinstance(data["sourceOrderCode"], str):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "sourceOrderCode"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "sourceRoomPositionId" not in data or not isinstance(data["sourceRoomPositionId"], int):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "sourceRoomPositionId"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "sourceEarlyPositionId" in data and not isinstance(data["sourceEarlyPositionId"], int):
            return JsonResponse(
                {"error": 'Invalid parameter "sourceEarlyPositionId"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "sourceLatePositionId" in data and not isinstance(data["sourceLatePositionId"], int):
            return JsonResponse(
                {"error": 'Invalid parameter "sourceLatePositionId"'}, status=status.HTTP_400_BAD_REQUEST
            )
        # Dest info
        if "destOrderCode" not in data or not isinstance(data["destOrderCode"], str):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "destOrderCode"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "destRoomPositionId" not in data or not isinstance(data["destRoomPositionId"], int):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "destRoomPositionId"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "destEarlyPositionId" in data and not isinstance(data["destEarlyPositionId"], int):
            return JsonResponse(
                {"error": 'Invalid parameter "destEarlyPositionId"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "destLatePositionId" in data and not isinstance(data["destLatePositionId"], int):
            return JsonResponse(
                {"error": 'Invalid parameter "destLatePositionId"'}, status=status.HTTP_400_BAD_REQUEST
            )
        # Extra
        if "manualPaymentComment" in data and not isinstance(data["manualPaymentComment"], str):
            return JsonResponse(
                {"error": 'Invalid parameter "manualPaymentComment"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "manualRefundComment" in data and not isinstance(data["manualRefundComment"], str):
            return JsonResponse(
                {"error": 'Invalid parameter "manualRefundComment"'}, status=status.HTTP_400_BAD_REQUEST
            )

        src = self.SideData(data, "source")
        dst = self.SideData(data, "dest")
        paymentComment = data.get("manualPaymentComment", None)
        refundComment = data.get("manualRefundComment", None)
        
        logger.info(
            f"ApiExchangeRooms [{src.orderCode}-{dst.orderCode}]: Got from req  src=[{src}]  dst=[{dst}]"
        )
        
        # Create an order over the orders to acquire the locks. In this way we prevent deadlocks
        # Ugly ass btw
        srcBigger = self.strCmp(src.orderCode, dst.orderCode) == src.orderCode
        ordAdata = src if srcBigger else dst
        ordBdata = dst if srcBigger else src
        
        CONTEXT = {}
        balance = self.Balance(0, 0)
        
        try:
            with transaction.atomic():
                # Aggressive locking, but I prefere instead of thinking of all possible quota to lock
                lock_objects([self.event])
                ordA = self.SideInstance(ordAdata, request)
                ordA.verifyPaymentsRefundsStatus()
                ordB = self.SideInstance(ordBdata, request)
                ordB.verifyPaymentsRefundsStatus()
                
        except FzException as fe:
            return JsonResponse(fe.extraData, status=status.HTTP_412_PRECONDITION_FAILED)
        
        logger.info(
            f"ApiExchangeRooms [{src.orderCode}-{dst.orderCode}]: Success"
        )

        return HttpResponse("")
    
    def exchangeOneWay(self, src, dest, ocmDest: FzOrderChangeManager):
        if src is None:
            return 0
        if dest is not None:
            ocmDest.change_item(
                position=dest.pos,
                item=newRootItem,
                variation=newRootItemVariation
            )
            ocmDest.change_price(
                position=rootPosition,
                price=0 #newRootItem.default_price if newRootItemVariation is None else newRootItemVariation.default_price
            )
        else:
            pass
    def exchange(self, a, b, ocmA: FzOrderChangeManager, ocmB: FzOrderChangeManager):
        pass


    def strCmp(self, x, y):
        if len(x) > len(y):
            return x
        if len(x)==len(y):
            return min(x,y) 
        else:
            return y
        
class Balance:
    balanceA: int
    balanceB: int
    def __init__(self, startingA, startingB):
        self.balanceA = startingA
        self.balanceB = startingB
    def __add__(self, o):
        return Balance(self.balanceA + o.balanceA, self.balanceB + o.balanceB)

class SideData:
    orderCode: str
    roomPosId: int
    earlyPosId: int
    latePosId: int
    def __init__(self, data, side:str):
        self.orderCode = data[f"{side}OrderCode"]
        self.roomPosId = data[f"{side}RoomPositionId"]
        self.earlyPosId = data.get(f"{side}EarlyPositionId", None)
        self.latePosId = data.get(f"{side}LatePositionId", None)
    def __str__(self):
        return f"[{self.orderCode}{{roomPosId={self.roomPosId} earlyPosId={self.earlyPosId} latePosId={self.latePosId}}}]"
    
class Element:
    pos: OrderPosition
    # Price ALWAYS includes taxes
    paid: int
    item: Item
    itemVar: ItemVariation
    itemPrice: int
    def __init__(self, positionId, order):
        self.pos = get_object_or_404(
            OrderPosition.objects.select_for_update(of=OF_SELF).filter(pk=positionId, order__pk=order.pk)
        )
        self.paid = self.pos.price
        self.item = self.pos.item
        self.itemVar = self.pos.variation
        self.price = self.itemVar.price if self.itemVar else self.item.default_price
    
class SideInstance:    
    order: Order
    ocm: FzOrderChangeManager
    room: Element
    early: Element
    late: Element
    # We assume we already are in a transaction.atomic()
    def __init__(self, data, request):
        self.order = get_object_or_404(
            Order.objects.select_for_update(of=OF_SELF).filter(event=request.event, code=data.orderCode, event__organizer=request.organizer)
        )
        self.ocm = FzOrderChangeManager(
            order=self.order,
            user=request.user if request.user.is_authenticated else None,
            auth=request.auth,
            notify=False,
            reissue_invoice=False,
        )
        self.room = self.Element(data.roomPosId, self.order)
        self.early = self.Element(data.earlyPosId, self.order) if data.earlyPosId else None
        self.late = self.Element(data.latePosId, self.order) if data.latePosId else None
            
    def verifyPaymentsRefundsStatus(self):
        # Already ordered in the Meta class of OrderPayment/Refund. Order is important for deadlock prevention
        payments: List[OrderPayment] = OrderPayment.objects.select_for_update(of=OF_SELF).filter(order__pk=self.order.pk, state__in=[OrderPayment.PAYMENT_STATE_CONFIRMED, OrderPayment.PAYMENT_STATE_CREATED, OrderPayment.PAYMENT_STATE_PENDING])
        for payment in payments:
            if payment.state != OrderPayment.PAYMENT_STATE_CONFIRMED:
                logger.error(
                    f"ApiExchangeRooms [{self.order.code}]: Payment {payment.full_id}: invalid state {payment.state}"
                )
                raise FzException("", extraData={"error": f'Payment {payment.full_id} is in invalid state {payment.state}'})
        refunds: List[OrderRefund] = OrderRefund.objects.select_for_update(of=OF_SELF).filter(order__pk=self.order.pk, state__in=[OrderRefund.REFUND_STATE_CREATED, OrderRefund.REFUND_STATE_TRANSIT, OrderRefund.REFUND_STATE_DONE, OrderRefund.REFUND_STATE_EXTERNAL])
        for refund in refunds:
            if refund.state in [OrderRefund.REFUND_STATE_CREATED, OrderRefund.REFUND_STATE_TRANSIT]:
                logger.error(
                    f"ApiExchangeRooms [{self.order.code}]: Refund {refund.full_id}: invalid state {refund.state}"
                )
                raise FzException("", extraData={"error": f'Refund {refund.full_id} is in invalid state {refund.state}'})
