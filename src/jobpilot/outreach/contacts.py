"""Public recruiting contacts: published in the posting, a careers-page inbox, or added by
the user. Nothing is ever inferred or constructed (no ``first.last@`` guessing); every
email kept is published text whose domain is the company's own domain.

Posting text and careers pages are untrusted: only regex-extracted, domain-checked
addresses and a deterministic name/title pattern leave this module.
"""

import ipaddress
import re
from typing import Literal
from urllib.parse import unquote, urlsplit

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


def _slug_matches(domain: str, company: str) -> bool:
    labels = base_domain(domain).split(".")
    slug = company_slug(company)
    return bool(slug) and len(labels) >= 2 and re.sub(r"[^a-z0-9]", "", labels[0]) == slug


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


async def fetch_careers_inbox(url: str | None, fetcher: RateLimitedFetcher,
                              company_dom: str | None, company: str | None = None
                              ) -> list[FoundContact]:
    """One GET of the careers page; ``mailto:`` links with an allowlisted local part on the
    company domain. Any fetch error yields no contacts."""
    url = public_http_url(url)
    if url is None or (company_dom is None and company is None):
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
