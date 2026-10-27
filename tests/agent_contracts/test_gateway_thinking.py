"""Thinking models must not burn the timeout on hidden reasoning tokens."""

import json

import httpx

from jobpilot.config import AppConfig, ModelRoute, load_config
from jobpilot.llm.ollama_client import ModelGateway, strip_think_blocks
from jobpilot.llm.prompts import build_messages
from tests.agent_contracts.test_gateway import GOOD, Extract, chat


def _gw(route: ModelRoute, seen: list[dict], response: httpx.Response) -> ModelGateway:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return response

    cfg = AppConfig(model_routes={"r": route})
    return ModelGateway(cfg, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_request_disables_thinking_and_caps_tokens():
    seen: list[dict] = []
    gw = _gw(ModelRoute(model_tag="m", max_tokens=256), seen, chat(GOOD))
    await gw.generate("r", build_messages("s", "u"), Extract, "v1")
    assert seen[0]["think"] is False
    assert seen[0]["options"]["num_predict"] == 256


async def test_no_num_predict_when_uncapped_and_think_opt_in():
    seen: list[dict] = []
    gw = _gw(ModelRoute(model_tag="m", think=True), seen, chat(GOOD))
    await gw.generate("r", build_messages("s", "u"), Extract, "v1")
    assert seen[0]["think"] is True
    assert "num_predict" not in seen[0]["options"]


async def test_thinking_field_ignored_and_leaked_blocks_stripped():
    seen: list[dict] = []
    resp = httpx.Response(200, json={
        "message": {"role": "assistant", "thinking": "long hidden reasoning",
                    "content": "<think>plan {not json}</think>\n" + GOOD},
        "done": True, "eval_count": 9})
    gw = _gw(ModelRoute(model_tag="m"), seen, resp)
    out, meta = await gw.generate("r", build_messages("s", "u"), Extract, "v1")
    assert out.title == "Engineer"
    assert meta.attempts == 1


def test_strip_think_blocks():
    assert strip_think_blocks("<think>a\nb</think>{}") == "{}"
    assert strip_think_blocks("<THINK>x</THINK> {} <think>y</think>") == "{}"
    assert strip_think_blocks("{}") == "{}"


def test_defaults():
    r = ModelRoute(model_tag="m")
    assert r.think is False and r.timeout_s == 300 and r.max_tokens is None


def test_shipped_routes_have_caps():
    routes = load_config().model_routes
    assert routes["job_extraction"].max_tokens == 1024
    assert routes["resume_tailoring"].max_tokens == 2048
    assert routes["outreach_draft"].max_tokens == 512
