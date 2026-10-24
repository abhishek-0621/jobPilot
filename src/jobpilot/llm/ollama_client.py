import time
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from jobpilot.config import AppConfig, ModelRoute
from jobpilot.contracts.enums import ErrorCode

T = TypeVar("T", bound=BaseModel)

_MAX_ATTEMPTS = 2


class ModelError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class GenerationMeta(BaseModel):
    model_tag: str
    prompt_version: str
    latency_ms: int
    attempts: int
    eval_count: int | None


def _error_summary(err: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in err.errors()[:3]
    )


class ModelGateway:
    def __init__(self, cfg: AppConfig, client: httpx.AsyncClient | None = None) -> None:
        self._cfg = cfg
        self._client = client or httpx.AsyncClient(base_url=cfg.ollama_url)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _url(self, path: str) -> str:
        # Works for caller-supplied clients without a base_url.
        return f"{self._cfg.ollama_url.rstrip('/')}{path}"

    async def available_models(self) -> set[str]:
        try:
            resp = await self._client.get(self._url("/api/tags"), timeout=10)
        except httpx.TimeoutException as e:
            raise ModelError(ErrorCode.MODEL_TIMEOUT, "ollama /api/tags timed out") from e
        except httpx.HTTPError as e:
            raise ModelError(ErrorCode.MODEL_UNAVAILABLE, f"ollama unreachable: {e}") from e
        if resp.status_code != 200:
            raise ModelError(ErrorCode.MODEL_UNAVAILABLE, f"ollama returned {resp.status_code}")
        return {m["name"] for m in resp.json().get("models", [])}

    async def _chat(
        self,
        route: ModelRoute,
        model_tag: str,
        messages: list[dict],
        schema: type[BaseModel],
    ) -> tuple[str, int | None]:
        body: dict[str, Any] = {
            "model": model_tag,
            "messages": messages,
            "stream": False,
            "format": schema.model_json_schema(),
            "options": {"temperature": route.temperature, "num_ctx": route.num_ctx},
            "keep_alive": route.keep_alive,
        }
        try:
            resp = await self._client.post(self._url("/api/chat"), json=body, timeout=route.timeout_s)
        except httpx.TimeoutException as e:
            raise ModelError(ErrorCode.MODEL_TIMEOUT, f"model {model_tag} timed out") from e
        except httpx.HTTPError as e:
            raise ModelError(ErrorCode.MODEL_UNAVAILABLE, f"model {model_tag} unreachable: {e}") from e
        if resp.status_code >= 500 or resp.status_code == 404:
            raise ModelError(
                ErrorCode.MODEL_UNAVAILABLE, f"model {model_tag} returned HTTP {resp.status_code}"
            )
        if resp.status_code != 200:
            raise ModelError(
                ErrorCode.MODEL_INVALID_SCHEMA, f"model {model_tag} returned HTTP {resp.status_code}"
            )
        try:
            data = resp.json()
            content = data["message"]["content"]
        except (ValueError, KeyError, TypeError) as e:
            raise ModelError(
                ErrorCode.MODEL_INVALID_SCHEMA, f"malformed response from {model_tag}"
            ) from e
        eval_count = data.get("eval_count")
        return content, eval_count if isinstance(eval_count, int) else None

    async def _generate_with(
        self,
        route: ModelRoute,
        model_tag: str,
        messages: list[dict],
        schema: type[T],
    ) -> tuple[T, int, int | None]:
        convo = list(messages)
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            content, eval_count = await self._chat(route, model_tag, convo, schema)
            try:
                return schema.model_validate_json(content), attempt, eval_count
            except ValidationError as err:
                if attempt == _MAX_ATTEMPTS:
                    raise ModelError(
                        ErrorCode.MODEL_INVALID_SCHEMA,
                        f"model {model_tag} output failed validation after repair: "
                        f"{_error_summary(err)}",
                    ) from err
                convo = [
                    *convo,
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": "Your previous output failed validation: "
                        f"{_error_summary(err)}. Return only JSON matching the schema.",
                    },
                ]
        raise AssertionError("unreachable")  # pragma: no cover

    async def generate(
        self, route: str, messages: list[dict], schema: type[T], prompt_version: str
    ) -> tuple[T, GenerationMeta]:
        if route not in self._cfg.model_routes:
            raise ValueError(f"unknown model route: {route}")
        cfg = self._cfg.model_routes[route]
        start = time.monotonic()
        model_tag = cfg.model_tag
        try:
            out, attempts, eval_count = await self._generate_with(cfg, model_tag, messages, schema)
        except ModelError as e:
            if cfg.fallback is None or e.code not in (
                ErrorCode.MODEL_UNAVAILABLE,
                ErrorCode.MODEL_TIMEOUT,
            ):
                raise
            model_tag = cfg.fallback
            out, attempts, eval_count = await self._generate_with(cfg, model_tag, messages, schema)
        meta = GenerationMeta(
            model_tag=model_tag,
            prompt_version=prompt_version,
            latency_ms=int((time.monotonic() - start) * 1000),
            attempts=attempts,
            eval_count=eval_count,
        )
        return out, meta
