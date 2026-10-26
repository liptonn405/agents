import http.client
import http.server
import json
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

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
        self.upstream_status = 200
        self.upstream_body = b'{"ok":true}'
        test = self

        class Upstream(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, dict(self.headers), body))
                self.send_response(test.upstream_status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(test.upstream_body)

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

    def test_upstream_auth_error_does_not_expose_credentials(self):
        self.upstream_status = 401
        self.upstream_body = b'{"error":"echoed fake-not-a-real-key and private response details"}'
        status, payload = self.request()
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(payload)["error"]["code"], "authentication_required")
        self.assertNotIn(b"fake-not-a-real-key", payload)
        self.assertNotIn(b"private response details", payload)
        self.assertEqual(self.requests[0][1]["Authorization"], "Bearer fake-not-a-real-key")

    def test_model_not_found_has_safe_diagnostic(self):
        self.upstream_status = 404
        self.upstream_body = b'{"error":"private upstream information"}'
        status, payload = self.request()
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(payload)["error"]["code"], "model_unavailable")
        self.assertNotIn(b"private upstream information", payload)

    def test_redirect_is_not_followed_or_reflected(self):
        self.upstream_status = 302
        self.upstream_body = b"fake-not-a-real-key"
        status, payload = self.request()
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(payload)["error"]["code"], "upstream_redirect_denied")
        self.assertNotIn(b"fake-not-a-real-key", payload)
        self.assertEqual(len(self.requests), 1)

    def test_only_one_completion_is_permitted(self):
        for count in (0, 2, True, "1"):
            with self.subTest(count=count):
                self.assertEqual(self.request(body={"model": "model", "n": count})[0], 400)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.request(body={"model": "model", "n": 1})[0], 200)

    def test_idle_or_partial_headers_do_not_block_other_requests(self):
        self.server.request_timeout = 0.15
        idle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(idle.close)
        idle.settimeout(1)
        idle.connect(str(self.socket_path))
        idle.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n")
        started = time.monotonic()
        self.assertEqual(self.request()[0], 200)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(idle.recv(1), b"")

    def test_concurrent_requests_cannot_overspend_budget(self):
        with self.server.budget_lock:
            self.server.remaining = 1
        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(lambda _: self.request()[0], range(4)))
        self.assertEqual(statuses.count(200), 1)
        self.assertEqual(statuses.count(429), 3)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.server.remaining, 0)

    def test_invalid_credential_header_is_rejected_without_echoing_it(self):
        credential = "private-key\r\nInjected: bad"
        with self.assertRaises(ValueError) as context:
            Gateway(str(self.socket_path), "http://127.0.0.1:8080/v1", "model", api_key=credential)
        self.assertNotIn("private-key", str(context.exception))

    def test_configured_upstream_timeout_supports_cold_start_over_sixty_seconds(self):
        # Simulate a model needing 90s before response headers. No real wait,
        # upstream request or relaxation of the request/token budgets occurs.
        def connection(*args, **kwargs):
            client = Mock()
            response = Mock(status=200)
            response.getheader.return_value = "application/json"
            response.read1.side_effect = [b'{"ok":true}', b""]
            if kwargs["timeout"] < 90:
                client.getresponse.side_effect = TimeoutError("simulated cold model")
            else:
                client.getresponse.return_value = response
            return client

        with patch("coder_agent.gateway.http.client.HTTPConnection", side_effect=connection) as factory:
            self.assertEqual(self.request()[0], 502)  # default remains fail closed
            self.server.upstream_timeout = 180
            self.assertEqual(self.request(), (200, b'{"ok":true}'))
        self.assertEqual([call.kwargs["timeout"] for call in factory.call_args_list], [60, 180])
        self.assertEqual(self.server.request_timeout, 10)
        self.assertEqual(self.server.remaining, 0)

    def test_upstream_timeout_is_finite_and_bounded(self):
        for timeout in (0, -1, float("inf"), float("nan"), 7201):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                Gateway(str(self.socket_path), "http://127.0.0.1:8080/v1", "model",
                        upstream_timeout=timeout)
