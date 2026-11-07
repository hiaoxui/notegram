"""Local-only tests: temporary SQLite/HTTP and fake Telegram; no live notifications."""

import io
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from notegram import NotegramHandler
from notegram.client import ClientConfig
from notegram.config import ConfigError, load_config
from notegram.server.api import IngestionServer
from notegram.server.bot import TelegramAPI, TelegramBot, TelegramError
from notegram.server.config import ServerConfig
from notegram.server.db import Storage
from notegram.transport import post_json

ROOT = Path(__file__).resolve().parents[1]


def event(eid="event-1", message="hello", **kwargs):
    return dict(
        id=eid,
        domain="app",
        level=30,
        timestamp=time.time(),
        message=message,
        format="plain",
        **kwargs,
    )


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.previous_cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.previous_cwd)
        environment = {"HOME": self.temp.name, "XDG_CONFIG_HOME": str(self.root / ".config")}
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def config_file(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path


class ConfigTests(IsolatedTest):
    def test_merge_precedence_and_explicit_directory(self):
        self.config_file(
            self.root / ".config/notegram/notegram.toml",
            '[notegram]\nurl="https://user.example"\ntoken="user"\n'
            "[notegram.client]\nqueue_size=12\ntimeout=7\n",
        )
        self.config_file(self.root / "notegram.toml", '[notegram]\ntoken="cwd"\n')
        explicit = self.config_file(
            self.root / "chosen/notegram.toml", '[notegram]\ntoken="explicit"\n'
        )
        self.assertEqual(ClientConfig.load(explicit.parent).token, "explicit")
        with patch.dict(os.environ, {"NOTEGRAM_TOKEN": "environment"}):
            config = ClientConfig.load(explicit)
            self.assertEqual(config.token, "environment")
            self.assertEqual(config.queue_size, 12)
            self.assertEqual(config.timeout, 7)
            config = ClientConfig.load(explicit, token="argument")
            self.assertEqual(config.token, "argument")
            self.assertNotIn("argument", repr(config))

    def test_legacy_config_and_constructor(self):
        cfg = self.config_file(
            self.root / "legacy/notegram.toml",
            '[notegram.telegram]\nsecret="shared"\ntoken="bot"\n',
        )
        client = ClientConfig.load(cfg.parent)
        server = ServerConfig.load(cfg)
        self.assertEqual(client.token, server.token)
        self.assertEqual(server.telegram_token, "bot")
        handler = NotegramHandler(cfg.parent)
        self.addCleanup(handler.close)
        self.assertTrue(handler.enabled)

    def test_missing_and_invalid_configuration(self):
        with self.assertRaises(ConfigError):
            ClientConfig.load("missing.toml")
        with self.assertRaises(ConfigError):
            ServerConfig.load()
        for url in [
            "http://remote.example",
            "https://user:pass@example.com",
            "https://example.com?token=x",
            "ftp://example.com",
            "https://a:bad",
        ]:
            with self.subTest(url=url), self.assertRaises(ConfigError):
                ClientConfig.load(url=url, token="shared")
        for values in [{"queue_size": 0}, {"timeout": float("nan")}, {"retries": -1}]:
            with self.assertRaises(ConfigError):
                ClientConfig.load(token="shared", **values)
        with self.assertWarns(RuntimeWarning):
            handler = NotegramHandler()
        handler.close()

    def test_repository_local_configuration_loads_without_network(self):
        local = ROOT / "notegram.toml"
        if not local.exists():
            self.skipTest("Local notegram.toml is intentionally not checked in")
        raw = load_config(local)
        server = ServerConfig.load(local)
        client = ClientConfig.load(local)
        self.assertTrue(server.token)
        self.assertEqual(server.token, client.token)
        self.assertEqual(server.telegram_token, raw.get("telegram", {}).get("token", ""))

    def test_example_file_is_not_loaded_from_repository_working_directory(self):
        self.config_file(
            self.root / "notegram.example.toml",
            '[notegram]\ntoken="placeholder"\nurl="https://example.com"\n',
        )
        self.config_file(
            self.root / "notegram.toml", '[notegram.telegram]\nsecret="actual-secret"\n'
        )
        config = ClientConfig.load()
        self.assertEqual(config.token, "actual-secret")
        self.assertEqual(config.url, "http://127.0.0.1:8765")


class HTTPTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.storage = Storage(self.root / "test.sqlite3")
        self.storage.link_chat("app", 123)
        self.server = IngestionServer(("127.0.0.1", 0), self.storage, "shared")
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()

    def post(self, payload, token="shared"):
        return post_json(self.url + "/v1/events", payload, token=token)

    def test_auth_idempotence_and_conflict(self):
        data = event()
        with self.assertRaises(HTTPError) as caught:
            self.post(data, token="wrong")
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()
        self.assertFalse(self.post(data)["duplicate"])
        self.assertTrue(self.post(data)["duplicate"])
        with self.storage.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 1)
        with self.assertRaises(HTTPError) as caught:
            self.post(dict(data, message="different"))
        self.assertEqual(caught.exception.code, 409)
        caught.exception.close()

    def test_validation_and_health(self):
        for changes in [
            {"level": True},
            {"timestamp": -1},
            {"domain": ""},
            {"format": "html"},
            {"message": "x" * (128 * 1024 + 1)},
            {"id": "bad\n"},
            {"domain": "\ud800"},
            {"message": "\ud800"},
            {"unexpected": 1},
        ]:
            with self.subTest(changes=list(changes)), self.assertRaises(HTTPError) as caught:
                self.post(dict(event(), **changes))
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()
        request = Request(
            self.url + "/v1/events",
            b"{}",
            method="POST",
            headers={"Authorization": "Bearer shared", "Content-Type": "text/plain"},
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request)
        self.assertEqual(caught.exception.code, 415)
        caught.exception.close()
        with urlopen(self.url + "/healthz") as response:
            self.assertEqual(json.load(response), {"status": "ok"})

    def test_real_handler_exception_markdown_and_record_snapshot(self):
        handler = NotegramHandler(url=self.url, token="shared")
        self.addCleanup(handler.close)
        logger = logging.getLogger("app")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        logger.warning("MARKDOWN *value %s*", 42)
        try:
            raise RuntimeError("test traceback")
        except RuntimeError:
            logger.exception("failed")
        self.assertTrue(handler.flush())
        self.assertEqual(handler.sent, 2)
        with self.storage.connect() as conn:
            rows = conn.execute("SELECT message,format FROM events ORDER BY received").fetchall()
        self.assertEqual(rows[0]["message"], "*value 42*")
        self.assertEqual(rows[0]["format"], "markdown")
        self.assertIn("RuntimeError: test traceback", rows[1]["message"])
        self.assertEqual(rows[1]["format"], "plain")

    def test_http_failure_does_not_recurse_through_logging(self):
        handler = NotegramHandler(url=self.url, token="wrong", retries=0)
        self.addCleanup(handler.close)
        logger = logging.getLogger("notegram")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        logger.error("a failing notification")
        self.assertTrue(handler.flush())
        self.assertEqual(handler.failed, 1)
        self.assertEqual(handler.sent, 0)
        self.assertEqual(handler.last_error, "HTTP 401")


class HandlerTests(IsolatedTest):
    def test_unexpected_sender_error_does_not_kill_worker(self):
        with patch("notegram.client.handler.post_json", side_effect=RuntimeError("unexpected")):
            handler = NotegramHandler(token="shared")
            self.addCleanup(handler.close)
            record = logging.LogRecord("app", 30, "", 1, "hello", (), None)
            handler.handle(record)
            self.assertTrue(handler.flush())
            self.assertTrue(handler._worker.is_alive())
            self.assertEqual(handler.failed, 1)

    def test_retry_keeps_id_and_snapshots_record(self):
        ids, messages = [], []

        def send(url, payload, **kwargs):
            ids.append(payload["id"])
            messages.append(payload["message"])
            if len(ids) == 1:
                raise HTTPError(url, 503, "temporary", {}, io.BytesIO())
            return {"id": payload["id"]}

        with patch("notegram.client.handler.post_json", side_effect=send):
            handler = NotegramHandler(token="shared", retries=1)
            self.addCleanup(handler.close)
            record = logging.LogRecord("app", 30, "", 1, "hello %s", ("world",), None)
            handler.handle(record)
            record.msg = "mutated"
            self.assertTrue(handler.flush())
            self.assertEqual(handler.sent, 1)
            self.assertEqual(len(ids), 2)
            self.assertEqual(ids[0], ids[1])
            self.assertEqual(messages, ["hello world", "hello world"])

    def test_bounded_queue_and_shutdown(self):
        entered, release = threading.Event(), threading.Event()

        def send(url, payload, **kwargs):
            entered.set()
            release.wait(2)
            return {"id": payload["id"]}

        with patch("notegram.client.handler.post_json", side_effect=send):
            handler = NotegramHandler(token="shared", queue_size=1, flush_timeout=0.01)
            self.addCleanup(handler.close)
            record = logging.LogRecord("app", 30, "", 1, "hello", (), None)
            handler.handle(record)
            self.assertTrue(entered.wait(1))
            for _ in range(20):
                handler.handle(record)
            self.assertEqual(handler.dropped, 19)
            self.assertFalse(handler.flush(timeout=0.01))
            started = time.monotonic()
            handler.close()
            self.assertLess(time.monotonic() - started, 0.5)
            release.set()
            handler._worker.join(2)
            self.assertFalse(handler._worker.is_alive())
            self.assertEqual(handler.dropped, 20)

    def test_redirect_does_not_forward_credentials(self):
        from notegram.transport import NoRedirect

        request = Request("https://source.example", headers={"Authorization": "Bearer shared"})
        with self.assertRaises(HTTPError) as caught:
            NoRedirect().redirect_request(
                request, io.BytesIO(), 302, "", {}, "https://other.example"
            )
        caught.exception.close()


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.failure = None
        self.fail_at = None
        self.batch = []
        self.offsets = []
        self.panels = []
        self.answers = []
        self.deleted = []

    def send(self, cid, text, **kwargs):
        if self.failure and (self.fail_at is None or len(self.sent) == self.fail_at):
            failure, self.failure = self.failure, None
            raise failure
        self.sent.append((cid, text, kwargs))
        if "reply_markup" in kwargs:
            self.panels.append((cid, text, kwargs["reply_markup"]))

    def edit(self, cid, message_id, text, *, reply_markup):
        self.panels.append((cid, text, reply_markup))

    def answer(self, query_id, text="", *, alert=False):
        self.answers.append((query_id, text, alert))

    def delete(self, cid, message_id):
        self.deleted.append((cid, message_id))

    def updates(self, offset):
        self.offsets.append(offset)
        return [u for u in self.batch if u["update_id"] >= offset]


class BotTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.storage = Storage(self.root / "bot.sqlite3")
        self.api = FakeTelegram()
        self.config = ServerConfig(
            "shared", str(self.root / "bot.sqlite3"), telegram_token="fake:bot"
        )
        self.bot = TelegramBot(config=self.config, storage=self.storage, api=self.api)

    def command(self, text, cid=123, uid=1):
        return {"update_id": uid, "message": {"chat": {"id": cid}, "text": text}}

    def delivery(self):
        with self.storage.connect() as conn:
            return dict(conn.execute("SELECT * FROM deliveries").fetchone())

    def test_subscriptions_auth_levels_unlink_and_history_scope(self):
        self.bot.handle_update(self.command("/link app wrong"))
        self.assertEqual(self.storage.subscriptions(123), [])
        self.bot.handle_update(self.command("/link app shared warning"))
        self.storage.post(event())
        self.bot.handle_update(self.command("/pull", cid=456))
        self.assertEqual(self.api.sent[-1][1], "No messages.")
        self.bot.handle_update(self.command("/level app error"))
        self.assertEqual(self.storage.subscriptions(123)[0]["level"], 40)
        self.bot.handle_update(self.command("/warning"))
        self.assertIn("hello", self.api.sent[-1][1])
        self.bot.handle_update(self.command("/pull nonsense"))
        self.assertIn("Invalid arguments", self.api.sent[-1][1])
        self.bot.handle_update(self.command("/unlink app"))
        self.assertEqual(self.storage.subscriptions(123), [])
        self.assertIsNone(self.storage.next_delivery())

    def test_delivery_survives_restart_without_time_window(self):
        self.storage.link_chat("app", 123)
        old = event()
        old["timestamp"] = time.time() - 365 * 86400
        self.storage.post(old)
        restarted = TelegramBot(
            config=self.config, storage=Storage(self.storage.path), api=self.api
        )
        self.assertTrue(restarted.deliver_once())
        self.assertEqual(self.delivery()["status"], "sent")
        self.assertFalse(restarted.deliver_once())
        self.assertEqual(len(self.api.sent), 1)

    def test_rate_limit_is_persisted_and_retried(self):
        self.storage.link_chat("app", 123)
        self.storage.post(event())
        self.api.failure = TelegramError(429, retry_after=60)
        self.bot.deliver_once()
        row = self.delivery()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 1)
        self.assertGreater(row["next_attempt"], time.time() + 50)
        self.assertFalse(self.bot.deliver_once())
        with self.storage.connect() as conn:
            conn.execute("UPDATE deliveries SET next_attempt=0")
        self.bot.deliver_once()
        self.assertEqual(self.delivery()["status"], "sent")

    def test_long_message_resumes_after_last_confirmed_chunk(self):
        self.storage.link_chat("app", 123)
        self.storage.post(event(message="😀" * 5000))
        self.api.failure = TelegramError(500)
        self.api.fail_at = 1
        self.bot.deliver_once()
        self.assertEqual(self.delivery()["next_chunk"], 1)
        first = self.api.sent[0]
        with self.storage.connect() as conn:
            conn.execute("UPDATE deliveries SET next_attempt=0")
        restarted = TelegramBot(
            config=self.config, storage=Storage(self.storage.path), api=self.api
        )
        restarted.deliver_once()
        self.assertEqual(self.delivery()["status"], "sent")
        self.assertEqual(self.api.sent.count(first), 1)
        self.assertEqual("".join(row[1] for row in self.api.sent), "[app] WARNING\n" + "😀" * 5000)

    def test_permanent_failure_is_inspectable_and_requeueable(self):
        self.storage.link_chat("app", 123)
        self.storage.post(event())
        self.api.failure = TelegramError(403)
        self.bot.deliver_once()
        self.assertEqual(self.delivery()["status"], "failed")
        self.assertFalse(self.bot.deliver_once())
        self.assertEqual(self.storage.retry_failed(), 1)
        self.bot.deliver_once()
        self.assertEqual(self.delivery()["status"], "sent")

    def test_update_offsets_persist_only_after_handling(self):
        self.api.batch = [self.command("/link app shared", uid=10), self.command("/ping", uid=11)]
        self.bot.poll_once()
        self.assertEqual(self.storage.get_state("telegram_offset"), 12)
        self.bot.poll_once()
        self.assertEqual(len(self.api.sent), 2)
        self.api.batch.append(self.command("/ping", uid=12))
        self.api.failure = TelegramError(500)
        with self.assertRaises(TelegramError):
            self.bot.poll_once()
        self.assertEqual(self.storage.get_state("telegram_offset"), 12)
        self.bot.poll_once()
        self.assertEqual(self.storage.get_state("telegram_offset"), 13)

    def test_blocked_command_chat_does_not_stall_other_updates(self):
        self.api.batch = [
            self.command("/ping", cid=123, uid=1),
            self.command("/ping", cid=456, uid=2),
        ]
        self.api.failure = TelegramError(403)
        self.bot.poll_once()
        self.assertEqual(self.storage.get_state("telegram_offset"), 3)
        self.assertEqual(self.api.sent[0][0], 456)

    def test_markdown_falls_back_and_retry_after_is_respected(self):
        api = TelegramAPI("fake:bot")
        with patch.object(api, "call", side_effect=[TelegramError(400), {"message_id": 1}]) as call:
            api.send(123, "*unclosed", markdown=True)
        self.assertEqual(call.call_count, 2)
        self.assertNotIn("parse_mode", call.call_args.args[1])
        api = TelegramAPI("fake:bot")
        with patch(
            "notegram.server.bot.post_json",
            side_effect=HTTPError(
                "https://redacted", 429, "", {}, io.BytesIO(b'{"parameters":{"retry_after":23}}')
            ),
        ):
            with self.assertRaises(TelegramError) as caught:
                api.send(123, "hello")
        self.assertEqual(caught.exception.retry_after, 23)
        self.assertGreater(api._next_send, time.monotonic() + 22)

    def test_prune_keeps_pending_and_failed_deliveries(self):
        self.storage.link_chat("app", 123)
        self.storage.post(event())
        with self.storage.connect() as conn:
            conn.execute("UPDATE events SET received=0")
        self.assertEqual(self.storage.prune(30), 0)
        self.bot.deliver_once()
        self.assertEqual(self.storage.prune(30), 1)


class ProcessTests(IsolatedTest):
    def test_server_cli_local_config_and_sigterm(self):
        local = ROOT / "notegram.toml"
        if not local.exists():
            local = self.config_file(self.root / "local.toml", '[notegram]\ntoken="test-shared"\n')
        config = ServerConfig.load(local)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(
            os.environ,
            NOTEGRAM_HOST="127.0.0.1",
            NOTEGRAM_PORT=str(port),
            NOTEGRAM_DATABASE=str(self.root / "cli.sqlite3"),
        )
        process = subprocess.Popen(
            [sys.executable, "-m", "notegram", "server", "--api-only", "--config", str(local)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 5
            while True:
                try:
                    with urlopen(url + "/healthz", timeout=0.1) as response:
                        self.assertEqual(response.status, 200)
                    break
                except OSError:
                    if time.monotonic() >= deadline or process.poll() is not None:
                        self.fail("CLI ingestion server did not start")
                    time.sleep(0.02)
            self.assertEqual(
                post_json(url + "/v1/events", event(), token=config.token)["id"], "event-1"
            )
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0)
            self.assertNotIn(config.token.encode(), stdout + stderr)
            if config.telegram_token:
                self.assertNotIn(config.telegram_token.encode(), stdout + stderr)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_client_install_has_no_runtime_dependencies_or_import_side_effects(self):
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                "import importlib.metadata as m, sys; import notegram; "
                'assert all("extra ==" in r for r in (m.requires("notegram") or [])); '
                'assert "telegram" not in sys.modules',
            ],
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(process.returncode, 0, process.stderr.decode())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_client_works_with_server_and_third_party_imports_blocked(self):
        # -I -S omits site-packages, including every installed third-party dependency.
        script = f"""
import sys
sys.path.insert(0, {str(ROOT / "src")!r})

class BlockServer:
    def find_spec(self, fullname, path=None, target=None):
        if (fullname == "notegram.server" or fullname.startswith("notegram.server.")
                or fullname.split(".")[0] in {{"sqlite3", "telegram"}}):
            raise ImportError("Client attempted to import " + fullname)

sys.meta_path.insert(0, BlockServer())
from notegram import NotegramHandler
from notegram.client import NotegramHandler as ClientHandler
from notegram.handler import NotegramHandler as LegacyHandler
assert NotegramHandler is ClientHandler is LegacyHandler
handler = NotegramHandler(url="http://127.0.0.1:8765", token="test-shared")
handler.close()
assert not any(m == "notegram.server" or m.startswith("notegram.server.") for m in sys.modules)
"""
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", script], capture_output=True, timeout=5
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_server_subpackage_entrypoint(self):
        result = subprocess.run(
            [sys.executable, "-m", "notegram.server", "--help"], capture_output=True, timeout=5
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(b"--api-only", result.stdout)


if __name__ == "__main__":
    unittest.main()
