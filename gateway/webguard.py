"""Shared checks that keep a localhost HTTP service out of reach of web pages.

Binding to 127.0.0.1 does not make a service private. Any page the user has open can
issue a cross-origin POST to it: a `fetch` with a string body is a "simple" request,
so no CORS preflight stands in the way and the browser sends it regardless of what
the response allows. The attacker never reads the reply — the side effect is the
attack. For this repo that would mean spending the GitLab token (broker) or starting
a Director run with edits and shell enabled (UI).

Three checks close that off, and each covers a different route in:

  require_client_header  a custom header is not a "simple" request, so a
                         cross-origin caller must first pass a CORS preflight —
                         which these servers refuse (see reject_preflight).
  reject browser Origin  browsers attach Origin to cross-origin POSTs; non-browser
                         clients (curl, the guard shims) never send one.
  host_is_loopback       a DNS-rebinding page resolves its own name to 127.0.0.1,
                         so the socket is loopback but Host is the attacker's name.

None of this authenticates a same-user process — that is out of scope by design, and
README's threat model says so.
"""

import secrets

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def host_is_loopback(host_header):
    """True if the Host header names a loopback address, ignoring any :port."""
    host = (host_header or "").strip()
    if not host:
        return False  # HTTP/1.1 requires Host; absent means a hand-rolled client
    if host.startswith("["):  # [::1]:8600 — strip the port after the bracket
        name = host[: host.find("]") + 1] if "]" in host else host
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return name.lower() in _LOOPBACK_HOSTS


def origin_is_allowed(origin, allowed=()):
    """Absent Origin is fine (non-browser client); otherwise it must be listed.

    Pass allowed=() to refuse every browser origin, which is what a service with no
    browser front end wants.
    """
    return not origin or origin in allowed


def token_matches(supplied, expected):
    """Constant-time compare of a shared secret, tolerating None."""
    if not expected:
        return True
    return secrets.compare_digest(str(supplied or ""), str(expected))


def check(headers, *, allowed_origins=(), client_header=None, client_token=None):
    """Return a reason string if this request must be refused, else None."""
    if not host_is_loopback(headers.get("Host")):
        return "Host header is not loopback"
    if not origin_is_allowed(headers.get("Origin"), allowed_origins):
        return "cross-origin request refused"
    if client_header and not token_matches(headers.get(client_header), client_token):
        return f"missing or bad {client_header} header"
    return None
