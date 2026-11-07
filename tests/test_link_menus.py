"""Exercise the Telegram management flow locally without sending bot messages."""

import time
import unittest
from unittest.mock import patch

from test_notegram import FakeTelegram, IsolatedTest, event

from notegram.server.bot import TelegramAPI, TelegramBot, TelegramError
from notegram.server.config import ServerConfig
from notegram.server.db import Storage


class LinkMenuTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.db = Storage(self.root / "menus.sqlite3")
        self.api = FakeTelegram()
        self.config = ServerConfig("shared", self.db.path, telegram_token="fake:bot")
        self.bot = TelegramBot(config=self.config, storage=self.db, api=self.api)

    def command(self, text, user=123, chat=None):
        chat = chat if chat is not None else {"id": user, "type": "private", "first_name": "Owner"}
        self.bot.handle_update(
            {"message": {"message_id": 1, "chat": chat, "from": {"id": user}, "text": text}}
        )

    def button(self, title):
        for row in self.api.panels[-1][2]["inline_keyboard"]:
            for button in row:
                if button["text"] == title or button["text"].startswith(title):
                    return button["callback_data"]
        self.fail(f"No button starting with {title!r}")

    def click(self, title=None, *, data=None, user=123, origin=123):
        data = data if data is not None else self.button(title)
        self.bot.handle_update(
            {
                "callback_query": {
                    "id": "query-1",
                    "from": {"id": user},
                    "data": data,
                    "message": {
                        "message_id": 99,
                        "chat": {"id": origin, "type": "private" if origin > 0 else "group"},
                    },
                }
            }
        )
        return data

    def text(self):
        return self.api.panels[-1][1]

    def test_complete_group_link_level_and_confirm_unlink_flow(self):
        self.db.post(event())
        self.command("/links", chat={"id": -100, "type": "supergroup", "title": "Operations"})
        self.assertEqual(self.api.panels, [])
        self.command("/auth shared")
        self.assertEqual(self.api.deleted, [(123, 1)])
        self.assertIn("Chat / logger links", self.text())
        self.click("Operations")
        self.assertIn("No linked loggers", self.text())
        self.click("＋ Add logger")
        self.click("app")
        self.assertIsNone(self.db.link_status("app", -100))
        self.click("Link at WARNING")
        self.assertEqual(self.db.link_status("app", -100)["level"], 30)
        self.assertIn("Threshold: WARNING", self.text())
        self.click("ERROR")
        self.assertEqual(self.db.link_status("app", -100)["level"], 40)
        fresh = event(eid="fresh")
        fresh["level"] = 40
        self.db.post(fresh)
        self.click("‹ Linked loggers")
        self.click("app")
        self.assertIn("Pending deliveries: 1", self.text())
        self.click("Unlink…")
        self.assertIn("Stored log history is kept", self.text())
        self.assertIsNotNone(self.db.link_status("app", -100))
        self.click("Cancel")
        self.assertIsNotNone(self.db.link_status("app", -100))
        self.click("Unlink…")
        confirmation = self.click("Confirm unlink")
        self.assertIsNone(self.db.link_status("app", -100))
        self.assertIsNone(self.db.next_delivery())
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM events").fetchone()[0], 2)
        # Replaying a consumed confirmation cannot delete a subsequently recreated link.
        self.db.link_chat("app", -100)
        self.click(data=confirmation)
        self.assertIsNotNone(self.db.link_status("app", -100))
        self.assertTrue(self.api.answers[-1][2])

    def test_unauthenticated_group_wrong_user_and_forged_callbacks_are_rejected(self):
        self.command("/links")
        self.assertEqual(self.api.panels, [])
        self.command("/auth wrong")
        self.assertFalse(self.bot.menus.authorized(123))
        self.command("/auth shared", chat={"id": -100, "type": "group", "title": "Operations"})
        self.assertFalse(self.bot.menus.authorized(123))
        self.command("/auth shared")
        button = self.button("Owner")
        self.click(data=button, user=456)
        self.assertTrue(self.api.answers[-1][2])
        self.click(data=button, origin=-100)
        self.assertTrue(self.api.answers[-1][2])
        self.click(data="ng:forged")
        self.assertTrue(self.api.answers[-1][2])
        self.command("/logout")
        self.click(data=button)
        self.assertTrue(self.api.answers[-1][2])

    def test_auth_survives_restart_but_token_rotation_and_restart_expire_buttons(self):
        self.command("/auth shared")
        old = self.button("Owner")
        self.bot = TelegramBot(config=self.config, storage=Storage(self.db.path), api=self.api)
        self.assertTrue(self.bot.menus.authorized(123))
        self.click(data=old)
        self.assertIn("expired", self.api.answers[-1][1])
        self.command("/links")
        config = ServerConfig("rotated", self.db.path, telegram_token="fake:bot")
        self.bot = TelegramBot(config=config, storage=self.db, api=self.api)
        self.assertFalse(self.bot.menus.authorized(123))
        self.command("/links")
        self.assertIn("/auth", self.api.sent[-1][1])

    def test_pagination_long_unicode_names_and_stale_link(self):
        self.db.remember_chat({"id": 123, "first_name": "Owner"})
        domains = ["logger-" + str(i) + "😀" * 90 for i in range(10)]
        for domain in domains:
            self.db.link_chat(domain, 123)
        self.command("/auth shared")
        self.click("Owner")
        self.assertIn("Page 1 of 2", self.text())
        self.click("Next ›")
        self.assertIn("Page 2 of 2", self.text())
        self.click("‹ Previous")
        self.click("logger-0")
        set_error = self.button("ERROR")
        self.db.unlink_chat(domains[0], 123)
        self.click(data=set_error)
        self.assertIn("no longer linked", self.text())
        self.assertIsNone(self.db.link_status(domains[0], 123))
        self.assertTrue(all(len(data.encode()) <= 64 for data in self.bot.menus._buttons))
        # Buttons contain opaque references rather than full logger names or the secret.
        self.assertTrue(
            all("logger-" not in data and "shared" not in data for data in self.bot.menus._buttons)
        )

    def test_expired_menu_and_existing_database_legacy_chat_are_handled(self):
        self.db.link_chat("legacy", -999)
        self.command("/auth shared")
        self.assertIn("-999", str(self.api.panels[-1]))
        button = self.button("-999")
        self.bot.menus._buttons[button]["expires"] = time.monotonic() - 1
        self.click(data=button)
        self.assertIn("expired", self.api.answers[-1][1])

    def test_menu_mutations_and_legacy_group_commands_require_the_authorized_actor(self):
        group = {"id": -100, "type": "supergroup", "title": "Operations"}
        self.db.remember_chat(group)
        self.db.link_chat("app", -100)
        self.command("/auth shared")
        self.click("Operations")
        self.click("app")
        button = self.button("ERROR")
        self.click(data=button, user=456)
        self.assertEqual(self.db.link_status("app", -100)["level"], 30)
        self.command("/level app critical", user=456, chat=group)
        self.assertEqual(self.db.link_status("app", -100)["level"], 30)
        self.command("/unlink app", user=456, chat=group)
        self.assertIsNotNone(self.db.link_status("app", -100))
        self.command("/level app critical", chat=group)
        self.assertEqual(self.db.link_status("app", -100)["level"], 50)
        self.command("/logout")
        self.click(data=button)
        self.assertEqual(self.db.link_status("app", -100)["level"], 50)

    def test_callback_updates_use_the_persisted_polling_offset(self):
        self.command("/auth shared")
        button = self.button("Owner")
        self.api.batch = [
            {
                "update_id": 42,
                "callback_query": {
                    "id": "q-42",
                    "from": {"id": 123},
                    "data": button,
                    "message": {"message_id": 99, "chat": {"id": 123, "type": "private"}},
                },
            }
        ]
        self.bot.poll_once()
        self.assertIn("Linked loggers", self.text())
        self.assertEqual(self.db.get_state("telegram_offset"), 43)

    def test_api_sends_markup_edits_and_acknowledges_callbacks(self):
        api = TelegramAPI("fake:bot")
        markup = {"inline_keyboard": [[{"text": "View", "callback_data": "ng:test"}]]}
        with patch.object(api, "call", return_value={"message_id": 1}) as call:
            api.send(123, "Links", reply_markup=markup)
            self.assertEqual(
                call.call_args.args,
                ("sendMessage", {"chat_id": 123, "text": "Links", "reply_markup": markup}),
            )
            api.answer("callback", "Done")
            self.assertEqual(call.call_args.args[0], "answerCallbackQuery")
        api = TelegramAPI("fake:bot")
        with patch.object(api, "call", side_effect=TelegramError(400, not_modified=True)):
            self.assertIsNone(api.edit(123, 99, "Links", reply_markup=markup))
        with patch.object(api, "call", return_value=[]) as call:
            api.updates(10)
            self.assertIn("callback_query", call.call_args.args[1]["allowed_updates"])
            api.set_commands()
            self.assertEqual(call.call_args.args[0], "setMyCommands")
            self.assertIn("links", [c["command"] for c in call.call_args.args[1]["commands"]])


if __name__ == "__main__":
    unittest.main()
