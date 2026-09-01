"""Uniform API error envelope for the DRF layer."""
from rest_framework.views import exception_handler


def crm_exception_handler(exc, context):
    """Wrap DRF errors as {"error": {"status", "detail"}} for a stable contract."""
    response = exception_handler(exc, context)
    if response is None:
        return None
    response.data = {
        "error": {
            "status": response.status_code,
            "detail": response.data,
        }
    }
    return response
