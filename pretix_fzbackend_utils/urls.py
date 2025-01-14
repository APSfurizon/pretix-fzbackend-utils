from django.urls import re_path

from .views import FznackendutilsSettings

from pretix.api.urls import event_router

urlpatterns = [
    re_path(r'^control/event/(?P<organizer>[^/]+)/(?P<event>[^/]+)/fzbackendutils/settings$',
            FznackendutilsSettings.as_view(), name='settings'),

    event_url(r'^fzbackendutils/api/set-item-bundle$',
            FznackendutilsSettings.as_view(), name='set-item-bundle'),
]
