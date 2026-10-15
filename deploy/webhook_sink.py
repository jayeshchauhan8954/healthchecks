"""Records every request it receives.

GET /_requests lists them, DELETE /_requests clears them.
POST /_hang/<seconds> makes later requests wait that long before getting a
response (after being recorded); POST /_hang/0 turns it off. Used to simulate
a webhook endpoint that hangs.
"""

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

received = []
hang = 0.0


class Handler(BaseHTTPRequestHandler):
    def _reply(self, status, body=b""):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode(errors="replace")
        received.append({
            "method": self.command, "path": self.path, "body": body,
            "client": self.client_address[0], "t": time.time(), "hung": hang,
        })
        print(f"{self.command} {self.path} {body} (hang {hang}s)", flush=True)
        if hang:
            time.sleep(hang)
        try:
            self._reply(200)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client gave up waiting

    def do_GET(self):
        if self.path == "/_requests":
            return self._reply(200, json.dumps(received).encode())
        self._record()

    def do_DELETE(self):
        if self.path == "/_requests":
            received.clear()
            return self._reply(204)
        self._record()

    def do_POST(self):
        global hang
        if self.path.startswith("/_hang/"):
            hang = float(self.path.removeprefix("/_hang/"))
            return self._reply(204)
        self._record()

    do_PUT = _record

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("", 8080), Handler).serve_forever()
