from __future__ import annotations

from app.clients.serpapi import SerpApiClient
from app.core.exceptions import AppError, ServiceConfigurationError
from app.schemas.search import SearchToolOutcome


class SearchService:
    def __init__(self, serpApiClient: SerpApiClient):
        self.serpApiClient = serpApiClient

    async def searchContent(self, query: str, count: int = 5) -> SearchToolOutcome:
        if not query:
            return SearchToolOutcome(status="skipped", query=query, reason="未提供文本搜索查询词")
        try:
            results = await self.serpApiClient.searchText(query, count)
        except ServiceConfigurationError as exc:
            return SearchToolOutcome(status="skipped", query=query, reason=exc.message)
        except AppError:
            raise
        return SearchToolOutcome(status="success", query=query, results=[result.model_dump() for result in results])
