import http.client
import socket
import sys
import threading
import unittest
from pathlib import Path

if sys.platform != "win32":
    raise unittest.SkipTest("Local Windows launcher tests use existing base Conda Paramiko")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from http.server import ThreadingHTTPServer

from launch_workspace import allowed_request, handler


class Channel:
    def __init__(self, fail=False):
        self.sent = []
        self.closed = False
        self.fail = fail
        self.parts = [
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: 15\r\nConnection: close\r\n\r\ndata: success\n\n",
            b"",
        ]

    def sendall(self, raw):
        self.sent.append(raw)
        if self.fail:
            raise EOFError()

    def recv(self, size):
        return self.parts.pop(0)

    def close(self):
        self.closed = True


class Connection:
    def __init__(self, fail=False):
        self.channel = Channel(fail)
        self.opens = 0

    def get(self):
        return self

    def open_channel(self, *args, **kwargs):
        self.opens += 1
        return self.channel


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.connection = Connection()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), handler(self.connection, "private-server-token", 5175, 18120)
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def call(self, method, path, body=None, headers=None):
        client = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            client.request(method, path, body=body, headers=headers or {})
            response = client.getresponse()
            return response.status, response.read()
        finally:
            client.close()

    def test_real_post_shape_and_server_only_auth_injection(self):
        code, _body = self.call(
            "POST",
            "/api/v1/hermes/threads",
            "{}",
            {"Origin": "http://127.0.0.1:5175", "Authorization": "Bearer browser-token"},
        )
        self.assertEqual(code, 200)
        raw = self.connection.channel.sent[0]
        self.assertIn(b"Authorization: Bearer private-server-token", raw)
        self.assertNotIn(b"browser-token", raw)
        self.assertIn(b"Content-Length: 2", raw)
        self.assertTrue(self.connection.channel.closed)

    def test_sse_response_is_not_replayed(self):
        code, body = self.call("GET", "/api/v1/hermes/threads/t/runs/r/events")
        self.assertEqual(code, 200)
        self.assertIn(b"data: success", body)
        self.assertEqual(self.connection.opens, 1)

    def test_cross_origin_and_non_business_writes_denied(self):
        self.assertEqual(
            self.call("POST", "/api/v1/hermes/threads", "{}", {"Origin": "https://other.example"})[
                0
            ],
            403,
        )
        self.assertEqual(self.call("POST", "/api/v1/scat/arbitrary", "{}")[0], 405)
        self.assertEqual(self.connection.opens, 0)

    def test_unbounded_or_ambiguous_body_denied(self):
        self.assertEqual(
            self.call("POST", "/api/v1/hermes/threads", headers={"Content-Length": "1000001"})[0],
            413,
        )
        with socket.create_connection(self.server.server_address, timeout=3) as sock:
            sock.sendall(
                b"POST /api/v1/hermes/threads HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\nContent-Length: 1\r\n\r\n"
            )
            self.assertIn(b"400", sock.recv(1024))
        self.assertEqual(self.connection.opens, 0)

    def test_uncertain_dispatch_is_never_repeated(self):
        self.connection.channel.fail = True
        with self.assertRaises(http.client.RemoteDisconnected):
            self.call("POST", "/api/v1/hermes/threads", "{}")
        self.assertEqual(self.connection.opens, 1)
        self.assertEqual(len(self.connection.channel.sent), 1)

    def test_target_routes_are_fixed(self):
        self.assertFalse(allowed_request("POST", "http://other/api/v1/hermes/threads"))
        self.assertFalse(allowed_request("POST", "//other/api/v1/hermes/threads"))
        self.assertFalse(allowed_request("POST", "/v1/settings", False))
        self.assertTrue(allowed_request("POST", "/v1/settings/proposals", True))


if __name__ == "__main__":
    unittest.main()
