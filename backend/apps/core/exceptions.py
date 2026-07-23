"""Uniform API error envelope (plan §5, Phase 0 step 8)."""
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
