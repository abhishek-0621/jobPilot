from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from jobpilot.contracts.enums import ErrorCode
from jobpilot.discovery.sources.base import SourceError
from jobpilot.discovery.sources.weworkremotely import WeWorkRemotelySource, split_title
from jobpilot.runtime import build_sources

FIX = Path(__file__).parent.parent / "fixtures" / "remote_boards" / "weworkremotely.rss"


def _client(body: str, seen: list[httpx.Request], status: int = 200) -> httpx.AsyncClient:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(status, text=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_wwr_parses_two_feeds() -> None:
    seen: list[httpx.Request] = []
    async with _client(FIX.read_text(), seen) as c:
        posts = await WeWorkRemotelySource(c, min_interval_s=0).list_board(max_postings=100)
    assert [str(r.url) for r in seen] == [
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss"]
    assert len(posts) == 2  # same items in both feeds deduped; javascript: link dropped
    p, q = posts
    assert p.source == "weworkremotely" and p.company == "Example Corp"
    assert p.title == "Senior AI Engineer" and p.location == "Anywhere in the World"
    assert p.description_html == "<p>Build <strong>RAG</strong> agents.</p>"
    assert p.canonical_url == "https://weworkremotely.com/remote-jobs/example-corp-senior-ai-engineer"
    assert p.posted_at == datetime(2026, 10, 6, 7, 31, 5, tzinfo=UTC)
    assert q.company == "Sample Labs" and q.title == "ML Engineer: Platform"
    assert q.posted_at is None


async def test_wwr_cap() -> None:
    async with _client(FIX.read_text(), []) as c:
        posts = await WeWorkRemotelySource(c, min_interval_s=0).list_board(max_postings=1)
    assert len(posts) == 1


def test_split_title() -> None:
    assert split_title("Example Corp: AI Engineer") == ("Example Corp", "AI Engineer")
    assert split_title("Just A Role") == ("", "Just A Role")


@pytest.mark.parametrize("body", [
    '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel/></rss>',
    '<!doctype rss SYSTEM "http://example.com/x.dtd"><rss><channel/></rss>',
    '<rss><channel><item><title>&ext;</title></item></channel></rss>',
    "<rss><channel>",
    "<html>not rss</html>",
])
async def test_wwr_rejects_dtd_entities_and_garbage(body: str) -> None:
    async with _client(body, []) as c:
        with pytest.raises(SourceError) as e:
            await WeWorkRemotelySource(c, min_interval_s=0).list_board(max_postings=5)
    assert e.value.code == ErrorCode.SOURCE_CHANGED


async def test_wwr_http_error() -> None:
    async with _client("", [], status=429) as c:
        with pytest.raises(SourceError) as e:
            await WeWorkRemotelySource(c, min_interval_s=0).list_board(max_postings=5)
    assert e.value.code == ErrorCode.SOURCE_RATE_LIMIT


def test_wwr_registered() -> None:
    assert build_sources(httpx.AsyncClient())["weworkremotely"].kind == "remote_board"  # type: ignore[attr-defined]
