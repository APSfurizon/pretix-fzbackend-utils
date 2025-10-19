import logging
from decimal import Decimal
from django.http import HttpRequest
from django.template.loader import get_template
from django.utils.translation import gettext_lazy as _
from pretix.base.models import Order, OrderPayment
from pretix.base.payment import ManualPayment

logger = logging.getLogger(__name__)

FZ_MANUAL_PAYMENT_PROVIDER_IDENTIFIER = "fzbackend-manual"
FZ_MANUAL_PAYMENT_PROVIDER_ISSUER = "fz-backend"


class FzbackendManualPaymentProvider(ManualPayment):
    identifier = FZ_MANUAL_PAYMENT_PROVIDER_IDENTIFIER
    verbose_name = _("FzBackendUtils Manual payment")
    public_name = _("FzBackendUtils - Manual payment")

    def is_implicit(self, request: HttpRequest):
        return "pretix_fzbackend_utils" not in self.event.plugins

    def is_allowed(self, request: HttpRequest, total: Decimal = None):
        return "pretix_fzbackend_utils" in self.event.plugins and super().is_allowed(
            request, total
        )

    def order_change_allowed(self, order: Order):
        return (
            "pretix_fzbackend_utils" in self.event.plugins
            and super().order_change_allowed(order)
        )

    def payment_control_render(
        self, request: HttpRequest, payment: OrderPayment
    ) -> str:
        if payment.provider != self.identifier or "comment" not in payment.info_data:
            return ""
        template = get_template("pretix_fzbackend_utils/control.html")
        ctx = {
            "request": request,
            "event": self.event,
            "comment": payment.info_data["comment"],
        }
        return template.render(ctx)
