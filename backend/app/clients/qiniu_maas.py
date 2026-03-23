from __future__ import annotations

from typing import Any, Optional

import httpx

from app.core.config import Settings
from app.core.exceptions import ServiceConfigurationError, UpstreamServiceError


class QiniuMaaSClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.baseUrl = settings.qiniuMaasBaseUrl.rstrip("/")
        self.httpClient = httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0))

    async def aclose(self) -> None:
        await self.httpClient.aclose()

    async def createChatCompletion(
        self,
        messages: list[dict[str, Any]],
        model: Optional[str] = None,
        maxTokens: int = 1024,
        temperature: float = 0.2,
    ) -> str:
        responseData = await self._request(
            "POST",
            "/chat/completions",
            {
                "model": model or self.settings.qiniuChatModel,
                "messages": messages,
                "max_tokens": maxTokens,
                "temperature": temperature,
            },
        )

        try:
            return responseData["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise UpstreamServiceError("七牛 MaaS 对话返回格式异常。", detail=responseData) from exc

    async def createImageTask(self, payload: dict[str, Any], multiImage: bool = False) -> dict[str, Any]:
        path = "/images/edits" if multiImage else "/images/generations"
        return await self._request("POST", path, payload)

    async def getImageTask(self, taskId: str) -> dict[str, Any]:
        return await self._request("GET", f"/images/tasks/{taskId}")

    async def _request(
        self,
        method: str,
        path: str,
        jsonBody: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        self.ensureConfigured()

        headers = {
            "Authorization": f"Bearer {self.settings.qiniuMaasApiKey}",
        }
        if jsonBody is not None:
            headers["Content-Type"] = "application/json"

        try:
            response = await self.httpClient.request(
                method,
                f"{self.baseUrl}{path}",
                json=jsonBody,
                headers=headers,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            detail: dict[str, Any] = {"statusCode": exc.response.status_code}
            try:
                detail["body"] = exc.response.json()
            except ValueError:
                detail["body"] = exc.response.text
            raise UpstreamServiceError("七牛 MaaS 请求失败。", detail=detail) from exc
        except httpx.HTTPError as exc:
            raise UpstreamServiceError("七牛 MaaS 网络请求失败。", detail={"reason": str(exc)}) from exc

    def ensureConfigured(self) -> None:
        if not self.settings.qiniuMaasApiKey:
            raise ServiceConfigurationError("缺少七牛 MaaS API Key 配置 QINIU_MAAS_API_KEY。")
