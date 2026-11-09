"""Private SSH loopback forward for the real renderer test; credential stays in memory."""
import getpass
import os
import select
import socketserver
import subprocess
import threading
from pathlib import Path

from preview_remote_frontend import SSHConnection

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    connection = SSHConnection(getpass.getpass("SSH password: "))
    connection.get()

    class Forward(socketserver.BaseRequestHandler):
        def handle(self):
            channel = connection.get().open_channel("direct-tcpip", ("127.0.0.1", 18136), self.client_address, timeout=10)
            try:
                while True:
                    ready, _, _ = select.select([self.request, channel], [], [], 1)
                    if self.request in ready:
                        value = self.request.recv(65536)
                        if not value:
                            break
                        channel.sendall(value)
                    if channel in ready:
                        value = channel.recv(65536)
                        if not value:
                            break
                        self.request.sendall(value)
            finally:
                channel.close()

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = Server(("127.0.0.1", 18136), Forward)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    temporary = ROOT / ".tmp/release-readiness/browser-real"
    temporary.mkdir(exist_ok=True)
    try:
        result = subprocess.run(["node", "scripts/probe_readiness_legacy_browser.mjs"], cwd=ROOT, env=os.environ | {"TEMP": str(temporary), "TMP": str(temporary)})
    finally:
        server.shutdown()
        server.server_close()
        connection.close()
    raise SystemExit(result.returncode)
