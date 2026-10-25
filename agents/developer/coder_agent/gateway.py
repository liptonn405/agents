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
import os
import socketserver
from pathlib import Path
from urllib.parse import urlsplit


class Gateway(socketserver.UnixStreamServer):
    def __init__(self, path: str, upstream: str, model: str, api_key: str = "",
                 max_requests: int = 100, max_tokens: int = 8192):
        url = urlsplit(upstream)
        if (url.scheme not in ("https", "http") or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in ("", "/", "/v1", "/v1/")
                or (url.scheme == "http" and url.hostname not in ("127.0.0.1", "::1"))):
            raise ValueError("Upstream must be HTTPS or loopback HTTP, optionally ending in /v1")
        self.upstream, self.model, self.api_key = url, model, api_key
        self.remaining, self.max_tokens = max_requests, max_tokens
        # Do not unlink/replace an existing socket: it might be another service.
        super().__init__(path, Handler)
        os.chmod(path, 0o600)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log headers, model prompts, responses or credentials.

    def do_POST(self):
        self.connection.settimeout(60)
        if self.path != "/v1/chat/completions" or self.headers.get("Transfer-Encoding"):
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2_097_152:
                raise ValueError()
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or body.get("model") != self.server.model:
                raise ValueError()
            if self.server.remaining <= 0:
                self.send_error(429)
                return
            for key in ("max_tokens", "max_completion_tokens"):
                if key in body and (type(body[key]) is not int or not 0 < body[key] <= self.server.max_tokens):
                    raise ValueError()
            if "max_tokens" not in body and "max_completion_tokens" not in body:
                body["max_tokens"] = self.server.max_tokens
            self.server.remaining -= 1
        except (ValueError, OSError):
            self.send_error(400)
            return
        url = self.server.upstream
        cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        connection = cls(url.hostname, url.port, timeout=60)
        started = False
        try:
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
            if self.server.api_key:
                headers["Authorization"] = "Bearer " + self.server.api_key
            connection.request("POST", "/v1/chat/completions", json.dumps(body).encode(), headers)
            response = connection.getresponse()
            if 300 <= response.status < 400:
                self.send_error(502)
                return
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
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
        except (OSError, http.client.HTTPException):
            if not started:
                self.send_error(502)
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
    args = parser.parse_args(argv)
    if args.max_requests < 1 or args.max_tokens < 1:
        parser.error("Limits must be positive")
    if not args.socket.is_absolute() or args.socket.parent.stat().st_mode & 0o077:
        parser.error("Socket must be in an absolute private directory (mode 0700)")
    with Gateway(str(args.socket), args.upstream, args.model,
                 os.environ.get("CODER_MODEL_API_KEY", ""), args.max_requests, args.max_tokens) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            args.socket.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
