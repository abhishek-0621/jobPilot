"""Public recruiting contacts: published in the posting, a careers-page inbox, or added by
the user. Nothing is ever inferred or constructed (no ``first.last@`` guessing); every
email kept is published text whose domain is the company's own domain.

Posting text and careers pages are untrusted: only regex-extracted, domain-checked
addresses and a deterministic name/title pattern leave this module.
"""

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

import httpcore
import httpx

from jobpilot.contracts.models import Contract
from jobpilot.discovery.normalize import html_to_text, require_http
from jobpilot.discovery.sources.base import RateLimitedFetcher, SourceError

ContactKind = Literal["user", "posting_named", "posting_inbox", "careers_inbox"]
Channel = Literal["email", "linkedin"]

POSTING_CONFIDENCE = 0.9
CAREERS_CONFIDENCE = 0.7
USER_CONFIDENCE = 1.0
MAX_POSTING_CHARS = 12000
INBOX_LOCAL_PARTS = frozenset({"careers", "jobs", "recruiting", "recruitment", "talent",
                               "hr", "hiring", "people"})
_RANK = {"user": 0, "posting_named": 1, "posting_inbox": 2, "careers_inbox": 2}

# ATS / job-board hosts: never the company's own domain.
NON_COMPANY_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com", "myworkdayjobs.com",
    "myworkdaysite.com", "workday.com", "jobvite.com", "workable.com", "icims.com",
    "bamboohr.com", "recruitee.com", "taleo.net", "breezy.hr", "wellfound.com",
    "angel.co", "jazzhr.com", "applytojob.com", "teamtailor.com", "personio.de",
    "personio.com", "rippling.com", "rippling-ats.com", "dover.com", "dover.io", "keka.com",
    "darwinbox.in", "darwinbox.com", "zohorecruit.com", "zohorecruit.in", "freshteam.com",
    "successfactors.com", "successfactors.eu", "oraclecloud.com", "pinpointhq.com",
    "comeet.com", "comeet.co", "recruiterbox.com", "hire.trakstar.com", "jobscore.com",
    "hirehive.com", "gem.com", "notion.site", "google.com", "forms.gle", "typeform.com",
    "github.io", "github.com", "herokuapp.com", "vercel.app", "netlify.app", "pages.dev",
    "web.app", "firebaseapp.com", "wixsite.com", "squarespace.com", "webflow.io",
    "linkedin.com", "naukri.com", "indeed.com", "glassdoor.com", "glassdoor.co.in",
    "instahyre.com", "iimjobs.com", "hirist.com", "cutshort.io", "foundit.in",
    "monster.com", "ziprecruiter.com", "gmail.com", "outlook.com", "yahoo.com",
    "hotmail.com")
_SECOND_LEVEL = frozenset({"co", "com", "org", "net", "ac", "gov", "edu"})

_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]{1,64})@([A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+)")
_MAILTO_RE = re.compile(r"""href\s*=\s*["']?\s*mailto:([^"'?\s>]+)""", re.IGNORECASE)
_NAMED_RE = re.compile(
    r"(?i:contact|recruiter|recruiting contact|hiring contact|talent partner|reach out to)"
    r"\s*[:\-]\s*(?P<name>[A-Z][a-z]+(?: [A-Z][a-z'\-]+){1,2})"
    r"(?:\s*,\s*(?P<title>[A-Z][A-Za-z&/ \-]{1,40}?))?(?=\s*[(<,.;]|\s+at\s|\s*$)")
_TITLE_WORDS = re.compile(r"(?i)\b(recruit\w*|talent|sourc\w*|hr|people|hiring)\b")


class FoundContact(Contract):
    kind: ContactKind
    name: str
    professional_title: str | None
    channel: Channel
    address: str
    public_source_url: str
    confidence: float


def _host_in(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def base_domain(host: str) -> str:
    labels = host.lower().rstrip(".").split(".")
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def domain_matches(domain: str, company: str) -> bool:
    d, c = domain.lower().rstrip("."), company.lower().rstrip(".")
    return bool(c) and (d == c or d.endswith("." + c))


def company_slug(company: str) -> str:
    return re.sub(r"[^a-z0-9]", "", company.casefold())


# Slug fallback (no known company domain) only on common TLDs: a lookalike on an exotic
# TLD (acme.zip, acme.xyz) is never taken for the company's own domain.
SLUG_TLDS = frozenset({"com", "io", "ai", "co", "in", "co.in", "net", "org", "dev", "tech",
                       "app"})


def _slug_matches(domain: str, company: str) -> bool:
    labels = base_domain(domain).split(".")
    slug = company_slug(company)
    return bool(slug) and len(labels) >= 2 and ".".join(labels[1:]) in SLUG_TLDS \
        and re.sub(r"[^a-z0-9]", "", labels[0]) == slug


def company_domain(canonical_url: str | None, application_url: str | None,
                   careers_url: str | None) -> str | None:
    """The company's own domain: the job/apply URL host unless it is an ATS or job board,
    else the configured careers_url host; None when it cannot be determined."""
    for url in (application_url, canonical_url, careers_url):
        if not url or require_http(url) is None:
            continue
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        if not host or "." not in host or _host_in(host, NON_COMPANY_HOSTS):
            continue
        if _is_ip(host):
            continue
        return base_domain(host)
    return None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def third_party(domain: str) -> bool:
    return _host_in(domain.lower().rstrip("."), NON_COMPANY_HOSTS)


def email_domain_ok(domain: str, company_dom: str | None, company: str | None) -> bool:
    """Accept an email domain only when it is the company's own: it matches the company
    domain and is not a known ATS/platform/mail host; otherwise the slug check."""
    if third_party(domain):
        return False
    if company_dom is not None and not third_party(company_dom) \
            and domain_matches(domain, company_dom):
        return True
    return company is not None and _slug_matches(domain, company)


_keep = email_domain_ok


def extract_from_posting(text: str, company_dom: str | None, *, company: str | None = None,
                         source_url: str = "") -> list[FoundContact]:
    """Emails published in the posting whose domain is the company's; a recruiter name and
    title only when they sit on the same line as such an email."""
    if "<" in text and ">" in text:
        text = html_to_text(text, MAX_POSTING_CHARS)
    text = text[:MAX_POSTING_CHARS]
    out: list[FoundContact] = []
    seen: set[str] = set()
    for line in text.splitlines():
        named = _NAMED_RE.search(line)
        for m in _EMAIL_RE.finditer(line):
            local, domain = m.group(1).strip("."), m.group(2).lower().strip(".")
            address = f"{local}@{domain}".lower()
            if not local or address in seen or not _keep(domain, company_dom, company):
                continue
            seen.add(address)
            if named is not None and local.lower() not in INBOX_LOCAL_PARTS:
                title = named.group("title")
                title = title.strip() if title and _TITLE_WORDS.search(title) else None
                out.append(FoundContact(
                    kind="posting_named", name=named.group("name"), professional_title=title,
                    channel="email", address=address, public_source_url=source_url,
                    confidence=POSTING_CONFIDENCE))
            else:
                out.append(FoundContact(
                    kind="posting_inbox", name="Recruiting inbox", professional_title=None,
                    channel="email", address=address, public_source_url=source_url,
                    confidence=POSTING_CONFIDENCE))
    return out


def public_http_url(url: str | None) -> str | None:
    """The URL when it is http(s) on a public host name (no IP literal, no localhost)."""
    url = require_http(url)
    if url is None:
        return None
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if not host or "." not in host or host == "localhost" or host.endswith(".localhost") \
            or _is_ip(host):
        return None
    return url


Resolver = Callable[[str], Awaitable[list[str]]]


async def resolve_host(host: str) -> list[str]:
    """Every address ``host`` resolves to (A and AAAA)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(i[4][0]) for i in infos]


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _public_ip(addr: str) -> bool:
    """Globally routable unicast only (rejects private, CGNAT 100.64/10, loopback, link-
    local, reserved...); IPv6 forms that embed an IPv4 address must embed a global one."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        embedded: list[ipaddress.IPv4Address] = []
        if ip.ipv4_mapped is not None:
            embedded.append(ip.ipv4_mapped)
        if ip in _NAT64:
            embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if ip.sixtofour is not None:
            embedded.append(ip.sixtofour)
        if ip.teredo is not None:
            embedded += list(ip.teredo)
        if embedded:
            return all(e.is_global and not e.is_multicast for e in embedded)
    return ip.is_global and not ip.is_multicast


class PinnedBackend(httpcore.AsyncNetworkBackend):
    """Resolves the host itself, refuses unless every address is public, and connects to
    the address it checked (TLS still uses the original host name for SNI and
    certificates). This closes the DNS-rebinding gap between check and connect."""

    def __init__(self, resolve: Resolver | None = None,
                 inner: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._resolve = resolve
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                          local_address: str | None = None,
                          socket_options: Any = None) -> httpcore.AsyncNetworkStream:
        try:
            addrs = await (self._resolve or resolve_host)(host)
        except (OSError, UnicodeError) as e:
            raise httpcore.ConnectError(f"cannot resolve {host}") from e
        if not addrs or not all(_public_ip(a) for a in addrs):
            raise httpcore.ConnectError(f"{host} resolves to a non-public address")
        return await self._inner.connect_tcp(addrs[0], port, timeout=timeout,
                                             local_address=local_address,
                                             socket_options=socket_options)

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options: Any = None) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def pinned_client() -> httpx.AsyncClient:
    """An httpx client whose every connection goes through ``PinnedBackend`` (no proxy)."""
    transport = httpx.AsyncHTTPTransport()
    transport._pool = httpcore.AsyncConnectionPool(  # only way to set the network backend
        ssl_context=httpx.create_ssl_context(), network_backend=PinnedBackend())
    return httpx.AsyncClient(transport=transport)


async def resolves_public(host: str, resolve: Resolver) -> bool:
    """True only when ``host`` resolves and every address is public (no private, loopback,
    link-local, reserved or unspecified address). Resolution errors are refusals."""
    try:
        addrs = await resolve(host)
    except (OSError, UnicodeError):
        return False
    return bool(addrs) and all(_public_ip(a) for a in addrs)


async def fetch_careers_inbox(url: str | None, fetcher: RateLimitedFetcher,
                              company_dom: str | None, company: str | None = None, *,
                              resolve: Resolver | None = None) -> list[FoundContact]:
    """One GET of the careers page; ``mailto:`` links with an allowlisted local part on the
    company domain. Any fetch error yields no contacts. A host that resolves to a private,
    loopback or link-local address is never fetched."""
    url = public_http_url(url)
    if url is None or (company_dom is None and company is None):
        return []
    if not await resolves_public(urlsplit(url).hostname or "", resolve or resolve_host):
        return []
    try:
        html = await fetcher.get_text(url)
    except SourceError:
        return []
    out: list[FoundContact] = []
    seen: set[str] = set()
    for raw in _MAILTO_RE.findall(html):
        m = _EMAIL_RE.fullmatch(unquote(raw).strip())
        if m is None:
            continue
        local, domain = m.group(1).lower(), m.group(2).lower().strip(".")
        address = f"{local}@{domain}"
        if local not in INBOX_LOCAL_PARTS or not email_domain_ok(domain, company_dom, company) \
                or address in seen:
            continue
        seen.add(address)
        out.append(FoundContact(
            kind="careers_inbox", name="Careers inbox", professional_title=None,
            channel="email", address=address, public_source_url=url,
            confidence=CAREERS_CONFIDENCE))
    return out


def rank(candidates: list[FoundContact]) -> list[FoundContact]:
    """User-added > named recruiter in posting > generic inbox; one per address."""
    ordered = sorted(candidates, key=lambda c: (_RANK[c.kind], -c.confidence))
    out: list[FoundContact] = []
    seen: set[str] = set()
    for c in ordered:
        key = c.address.casefold()
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out
