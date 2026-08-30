from __future__ import annotations

import http.client
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def open_trusted_https(
    request: urllib.request.Request,
    *,
    timeout: float,
    host_allowed: Callable[[str], bool],
) -> Iterator[http.client.HTTPResponse]:
    """Open one non-redirecting HTTPS request to an explicitly trusted host."""

    parsed = urllib.parse.urlsplit(request.full_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not host
        or not host_allowed(host)
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise urllib.error.URLError("untrusted HTTPS destination")

    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    connection = http.client.HTTPSConnection(host, port=443, timeout=timeout)
    try:
        connection.request(
            request.get_method(),
            path,
            body=request.data,
            headers=dict(request.header_items()),
        )
        response = connection.getresponse()
        if response.status >= 400:
            raise urllib.error.HTTPError(
                "https://redacted.invalid/",
                response.status,
                response.reason,
                response.headers,
                None,
            )
        yield response
    except urllib.error.HTTPError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise urllib.error.URLError(type(exc).__name__) from exc
    finally:
        connection.close()
