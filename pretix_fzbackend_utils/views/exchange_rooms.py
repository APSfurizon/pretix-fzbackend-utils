from typing import List

import logging
from django.db import transaction
from django.db.models import Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.utils.timezone import now
from django.views import View
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from pretix.api.serializers.order import (
    OrderPaymentCreateSerializer,
    OrderRefundCreateSerializer,
)
from pretix.base.models import (
    Item,
    ItemVariation,
    Order,
    OrderPayment,
    OrderPosition,
    OrderRefund,
)
from pretix.base.services.locking import lock_objects
from pretix.helpers import OF_SELF
from rest_framework import serializers, status
from rest_framework.views import APIView

from pretix_fzbackend_utils.fz_utilites.fzException import FzException
from pretix_fzbackend_utils.fz_utilites.fzOrderChangeManager import FzOrderChangeManager
from pretix_fzbackend_utils.payment import (
    FZ_MANUAL_PAYMENT_PROVIDER_IDENTIFIER,
    FZ_MANUAL_PAYMENT_PROVIDER_ISSUER,
)
from pretix_fzbackend_utils.utils import (
    STATUS_CODE_PAYMENT_INVALID,
    STATUS_CODE_POSITION_CANCELED,
    STATUS_CODE_REFUND_INVALID,
    verifyToken,
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
    rootPositionId: int
    positions: List[int]

    def __init__(self, data, side: str):
        self.orderCode = data[f"{side}OrderCode"]
        self.rootPositionId = data[f"{side}RootPositionId"]
        self.positions = []
        for exchange in data["exchanges"]:
            posId = None
            if f"{side}PositionId" in exchange and exchange[f"{side}PositionId"] is not None:
                posId = exchange[f"{side}PositionId"]
            # Append anyway to not mess with indexes
            self.positions.append(posId)

    def position(self, idx: int) -> int:
        return self.positions[idx]

    def __str__(self):
        return f"{self.orderCode}{{{', '.join([str(p) if p is not None else 'None' for p in self.positions])}}}"


class Element:
    pos: OrderPosition
    # Price ALWAYS includes taxes
    paid: int
    item: Item
    price: int
    itemVar: ItemVariation

    def __init__(self, positionId, order):
        self.pos = get_object_or_404(
            # Cancelation validation is done later for improved error reporting
            OrderPosition.all.select_for_update(of=OF_SELF).filter(pk=positionId, order__pk=order.pk)
        )
        self.paid = self.pos.price
        self.item = self.pos.item
        self.itemVar = self.pos.variation
        self.price = self.itemVar.price if self.itemVar else self.item.default_price
        # Remove the bundle price
        self.price -= self.item.bundles.aggregate(Sum('designated_price'))['designated_price__sum'] or 0


class SideInstance:
    order: Order
    ocm: FzOrderChangeManager
    rootPosition: OrderPosition
    instances: List[Element]

    # We assume we already are in a transaction.atomic()
    def __init__(self, data: SideData, request):
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
        self.instances = []
        for posId in data.positions:
            if posId is not None:
                e = Element(posId, self.order)
                self.instances.append(e)
                if posId == data.rootPositionId:
                    self.rootPosition = e.pos
            else:
                # Store also None for easier indexing
                self.instances.append(None)

    def instance(self, idx: int) -> Element:
        return self.instances[idx]

    def verifyCancelation(self):
        for element in self.instances:
            if element is not None and element.pos.canceled:
                logger.error(
                    f"ApiExchangeRooms [{self.order.code}]: Position {element.pos.pk} is canceled"
                )
                raise FzException("", extraData={"error": f'Position {element.pos.pk} is canceled'}, code=STATUS_CODE_POSITION_CANCELED)

    def verifyPaymentsRefundsStatus(self):
        # Already ordered in the Meta class of OrderPayment/Refund. Order is important for deadlock prevention
        payments: List[OrderPayment] = OrderPayment.objects.select_for_update(of=OF_SELF).filter(order__pk=self.order.pk, state__in=[
            OrderPayment.PAYMENT_STATE_CREATED,
            OrderPayment.PAYMENT_STATE_PENDING
        ])
        for payment in payments:
            logger.error(
                f"ApiExchangeRooms [{self.order.code}]: Payment {payment.full_id}: invalid state {payment.state}"
            )
            raise FzException("", extraData={"error": f'Payment {payment.full_id} is in invalid state {payment.state}'}, code=STATUS_CODE_PAYMENT_INVALID)
        refunds: List[OrderRefund] = OrderRefund.objects.select_for_update(of=OF_SELF).filter(order__pk=self.order.pk, state__in=[
            OrderRefund.REFUND_STATE_CREATED,
            OrderRefund.REFUND_STATE_TRANSIT
        ])
        for refund in refunds:
            logger.error(
                f"ApiExchangeRooms [{self.order.code}]: Refund {refund.full_id}: invalid state {refund.state}"
            )
            raise FzException("", extraData={"error": f'Refund {refund.full_id} is in invalid state {refund.state}'}, code=STATUS_CODE_REFUND_INVALID)


@method_decorator(xframe_options_exempt, "dispatch")
@method_decorator(csrf_exempt, "dispatch")
class ApiExchangeRooms(APIView, View):
    permission = "can_change_orders"

    def post(self, request, organizer, event, *args, **kwargs):
        verifyToken(request)
        data = request.data

        # Source info
        if "sourceOrderCode" not in data or not isinstance(data["sourceOrderCode"], str):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "sourceOrderCode"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "sourceRootPositionId" not in data or not isinstance(data["sourceRootPositionId"], int):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "sourceRootPositionId"'}, status=status.HTTP_400_BAD_REQUEST
            )
        # Dest info
        if "destOrderCode" not in data or not isinstance(data["destOrderCode"], str):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "destOrderCode"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "destRootPositionId" not in data or not isinstance(data["destRootPositionId"], int):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "destRootPositionId"'}, status=status.HTTP_400_BAD_REQUEST
            )
        # Exchange data
        if "exchanges" not in data or not isinstance(data["exchanges"], list):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "exchanges"'}, status=status.HTTP_400_BAD_REQUEST
            )
        for exchangeReq in data["exchanges"]:
            if "sourcePositionId" in exchangeReq and exchangeReq["sourcePositionId"] is not None and not isinstance(exchangeReq["sourcePositionId"], int):
                return JsonResponse(
                    {"error": 'Invalid parameter "sourcePositionId"'}, status=status.HTTP_400_BAD_REQUEST
                )
            if "destPositionId" in exchangeReq and exchangeReq["destPositionId"] is not None and not isinstance(exchangeReq["destPositionId"], int):
                return JsonResponse(
                    {"error": 'Invalid parameter "destPositionId"'}, status=status.HTTP_400_BAD_REQUEST
                )
        # Extra
        if "manualPaymentComment" in data and data["manualPaymentComment"] and not isinstance(data["manualPaymentComment"], str):
            return JsonResponse(
                {"error": 'Invalid parameter "manualPaymentComment"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "manualRefundComment" in data and data["manualRefundComment"] and not isinstance(data["manualRefundComment"], str):
            return JsonResponse(
                {"error": 'Invalid parameter "manualRefundComment"'}, status=status.HTTP_400_BAD_REQUEST
            )

        src = SideData(data, "source")
        dst = SideData(data, "dest")
        paymentComment = data.get("manualPaymentComment", None)
        refundComment = data.get("manualRefundComment", None)

        logger.info(
            f"ApiExchangeRooms [{src.orderCode}-{dst.orderCode}]: Got from req  src={src}  dst={dst}"
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
                ordA.verifyCancelation()
                ordA.verifyPaymentsRefundsStatus()
                ordB = SideInstance(ordBdata, request)
                ordB.verifyCancelation()
                ordB.verifyPaymentsRefundsStatus()
                logger.debug(f"ApiExchangeRooms [{src.orderCode}-{dst.orderCode}]: Loaded instances and verified payments/refunds")

                rootPosA: OrderPosition = ordA.rootPosition
                rootPosB: OrderPosition = ordB.rootPosition

                for idx in range(len(data["exchanges"])):
                    posA: Element = ordA.instance(idx)
                    posB: Element = ordB.instance(idx)
                    logger.debug(f"ApiExchangeRooms [{src.orderCode}-{dst.orderCode}]: Exchanging pair idx={idx} "
                                 f"posA={posA.pos.pk if posA is not None else 'None'} "
                                 f"posB={posB.pos.pk if posB is not None else 'None'}")
                    balance += exchange(posA, posB, rootPosA, rootPosB, ordA.ocm, ordB.ocm)
                logger.debug(f"ApiExchangeRooms [{src.orderCode}-{dst.orderCode}]: Exchanges done")

                fixPaymentStatus(balance.balanceA, ordA.order, refundComment, paymentComment, request, {"order": ordA.order, "event": request.event})
                fixPaymentStatus(balance.balanceB, ordB.order, refundComment, paymentComment, request, {"order": ordB.order, "event": request.event})
                logger.debug(f"ApiExchangeRooms [{src.orderCode}-{dst.orderCode}]: Payment status fixed")

                ordA.ocm.fz_enable_locking = False
                ordB.ocm.fz_enable_locking = False
                ordA.ocm.commit(check_quotas=False)
                ordB.ocm.commit(check_quotas=False)

        except FzException as fe:
            status_code = fe.code if fe.code is not None else status.HTTP_400_BAD_REQUEST
            return JsonResponse(fe.extraData, status=status_code)

        logger.info(
            f"ApiExchangeRooms [{src.orderCode}-{dst.orderCode}]: Success"
        )

        return HttpResponse("")


def fixPaymentStatus(balance: int, order: Order, refundComment: str, paymentComment: str, request, orderContext):
    amount = serializers.DecimalField(max_digits=13, decimal_places=2).to_internal_value(str(abs(balance)))
    dateNow = serializers.DateTimeField().to_internal_value(now())

    if balance < 0:
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
    elif balance > 0:
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
                # membership=rootPosition.membership,
                valid_from=src.pos.valid_from,
                valid_until=src.pos.valid_until,
                is_bundled=False
            )
            return src.price
        else:
            ocmDest.change_item(dest.pos, src.item, src.itemVar)
            ocmDest.change_price(dest.pos, src.price)
            if src.pos.subevent is not None:
                ocmDest.change_subevent(dest.pos, src.pos.subevent)
            if src.pos.seat is not None:
                ocmDest.change_seat(dest.pos, src.pos.seat)
            if src.pos.valid_from is not None:
                ocmDest.change_valid_from(dest.pos, src.pos.valid_from)
            if src.pos.valid_until is not None:
                ocmDest.change_valid_until(dest.pos, src.pos.valid_until)
            # Currently we cannot change the bundle status
            return src.price - dest.paid


def exchange(a: Element, b: Element,
             rootPositionA: OrderPosition, rootPositionB: OrderPosition,
             ocmA: FzOrderChangeManager, ocmB: FzOrderChangeManager) -> Balance:
    # Cannot be an addon of itself
    if a is not None and a.pos.pk == rootPositionA.pk:
        rootPositionA = None
    if b is not None and b.pos.pk == rootPositionB.pk:
        rootPositionB = None
    balB = transfer(a, b, rootPositionB, ocmB)
    balA = transfer(b, a, rootPositionA, ocmA)
    return Balance(balA, balB)


def strCmp(x, y):
    if len(x) > len(y):
        return x
    if len(x) == len(y):
        return min(x, y)
    else:
        return y
