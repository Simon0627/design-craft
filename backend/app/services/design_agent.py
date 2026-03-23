from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from langchain_core.prompts import ChatPromptTemplate

from app.clients.kodo import KodoClient
from app.clients.qiniu_maas import QiniuMaaSClient
from app.core.config import Settings
from app.core.exceptions import AppError
from app.schemas.design import (
    DesignGenerateRequest,
    DesignGenerateResponse,
    DesignPlan,
    DesignPlanRequest,
    ImageTaskStatusResponse,
    StoredObject,
)
from app.services.image_assets import ImageAssetService
from app.services.skills import SkillService


class DesignAgentService:
    def __init__(
        self,
        settings: Settings,
        maasClient: QiniuMaaSClient,
        kodoClient: KodoClient,
        skillService: SkillService,
        imageAssetService: ImageAssetService,
    ):
        self.settings = settings
        self.maasClient = maasClient
        self.kodoClient = kodoClient
        self.skillService = skillService
        self.imageAssetService = imageAssetService
        self.planFormatInstructions = "\n".join(
            [
                "返回一个 JSON 对象，字段必须完整：",
                '{',
                '  "intentSummary": "string，用户目标摘要",',
                '  "generationMode": "text_to_image | image_to_image | multi_image_edit",',
                '  "prompt": "string，发给生图接口的提示词",',
                '  "aspectRatio": "16:9 | 9:16 | 1:1 | 4:3 | 3:4 | 3:2 | 2:3 | 21:9",',
                '  "shouldUseSearch": "boolean",',
                '  "searchQueries": ["string"],',
                '  "contentSearchQueries": ["string"],',
                '  "imageSearchQueries": ["string"],',
                '  "selectedSkillNames": ["string"],',
                '  "assetUrls": ["string"],',
                '  "referenceLinks": ["string"],',
                '  "notes": ["string"]',
                '}',
                "不要输出额外解释。",
            ]
        )
        self.planPrompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "\n".join(
                        [
                            "你是 DesignCraft 的设计任务规划代理。",
                            "你的工作是理解用户需求、素材和参考链接，给出结构化的生图规划。",
                            "只允许选择以下 generationMode：text_to_image、image_to_image、multi_image_edit。",
                            "如果用户强制指定 generationMode，必须优先遵守。",
                            "如果素材数为 0，优先选择 text_to_image；素材数为 1，优先选择 image_to_image；素材数 >= 2，优先选择 multi_image_edit。",
                            "contentSearchQueries 用于文本搜索，imageSearchQueries 用于图片搜索；每个字段尽量给出 1-3 条查询词。",
                            "searchQueries 作为兼容字段，应与 contentSearchQueries 保持一致或为空。",
                            "selectedSkillNames 只能从可用技能列表中选择；没有合适技能时返回空数组。",
                            "{formatInstructions}",
                        ]
                    ),
                ),
                (
                    "human",
                    "\n".join(
                        [
                            "用户需求：{userInput}",
                            "用户指定模式：{forcedMode}",
                            "用户指定比例：{aspectRatio}",
                            "素材列表：{assetUrlsText}",
                            "参考链接：{referenceLinksText}",
                            "可用技能：{availableSkills}",
                            "请输出严格 JSON。",
                        ]
                    ),
                ),
            ]
        )

    async def planDesign(self, request: DesignPlanRequest) -> DesignPlan:
        availableSkills = self.skillService.listSkills()
        promptMessages = self.planPrompt.format_messages(
            formatInstructions=self.planFormatInstructions,
            userInput=request.userInput,
            forcedMode=request.generationMode or "auto",
            aspectRatio=request.aspectRatio or self.settings.defaultAspectRatio,
            assetUrlsText=self._formatList(request.assetUrls),
            referenceLinksText=self._formatList(request.referenceLinks),
            availableSkills=self._formatSkills(availableSkills),
        )
        messages = self._buildMaasMessages(promptMessages, request)
        rawContent = await self.maasClient.createChatCompletion(
            messages=messages,
            model=self.settings.qiniuChatModel,
            maxTokens=self.settings.plannerMaxTokens,
        )
        return self._parsePlan(rawContent, request, availableSkills)

    async def generateDesign(self, request: DesignGenerateRequest) -> DesignGenerateResponse:
        plan = await self.planDesign(request)
        return await self.generateFromPlan(plan, request)

    async def generateFromPlan(self, plan: DesignPlan, request: DesignGenerateRequest) -> DesignGenerateResponse:
        await self._validateAssetsForGeneration(plan.generationMode, request.assetUrls)
        payload, multiImage = await self._buildImagePayload(plan, request)
        submitResult = await self.maasClient.createImageTask(payload, multiImage=multiImage)
        taskId = submitResult.get("task_id")
        if not taskId:
            raise AppError("七牛图片任务创建成功但未返回 task_id。", statusCode=502, code="invalid_upstream_response")

        if request.waitForResult:
            taskResponse = await self.waitForTask(
                taskId=taskId,
                autoStoreResult=request.autoStoreResult,
                outputKeyPrefix=request.outputKeyPrefix,
                timeoutSeconds=request.taskPollTimeoutSeconds or self.settings.taskPollTimeoutSeconds,
            )
            return DesignGenerateResponse(plan=plan, **taskResponse.model_dump())

        return DesignGenerateResponse(
            plan=plan,
            taskId=taskId,
            status="submitted",
            statusMessage="任务已提交",
            created=None,
            resultUrls=[],
            storedResults=[],
            rawTask=submitResult,
        )

    async def refinePlanWithResearch(
        self,
        plan: DesignPlan,
        contentSearchResults: dict[str, Any],
        imageSearchResults: dict[str, Any],
    ) -> DesignPlan:
        contentSummary = self._summarizeResearch(contentSearchResults.get("results", []), "文本")
        imageSummary = self._summarizeResearch(imageSearchResults.get("results", []), "图片")
        if not contentSummary and not imageSummary:
            return plan

        promptMessages = [
            {
                "role": "system",
                "content": (
                    "你是电商设计提示词优化助手。"
                    "请结合已有提示词、文本搜索结果和图片搜索结果，生成更适合图片创作模型的最终提示词。"
                    "只返回 JSON：{\"prompt\": \"...\", \"notes\": [\"...\"]}"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"原始提示词：{plan.prompt}\n"
                    f"文本搜索摘要：{contentSummary or '无'}\n"
                    f"图片搜索摘要：{imageSummary or '无'}\n"
                    "请输出更适合电商图片创作的提示词。"
                ),
            },
        ]

        try:
            rawContent = await self.maasClient.createChatCompletion(
                messages=promptMessages,
                model=self.settings.qiniuChatModel,
                maxTokens=512,
                temperature=0.2,
            )
            parsed = json.loads(self._extractJson(rawContent))
            return plan.model_copy(
                update={
                    "prompt": parsed.get("prompt", plan.prompt),
                    "notes": plan.notes + parsed.get("notes", []),
                }
            )
        except Exception:
            return plan

    async def getTaskStatus(
        self,
        taskId: str,
        autoStoreResult: bool = True,
        outputKeyPrefix: str = "generated",
    ) -> ImageTaskStatusResponse:
        taskData = await self.maasClient.getImageTask(taskId)
        return await self._buildTaskStatusResponse(taskData, autoStoreResult, outputKeyPrefix)

    async def waitForTask(
        self,
        taskId: str,
        autoStoreResult: bool,
        outputKeyPrefix: str,
        timeoutSeconds: int,
    ) -> ImageTaskStatusResponse:
        deadline = asyncio.get_running_loop().time() + timeoutSeconds
        while True:
            statusResponse = await self.getTaskStatus(taskId, autoStoreResult, outputKeyPrefix)
            if statusResponse.status in {"succeed", "failed"}:
                return statusResponse
            if asyncio.get_running_loop().time() >= deadline:
                raise AppError(
                    "等待图片生成结果超时。",
                    statusCode=504,
                    code="task_timeout",
                    detail={"taskId": taskId, "timeoutSeconds": timeoutSeconds},
                )
            await asyncio.sleep(self.settings.taskPollIntervalSeconds)

    async def _buildTaskStatusResponse(
        self,
        taskData: dict[str, Any],
        autoStoreResult: bool,
        outputKeyPrefix: str,
    ) -> ImageTaskStatusResponse:
        resultUrls = [item.get("url", "") for item in taskData.get("data", []) if item.get("url")]
        storedResults: list[StoredObject] = []

        if autoStoreResult and taskData.get("status") == "succeed":
            storedResults = await self.storeGeneratedResults(taskData["task_id"], resultUrls, outputKeyPrefix)

        return ImageTaskStatusResponse(
            taskId=taskData["task_id"],
            status=taskData["status"],
            statusMessage=taskData.get("status_message", ""),
            created=taskData.get("created"),
            resultUrls=resultUrls,
            storedResults=storedResults,
            rawTask=taskData,
        )

    async def storeGeneratedResults(
        self,
        taskId: str,
        resultUrls: list[str],
        outputKeyPrefix: str = "generated",
    ) -> list[StoredObject]:
        storedResults: list[StoredObject] = []
        for index, url in enumerate(resultUrls):
            objectKey = self.kodoClient.buildResultKey(taskId, index, url, outputKeyPrefix)
            storedResults.append(await self.kodoClient.mirrorRemoteFile(url, objectKey))
        return storedResults

    def _parsePlan(
        self,
        rawContent: str,
        request: DesignPlanRequest,
        availableSkills: list[Any],
    ) -> DesignPlan:
        try:
            jsonContent = self._extractJson(rawContent)
            parsedPlan = DesignPlan.model_validate(json.loads(jsonContent))
        except Exception:
            parsedPlan = self._buildFallbackPlan(request)

        validSkillNames = {skill.name for skill in availableSkills}
        filteredSkillNames = [name for name in parsedPlan.selectedSkillNames if name in validSkillNames]

        planData = parsedPlan.model_dump()
        planData["selectedSkillNames"] = filteredSkillNames
        planData["contentSearchQueries"] = planData.get("contentSearchQueries") or planData.get("searchQueries") or []
        planData["imageSearchQueries"] = planData.get("imageSearchQueries") or []
        planData["searchQueries"] = planData["contentSearchQueries"]
        planData["assetUrls"] = request.assetUrls
        planData["referenceLinks"] = request.referenceLinks
        if request.aspectRatio:
            planData["aspectRatio"] = request.aspectRatio
        if request.generationMode:
            planData["generationMode"] = request.generationMode

        return DesignPlan.model_validate(planData)

    def _buildFallbackPlan(self, request: DesignPlanRequest) -> DesignPlan:
        if request.generationMode:
            generationMode = request.generationMode
        elif len(request.assetUrls) >= 2:
            generationMode = "multi_image_edit"
        elif len(request.assetUrls) == 1:
            generationMode = "image_to_image"
        else:
            generationMode = "text_to_image"

        return DesignPlan(
            intentSummary=request.userInput[:80],
            generationMode=generationMode,
            prompt=request.userInput,
            aspectRatio=request.aspectRatio or self.settings.defaultAspectRatio,
            shouldUseSearch=False,
            searchQueries=[],
            contentSearchQueries=[request.userInput[:80]],
            imageSearchQueries=[request.userInput[:80]],
            selectedSkillNames=[],
            assetUrls=request.assetUrls,
            referenceLinks=request.referenceLinks,
            notes=["规划结果由兜底规则生成，建议后续补充更多技能或提示词优化。"],
        )

    def _buildMaasMessages(self, promptMessages: list[Any], request: DesignPlanRequest) -> list[dict[str, Any]]:
        roleMap = {"system": "system", "human": "user", "ai": "assistant"}
        maasMessages: list[dict[str, Any]] = []

        for message in promptMessages:
            if message.type == "human":
                content: Any = [{"type": "text", "text": str(message.content)}]
                for assetUrl in request.assetUrls:
                    content.append({"type": "image_url", "image_url": {"url": assetUrl}})
                maasMessages.append({"role": "user", "content": content})
                continue

            maasMessages.append({"role": roleMap.get(message.type, "user"), "content": str(message.content)})

        return maasMessages

    async def _validateAssetsForGeneration(self, generationMode: str, assetUrls: list[str]) -> None:
        if generationMode == "text_to_image":
            return
        for assetUrl in assetUrls:
            validationUrl = assetUrl
            if self.kodoClient.isBucketUrl(assetUrl):
                validationUrl = self.kodoClient.buildPrivateDownloadUrl(assetUrl)
            imageMeta = await self.imageAssetService.fetchRemoteImageMeta(validationUrl)
            imageMeta.url = assetUrl
            self.imageAssetService.validateForGeneration(imageMeta)

    async def _buildImagePayload(
        self,
        plan: DesignPlan,
        request: DesignGenerateRequest,
    ) -> tuple[dict[str, Any], bool]:
        payload: dict[str, Any] = {
            "model": self.settings.qiniuImageModel,
            "prompt": plan.prompt,
            "n": request.imageCount or self.settings.defaultImageCount,
            "aspect_ratio": plan.aspectRatio,
        }

        if plan.generationMode == "text_to_image":
            return payload, False

        if plan.generationMode == "image_to_image":
            if not request.assetUrls:
                raise AppError("单图生图至少需要 1 张素材。", statusCode=422, code="invalid_request")
            if self.kodoClient.isBucketUrl(request.assetUrls[0]):
                payload["image"] = await self.kodoClient.fetchObjectBase64(request.assetUrls[0])
            else:
                payload["image"] = request.assetUrls[0]
            return payload, False

        if len(request.assetUrls) < 2:
            raise AppError("多图生图至少需要 2 张素材。", statusCode=422, code="invalid_request")
        payload["image"] = ""
        payload["subject_image_list"] = [{"subject_image": url} for url in request.assetUrls[:4]]
        return payload, True

    def _extractJson(self, rawContent: str) -> str:
        fencedMatch = re.search(r"```json\s*(\{.*?\})\s*```", rawContent, re.DOTALL)
        if fencedMatch:
            return fencedMatch.group(1)

        plainMatch = re.search(r"(\{.*\})", rawContent, re.DOTALL)
        if plainMatch:
            json.loads(plainMatch.group(1))
            return plainMatch.group(1)

        json.loads(rawContent)
        return rawContent

    def _formatList(self, values: list[str]) -> str:
        if not values:
            return "无"
        return "\n".join(f"- {value}" for value in values)

    def _formatSkills(self, skills: list[Any]) -> str:
        if not skills:
            return "无"
        return "\n".join(f"- {skill.name}: {skill.description}" for skill in skills)

    def _summarizeResearch(self, results: list[dict[str, Any]], label: str) -> str:
        if not results:
            return ""
        parts: list[str] = []
        for item in results[:3]:
            title = item.get("title", "")
            snippet = item.get("snippet", "") or item.get("hostPageUrl", "")
            if title or snippet:
                parts.append(f"{label}结果：{title} {snippet}".strip())
        return "；".join(parts)
