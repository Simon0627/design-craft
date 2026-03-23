from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.api.deps import getImageAssetService, getKodoClient
from app.clients.kodo import KodoClient
from app.schemas.upload import UploadFileResponse, UploadTokenRequest, UploadTokenResponse
from app.services.image_assets import ImageAssetService

router = APIRouter()


@router.post("/token", response_model=UploadTokenResponse)
async def createUploadToken(
    request: UploadTokenRequest,
    kodoClient: KodoClient = Depends(getKodoClient),
) -> UploadTokenResponse:
    objectKey = request.key or kodoClient.buildObjectKey(request.fileName, request.prefix)
    token = kodoClient.createUploadToken(objectKey, request.expiresIn)
    return UploadTokenResponse(
        bucketName=kodoClient.settings.qiniuBucketName,
        bucketDomain=kodoClient.normalizedBucketDomain,
        uploadHost=kodoClient.settings.qiniuUploadHost,
        uploadUrl=f"https://{kodoClient.settings.qiniuUploadHost}",
        key=objectKey,
        fileName=request.fileName,
        token=token,
    )


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
