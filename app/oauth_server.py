from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit, parse_qs
from pathlib import Path
from hmac import compare_digest
import os
import time

BASE = Path("/var/lib/oauth")
STATE = BASE / "state"
CODE = BASE / "code"
MAX_AGE = 900


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, message):
        body = (message + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if len(self.path) > 4096:
            return self.reply(414, "Request too long")

        parts = urlsplit(self.path)

        if parts.path == "/healthz":
            return self.reply(200, "ok")

        if parts.path != "/callback":
            return self.reply(404, "Not found")

        try:
            params = parse_qs(parts.query, max_num_fields=10)
            received = params.get("state", [])
            expected = STATE.read_text().strip()
            age = time.time() - STATE.stat().st_mtime
        except (OSError, ValueError):
            return self.reply(403, "Invalid OAuth state")

        if (
            len(received) != 1
            or not expected
            or not compare_digest(received[0], expected)
            or age < 0
            or age > MAX_AGE
        ):
            return self.reply(403, "Invalid OAuth state")

        STATE.unlink()

        if "error" in params:
            return self.reply(400, "Authorization declined")

        codes = params.get("code", [])

        if len(codes) != 1 or not 0 < len(codes[0]) <= 2048:
            return self.reply(400, "Invalid authorization code")

        try:
            fd = os.open(CODE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                output.write(codes[0])
        except FileExistsError:
            return self.reply(409, "Authorization code already stored")

        return self.reply(200, "Authorization received. You can close this page.")

    def log_message(self, *args):
        pass


def main(server_class=HTTPServer):
    server_class(("0.0.0.0", 8000), Handler).serve_forever()


if __name__ == "__main__":
    main()
