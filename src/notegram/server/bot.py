"""Telegram long polling and durable delivery, with no Telegram SDK dependency."""

import hmac
import json
import logging
import re
import threading
import time
from urllib.error import HTTPError

from ..transport import post_json
from .config import ServerConfig
from .db import Storage
from .menus import LinkMenus

logger = logging.getLogger(__name__)
LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40, "critical": 50}
HELP = (
    "/links — chat/logger management panel (private chat)\n"
    "/auth TOKEN — unlock management in private\n/logout — revoke management access\n"
    "/link DOMAIN TOKEN [LEVEL] — subscribe (default: warning)\n"
    "/unlink DOMAIN — unsubscribe\n/subscriptions — list subscriptions\n"
    "/level DOMAIN LEVEL — change threshold\n"
    "/pull [SECONDS] — recent messages (up to 100)\n"
    "/debug, /info, /warning, /error, /critical [SECONDS] — history at one level\n"
    "/ping — connection check\n"
    "LEVEL can be a name or a number from 0 to 100.\n"
    "Use /link in a private conversation: TOKEN grants access to all domains."
)


class TelegramError(Exception):
    def __init__(self, code=0, retry_after=0, *, not_modified=False):
        self.code = code
        self.retry_after = retry_after
        self.not_modified = not_modified
        # No Telegram response, request URL, or token in the exception message.
        super().__init__(f"Telegram request failed (code {code})")

    @property
    def permanent(self):
        return self.code in {400, 403, 404}


def split_text(text, limit=3500):
    """Split below Telegram's 4096 limit, counting UTF-16 units conservatively."""
    chunks, current, units = [], [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > limit:
            chunks.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += width
    if current:
        chunks.append("".join(current))
    return chunks or ["(empty message)"]


def event_chunks(event):
    level = logging.getLevelName(event["level"])
    header = f"[{event['domain']}] {level}\n"
    if event["format"] == "markdown":
        # Telegram's legacy Markdown uses these metacharacters.
        header = re.sub(r"([_*`\[])", r"\\\1", header)
    return split_text(header + event["message"])


class TelegramAPI:
    def __init__(self, token, *, stop_event=None):
        if not token or any(c.isspace() for c in token) or "/" in token:
            raise ValueError("A valid Telegram bot token is required")
        self._base = f"https://api.telegram.org/bot{token}/"
        self._send_lock = threading.Lock()
        self._next_send = 0.0
        self._chat_next_send = {}
        self._stop = stop_event if stop_event is not None else threading.Event()

    def call(self, method, payload, timeout=10):
        try:
            result = post_json(self._base + method, payload, timeout=timeout)
        except HTTPError as exc:
            code = exc.code
            retry_after = 0
            not_modified = False
            try:
                body = json.loads(exc.read(65536))
                retry_after = max(0, float(body.get("parameters", {}).get("retry_after", 0)))
                not_modified = "message is not modified" in body.get("description", "").lower()
            except (ValueError, TypeError, AttributeError):
                pass
            finally:
                exc.close()
            raise TelegramError(code, retry_after, not_modified=not_modified) from None
        except (OSError, ValueError, TypeError):
            raise TelegramError() from None
        if not isinstance(result, dict) or not result.get("ok"):
            code = result.get("error_code", 0) if isinstance(result, dict) else 0
            retry = (
                result.get("parameters", {}).get("retry_after", 0)
                if isinstance(result, dict)
                else 0
            )
            unchanged = (
                isinstance(result, dict)
                and "message is not modified" in result.get("description", "").lower()
            )
            raise TelegramError(code, retry, not_modified=unchanged)
        return result["result"]

    def updates(self, offset):
        return self.call(
            "getUpdates",
            {"offset": offset, "timeout": 20, "allowed_updates": ["message", "callback_query"]},
            timeout=25,
        )

    def set_commands(self):
        return self.call(
            "setMyCommands",
            {
                "commands": [
                    {"command": "links", "description": "Manage chat and logger links"},
                    {"command": "help", "description": "Commands and authentication help"},
                    {"command": "subscriptions", "description": "List this chat’s linked loggers"},
                    {"command": "pull", "description": "Read recent logs for this chat"},
                    {"command": "ping", "description": "Check the bot connection"},
                    {"command": "logout", "description": "Revoke your management access"},
                ]
            },
        )

    def send(self, chat_id, text, *, markdown=False, reply_markup=None):
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if markdown:
            payload["parse_mode"] = "Markdown"
        return self._message("sendMessage", payload)

    def edit(self, chat_id, message_id, text, *, reply_markup):
        return self._message(
            "editMessageText",
            dict(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup),
        )

    def answer(self, query_id, text="", *, alert=False):
        # Callback acknowledgement bypasses the sendMessage limiter to stop the spinner promptly.
        return self.call(
            "answerCallbackQuery", dict(callback_query_id=query_id, text=text, show_alert=alert)
        )

    def delete(self, chat_id, message_id):
        try:
            self.call("deleteMessage", dict(chat_id=chat_id, message_id=message_id))
        except TelegramError:
            # Deletion can fail for old messages or insufficient privileges; never echo the token.
            pass

    def _message(self, method, payload):
        chat_id = payload["chat_id"]
        # Polling has a separate connection; sends share a global and per-chat limiter.
        with self._send_lock:
            next_send = max(self._next_send, self._chat_next_send.get(chat_id, 0))
            if self._stop.wait(max(0, next_send - time.monotonic())):
                raise TelegramError()
            try:
                try:
                    return self.call(method, payload)
                except TelegramError as exc:
                    if method == "editMessageText" and exc.not_modified:
                        return None
                    # Invalid Markdown must not permanently lose an otherwise valid notification.
                    if exc.code != 400 or "parse_mode" not in payload:
                        raise
                    payload.pop("parse_mode")
                    return self.call(method, payload)
            except TelegramError as exc:
                if exc.retry_after:
                    self._next_send = time.monotonic() + exc.retry_after
                raise
            finally:
                now = time.monotonic()
                self._next_send = max(self._next_send, now + 0.05)
                self._chat_next_send[chat_id] = now + (3.1 if chat_id < 0 else 1.1)


class TelegramBot:
    """One command poller and one delivery worker per SQLite database.

    Network calls never run in the ingestion request or database transaction.
    Progress is persisted after each chunk; crash-window duplicates remain possible.
    """

    def __init__(self, cfg_path=None, *, config=None, storage=None, api=None):
        self.config = config if config is not None else ServerConfig.load(cfg_path)
        self.db = storage if storage is not None else Storage(self.config.database)
        self._stop = threading.Event()
        self.api = (
            api
            if api is not None
            else TelegramAPI(self.config.telegram_token, stop_event=self._stop)
        )
        self._threads = []
        self.menus = LinkMenus(self.db, self.api, self.config.token)

    def start(self):
        if self._threads:
            raise RuntimeError("Bot already started")
        for target, name in [
            (self._deliver_loop, "notegram-delivery"),
            (self._poll_loop, "notegram-commands"),
        ]:
            thread = threading.Thread(target=target, name=name, daemon=True)
            self._threads.append(thread)
            thread.start()

    def stop(self):
        self._stop.set()
        deadline = time.monotonic() + 30
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))

    def run(self):
        self.start()
        try:
            while not self._stop.wait(1):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def deliver_once(self):
        event = self.db.next_delivery()
        if event is None:
            return False
        eid, cid = event["id"], event["chat_id"]
        chunks = event_chunks(event)
        try:
            for index in range(event["next_chunk"], len(chunks)):
                if self._stop.is_set():
                    return False
                self.api.send(cid, chunks[index], markdown=event["format"] == "markdown")
                self.db.checkpoint(eid, cid, index + 1)
            self.db.delivered(eid, cid)
        except TelegramError as exc:
            delay = max(exc.retry_after, min(3600, 2 ** min(event["attempts"] + 1, 12)))
            self.db.retry(
                eid, cid, delay=delay, error=f"Telegram {exc.code}", permanent=exc.permanent
            )
            logger.warning("Telegram delivery deferred (code %s)", exc.code)
        return True

    def _deliver_loop(self):
        while not self._stop.is_set():
            try:
                if not self.deliver_once():
                    self._stop.wait(0.5)
            except Exception:
                logger.error("Delivery worker failed; retrying")
                self._stop.wait(2)

    def poll_once(self):
        offset = self.db.get_state("telegram_offset", 0)
        for update in self.api.updates(offset):
            if self._stop.is_set():
                break
            try:
                self.handle_update(update)
            except TelegramError as exc:
                if not exc.permanent:
                    raise
                # A blocked chat or an invalid reply must not poison the update queue.
                logger.warning("Skipping an undeliverable command reply (code %s)", exc.code)
            # Telegram confirms only on the next getUpdates call with this offset.
            self.db.set_state("telegram_offset", update["update_id"] + 1)

    def _poll_loop(self):
        failures = 0
        commands_registered = False
        while not self._stop.is_set():
            try:
                if not commands_registered:
                    try:
                        self.api.set_commands()
                        commands_registered = True
                    except TelegramError as exc:
                        # Command discovery is optional; polling must still work if registration fails.
                        commands_registered = exc.permanent
                        logger.warning(
                            "Could not register Telegram command menu (code %s)", exc.code
                        )
                self.poll_once()
                failures = 0
            except TelegramError as exc:
                failures += 1
                logger.warning("Telegram polling deferred (code %s)", exc.code)
                self._stop.wait(max(exc.retry_after, min(60, 2 ** min(failures, 6))))
            except Exception:
                logger.error("Command worker failed; retrying")
                self._stop.wait(5)

    def _reply(self, chat_id, text):
        for chunk in split_text(text):
            self.api.send(chat_id, chunk)

    @staticmethod
    def _level(value):
        level = LEVELS.get(value.lower())
        if level is None:
            level = int(value)
        if not 0 <= level <= 100:
            raise ValueError
        return level

    def handle_update(self, update):
        if "callback_query" in update:
            self.menus.callback(update["callback_query"])
            return
        message = update.get("message", {})
        text = message.get("text", "")
        if not text.startswith("/") or "chat" not in message:
            return
        cid = message["chat"]["id"]
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()[1:]
        args = parts[1:]
        # Capture names for new chats; legacy subscriptions still appear by numeric ID.
        self.db.remember_chat(message["chat"])
        if self.menus.command(message, command, args):
            return
        if (
            cid < 0
            and command in {"unlink", "level"}
            and not self.menus.authorized(message.get("from", {}).get("id"))
        ):
            self._reply(cid, "Manage group links in private with /auth TOKEN and /links.")
            return
        try:
            if command == "start" and self.menus.authorized(message.get("from", {}).get("id")):
                self.menus.command(message, "links", [])
            elif command in {"start", "help"}:
                self._reply(cid, HELP)
            elif command == "ping":
                self._reply(cid, "pong")
            elif command == "link":
                if len(args) not in {2, 3}:
                    raise ValueError
                domain, token = args[:2]
                if not hmac.compare_digest(token.encode(), self.config.token.encode()):
                    self._reply(cid, "Authentication failed.")
                    return
                if not 1 <= len(domain) <= 100 or any(ord(c) < 32 for c in domain):
                    raise ValueError
                level = self._level(args[2]) if len(args) == 3 else 30
                self.db.link_chat(domain, cid, level)
                self._reply(cid, f"Subscribed to {domain} at level {level}.")
            elif command == "unlink":
                if len(args) != 1:
                    raise ValueError
                self.db.unlink_chat(args[0], cid)
                self._reply(cid, f"Unsubscribed from {args[0]}.")
            elif command == "subscriptions":
                rows = self.db.subscriptions(cid)
                self._reply(
                    cid,
                    "\n".join(f"{r['domain']}: {r['level']}" for r in rows) or "No subscriptions.",
                )
            elif command == "level":
                if len(args) != 2:
                    raise ValueError
                if args[0] not in {row["domain"] for row in self.db.subscriptions(cid)}:
                    self._reply(cid, "Subscribe with /link first.")
                    return
                level = self._level(args[1])
                self.db.link_chat(args[0], cid, level)
                self._reply(cid, f"Threshold set to {level} for {args[0]}.")
            elif command == "pull" or command in LEVELS:
                if len(args) > 1:
                    raise ValueError
                past = int(args[0]) if args else 3600
                if not 1 <= past <= 30 * 86400:
                    raise ValueError
                low, high = (0, 100) if command == "pull" else (LEVELS[command], LEVELS[command])
                events = self.db.history(cid, low, high, past)
                if not events:
                    self._reply(cid, "No messages.")
                for event in events:
                    for chunk in event_chunks(event):
                        self.api.send(cid, chunk, markdown=event["format"] == "markdown")
            else:
                self._reply(cid, "Unknown command. Use /help.")
        except (ValueError, TypeError):
            self._reply(cid, "Invalid arguments. Use /help for command syntax.")
