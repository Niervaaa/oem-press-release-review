import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SKILL_DIR / "scripts" / "capture_public_page.py"


def load_capture_module():
    spec = importlib.util.spec_from_file_location("capture_public_page_hardening", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capture = load_capture_module()


def request_key(url):
    parsed = urllib.parse.urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port, parsed.path or "/"


class FakeResponse:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self.data = body
        self.headers = headers or {}

    def getheader(self, name, default=None):
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return default

    def read(self, limit=-1):
        return self.data if limit < 0 else self.data[:limit]

    def close(self):
        pass


class FakeConnection:
    def __init__(self, transport, scheme, host, port, timeout, allow_loopback):
        self.transport = transport
        self.scheme = scheme
        self.host = host
        self.port = port or (443 if scheme == "https" else 80)
        self.timeout = timeout
        self.allow_loopback = allow_loopback
        self.response = None

    def request(self, method, path, headers=None):
        key = (self.scheme, self.host.lower(), self.port, urllib.parse.urlsplit(path).path)
        self.transport.requests.append(key)
        if key not in self.transport.routes:
            raise AssertionError(f"unexpected outbound request: {key}")
        self.response = FakeResponse(*self.transport.routes[key])

    def getresponse(self):
        return self.response

    def close(self):
        pass


class FakeTransport:
    def __init__(self):
        self.routes = {}
        self.requests = []

    def add(self, url, status=200, body=b"", headers=None):
        self.routes[request_key(url)] = (status, body, headers or {})

    def redirect(self, url, location):
        self.add(url, 302, headers={"Location": location})

    def connection(self, scheme, host, port, timeout, allow_loopback):
        return FakeConnection(self, scheme, host, port, timeout, allow_loopback)


@contextlib.contextmanager
def offline_network(transport):
    with mock.patch.object(capture, "validate_url", side_effect=lambda url, allow_loopback=False: urllib.parse.urlsplit(url)), \
            mock.patch.object(capture, "GuardedHTTPConnection", side_effect=lambda host, port, timeout, allow_loopback: transport.connection("http", host, port, timeout, allow_loopback)), \
            mock.patch.object(capture, "GuardedHTTPSConnection", side_effect=lambda host, port, timeout, allow_loopback: transport.connection("https", host, port, timeout, allow_loopback)):
        yield


class PublicPageHardeningTests(unittest.TestCase):
    def run_cli(self, transport, url, *args):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        out = Path(temp_dir.name) / "capture"
        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = [str(SCRIPT_PATH), url, "--out", str(out), *map(str, args)]
        with offline_network(transport), mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            returncode = capture.main()
        metadata = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
        return returncode, out, metadata, stdout.getvalue(), stderr.getvalue()

    def test_api_cross_origin_round_trip_is_blocked_before_request(self):
        transport = FakeTransport()
        page_url = "http://origin.example/"
        api_url = "http://origin.example/api/data"
        cross_url = "http://cross.example/collect"
        final_url = "http://origin.example/final"
        transport.add(page_url, body=b"<html><body>public page</body></html>")
        transport.redirect(api_url, cross_url)
        transport.redirect(cross_url, final_url)
        transport.add(final_url, body=b'{"ok":true}', headers={"Content-Type": "application/json"})

        returncode, out, metadata, stdout, stderr = self.run_cli(transport, page_url, "--api-url", api_url)

        self.assertEqual(transport.requests, [request_key(page_url), request_key(api_url)])
        self.assertNotIn(request_key(cross_url), transport.requests)
        self.assertEqual(returncode, 1, stderr)
        self.assertEqual(metadata["api"]["status"], "failed")
        self.assertTrue(any(item.startswith("API_FETCH_FAILED:") for item in metadata["warnings"]))
        self.assertFalse((out / "api.json").exists())
        self.assertFalse((out / "api.raw.json").exists())
        self.assertFalse((out / "api.raw.bin").exists())
        self.assertNotIn("Traceback", stdout + stderr)

    def test_script_cross_origin_round_trip_is_blocked_before_request(self):
        transport = FakeTransport()
        page_url = "http://origin.example/"
        script_url = "http://origin.example/app.js"
        cross_url = "http://cross.example/collect.js"
        final_url = "http://origin.example/final.js"
        transport.add(page_url, body=b"<html><script src='/app.js'></script><body>public page</body></html>")
        transport.redirect(script_url, cross_url)
        transport.redirect(cross_url, final_url)
        transport.add(final_url, body=b'window.ok = true;', headers={"Content-Type": "application/javascript"})

        returncode, out, metadata, stdout, stderr = self.run_cli(transport, page_url, "--fetch-scripts")

        self.assertEqual(transport.requests, [request_key(page_url), request_key(script_url)])
        self.assertNotIn(request_key(cross_url), transport.requests)
        self.assertEqual(returncode, 1, stderr)
        scripts = json.loads((out / "scripts.json").read_text(encoding="utf-8"))
        self.assertEqual(scripts[0]["status"], "failed")
        self.assertTrue(any(item.startswith("SCRIPT_FETCH_FAILED:") for item in metadata["warnings"]))
        self.assertFalse((out / "scripts" / "app.js").exists())
        self.assertNotIn("Traceback", stdout + stderr)

    def test_fetch_expected_origin_checks_scheme_host_and_effective_port(self):
        start = "http://origin.example:8080/api"
        expected_origin = capture.origin(urllib.parse.urlsplit(start))
        for cross_url in (
            "https://origin.example:8080/collect",
            "http://cross.example:8080/collect",
            "http://origin.example:8081/collect",
        ):
            with self.subTest(cross_url=cross_url):
                transport = FakeTransport()
                transport.redirect(start, cross_url)
                transport.add(cross_url, body=b"must not be requested")
                with offline_network(transport), self.assertRaisesRegex(ValueError, "同源|跨源"):
                    capture.fetch(start, 25, 1024, expected_origin=expected_origin)
                self.assertEqual(transport.requests, [request_key(start)])

    def test_fetch_allows_explicit_default_port_in_same_origin(self):
        transport = FakeTransport()
        start = "http://origin.example:80/api"
        final_url = "http://origin.example/final"
        transport.redirect(start, final_url)
        transport.add(final_url, body=b"ok")
        with offline_network(transport):
            response = capture.fetch(start, 25, 1024, expected_origin=capture.origin(urllib.parse.urlsplit(start)))
        self.assertEqual(response["url"], final_url)
        self.assertEqual(response["data"], b"ok")
        self.assertEqual(transport.requests, [request_key(start), request_key(final_url)])

    def test_page_may_redirect_to_another_public_origin(self):
        transport = FakeTransport()
        start = "http://origin.example/start"
        final_url = "https://public.example/final"
        transport.redirect(start, final_url)
        transport.add(final_url, body=b"<html><body>redirected page</body></html>")
        with offline_network(transport):
            response = capture.fetch(start, 25, 1024)
        self.assertEqual(response["url"], final_url)
        self.assertEqual(transport.requests, [request_key(start), request_key(final_url)])

    def test_api_http_404_and_500_are_failed_and_not_saved(self):
        for status in (404, 500):
            with self.subTest(status=status):
                transport = FakeTransport()
                page_url = "http://origin.example/"
                api_url = "http://origin.example/api/data"
                transport.add(page_url, body=b"<html><body>public page</body></html>")
                transport.add(api_url, status, b'{"error":"must not become evidence"}', {"Content-Type": "application/json"})

                returncode, out, metadata, stdout, stderr = self.run_cli(transport, page_url, "--api-url", api_url)

                self.assertEqual(returncode, 1, stderr)
                self.assertEqual(metadata["api"]["status"], "failed")
                self.assertIn(str(status), metadata["api"]["error"])
                self.assertTrue(any(item.startswith("API_FETCH_FAILED:") for item in metadata["warnings"]))
                self.assertFalse((out / "api.json").exists())
                self.assertFalse((out / "api.raw.json").exists())
                self.assertFalse((out / "api.raw.bin").exists())
                self.assertNotIn("Traceback", stdout + stderr)

    def test_script_http_404_is_failed_and_not_saved(self):
        transport = FakeTransport()
        page_url = "http://origin.example/"
        script_url = "http://origin.example/app.js"
        transport.add(page_url, body=b"<html><script src='/app.js'></script><body>public page</body></html>")
        transport.add(script_url, 404, b"must not become evidence", {"Content-Type": "application/javascript"})

        returncode, out, metadata, stdout, stderr = self.run_cli(transport, page_url, "--fetch-scripts")

        self.assertEqual(returncode, 1, stderr)
        scripts = json.loads((out / "scripts.json").read_text(encoding="utf-8"))
        self.assertEqual(scripts[0]["status"], "failed")
        self.assertIn("404", scripts[0]["error"])
        self.assertTrue(any(item.startswith("SCRIPT_FETCH_FAILED:") for item in metadata["warnings"]))
        self.assertFalse((out / "scripts" / "app.js").exists())
        self.assertNotIn("Traceback", stdout + stderr)

    def test_page_http_404_preserves_evidence_and_exits_two(self):
        transport = FakeTransport()
        page_url = "http://origin.example/missing"
        body = b"<html><head><title>Missing</title></head><body>original error page</body></html>"
        transport.add(page_url, 404, body, {"Content-Type": "text/html; charset=utf-8"})

        returncode, out, metadata, stdout, stderr = self.run_cli(transport, page_url)

        self.assertEqual(returncode, 2, stderr)
        self.assertEqual((out / "page.html").read_bytes(), body)
        self.assertIn("original error page", (out / "visible_text.txt").read_text(encoding="utf-8"))
        self.assertEqual(metadata["status"], 404)
        self.assertEqual(metadata["final_url"], page_url)
        self.assertTrue(any(item.startswith("PAGE_HTTP_ERROR:") and "404" in item for item in metadata["warnings"]))
        self.assertNotIn("Traceback", stdout + stderr)


if __name__ == "__main__":
    unittest.main()
