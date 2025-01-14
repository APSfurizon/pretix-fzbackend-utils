import re
import logging
from django import forms
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from pretix.base.forms import SettingsForm
from pretix.base.models import Event
from django.utils.decorators import method_decorator
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views import View
from django.http import HttpResponse, JsonResponse
from pretix.base.models import Event, OrderPosition
from pretix.control.views.event import EventSettingsFormView, EventSettingsViewMixin
from django.shortcuts import get_object_or_404
from rest_framework import viewsets
from rest_framework.response import Response
import json

logger = logging.getLogger(__name__)


class FznackendutilsSettingsForm(SettingsForm):
    fzbackendutils_redirect_url = forms.RegexField(
        label=_("Order redirect url"),
        help_text=_("When an user has done, has modified or has paid an order, pretix will redirect him to this spacified url, "
                    "with the order code and secret appended as query parameters (<code>?c={orderCode}&s={orderSecret}&m={statusMessages}</code>). "
                    "This page should call <code>/api/v1/orders-workflow/link-order</code> of the backend to link this order "
                    "to the logged in user."),
        required=False,
        widget=forms.TextInput,
        regex=re.compile(r'^(https://.*/.*|http://localhost[:/].*)*$')
    )


class FznackendutilsSettings(EventSettingsViewMixin, EventSettingsFormView):
    model = Event
    form_class = FznackendutilsSettingsForm
    template_name = 'pretix_fzbackend_utils/settings.html'
    permission = 'can_change_settings'

    def get_success_url(self) -> str:
        return reverse('plugins:pretix_fzbackend_utils:settings', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug
        })


@method_decorator(xframe_options_exempt, "dispatch")
class ApiViewSet(viewsets.ViewSet):
    permission = 'can_view_orders'
    write_permission = 'can_change_orders'

    def set_is_bundle(self, request):
        data = json.loads(request.body)
        logger.info(f"Backend is trying to set is_bundle for position {data['position']} to {data['bundle']}")

        if 'position' not in data or 'bundle' not in data:
            return JsonResponse({'error': 'Missing parameters'}, status=400)
        if data['bundle'] is not True and data['bundle'] is not False and not isinstance(data['bundle'], bool):
            return JsonResponse({'error': 'Invalid bundle value'}, status=400)

        position = get_object_or_404(OrderPosition.objects.filter(id=data['position']))

        position.is_bundled = data['bundle']
        position.save(update_fields=['is_bundled'])
        logger.info(f"Backend successfully set is_bundle for position {data['position']} to {data['bundle']}")

        return HttpResponse('')
