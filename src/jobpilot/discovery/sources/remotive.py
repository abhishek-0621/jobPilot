"""Remotive public API: one call per run (their terms ask for only a few calls a day)."""

from jobpilot.discovery.sources.base import RawPosting
from jobpilot.discovery.sources.remote_common import (
    SHAPE_ERRORS,
    RemoteBoardBase,
    as_dict,
    as_list,
    changed,
    posting,
)

URL = "https://remotive.com/api/remote-jobs?category=software-dev"


class RemotiveSource(RemoteBoardBase):
    source = "remotive"

    async def list_board(self, max_postings: int) -> list[RawPosting]:
        data = await self._fetch.get_json(URL)
        out: list[RawPosting] = []
        try:
            for j in as_list(as_dict(data)["jobs"]):
                j = as_dict(j)
                p = posting(self.source, job_id=j["id"], company=j.get("company_name"),
                            title=j["title"], location=j.get("candidate_required_location"),
                            description=j.get("description"), url=j["url"],
                            posted_at=j.get("publication_date"))
                if p is not None:
                    out.append(p)
                if len(out) >= max_postings:
                    break
        except SHAPE_ERRORS as e:
            raise changed(e) from e
        return out
