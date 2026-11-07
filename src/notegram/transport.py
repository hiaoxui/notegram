"""Small JSON transport. Never forward bearer credentials through redirects."""

import json
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "Redirects are disabled", headers, fp)


def post_json(url, payload, *, token=None, timeout=5):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
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
