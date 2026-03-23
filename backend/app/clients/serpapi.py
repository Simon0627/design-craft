from __future__ import annotations

from typing import Union

import httpx

from app.core.config import Settings
from app.core.exceptions import ServiceConfigurationError, UpstreamServiceError
from app.schemas.search import TextSearchResult


class SerpApiClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.httpClient = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    async def aclose(self) -> None:
        await self.httpClient.aclose()

    async def searchText(self, query: str, count: int = 5) -> list[TextSearchResult]:
        self.ensureConfigured()
        params: dict[str, Union[str, int]] = {
            "engine": "google",
            "q": query,
            "api_key": self.settings.serpApiKey,
            "google_domain": self.settings.serpApiGoogleDomain,
            "gl": self.settings.serpApiGl,
            "hl": self.settings.serpApiHl,
            "num": count,
        }

        try:
            response = await self.httpClient.get(self.settings.serpApiBaseUrl, params=params)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise UpstreamServiceError(
                "SerpAPI 文本搜索请求失败。",
                detail={"statusCode": exc.response.status_code, "body": exc.response.text},
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamServiceError("SerpAPI 文本搜索网络异常。", detail={"reason": str(exc)}) from exc

        if payload.get("error"):
            raise UpstreamServiceError("SerpAPI 返回错误。", detail={"error": payload["error"]})

        results: list[TextSearchResult] = []
        for item in payload.get("organic_results", [])[:count]:
            results.append(
                TextSearchResult(
                    title=item.get("title", ""),
                    link=item.get("link", ""),
                    snippet=item.get("snippet", ""),
                    source="serpapi",
                    position=item.get("position"),
                )
            )
        return results

    def ensureConfigured(self) -> None:
        if not self.settings.serpApiKey:
            raise ServiceConfigurationError("缺少 SerpAPI Key 配置 SERPAPI_API_KEY。")
