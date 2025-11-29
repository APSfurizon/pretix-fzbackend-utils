from pretix.base.settings import GlobalSettingsObject
from django.http import Http404 

STATUS_CODE_POSITION_CANCELED=461
STATUS_CODE_PAYMENT_INVALID=462
STATUS_CODE_REFUND_INVALID=463

def verifyToken(request):
    token = request.headers.get("fz-backend-api")
    settings = GlobalSettingsObject().settings
    if settings.fzbackendutils_internal_endpoint_token and (
        not token or token != settings.fzbackendutils_internal_endpoint_token
    ):
        return Http404("Token not found (invalid)")