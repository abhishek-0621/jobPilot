"""We Work Remotely public RSS feeds (programming + devops), parsed with defused stdlib XML."""

import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from jobpilot.discovery.sources.base import RawPosting
from jobpilot.discovery.sources.remote_common import RemoteBoardBase, changed, posting

FEEDS = ("https://weworkremotely.com/categories/remote-programming-jobs.rss",
         "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss")
# No DTDs, no entity declarations, no external references: feed XML is untrusted.
_FORBIDDEN = re.compile(r"<!\s*(?:DOCTYPE|ENTITY|ELEMENT|ATTLIST)", re.IGNORECASE)


def split_title(title: str) -> tuple[str, str]:
    """WWR item titles read "Company: Role"; the role may itself contain colons."""
    company, sep, role = title.partition(":")
    if not sep or not role.strip():
        return "", title.strip()
    return company.strip(), role.strip()


def parse_rss(text: str) -> ET.Element:
    """The <channel> of an RSS document. Raises ValueError for DTDs/entities or bad XML."""
    if _FORBIDDEN.search(text):
        raise ValueError("DTD or entity declaration in feed")
    try:
        root = ET.fromstring(text)  # noqa: S314 - DTD/entities rejected above
    except ET.ParseError as e:  # includes undefined entities such as &ext;
        raise ValueError("invalid xml") from e
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise ValueError("not an RSS feed")
    return channel


def _date(value: str | None) -> object:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, IndexError):
        return None


class WeWorkRemotelySource(RemoteBoardBase):
    source = "weworkremotely"

    async def list_board(self, max_postings: int) -> list[RawPosting]:
        out: list[RawPosting] = []
        seen: set[str] = set()
        for feed in FEEDS:
            if len(out) >= max_postings:
                break
            text = await self._fetch.get_text(feed)
            try:
                channel = parse_rss(text)
            except ValueError as e:
                raise changed(e) from e
            for item in channel.iter("item"):
                company, role = split_title(item.findtext("title") or "")
                link = item.findtext("link")
                p = posting(self.source, job_id=item.findtext("guid") or link,
                            company=company, title=role, location=item.findtext("region"),
                            description=item.findtext("description"), url=link,
                            posted_at=_date(item.findtext("pubDate")))
                if p is None or p.source_job_id in seen:
                    continue
                seen.add(p.source_job_id)
                out.append(p)
                if len(out) >= max_postings:
                    break
        return out
