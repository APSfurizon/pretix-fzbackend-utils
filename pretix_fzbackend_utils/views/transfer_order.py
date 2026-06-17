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
    OrderCreateSerializer,
    Question,
)
from pretix.base.models import (
    Item,
    Order,
    OrderFee,
    OrderPosition,
    QuestionAnswer,
    OrderPayment,
    OrderRefund,
)
from pretix.helpers import OF_SELF
from rest_framework import serializers, status
from rest_framework.views import APIView

from pretix.base.signals import (
    order_paid, order_placed    
)

from pretix.base.i18n import language
from pretix.base.services.orders import cancel_order

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
logger.setLevel(logging.DEBUG)


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
        if "membershipCardItemId" not in data or not isinstance(data["membershipCardItemId"], int):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "membershipCardItemId"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "userIdQuestionId" not in data or not isinstance(data["userIdQuestionId"], int):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "userIdQuestionId"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "newUserId" not in data or not isinstance(data["newUserId"], int):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "newUserId"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "newEmail" not in data or not isinstance(data["newEmail"], str):
            return JsonResponse(
                {"error": 'Missing or invalid parameter "newEmail"'}, status=status.HTTP_400_BAD_REQUEST
            )
        if "cancelationComment" in data and data["cancelationComment"] and not isinstance(data["cancelationComment"], str):
            return JsonResponse(
                {"error": 'Invalid parameter "cancelationComment"'}, status=status.HTTP_400_BAD_REQUEST
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
        membershipCardItemId = data["membershipCardItemId"]
        membershipCardNeededForNewUser = True
        userIdQuestionId = data["userIdQuestionId"]
        newUserId = data["newUserId"]
        newEmail = data["newEmail"]
        ticketItemIds = []
        name = None
        street = None
        zipcode = None
        city = None
        country = None
        state = None
        cancelationComment = data.get("cancelationComment", None)
        paymentComment = data.get("manualPaymentComment", None)
        refundComment = data.get("manualRefundComment", None)

        #logger.info(
        #    f"ApiTransferOrder [{orderCode}]: Got from req posId={positionId} qId={questionId} newUserId={newUserId}"
        #)

        CONTEXT = {"event": request.event, "pdf_data": False, "check_quotas": False, "auth": request.auth}

        try:
            with transaction.atomic():
                membershipCardItem = get_object_or_404(
                    Item.objects.filter(event=request.event, id=membershipCardItemId)
                )
                sourceOrder: Order = get_object_or_404(
                    Order.objects.select_for_update(of=OF_SELF).filter(event=request.event, code=orderCode, event__organizer=request.organizer)
                )
                sourcePositions = sourceOrder.positions.all()
                sourceFees = sourceOrder.fees.all()
                
                # FIRST CREATES THE NEW ORDER FOR THE DEST USER
                
                # Copy positions and answers
                
                #First copy run
                basePositions = []
                positionIdToAddons = {}
                position: OrderPosition
                for position in sourcePositions:
                    if (position.item_id == membershipCardItemId):
                        continue # Skip copying membership cards
                    
                    answers = position.answers.all()
                    newAnswers = []
                    answer: QuestionAnswer
                    for answer in answers:
                        newAnswer = {
                            "question": answer.question_id,
                            "answer": answer.answer,
                            "options": [option.identifier for option in answer.options.all()] if answer.options else None
                        }
                        if (answer.question_id == userIdQuestionId):
                            if answer.question.type != Question.TYPE_NUMBER:
                                raise FzException("", extraData={"error": f'Question {userIdQuestionId} is not of type number'}, code=status.HTTP_400_BAD_REQUEST)
                            newAnswer["answer"] = serializers.DecimalField(max_digits=50, decimal_places=1).to_internal_value(newUserId)
                            newAnswer["options"] = None
                        newAnswers.append(newAnswer)
                    
                    
                    newPos = {
                        "positionid": None, # Filled later
                        "item": position.item_id,
                        "variation": position.variation_id if position.variation else None,
                        "price": position.price,
                        "seat": position.seat.seat_guid if position.seat else None,
                        "attendee_name": position.attendee_name,
                        #"voucher": position.voucher.id if position.voucher else None,
                        "attendee_email": position.attendee_email,
                        "company": position.company,
                        "street": position.street,
                        "zipcode": position.zipcode,
                        "city": position.city,
                        "country": position.country,
                        "state": position.state,
                        #"secret": position.secret,
                        "subevent": position.subevent_id if position.subevent else None,
                        "valid_from": position.valid_from,
                        "valid_until": position.valid_until,
                        "discount": position.discount,
                        "answers": newAnswers
                    }
                    
                    addon = position.addon_to_id if position.addon_to else None
                    if (addon is None):
                        basePositions.append(newPos)
                    else:
                        l = positionIdToAddons[addon]
                        if l is None:
                            l = []
                            positionIdToAddons[addon] = l
                        l.append(newPos)
                #Adjust positionid and positions
                posId = 1
                newPositions = []
                for pos in basePositions:
                    pos["positionid"] = posId
                    basePosId = posId
                    posId += 1
                    newPositions.append(pos)
                    if (posId in positionIdToAddons):
                        for addonPos in positionIdToAddons[posId]:
                            addonPos["addon_to"] = basePosId
                            addonPos["positionid"] = posId
                            posId += 1
                            newPositions.append(addonPos)
                
                newFees = []
                fee: OrderFee
                for fee in sourceFees:
                    newFee = {
                        "fee_type": fee.fee_type,
                        "value": fee.value,
                        "internal_type": fee.internal_type,
                        "tax_rule": fee.tax_rule_id if fee.tax_rule else None,
                        "description": fee.description,
                    }
                    newFees.append(newFee)
                
                # CREATE NEW ORDER
                orderData = {
                    "status": "p", #paid
                    "email": newEmail,
                    "invoice_address": {
                        "name": name,
                        "street": street,
                        "zipcode": zipcode,
                        "city": city,
                        "country": country,
                        "state": state
                    },
                    "force": True,
                    "send_email": False,
                    "positions": newPositions,
                    "fees": newFees,
                    "payment_provider": FZ_MANUAL_PAYMENT_PROVIDER_IDENTIFIER,
                    "payment_info": {
                        "issued_by": FZ_MANUAL_PAYMENT_PROVIDER_ISSUER,
                        "comment": paymentComment
                    }
                }
                # Actually create the order. Code taken from the create order api endpoint
                createOrderSerializer = OrderCreateSerializer(data = orderData, context=CONTEXT)
                createOrderSerializer.is_valid(raise_exception=True)
                createOrderSerializer.save()
                newOrder: Order = createOrderSerializer.instance
                newOrder.log_action(
                    'pretix.event.order.placed',
                    user=request.user if request.user.is_authenticated else None,
                    auth=request.auth,
                )
                with language(newOrder.locale, self.request.event.settings.region):
                    payment = newOrder.payments.last()
                    # OrderCreateSerializer creates at most one payment
                    if payment and payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
                        newOrder.log_action(
                            'pretix.event.order.payment.confirmed', {
                                'local_id': payment.local_id,
                                'provider': payment.provider,
                            },
                            user=request.user if request.user.is_authenticated else None,
                            auth=request.auth,
                        )
                    order_placed.send(self.request.event, order=newOrder, bulk=False)
                    if newOrder.status == Order.STATUS_PAID:
                        order_paid.send(self.request.event, order=newOrder)
                        newOrder.log_action(
                            'pretix.event.order.paid',
                            {
                                'provider': payment.provider if payment else None,
                                'info': {},
                                'date': now().isoformat(),
                                'force': False
                            },
                            user=request.user if request.user.is_authenticated else None,
                            auth=request.auth,
                        )
                logger.debug(f"ApiTransferOrder [{orderCode}]: New order created for user {newUserId} with code {newOrder.code}")
                # If users needs a membership card, we add it there
                if membershipCardNeededForNewUser:
                    pos: OrderPosition
                    for pos in newOrder.positions.all():
                        if (pos.item_id in ticketItemIds):
                            ocm = FzOrderChangeManager(
                                    order=newOrder,
                                    user=self.request.user if self.request.user.is_authenticated else None,
                                    auth=request.auth,
                                    notify=False,
                                    reissue_invoice=True,
                                )
                            ocm.add_position_no_addon_validation(item=membershipCardItem, variation=None, price=membershipCardItem.default_price, addon_to=pos)
                            ocm.commit()
                            logger.debug(f"ApiTransferOrder [{orderCode}]: Membership card added to new order {newOrder.code} for user {newUserId}")
                            break
                
                # FIX PAYMENTS ON SOURCE ORDER

                # Prevent refunds so admin CANNOT refund the wrong owner
                totalPaid = 0
                # Already ordered in the Meta class of OrderPayment/Refund. Order is important for deadlock prevention
                payments: List[OrderPayment] = OrderPayment.objects.select_for_update(of=OF_SELF).filter(order__pk=sourceOrder.pk, state__in=[
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
                    sourceOrder.log_action(
                        'pretix.event.order.payment.refunded', {
                            'local_id': payment.local_id,
                            'provider': payment.provider,
                        },
                        user=request.user if request.user.is_authenticated else None,
                        auth=request.auth
                    )
                    totalPaid += payment.amount
                refunds: List[OrderRefund] = OrderRefund.objects.select_for_update(of=OF_SELF).filter(order__pk=sourceOrder.pk, state__in=[
                    OrderRefund.REFUND_STATE_CREATED,
                    OrderRefund.REFUND_STATE_TRANSIT
                ])
                for refund in refunds:
                    logger.error(
                        f"ApiTransferOrder [{orderCode}]: Refund {refund.full_id}: invalid state {refund.state}"
                    )
                    raise FzException("", extraData={"error": f'Refund {refund.full_id} is in invalid state {refund.state}'},
                                      code=STATUS_CODE_REFUND_INVALID)

                orderContext = {"order": sourceOrder, **CONTEXT}

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
                sourceOrder.log_action(
                    'pretix.event.order.refund.created', {
                        'local_id': newRefund.local_id,
                        'provider': newRefund.provider,
                    },
                    user=request.user if request.user.is_authenticated else None,
                    auth=request.auth
                )
                sourceOrder.log_action(
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
                sourceOrder.log_action(
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
                    order=sourceOrder,
                    user=self.request.user if self.request.user.is_authenticated else None,
                    auth=request.auth,
                    notify=False,
                    reissue_invoice=False,
                )
                ocm.recomputeOperation()
                ocm.commit()
                logger.debug(f"ApiTransferOrder [{orderCode}]: OCM recompute")

                # Both already done inside ocm
                # tickets.invalidate_cache.apply_async(kwargs={'event': request.event.pk, 'order': order.pk})
                # order_modified.send(sender=request.event, order=order)
                
                # Cancel order with paid fee
                cancel_order(
                    self.order.pk,
                    user=self.request.user,
                    email_comment=cancelationComment,
                    send_mail=False,
                    cancel_invoice=False,
                    cancellation_fee=sourceOrder.total
                )
                logger.debug(f"ApiTransferOrder [{orderCode}]: Order canceled with paid fee ({sourceOrder.total})")
        except FzException as fe:
            status_code = fe.code if fe.code is not None else status.HTTP_400_BAD_REQUEST
            return JsonResponse(fe.extraData, status=status_code)

        logger.info(
            f"ApiTransferOrder [{orderCode}]: Success"
        )

        return HttpResponse("")
