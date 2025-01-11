from django.urls import re_path

from .views import FznackendutilsSettings

urlpatterns = [
    re_path(r'^control/event/(?P<organizer>[^/]+)/(?P<event>[^/]+)/fzbackendutils/settings$',
            FznackendutilsSettings.as_view(), name='settings'),
]
