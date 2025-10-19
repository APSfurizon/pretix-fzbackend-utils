import logging
from collections import namedtuple
from datetime import datetime
from decimal import Decimal
from pretix.base.models import Item, ItemVariation, Membership, OrderPosition, Seat
from pretix.base.models.event import SubEvent
from pretix.base.models.tax import TaxedPrice, TaxRule
from pretix.base.services.orders import OrderChangeManager, OrderError, error_messages
from pretix.base.services.pricing import get_price

logger = logging.getLogger(__name__)


class FzOrderChangeManager(OrderChangeManager):
    NopOperation = namedtuple('ItemOperation', ())

    fz_enable_locking = True

    # If fz_enable_locking is set to False, the caller takes responsability for calling `lock_objects([event])` once per transaction
    def _create_locks(self):
        if self.fz_enable_locking:
            super()._create_locks()

    def nopOperation(self):
        self._operations.append(self.NopOperation())

    # Like add_position, but without addon hierarchy validation
    def add_position_no_addon_validation(self, item: Item, variation: ItemVariation, price: Decimal, addon_to: OrderPosition = None,
                                         subevent: SubEvent = None, seat: Seat = None, membership: Membership = None,
                                         valid_from: datetime = None, valid_until: datetime = None, is_bundled: bool = False):
        if isinstance(seat, str):
            if not seat:
                seat = None
            else:
                try:
                    seat = Seat.objects.get(
                        event=self.event,
                        subevent=subevent,
                        seat_guid=seat
                    )
                except Seat.DoesNotExist:
                    raise OrderError(error_messages['seat_invalid'])

        try:
            if price is None:
                price = get_price(item, variation, subevent=subevent, invoice_address=self._invoice_address)
            elif not isinstance(price, TaxedPrice):
                price = item.tax(price, base_price_is='gross', invoice_address=self._invoice_address,
                                 force_fixed_gross_price=True)
        except TaxRule.SaleNotAllowed:
            raise OrderError(self.error_messages['tax_rule_country_blocked'])

        if price is None:
            raise OrderError(self.error_messages['product_invalid'])
        if item.variations.exists() and not variation:
            raise OrderError(self.error_messages['product_without_variation'])
        if self.order.event.has_subevents and not subevent:
            raise OrderError(self.error_messages['subevent_required'])

        seated = item.seat_category_mappings.filter(subevent=subevent).exists()
        if seated and not seat and self.event.settings.seating_choice:
            raise OrderError(self.error_messages['seat_required'])
        elif not seated and seat:
            raise OrderError(self.error_messages['seat_forbidden'])
        if seat and subevent and seat.subevent_id != subevent.pk:
            raise OrderError(self.error_messages['seat_subevent_mismatch'].format(seat=seat.name))

        new_quotas = (variation.quotas.filter(subevent=subevent)
                      if variation else item.quotas.filter(subevent=subevent))
        if not new_quotas:
            raise OrderError(self.error_messages['quota_missing'])

        if self.order.event.settings.invoice_include_free or price.gross != Decimal('0.00'):
            self._invoice_dirty = True

        self._totaldiff += price.gross
        self._quotadiff.update(new_quotas)
        if seat:
            self._seatdiff.update([seat])
        self._operations.append(
            self.AddOperation(
                item,
                variation,
                price,
                addon_to,
                subevent,
                seat,
                membership,
                valid_from,
                valid_until,
                is_bundled
            )
        )
