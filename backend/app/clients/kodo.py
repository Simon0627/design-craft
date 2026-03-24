from __future__ import annotations

import asyncio
import base64
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

import httpx
from qiniu import Auth, put_data

from app.core.config import Settings
from app.core.exceptions import ServiceConfigurationError, UpstreamServiceError
from app.schemas.design import StoredObject
from app.schemas.upload import UploadFileResponse


class KodoClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.auth: Optional[Auth] = None

    @property
    def normalizedBucketDomain(self) -> str:
        domain = self.settings.qiniuBucketDomain.strip()
        if not domain:
            raise ServiceConfigurationError("缺少七牛空间域名配置 QINIU_BUCKET_DOMAIN。")
        if domain.startswith("http://") or domain.startswith("https://"):
            return domain.rstrip("/")
        return f"{self.settings.qiniuBucketScheme}://{domain.rstrip('/')}"

    @property
    def bucketHost(self) -> str:
        return urlparse(self.normalizedBucketDomain).netloc

    def createUploadToken(self, key: Optional[str] = None, expiresIn: int = 3600) -> str:
        self.ensureUploadConfigured()
        return self.getAuth().upload_token(self.settings.qiniuBucketName, key=key, expires=expiresIn)

    def buildObjectKey(self, fileName: Optional[str] = None, prefix: str = "uploads") -> str:
        safePrefix = prefix.strip("/ ") or "uploads"
        suffix = Path(fileName or "").suffix if fileName else ""
        datePath = datetime.utcnow().strftime("%Y/%m/%d")
        return f"{safePrefix}/{datePath}/{uuid.uuid4().hex}{suffix}"

    def buildPublicUrl(self, key: str) -> str:
        return f"{self.normalizedBucketDomain}/{quote(key)}"

    def buildAccessibleUrl(self, keyOrUrl: str, expiresIn: int = 3600) -> str:
        return self.buildPrivateDownloadUrl(keyOrUrl, expiresIn)

    def isBucketUrl(self, url: str) -> bool:
        return urlparse(url).netloc == self.bucketHost

    def extractKeyFromUrl(self, url: str) -> str:
        parsed = urlparse(url)
        return parsed.path.lstrip("/")

    def buildPrivateDownloadUrl(self, keyOrUrl: str, expiresIn: int = 3600) -> str:
        self.ensureUploadConfigured()
        if self.isBucketUrl(keyOrUrl):
            baseUrl = keyOrUrl
        else:
            baseUrl = self.buildPublicUrl(keyOrUrl)
        return self.getAuth().private_download_url(baseUrl, expires=expiresIn)

    async def fetchObjectBytes(self, keyOrUrl: str, expiresIn: int = 3600) -> bytes:
        downloadUrl = self.buildPrivateDownloadUrl(keyOrUrl, expiresIn)
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as httpClient:
            response = await httpClient.get(downloadUrl)
            response.raise_for_status()
            return response.content

    async def fetchObjectBase64(self, keyOrUrl: str, expiresIn: int = 3600) -> str:
        content = await self.fetchObjectBytes(keyOrUrl, expiresIn)
        return base64.b64encode(content).decode("utf-8")

    async def mirrorRemoteFile(self, remoteUrl: str, key: str) -> StoredObject:
        self.ensureUploadConfigured()

        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as httpClient:
            response = await httpClient.get(remoteUrl)
            response.raise_for_status()
            contentType = response.headers.get("content-type", "application/octet-stream")
            content = response.content

        uploadToken = self.createUploadToken(key=key, expiresIn=3600)
        _, uploadInfo = await asyncio.to_thread(
            put_data,
            uploadToken,
            key,
            content,
            None,
            contentType,
        )

        if uploadInfo.status_code not in {200, 201}:
            raise UpstreamServiceError(
                "七牛 Kodo 转存生成结果失败。",
                detail={"statusCode": uploadInfo.status_code, "text": uploadInfo.text_body},
            )

        return StoredObject(key=key, url=self.buildAccessibleUrl(key), sourceUrl=remoteUrl)

    async def mirrorRemoteFiles(self, items: list[tuple[str, str]]) -> list[StoredObject]:
        tasks = [self.mirrorRemoteFile(remoteUrl, key) for remoteUrl, key in items]
        return await asyncio.gather(*tasks)

    async def uploadBytes(
        self,
        content: bytes,
        key: str,
        fileName: str,
        contentType: str = "application/octet-stream",
    ) -> UploadFileResponse:
        self.ensureUploadConfigured()

        uploadToken = self.createUploadToken(key=key, expiresIn=3600)
        _, uploadInfo = await asyncio.to_thread(
            put_data,
            uploadToken,
            key,
            content,
            None,
            contentType,
            False,
            None,
            fileName,
        )

        if uploadInfo.status_code not in {200, 201}:
            raise UpstreamServiceError(
                "七牛 Kodo 上传参考图片失败。",
                detail={"statusCode": uploadInfo.status_code, "text": uploadInfo.text_body},
            )

        return UploadFileResponse(
            bucketName=self.settings.qiniuBucketName,
            bucketDomain=self.normalizedBucketDomain,
            key=key,
            fileName=fileName,
            contentType=contentType,
            size=len(content),
            url=self.buildAccessibleUrl(key),
        )

    def buildResultKey(
        self,
        taskId: str,
        index: int,
        sourceUrl: str,
        prefix: str = "generated",
        assetName: str = "",
        sectionId: str = "",
    ) -> str:
        parsedUrl = urlparse(sourceUrl)
        suffix = Path(parsedUrl.path).suffix or ".png"
        safePrefix = prefix.strip("/ ") or "generated"
        safeSection = self._normalizeKeySegment(sectionId)
        safeName = self._normalizeKeySegment(assetName)
        fileName = safeName or str(index)
        if safeSection:
            return f"{safePrefix}/{taskId}/{safeSection}/{fileName}{suffix}"
        return f"{safePrefix}/{taskId}/{fileName}{suffix}"

    def _normalizeKeySegment(self, value: str) -> str:
        normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", value.strip()).strip("-")
        return normalized[:64]

    def ensureUploadConfigured(self) -> None:
        if not self.settings.qiniuAccessKey or not self.settings.qiniuSecretKey:
            raise ServiceConfigurationError("缺少七牛 Kodo 凭证配置：QINIU_ACCESS_KEY / QINIU_SECRET_KEY。")
        if not self.settings.qiniuBucketName:
            raise ServiceConfigurationError("缺少七牛存储空间配置 QINIU_BUCKET_NAME。")

    def getAuth(self) -> Auth:
        if self.auth is None:
            self.auth = Auth(self.settings.qiniuAccessKey, self.settings.qiniuSecretKey)
        return self.auth
