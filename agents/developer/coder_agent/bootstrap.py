"""Executed ONLY inside the sandbox using its trusted Python runtime.

Expose one HTTP-over-Unix gateway on private loopback. No host networking, DNS,
shell, credentials or configurable destinations are available here.
"""
from __future__ import annotations

import os
import resource
import select
import socket
import socketserver
import subprocess
import sys
import threading


class Bridge(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
            upstream.settimeout(60)
            try:
                upstream.connect("/tmp/model.sock")
                sockets = (self.request, upstream)
                while True:
                    ready, _, _ = select.select(sockets, [], [], 60)
                    if not ready:
                        return
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        (upstream if source is self.request else self.request).sendall(data)
            except OSError:
                return


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    cpu, processes, file_bytes = map(int, sys.argv[1:4])
    for kind, value in ((resource.RLIMIT_CPU, cpu), (resource.RLIMIT_NPROC, processes),
                        (resource.RLIMIT_FSIZE, file_bytes), (resource.RLIMIT_NOFILE, 256),
                        (resource.RLIMIT_CORE, 0)):
        resource.setrlimit(kind, (value, value))
    if os.getuid() == 0:
        return 125
    if os.path.exists("/tmp/model.sock"):
        server = Server(("127.0.0.1", 8765), Bridge)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    return subprocess.call(sys.argv[4:], stdin=subprocess.DEVNULL)


if __name__ == "__main__":
    raise SystemExit(main())
