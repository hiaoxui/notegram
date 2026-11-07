"""Server-owned durable storage. Every operation uses its own SQLite connection."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, domain TEXT NOT NULL, level INTEGER NOT NULL,
    timestamp REAL NOT NULL, received REAL NOT NULL,
    message TEXT NOT NULL, format TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_history ON events(domain, timestamp);
CREATE TABLE IF NOT EXISTS subscriptions (
    domain TEXT NOT NULL, chat_id INTEGER NOT NULL, level INTEGER NOT NULL DEFAULT 30,
    PRIMARY KEY(domain, chat_id)
);
CREATE TABLE IF NOT EXISTS deliveries (
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    domain TEXT NOT NULL, chat_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0, next_chunk INTEGER NOT NULL DEFAULT 0,
    last_error TEXT, delivered_at REAL,
    PRIMARY KEY(event_id, chat_id),
    FOREIGN KEY(domain, chat_id) REFERENCES subscriptions(domain, chat_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(status, next_attempt);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chats (chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL);
PRAGMA user_version = 1;
"""


class EventConflict(ValueError):
    """An event ID was reused with different contents."""


class Storage:
    def __init__(self, path="notegram.sqlite3"):
        self.path = str(Path(path).expanduser().resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise ValueError("Database was created by a newer Notegram version")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def post(self, event: dict, *, schedule=True, connection=None) -> bool:
        if connection is None:
            with self.connect() as conn:
                return self.post(event, schedule=schedule, connection=conn)
        conn = connection
        # INSERT serializes writers before checking for existing content.
        cursor = conn.execute(
            """INSERT INTO events
            (id,domain,level,timestamp,received,message,format) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(id) DO NOTHING""",
            (
                event["id"],
                event["domain"],
                event["level"],
                event["timestamp"],
                time.time(),
                event["message"],
                event["format"],
            ),
        )
        if not cursor.rowcount:
            old = dict(conn.execute("SELECT * FROM events WHERE id=?", (event["id"],)).fetchone())
            if any(old[key] != event[key] for key in event):
                raise EventConflict("Event ID already exists with different contents")
            return False
        if schedule:
            conn.execute(
                """INSERT INTO deliveries(event_id,domain,chat_id)
                SELECT ?,domain,chat_id FROM subscriptions WHERE domain=? AND level<=?""",
                (event["id"], event["domain"], event["level"]),
            )
        return True

    def link_chat(self, domain: str, chat_id: int, level=30, *, connection=None):
        if connection is None:
            with self.connect() as conn:
                return self.link_chat(domain, chat_id, level, connection=conn)
        connection.execute(
            """INSERT INTO subscriptions(domain,chat_id,level) VALUES (?,?,?)
            ON CONFLICT(domain,chat_id) DO UPDATE SET level=excluded.level""",
            (domain, chat_id, level),
        )

    def unlink_chat(self, domain, chat_id):
        with self.connect() as conn:
            return (
                conn.execute(
                    "DELETE FROM subscriptions WHERE domain=? AND chat_id=?", (domain, chat_id)
                ).rowcount
                > 0
            )

    def subscriptions(self, chat_id):
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT domain,level FROM subscriptions WHERE chat_id=? ORDER BY domain",
                    (chat_id,),
                )
            ]

    def remember_chat(self, chat):
        title = (
            chat.get("title") or chat.get("first_name") or chat.get("username") or str(chat["id"])
        )
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO chats VALUES (?,?)
                ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title""",
                (chat["id"], title),
            )

    def chats(self):
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute("""SELECT ids.chat_id,
                COALESCE(c.title,CAST(ids.chat_id AS TEXT)) AS title,
                (SELECT count(*) FROM subscriptions s WHERE s.chat_id=ids.chat_id) AS links
                FROM (SELECT chat_id FROM chats UNION SELECT chat_id FROM subscriptions) ids
                LEFT JOIN chats c ON c.chat_id=ids.chat_id ORDER BY title,ids.chat_id""")
            ]

    def loggers(self):
        with self.connect() as conn:
            return [
                row[0]
                for row in conn.execute("""SELECT domain FROM events
                UNION SELECT domain FROM subscriptions ORDER BY domain""")
            ]

    def link_status(self, domain, chat_id):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT level FROM subscriptions WHERE domain=? AND chat_id=?", (domain, chat_id)
            ).fetchone()
            if row is None:
                return None
            counts = dict(
                conn.execute(
                    """SELECT status,count(*) FROM deliveries
                WHERE domain=? AND chat_id=? GROUP BY status""",
                    (domain, chat_id),
                )
            )
            return dict(
                level=row["level"], pending=counts.get("pending", 0), failed=counts.get("failed", 0)
            )

    def set_level(self, domain, chat_id, level):
        """Change an existing link without recreating one removed by another action."""
        with self.connect() as conn:
            return (
                conn.execute(
                    "UPDATE subscriptions SET level=? WHERE domain=? AND chat_id=?",
                    (level, domain, chat_id),
                ).rowcount
                > 0
            )

    def history(self, chat_id, low=10, high=100, past=3600, limit=100):
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT e.* FROM events e JOIN subscriptions s
                ON s.domain=e.domain WHERE s.chat_id=? AND e.level BETWEEN ? AND ?
                AND e.timestamp>=? ORDER BY e.timestamp DESC,e.id DESC LIMIT ?""",
                (chat_id, low, high, time.time() - past, limit),
            )
            return list(reversed([dict(row) for row in rows]))

    def next_delivery(self):
        with self.connect() as conn:
            row = conn.execute(
                """SELECT e.*, d.chat_id,d.attempts,d.next_chunk FROM deliveries d
                JOIN events e ON e.id=d.event_id WHERE d.status='pending' AND d.next_attempt<=?
                ORDER BY d.next_attempt,e.received,e.id LIMIT 1""",
                (time.time(),),
            ).fetchone()
            return dict(row) if row else None

    def checkpoint(self, event_id, chat_id, next_chunk):
        with self.connect() as conn:
            conn.execute(
                "UPDATE deliveries SET next_chunk=? WHERE event_id=? AND chat_id=?",
                (next_chunk, event_id, chat_id),
            )

    def delivered(self, event_id, chat_id):
        with self.connect() as conn:
            conn.execute(
                """UPDATE deliveries SET status='sent',delivered_at=?,last_error=NULL
                WHERE event_id=? AND chat_id=?""",
                (time.time(), event_id, chat_id),
            )

    def retry(self, event_id, chat_id, *, delay, error, permanent=False):
        with self.connect() as conn:
            conn.execute(
                """UPDATE deliveries SET attempts=attempts+1,next_attempt=?,last_error=?,
                status=? WHERE event_id=? AND chat_id=?""",
                (
                    time.time() + delay,
                    error,
                    "failed" if permanent else "pending",
                    event_id,
                    chat_id,
                ),
            )

    def get_state(self, key, default=None):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_state(self, key, value):
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO state VALUES (?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, json.dumps(value)),
            )

    def retry_failed(self):
        with self.connect() as conn:
            return conn.execute(
                "UPDATE deliveries SET status='pending',next_attempt=0 WHERE status='failed'"
            ).rowcount

    def prune(self, days):
        with self.connect() as conn:
            return conn.execute(
                """DELETE FROM events WHERE received<? AND NOT EXISTS
                (SELECT 1 FROM deliveries d WHERE d.event_id=events.id AND d.status!='sent')""",
                (time.time() - days * 86400,),
            ).rowcount
