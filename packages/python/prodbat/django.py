"""Add "prodbat.django.Middleware" to MIDDLEWARE to report unhandled
exceptions with their cause, and any other 5xx answer."""

from . import capture_exception, capture_status


class Middleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        status = getattr(response, "status_code", 0)
        if status >= 500 and not getattr(request, "_prodbat_reported", False):
            capture_status(status, f"{request.method} {request.path}")
        return response

    def process_exception(self, request, exception):
        request._prodbat_reported = True
        capture_exception(exception, f"{request.method} {request.path}")
        return None  # let Django carry on with its own error handling
