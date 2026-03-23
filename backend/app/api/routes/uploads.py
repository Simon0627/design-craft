from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.api.deps import getImageAssetService, getKodoClient
from app.clients.kodo import KodoClient
from app.schemas.upload import UploadFileResponse
from app.services.image_assets import ImageAssetService

router = APIRouter()


@router.post("/file", response_model=UploadFileResponse)
async def uploadReferenceFile(
    file: UploadFile = File(...),
    prefix: str = Form(default="references"),
    key: Optional[str] = Form(default=None),
    kodoClient: KodoClient = Depends(getKodoClient),
    imageAssetService: ImageAssetService = Depends(getImageAssetService),
) -> UploadFileResponse:
    normalizedContent, normalizedFileName, normalizedContentType, imageMeta = imageAssetService.normalizeForUpload(
        await file.read(),
        file.filename or "upload.bin",
        file.content_type or "application/octet-stream",
    )
    imageAssetService.validateForGeneration(imageMeta)
    objectKey = key or kodoClient.buildObjectKey(normalizedFileName, prefix)
    uploadResult = UploadFileResponse.model_validate(
        await kodoClient.uploadBytes(normalizedContent, objectKey, normalizedFileName, normalizedContentType)
    )
    uploadResult.width = imageMeta.width
    uploadResult.height = imageMeta.height
    return uploadResult
