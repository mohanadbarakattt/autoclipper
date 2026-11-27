"""Keeps web pages away from the dashboard, which has no password.

Listening on this computer only doesn't stop the browser running on it, so every request is checked:

- Host: a website can point its own domain at 127.0.0.1 ("DNS rebinding") and then read and change
  everything. Only IP addresses and names that a website can't point here are answered.
- Sec-Fetch-Site / Origin, on anything that changes something: any website can make a browser send a
  POST to localhost. Browsers say where a request comes from; curl, scripts and agents send neither
  header and work as before.
"""
from __future__ import annotations

import functools
import ipaddress
import logging
import re
import socket

from starlette.requests import Request
from starlette.responses import JSONResponse

from ..config import DASHBOARD_ALLOWED_HOSTS, DASHBOARD_HOST

log = logging.getLogger("dashboard")

LOOPBACK = ("127.0.0.1", "localhost", "::1")
# endings nobody can register on the internet
PRIVATE_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal")
_HOST = re.compile(r"(\[[^\[\]]+\]|[^\[\]:/@\\,\s]+)(?::\d*)?")


def _name(host: str) -> str:
    """'My-PC.local.:8090' -> 'my-pc.local', '[::1]:8090' -> '[::1]'; '' if it isn't a host."""
    m = _HOST.fullmatch(host)
    return m[1].lower().rstrip(".") if m else ""


_FROM_SETTINGS = {_name(h) for h in (DASHBOARD_HOST, *DASHBOARD_ALLOWED_HOSTS)}


@functools.lru_cache(maxsize=1)
def computer_names() -> frozenset[str]:
    return frozenset(_name(n) for n in (socket.gethostname(), socket.getfqdn()))


def host_allowed(host: str) -> bool:
    name = _name(host)
    try:
        ipaddress.ip_address(name.strip("[]"))
        return True  # an address can't be re-pointed the way a name can
    except ValueError:
        pass
    if not name or name.startswith(("[", ".")):
        return False
    if (name in ("localhost", "host.docker.internal") or name.endswith(".localhost")
            or name in _FROM_SETTINGS or name in computer_names()):
        return True
    # Opened to the network (always so inside Docker) it also answers local network names: a computer or
    # Docker service name, mypc.local, ... Whoever runs the network can point those anywhere, so a
    # dashboard for this computer only ignores them.
    return DASHBOARD_HOST not in LOOPBACK and ("." not in name or name.endswith(PRIVATE_SUFFIXES))


def _from_dashboard(request: Request) -> bool:
    """False if a browser says another website (or another app on this computer) sent this."""
    site = request.headers.get("sec-fetch-site")  # sent to https and localhost; pages can't fake it
    if site is not None:
        return site in ("same-origin", "none")
    origin = request.headers.get("origin")  # plain http on the network, and older browsers
    return origin is None or origin.partition("://")[2].lower() == request.headers["host"].lower()


def _refusal(request: Request) -> str | None:
    host = request.headers.get("host", "")
    if not host_allowed(host):
        return (f"Host '{_name(host) or host}' is not allowed. To open the dashboard under this name, add it to "
                "DASHBOARD_ALLOWED_HOSTS in .env and restart.")
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _from_dashboard(request):
        return "Blocked: this request was sent by a different website or app, not by the dashboard."
    return None


class BrowserGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            request = Request(scope)
            why = _refusal(request)
            if why:
                log.warning("refused %s %r: %s", request.method, scope["path"], why)
                return await JSONResponse({"detail": why}, status_code=403)(scope, receive, send)
        await self.app(scope, receive, send)
