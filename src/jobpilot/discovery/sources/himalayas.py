"""Himalayas public remote-jobs API (paged JSON, at most MAX_PAGES pages per run)."""

from jobpilot.discovery.sources.base import RawPosting
from jobpilot.discovery.sources.remote_common import (
    SHAPE_ERRORS,
    RemoteBoardBase,
    as_dict,
    as_list,
    changed,
    posting,
)

BASE = "https://himalayas.app/jobs/api"
PAGE = 20
MAX_PAGES = 5


class HimalayasSource(RemoteBoardBase):
    source = "himalayas"

    async def list_board(self, max_postings: int) -> list[RawPosting]:
        out: list[RawPosting] = []
        for page in range(MAX_PAGES):
            offset = page * PAGE
            data = await self._fetch.get_json(f"{BASE}?limit={PAGE}&offset={offset}")
            try:
                body = as_dict(data)
                jobs = as_list(body["jobs"])
                total = int(body.get("totalCount") or 0)
                for j in jobs:
                    j = as_dict(j)
                    restrictions = [str(x) for x in as_list(j.get("locationRestrictions") or [])]
                    p = posting(
                        self.source, job_id=j["guid"], company=j.get("companyName"),
                        title=j["title"], location=", ".join(restrictions),
                        description=j.get("description"), url=j["guid"],
                        apply_url=j.get("applicationLink"), posted_at=j.get("pubDate"))
                    if p is not None:
                        out.append(p)
            except SHAPE_ERRORS as e:
                raise changed(e) from e
            if len(out) >= max_postings or not jobs or offset + PAGE >= total:
                break
        return out[:max_postings]
