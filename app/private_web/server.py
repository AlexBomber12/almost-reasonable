"""Private HTTP handlers never write raw requests or exception details to logs."""

from io import StringIO
from socketserver import ThreadingMixIn
from threading import BoundedSemaphore
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer


class PrivateWSGIServer(ThreadingMixIn, WSGIServer):
    """Bounded HTTP concurrency keeps metadata reads off the model reply queue."""

    daemon_threads = True
    max_threads = 8

    def __init__(self, *args, **kwargs):
        self._slots = BoundedSemaphore(self.max_threads)
        super().__init__(*args, **kwargs)

    def get_app(self):
        application = super().get_app()

        def threaded(environ, start_response):
            environ["wsgi.multithread"] = True
            return application(environ, start_response)

        return threaded

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


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
