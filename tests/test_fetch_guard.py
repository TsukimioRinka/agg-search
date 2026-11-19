#!/usr/bin/env python3
"""Zero-network tests for the fetch pre-flight guard and DeepSeek size cap.

Run: python3 -m unittest tests.test_fetch_guard -v
DNS is monkeypatched; no sockets, no urllib requests leave the process.
"""
import contextlib
import io
import json
import socket
import subprocess
import sys
import types
import unittest
from unittest import mock

from agg_search import fetch as m

MAX = 4 * 1024 * 1024


def gai(*addresses):
    """Fake getaddrinfo returning one entry per address (in order)."""
    def fake(host, port, *args, **kwargs):
        out = []
        for addr in addresses:
            if ":" in addr:
                out.append((socket.AF_INET6, socket.SOCK_STREAM,
                            socket.IPPROTO_TCP, "", (addr, port, 0, 0)))
            else:
                out.append((socket.AF_INET, socket.SOCK_STREAM,
                            socket.IPPROTO_TCP, "", (addr, port)))
        return out
    return fake


class GuardTest(unittest.TestCase):
    def check(self, url, allow_private=False, addresses=("93.184.216.34",)):
        with mock.patch.object(m.socket, "getaddrinfo", gai(*addresses)):
            return m.check_url(url, allow_private)

    def test_scheme_rejection(self):
        for url, scheme in (("file:///etc/passwd", "file"),
                            ("ftp://example.com/x", "ftp"),
                            ("gopher://example.com/", "gopher"),
                            ("javascript:alert(1)", "javascript"),
                            ("data:text/html,hi", "data")):
            ok, reason = self.check(url)
            self.assertFalse(ok, url)
            self.assertEqual(reason, "unsupported scheme: " + scheme)

    def test_userinfo_and_empty_host(self):
        for url in ("http://user:pass@example.com/",
                    "https://example.com@evil.test/"):
            ok, reason = self.check(url)
            self.assertFalse(ok, url)
            self.assertIn("userinfo", reason)
        for url in ("http:///path", "https://"):
            ok, reason = self.check(url)
            self.assertFalse(ok, url)
            self.assertIn("missing host", reason)

    def test_bad_port_and_idn(self):
        ok, reason = self.check("http://example.com:99999/")
        self.assertFalse(ok)
        self.assertIn("port", reason)
        with mock.patch.object(m.socket, "getaddrinfo",
                               side_effect=AssertionError("DNS must not run")):
            ok, reason = m.check_url("http://bad\ufffd.com/")
        self.assertFalse(ok)
        self.assertIn("IDN", reason)

    def test_localhost_names(self):
        for url in ("http://localhost/", "http://api.localhost/",
                    "http://printer.local/", "http://LOCALHOST./"):
            ok, reason = self.check(url)
            self.assertFalse(ok, url)
            self.assertIn("private host", reason)

    def test_loopback_and_private_addresses(self):
        cases = ("127.0.0.1", "10.1.2.3", "172.16.0.9", "192.168.1.10",
                 "169.254.169.254", "0.0.0.0", "224.0.0.1", "240.0.0.1")
        for addr in cases:
            ok, reason = self.check("http://example.com/", addresses=(addr,))
            self.assertFalse(ok, addr)
            self.assertIn("non-public address", reason)

    def test_ipv6_loopback(self):
        ok, reason = self.check("http://[::1]:8080/", addresses=("::1",))
        self.assertFalse(ok)
        self.assertIn("non-public address", reason)
        ok, reason = self.check("http://[::ffff:169.254.169.254]/",
                                addresses=("::ffff:169.254.169.254",))
        self.assertFalse(ok)

    def test_any_private_address_blocks(self):
        ok, reason = self.check("https://example.com/",
                                addresses=("93.184.216.34", "10.0.0.1"))
        self.assertFalse(ok)
        self.assertIn("10.0.0.1", reason)

    def test_metadata_host_always_blocked(self):
        with mock.patch.object(m.socket, "getaddrinfo",
                               side_effect=AssertionError("DNS must not run")):
            for allow in (False, True):
                ok, reason = m.check_url("http://metadata.google.internal/",
                                         allow)
                self.assertFalse(ok)
                self.assertIn("metadata host", reason)

    def test_allow_private_bypasses_loopback(self):
        for url in ("http://127.0.0.1:8080/x", "http://localhost:3000/",
                    "http://[::1]/", "http://dev.local/"):
            ok, reason = self.check(url, allow_private=True,
                                    addresses=("127.0.0.1",))
            self.assertTrue(ok, "%s: %s" % (url, reason))

    def test_public_host_ok(self):
        ok, reason = self.check("https://example.com/page?q=1")
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")

    def test_unresolvable_host_blocked(self):
        def boom(host, port, *args, **kwargs):
            raise socket.gaierror("nope")
        with mock.patch.object(m.socket, "getaddrinfo", boom):
            ok, reason = m.check_url("https://no-such-host.test/")
        self.assertFalse(ok)
        self.assertIn("cannot resolve", reason)


class DeepSeekSizeTest(unittest.TestCase):
    class Resp:
        status = 200

        def __init__(self, body):
            self.body = body
            self.read_sizes = []

        def read(self, n=-1):
            self.read_sizes.append(n)
            return self.body if n < 0 else self.body[:n]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def run_fetch(self, body):
        resp = self.Resp(body)
        args = types.SimpleNamespace(timeout=5, text_limit=100, out="json")

        def fake_getenv(key, default=None):
            return "test-key" if key == "DEEPSEEK_API_KEY" else default

        with mock.patch.object(m.urllib.request, "urlopen",
                               return_value=resp) as op, \
                mock.patch.object(m, "getenv", fake_getenv):
            d = m.deepseek_fetch("https://example.com/", args, "test-key")
        self.assertEqual(op.call_count, 1)
        return d, resp

    def test_oversize_response(self):
        d, resp = self.run_fetch(b"x" * (MAX + 1))
        self.assertEqual(d["error"], "response too large")
        self.assertFalse(d["blocked"])
        self.assertEqual(resp.read_sizes, [MAX + 1])

    def test_normal_response_still_parses(self):
        md = "# Title\n\n" + ("body " * 100)
        d, _ = self.run_fetch(json.dumps({"content": md,
                                          "title": "T"}).encode())
        self.assertEqual(d["error"], "")
        self.assertEqual(d["title"], "T")
        self.assertEqual(d["status"], 200)


class GuardCliTest(unittest.TestCase):
    def argv(self, *urls):
        return ["crawl-fetch"] + [x for u in urls for x in ("--url", u)]

    def test_all_blocked_exits_3_without_container(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv",
                               self.argv("file:///etc/passwd",
                                         "http://127.0.0.1:9/")), \
                mock.patch.object(m.socket, "getaddrinfo",
                                  gai("127.0.0.1")), \
                mock.patch.object(m, "getenv", lambda k, d=None: None), \
                mock.patch.object(m, "find_launcher",
                                  side_effect=AssertionError("no ladder")), \
                contextlib.redirect_stdout(out):
            rc = m.main()
        self.assertEqual(rc, 3)
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["tier_used"], "guard")
        self.assertTrue(lines[0]["blocked"])
        self.assertEqual(lines[0]["error"],
                         "blocked: unsupported scheme: file")
        self.assertTrue(lines[1]["error"].startswith("blocked: "))

    def test_blocked_url_excluded_from_container(self):
        good = "https://example.com/page"
        fake = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"url": good, "tier_used": "static",
                                      "blocked": False, "error": ""}) + "\n",
            stderr="")
        out = io.StringIO()
        with mock.patch.object(sys, "argv",
                               self.argv("file:///etc/passwd", good)), \
                mock.patch.object(m.socket, "getaddrinfo",
                                  gai("93.184.216.34")), \
                mock.patch.object(m, "getenv", lambda k, d=None: None), \
                mock.patch.object(m, "find_launcher",
                                  return_value="/usr/bin/crawl-env"), \
                mock.patch.object(m.subprocess, "run",
                                  return_value=fake) as run, \
                contextlib.redirect_stdout(out):
            rc = m.main()
        self.assertEqual(rc, 0)
        cmd = run.call_args[0][0]
        self.assertNotIn("file:///etc/passwd", cmd)
        self.assertIn(good, cmd)
        printed = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(len(printed), 2)
        self.assertEqual(printed[0]["tier_used"], "guard")


if __name__ == "__main__":
    unittest.main()
