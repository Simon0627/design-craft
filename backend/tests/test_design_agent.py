from __future__ import annotations

import anyio

from app.clients.kodo import KodoClient
from app.core.config import Settings
from app.schemas.design import DesignGenerateRequest, DesignPlanRequest
from app.schemas.skill import SkillDescriptor
from app.services.design_agent import DesignAgentService
from app.services.image_assets import ImageAssetMeta


class FakeMaaSClient:
    def __init__(self, chatContent: str):
        self.chatContent = chatContent
        self.imagePayloads: list[tuple[dict, bool]] = []

    async def createChatCompletion(self, **_: object) -> str:
        return self.chatContent

    async def createImageTask(self, payload: dict, multiImage: bool = False) -> dict:
        self.imagePayloads.append((payload, multiImage))
        return {"task_id": "task-123"}

    async def getImageTask(self, taskId: str) -> dict:
        return {
            "task_id": taskId,
            "status": "succeed",
            "status_message": "成功",
            "created": 123456,
            "data": [{"index": 0, "url": "https://example.com/result.png"}],
        }


class FakeKodoClient:
    def isBucketUrl(self, url: str) -> bool:
        return False

    def buildResultKey(self, taskId: str, index: int, sourceUrl: str, prefix: str = "generated") -> str:
        return f"{prefix}/{taskId}/{index}.png"

    async def mirrorRemoteFile(self, remoteUrl: str, key: str):
        return {"key": key, "url": f"https://cdn.example.com/{key}", "sourceUrl": remoteUrl}


class FakeSkillService:
    def listSkills(self) -> list[SkillDescriptor]:
        return [SkillDescriptor(name="image-edit", description="编辑图片", path="/tmp/SKILL.md")]


class FakeImageAssetService:
    async def fetchRemoteImageMeta(self, url: str) -> ImageAssetMeta:
        return ImageAssetMeta(url=url, contentType="image/png", imageFormat="png", width=512, height=512, size=1024)

    def validateForGeneration(self, meta: ImageAssetMeta) -> None:
        return None


def createService(chatContent: str) -> DesignAgentService:
    settings = Settings(qiniuBucketName="design-craft", qiniuBucketDomain="cdn.example.com")
    return DesignAgentService(
        settings,
        FakeMaaSClient(chatContent),
        FakeKodoClient(),
        FakeSkillService(),
        FakeImageAssetService(),
    )


def testPlanDesignCanParseFencedJson() -> None:
    service = createService(
        """```json
        {
          "intentSummary": "生成一张产品主图",
          "generationMode": "image_to_image",
          "prompt": "白底电商主图，产品清晰，柔和布光",
          "aspectRatio": "1:1",
          "shouldUseSearch": false,
          "searchQueries": [],
          "contentSearchQueries": ["产品主图 电商案例"],
          "imageSearchQueries": ["白底主图 灯光"],
          "selectedSkillNames": ["image-edit"],
          "assetUrls": [],
          "referenceLinks": [],
          "notes": []
        }
        ```"""
    )

    plan = anyio.run(
        service.planDesign,
        DesignPlanRequest(userInput="把商品图做成白底主图", assetUrls=["https://example.com/a.png"]),
    )

    assert plan.generationMode == "image_to_image"
    assert plan.aspectRatio == "16:9" or plan.aspectRatio == "1:1"
    assert plan.selectedSkillNames == ["image-edit"]
    assert plan.contentSearchQueries == ["产品主图 电商案例"]
    assert plan.assetUrls == ["https://example.com/a.png"]


def testGenerateDesignBuildsMultiImagePayload() -> None:
    service = createService(
        """{
          "intentSummary": "融合两张素材生成空间效果图",
          "generationMode": "multi_image_edit",
          "prompt": "保留主体结构，生成现代奶油风空间设计图",
          "aspectRatio": "16:9",
          "shouldUseSearch": false,
          "searchQueries": [],
          "contentSearchQueries": [],
          "imageSearchQueries": [],
          "selectedSkillNames": [],
          "assetUrls": [],
          "referenceLinks": [],
          "notes": []
        }"""
    )

    response = anyio.run(
        service.generateDesign,
        DesignGenerateRequest(
            userInput="把两张空间图融合成新的客厅方案",
            assetUrls=["https://example.com/1.png", "https://example.com/2.png"],
            waitForResult=False,
        ),
    )

    payload, multiImage = service.maasClient.imagePayloads[0]
    assert response.taskId == "task-123"
    assert multiImage is True
    assert payload["image"] == ""
    assert len(payload["subject_image_list"]) == 2


def testKodoBuildResultKeyUsesReadableNameAndIndex() -> None:
    client = KodoClient(Settings(qiniuBucketName="design-craft", qiniuBucketDomain="cdn.example.com"))

    key = client.buildResultKey(
        taskId="task-123",
        index=1,
        sourceUrl="https://example.com/result.png",
        prefix="generated",
        assetName="卧室香薰机配图",
        sectionId="hook",
    )

    assert key == "generated/task-123/hook/卧室香薰机配图-2.png"
