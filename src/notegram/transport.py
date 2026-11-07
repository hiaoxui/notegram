"""Small JSON transport. Never forward bearer credentials through redirects."""

import json
import socket
import ssl
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


def transport_error(exc):
    """Describe common failures without exposing exception strings, URLs, or secrets."""
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "TLS certificate verification failed"
    if isinstance(reason, ssl.SSLError):
        return "TLS connection failed"
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused"
    if isinstance(reason, socket.gaierror):
        return "hostname resolution failed"
    if isinstance(reason, TimeoutError):
        return "request timed out"
    if isinstance(reason, PermissionError):
        return "connection permission denied"
    if isinstance(reason, (ValueError, TypeError, AttributeError)):
        return "invalid server response"
    return "transport failure"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "Redirects are disabled", headers, fp)


def post_json(url, payload, *, token=None, timeout=5):
    # Identify the application: Cloudflare can reject urllib's default browser signature.
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "notegram/2.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        url, data=json.dumps(payload, allow_nan=False).encode(), headers=headers, method="POST"
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            # Both APIs return small JSON objects; bound untrusted responses.
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ValueError("Response is too large")
            return json.loads(body)
    except HTTPException:
        raise OSError("Incomplete HTTP response") from None
    except RecursionError:
        raise ValueError("Invalid JSON response") from None
