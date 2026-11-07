"""Manual smoke check. Sends a real notification only when run explicitly."""

import argparse
import logging

from notegram import NotegramHandler


def failure_hint(error):
    if error == "connection refused":
        return (
            "Start the local server, or set NOTEGRAM_URL to the remote server’s HTTPS base URL. "
            "Copying a server config does not set the client URL."
        )
    if error == "HTTP 401":
        return "Set the same Notegram authentication token on the client and server."
    if error == "HTTP 403":
        return (
            "The Notegram ingestion API does not generate 403. Check reverse-proxy, "
            "Cloudflare, or access-control rules. Update Notegram to send its application User-Agent."
        )
    if error == "HTTP 404":
        return "Use the server base URL without /v1/events, and check reverse proxy routing."
    if error and error.startswith("HTTP 3"):
        return "Use the final HTTPS base URL; the client does not follow redirects."
    if error and error.startswith("TLS"):
        return (
            "Check the HTTPS hostname and certificate chain, and whether the listener supports TLS."
        )
    if error == "hostname resolution failed":
        return "Check the hostname in NOTEGRAM_URL and its DNS records."
    if error == "HTTP 503":
        return "Check the server database and server logs."
    return "Check NOTEGRAM_URL, server availability, and server logs."


def main(argv=None):
    parser = argparse.ArgumentParser(description="Send one Notegram smoke-test notification")
    parser.add_argument("--config", help="Explicit configuration file or directory")
    parser.add_argument("--url", help="Override the server base URL")
    args = parser.parse_args(argv)
    # A diagnostic probe makes one attempt; avoid hiding the cause behind background retries.
    handler = NotegramHandler(args.config, url=args.url, retries=0)
    logger = logging.getLogger("notegram_test")
    logger.setLevel(logging.INFO)
    console = logging.StreamHandler()
    logger.addHandler(handler)
    logger.addHandler(console)
    try:
        if not handler.enabled:
            raise SystemExit("Notegram is disabled: set NOTEGRAM_TOKEN or [notegram].token.")
        endpoint = handler.config.url.replace(handler.config.token, "[redacted]")
        print(f"Notegram endpoint: {endpoint}", flush=True)
        logger.warning("A test message is sent!")
        completed = handler.flush(timeout=handler.config.timeout + 1)
        if not completed or handler.failed or handler.dropped or handler.sent != 1:
            reason = handler.last_error or (
                "delivery still pending" if not completed else "no event accepted"
            )
            raise SystemExit(f"Notification not confirmed: {reason}.\n{failure_hint(reason)}")
        print(
            "Notification accepted by server. For Telegram delivery, link logger "
            "'notegram_test' to your chat using /links."
        )
    finally:
        logger.removeHandler(handler)
        logger.removeHandler(console)
        handler.close()
        console.close()


if __name__ == "__main__":
    main()
