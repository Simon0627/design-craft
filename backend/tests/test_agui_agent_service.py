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
        aspectRatio="1:1",
    )

    decision = service._fallbackDecision(parsedRequest, {"latestImageResult": None, "latestSearch": None})

    assert decision["actionType"] == "tool"
    assert decision["toolName"] == "create_image"
    assert decision["toolArgs"]["generationMode"] == "text_to_image"


def testFallbackDecisionChoosesReadReferenceImagesBeforeImageGeneration() -> None:
    service = createService()
    parsedRequest = ParsedAgentRequest(
        userInput="基于这张参考图生成一张香薰机电商主图",
        combinedUserContext="基于这张参考图生成一张香薰机电商主图",
        assetUrls=["https://example.com/a.png"],
        aspectRatio="1:1",
    )

    decision = service._fallbackDecision(
        parsedRequest,
        {
            "latestImageResult": None,
            "latestImageUnderstanding": None,
            "latestSearch": None,
        },
    )

    assert decision["actionType"] == "tool"
    assert decision["toolName"] == "read_reference_images"
    assert decision["toolArgs"]["assetUrls"] == ["https://example.com/a.png"]


def testFallbackDecisionDoesNotReadReferenceImagesWhenPromptIsGeneric() -> None:
    service = createService()
    parsedRequest = ParsedAgentRequest(
        userInput="帮我生成一张香薰机电商主图",
        combinedUserContext="帮我生成一张香薰机电商主图",
        assetUrls=["https://example.com/a.png"],
        aspectRatio="1:1",
    )

    decision = service._fallbackDecision(
        parsedRequest,
        {
            "latestImageResult": None,
            "latestImageUnderstanding": None,
            "latestSearch": None,
        },
    )

    assert decision["actionType"] == "tool"
    assert decision["toolName"] == "create_image"


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


def testFallbackDecisionChoosesCreateCopyForWebRequest() -> None:
    service = createService()
    parsedRequest = ParsedAgentRequest(
        userInput="帮我做一篇香薰机微信公众号长图文",
        combinedUserContext="帮我做一篇香薰机微信公众号长图文",
    )

    decision = service._fallbackDecision(
        parsedRequest,
        {"latestImageResult": None, "latestSearch": None, "latestCopyResult": None, "latestWebResult": None},
    )

    assert decision["actionType"] == "tool"
    assert decision["toolName"] == "create_copy"


def testFallbackDecisionChoosesComposeWebWhenCopyAndImagesReady() -> None:
    service = createService()
    parsedRequest = ParsedAgentRequest(
        userInput="帮我做一篇香薰机微信公众号长图文",
        combinedUserContext="帮我做一篇香薰机微信公众号长图文",
    )

    decision = service._fallbackDecision(
        parsedRequest,
        {
            "latestSearch": None,
            "latestCopyResult": {
                "title": "香薰机春日图文",
                "sections": [{"sectionId": "highlight", "heading": "亮点", "body": "内容", "imagePrompt": "提示词"}],
            },
            "imageArtifacts": [
                {
                    "artifactId": "section::highlight",
                    "taskId": "task-1",
                    "assetName": "亮点配图",
                    "targetSectionId": "highlight",
                    "targetSectionTitle": "亮点",
                    "status": "succeed",
                    "storedResults": [{"url": "https://example.com/1.png"}],
                    "resultUrls": [],
                }
            ],
            "latestImageResult": None,
            "latestWebResult": None,
        },
    )

    assert decision["actionType"] == "tool"
    assert decision["toolName"] == "compose_web"
    assert decision["toolArgs"]["imageAssets"][0]["targetSectionId"] == "highlight"


def testUpdateStateFromToolResultAccumulatesImageArtifacts() -> None:
    service = createService()
    state: dict[str, object] = {"imageArtifacts": []}

    service._updateStateFromToolResult(
        "create_image",
        {
            "artifactId": "section::highlight",
            "taskId": "task-1",
            "assetName": "亮点配图",
            "targetSectionId": "highlight",
            "targetSectionTitle": "亮点",
            "status": "succeed",
            "resultUrls": ["https://example.com/1-temp.png"],
            "storedResults": [],
        },
        state,
    )
    service._updateStateFromToolResult(
        "create_image",
        {
            "artifactId": "section::detail",
            "taskId": "task-2",
            "assetName": "细节配图",
            "targetSectionId": "detail",
            "targetSectionTitle": "细节",
            "status": "succeed",
            "resultUrls": ["https://example.com/2-temp.png"],
            "storedResults": [],
        },
        state,
    )

    artifacts = state["imageArtifacts"]
    assert isinstance(artifacts, list)
    assert len(artifacts) == 2


def testUpdateStateFromToolResultStoresImageUnderstanding() -> None:
    service = createService()
    state: dict[str, object] = {}

    service._updateStateFromToolResult(
        "read_reference_images",
        {
            "summary": "已完成参考图理解。",
            "keyDetails": ["主体信息", "构图信息"],
            "promptHints": ["保留关键元素", "补充具体视觉细节"],
            "images": [{"url": "https://example.com/a.png"}],
        },
        state,
    )

    understanding = state["latestImageUnderstanding"]
    assert isinstance(understanding, dict)
    assert understanding["summary"] == "已完成参考图理解。"


def testFindNextImageSlotSkipsCoveredSections() -> None:
    service = createService()

    slot = service._findNextImageSlot(
        {
            "sections": [
                {"sectionId": "highlight", "heading": "亮点", "body": "内容", "imagePrompt": "图一"},
                {"sectionId": "detail", "heading": "细节", "body": "内容", "imagePrompt": "图二"},
            ]
        },
        [
            {
                "artifactId": "section::highlight",
                "targetSectionId": "highlight",
                "storedResults": [{"url": "https://example.com/1.png"}],
                "resultUrls": [],
            }
        ],
    )

    assert slot is not None
    assert slot["targetSectionId"] == "detail"


def testSanitizeFinalResponseRemovesLinks() -> None:
    service = createService()

    result = service._sanitizeFinalResponse(
        "这是效果图，点击查看：[法式客厅效果图](https://example.com/a.png)",
        {},
    )

    assert "http" not in result
    assert "点击查看" not in result
    assert result == "图片已经生成好了，可以继续告诉我你想怎么调整。"


def testFallbackFinalResponseForWebResult() -> None:
    service = createService()

    result = service._fallbackFinalResponse(
        {"latestWebResult": {"html": "<!doctype html><html></html>"}},
    )

    assert result == "图文内容已经排版好了，你可以继续告诉我想怎么调整。"
