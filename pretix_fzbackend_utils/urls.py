from django.urls import include, path, re_path

from .views import ApiConvertTicketOnlyOrder, ApiTransferOrder, ApiSetItemBundle, FznackendutilsSettings

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
            ]
        ),
    ),
]
