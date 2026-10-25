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


def _raw_form_url(app: dict[str, Any]) -> str:
    url = app.get("application_url")
    if url:
        return str(url)
    cls = ADAPTERS.get(app.get("source") or "")
    canonical = app.get("canonical_url")
    if cls is None or not canonical:
        raise PortalConfigError("no_application_url")
    return cls.apply_url(str(canonical))


def form_url(app: dict[str, Any]) -> str:
    """Where the form lives. Raises PortalConfigError for an application that must take
    the manual path, so no caller can open a browser for it."""
    if apply_mode(app) != "automated":
        raise PortalConfigError("manual_apply_mode")
    return _raw_form_url(app)


AUTOMATED_SOURCES = frozenset({"greenhouse", "lever", "ashby"})
# Sites that need a login or forbid automation: never opened by a browser, always manual.
DENY_HOSTS = ("linkedin.com", "naukri.com", "indeed.com", "glassdoor.com", "glassdoor.co.in",
              "myworkdayjobs.com", "smartrecruiters.com")


def denied_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in DENY_HOSTS)


def apply_mode(job: Any) -> str:
    """"automated" only for a supported form portal whose application host is not
    deny-listed; everything else (manual adds, LinkedIn, Workday, ...) is "manual"."""
    data = job if isinstance(job, dict) else {
        k: getattr(job, k, None) for k in ("source", "application_url", "canonical_url")}
    if data.get("source") not in AUTOMATED_SOURCES:
        return "manual"
    try:
        url = _raw_form_url(data)
    except PortalConfigError:
        return "manual"
    host = urlsplit(url).hostname or ""
    return "manual" if not host or denied_host(host) else "automated"
