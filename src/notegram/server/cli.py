"""python -m notegram server --config /path/to/notegram.toml"""

import argparse
import logging

from ..config import ConfigError
from .config import ServerConfig


def run(argv=None):
    parser = argparse.ArgumentParser(
        description="Notegram HTTPS logging and Telegram notifications"
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="server",
        choices=["server", "bot", "retry-failed", "prune"],
    )
    parser.add_argument("--config", help="TOML file or configuration directory")
    parser.add_argument(
        "--api-only", action="store_true", help="Run ingestion without the Telegram bot"
    )
    parser.add_argument(
        "--days", type=int, default=30, help="Retention age for prune (default: 30)"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = ServerConfig.load(args.config)
        if args.command in {"server", "bot"}:
            # The legacy `bot` command now starts the complete server as well.
            from .api import run_server

            run_server(config, with_bot=not args.api_only)
        else:
            from .db import Storage

            storage = Storage(config.database)
            if args.command == "retry-failed":
                print(f"Requeued {storage.retry_failed()} failed deliveries.")
            elif args.command == "prune":
                if args.days < 1:
                    parser.error("--days must be at least 1")
                print(f"Deleted {storage.prune(args.days)} old events.")
    except ConfigError as exc:
        parser.error(str(exc))
    except Exception:
        # Exception strings may contain credentials.
        parser.exit(
            1,
            "Notegram could not complete the operation; check configuration and service availability.\n",
        )


if __name__ == "__main__":
    run()
