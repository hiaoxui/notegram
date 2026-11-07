"""Smoke-test diagnostics use fake transports, never live Telegram delivery."""

import importlib.util
import io
import socket
import ssl
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from urllib.error import URLError

from test_notegram import ROOT, IsolatedTest

spec = importlib.util.spec_from_file_location("manual_smoke", ROOT / "test_notifier.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class SmokeDiagnosticsTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.config = self.config_file(
            self.root / "notegram.toml",
            '[notegram]\nurl="https://server.example"\ntoken="private-test-token"\n',
        )

    def test_specific_transport_failures_are_reported_without_secrets(self):
        cases = [
            (URLError(ConnectionRefusedError("private-test-token")), "connection refused"),
            (URLError(socket.gaierror("private-test-token")), "hostname resolution failed"),
            (URLError(TimeoutError("private-test-token")), "request timed out"),
            (
                URLError(ssl.SSLCertVerificationError("private-test-token")),
                "TLS certificate verification failed",
            ),
            (ValueError("private-test-token"), "invalid server response"),
        ]
        for failure, expected in cases:
            with self.subTest(error=expected):
                out, err = io.StringIO(), io.StringIO()
                with (
                    patch("notegram.client.handler.post_json", side_effect=failure),
                    redirect_stdout(out),
                    redirect_stderr(err),
                    self.assertRaises(SystemExit) as caught,
                ):
                    smoke.main(["--config", str(self.config)])
                combined = out.getvalue() + err.getvalue() + str(caught.exception)
                self.assertIn(expected, combined)
                self.assertIn("https://server.example", combined)
                self.assertNotIn("private-test-token", combined)

    def test_smoke_reports_acceptance_separately_from_telegram_delivery(self):
        out = io.StringIO()

        def acknowledge(url, event, **kwargs):
            self.assertEqual(url, "https://override.example/v1/events")
            return {"id": event["id"]}

        with (
            patch("notegram.client.handler.post_json", side_effect=acknowledge),
            redirect_stdout(out),
            redirect_stderr(io.StringIO()),
        ):
            smoke.main(["--config", str(self.config), "--url", "https://override.example"])
        self.assertIn("accepted by server", out.getvalue())
        self.assertIn("notegram_test", out.getvalue())

    def test_successful_retry_clears_previous_error(self):
        import logging

        from notegram import NotegramHandler

        attempts = 0

        def acknowledge(url, event, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise URLError(ConnectionRefusedError())
            return {"id": event["id"]}

        with patch("notegram.client.handler.post_json", side_effect=acknowledge):
            handler = NotegramHandler(self.config, retries=1)
            self.addCleanup(handler.close)
            handler.handle(logging.LogRecord("app", 30, "", 1, "test", (), None))
            self.assertTrue(handler.flush())
            self.assertEqual(handler.sent, 1)
            self.assertIsNone(handler.last_error)
