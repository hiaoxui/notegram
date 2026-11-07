"""Log cleanup only touches temporary SQLite databases."""

import io
from contextlib import redirect_stdout
from unittest.mock import patch

from notegram.server.cli import run
from notegram.server.db import Storage
from test_notegram import IsolatedTest, event


class CleanupTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.db = Storage(self.root / "logs.sqlite3")

    def test_cleanup_preserves_unfinished_deliveries_recent_logs_and_links(self):
        now = 1800000000
        self.db.link_chat("app", 123)
        self.db.link_chat("app", 456)
        for name in (
            "sent",
            "pending",
            "failed",
            "mixed",
            "recent",
            "boundary",
            "no-chat",
            "old-client",
        ):
            data = event(eid=name)
            if name == "no-chat":
                data["domain"] = "unlinked"
            if name == "old-client":
                data["timestamp"] = 0
            self.db.post(data)
        with self.db.connect() as conn:
            conn.execute("UPDATE events SET received=?", (now - 31 * 86400,))
            conn.execute("UPDATE events SET received=? WHERE id IN ('recent','old-client')", (now,))
            conn.execute("UPDATE events SET received=? WHERE id='boundary'", (now - 30 * 86400,))
            conn.execute("UPDATE deliveries SET status='sent'")
            conn.execute("UPDATE deliveries SET status='pending' WHERE event_id='pending'")
            conn.execute("UPDATE deliveries SET status='failed' WHERE event_id='failed'")
            conn.execute(
                "UPDATE deliveries SET status='pending' WHERE event_id='mixed' AND chat_id=456"
            )
        with patch("notegram.server.db.time.time", return_value=now):
            self.assertEqual(self.db.clean_logs(), 2)
            self.assertEqual(self.db.clean_logs(), 0)
        with self.db.connect() as conn:
            remaining = {row[0] for row in conn.execute("SELECT id FROM events")}
            self.assertEqual(
                remaining, {"pending", "failed", "mixed", "recent", "boundary", "old-client"}
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM deliveries WHERE event_id='sent'").fetchone()[0],
                0,
            )
            self.assertEqual(conn.execute("SELECT count(*) FROM subscriptions").fetchone()[0], 2)
            self.assertEqual(list(conn.execute("PRAGMA foreign_key_check")), [])

    def test_invalid_retention_does_not_delete_logs(self):
        self.db.post(event())
        for days in (0, -1, True, 1.5, "30", None):
            with self.subTest(days=days), self.assertRaises(ValueError):
                self.db.clean_logs(days)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM events").fetchone()[0], 1)

    def test_clean_logs_cli_and_prune_alias(self):
        config = self.config_file(
            self.root / "notegram.toml",
            '[notegram]\ntoken="test-shared"\n[notegram.server]\ndatabase="logs.sqlite3"\n',
        )
        for command in ("clean-logs", "prune"):
            data = event(eid=command)
            self.db.post(data)
            with self.db.connect() as conn:
                conn.execute("UPDATE events SET received=0")
            out = io.StringIO()
            with redirect_stdout(out), patch("notegram.server.cli.logging.basicConfig"):
                run([command, "--days", "7", "--config", str(config)])
            self.assertIn("Deleted 1 old logs.", out.getvalue())
