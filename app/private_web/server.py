"""Private HTTP handlers never write raw requests or exception details to logs."""

from io import StringIO
from wsgiref.simple_server import WSGIRequestHandler


class PrivateWSGIRequestHandler(WSGIRequestHandler):
    """Keep query strings, credentials, cookies and bodies out of server logs.

    Operators use the application status and bounded sanitized audit instead of
    raw HTTP access logs or WSGI exception traces. Proxy logging is configured
    independently and must follow the same policy.
    """

    def log_message(self, format, *args):
        pass

    def get_stderr(self):
        return StringIO()
