import http.client
import http.server
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

import coder_support
from coder_agent.gateway import Gateway


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=3)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(3)
        self.sock.connect(self.path)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.socket_path = Path(temp.name) / "gateway.sock"
        self.requests = []
        requests = self.requests

        class Upstream(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, dict(self.headers), body))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

        try:
            self.upstream = http.server.HTTPServer(("127.0.0.1", 0), Upstream)
        except PermissionError:
            self.skipTest("Supervisor denies local listening sockets")
        self.addCleanup(self.upstream.server_close)
        self.server = Gateway(str(self.socket_path), f"http://127.0.0.1:{self.upstream.server_port}",
                              "model", api_key="fake-not-a-real-key", max_requests=2, max_tokens=10)
        self.addCleanup(self.server.server_close)
        for server in (self.upstream, self.server):
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            worker.start()
            self.addCleanup(worker.join, 2)
            self.addCleanup(server.shutdown)

    def request(self, path="/v1/chat/completions", body=None, method="POST"):
        connection = UnixConnection(self.socket_path)
        try:
            connection.request(method, path, json.dumps(body or {"model": "model", "messages": []}),
                               {"Content-Type": "application/json", "Authorization": "untrusted"})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_fixed_destination_and_host_credential(self):
        self.assertEqual(self.request(), (200, b'{"ok":true}'))
        path, headers, body = self.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer fake-not-a-real-key")
        self.assertEqual(body["max_tokens"], 10)

    def test_other_paths_models_and_proxying_denied(self):
        for path in ("/v1/models", "http://example.test/", "/v1/chat/completions?host=evil"):
            self.assertEqual(self.request(path)[0], 403)
        self.assertEqual(self.request(body={"model": "other"})[0], 400)
        self.assertEqual(self.request(method="CONNECT")[0], 501)
        self.assertEqual(self.requests, [])

    def test_budgets(self):
        self.assertEqual(self.request(body={"model": "model", "max_tokens": 11})[0], 400)
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.request()[0], 429)

    def test_upstream_validation(self):
        for value in ("http://example.test", "https://host/path", "https://user:pass@host", "file:///tmp/x"):
            with self.assertRaises(ValueError):
                Gateway(str(self.socket_path), value, "model")
