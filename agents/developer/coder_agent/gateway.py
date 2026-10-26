"""Optional foreground model-only HTTP-over-Unix gateway; not a service installer.

The sandbox receives this socket, never the upstream URL or API credential.
Only one fixed model's chat-completion endpoint can be reached. No CONNECT,
redirects, arbitrary headers, filesystem paths or client-selected destinations.
"""
from __future__ import annotations

import argparse
import http.client
import http.server
import json
import math
import os
import socketserver
import threading
from pathlib import Path
from urllib.parse import urlsplit


def error_response(status: int, code: str, message: str) -> bytes:
    """Small, stable diagnostics which never include upstream data."""
    return json.dumps({"error": {"type": "coder_gateway_error", "code": code,
                                  "message": message, "statusCode": status}}).encode()


class Gateway(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, upstream: str, model: str, api_key: str = "",
                 max_requests: int = 100, max_tokens: int = 8192, *,
                 request_timeout: float = 10, max_connections: int = 16,
                 upstream_timeout: float = 60):
        url = urlsplit(upstream)
        if (url.scheme not in ("https", "http") or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in ("", "/", "/v1", "/v1/")
                or (url.scheme == "http" and url.hostname not in ("127.0.0.1", "::1"))):
            raise ValueError("Upstream must be HTTPS or loopback HTTP, optionally ending in /v1")
        try:
            url.port
        except ValueError:
            raise ValueError("Invalid upstream port") from None
        if (max_requests < 1 or max_tokens < 1 or not math.isfinite(request_timeout)
                or not 0 < request_timeout <= 7200 or max_connections < 1
                or not math.isfinite(upstream_timeout) or not 0 < upstream_timeout <= 7200
                or any(ord(char) < 32 or ord(char) > 126 for char in api_key)):
            raise ValueError("Invalid gateway limits or credential format")
        self.upstream, self.model, self.api_key = url, model, api_key
        self.remaining, self.max_tokens = max_requests, max_tokens
        self.request_timeout = request_timeout
        self.upstream_timeout = upstream_timeout
        self.budget_lock = threading.Lock()
        self.connection_slots = threading.BoundedSemaphore(max_connections)
        # Do not unlink/replace an existing socket: it might be another service.
        super().__init__(path, Handler)
        os.chmod(path, 0o600)

    def get_request(self):
        connection, address = super().get_request()
        # Set before BaseHTTPRequestHandler reads even the request line/headers.
        connection.settimeout(self.request_timeout)
        return connection, address

    def process_request(self, request, client_address):
        if not self.connection_slots.acquire(blocking=False):
            body = error_response(503, "gateway_busy", "Model gateway connection limit reached")
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()

    def handle_error(self, request, client_address):
        # A traceback can embed HTTP headers or exception messages containing a
        # credential. Handler responses already carry bounded safe diagnostics.
        pass


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log headers, model prompts, responses or credentials.

    def gateway_error(self, status: int, code: str, message: str):
        body = error_response(status, code, message)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_POST(self):
        if self.path != "/v1/chat/completions" or self.headers.get("Transfer-Encoding"):
            self.gateway_error(403, "invalid_endpoint", "Only the configured chat completions endpoint is permitted")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2_097_152:
                raise ValueError()
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or body.get("model") != self.server.model:
                raise ValueError()
            if type(body.get("n", 1)) is not int or body.get("n", 1) != 1:
                raise ValueError()
            for key in ("max_tokens", "max_completion_tokens"):
                if key in body and (type(body[key]) is not int or not 0 < body[key] <= self.server.max_tokens):
                    raise ValueError()
            if "max_tokens" not in body and "max_completion_tokens" not in body:
                body["max_tokens"] = self.server.max_tokens
        except (ValueError, OSError):
            self.gateway_error(400, "invalid_request", "Invalid model request or requested limits")
            return
        with self.server.budget_lock:
            exhausted = self.server.remaining <= 0
            if not exhausted:
                self.server.remaining -= 1
        if exhausted:
            self.gateway_error(429, "gateway_budget_exhausted", "Model gateway request budget exhausted")
            return
        url = self.server.upstream
        cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        connection = cls(url.hostname, url.port, timeout=self.server.upstream_timeout)
        started = False
        try:
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
            if self.server.api_key:
                headers["Authorization"] = "Bearer " + self.server.api_key
            connection.request("POST", "/v1/chat/completions", json.dumps(body).encode(), headers)
            response = connection.getresponse()
            if 300 <= response.status < 400:
                self.gateway_error(502, "upstream_redirect_denied", "Model upstream redirects are disabled")
                return
            if not 200 <= response.status < 300:
                code, message = {
                    401: ("authentication_required", "Model provider authentication failed or is missing"),
                    403: ("provider_access_denied", "Model provider denied access"),
                    404: ("model_unavailable", "Configured model or model endpoint is unavailable"),
                    429: ("provider_rate_limited", "Model provider request budget or rate limit exceeded"),
                }.get(response.status, ("provider_unavailable" if response.status >= 500 else "provider_request_rejected",
                                        "Model provider could not serve the configured request"))
                # Never read/forward upstream error bodies or error headers:
                # some providers reflect credentials or request details in them.
                self.gateway_error(response.status, code, message)
                return
            self.send_response(response.status)
            content_type = response.getheader("Content-Type", "application/json")
            self.send_header("Content-Type", "text/event-stream" if content_type.startswith("text/event-stream") else "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            started = True
            remaining = 16_777_216
            while remaining:
                chunk = response.read1(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                remaining -= len(chunk)
        except (OSError, http.client.HTTPException, ValueError):
            if not started:
                self.gateway_error(502, "gateway_unavailable", "Model gateway cannot reach its configured upstream")
        finally:
            connection.close()
            self.close_connection = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Foreground fixed-model Unix gateway")
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--upstream", required=True, help="Fixed HTTPS endpoint or loopback HTTP")
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-requests", type=int, default=100)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--upstream-timeout", type=float, default=60,
                        help="Bounded upstream connect/read inactivity timeout in seconds (default: 60, maximum: 7200)")
    args = parser.parse_args(argv)
    if args.max_requests < 1 or args.max_tokens < 1:
        parser.error("Limits must be positive")
    if not math.isfinite(args.upstream_timeout) or not 0 < args.upstream_timeout <= 7200:
        parser.error("--upstream-timeout must be positive, finite and at most 7200 seconds")
    if not args.socket.is_absolute() or args.socket.parent.stat().st_mode & 0o077:
        parser.error("Socket must be in an absolute private directory (mode 0700)")
    with Gateway(str(args.socket), args.upstream, args.model,
                 os.environ.get("CODER_MODEL_API_KEY", ""), args.max_requests, args.max_tokens,
                 upstream_timeout=args.upstream_timeout) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            args.socket.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
