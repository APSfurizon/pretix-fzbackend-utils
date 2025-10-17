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
            
        orderCode = data["orderCode"]
        positionId = data["positionId"]
        questionId = data["questionId"]
        newUserId = data["newUserId"]
        comment = data.get("manualPaymentComment", "")
        
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
                
                # Prevent refunds so admin CANNOT refund the wrong owner
                totalPaid = 0
                # Already ordered in the Meta class of OrderPayment/Refund. Order is important for deadlock prevention
                payments: List[OrderPayment] = OrderPayment.objects.select_for_update(of=OF_SELF).filter(order__pk=order.pk, state__in=[OrderPayment.PAYMENT_STATE_CONFIRMED, OrderPayment.PAYMENT_STATE_CREATED, OrderPayment.PAYMENT_STATE_PENDING])
                for payment in payments:
                    if payment.state != OrderPayment.PAYMENT_STATE_CONFIRMED:
                        logger.error(
                            f"ApiTransferOrder [{orderCode}]: Payment {payment.pk}: invalid state {payment.state}"
                        )
                        raise FzException("", extraData={"error": f'Payment {payment.pk} is in invalid state {payment.state}'})
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
                            f"ApiTransferOrder [{orderCode}]: Refund {refund.pk}: invalid state {refund.state}"
                        )
                        raise FzException("", extraData={"error": f'Refund {refund.pk} is in invalid state {refund.state}'})
                    totalPaid -= refund.amount
                
                # It's enough to mark payment as refunded. However this may seem an inconsistent state (order paid with no valid payments),
                # so we create a refund and a payment objects as well

                refundSerializer = OrderRefundCreateSerializer(data=request.data, context=self.get_serializer_context())
                refundSerializer.is_valid(raise_exception=True)
                refundSerializer.save()

                # Create the new payment to compensate of the refunded ones
                paymentData = {
                    "state": OrderPayment.PAYMENT_STATE_PENDING,
                    "amount": serializers.DecimalField(max_digits=13, decimal_places=2).to_internal_value(str(totalPaid)),
                    "payment_date": serializers.DateTimeField().to_internal_value(now()),
                    "sendEmail": False,
                    "provider": FZ_MANUAL_PAYMENT_PROVIDER_IDENTIFIER,
                    "info": {
                        "issued_by": FZ_MANUAL_PAYMENT_PROVIDER_ISSUER,
                        "comment": comment
                    }
                }                
                paymentSerializer = OrderPaymentCreateSerializer(data=paymentData, context={"order": order, **CONTEXT})
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
                
                tickets.invalidate_cache.apply_async(kwargs={'event': request.event.pk, 'order': order.pk})
                #order_modified.send(sender=request.event, order=order) # Already sent inside OCM
        except FzException as fe:
            return JsonResponse(fe.extraData, status=status.HTTP_412_PRECONDITION_FAILED)
        
        logger.info(
            f"ApiTransferOrder [{orderCode}]: Success"
        )

        return HttpResponse("")