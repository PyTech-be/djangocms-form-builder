"""Pluggable HTTP transport for the webhook action.

All webhook delivery goes through :func:`send_webhook`, a thin seam that posts a
JSON body and returns ``(status_code, response_text, response_headers)``. This
keeps the feature free of any hard HTTP-client dependency: the default transport
uses `niquests <https://niquests.readthedocs.io/>`_ when it is installed (a
drop-in, maintained ``requests`` replacement) and otherwise falls back to the
standard library's :mod:`urllib.request`.

Advanced users can point ``DJANGOCMS_FORM_BUILDER_WEBHOOK_HTTP_SENDER`` at their
own callable ``sender(url, *, body, headers, timeout) -> (int, str, dict)`` to
use httpx or any other client.
"""

import json as json_module
import logging
import urllib.error
import urllib.request

from django.utils.module_loading import import_string

from .settings import WEBHOOK_HTTP_SENDER

logger = logging.getLogger(__name__)

# Cached resolved sender, so backend detection only happens once.
_sender = None


class WebhookTransportError(Exception):
    """Raised when a webhook could not be delivered (no HTTP response).

    A non-2xx HTTP response is *not* a transport error - it is returned
    normally so the caller can record the status. This is raised only for
    connection failures, timeouts and DNS errors.
    """


def _send_with_niquests(url, *, body, headers, timeout):
    import niquests

    try:
        response = niquests.post(
            url, data=body.encode("utf-8"), headers=headers, timeout=timeout
        )
    except niquests.exceptions.RequestException as exc:
        raise WebhookTransportError(str(exc)) from exc
    return response.status_code, response.text or "", dict(response.headers)


def _send_with_stdlib(url, *, body, headers, timeout):
    request = urllib.request.Request(
        url, data=body.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            text = response.read().decode(charset, errors="replace")
            return response.status, text, dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        # An HTTP error status is still a response, not a transport failure.
        charset = (
            exc.headers.get_content_charset() or "utf-8" if exc.headers else "utf-8"
        )
        text = exc.read().decode(charset, errors="replace")
        response_headers = dict(exc.headers.items()) if exc.headers else {}
        return exc.code, text, response_headers
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WebhookTransportError(str(exc)) from exc


def _resolve_sender():
    if WEBHOOK_HTTP_SENDER:
        return import_string(WEBHOOK_HTTP_SENDER)
    try:
        import niquests  # noqa: F401

        return _send_with_niquests
    except ImportError:
        return _send_with_stdlib


def send_webhook(url, *, json, headers, timeout):
    """POST ``json`` to ``url``.

    Returns ``(status_code, response_text, response_headers)``. ``headers``
    should contain auth/user-agent headers; ``Content-Type: application/json``
    is added automatically. Raises :class:`WebhookTransportError` when no HTTP
    response could be obtained.
    """
    global _sender
    if _sender is None:
        _sender = _resolve_sender()
    body = json_module.dumps(json)
    all_headers = {"Content-Type": "application/json", **headers}
    return _sender(url, body=body, headers=all_headers, timeout=timeout)
