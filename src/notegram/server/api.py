"""Private HTTP ingestion service. Put a TLS reverse proxy in front for remote use."""

import hmac
import json
import logging
import math
import signal
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..protocol import MAX_MESSAGE_BYTES
from .cleanup import cleanup_loop
from .config import ServerConfig
from .db import EventConflict, Storage

logger = logging.getLogger(__name__)
MAX_BODY_BYTES = 1024 * 1024


def validate_event(value):
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    required = {"id", "domain", "level", "timestamp", "message"}
    if not required <= value.keys() or value.keys() - (required | {"format"}):
        raise ValueError("Expected id, domain, level, timestamp, message, and optional format")
    event = dict(value)
    event.setdefault("format", "plain")
    if not isinstance(event["id"], str) or not 1 <= len(event["id"]) <= 100:
        raise ValueError("id must be a string of 1 to 100 characters")
    if not isinstance(event["domain"], str) or not 1 <= len(event["domain"]) <= 100:
        raise ValueError("domain must be a string of 1 to 100 characters")
    # JSON can represent lone surrogates; SQLite and UTF-8 cannot.
    event["id"].encode("utf-8")
    event["domain"].encode("utf-8")
    if any(ord(c) < 32 for c in event["domain"] + event["id"]):
        raise ValueError("id and domain cannot contain control characters")
    if type(event["level"]) is not int or not 0 <= event["level"] <= 100:
        raise ValueError("level must be an integer between 0 and 100")
    ts = event["timestamp"]
    if type(ts) not in {int, float} or not math.isfinite(ts) or not 0 <= ts <= 253402300799:
        raise ValueError("timestamp must be a finite Unix timestamp")
    if not isinstance(event["message"], str) or not event["message"]:
        raise ValueError("message must be a nonempty string")
    if len(event["message"].encode()) > MAX_MESSAGE_BYTES:
        raise ValueError("message exceeds 128 KiB")
    if event["format"] not in ("plain", "markdown"):
        raise ValueError("format must be plain or markdown")
    return event


class IngestionServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, storage, token):
        if not token:
            raise ValueError("A shared authentication token is required")
        self.storage = storage
        self.token = token.encode()
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, IngestionHandler)


class IngestionHandler(BaseHTTPRequestHandler):
    # HTTP/1.0 closes each request, bounding malformed-body handling.
    server_version = "Notegram"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        # No request paths, headers, payloads, or tokens in access logs.
        pass

    def respond(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/healthz":
            self.respond(404, {"error": "not found"})
            return
        try:
            with self.server.storage.connect() as conn:
                conn.execute("SELECT 1 FROM events LIMIT 1")
            self.respond(200, {"status": "ok"})
        except Exception:
            self.respond(503, {"error": "storage unavailable"})

    def do_POST(self):
        if self.path != "/v1/events":
            self.respond(404, {"error": "not found"})
            return
        auth = self.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(auth, b"Bearer " + self.server.token):
            self.respond(401, {"error": "unauthorized"})
            return
        if self.headers.get("Transfer-Encoding"):
            self.respond(400, {"error": "transfer encoding is unsupported"})
            return
        if self.headers.get_content_type() != "application/json":
            self.respond(415, {"error": "expected application/json"})
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1:
                raise ValueError
            length = int(lengths[0])
            if not 0 < length <= MAX_BODY_BYTES:
                self.respond(413, {"error": "request body exceeds limit or is empty"})
                return
        except ValueError:
            self.respond(400, {"error": "expected one valid Content-Length"})
            return
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete request body")
            event = validate_event(json.loads(body))
        except (ValueError, UnicodeError, RecursionError, OverflowError):
            self.respond(400, {"error": "invalid event: check types, sizes, and required fields"})
            return
        except OSError:
            self.respond(408, {"error": "request timed out"})
            return
        try:
            created = self.server.storage.post(event)
        except EventConflict:
            self.respond(409, {"error": "event id already used with different contents"})
            return
        except Exception:
            logger.error("Could not persist event")
            self.respond(503, {"error": "storage unavailable"})
            return
        self.respond(201 if created else 200, {"id": event["id"], "duplicate": not created})


def run_server(config: ServerConfig, *, with_bot=True):
    from .bot import TelegramBot

    storage = Storage(config.database)
    server = IngestionServer((config.host, config.port), storage, config.token)
    bot = (
        TelegramBot(config=config, storage=storage) if with_bot and config.telegram_token else None
    )
    stopped = threading.Event()
    cleanup = threading.Thread(
        target=cleanup_loop, args=(storage, stopped), name="notegram-cleanup", daemon=True
    )

    def stop(signum, frame):
        if not stopped.is_set():
            stopped.set()
            # shutdown() must run outside serve_forever's thread.
            threading.Thread(target=server.shutdown, daemon=True).start()

    old_handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        cleanup.start()
        if bot:
            bot.start()
        logger.info("Notegram listening on %s:%s", config.host, config.port)
        server.serve_forever(poll_interval=0.2)
    finally:
        stopped.set()
        server.server_close()
        cleanup.join(timeout=12)
        if bot:
            bot.stop()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
