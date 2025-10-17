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

# curl 127.0.0.1:8000/suca/testBackend/fzbackendutils/api/convert-ticket-only-order/ -H "Authorization: Token TOKEN" -H "Content-Type: application/json" -X Post --data '{"orderCode": "J0SN9", "rootPositionId": 99, "newRootItemId": 17}'
@method_decorator(xframe_options_exempt, "dispatch")
@method_decorator(csrf_exempt, "dispatch")
class ApiConvertTicketOnlyOrder(APIView, View):
    permission = "can_change_orders"
    def post(self, request, organizer, event, *args, **kwargs):
        #TODO header check        
        #907E7
        
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
        if "newRootItemVariationId" in data and not isinstance(data["newRootItemVariationId"], int):
            return JsonResponse(
                {"error": 'Invalid parameter "newRootItemVariationId"'}, status=status.HTTP_400_BAD_REQUEST
            )
        
        orderCode = data["orderCode"]
        currentRootPositionId = data["rootPositionId"]
        newRootItemId = data["newRootItemId"]
        newRootItemVariationId = data.get("newRootItemVariationId", None)
        
        logger.info(
            f"ApiConvertTicketOnlyOrder [{orderCode}]: Got from req rootPosId={currentRootPositionId} newRootItemId={newRootItemId} newRootItemVariationId={newRootItemVariationId}"
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
                f"ApiConvertTicketOnlyOrder [{orderCode}]: Fetched current rootItem={rootItem.pk} rootItemVariation={rootItemVariation.pk if rootItemVariation else None}"
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
                #membership=rootPosition.membership,
                valid_from=rootPosition.valid_from,
                valid_until=rootPosition.valid_until,
                is_bundled=True # IMPORTANT!
            )
            ocm.change_item(
                position=rootPosition,
                item=newRootItem,
                variation=newRootItemVariation
            )
            ocm.change_price(
                position=rootPosition,
                price=0 #newRootItem.default_price if newRootItemVariation is None else newRootItemVariation.default_price
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
            order_modified.send(sender=request.event, order=order) # Sadly signal has to be sent twice: One after changing the extra info, and one inside ocm


        logger.info(
            f"ApiConvertTicketOnlyOrder [{orderCode}]: Success"
        )

        return HttpResponse("")


# curl 127.0.0.1:8000/suca/testBackend/fzbackendutils/api/transfer-order/ -H "Authorization: Token AAAAAAA" -H "Content-Type: application/json" -X Post --data '{"orderCode": "J0SN9", "manualPaymentComment": "stocazzo", "positionId": 109, "questionId": 3, "newUserId": 121, "manualRefundComment": "staminchia"}'
@method_decorator(xframe_options_exempt, "dispatch")
@method_decorator(csrf_exempt, "dispatch")
class ApiTransferOrder(APIView, View):
    permission = "can_change_orders"
    def post(self, request, organizer, event, *args, **kwargs):
        data = request.data
        
        if "orderCode" not in data or not isinstance(data["orderCode"], str):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "orderCode"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "positionId" not in data or not isinstance(data["positionId"], int):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "positionId"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "questionId" not in data or not isinstance(data["questionId"], int):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "questionId"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "newUserId" not in data or not isinstance(data["newUserId"], int):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "newUserId"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "manualPaymentComment" in data and not isinstance(data["manualPaymentComment"], str):
            return JsonResponse(
                {"error": 'Invalid parameter "manualPaymentComment"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "manualRefundComment" in data and not isinstance(data["manualRefundComment"], str):
            return JsonResponse(
                {"error": 'Invalid parameter "manualRefundComment"'}, status=status.HTTP_400_BAD_REQUEST
            )
            
        orderCode = data["orderCode"]
        positionId = data["positionId"]
        questionId = data["questionId"]
        newUserId = data["newUserId"]
        paymentComment = data.get("manualPaymentComment", None)
        refundComment = data.get("manualRefundComment", None)
        
        logger.info(
            f"ApiTransferOrder [{orderCode}]: Got from req posId={positionId} qId={questionId} newUserId={newUserId}"
        )

        CONTEXT = {"event": request.event, "pdf_data": False, "check_quotas": False}
        
        try:
            with transaction.atomic():
                # Actually change the answer
                answer: QuestionAnswer = get_object_or_404(
                    QuestionAnswer.objects.select_for_update(of=OF_SELF).filter(
                        question__pk=questionId,
                        orderposition__pk=positionId,
                        orderposition__order__code=orderCode,
                        orderposition__order__event=request.event,
                        orderposition__order__event__organizer=request.organizer
                    )
                )
                if answer.question.type != Question.TYPE_NUMBER:
                    raise FzException("", extraData={"error": f'Question {questionId} is not of type number'})
                # Same as AnswerSerializer for numeric fields
                answer.answer = serializers.DecimalField(max_digits=50, decimal_places=1).to_internal_value(newUserId)
                answer.save(update_fields=["answer"])

                order: Order = get_object_or_404(
                    Order.objects.select_for_update(of=OF_SELF).filter(event=request.event, code=orderCode, event__organizer=request.organizer)
                )
                order.log_action(
                    'pretix.event.order.modified',
                    user=self.request.user,
                    auth=self.request.auth,
                    data={
                        'data': [
                            {
                                "position": answer.orderposition.pk,
                                f'question_{answer.question.pk}': answer.answer
                            }
                        ]
                    }
                )
                logger.debug(f"ApiTransferOrder [{orderCode}]: Answer updated")
                
                # Prevent refunds so admin CANNOT refund the wrong owner
                totalPaid = 0
                # Already ordered in the Meta class of OrderPayment/Refund. Order is important for deadlock prevention
                payments: List[OrderPayment] = OrderPayment.objects.select_for_update(of=OF_SELF).filter(order__pk=order.pk, state__in=[OrderPayment.PAYMENT_STATE_CONFIRMED, OrderPayment.PAYMENT_STATE_CREATED, OrderPayment.PAYMENT_STATE_PENDING])
                for payment in payments:
                    if payment.state != OrderPayment.PAYMENT_STATE_CONFIRMED:
                        logger.error(
                            f"ApiTransferOrder [{orderCode}]: Payment {payment.full_id}: invalid state {payment.state}"
                        )
                        raise FzException("", extraData={"error": f'Payment {payment.full_id} is in invalid state {payment.state}'})
                    payment.state = OrderPayment.PAYMENT_STATE_REFUNDED
                    payment.save(update_fields=["state"])
                    order.log_action(
                        'pretix.event.order.payment.refunded', {
                            'local_id': payment.local_id,
                            'provider': payment.provider,
                        },
                        user=request.user if request.user.is_authenticated else None,
                        auth=request.auth
                    )
                    totalPaid += payment.amount
                refunds: List[OrderRefund] = OrderRefund.objects.select_for_update(of=OF_SELF).filter(order__pk=order.pk, state__in=[OrderRefund.REFUND_STATE_CREATED, OrderRefund.REFUND_STATE_TRANSIT, OrderRefund.REFUND_STATE_DONE, OrderRefund.REFUND_STATE_EXTERNAL])
                for refund in refunds:
                    if refund.state in [OrderRefund.REFUND_STATE_CREATED, OrderRefund.REFUND_STATE_TRANSIT]:
                        logger.error(
                            f"ApiTransferOrder [{orderCode}]: Refund {refund.full_id}: invalid state {refund.state}"
                        )
                        raise FzException("", extraData={"error": f'Refund {refund.full_id} is in invalid state {refund.state}'})
                    totalPaid -= refund.amount
                    
                orderContext = {"order": order, **CONTEXT}
                
                logger.debug(f"ApiTransferOrder [{orderCode}]: Payments marked as refunded")
                
                # It's enough to mark payment as refunded. However this may seem an inconsistent state (order paid with no valid payments),
                # so we create a refund and a payment objects as well
                amount = serializers.DecimalField(max_digits=13, decimal_places=2).to_internal_value(str(totalPaid))
                dateNow = serializers.DateTimeField().to_internal_value(now())
                
                # Perform refund
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
                logger.debug(f"ApiTransferOrder [{orderCode}]: Refund created")

                # Create the new payment to compensate of the refunded ones
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
                    user=self.request.user if self.request.user.is_authenticated else None,
                    auth=self.request.auth,
                    count_waitinglist=False,
                    ignore_date=True,
                    force=True,
                    send_mail=False,
                )
                logger.debug(f"ApiTransferOrder [{orderCode}]: Payment created")
                
                # Let OCM update the internal fields of the order
                ocm = FzOrderChangeManager(
                    order=order,
                    user=self.request.user if self.request.user.is_authenticated else None,
                    auth=request.auth,
                    notify=False,
                    reissue_invoice=False,
                )
                ocm.nopOperation()
                ocm.commit()
                logger.debug(f"ApiTransferOrder [{orderCode}]: OCM nop")
                
                # Both already done inside ocm
                #tickets.invalidate_cache.apply_async(kwargs={'event': request.event.pk, 'order': order.pk})
                #order_modified.send(sender=request.event, order=order)
        except FzException as fe:
            return JsonResponse(fe.extraData, status=status.HTTP_412_PRECONDITION_FAILED)
        
        logger.info(
            f"ApiTransferOrder [{orderCode}]: Success"
        )

        return HttpResponse("")
    
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
            return ApiExchangeRooms.Balance(self.balanceA + o.balanceA, self.balanceB + o.balanceB)

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
        
    class SideInstance:
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
