from typing import List

import logging
from django.db import transaction
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
    Order,
    OrderPayment,
    OrderRefund,
    Question,
    QuestionAnswer,
)
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
    STATUS_CODE_REFUND_INVALID,
    verifyToken,
)

logger = logging.getLogger(__name__)


@method_decorator(xframe_options_exempt, "dispatch")
@method_decorator(csrf_exempt, "dispatch")
class ApiTransferOrder(APIView, View):
    permission = "can_change_orders"

    def post(self, request, organizer, event, *args, **kwargs):
        verifyToken(request)
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
        if "manualPaymentComment" in data and data["manualPaymentComment"] and not isinstance(data["manualPaymentComment"], str):
            return JsonResponse(
                {"error": 'Invalid parameter "manualPaymentComment"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "manualRefundComment" in data and data["manualRefundComment"] and not isinstance(data["manualRefundComment"], str):
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
                    raise FzException("", extraData={"error": f'Question {questionId} is not of type number'}, code=status.HTTP_400_BAD_REQUEST)
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
                payments: List[OrderPayment] = OrderPayment.objects.select_for_update(of=OF_SELF).filter(order__pk=order.pk, state__in=[
                    OrderPayment.PAYMENT_STATE_CONFIRMED,
                    OrderPayment.PAYMENT_STATE_CREATED,
                    OrderPayment.PAYMENT_STATE_PENDING
                ])
                for payment in payments:
                    if payment.state != OrderPayment.PAYMENT_STATE_CONFIRMED:
                        logger.error(
                            f"ApiTransferOrder [{orderCode}]: Payment {payment.full_id}: invalid state {payment.state}"
                        )
                        raise FzException("", extraData={"error": f'Payment {payment.full_id} is in invalid state {payment.state}'},
                                          code=STATUS_CODE_PAYMENT_INVALID)
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
                refunds: List[OrderRefund] = OrderRefund.objects.select_for_update(of=OF_SELF).filter(order__pk=order.pk, state__in=[
                    OrderRefund.REFUND_STATE_CREATED,
                    OrderRefund.REFUND_STATE_TRANSIT
                ])
                for refund in refunds:
                    logger.error(
                        f"ApiTransferOrder [{orderCode}]: Refund {refund.full_id}: invalid state {refund.state}"
                    )
                    raise FzException("", extraData={"error": f'Refund {refund.full_id} is in invalid state {refund.state}'},
                                        code=STATUS_CODE_REFUND_INVALID)

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
                # tickets.invalidate_cache.apply_async(kwargs={'event': request.event.pk, 'order': order.pk})
                # order_modified.send(sender=request.event, order=order)
        except FzException as fe:
            status_code = fe.code if fe.code is not None else status.HTTP_400_BAD_REQUEST
            return JsonResponse(fe.extraData, status=status_code)

        logger.info(
            f"ApiTransferOrder [{orderCode}]: Success"
        )

        return HttpResponse("")
