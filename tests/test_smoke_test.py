"""Smoke-test diagnostics use fake transports, never live Telegram delivery."""

import importlib.util
import io
import os
import socket
import ssl
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from urllib.error import URLError

from test_notegram import ROOT, IsolatedTest

spec = importlib.util.spec_from_file_location("manual_smoke", ROOT / "test_notegram.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class SmokeDiagnosticsTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.config = self.config_file(
            self.root / "notegram.toml",
            '[notegram]\nurl="https://server.example"\ntoken="private-test-token"\n',
        )

    def test_prints_loaded_files_environment_and_effective_values_in_order(self):
        home = self.config_file(
            self.root / ".config/notegram.toml",
            '[notegram]\nurl="https://home.example"\ntoken="home-secret"\n'
            '[notegram.telegram]\ntoken="bot-secret"\n',
        )
        selected = self.config_file(self.root / "selected.toml", "[notegram.client]\ntimeout=8\n")
        out = io.StringIO()

        def accept(url, event, **kwargs):
            self.assertEqual(url, "https://argument.example/v1/events")
            self.assertEqual(kwargs["token"], "environment-secret")
            return {"id": event["id"]}

        with (
            patch.dict(
                os.environ,
                {
                    "NOTEGRAM_URL": "https://environment.example",
                    "NOTEGRAM_TOKEN": "environment-secret",
                },
            ),
            patch("notegram.client.handler.post_json", side_effect=accept),
            redirect_stdout(out),
            redirect_stderr(io.StringIO()),
        ):
            smoke.main(["--config", str(selected), "--url", "https://argument.example"])
        printed = out.getvalue()
        markers = [
            f"file: {home}",
            f"file: {self.config}",
            f"file: {selected}",
            "environment: NOTEGRAM_URL",
            "environment: NOTEGRAM_TOKEN",
            "argument: url",
            "Effective client configuration:",
        ]
        positions = [printed.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('"timeout": 8.0', printed)
        self.assertIn('"retries": 0', printed)
        self.assertIn("[redacted]", printed)
        for secret in ["home-secret", "bot-secret", "environment-secret", "private-test-token"]:
            self.assertNotIn(secret, printed)

    def test_environment_selected_file_is_reported_and_explicit_file_takes_precedence(self):
        from notegram.client import ClientConfig

        selected = self.config_file(self.root / "selected.toml", "[notegram.client]\ntimeout=9\n")
        with patch.dict(os.environ, {"NOTEGRAM_CONFIG": str(selected)}):
            config = ClientConfig.load()
            self.assertEqual(config.timeout, 9)
            self.assertTrue(any(s.name == "NOTEGRAM_CONFIG" for s in config.sources))
            explicit = ClientConfig.load(self.config)
            self.assertEqual(explicit.timeout, 5)
            self.assertFalse(any(s.name == "NOTEGRAM_CONFIG" for s in explicit.sources))

    def test_nested_credentials_are_redacted_without_mutating_input(self):
        from notegram.config import redact_config

        original = {
            "token": "auth-secret",
            "client": {"timeout": 5},
            "other": [{"password": "password-secret", "api_key": "key-secret"}],
        }
        masked = redact_config(original)
        self.assertNotIn("auth-secret", str(masked))
        self.assertNotIn("password-secret", str(masked))
        self.assertNotIn("key-secret", str(masked))
        self.assertEqual(masked["client"]["timeout"], 5)
        self.assertEqual(original["token"], "auth-secret")

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
