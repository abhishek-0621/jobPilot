"""Jobicy public remote-jobs API (one call per tag; postings deduped by id)."""

from jobpilot.discovery.sources.base import RawPosting
from jobpilot.discovery.sources.remote_common import (
    SHAPE_ERRORS,
    RemoteBoardBase,
    as_dict,
    as_list,
    changed,
    posting,
)

BASE = "https://jobicy.com/api/v2/remote-jobs?count=50"
TAGS = ("python", "ai")


class JobicySource(RemoteBoardBase):
    source = "jobicy"

    async def list_board(self, max_postings: int) -> list[RawPosting]:
        out: list[RawPosting] = []
        seen: set[str] = set()
        for tag in TAGS:
            if len(out) >= max_postings:
                break
            data = await self._fetch.get_json(f"{BASE}&tag={tag}")
            try:
                for j in as_list(as_dict(data)["jobs"]):
                    j = as_dict(j)
                    p = posting(self.source, job_id=j["id"], company=j.get("companyName"),
                                title=j["jobTitle"], location=j.get("jobGeo"),
                                description=j.get("jobDescription"), url=j["url"],
                                posted_at=j.get("pubDate"))
                    if p is None or p.source_job_id in seen:
                        continue
                    seen.add(p.source_job_id)
                    out.append(p)
                    if len(out) >= max_postings:
                        break
            except SHAPE_ERRORS as e:
                raise changed(e) from e
        return out
