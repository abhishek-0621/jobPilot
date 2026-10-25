"""Working Nomads public jobs API (JSON array; items have no id, the URL slug is used)."""

from urllib.parse import urlsplit

from jobpilot.discovery.sources.base import RawPosting
from jobpilot.discovery.sources.remote_common import (
    SHAPE_ERRORS,
    RemoteBoardBase,
    as_dict,
    as_list,
    changed,
    posting,
)

URL = "https://www.workingnomads.com/api/exposed_jobs/"


class WorkingNomadsSource(RemoteBoardBase):
    source = "workingnomads"

    async def list_board(self, max_postings: int) -> list[RawPosting]:
        data = await self._fetch.get_json(URL)
        out: list[RawPosting] = []
        try:
            for j in as_list(data):
                j = as_dict(j)
                url = str(j["url"])
                slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
                p = posting(self.source, job_id=slug, company=j.get("company_name"),
                            title=j["title"], location=j.get("location"),
                            description=j.get("description"), url=url,
                            posted_at=j.get("pub_date"))
                if p is not None:
                    out.append(p)
                if len(out) >= max_postings:
                    break
        except SHAPE_ERRORS as e:
            raise changed(e) from e
        return out
