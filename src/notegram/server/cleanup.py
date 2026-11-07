"""Seven-day retention while the ingestion service is running."""

import logging

logger = logging.getLogger(__name__)
RETENTION_DAYS = 7
CLEANUP_INTERVAL = 3600


def cleanup_loop(storage, stopped, *, interval=CLEANUP_INTERVAL):
    """Clean immediately at startup, then hourly; retry failures on the next run."""
    while not stopped.is_set():
        try:
            deleted = storage.clean_logs(RETENTION_DAYS, stop_event=stopped)
            logger.info(
                "Automatic log cleanup: deleted %s logs older than %s days", deleted, RETENTION_DAYS
            )
        except Exception as exc:
            # Do not expose database contents or credentials through exception strings.
            logger.error("Automatic log cleanup failed (%s); will retry", type(exc).__name__)
        if stopped.wait(interval):
            break
