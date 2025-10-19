from django.urls import include, path, re_path

from .general_views import ApiSetItemBundle, FznackendutilsSettings
from .views.convert_ticket_only import ApiConvertTicketOnlyOrder
from .views.exchange_rooms import ApiExchangeRooms
from .views.transfer_order import ApiTransferOrder

urlpatterns = [
    re_path(
        r"^control/event/(?P<organizer>[^/]+)/(?P<event>[^/]+)/fzbackendutils/settings$",
        FznackendutilsSettings.as_view(),
        name="settings",
    ),
]

event_patterns = [
    re_path(
        r"^fzbackendutils/api/",
        include(
            [
                path(
                    "set-item-bundle/",
                    ApiSetItemBundle.as_view(),
                    name="set-item-bundle",
                ),
                path(
                    "convert-ticket-only-order/",
                    ApiConvertTicketOnlyOrder.as_view(),
                    name="convert-ticket-only-order",
                ),
                path(
                    "transfer-order/",
                    ApiTransferOrder.as_view(),
                    name="transfer-order",
                ),
                path(
                    "exchange-rooms/",
                    ApiExchangeRooms.as_view(),
                    name="exchange-rooms",
                ),
            ]
        ),
    ),
]
