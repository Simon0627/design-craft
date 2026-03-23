from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, UnidentifiedImageError

from app.core.exceptions import AppError


@dataclass
class ImageAssetMeta:
    url: str
    contentType: str
    imageFormat: str
    width: int
    height: int
    size: int

    @property
    def aspectRatio(self) -> float:
        return self.width / self.height


class ImageAssetService:
    def __init__(self):
        self.httpClient = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    async def aclose(self) -> None:
        await self.httpClient.aclose()

    def inspectImageBytes(
        self,
        content: bytes,
        fileName: str,
        contentType: str = "application/octet-stream",
    ) -> ImageAssetMeta:
        try:
            image = Image.open(io.BytesIO(content))
            width, height = image.size
            imageFormat = (image.format or "").lower()
        except (UnidentifiedImageError, OSError) as exc:
            raise AppError(
                "上传的文件不是可识别的图片格式。",
                statusCode=422,
                code="invalid_image_file",
                detail={"fileName": fileName, "contentType": contentType},
            ) from exc

        return ImageAssetMeta(
            url="",
            contentType=contentType,
            imageFormat=imageFormat,
            width=width,
            height=height,
            size=len(content),
        )

    def normalizeForUpload(
        self,
        content: bytes,
        fileName: str,
        contentType: str = "application/octet-stream",
    ) -> tuple[bytes, str, str, ImageAssetMeta]:
        meta = self.inspectImageBytes(content, fileName, contentType)
        if meta.imageFormat in {"png", "jpeg", "jpg"}:
            return content, fileName, self._normalizeContentType(meta.imageFormat, contentType), meta

        try:
            image = Image.open(io.BytesIO(content))
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "A" in image.mode else "RGB")
            normalizedBuffer = io.BytesIO()
            image.save(normalizedBuffer, format="PNG")
        except (UnidentifiedImageError, OSError) as exc:
            raise AppError(
                "参考图片格式无法转换，请使用 PNG 或 JPG 图片。",
                statusCode=422,
                code="unsupported_image_format",
                detail={"fileName": fileName, "contentType": contentType},
            ) from exc

        normalizedContent = normalizedBuffer.getvalue()
        normalizedFileName = f"{Path(fileName).stem or 'upload'}.png"
        normalizedContentType = "image/png"
        normalizedMeta = self.inspectImageBytes(normalizedContent, normalizedFileName, normalizedContentType)
        return normalizedContent, normalizedFileName, normalizedContentType, normalizedMeta

    async def fetchRemoteImageMeta(self, url: str) -> ImageAssetMeta:
        try:
            response = await self.httpClient.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AppError(
                "无法访问参考图片 URL，请确认链接可公开访问。",
                statusCode=422,
                code="invalid_asset_url",
                detail={"url": url, "reason": str(exc)},
            ) from exc

        meta = self.inspectImageBytes(
            content=response.content,
            fileName=url,
            contentType=response.headers.get("content-type", "application/octet-stream"),
        )
        meta.url = url
        return meta

    def validateForGeneration(self, meta: ImageAssetMeta) -> None:
        if meta.imageFormat not in {"png", "jpeg", "jpg"}:
            raise AppError(
                "参考图片格式不受支持，七牛图生图当前仅支持 PNG 或 JPG。",
                statusCode=422,
                code="unsupported_image_format",
                detail={"url": meta.url, "contentType": meta.contentType, "imageFormat": meta.imageFormat},
            )

        if meta.width < 300 or meta.height < 300:
            raise AppError(
                "参考图片尺寸过小，七牛图生图要求宽高都不小于 300px。",
                statusCode=422,
                code="asset_too_small",
                detail={
                    "url": meta.url,
                    "width": meta.width,
                    "height": meta.height,
                    "minWidth": 300,
                    "minHeight": 300,
                },
            )

        ratio = meta.aspectRatio
        if ratio < 1 / 2.5 or ratio > 2.5:
            raise AppError(
                "参考图片宽高比不符合七牛图生图要求，需要在 1:2.5 到 2.5:1 之间。",
                statusCode=422,
                code="asset_invalid_aspect_ratio",
                detail={"url": meta.url, "width": meta.width, "height": meta.height},
            )

    def _normalizeContentType(self, imageFormat: str, fallback: str) -> str:
        if imageFormat in {"jpg", "jpeg"}:
            return "image/jpeg"
        if imageFormat == "png":
            return "image/png"
        return fallback
