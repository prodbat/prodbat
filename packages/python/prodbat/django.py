"""Add "prodbat.django.Middleware" to MIDDLEWARE to report unhandled
exceptions with their cause, and any other 5xx answer.

An exception is reported once Django has answered, and only if the answer
is a 5xx: Http404, PermissionDenied and the like become 404, 403 and 400
pages, which are the site working normally."""

from . import capture_exception, capture_status


class Middleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        status = getattr(response, "status_code", 0)
        if status >= 500:
            route = f"{request.method} {request.path}"
            exception = getattr(request, "_prodbat_exception", None)
            if exception is not None:
                capture_exception(exception, route)
            else:
                capture_status(status, route)
        return response

    def process_exception(self, request, exception):
        request._prodbat_exception = exception  # reported above, if Django answers it with a 5xx
        return None  # let Django carry on with its own error handling
