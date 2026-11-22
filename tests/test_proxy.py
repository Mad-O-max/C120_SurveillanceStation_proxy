import json
import http.server
import logging
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import c120_onvif_proxy as proxy
from proxy_settings import configure_logging, credentials

ROOT = Path(__file__).resolve().parent.parent


class ProtocolTests(unittest.TestCase):
    def test_local_http_transport(self):
        request = b"<Envelope><Body><GetProfiles/></Body></Envelope>"
        response = b"<Envelope><Body><GetProfilesResponse/></Body></Envelope>"
        received = []

        class Camera(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                received.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        with http.server.HTTPServer(("127.0.0.1", 0), Camera) as camera:
            worker = threading.Thread(target=camera.handle_request, daemon=True)
            worker.start()
            endpoint = "http://127.0.0.1:{}/onvif/service".format(camera.server_port)
            status, _, payload = proxy.exchange("127.0.0.1", endpoint, request, timeout=2)
            worker.join(2)
        self.assertEqual((status, payload), (200, response))
        self.assertEqual(received, [("/onvif/service", request)])

    def test_endpoint_allowlist(self):
        for endpoint in (
            "http://192.0.2.4:0/onvif/service",
            "https://192.0.2.4/onvif/service",
            "http://192.0.2.5/onvif/service",
            "http://secret@192.0.2.4/onvif/service",
            "http://192.0.2.4/private",
            "http://192.0.2.4/onvif/../private",
            "http://192.0.2.4/onvif/%2e%2e/private",
            "http://192.0.2.4:1024/event-1024#private",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                proxy.camera_endpoint("192.0.2.4", endpoint)
        endpoint = "http://192.0.2.4:1042/event-1042_1042?token=A%20B"
        self.assertEqual(proxy.camera_endpoint("192.0.2.4", endpoint)[0].port, 1042)

    def test_event_body_unchanged(self):
        routing = proxy.Routing("192.0.2.4", "http://127.0.0.1:12020")
        old = b"http://192.0.2.4:1024/event-1024"
        event = (b'<NotificationMessage><Topic>tns1:RuleEngine/PeopleDetector/People</Topic>'
                 b'<Message PropertyOperation="Initialized"><SimpleItem Name="IsPeople" Value="true"/>'
                 b'<Note>' + old + b'</Note></Message></NotificationMessage>')
        body = b"<r><Address>" + old + b"</Address>" + event + b"</r>"
        self.assertIn(event, routing.response(body))
        self.assertIn(b"<Address>http://127.0.0.1:12020/event-1024</Address>", routing.response(body))
        routing.response(b"<Address>http://192.0.2.4:1024/onvif/service</Address>")
        self.assertEqual(routing.destination("/onvif/service")[0], 2020)

    def test_parallel_pull_busy_and_renew(self):
        routing = proxy.Routing("192.0.2.4", "http://127.0.0.1:12020")
        request = b"<Envelope><Body><PullMessages><Timeout>PT5S</Timeout></PullMessages></Body></Envelope>"
        response = (b"<Envelope><Body><PullMessagesResponse><NotificationMessage>"
                    b"<Address>http://192.0.2.4:1024/event-1</Address>"
                    b"</NotificationMessage></PullMessagesResponse></Body></Envelope>")
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def send(url, body, headers=None, timeout=8):
            calls.append(proxy.soap_summary(body)[0])
            entered.set()
            if not release.wait(2):
                raise AssertionError("Pull did not release")
            return 200, [], response

        manager = proxy.Subscriptions(routing, "user", "secret", transport=send)
        sub = proxy.Subscription("/event-1", "http://192.0.2.4:1024/event-1", "", b"")
        clock = [100]
        sub.deadline, sub.margin = 140, 30
        manager.items[sub.alias] = sub
        result = []
        with patch.object(proxy.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(manager, "renew") as renew:
            worker = threading.Thread(target=lambda: result.append(manager.forward(sub.alias, request, {})))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                with self.assertRaises(proxy.RecoveryBackoff):
                    manager.forward(sub.alias, request, {})
                clock[0] = 118
                manager.maintain(sub)
                renew.assert_not_called()
            finally:
                release.set()
                worker.join(3)
            manager.maintain(sub)
            renew.assert_called_once_with(sub)
        self.assertEqual(calls, ["PullMessages"])
        self.assertEqual(result[0][0], 200)
        self.assertEqual(result[0][2], response)

    def test_cleanup_unsubscribes_owned_lease(self):
        routing = proxy.Routing("192.0.2.4", "http://127.0.0.1:12020")
        sub = proxy.Subscription("/event-1", "http://192.0.2.4:1024/event-1", "", b"")
        sub.deadline = time.monotonic() + 60
        manager = proxy.Subscriptions(routing, "user", "secret")
        manager.items[sub.alias] = sub
        with patch.object(manager, "send") as send:
            manager.close()
        self.assertEqual(proxy.soap_summary(send.call_args.args[1])[0], "Unsubscribe")
        self.assertLessEqual(send.call_args.kwargs["timeout"], 1)

    def test_auth_backoff(self):
        routing = proxy.Routing("192.0.2.4", "http://127.0.0.1:12020")
        manager = proxy.Subscriptions(routing, "user", "secret",
                                      transport=lambda *args, **kwargs: (401, [], b""))
        sub = proxy.Subscription("/event-1", "http://192.0.2.4:1024/event-1",
                                 "http://192.0.2.4:2020/onvif/service", b"<Envelope/>")
        with patch.object(proxy.time, "monotonic", return_value=100):
            with self.assertRaises(proxy.CameraAuthError):
                manager.ensure(sub)
            self.assertEqual(sub.retry_at, 400)
            with self.assertRaises(proxy.RecoveryBackoff):
                manager.ensure(sub)

    def test_fault_and_configuration_do_not_echo_input(self):
        secret = "__TEST_SECRET__"
        fault = "<Fault><Value>{}</Value></Fault>".format(secret).encode()
        self.assertEqual(proxy.fault_name(fault), "Fault")
        result = subprocess.run([sys.executable, str(ROOT / "c120_onvif_proxy.py"),
                                 "--camera", secret], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(secret, result.stderr)

    def test_rotating_log(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "proxy.log"
            logger = configure_logging("INFO", path, logging.getLogger("test.rotation"))
            try:
                logger.debug("hidden-debug")
                for _ in range(10):
                    logger.info("x" * (256 * 1024))
                files = list(Path(temp).glob("proxy.log*"))
                self.assertEqual(len(files), 4)
                self.assertTrue(all(f.stat().st_size <= 512 * 1024 for f in files))
                self.assertNotIn("hidden-debug", path.read_text())
            finally:
                for handler in logger.handlers[:]:
                    logger.removeHandler(handler)
                    handler.close()


@unittest.skipUnless(os.name == "posix", "POSIX credential and lifecycle checks")
class PosixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="c120-")
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.key = self.state / "credentials.json"
        self.key.write_text(json.dumps({"username": "test-user", "password": "__TEST_SECRET__"}))
        self.key.chmod(0o600)

    def test_credential_modes(self):
        self.assertEqual(credentials(self.key)[0], "test-user")
        self.key.chmod(0o644)
        with self.assertRaises(ValueError):
            credentials(self.key)
        self.key.chmod(0o600)
        self.state.chmod(0o755)
        try:
            with self.assertRaises(ValueError):
                credentials(self.key)
        finally:
            self.state.chmod(0o700)
        link = self.state / "link.json"
        link.symlink_to(self.key)
        with self.assertRaises(OSError):
            credentials(link)

    def test_lifecycle(self):
        with socket.socket() as a, socket.socket() as b:
            a.bind(("127.0.0.1", 0))
            b.bind(("127.0.0.1", 0))
            port, rtsp_port = a.getsockname()[1], b.getsockname()[1]
        (self.state / "proxy.json").write_text(json.dumps(
            dict(camera="192.0.2.4", port=port, rtsp_port=rtsp_port)))
        service = [sys.executable, str(ROOT / "scripts/service.py"), "--state", str(self.state)]

        def ctl(action):
            proc = subprocess.run(service + [action], capture_output=True, text=True, timeout=25)
            return proc.returncode, proc.stdout + proc.stderr

        try:
            self.assertEqual(ctl("start"), (0, "Configuration and credential permissions valid\nService running\n"))
            self.assertIn("already active", ctl("start")[1])
            self.assertEqual(ctl("status"), (0, "running\n"))
            with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
                client.sendall(b"GET /healthz HTTP/1.0\r\n\r\n")
                self.assertIn(b"200 OK", client.recv(4096))
            self.assertNotIn("192.0.2.4", ctl("diagnose")[1])
            if sys.platform == "linux":
                proxy_pid = self.find_child()
                self.assertIsNotNone(proxy_pid)
                os.kill(proxy_pid, signal.SIGKILL)
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if ctl("status") == (0, "running\n") and self.find_child() != proxy_pid:
                        break
                    time.sleep(0.2)
                else:
                    self.fail("Supervisor did not replace the crashed proxy")
                self.assertIn("Proxy exited with code -9", (self.state / "service.log").read_text())
            self.assertEqual(ctl("restart")[0], 0)
            self.assertEqual(ctl("stop")[0], 0)
            self.assertEqual(ctl("status"), (1, "stopped\n"))
            self.assertIn("Proxy stopped", (self.state / "proxy.log").read_text())
            for path in self.state.glob("*.log"):
                self.assertNotIn("__TEST_SECRET__", path.read_text())
        finally:
            ctl("stop")

    def find_child(self):
        marker = os.fsencode(str(self.state / "credentials.json"))
        for entry in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                args = entry.read_bytes().split(b"\x00")
            except OSError:
                continue
            if marker in args and b"--log-file" in args:
                return int(entry.parent.name)
        return None


if __name__ == "__main__":
    unittest.main()
