"""Offline full web toolbox boundaries and faithful evidence receipts."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from datasets.lift.full_tools import (
    MAX_BODY,
    MAX_TEXT,
    FullLocalTools,
    WebFailure,
    fetch_public,
    public_destination,
)
from datasets.lift.tools import NAMES
from tests.support.full_lift_web import Connection, Response, dns, page


class DestinationTests(unittest.TestCase):
    def test_reject_private_mixed_dns_userinfo_scheme_and_port(self):
        for ip in (
            "127.0.0.1",
            "10.0.0.1",
            "169.254.169.254",
            "::1",
            "fe80::1",
            "192.0.2.1",
            "::ffff:127.0.0.1",
            "224.0.0.1",
        ):
            with (
                self.subTest(ip=ip),
                patch("datasets.lift.full_tools.socket.getaddrinfo", return_value=dns(ip)),
            ):
                with self.assertRaises(WebFailure):
                    public_destination("https://public.example/")
        with patch(
            "datasets.lift.full_tools.socket.getaddrinfo", return_value=dns() + dns("127.0.0.1")
        ):
            with self.assertRaises(WebFailure):
                public_destination("https://public.example/")
        for url in (
            "file:///etc/passwd",
            "https://user:SECRET@example.org/",
            "http://example.org:8080",
            "https://198.18.0.72/",
        ):
            with self.assertRaises(WebFailure):
                public_destination(url)

    def test_redirect_destination_checked_before_second_connection(self):
        Connection.calls = []
        Connection.responses = [Response(302, headers={"Location": "http://127.0.0.1/private"})]
        with (
            patch("datasets.lift.full_tools.PinnedHTTP", Connection),
            patch(
                "datasets.lift.full_tools.socket.getaddrinfo", side_effect=[dns(), dns("127.0.0.1")]
            ),
        ):
            with self.assertRaisesRegex(WebFailure, "nonpublic_destination"):
                fetch_public("https://example.org/")
        self.assertEqual(len(Connection.calls), 1)
        self.assertEqual(Connection.calls[0][2], "93.184.216.34")

    def test_oversize_declared_streaming_and_http_block(self):
        for response, error in (
            (Response(headers={"Content-Length": str(MAX_BODY + 1)}), "body_limit"),
            (Response(body=b"x" * (MAX_BODY + 1)), "body_limit"),
            (Response(403), "provider_blocked"),
            (Response(202), "provider_challenge"),
        ):
            Connection.responses = [response]
            with (
                patch("datasets.lift.full_tools.PinnedHTTP", Connection),
                patch("datasets.lift.full_tools.socket.getaddrinfo", return_value=dns()),
            ):
                with self.assertRaisesRegex(WebFailure, error):
                    fetch_public("https://example.org/")


class FullToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_shape_parsing_limits_readonly_and_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = FullLocalTools(root / "workspace", readonly=True)
            tools.audit_path = root / "audit"
            self.assertEqual(
                tuple(t.name for t in tools.specs()), NAMES[:-1] + ("search_web", "read_web")
            )
            with patch("datasets.lift.full_tools.fetch_public", return_value=page()):
                result = await tools.search_web({"query": "Python", "max_results": 1})
            self.assertEqual(
                result["results"],
                [
                    {
                        "title": "Official Python",
                        "url": "https://www.python.org/",
                        "snippet": "Python & docs",
                    }
                ],
            )
            self.assertGreater(result["retrieved_at"], 0)
            with patch(
                "datasets.lift.full_tools.fetch_public",
                return_value=page(
                    "<title>Example</title><script>SECRET</script><p>"
                    + "x" * (MAX_TEXT + 1)
                    + "</p>",
                    "text/html",
                ),
            ):
                read = await tools.read_web({"url": "https://example.org/"})
            self.assertEqual(read["title"], "Example")
            self.assertTrue(read["truncated"])
            self.assertEqual(len(read["text"]), MAX_TEXT)
            self.assertNotIn("SECRET", read["text"])
            with self.assertRaises(PermissionError):
                await tools.write_text({"path": "denied", "content": "denied"})
            receipts = [json.loads(p.read_text()) for p in tools.audit_path.glob("*.json")]
            self.assertEqual(len(receipts), 3)
            self.assertEqual(sum(r["output"]["ok"] for r in receipts), 2)
            self.assertTrue(all(r["finished"] >= r["started"] for r in receipts))
            self.assertIn("full_tools_sha256", tools.identity())

    async def test_no_results_challenge_invalid_provider_and_failures_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = FullLocalTools(Path(directory) / "workspace")
            tools.audit_path = Path(directory) / "audit"
            with patch(
                "datasets.lift.full_tools.fetch_public",
                return_value=page(
                    "<html><title>none at DuckDuckGo</title><input name='q' value='none'>No results found</html>"
                ),
            ):
                self.assertEqual(
                    (await tools.search_web({"query": "none"}))["status"], "no_results"
                )
            for body, error in (
                ("<html>verify you are human</html>", "provider_challenge"),
                ("<html>blocked</html>", "invalid_search_response"),
            ):
                with patch("datasets.lift.full_tools.fetch_public", return_value=page(body)):
                    with self.assertRaisesRegex(WebFailure, error):
                        await tools.search_web({"query": "Python"})
            with self.assertRaises(WebFailure):
                await tools.read_web({"url": "https://alice:TOPSECRET@example.org/"})
            for path in tools.audit_path.glob("*.json"):
                self.assertNotIn("TOPSECRET", path.read_text())
            self.assertEqual(len(list(tools.audit_path.glob("*.json"))), 4)

    async def test_original_file_primitives_and_success_failure_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = FullLocalTools(Path(directory) / "workspace")
            tools.audit_path = Path(directory) / "audit"
            await tools.write_text({"path": "report.html", "content": "<h1>Report</h1>"})
            self.assertEqual(
                (await tools.read_text({"path": "report.html"}))["content"], "<h1>Report</h1>"
            )
            with self.assertRaises(WebFailure):
                await tools.invoke("shell", {})
            self.assertEqual(len(list(tools.audit_path.glob("*.json"))), 3)

    async def test_network_failure_is_sanitized_and_default_audit_is_outside_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = FullLocalTools(Path(directory) / "workspace")
            self.assertFalse(tools.audit_path.is_relative_to(Path(tools.workspace)))
            with patch(
                "datasets.lift.full_tools.fetch_public",
                side_effect=RuntimeError("credential TOPSECRET"),
            ):
                with self.assertRaisesRegex(WebFailure, "tool_failure"):
                    await tools.read_web({"url": "https://example.org/"})
            records = list(tools.audit_path.glob("*.json"))
            self.assertEqual(len(records), 1)
            self.assertNotIn("TOPSECRET", records[0].read_text())
            self.assertFalse(json.loads(records[0].read_text())["output"]["ok"])


class DohBoundaryTests(unittest.TestCase):
    def test_fake_domain_only_uses_public_doh_and_literal_rejects(self):
        with (
            patch("datasets.lift.full_tools.socket.getaddrinfo", return_value=dns("198.18.0.72")),
            patch(
                "datasets.lift.full_tools.doh_addresses", return_value=["93.184.216.34"]
            ) as resolve,
        ):
            self.assertEqual(public_destination("https://public.example/")[2], "93.184.216.34")
            resolve.assert_called_once()
            with self.assertRaises(WebFailure):
                public_destination("https://198.18.0.72/")
            self.assertEqual(resolve.call_count, 1)
        with (
            patch("datasets.lift.full_tools.socket.getaddrinfo", return_value=dns("10.0.0.1")),
            patch("datasets.lift.full_tools.doh_addresses") as resolve,
        ):
            with self.assertRaises(WebFailure):
                public_destination("https://lan.example/")
            resolve.assert_not_called()

    def test_doh_wrong_question_and_private_answer_reject(self):
        import time

        from datasets.lift.full_tools import doh_addresses

        for data, code in (
            (
                {"Status": 0, "Question": [{"name": "wrong.example", "type": 1}], "Answer": []},
                "dns_response_mismatch",
            ),
            (
                {
                    "Status": 0,
                    "Question": [{"name": "public.example", "type": 1}],
                    "Answer": [{"type": 1, "data": "127.0.0.1"}],
                },
                "nonpublic_destination",
            ),
        ):
            Connection.responses = [Response(body=json.dumps(data).encode())]
            with patch("datasets.lift.full_tools.PinnedHTTP", Connection):
                with self.assertRaisesRegex(WebFailure, code):
                    doh_addresses("public.example", time.monotonic() + 20)


class CompressedBodyTests(unittest.TestCase):
    def test_gzip_read_and_decompression_limit(self):
        import gzip

        for body, expected in ((b"hello", None), (b"x" * (MAX_BODY + 1), "decoded_body_limit")):
            Connection.responses = [
                Response(
                    body=gzip.compress(body),
                    headers={"Content-Type": "text/plain", "Content-Encoding": "gzip"},
                )
            ]
            with (
                patch("datasets.lift.full_tools.PinnedHTTP", Connection),
                patch("datasets.lift.full_tools.socket.getaddrinfo", return_value=dns()),
            ):
                if expected:
                    with self.assertRaisesRegex(WebFailure, expected):
                        fetch_public("https://example.org/")
                else:
                    self.assertEqual(fetch_public("https://example.org/")["body"], "hello")
