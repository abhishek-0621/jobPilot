"""RemoteOK public API (JSON array; the first element is a legal notice and is skipped).
Their terms require a link back: the job page shows "via RemoteOK" with the source link."""

from jobpilot.discovery.sources.base import RawPosting
from jobpilot.discovery.sources.remote_common import (
    SHAPE_ERRORS,
    RemoteBoardBase,
    as_dict,
    as_list,
    changed,
    posting,
)

URL = "https://remoteok.com/api"


class RemoteOKSource(RemoteBoardBase):
    source = "remoteok"

    async def list_board(self, max_postings: int) -> list[RawPosting]:
        data = await self._fetch.get_json(URL)
        out: list[RawPosting] = []
        try:
            for j in as_list(data):
                j = as_dict(j)
                if "legal" in j and "id" not in j:
                    continue
                p = posting(self.source, job_id=j["id"], company=j.get("company"),
                            title=j["position"], location=j.get("location"),
                            description=j.get("description"), url=j["url"],
                            apply_url=j.get("apply_url"), posted_at=j.get("date"))
                if p is not None:
                    out.append(p)
                if len(out) >= max_postings:
                    break
        except SHAPE_ERRORS as e:
            raise changed(e) from e
        return out
