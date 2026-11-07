"""Manual smoke check. Sends a real notification only when run explicitly."""

import logging

from notegram import NotegramHandler


def main():
    handler = NotegramHandler()
    logger = logging.getLogger("notegram_test")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler())
    try:
        logger.warning("A test message is sent!")
        if not handler.flush() or handler.failed or handler.dropped or not handler.enabled:
            raise SystemExit(
                "Notification was not accepted; check URL, token, and server availability."
            )
    finally:
        handler.close()


if __name__ == "__main__":
    main()
