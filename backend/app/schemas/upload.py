from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class UploadTokenRequest(BaseModel):
    key: Optional[str] = Field(default=None, description="可选，指定上传对象名")
    fileName: Optional[str] = Field(default=None, description="原始文件名")
    prefix: str = Field(default="uploads", description="自动生成 key 时使用的前缀")
    expiresIn: int = Field(default=3600, ge=60, le=86400, description="上传凭证有效期，单位秒")


class UploadTokenResponse(BaseModel):
    bucketName: str
    bucketDomain: str
    uploadHost: str
    uploadUrl: str
    key: str
    fileName: Optional[str] = None
    token: str


class UploadFileResponse(BaseModel):
    bucketName: str
    bucketDomain: str
    key: str
    fileName: str
    contentType: str
    size: int
    width: Optional[int] = None
    height: Optional[int] = None
    url: str
