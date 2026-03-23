from __future__ import annotations

from app.core.config import Settings
from app.schemas.agui import ParsedAgentRequest
from app.services.agui_agent import AgUiAgentService


class FakeDesignService:
    maasClient = None


class FakeSearchService:
    pass


def createService() -> AgUiAgentService:
    settings = Settings()
    service = AgUiAgentService(settings, FakeDesignService(), FakeSearchService())
    return service


def testFallbackDecisionChoosesCreateImageForImageRequest() -> None:
    service = createService()
    parsedRequest = ParsedAgentRequest(
        userInput="帮我生成一张香薰机电商主图",
        assetUrls=["https://example.com/a.png"],
        aspectRatio="1:1",
    )

    decision = service._fallbackDecision(parsedRequest, {"latestImageResult": None, "latestSearch": None})

    assert decision["actionType"] == "tool"
    assert decision["toolName"] == "create_image"
    assert decision["toolArgs"]["generationMode"] == "image_to_image"


def testFallbackDecisionChoosesStoreResultAfterImageCreated() -> None:
    service = createService()
    parsedRequest = ParsedAgentRequest(
        userInput="生成一张电商主图",
        outputKeyPrefix="generated",
    )

    decision = service._fallbackDecision(
        parsedRequest,
        {
            "latestImageResult": {
                "taskId": "task-1",
                "status": "succeed",
                "resultUrls": ["https://example.com/result.png"],
                "storedResults": [],
            }
        },
    )

    assert decision["actionType"] == "tool"
    assert decision["toolName"] == "store_result"
    assert decision["toolArgs"]["taskId"] == "task-1"


def testFallbackDecisionChoosesAskFollowUpForVagueImageRequest() -> None:
    service = createService()
    parsedRequest = ParsedAgentRequest(
        userInput="帮我生成一个效果图",
        combinedUserContext="帮我生成一个效果图",
    )

    decision = service._fallbackDecision(parsedRequest, {"latestImageResult": None, "latestSearch": None})

    assert decision["actionType"] == "tool"
    assert decision["toolName"] == "ask_followup"
    assert len(decision["toolArgs"]["options"]) >= 2


def testSanitizeFinalResponseRemovesLinks() -> None:
    service = createService()

    result = service._sanitizeFinalResponse(
        "这是效果图，点击查看：[法式客厅效果图](https://example.com/a.png)",
        {},
    )

    assert "http" not in result
    assert "点击查看" not in result
    assert result == "图片已经生成好了，可以继续告诉我你想怎么调整。"
