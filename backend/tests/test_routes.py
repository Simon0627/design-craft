from __future__ import annotations

from typing import Optional

from app.schemas.design import DesignPlan
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


class FakeDesignService:
    async def planDesign(self, request):
        return DesignPlan(
            intentSummary=request.userInput,
            generationMode="text_to_image",
            prompt=request.userInput,
            aspectRatio="16:9",
            shouldUseSearch=False,
            searchQueries=[],
            selectedSkillNames=[],
            assetUrls=request.assetUrls,
            referenceLinks=request.referenceLinks,
            notes=[],
        )

    async def generateDesign(self, request):
        plan = await self.planDesign(request)
        return {
            "plan": plan,
            "taskId": "task-1",
            "status": "submitted",
            "statusMessage": "任务已提交",
            "created": None,
            "resultUrls": [],
            "storedResults": [],
            "rawTask": {"task_id": "task-1"},
        }

    async def getTaskStatus(self, taskId: str, autoStoreResult: bool = True, outputKeyPrefix: str = "generated"):
        return {
            "taskId": taskId,
            "status": "processing",
            "statusMessage": "处理中",
            "created": 123,
            "resultUrls": [],
            "storedResults": [],
            "rawTask": {"task_id": taskId},
        }


def testListSkillsRoute() -> None:
    with createTestClient() as client:
        client.app.state.skillService = FakeSkillService()
        client.app.state.imageAssetService = FakeImageAssetService()
        client.app.state.kodoClient = FakeKodoClient()
        client.app.state.designService = FakeDesignService()
        response = client.get("/api/v1/designs/skills")
    assert response.status_code == 200
    assert response.json()[0]["name"] == "image-edit"


def testCreateUploadTokenRoute() -> None:
    with createTestClient() as client:
        client.app.state.skillService = FakeSkillService()
        client.app.state.imageAssetService = FakeImageAssetService()
        client.app.state.kodoClient = FakeKodoClient()
        client.app.state.designService = FakeDesignService()
        response = client.post("/api/v1/uploads/token", json={"fileName": "demo.png"})
    assert response.status_code == 200
    assert response.json()["key"] == "uploads/demo.png"


def testPlanRoute() -> None:
    with createTestClient() as client:
        client.app.state.skillService = FakeSkillService()
        client.app.state.imageAssetService = FakeImageAssetService()
        client.app.state.kodoClient = FakeKodoClient()
        client.app.state.designService = FakeDesignService()
        response = client.post("/api/v1/designs/plan", json={"userInput": "生成一张海报"})
    assert response.status_code == 200
    assert response.json()["plan"]["generationMode"] == "text_to_image"


def testUploadFileRoute() -> None:
    with createTestClient() as client:
        client.app.state.skillService = FakeSkillService()
        client.app.state.imageAssetService = FakeImageAssetService()
        client.app.state.kodoClient = FakeKodoClient()
        client.app.state.designService = FakeDesignService()
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
        client.app.state.skillService = FakeSkillService()
        client.app.state.imageAssetService = FakeImageAssetService()
        client.app.state.kodoClient = FakeKodoClient()
        client.app.state.designService = FakeDesignService()
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
        client.app.state.skillService = FakeSkillService()
        client.app.state.kodoClient = FakeKodoClient()
        client.app.state.designService = FakeDesignService()
        client.app.state.imageAssetService = __import__("app.services.image_assets", fromlist=["ImageAssetService"]).ImageAssetService()
        response = client.post(
            "/api/v1/uploads/file",
            data={"prefix": "references"},
            files={"file": ("tiny.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x18\x00\x00\x00\x18\x08\x06\x00\x00\x00\xe0w=\xf8\x00\x00\x00\x19IDATx\x9cc`\xa0\x100\xfe\xcf@\x1a`\"U\xfd\xa8\x86Q\rCH\x03\x00@\x90\x02.\xf6\xee\xe4\xcf\x00\x00\x00\x00IEND\xaeB`\x82", "image/png")},
        )
    assert response.status_code == 422
    assert response.json()["code"] == "asset_too_small"
