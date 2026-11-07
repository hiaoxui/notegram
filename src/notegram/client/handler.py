"""Nonblocking Python logging handler with a bounded, best-effort queue."""

import json
import logging
import queue
import re
import threading
import time
import warnings
from urllib.error import HTTPError, URLError
from uuid import uuid4

from ..protocol import MAX_MESSAGE_BYTES
from ..transport import post_json, transport_error
from .config import ClientConfig


class NotegramHandler(logging.Handler):
    """Send records to Notegram without database drivers or application-thread I/O.

    ``cfg_path`` and legacy ``MARKDOWN`` messages remain supported. A full queue
    drops the newest record. flush() returns whether all queued work completed.
    """

    def __init__(
        self,
        cfg_path=None,
        *,
        url=None,
        token=None,
        queue_size=None,
        timeout=None,
        retries=None,
        flush_timeout=None,
        level=logging.NOTSET,
    ):
        super().__init__(level)
        self._worker = None
        self._stop = threading.Event()
        self._accepting = True
        self.sent = self.failed = self.dropped = 0
        self.last_error: str | None = None
        try:
            self.config = ClientConfig.load(
                cfg_path,
                url=url,
                token=token,
                queue_size=queue_size,
                timeout=timeout,
                retries=retries,
                flush_timeout=flush_timeout,
            )
            self._queue = queue.Queue(self.config.queue_size)
            self.enabled = bool(self.config.token)
            if self.enabled:
                self._worker = threading.Thread(
                    target=self._run, name="notegram-sender", daemon=True
                )
                self._worker.start()
            else:
                warnings.warn(
                    "Notegram disabled: configure NOTEGRAM_TOKEN", RuntimeWarning, stacklevel=2
                )
        except BaseException:
            self._accepting = False
            self.enabled = False
            super().close()
            raise

    def emit(self, record: logging.LogRecord) -> None:
        if not self.enabled or not self._accepting:
            return
        try:
            message = record.getMessage()
            fmt = getattr(record, "notegram_format", "plain")
            if message.startswith("MARKDOWN"):
                message = re.sub(r"^MARKDOWN\s*", "", message)
                fmt = "markdown"
            if record.exc_info:
                message += "\n" + logging.Formatter().formatException(record.exc_info)
            elif record.exc_text:
                message += "\n" + record.exc_text
            if record.stack_info:
                message += "\n" + record.stack_info
            if len(message.encode()) > MAX_MESSAGE_BYTES or len(record.name) > 100:
                raise ValueError("Log record exceeds Notegram limits")
            if fmt not in {"plain", "markdown"}:
                raise ValueError("Invalid notegram_format")
            event = {
                "id": str(uuid4()),
                "timestamp": record.created,
                "domain": record.name,
                "level": record.levelno,
                "message": message,
                "format": fmt,
            }
            # Snapshot now: LogRecords can be modified by other handlers afterward.
            json.dumps(event, allow_nan=False)
            self._queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1
            self.last_error = "queue full"
        except Exception:
            self.failed += 1
            self.last_error = "invalid log record"

    def _send(self, event):
        for attempt in range(self.config.retries + 1):
            if self._stop.is_set():
                return False
            delay = min(0.5 * 2 ** min(attempt, 6), 30)
            try:
                result = post_json(
                    self.config.url + "/v1/events",
                    event,
                    token=self.config.token,
                    timeout=self.config.timeout,
                )
                if result.get("id") != event["id"]:
                    raise ValueError("Invalid acknowledgement")
                self.last_error = None
                return True
            except HTTPError as exc:
                self.last_error = f"HTTP {exc.code}"
                retry = exc.code in {408, 425, 429} or 500 <= exc.code <= 599
                try:
                    delay = min(30, max(delay, float(exc.headers.get("Retry-After", 0))))
                except (ValueError, TypeError, AttributeError):
                    pass
                exc.close()
                if not retry:
                    return False
            except (OSError, URLError, ValueError, TypeError, AttributeError) as exc:
                # Exception strings can contain URLs or secrets; never log them.
                self.last_error = transport_error(exc)
            if attempt < self.config.retries and self._stop.wait(delay):
                break
        return False

    def _run(self):
        while not self._stop.is_set():
            try:
                event = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                try:
                    accepted = self._send(event)
                except Exception:
                    # Keep subsequent records moving even after an unexpected protocol failure.
                    accepted = False
                    self.last_error = "transport failure"
                if accepted:
                    self.sent += 1
                else:
                    self.failed += 1
            finally:
                self._queue.task_done()
        while True:
            try:
                self._queue.get_nowait()
                self.dropped += 1
                self._queue.task_done()
            except queue.Empty:
                break

    def flush(self, timeout=None):
        if not getattr(self, "enabled", False):
            return True
        deadline = time.monotonic() + (self.config.flush_timeout if timeout is None else timeout)
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._queue.all_tasks_done.wait(remaining)
        return True

    def close(self):
        with self.lock:
            if not self._accepting:
                return
            self._accepting = False
        self.flush()
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=0.2)
        super().close()
