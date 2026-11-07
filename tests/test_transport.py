"""Verify application identity without making network requests."""

import io
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from notegram.server.bot import TelegramAPI, TelegramError
from notegram.transport import post_json


class TransportTests(unittest.TestCase):
    def test_telegram_connection_diagnostics_do_not_leak_credentials(self):
        api = TelegramAPI("fake:bot")
        with patch("notegram.server.bot.post_json", side_effect=URLError(TimeoutError("secret"))):
            with self.assertRaises(TelegramError) as caught:
                api.updates(0)
        self.assertEqual(caught.exception.reason, "request timed out")
        self.assertNotIn("secret", str(caught.exception))

    def test_application_user_agent_is_sent_for_client_and_telegram_calls(self):
        for token in [None, "test-secret"]:
            with self.subTest(authenticated=token is not None):
                opener = MagicMock()
                opener.open.return_value.__enter__.return_value = io.BytesIO(b'{"ok":true}')
                with patch("notegram.transport.build_opener", return_value=opener):
                    self.assertEqual(
                        post_json("https://example.com/api", {"test": 1}, token=token), {"ok": True}
                    )
                request = opener.open.call_args.args[0]
                self.assertEqual(request.get_header("User-agent"), "notegram/2.0")
                self.assertEqual(request.get_header("Accept"), "application/json")
                self.assertEqual(request.get_header("Content-type"), "application/json")
                self.assertEqual(
                    request.get_header("Authorization"), "Bearer test-secret" if token else None
                )
