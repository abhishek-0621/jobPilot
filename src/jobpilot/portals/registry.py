"""Which form adapter serves which portal, where its form lives, and which hosts its
browser may reach. Shared by form preparation and live submission."""

from typing import Any
from urllib.parse import urlsplit

from jobpilot.portals.ashby import AshbyAdapter
from jobpilot.portals.greenhouse import GreenhouseAdapter
from jobpilot.portals.lever import LeverAdapter
from jobpilot.portals.playwright_form import PlaywrightFormAdapter

ADAPTERS: dict[str, type[PlaywrightFormAdapter]] = {
    "greenhouse": GreenhouseAdapter, "lever": LeverAdapter, "ashby": AshbyAdapter}
_LOOPBACK = frozenset({"127.0.0.1", "localhost"})


class PortalConfigError(Exception):
    """The application cannot be opened on its portal; ``code`` is a reason code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def allowed_hosts(adapter_cls: type[PlaywrightFormAdapter], url: str) -> set[str]:
    """The application URL's host plus the portal's asset hosts (loopback only for a
    loopback application host, i.e. local test servers)."""
    host = (urlsplit(url).hostname or "").lower()
    hosts = {host, *adapter_cls.ASSET_HOSTS} - {""}
    if host in _LOOPBACK:
        hosts |= _LOOPBACK
    return hosts


def form_url(app: dict[str, Any]) -> str:
    url = app.get("application_url")
    if url:
        return str(url)
    cls = ADAPTERS.get(app.get("source") or "")
    canonical = app.get("canonical_url")
    if cls is None or not canonical:
        raise PortalConfigError("no_application_url")
    return cls.apply_url(str(canonical))
