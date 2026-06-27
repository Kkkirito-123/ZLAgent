"""Credential scrubbing for MCP-side log lines and error returns.

Three callable surfaces:

* :func:`redact_url` — strip basic-auth user:pass and rewrite known
  secret-bearing query parameters (``token``, ``api_key``,
  ``access_token``, ``secret_key``).
* :func:`redact_for_log` — composes :func:`redact_url` with
  :func:`backend.core.redact.redact_text` so a single log line can
  carry both a URL with embedded creds *and* a Bearer/sk- token
  elsewhere on the line.
* :func:`redact_headers` — return a shallow copy of a header dict
  with any known secret-bearing header value replaced by ``[REDACTED]``.

Empty / non-string inputs round-trip unchanged so callers can lean
on these helpers without defensive type checks.
"""
from __future__ import annotations

import re
from typing import Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..core.redact import REDACTED, redact_text


# Headers whose entire value is a credential. Comparison is case-
# insensitive — HTTP headers are case-insensitive on the wire.
_SECRET_HEADER_NAMES = frozenset(
    name.lower() for name in (
        "Authorization",
        "Proxy-Authorization",
        "Cookie",
        "Set-Cookie",
        "X-Api-Key",
        "X-API-Key",
        "X-Auth-Token",
        "X-Amz-Security-Token",
        "X-Goog-Api-Key",
    )
)

# Query-parameter names that almost always carry a secret. We rewrite
# the *value* but keep the parameter name + position so the redacted
# URL is still useful for triage.
_SECRET_QUERY_PARAMS = frozenset(
    name.lower() for name in (
        "token",
        "api_key",
        "apikey",
        "access_token",
        "secret_key",
        "secret",
        "password",
        "client_secret",
        "refresh_token",
    )
)


def redact_url(url: Optional[str]) -> Optional[str]:
    """Return ``url`` with embedded creds and known secret params masked.

    * Basic-auth ``user:pass@`` is stripped entirely (both halves —
      a leaked username is itself a low-grade IOC, and keeping it
      while removing the password gives a false sense of safety).
    * Query parameters whose name is in :data:`_SECRET_QUERY_PARAMS`
      are rewritten to ``<name>=[REDACTED]``.
    * Other parameters and the path are left untouched.
    """
    if url is None:
        return None
    if not isinstance(url, str) or not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        # Pathological URL — fall back to the text-level redaction so
        # we still scrub Bearer/sk- patterns inline.
        return redact_text(url)

    netloc = parts.netloc
    # Replace user-info (basic auth) with a single ``[REDACTED]@`` marker.
    # Keeping the marker means triage logs *show* a credential was
    # present and scrubbed — easier to spot leaks than a silent strip.
    # ``netloc`` is ``[user[:pass]@]host[:port]``.
    if "@" in netloc:
        host_port = netloc.rsplit("@", 1)[1]
        netloc = f"{REDACTED}@{host_port}"

    query = parts.query
    if query:
        rebuilt: list[tuple[str, str]] = []
        for k, v in parse_qsl(query, keep_blank_values=True):
            if k.lower() in _SECRET_QUERY_PARAMS and v:
                rebuilt.append((k, REDACTED))
            else:
                rebuilt.append((k, v))
        query = urlencode(rebuilt, doseq=True)

    return urlunsplit(
        (parts.scheme, netloc, parts.path, query, parts.fragment)
    )


def redact_for_log(text: Optional[str]) -> Optional[str]:
    """Compose URL redaction with :func:`redact_text` for log lines.

    Pulls every URL-shaped token out of ``text``, runs each through
    :func:`redact_url`, then runs the rest of the line through
    :func:`redact_text` to catch loose Bearer / sk- / KEY=value
    patterns the URL pass cannot reach.
    """
    if text is None:
        return None
    if not isinstance(text, str) or not text:
        return text

    url_re = re.compile(r"https?://[^\s\"'<>]+")

    def _swap(match: re.Match[str]) -> str:
        original = match.group(0)
        redacted = redact_url(original)
        return redacted if isinstance(redacted, str) else original

    swapped = url_re.sub(_swap, text)
    return redact_text(swapped)


def redact_headers(headers: Optional[Mapping[str, str]]) -> dict[str, str]:
    """Return a shallow copy of ``headers`` with secret-bearing values masked."""
    if not headers:
        return {}
    out: dict[str, str] = {}
    for k, v in headers.items():
        if not isinstance(k, str):
            out[str(k)] = str(v)
            continue
        if k.lower() in _SECRET_HEADER_NAMES and v:
            out[k] = REDACTED
        else:
            out[k] = v if isinstance(v, str) else str(v)
    return out


__all__ = [
    "redact_url",
    "redact_for_log",
    "redact_headers",
    "REDACTED",
]
