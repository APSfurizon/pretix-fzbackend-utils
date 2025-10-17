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

        src = SideData(data, "source")
        dst = SideData(data, "dest")
        paymentComment = data.get("manualPaymentComment", None)
        refundComment = data.get("manualRefundComment", None)
        
        logger.info(
            f"ApiExchangeRooms [{src.orderCode}-{dst.orderCode}]: Got from req  src=[{src}]  dst=[{dst}]"
        )
        
        if src.roomPosId == None:
            return JsonResponse(
                {"error": 'Source should have a valid room position!'}, status=status.HTTP_400_BAD_REQUEST
            )
        if dst.roomPosId == None:
            return JsonResponse(
                {"error": 'Dest should have a valid room position!'}, status=status.HTTP_400_BAD_REQUEST
            )
        
        # Create an order over the orders to acquire the locks. In this way we prevent deadlocks
        # Ugly ass btw
        srcBigger = strCmp(src.orderCode, dst.orderCode) == src.orderCode
        ordAdata = src if srcBigger else dst
        ordBdata = dst if srcBigger else src
        
        balance = Balance(0, 0)
        
        try:
            with transaction.atomic():
                # Aggressive locking, but I prefere instead of thinking of all possible quota to lock
                lock_objects([request.event])
                ordA = SideInstance(ordAdata, request)
                ordA.verifyPaymentsRefundsStatus()
                ordB = SideInstance(ordBdata, request)
                ordB.verifyPaymentsRefundsStatus()
                logger.debug(f"ApiExchangeRooms [{src.orderCode}-{dst.orderCode}]: Loaded instances and verified payments/refunds")
                
                balance += exchange(ordA.room, ordB.room, ordA.ocm, ordB.ocm)
                balance += exchange(ordA.early, ordB.early, ordA.ocm, ordB.ocm)
                balance += exchange(ordA.late, ordB.late, ordA.ocm, ordB.ocm)
                logger.debug(f"ApiExchangeRooms [{src.orderCode}-{dst.orderCode}]: Exchanges done")
                
                fixPaymentStatus(balance.balanceA, ordA.order, refundComment, paymentComment, request, {"order": ordA.order, "event": request.event})
                fixPaymentStatus(balance.balanceB, ordB.order, refundComment, paymentComment, request, {"order": ordB.order, "event": request.event})
                logger.debug(f"ApiExchangeRooms [{src.orderCode}-{dst.orderCode}]: Payment status fixed")
                
                ordA.ocm.fz_enable_locking = False
                ordB.ocm.fz_enable_locking = False
                ordA.ocm.commit(check_quotas=False)
                ordB.ocm.commit(check_quotas=False)
                
        except FzException as fe:
            return JsonResponse(fe.extraData, status=status.HTTP_412_PRECONDITION_FAILED)
        
        logger.info(
            f"ApiExchangeRooms [{src.orderCode}-{dst.orderCode}]: Success"
        )

        return HttpResponse("")
    
def fixPaymentStatus(balance: int, order: Order, refundComment: str, paymentComment: str, request, orderContext):
    amount = serializers.DecimalField(max_digits=13, decimal_places=2).to_internal_value(str(balance))
    dateNow = serializers.DateTimeField().to_internal_value(now())
    
    if balance > 0:
        refundData = {
            "state": OrderRefund.REFUND_STATE_DONE,
            "source": OrderRefund.REFUND_SOURCE_EXTERNAL,
            "amount": amount,
            "execution_date": dateNow,
            "comment": refundComment,
            "provider": FZ_MANUAL_PAYMENT_PROVIDER_IDENTIFIER,
            # mark canceled/pending not needed
        }
        refundSerializer = OrderRefundCreateSerializer(data=refundData, context=orderContext)
        refundSerializer.is_valid(raise_exception=True)
        refundSerializer.save()
        newRefund: OrderRefund = refundSerializer.instance
        # Double log to follow what the api.views.order.RefundViewSet.create() does
        order.log_action(
            'pretix.event.order.refund.created', {
                'local_id': newRefund.local_id,
                'provider': newRefund.provider,
            },
            user=request.user if request.user.is_authenticated else None,
            auth=request.auth
        )
        order.log_action(
            f'pretix.event.order.refund.{newRefund.state}', {
                'local_id': newRefund.local_id,
                'provider': newRefund.provider,
            },
            user=request.user if request.user.is_authenticated else None,
            auth=request.auth
        )
    elif balance < 0:
        paymentData = {
            "state": OrderPayment.PAYMENT_STATE_PENDING,
            "amount": amount,
            "payment_date": dateNow,
            "sendEmail": False,
            "provider": FZ_MANUAL_PAYMENT_PROVIDER_IDENTIFIER,
            "info": {
                "issued_by": FZ_MANUAL_PAYMENT_PROVIDER_ISSUER,
                "comment": paymentComment
            }
        }                
        paymentSerializer = OrderPaymentCreateSerializer(data=paymentData, context=orderContext)
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
            user=request.user if request.user.is_authenticated else None,
            auth=request.auth,
            count_waitinglist=False,
            ignore_date=True,
            force=True,
            send_mail=False,
        )
    
    
# We return >0 if dest should pay more, <0 if dest should instead get a refund
def transfer(src: Element, dest: Element, addonTo: OrderPosition, ocmDest: FzOrderChangeManager) -> int:
    # This DOES NOT copy or transfer extra position information!!
    # With the current OCM implementation we have no access to the exact newly created position, making us impossible
    # the job of updating with the extra information
    if src is None:
        if dest is None:
            return 0
        else:
            ocmDest.cancel(dest.pos)
            return -dest.paid
    else:
        if dest is None:
            ocmDest.add_position_no_addon_validation(
                item=src.item,
                variation=src.itemVar,
                price=src.price,
                addon_to=addonTo,
                subevent=src.pos.subevent,
                seat=src.pos.seat,
                #membership=rootPosition.membership,
                valid_from=src.pos.valid_from,
                valid_until=src.pos.valid_until,
                is_bundled=False
            )
            return src.price
        else:
            ocmDest.change_item(dest.pos, src.item, src.itemVar)
            ocmDest.change_price(dest.pos, src.price)
            ocmDest.change_subevent(dest.pos, src.pos.subevent)
            ocmDest.change_seat(dest.pos, src.pos.seat)
            ocmDest.change_valid_from(dest.pos, src.pos.valid_from)
            ocmDest.change_valid_until(dest.pos, src.pos.valid_until)
            # Currently we cannot change the bundle status
            return src.price - dest.paid
            
def exchange(a: Element, b: Element, ocmA: FzOrderChangeManager, ocmB: FzOrderChangeManager) -> Balance:
    balB = transfer(a, b, ocmB)
    balA = transfer(b, a, ocmA)
    return Balance(balA, balB)

def strCmp(x, y):
    if len(x) > len(y):
        return x
    if len(x)==len(y):
        return min(x,y) 
    else:
        return y