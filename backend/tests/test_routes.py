from __future__ import annotations

import importlib
from typing import Optional

from app.schemas.skill import SkillDescriptor
from tests.helpers import createTestClient


class FakeSkillService:
    def listSkills(self) -> list[SkillDescriptor]:
        return [SkillDescriptor(name="image-edit", description="编辑图片", path="/tmp/SKILL.md")]


class FakeKodoClient:
    settings = type(
        "Settings",
        (),
        {
            "qiniuBucketName": "design-craft",
            "qiniuUploadHost": "up.qiniup.com",
        },
    )()
    normalizedBucketDomain = "https://cdn.example.com"

    def buildObjectKey(self, fileName: Optional[str] = None, prefix: str = "uploads") -> str:
        return f"{prefix}/demo.png"

    def createUploadToken(self, key: Optional[str] = None, expiresIn: int = 3600) -> str:
        return f"token-for-{key}-{expiresIn}"

    async def uploadBytes(
        self,
        content: bytes,
        key: str,
        fileName: str,
        contentType: str = "application/octet-stream",
    ):
        return {
            "bucketName": "design-craft",
            "bucketDomain": "https://cdn.example.com",
            "key": key,
            "fileName": fileName,
            "contentType": contentType,
            "size": len(content),
            "width": 512,
            "height": 512,
            "url": f"https://cdn.example.com/{key}",
        }


class FakeImageAssetService:
    def normalizeForUpload(self, content: bytes, fileName: str, contentType: str = "application/octet-stream"):
        normalizedFileName = "test.png"
        if fileName.endswith(".webp"):
            normalizedFileName = "test.png"
        return content, normalizedFileName, "image/png", type(
            "ImageMeta",
            (),
            {"width": 512, "height": 512, "imageFormat": "png", "contentType": "image/png"},
        )()

    def validateForGeneration(self, meta) -> None:
        return None


def testUploadFileRoute() -> None:
    with createTestClient() as client:
        client.app.state.imageAssetService = FakeImageAssetService()
        client.app.state.kodoClient = FakeKodoClient()
        response = client.post(
            "/api/v1/uploads/file",
            data={"prefix": "references"},
            files={"file": ("test.png", b"fake-image-bytes", "image/png")},
        )
    assert response.status_code == 200
    assert response.json()["fileName"] == "test.png"
    assert response.json()["key"] == "references/demo.png"


def testUploadFileRouteConvertsWebp() -> None:
    with createTestClient() as client:
        client.app.state.imageAssetService = FakeImageAssetService()
        client.app.state.kodoClient = FakeKodoClient()
        response = client.post(
            "/api/v1/uploads/file",
            data={"prefix": "references"},
            files={"file": ("aroma.webp", b"fake-webp-bytes", "image/webp")},
        )
    assert response.status_code == 200
    assert response.json()["fileName"] == "test.png"
    assert response.json()["contentType"] == "image/png"


def testUploadFileRouteRejectsSmallImage() -> None:
    with createTestClient() as client:
        client.app.state.kodoClient = FakeKodoClient()
        client.app.state.imageAssetService = importlib.import_module("app.services.image_assets").ImageAssetService()
        response = client.post(
            "/api/v1/uploads/file",
            data={"prefix": "references"},
            files={"file": ("tiny.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x18\x00\x00\x00\x18\x08\x06\x00\x00\x00\xe0w=\xf8\x00\x00\x00\x19IDATx\x9cc`\xa0\x100\xfe\xcf@\x1a`\"U\xfd\xa8\x86Q\rCH\x03\x00@\x90\x02.\xf6\xee\xe4\xcf\x00\x00\x00\x00IEND\xaeB`\x82", "image/png")},
        )
    assert response.status_code == 422
    assert response.json()["code"] == "asset_too_small"
