from __future__ import annotations

from html import escape
import json
import re
import uuid
from typing import Any, AsyncIterator

from app.core.config import Settings
from app.core.exceptions import AppError
from app.schemas.agui import ParsedAgentRequest, RunAgentInput
from app.schemas.design import DesignGenerateRequest, DesignPlan, GenerationMode
from app.services.design_agent import DesignAgentService
from app.services.search import SearchService


class AgUiAgentService:
    def __init__(
        self,
        settings: Settings,
        designService: DesignAgentService,
        searchService: SearchService,
    ):
        self.settings = settings
        self.designService = designService
        self.searchService = searchService
        self.maxToolIterations = 10

    async def run(self, agentInput: RunAgentInput) -> AsyncIterator[dict[str, Any]]:
        threadId = agentInput.threadId
        runId = agentInput.runId
        assistantMessageId = f"msg_{uuid.uuid4().hex}"
        state: dict[str, Any] = {
            "threadId": threadId,
            "runId": runId,
            "phase": "started",
            "toolCalls": [],
            "latestSearch": None,
            "latestImageUnderstanding": None,
            "latestCopyResult": None,
            "latestImageResult": None,
            "imageArtifacts": [],
            "latestWebResult": None,
            "finalResponse": None,
            "pendingFollowUp": None,
        }

        yield self._event(
            "RUN_STARTED",
            threadId=threadId,
            runId=runId,
            parentRunId=agentInput.parentRunId,
            input=agentInput.model_dump(),
        )
        yield self._event("STATE_SNAPSHOT", snapshot=state)

        try:
            parsedRequest = self._parseAgentInput(agentInput)
            if not parsedRequest.userInput:
                raise AppError("AG-UI 请求缺少有效的用户文本输入。", statusCode=422, code="missing_user_input")

            for iteration in range(1, self.maxToolIterations + 1):
                stepName = f"llm_decision_{iteration}"
                yield self._event("STEP_STARTED", stepName=stepName)
                decision = await self._decideNextAction(parsedRequest, state)
                state["phase"] = f"decision_{iteration}"
                yield self._event("TEXT_MESSAGE_START", messageId=assistantMessageId, role="assistant")
                yield self._event("TEXT_MESSAGE_CONTENT", messageId=assistantMessageId, delta=decision.get("thinking", ""))
                yield self._event("TEXT_MESSAGE_END", messageId=assistantMessageId)
                yield self._event(
                    "STATE_SNAPSHOT",
                    snapshot={**state, "latestDecision": decision},
                )
                yield self._event("STEP_FINISHED", stepName=stepName)

                if decision.get("actionType") == "final":
                    finalResponse = decision.get("finalResponse") or self._fallbackFinalResponse(state)
                    state["phase"] = "completed"
                    state["finalResponse"] = finalResponse
                    finalMessageId = f"msg_{uuid.uuid4().hex}"
                    yield self._event("TEXT_MESSAGE_START", messageId=finalMessageId, role="assistant")
                    yield self._event("TEXT_MESSAGE_CONTENT", messageId=finalMessageId, delta=finalResponse)
                    yield self._event("TEXT_MESSAGE_END", messageId=finalMessageId)
                    yield self._event("STATE_SNAPSHOT", snapshot=state)
                    yield self._event("RUN_FINISHED", threadId=threadId, runId=runId, result=state)
                    return

                toolName = decision.get("toolName", "")
                toolArgs = decision.get("toolArgs", {}) if isinstance(decision.get("toolArgs"), dict) else {}
                yield self._event("STEP_STARTED", stepName=toolName or f"tool_{iteration}")
                async for event in self._executeTool(toolName, toolArgs, parsedRequest, state, assistantMessageId):
                    yield event
                yield self._event("STEP_FINISHED", stepName=toolName or f"tool_{iteration}")

                if toolName == "ask_followup":
                    state["phase"] = "needs_followup"
                    yield self._event("STATE_SNAPSHOT", snapshot=state)
                    yield self._event("RUN_FINISHED", threadId=threadId, runId=runId, result=state)
                    return

            raise AppError("顶层 LLM 超过最大工具调用次数，仍未完成任务。", statusCode=500, code="agent_max_iterations")
        except Exception as exc:
            appError = exc if isinstance(exc, AppError) else AppError(
                "AG-UI 运行失败。",
                statusCode=500,
                code="agui_run_error",
                detail={"reason": str(exc)},
            )
            yield self._event("RUN_ERROR", message=appError.message, code=appError.code, detail=appError.detail)

    async def _decideNextAction(self, parsedRequest: ParsedAgentRequest, state: dict[str, Any]) -> dict[str, Any]:
        toolDescriptions = [
            {
                "name": "ask_followup",
                "description": "当用户信息不足，暂时无法稳定交付高质量结果时，向用户追问关键信息。",
                "args": {
                    "question": "string，给用户看的追问句子",
                    "options": "string[]，2 到 4 个可选项",
                    "inputPlaceholder": "string，可选，提示用户也可以补充一句自己的要求",
                },
            },
            {
                "name": "search_content",
                "description": "当你需要补充商品卖点、风格参考、行业表达、营销信息时使用文本搜索。",
                "args": {
                    "query": "string，搜索词",
                    "count": "integer，可选，返回条数，默认 5",
                },
            },
            {
                "name": "read_reference_images",
                "description": "当任务依赖参考图理解时使用。适合识别产品主体、空间布局、风格元素、材质、构图、视角、应保留的部分，以及应该写进提示词的细节。",
                "args": {
                    "focus": "string，可选，本次读图想重点关注什么",
                    "assetUrls": "string[]，可选，要分析的参考图列表，默认使用当前请求里的参考图",
                },
            },
            {
                "name": "create_copy",
                "description": "当你需要先产出长图文、公众号、H5 页面所需的文案结构、段落内容、分节标题时使用。",
                "args": {
                    "brief": "string，可选，想重点强调的内容方向",
                    "tone": "string，可选，文案语气",
                    "sections": "integer，可选，期望分成几段，默认 4",
                },
            },
            {
                "name": "create_image",
                "description": "当用户需要产出图片时使用。你需要结合风格、内容给出最终生图详细的 prompt。",
                "args": {
                    "prompt": "string，最终生图提示词",
                    "aspectRatio": "string，枚举值：16:9、9:16、1:1、4:3、3:4、3:2、2:3、21:9",
                    "assetUrls": "string[]，可选",
                    "generationMode": "text_to_image | image_to_image | multi_image_edit，可选",
                    "imageCount": "integer，可选",
                    "assetName": "string，可选，这张图的名字，比如头图、卖点配图 1",
                    "targetSectionId": "string，可选，这张图对应的 sectionId",
                    "targetSectionTitle": "string，可选，这张图对应的小节标题",
                },
            },
            {
                "name": "store_result",
                "description": "当 create_image 已经生成出临时图片，需要批量转存到七牛空间交付时使用。",
                "args": {
                    "taskId": "string，create_image 返回的 taskId",
                    "resultUrls": "string[]，create_image 返回的临时结果 URL",
                    "outputKeyPrefix": "string，可选",
                    "assetName": "string，可选，对应的图片名字",
                    "targetSectionId": "string，可选，对应的 sectionId",
                    "targetSectionTitle": "string，可选，对应的小节标题",
                    "artifacts": "object[]，可选，批量待转存图片",
                },
            },
            {
                "name": "compose_web",
                "description": "当你已经拿到文案和图片素材，需要生成可浏览、可预览的图文网页时使用。优先使用 state.imageArtifacts 里累计好的素材，而不是只看最后一张图。",
                "args": {
                    "title": "string，可选，网页标题",
                    "layoutStyle": "string，可选，如公众号长图文、品牌长页、H5 落地页",
                    "copyOutline": "object，可选，可直接传文案结构",
                    "imageAssets": "object[]，可选，结构化图片素材清单，包含名字、段落、图片地址",
                },
            },
        ]

        messages = [
            {
                "role": "system",
                "content": (
                    "你是 DesignCraft 的顶层 Agent。"
                    "你可以自由决定是否调用工具，也可以直接给出最终答复。"
                    "你的唯一可用工具只有：ask_followup、search_content、read_reference_images、create_copy、create_image、store_result、compose_web。"
                    "请基于当前上下文选择最合适的一步。"
                    "如果用户只是咨询建议，可直接 final。"
                    "如果用户要图片，通常需要 create_image。"
                    "如果用户明确提到小红书、种草笔记、图文社媒卡片、社媒笔记等场景，不要调用额外的新工具。"
                    "你应该复用 create_copy 和 create_image：先生成一篇带 emoji 的小红书文案，再根据每段文案生成多张风格统一的配图。"
                    "小红书场景不需要 compose_web，最终直接把文案作为聊天回复返回给用户即可。"
                    "这段文案不要带任何 Markdown 标记，不要有标题井号、加粗符号、列表符号或代码块。"
                    "小红书图片默认比例是 3:4，风格要更可爱、卡通、二维、高饱和度，并保持整组图片在色调、装饰元素、氛围上的连续性。"
                    "请优先把图片理解成插画感图文卡片，而不是照片。"
                    "要主动强调：二维插画、平面设计、手绘/扁平、海报卡片感、非真实摄影、非写实产品照。"
                    "注意：小红书场景里的图片应该和文案互相补充。"
                    "如果某一段内容适合做成带大标题、重点句、清单或短段落的图文卡片，你应该把这些文字内容一并写进 create_image 的 prompt，"
                    "让图里的文字表达和最终交付文案保持一致。"
                    "如果小红书需求描述得很模糊，先 ask_followup。最终交付应该是一段文案 + 多张图片。"
                    "小红书最终回复建议直接输出“标题 + 正文”，标题单独成行，正文可多行展开。"
                    "不是所有带参考图的任务都必须先 read_reference_images。"
                    "只有当参考图理解会明显帮助你判断主体、视角、构图、材质、空间结构、应保留元素，或者能让后续文案和提示词更具体时，才调用它。"
                    "如果任务明显依赖参考图理解，比如需要保留原图主体、视角、构图、空间结构、产品细节、材质、颜色、风格元素，"
                    "或者你准备基于参考图去写更具体的文案和提示词，应优先调用 read_reference_images。"
                    "read_reference_images 的结果会累积到 state.latestImageUnderstanding，你后续写 create_copy 和 create_image 参数时应该主动参考它。"
                    "如果已经拿到 resultUrls 且还没有 storedResults，通常需要 store_result。"
                    "如果用户要的是长图文、公众号排版、图文长页、H5 风格页面或可浏览的 Web 内容，"
                    "你应该把任务拆成多步：如有参考图先 read_reference_images，必要时 search_content 补充资料，再 create_copy 生成文案结构，"
                    "再按需要多次调用 create_image 为每段文本生成配图，必要时调用 store_result 固化图片地址，最后调用 compose_web 生成网页。"
                    "你可以多次循环调用 create_copy 和 create_image，不要急着一次做完。"
                    "read_reference_images、create_copy、create_image、store_result 这些子任务的结构化结果，都会累积在当前 state 中返回给你。"
                    "你在决定 compose_web 时，应该优先查看 state.latestCopyResult、state.latestImageUnderstanding 和 state.imageArtifacts，确认每张图对应哪一段内容。"
                    "在这类 Web 内容场景里，不要直接把 HTML 放进 finalResponse，必须通过 compose_web 工具输出。"
                    "如果用户信息明显不足，暂时无法稳定交付高质量结果，应优先调用 ask_followup。"
                    "ask_followup 的 question、options、inputPlaceholder 都必须是亲切自然的中文。"
                    "`thinking` 和 `finalResponse` 必须始终使用自然、亲切、简洁的中文，不要返回英文，也不要中英夹杂。"
                    "即使是思考摘要，也请直接写给用户可读的中文短句。"
                    "除非用户明确要求英文，否则不要输出英文。"
                    "`finalResponse` 必须是一句简短中文，不要使用 Markdown 超链接、不要输出 URL、不要写“点击查看”。"
                    "当 compose_web 已经完成时，只需要简洁告诉用户图文内容已经排版好了。"
                    "如果图片已经生成完成，只需要简洁告诉用户“图片已经生成好了，可以继续调整”。"
                    "只返回严格 JSON："
                    "{\"thinking\":\"...\",\"actionType\":\"tool|final\",\"toolName\":\"...\",\"toolArgs\":{},\"finalResponse\":\"...\"}"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"用户需求：{parsedRequest.userInput}\n"
                    f"用户上下文：{parsedRequest.combinedUserContext or parsedRequest.userInput}\n"
                    f"对话历史：{json.dumps(parsedRequest.conversationHistory, ensure_ascii=False)}\n"
                    f"参考素材：{json.dumps(parsedRequest.assetUrls, ensure_ascii=False)}\n"
                    f"参考链接：{json.dumps(parsedRequest.referenceLinks, ensure_ascii=False)}\n"
                    f"用户指定比例：{parsedRequest.aspectRatio or '无'}\n"
                    f"当前状态：{json.dumps(state, ensure_ascii=False)}\n"
                    f"可用工具：{json.dumps(toolDescriptions, ensure_ascii=False)}"
                ),
            },
        ]

        try:
            rawContent = await self.designService.maasClient.createChatCompletion(
                messages=messages,
                model=self.settings.qiniuChatModel,
                maxTokens=800,
                temperature=0.2,
            )
            decision = json.loads(self._extractJson(rawContent))
            return self._normalizeDecision(decision, parsedRequest, state)
        except Exception:
            return self._fallbackDecision(parsedRequest, state)

    async def _executeTool(
        self,
        toolName: str,
        toolArgs: dict[str, Any],
        parsedRequest: ParsedAgentRequest,
        state: dict[str, Any],
        parentMessageId: str,
    ) -> AsyncIterator[dict[str, Any]]:
        toolCallId = f"tool_{uuid.uuid4().hex}"
        yield self._event("TOOL_CALL_START", toolCallId=toolCallId, toolCallName=toolName, parentMessageId=parentMessageId)
        yield self._event("TOOL_CALL_ARGS", toolCallId=toolCallId, delta=json.dumps(toolArgs, ensure_ascii=False))
        yield self._event("TOOL_CALL_END", toolCallId=toolCallId)

        result = await self._runTool(toolName, toolArgs, parsedRequest, state)
        resultPayload = result.model_dump() if hasattr(result, "model_dump") else result
        state["toolCalls"].append({"toolName": toolName, "toolArgs": toolArgs, "result": resultPayload})
        self._updateStateFromToolResult(toolName, resultPayload, state)
        yield self._event(
            "TOOL_CALL_RESULT",
            messageId=f"msg_{uuid.uuid4().hex}",
            toolCallId=toolCallId,
            content=json.dumps(resultPayload, ensure_ascii=False),
            role="tool",
        )
        yield self._event("STATE_SNAPSHOT", snapshot=state)

    async def _runTool(
        self,
        toolName: str,
        toolArgs: dict[str, Any],
        parsedRequest: ParsedAgentRequest,
        state: dict[str, Any],
    ) -> Any:
        if toolName == "ask_followup":
            return self._sanitizeFollowUp(toolArgs, parsedRequest)

        if toolName == "search_content":
            query = str(toolArgs.get("query") or parsedRequest.combinedUserContext or parsedRequest.userInput)
            count = int(toolArgs.get("count") or self.settings.agUiSearchResultLimit)
            return await self.searchService.searchContent(query, count)

        if toolName == "read_reference_images":
            return await self._readReferenceImages(toolArgs, parsedRequest)

        if toolName == "create_copy":
            return await self._createCopy(toolArgs, parsedRequest, state)

        if toolName == "create_image":
            assetUrls = self._mergeUnique(toolArgs.get("assetUrls", []), parsedRequest.assetUrls)
            aspectRatio = toolArgs.get("aspectRatio") or parsedRequest.aspectRatio or self.settings.defaultAspectRatio
            imageCount = int(toolArgs.get("imageCount") or parsedRequest.imageCount or self.settings.defaultImageCount)
            generationMode = self._normalizeGenerationMode(toolArgs.get("generationMode"), assetUrls)
            prompt = str(toolArgs.get("prompt") or parsedRequest.combinedUserContext or parsedRequest.userInput)
            assetName = str(toolArgs.get("assetName") or "配图").strip() or "配图"
            targetSectionId = str(toolArgs.get("targetSectionId") or "").strip()
            targetSectionTitle = str(toolArgs.get("targetSectionTitle") or "").strip()

            request = DesignGenerateRequest(
                userInput=prompt,
                assetUrls=assetUrls,
                referenceLinks=parsedRequest.referenceLinks,
                aspectRatio=aspectRatio,
                generationMode=generationMode,
                imageCount=imageCount,
                autoStoreResult=False,
                waitForResult=False,
                outputKeyPrefix=parsedRequest.outputKeyPrefix,
                taskPollTimeoutSeconds=parsedRequest.taskPollTimeoutSeconds,
            )
            plan = DesignPlan(
                intentSummary=prompt[:80],
                generationMode=generationMode,
                prompt=prompt,
                aspectRatio=aspectRatio,
                shouldUseSearch=False,
                searchQueries=[],
                contentSearchQueries=[],
                imageSearchQueries=[],
                selectedSkillNames=[],
                assetUrls=assetUrls,
                referenceLinks=parsedRequest.referenceLinks,
                notes=["由顶层 LLM 通过 create_image 工具触发生成。"],
            )
            submitted = await self.designService.generateFromPlan(plan, request)
            finalResult = await self.designService.waitForTask(
                taskId=submitted.taskId,
                autoStoreResult=False,
                outputKeyPrefix=parsedRequest.outputKeyPrefix,
                timeoutSeconds=parsedRequest.taskPollTimeoutSeconds or self.settings.taskPollTimeoutSeconds,
            )
            resultPayload = finalResult.model_dump()
            resultPayload["plan"] = plan.model_dump()
            resultPayload["assetName"] = assetName
            resultPayload["targetSectionId"] = targetSectionId
            resultPayload["targetSectionTitle"] = targetSectionTitle
            resultPayload["prompt"] = prompt
            resultPayload["artifactId"] = self._buildImageArtifactId(targetSectionId, assetName, submitted.taskId)
            return resultPayload

        if toolName == "store_result":
            latestImageResult = state.get("latestImageResult") or {}
            outputKeyPrefix = str(toolArgs.get("outputKeyPrefix") or parsedRequest.outputKeyPrefix)
            artifactsArg = toolArgs.get("artifacts")
            if isinstance(artifactsArg, list):
                artifacts = [item for item in artifactsArg if isinstance(item, dict)]
            else:
                taskId = str(toolArgs.get("taskId") or latestImageResult.get("taskId") or "")
                resultUrls = toolArgs.get("resultUrls") or latestImageResult.get("resultUrls") or []
                if not taskId or not resultUrls:
                    raise AppError("store_result 缺少 taskId 或 resultUrls。", statusCode=422, code="invalid_store_result_args")
                artifacts = [
                    {
                        "artifactId": str(toolArgs.get("artifactId") or latestImageResult.get("artifactId") or self._buildImageArtifactId("", "配图", taskId)),
                        "taskId": taskId,
                        "resultUrls": resultUrls,
                        "assetName": str(toolArgs.get("assetName") or latestImageResult.get("assetName") or "配图"),
                        "targetSectionId": str(toolArgs.get("targetSectionId") or latestImageResult.get("targetSectionId") or ""),
                        "targetSectionTitle": str(toolArgs.get("targetSectionTitle") or latestImageResult.get("targetSectionTitle") or ""),
                    }
                ]

            if not artifacts:
                raise AppError("store_result 缺少可转存的 artifacts。", statusCode=422, code="invalid_store_result_args")

            storedArtifacts = await self.designService.storeGeneratedArtifactBatch(artifacts, outputKeyPrefix)
            flatStoredResults = [
                result
                for artifact in storedArtifacts
                for result in artifact.get("storedResults", [])
                if isinstance(result, dict)
            ]
            primaryArtifact = storedArtifacts[0] if storedArtifacts else {}
            return {
                "taskId": str(primaryArtifact.get("taskId") or ""),
                "storedResults": flatStoredResults,
                "storedArtifacts": storedArtifacts,
                "outputKeyPrefix": outputKeyPrefix,
                "artifactId": str(primaryArtifact.get("artifactId") or ""),
                "assetName": str(primaryArtifact.get("assetName") or ""),
                "targetSectionId": str(primaryArtifact.get("targetSectionId") or ""),
                "targetSectionTitle": str(primaryArtifact.get("targetSectionTitle") or ""),
            }

        if toolName == "compose_web":
            return await self._composeWeb(toolArgs, parsedRequest, state)

        raise AppError(f"不支持的工具：{toolName}", statusCode=422, code="unsupported_tool")

    def _updateStateFromToolResult(self, toolName: str, resultPayload: Any, state: dict[str, Any]) -> None:
        if toolName == "ask_followup":
            state["pendingFollowUp"] = resultPayload
            return

        if toolName == "search_content":
            state["latestSearch"] = resultPayload
            return

        if toolName == "read_reference_images":
            state["latestImageUnderstanding"] = resultPayload
            return

        if toolName == "create_copy":
            state["latestCopyResult"] = resultPayload
            return

        if toolName == "create_image":
            state["latestImageResult"] = resultPayload
            self._upsertImageArtifact(resultPayload, state)
            return

        if toolName == "store_result" and isinstance(resultPayload, dict):
            latestImageResult = state.get("latestImageResult")
            if isinstance(latestImageResult, dict):
                latestImageResult["storedResults"] = resultPayload.get("storedResults", [])
            storedArtifacts = resultPayload.get("storedArtifacts")
            if isinstance(storedArtifacts, list):
                for artifact in storedArtifacts:
                    self._upsertImageArtifact(artifact, state)
            else:
                self._upsertImageArtifact(resultPayload, state)
            return

        if toolName == "compose_web":
            state["latestWebResult"] = resultPayload

    def _normalizeDecision(
        self,
        decision: dict[str, Any],
        parsedRequest: ParsedAgentRequest,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        actionType = str(decision.get("actionType") or "final")
        toolName = str(decision.get("toolName") or "")
        toolArgs = decision.get("toolArgs") if isinstance(decision.get("toolArgs"), dict) else {}
        finalResponse = self._sanitizeFinalResponse(str(decision.get("finalResponse") or ""), state)
        thinking = str(decision.get("thinking") or "")

        if actionType not in {"tool", "final"}:
            return self._fallbackDecision(parsedRequest, state)
        if actionType == "tool" and toolName == "compose_web" and self._looksLikeXiaohongshuRequest(parsedRequest.combinedUserContext or parsedRequest.userInput):
            return self._fallbackDecision(parsedRequest, state)
        if actionType == "tool" and toolName not in {
            "ask_followup",
            "search_content",
            "read_reference_images",
            "create_copy",
            "create_image",
            "store_result",
            "compose_web",
        }:
            return self._fallbackDecision(parsedRequest, state)

        return {
            "thinking": thinking,
            "actionType": actionType,
            "toolName": toolName,
            "toolArgs": toolArgs,
            "finalResponse": finalResponse,
        }

    def _fallbackDecision(self, parsedRequest: ParsedAgentRequest, state: dict[str, Any]) -> dict[str, Any]:
        latestImageResult = state.get("latestImageResult")
        latestCopyResult = state.get("latestCopyResult")
        latestSearch = state.get("latestSearch")
        latestImageUnderstanding = state.get("latestImageUnderstanding")
        latestWebResult = state.get("latestWebResult")
        imageArtifacts = self._getImageArtifacts(state)

        if isinstance(latestWebResult, dict) and latestWebResult.get("html"):
            return {
                "thinking": "图文网页已经排版完成，可以直接向用户交付。",
                "actionType": "final",
                "toolName": "",
                "toolArgs": {},
                "finalResponse": self._fallbackFinalResponse(state),
            }

        if self._looksLikeXiaohongshuRequest(parsedRequest.combinedUserContext or parsedRequest.userInput):
            if self._shouldAskXiaohongshuFollowUp(parsedRequest, state):
                return {
                    "thinking": "小红书图文还缺少几个关键信息，先补齐会更容易做得更像一套完整笔记。",
                    "actionType": "tool",
                    "toolName": "ask_followup",
                    "toolArgs": self._buildFollowUp(parsedRequest),
                    "finalResponse": "",
                }
            if self._shouldReadReferenceImages(parsedRequest, state):
                return {
                    "thinking": "我先看一下参考图里的主体、风格和细节，这样后面的笔记文案和配图会更具体。",
                    "actionType": "tool",
                    "toolName": "read_reference_images",
                    "toolArgs": {
                        "focus": parsedRequest.combinedUserContext or parsedRequest.userInput,
                        "assetUrls": parsedRequest.assetUrls,
                    },
                    "finalResponse": "",
                }
            if not isinstance(latestCopyResult, dict):
                return {
                    "thinking": "我先把这篇小红书的文案整理出来，再按每段内容去配图。",
                    "actionType": "tool",
                    "toolName": "create_copy",
                    "toolArgs": {
                        "brief": parsedRequest.combinedUserContext or parsedRequest.userInput,
                        "tone": "小红书风格，带 emoji，亲切、种草感强、适合社媒发布",
                        "sections": 4,
                    },
                    "finalResponse": "",
                }

            pendingStoreArtifact = self._findPendingStoreArtifact(imageArtifacts)
            if pendingStoreArtifact:
                pendingArtifacts = self._findPendingStoreArtifacts(imageArtifacts)
                return {
                    "thinking": "我先把这一组小红书配图统一转存，保证交付时拿到的是完整图片。",
                    "actionType": "tool",
                    "toolName": "store_result",
                    "toolArgs": {
                        "outputKeyPrefix": parsedRequest.outputKeyPrefix,
                        "artifacts": pendingArtifacts,
                    },
                    "finalResponse": "",
                }

            nextImageSlot = self._findNextImageSlot(latestCopyResult, imageArtifacts)
            if nextImageSlot:
                return {
                    "thinking": "我继续根据文案内容补齐下一张配图，保持整组图片的风格和氛围一致。",
                    "actionType": "tool",
                    "toolName": "create_image",
                    "toolArgs": {
                        "prompt": self._buildImagePromptForSlot(parsedRequest, latestCopyResult, nextImageSlot, latestImageUnderstanding),
                        "aspectRatio": parsedRequest.aspectRatio or "3:4",
                        "assetUrls": parsedRequest.assetUrls,
                        "imageCount": 1,
                        "generationMode": self._inferGenerationMode(parsedRequest.assetUrls),
                        "assetName": nextImageSlot.get("assetName", ""),
                        "targetSectionId": nextImageSlot.get("targetSectionId", ""),
                        "targetSectionTitle": nextImageSlot.get("targetSectionTitle", ""),
                    },
                    "finalResponse": "",
                }

            return {
                "thinking": "小红书文案和配图都已经准备好了，可以直接交付给用户。",
                "actionType": "final",
                "toolName": "",
                "toolArgs": {},
                "finalResponse": self._fallbackFinalResponse(state),
            }

        if self._shouldReadReferenceImages(parsedRequest, state):
            return {
                "thinking": "我先看一下参考图里的主体、风格和细节，这样后面的文案和提示词会更准确。",
                "actionType": "tool",
                "toolName": "read_reference_images",
                "toolArgs": {
                    "focus": parsedRequest.combinedUserContext or parsedRequest.userInput,
                    "assetUrls": parsedRequest.assetUrls,
                },
                "finalResponse": "",
            }

        if self._looksLikeWebRequest(parsedRequest.combinedUserContext or parsedRequest.userInput):
            if self._shouldAskFollowUp(parsedRequest, state):
                return {
                    "thinking": "长图文页面还缺少一些关键信息，先补齐会更容易做得准确。",
                    "actionType": "tool",
                    "toolName": "ask_followup",
                    "toolArgs": self._buildFollowUp(parsedRequest),
                    "finalResponse": "",
                }

            if latestSearch is None and self._looksLikeSearchHelpful(parsedRequest.userInput):
                return {
                    "thinking": "这类图文内容通常需要先补充资料，我先去搜索一些参考信息。",
                    "actionType": "tool",
                    "toolName": "search_content",
                    "toolArgs": {"query": parsedRequest.combinedUserContext or parsedRequest.userInput, "count": self.settings.agUiSearchResultLimit},
                    "finalResponse": "",
                }

            if not isinstance(latestCopyResult, dict):
                return {
                    "thinking": "我先把长图文的标题、段落和内容结构整理出来。",
                    "actionType": "tool",
                    "toolName": "create_copy",
                    "toolArgs": {
                        "brief": parsedRequest.combinedUserContext or parsedRequest.userInput,
                        "sections": 4,
                    },
                    "finalResponse": "",
                }

            pendingStoreArtifact = self._findPendingStoreArtifact(imageArtifacts)
            if pendingStoreArtifact:
                pendingArtifacts = self._findPendingStoreArtifacts(imageArtifacts)
                return {
                    "thinking": "我先把这一批新生成的图片统一转存，后面排版时就能稳定引用全部素材。",
                    "actionType": "tool",
                    "toolName": "store_result",
                    "toolArgs": {
                        "outputKeyPrefix": parsedRequest.outputKeyPrefix,
                        "artifacts": pendingArtifacts,
                    },
                    "finalResponse": "",
                }

            nextImageSlot = self._findNextImageSlot(latestCopyResult, imageArtifacts)
            if nextImageSlot:
                return {
                    "thinking": "我先把下一段内容需要的配图补齐，这样后面排版时每个段落都有对应素材。",
                    "actionType": "tool",
                    "toolName": "create_image",
                    "toolArgs": {
                        "prompt": self._buildImagePromptForSlot(parsedRequest, latestCopyResult, nextImageSlot, latestImageUnderstanding),
                        "aspectRatio": parsedRequest.aspectRatio or self.settings.defaultAspectRatio,
                        "assetUrls": parsedRequest.assetUrls,
                        "imageCount": 1,
                        "generationMode": self._inferGenerationMode(parsedRequest.assetUrls),
                        "assetName": nextImageSlot.get("assetName", ""),
                        "targetSectionId": nextImageSlot.get("targetSectionId", ""),
                        "targetSectionTitle": nextImageSlot.get("targetSectionTitle", ""),
                    },
                    "finalResponse": "",
                }

            return {
                "thinking": "文案和图片都已经准备好了，接下来排成可浏览的图文网页。",
                "actionType": "tool",
                "toolName": "compose_web",
                "toolArgs": {
                    "title": latestCopyResult.get("title") or parsedRequest.userInput[:24],
                    "layoutStyle": "公众号长图文",
                    "copyOutline": latestCopyResult,
                    "imageAssets": imageArtifacts,
                },
                "finalResponse": "",
            }

        if isinstance(latestImageResult, dict):
            if latestImageResult.get("status") == "succeed" and latestImageResult.get("resultUrls") and not latestImageResult.get("storedResults"):
                return {
                    "thinking": "已经生成出图片结果，下一步将结果转存到七牛空间。",
                    "actionType": "tool",
                    "toolName": "store_result",
                    "toolArgs": {
                        "taskId": latestImageResult.get("taskId"),
                        "resultUrls": latestImageResult.get("resultUrls", []),
                        "outputKeyPrefix": parsedRequest.outputKeyPrefix,
                    },
                    "finalResponse": "",
                }
            if latestImageResult.get("storedResults"):
                return {
                    "thinking": "结果已经准备完毕，可以向用户交付。",
                    "actionType": "final",
                    "toolName": "",
                    "toolArgs": {},
                    "finalResponse": self._fallbackFinalResponse(state),
                }

        if self._looksLikeImageRequest(parsedRequest.userInput):
            if self._shouldAskFollowUp(parsedRequest, state):
                return {
                    "thinking": "当前信息还不够完整，先追问几个关键点再继续会更稳妥。",
                    "actionType": "tool",
                    "toolName": "ask_followup",
                    "toolArgs": self._buildFollowUp(parsedRequest),
                    "finalResponse": "",
                }
            return {
                "thinking": "用户明确需要图片结果，优先调用 create_image 工具。",
                "actionType": "tool",
                "toolName": "create_image",
                "toolArgs": {
                    "prompt": self._buildDirectImagePrompt(parsedRequest, latestImageUnderstanding),
                    "aspectRatio": parsedRequest.aspectRatio or self.settings.defaultAspectRatio,
                    "assetUrls": parsedRequest.assetUrls,
                    "imageCount": parsedRequest.imageCount,
                    "generationMode": self._inferGenerationMode(parsedRequest.assetUrls),
                },
                "finalResponse": "",
            }

        if latestSearch is None and self._looksLikeSearchHelpful(parsedRequest.userInput):
            return {
                "thinking": "这个问题可能需要补充资料，先执行文本搜索。",
                "actionType": "tool",
                "toolName": "search_content",
                "toolArgs": {"query": parsedRequest.userInput, "count": self.settings.agUiSearchResultLimit},
                "finalResponse": "",
            }

        return {
            "thinking": "当前信息已足够，直接给出最终答复。",
            "actionType": "final",
            "toolName": "",
            "toolArgs": {},
            "finalResponse": self._fallbackFinalResponse(state, parsedRequest.userInput),
        }

    def _fallbackFinalResponse(self, state: dict[str, Any], userInput: str = "") -> str:
        latestCopyResult = state.get("latestCopyResult")
        if isinstance(latestCopyResult, dict) and latestCopyResult.get("contentType") == "xiaohongshu":
            imageArtifacts = self._getImageArtifacts(state)
            hasDeliveredImages = any(item.get("storedResults") or item.get("resultUrls") for item in imageArtifacts)
            if hasDeliveredImages:
                formattedReply = self._formatXiaohongshuReply(latestCopyResult)
                if formattedReply:
                    return formattedReply
                return "今日分享\n✨ 这篇小红书文案和配图已经准备好了\n📌 你可以继续告诉我想怎么调整"

        latestWebResult = state.get("latestWebResult")
        if isinstance(latestWebResult, dict) and latestWebResult.get("html"):
            return "图文内容已经排版好了，你可以继续告诉我想怎么调整。"

        latestImageResult = state.get("latestImageResult")
        if isinstance(latestImageResult, dict):
            storedResults = latestImageResult.get("storedResults", [])
            if storedResults:
                return "图片已经生成好了，可以继续告诉我你想怎么调整。"
            resultUrls = latestImageResult.get("resultUrls", [])
            if resultUrls:
                return "图片已经生成好了，可以继续告诉我你想怎么调整。"

        latestSearch = state.get("latestSearch")
        if isinstance(latestSearch, dict) and latestSearch.get("results"):
            return "我已经整理好相关参考信息了，你可以继续告诉我下一步想法。"

        return "我已经处理好了，你可以继续告诉我下一步需求。"

    def _sanitizeFinalResponse(self, text: str, state: dict[str, Any]) -> str:
        latestCopyResult = state.get("latestCopyResult")
        if isinstance(latestCopyResult, dict) and latestCopyResult.get("contentType") == "xiaohongshu":
            return self._formatXiaohongshuReply(latestCopyResult, text) or self._fallbackFinalResponse(state)

        normalized = text.strip()
        if not normalized:
            return self._fallbackFinalResponse(state)

        if self._looksLikeHtmlDocument(normalized):
            return self._fallbackFinalResponse(state)

        normalized = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", normalized)
        normalized = re.sub(r"https?://\S+", "", normalized)
        normalized = normalized.replace("点击查看", "").replace("点此查看", "").replace("查看大图", "")
        normalized = re.sub(r"\s+", " ", normalized).strip(" ，。；：\n\t")

        if not normalized:
            return self._fallbackFinalResponse(state)

        if "图片" in normalized or "效果图" in normalized:
            return "图片已经生成好了，可以继续告诉我你想怎么调整。"

        if len(normalized) > 36:
            normalized = f"{normalized[:35].rstrip('，。；： ')}。"
        elif not normalized.endswith(("。", "！", "？")):
            normalized = f"{normalized}。"

        return normalized

    def _formatXiaohongshuReply(self, copyResult: dict[str, Any], preferredText: str = "") -> str:
        title = self._sanitizeXiaohongshuText(str(copyResult.get("title") or ""))
        captionSource = preferredText.strip() or str(copyResult.get("caption") or "")
        caption = self._sanitizeXiaohongshuText(captionSource)

        if title and caption:
            if caption.startswith(title):
                return caption
            return f"{title}\n{caption}"
        if title:
            return title
        if caption:
            return caption
        return ""

    def _sanitizeXiaohongshuText(self, text: str) -> str:
        normalized = text.strip()
        if not normalized:
            return ""

        normalized = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", normalized)
        normalized = re.sub(r"https?://\S+", "", normalized)
        normalized = re.sub(r"```[\s\S]*?```", "", normalized)
        normalized = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", normalized)
        normalized = re.sub(r"(?m)^\s*[-*+]\s+", "", normalized)
        normalized = re.sub(r"(?m)^\s*\d+\.\s+", "", normalized)
        normalized = normalized.replace("**", "").replace("__", "").replace("`", "")
        normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
        return normalized

    def _looksLikeHtmlDocument(self, text: str) -> bool:
        normalized = text.strip()
        if not normalized:
            return False

        if re.search(r"<!doctype html|<html[\s>]|<body[\s>]", normalized, flags=re.IGNORECASE):
            return True

        htmlTagCount = len(
            re.findall(r"<(div|section|article|header|footer|main|style|img|figure|p|h1|h2|h3)\b", normalized, flags=re.IGNORECASE)
        )
        return htmlTagCount >= 3 and "</" in normalized

    def _sanitizeFollowUp(self, followUp: Any, parsedRequest: ParsedAgentRequest) -> dict[str, Any]:
        if not isinstance(followUp, dict):
            return self._buildFollowUp(parsedRequest)

        question = str(followUp.get("question") or "").strip()
        rawOptions = followUp.get("options")
        options = [str(item).strip() for item in rawOptions] if isinstance(rawOptions, list) else []
        options = [item for item in options if item][:4]
        inputPlaceholder = str(followUp.get("inputPlaceholder") or "").strip()

        fallback = self._buildFollowUp(parsedRequest)
        if not question:
            question = fallback["question"]
        if len(options) < 2:
            options = fallback["options"]
        if not inputPlaceholder:
            inputPlaceholder = fallback["inputPlaceholder"]

        return {
            "question": question,
            "options": options,
            "inputPlaceholder": inputPlaceholder,
        }

    def _shouldAskFollowUp(self, parsedRequest: ParsedAgentRequest, state: dict[str, Any]) -> bool:
        if state.get("pendingFollowUp"):
            return False
        if parsedRequest.assetUrls:
            return False
        if sum(1 for item in parsedRequest.conversationHistory if item.get("role") == "user") >= 2:
            return False

        context = (parsedRequest.combinedUserContext or parsedRequest.userInput).strip()
        if self._looksLikeWebRequest(context):
            return False
        if len(context) >= 28:
            return False

        detailKeywords = (
            "法式",
            "现代",
            "北欧",
            "极简",
            "复古",
            "电商",
            "海报",
            "客厅",
            "卧室",
            "banner",
            "主图",
            "暖色",
            "冷色",
            "高级感",
            "明亮",
            "ins",
        )
        detailCount = sum(1 for keyword in detailKeywords if keyword in context)
        return detailCount < 2

    def _buildFollowUp(self, parsedRequest: ParsedAgentRequest) -> dict[str, Any]:
        context = parsedRequest.combinedUserContext or parsedRequest.userInput
        if self._looksLikeXiaohongshuRequest(context):
            return {
                "question": "为了把这套小红书图文做得更像一篇完整笔记，你更想突出哪一类内容？",
                "options": ["产品种草", "经验攻略", "清单推荐", "前后对比"],
                "inputPlaceholder": "也可以补充主题、人群、使用场景，或者你想要的语气和视觉感觉。",
            }

        if any(keyword in context for keyword in ("客厅", "卧室", "餐厅", "空间", "家装", "效果图")):
            return {
                "question": "我先补齐几个关键点，这样效果图会更贴近你的预期。你更偏向哪种风格？",
                "options": ["法式奶油", "现代极简", "原木自然", "轻奢质感"],
                "inputPlaceholder": "也可以补充空间类型、主色调，或者你特别想保留的元素。",
            }

        if any(keyword in context for keyword in ("海报", "主图", "banner", "电商", "详情页")):
            return {
                "question": "为了把画面做得更准一些，你更想突出哪一类信息？",
                "options": ["产品主体", "价格促销", "品牌质感", "节日氛围"],
                "inputPlaceholder": "也可以补充目标人群、使用场景、文案重点或配色方向。",
            }

        return {
            "question": "我还差一点关键信息，先补充一下会更容易做出你想要的效果。你最想强调哪一项？",
            "options": ["整体风格", "画面场景", "主视觉主体", "颜色氛围"],
            "inputPlaceholder": "也可以直接补充一句你的具体要求，比如风格、场景、用途或参考感觉。",
        }

    def _looksLikeImageRequest(self, userInput: str) -> bool:
        keywords = ("生成", "图片", "图", "海报", "主图", "效果图", "banner", "封面", "设计图")
        return any(keyword in userInput for keyword in keywords)

    def _looksLikeXiaohongshuRequest(self, userInput: str) -> bool:
        normalized = userInput.lower()
        keywords = (
            "小红书",
            "种草",
            "笔记",
            "图文笔记",
            "社媒图文",
            "社媒笔记",
            "红书",
            "ins post",
            "instagram post",
        )
        return any(keyword in normalized for keyword in keywords)

    def _looksLikeWebRequest(self, userInput: str) -> bool:
        keywords = ("长图文", "公众号", "推文", "图文排版", "图文长页", "H5", "网页", "web", "落地页", "长图")
        normalized = userInput.lower()
        return any(keyword.lower() in normalized for keyword in keywords)

    def _looksLikeSearchHelpful(self, userInput: str) -> bool:
        keywords = ("搜索", "查", "调研", "参考", "案例", "卖点", "竞品", "风格")
        return any(keyword in userInput for keyword in keywords)

    def _shouldReadReferenceImages(self, parsedRequest: ParsedAgentRequest, state: dict[str, Any]) -> bool:
        if not parsedRequest.assetUrls:
            return False
        if isinstance(state.get("latestImageUnderstanding"), dict):
            return False

        context = parsedRequest.combinedUserContext or parsedRequest.userInput
        keywords = (
            "参考图",
            "原图",
            "产品图",
            "根据图片",
            "保留",
            "构图",
            "视角",
            "材质",
            "细节",
            "根据这张图",
            "基于这张图",
            "按这张图",
            "按参考图",
            "看图",
            "读图",
            "分析图片",
            "分析参考图",
            "主体不变",
            "保持原图",
            "保留原有",
            "延续原图",
            "沿用原图",
            "空间结构",
            "图里的",
            "图中",
        )
        return any(keyword in context for keyword in keywords)

    def _shouldAskXiaohongshuFollowUp(self, parsedRequest: ParsedAgentRequest, state: dict[str, Any]) -> bool:
        if state.get("pendingFollowUp"):
            return False
        if sum(1 for item in parsedRequest.conversationHistory if item.get("role") == "user") >= 2:
            return False

        context = (parsedRequest.combinedUserContext or parsedRequest.userInput).strip()
        if len(context) >= 24:
            return False

        detailKeywords = (
            "产品",
            "品牌",
            "主题",
            "功效",
            "人群",
            "场景",
            "通勤",
            "学生党",
            "租房",
            "护肤",
            "穿搭",
            "探店",
            "清单",
            "攻略",
            "教程",
            "对比",
            "避雷",
            "香薰",
        )
        detailCount = sum(1 for keyword in detailKeywords if keyword in context)
        if detailCount >= 2:
            return False
        if detailCount >= 1 and len(context) >= 14:
            return False
        return True

    def _inferGenerationMode(self, assetUrls: list[str]) -> str:
        if len(assetUrls) >= 2:
            return "multi_image_edit"
        if len(assetUrls) == 1:
            return "image_to_image"
        return "text_to_image"

    def _normalizeGenerationMode(self, generationMode: Any, assetUrls: list[str]) -> GenerationMode:
        if generationMode in {"text_to_image", "image_to_image", "multi_image_edit"}:
            return generationMode
        inferred = self._inferGenerationMode(assetUrls)
        if inferred == "image_to_image":
            return "image_to_image"
        if inferred == "multi_image_edit":
            return "multi_image_edit"
        return "text_to_image"

    async def _createCopy(
        self,
        toolArgs: dict[str, Any],
        parsedRequest: ParsedAgentRequest,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        brief = str(toolArgs.get("brief") or parsedRequest.combinedUserContext or parsedRequest.userInput)
        tone = str(toolArgs.get("tone") or "亲切、可信、有设计感")
        sections = max(2, min(int(toolArgs.get("sections") or 4), 8))
        latestSearch = state.get("latestSearch") if isinstance(state.get("latestSearch"), dict) else {}
        searchSummary = self._summarizeSearchResults(latestSearch.get("results", []) if isinstance(latestSearch, dict) else [])
        imageUnderstanding = state.get("latestImageUnderstanding") if isinstance(state.get("latestImageUnderstanding"), dict) else {}
        imageSummary = self._summarizeImageUnderstanding(imageUnderstanding)
        isXiaohongshu = self._looksLikeXiaohongshuRequest(brief) or self._looksLikeXiaohongshuRequest(tone)

        if isXiaohongshu:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是小红书文案策划助手。"
                        "请先生成一篇适合小红书发布的中文图文文案。"
                        "文案要自然、轻快、可读，带适量 emoji 装饰。"
                        "title 必须是适合直接展示的一行中文标题。"
                        "caption 必须是可以直接发在聊天里的纯文本中文文案，不要带任何 Markdown 标记，不要输出 #、*、-、``` 这类格式符号。"
                        "再把内容拆成多个小节，供后续逐张配图。"
                        "图片需要和文案互相补充。"
                        "imagePrompt 应该明确这一张图想呈现的版式、标题、重点句、正文摘录或清单内容，"
                        "让图里的中文文案与对应段落保持一致，同时整体风格仍然可爱、卡通、二维、高饱和度。"
                        "整体风格需要是二维插画、平面设计、手绘或扁平海报感，不要生成真实摄影风、写实产品照或 3D 渲染感画面，并保持系列感。"
                        "只返回 JSON："
                        '{"contentType":"xiaohongshu","title":"...","caption":"...","summary":"...","layoutStyle":"...","seriesStylePrompt":"...","sections":[{"sectionId":"...","heading":"...","body":"...","imagePrompt":"..."}]}'
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"用户需求：{brief}\n"
                        f"整体语气：{tone}\n"
                        f"段落数量：{sections}\n"
                        f"搜索参考：{searchSummary or '无'}\n"
                        f"参考图理解：{imageSummary or '无'}\n"
                        "请输出适合小红书图文发布的完整文案和分节结构。"
                    ),
                },
            ]
            fallback = self._buildXiaohongshuCopyFallback(brief, sections)
        else:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是图文内容策划助手。"
                        "请为公众号长图文、H5 或图文长页生成结构化文案。"
                        "只返回 JSON："
                        '{"title":"...","subtitle":"...","summary":"...","layoutStyle":"...","heroImagePrompt":"...","sections":[{"sectionId":"...","heading":"...","body":"...","imagePrompt":"..."}]}'
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"用户需求：{brief}\n"
                        f"整体语气：{tone}\n"
                        f"段落数量：{sections}\n"
                        f"搜索参考：{searchSummary or '无'}\n"
                        f"参考图理解：{imageSummary or '无'}\n"
                        "请输出适合中文图文页面的结构化内容。"
                    ),
                },
            ]
            fallback = self._buildCopyFallback(brief, sections)

        try:
            rawContent = await self.designService.maasClient.createChatCompletion(
                messages=messages,
                model=self.settings.qiniuChatModel,
                maxTokens=1200,
                temperature=0.4,
            )
            parsed = json.loads(self._extractJson(rawContent))
            if isXiaohongshu:
                return {
                    "contentType": "xiaohongshu",
                    "title": str(parsed.get("title") or fallback["title"]),
                    "caption": str(parsed.get("caption") or fallback["caption"]),
                    "summary": str(parsed.get("summary") or fallback["summary"]),
                    "layoutStyle": str(parsed.get("layoutStyle") or "小红书图文"),
                    "seriesStylePrompt": str(parsed.get("seriesStylePrompt") or fallback["seriesStylePrompt"]),
                    "sections": self._normalizeCopySections(parsed.get("sections"), fallback["sections"]),
                }
            return {
                "title": str(parsed.get("title") or fallback["title"]),
                "subtitle": str(parsed.get("subtitle") or fallback["subtitle"]),
                "summary": str(parsed.get("summary") or fallback["summary"]),
                "layoutStyle": str(parsed.get("layoutStyle") or "公众号长图文"),
                "heroImagePrompt": str(parsed.get("heroImagePrompt") or fallback["heroImagePrompt"]),
                "sections": self._normalizeCopySections(parsed.get("sections"), fallback["sections"]),
            }
        except Exception:
            return fallback

    async def _readReferenceImages(
        self,
        toolArgs: dict[str, Any],
        parsedRequest: ParsedAgentRequest,
    ) -> dict[str, Any]:
        assetUrls = self._mergeUnique(toolArgs.get("assetUrls", []), parsedRequest.assetUrls)
        if not assetUrls:
            raise AppError("read_reference_images 缺少参考图。", statusCode=422, code="missing_reference_assets")

        focus = str(toolArgs.get("focus") or parsedRequest.combinedUserContext or parsedRequest.userInput).strip()
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "请认真理解这些参考图。"
                    "重点总结主体是什么、画面构图与视角、风格与氛围、颜色材质、应保留的关键元素，"
                    "以及后续写文案或生图提示词时最值得补充的细节。"
                    "只返回 JSON："
                    '{"summary":"...","keyDetails":["..."],"promptHints":["..."],"images":[{"url":"...","subject":"...","composition":"...","style":"...","colors":"...","keepElements":["..."]}]}'
                    f"\n当前任务：{focus or '请围绕当前设计任务理解参考图。'}"
                ),
            }
        ]
        for assetUrl in assetUrls:
            content.append({"type": "image_url", "image_url": {"url": assetUrl}})

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": "你是设计任务里的参考图读图助手。请用简洁准确的中文输出结构化理解结果。",
            },
            {
                "role": "user",
                "content": content,
            },
        ]

        fallback = self._buildImageUnderstandingFallback(assetUrls, focus)
        try:
            rawContent = await self.designService.maasClient.createChatCompletion(
                messages=messages,
                model=self.settings.qiniuVisionModel,
                maxTokens=900,
                temperature=0.2,
            )
            parsed = json.loads(self._extractJson(rawContent))
            return self._normalizeImageUnderstanding(parsed, assetUrls, focus, fallback)
        except Exception:
            return fallback

    async def _composeWeb(
        self,
        toolArgs: dict[str, Any],
        parsedRequest: ParsedAgentRequest,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        rawCopyOutline = toolArgs.get("copyOutline")
        if isinstance(rawCopyOutline, dict):
            copyOutline: dict[str, Any] = rawCopyOutline
        else:
            stateCopyOutline = state.get("latestCopyResult")
            copyOutline = stateCopyOutline if isinstance(stateCopyOutline, dict) else {}

        imageAssets = toolArgs.get("imageAssets")
        if not isinstance(imageAssets, list):
            imageAssets = self._getImageArtifacts(state)

        normalizedImageAssets = self._normalizeImageAssetsForCompose(imageAssets)
        layoutStyle = str(toolArgs.get("layoutStyle") or copyOutline.get("layoutStyle") or "公众号长图文")
        title = str(toolArgs.get("title") or copyOutline.get("title") or "图文内容")

        messages = [
            {
                "role": "system",
                "content": (
                    "你是中文内容页面设计师。"
                    "请输出一个完整可渲染的 HTML 页面，用于公众号长图文、图文长页或 H5 风格内容。"
                    "必须内联样式，排版美观，适合中文阅读。"
                    "图片直接使用给定 URL，不要输出 Markdown。"
                    "只返回 JSON：{\"title\":\"...\",\"summary\":\"...\",\"html\":\"<!doctype html>...\"}"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"用户需求：{parsedRequest.combinedUserContext or parsedRequest.userInput}\n"
                    f"页面风格：{layoutStyle}\n"
                    f"文案结构：{json.dumps(copyOutline, ensure_ascii=False)}\n"
                    f"图片素材：{json.dumps(normalizedImageAssets, ensure_ascii=False)}\n"
                    "请生成适合前端直接预览的完整 HTML。"
                ),
            },
        ]

        fallbackHtml = self._buildWebHtmlFallback(title, copyOutline, normalizedImageAssets, layoutStyle)
        try:
            rawContent = await self.designService.maasClient.createChatCompletion(
                messages=messages,
                model=self.settings.qiniuChatModel,
                maxTokens=2400,
                temperature=0.3,
            )
            parsed = json.loads(self._extractJson(rawContent))
            html = str(parsed.get("html") or "").strip()
            if not self._looksLikeHtmlDocument(html):
                html = fallbackHtml
            return {
                "title": str(parsed.get("title") or title),
                "summary": str(parsed.get("summary") or "图文内容已经整理并排版完成。"),
                "layoutStyle": layoutStyle,
                "html": html,
                "imageAssets": normalizedImageAssets,
            }
        except Exception:
            return {
                "title": title,
                "summary": "图文内容已经整理并排版完成。",
                "layoutStyle": layoutStyle,
                "html": fallbackHtml,
                "imageAssets": normalizedImageAssets,
            }

    def _normalizeCopySections(self, sections: Any, fallbackSections: list[dict[str, str]]) -> list[dict[str, str]]:
        if not isinstance(sections, list):
            return fallbackSections

        normalized: list[dict[str, str]] = []
        for index, item in enumerate(sections):
            if not isinstance(item, dict):
                continue
            sectionId = str(item.get("sectionId") or self._buildSectionId(str(item.get("heading") or ""), index)).strip()
            heading = str(item.get("heading") or "").strip()
            body = str(item.get("body") or "").strip()
            imagePrompt = str(item.get("imagePrompt") or "").strip()
            if heading and body:
                normalized.append(
                    {
                        "sectionId": sectionId or f"section-{index + 1}",
                        "heading": heading,
                        "body": body,
                        "imagePrompt": imagePrompt or f"围绕“{heading}”生成适合图文排版的小节配图，画面清晰，质感统一。",
                    }
                )

        return normalized or fallbackSections

    def _buildCopyFallback(self, brief: str, sections: int) -> dict[str, Any]:
        briefText = brief.strip() or "本次图文内容"
        normalizedSections = [
            {
                "sectionId": "core-highlights",
                "heading": "先看核心亮点",
                "body": f"围绕“{briefText}”提炼最值得用户先看到的重点信息，让读者快速理解价值。",
                "imagePrompt": f"为“{briefText}”生成一张突出核心亮点的头图，适合公众号或长图文开篇，画面简洁、有质感。",
            },
            {
                "sectionId": "detail-expansion",
                "heading": "再看细节展开",
                "body": "补充场景、体验、风格和使用感受，让内容更完整，也更容易形成画面感。",
                "imagePrompt": "生成一张体现使用场景和产品细节的配图，适合中文图文页面中段展示。",
            },
            {
                "sectionId": "closing-cta",
                "heading": "最后给出行动引导",
                "body": "用自然、可信的中文收束整篇内容，引导用户继续了解、咨询或下单。",
                "imagePrompt": "生成一张适合作为结尾收束的氛围配图，整体风格与前文统一。",
            },
        ]
        while len(normalizedSections) < sections:
            normalizedSections.insert(
                -1,
                {
                    "sectionId": f"section-{len(normalizedSections)}",
                    "heading": f"内容补充 {len(normalizedSections)}",
                    "body": "补充一段更具体的卖点、场景或风格说明，让整页节奏更完整。",
                    "imagePrompt": "生成一张辅助说明这一段内容的配图，保持整体视觉统一。",
                },
            )

        return {
            "title": briefText[:24],
            "subtitle": "把关键信息排成一页更好读的中文内容。",
            "summary": "已整理出适合长图文排版的标题、摘要和分节内容。",
            "layoutStyle": "公众号长图文",
            "heroImagePrompt": f"围绕“{briefText}”生成适合中文图文长页使用的头图，版面干净，光线自然，质感高级。",
            "sections": normalizedSections[:sections],
        }

    def _buildXiaohongshuCopyFallback(self, brief: str, sections: int) -> dict[str, Any]:
        briefText = brief.strip() or "这次分享主题"
        normalizedSections = [
            {
                "sectionId": "hook",
                "heading": "先抛出最想分享的点",
                "body": "把最容易吸引人继续看下去的亮点放在前面，语气轻松一点，也更有种草感。",
                "imagePrompt": "围绕开篇亮点生成一张小红书风格图文卡片，二维插画、扁平手绘、海报卡片感、非真实摄影、非写实产品照，可爱、卡通、高饱和度，画面精致、有装饰感，加入能概括这一段内容的中文大标题和一句重点文案。",
            },
            {
                "sectionId": "detail-1",
                "heading": "再讲具体体验",
                "body": "把使用感受、场景、优点或对比写得更具体，让读者更容易共鸣。",
                "imagePrompt": "围绕具体体验生成一张小红书风格图文卡片，二维插画、扁平手绘、海报卡片感、非真实摄影、非写实产品照，可爱、卡通、高饱和度，和前一张保持统一色调与装饰元素，加入对应体验的小标题和 2 到 3 句中文短文案。",
            },
            {
                "sectionId": "detail-2",
                "heading": "补一条更实用的信息",
                "body": "补充一条更实用的提醒、建议或总结，让整篇内容更完整。",
                "imagePrompt": "围绕实用信息生成一张小红书风格图文卡片，二维插画、扁平手绘、海报卡片感、非真实摄影、非写实产品照，可爱、卡通、高饱和度，保持系列感，加入清单式或要点式中文文案。",
            },
            {
                "sectionId": "closing",
                "heading": "最后轻轻收尾",
                "body": "收尾可以更口语一点，也可以留一点互动空间，让整篇笔记更自然。",
                "imagePrompt": "围绕结尾氛围生成一张小红书风格图文卡片，二维插画、扁平手绘、海报卡片感、非真实摄影、非写实产品照，可爱、卡通、高饱和度，画面统一有连续性，加入结尾总结和互动感中文短句。",
            },
        ]
        while len(normalizedSections) < sections:
            normalizedSections.insert(
                -1,
                {
                    "sectionId": f"section-{len(normalizedSections)}",
                    "heading": f"补充内容 {len(normalizedSections)}",
                    "body": "补充一段更适合社媒传播的内容点，让整套图文节奏更完整。",
                    "imagePrompt": "生成一张小红书风格图文卡片，二维插画、扁平手绘、海报卡片感、非真实摄影、非写实产品照，可爱、卡通、高饱和度，风格统一，并加入和这段内容一致的中文文案排版。",
                },
            )

        return {
            "contentType": "xiaohongshu",
            "title": briefText[:24],
            "caption": "✨ 今天想认真分享这件事\n💡 把我最想说的重点都整理成一篇小红书文案\n📌 如果你也喜欢这种感觉，继续告诉我想怎么调整",
            "summary": "已整理出一篇适合小红书发布的文案和分节结构。",
            "layoutStyle": "小红书图文",
            "seriesStylePrompt": "小红书图文卡片风格，二维插画、扁平手绘、平面海报感、非真实摄影、非写实产品照、非 3D 渲染，可爱、卡通、高饱和度，色调统一，装饰元素有连续性，画面精致有氛围，允许加入清晰可读的中文标题、重点句和短段落排版。",
            "sections": normalizedSections[:sections],
        }

    def _getImageArtifacts(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        artifacts = state.get("imageArtifacts")
        if isinstance(artifacts, list):
            return [item for item in artifacts if isinstance(item, dict)]

        latestImageResult = state.get("latestImageResult")
        if isinstance(latestImageResult, dict):
            return [
                {
                    "artifactId": str(latestImageResult.get("artifactId") or self._buildImageArtifactId(
                        str(latestImageResult.get("targetSectionId") or ""),
                        str(latestImageResult.get("assetName") or "配图"),
                        str(latestImageResult.get("taskId") or ""),
                    )),
                    "taskId": str(latestImageResult.get("taskId") or ""),
                    "assetName": str(latestImageResult.get("assetName") or "配图"),
                    "targetSectionId": str(latestImageResult.get("targetSectionId") or ""),
                    "targetSectionTitle": str(latestImageResult.get("targetSectionTitle") or ""),
                    "prompt": str(latestImageResult.get("prompt") or ""),
                    "status": str(latestImageResult.get("status") or ""),
                    "resultUrls": [str(item) for item in latestImageResult.get("resultUrls", []) if item],
                    "storedResults": [item for item in latestImageResult.get("storedResults", []) if isinstance(item, dict)],
                }
            ]
        return []

    def _upsertImageArtifact(self, payload: Any, state: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return

        artifacts = self._getImageArtifacts(state)
        artifactId = str(payload.get("artifactId") or self._buildImageArtifactId(
            str(payload.get("targetSectionId") or ""),
            str(payload.get("assetName") or "配图"),
            str(payload.get("taskId") or ""),
        ))
        artifact = {
            "artifactId": artifactId,
            "taskId": str(payload.get("taskId") or ""),
            "assetName": str(payload.get("assetName") or "配图"),
            "targetSectionId": str(payload.get("targetSectionId") or ""),
            "targetSectionTitle": str(payload.get("targetSectionTitle") or ""),
            "prompt": str(payload.get("prompt") or ""),
            "status": str(payload.get("status") or ""),
            "resultUrls": [str(item) for item in payload.get("resultUrls", []) if item],
            "storedResults": [item for item in payload.get("storedResults", []) if isinstance(item, dict)],
        }

        existingIndex = next((index for index, item in enumerate(artifacts) if item.get("artifactId") == artifactId), -1)
        if existingIndex >= 0:
            existing = artifacts[existingIndex]
            existing.update({key: value for key, value in artifact.items() if value not in ("", [], None)})
        else:
            artifacts.append(artifact)
        state["imageArtifacts"] = artifacts

    def _buildImageArtifactId(self, targetSectionId: str, assetName: str, taskId: str) -> str:
        if targetSectionId:
            return f"section::{targetSectionId}"
        if assetName:
            return f"name::{assetName}"
        return f"task::{taskId}"

    def _findPendingStoreArtifact(self, imageArtifacts: list[dict[str, Any]]) -> dict[str, Any] | None:
        for artifact in imageArtifacts:
            if artifact.get("status") == "succeed" and artifact.get("resultUrls") and not artifact.get("storedResults"):
                return artifact
        return None

    def _findPendingStoreArtifacts(self, imageArtifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pendingArtifacts: list[dict[str, Any]] = []
        for artifact in imageArtifacts:
            if artifact.get("status") == "succeed" and artifact.get("resultUrls") and not artifact.get("storedResults"):
                pendingArtifacts.append(artifact)
        return pendingArtifacts

    def _findNextImageSlot(self, copyOutline: Any, imageArtifacts: list[dict[str, Any]]) -> dict[str, str] | None:
        if not isinstance(copyOutline, dict):
            return None

        coveredSectionIds = {
            str(item.get("targetSectionId") or "")
            for item in imageArtifacts
            if item.get("storedResults") or item.get("resultUrls")
        }

        sections = copyOutline.get("sections")
        if not isinstance(sections, list):
            return None

        for index, item in enumerate(sections):
            if not isinstance(item, dict):
                continue
            sectionId = str(item.get("sectionId") or self._buildSectionId(str(item.get("heading") or ""), index)).strip()
            if sectionId in coveredSectionIds:
                continue
            heading = str(item.get("heading") or f"内容小节 {index + 1}").strip()
            copyTitle = str(copyOutline.get("title") or "").strip()
            if copyOutline.get("contentType") == "xiaohongshu" and copyTitle:
                assetName = f"{copyTitle}-{heading}配图"
            else:
                assetName = f"{heading or f'配图 {index + 1}'}配图"
            return {
                "targetSectionId": sectionId,
                "targetSectionTitle": heading,
                "assetName": assetName,
                "imagePrompt": str(item.get("imagePrompt") or ""),
            }
        return None

    def _normalizeImageAssetsForCompose(self, imageAssets: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in imageAssets:
            if not isinstance(item, dict):
                continue
            storedResults = item.get("storedResults") if isinstance(item.get("storedResults"), list) else []
            resultUrls = item.get("resultUrls") if isinstance(item.get("resultUrls"), list) else []
            chosenUrl = ""
            if storedResults:
                firstStored = next((result for result in storedResults if isinstance(result, dict) and result.get("url")), None)
                if isinstance(firstStored, dict):
                    chosenUrl = str(firstStored.get("url") or "")
            if not chosenUrl and resultUrls:
                chosenUrl = str(resultUrls[0] or "")
            if not chosenUrl:
                continue
            normalized.append(
                {
                    "artifactId": str(item.get("artifactId") or ""),
                    "assetName": str(item.get("assetName") or "配图"),
                    "targetSectionId": str(item.get("targetSectionId") or ""),
                    "targetSectionTitle": str(item.get("targetSectionTitle") or ""),
                    "url": chosenUrl,
                    "prompt": str(item.get("prompt") or ""),
                }
            )
        return normalized

    def _buildImagePromptForSlot(
        self,
        parsedRequest: ParsedAgentRequest,
        copyResult: Any,
        slot: dict[str, str],
        imageUnderstanding: Any = None,
    ) -> str:
        if isinstance(copyResult, dict) and copyResult.get("contentType") == "xiaohongshu":
            sectionTitle = str(slot.get("targetSectionTitle") or "").strip()
            slotPrompt = str(slot.get("imagePrompt") or "").strip()
            sectionBody = ""
            rawSections = copyResult.get("sections")
            if isinstance(rawSections, list):
                matched = next(
                    (
                        item
                        for item in rawSections
                        if isinstance(item, dict) and str(item.get("sectionId") or "") == str(slot.get("targetSectionId") or "")
                    ),
                    None,
                )
                if isinstance(matched, dict):
                    sectionBody = str(matched.get("body") or "").strip()
            textLayoutInstruction = ""
            if sectionTitle or sectionBody:
                textLayoutInstruction = self._mergePromptSegments(
                    f"这张图需要承载和“{sectionTitle or '当前内容'}”一致的中文文案排版",
                    f"图内主标题可概括为：{sectionTitle}" if sectionTitle else "",
                    f"图内正文、重点句或清单内容请围绕这段文案展开：{sectionBody}" if sectionBody else "",
                    "确保图里的文字表达与这一段文案一致，相互补充，不要写和正文无关的新内容",
                    "整张图请保持二维插画、扁平手绘、平面海报卡片感，避免真实摄影风、写实产品照或 3D 渲染感",
                )
            return self._mergePromptSegments(
                slotPrompt or f"围绕“小红书文案中的 {sectionTitle or '当前内容'}”生成一张风格化图文卡片，表达这一段的情绪和内容重点。",
                str(copyResult.get("seriesStylePrompt") or "").strip(),
                textLayoutInstruction,
                self._summarizeImageUnderstanding(imageUnderstanding),
            )

        slotPrompt = str(slot.get("imagePrompt") or "").strip()
        understandingSummary = self._summarizeImageUnderstanding(imageUnderstanding)
        if slotPrompt:
            return self._mergePromptSegments(slotPrompt, understandingSummary)

        if isinstance(copyResult, dict):
            title = str(copyResult.get("title") or "").strip()
            summary = str(copyResult.get("summary") or "").strip()
            sectionTitle = str(slot.get("targetSectionTitle") or "").strip()
            if sectionTitle:
                return self._mergePromptSegments(
                    f"{title}。围绕“小节 {sectionTitle}”生成一张适合图文排版的配图。{summary}".strip("。"),
                    understandingSummary,
                )

        return self._buildImagePromptForWeb(parsedRequest, copyResult, imageUnderstanding)

    def _buildDirectImagePrompt(self, parsedRequest: ParsedAgentRequest, imageUnderstanding: Any = None) -> str:
        return self._mergePromptSegments(
            parsedRequest.combinedUserContext or parsedRequest.userInput,
            self._summarizeImageUnderstanding(imageUnderstanding),
        )

    def _buildSectionId(self, heading: str, index: int) -> str:
        normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", heading.lower()).strip("-")
        return normalized or f"section-{index + 1}"

    def _normalizeImageUnderstanding(
        self,
        payload: Any,
        assetUrls: list[str],
        focus: str,
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return fallback

        rawKeyDetails = payload.get("keyDetails")
        keyDetails = [str(item).strip() for item in rawKeyDetails] if isinstance(rawKeyDetails, list) else []
        keyDetails = [item for item in keyDetails if item][:8]

        rawPromptHints = payload.get("promptHints")
        promptHints = [str(item).strip() for item in rawPromptHints] if isinstance(rawPromptHints, list) else []
        promptHints = [item for item in promptHints if item][:8]

        rawImages = payload.get("images")
        images: list[dict[str, Any]] = []
        if isinstance(rawImages, list):
            for index, item in enumerate(rawImages):
                if not isinstance(item, dict):
                    continue
                rawKeepElements = item.get("keepElements")
                keepElements = [str(element).strip() for element in rawKeepElements] if isinstance(rawKeepElements, list) else []
                keepElements = [element for element in keepElements if element][:6]
                imageUrl = str(item.get("url") or "").strip()
                if not imageUrl and index < len(assetUrls):
                    imageUrl = assetUrls[index]
                images.append(
                    {
                        "url": imageUrl,
                        "subject": str(item.get("subject") or "").strip(),
                        "composition": str(item.get("composition") or "").strip(),
                        "style": str(item.get("style") or "").strip(),
                        "colors": str(item.get("colors") or "").strip(),
                        "keepElements": keepElements,
                    }
                )

        summary = str(payload.get("summary") or "").strip()
        normalized = {
            "focus": focus,
            "summary": summary or fallback["summary"],
            "keyDetails": keyDetails or fallback["keyDetails"],
            "promptHints": promptHints or fallback["promptHints"],
            "images": images or fallback["images"],
        }
        return normalized

    def _buildImageUnderstandingFallback(self, assetUrls: list[str], focus: str) -> dict[str, Any]:
        return {
            "focus": focus,
            "summary": "",
            "keyDetails": [],
            "promptHints": [],
            "images": [
                {
                    "url": assetUrl,
                    "subject": "",
                    "composition": "",
                    "style": "",
                    "colors": "",
                    "keepElements": [],
                }
                for assetUrl in assetUrls
            ],
        }

    def _summarizeImageUnderstanding(self, imageUnderstanding: Any) -> str:
        if not isinstance(imageUnderstanding, dict):
            return ""

        summaryParts: list[str] = []
        summary = str(imageUnderstanding.get("summary") or "").strip()
        if summary:
            summaryParts.append(summary)

        keyDetails = imageUnderstanding.get("keyDetails")
        if isinstance(keyDetails, list):
            normalizedDetails = [str(item).strip() for item in keyDetails if str(item).strip()]
            if normalizedDetails:
                summaryParts.append("关键信息：" + "；".join(normalizedDetails[:4]))

        promptHints = imageUnderstanding.get("promptHints")
        if isinstance(promptHints, list):
            normalizedHints = [str(item).strip() for item in promptHints if str(item).strip()]
            if normalizedHints:
                summaryParts.append("提示词补充：" + "；".join(normalizedHints[:4]))

        return "\n".join(summaryParts)

    def _mergePromptSegments(self, *segments: str) -> str:
        normalizedSegments = [segment.strip("。 \n\t") for segment in segments if segment and segment.strip("。 \n\t")]
        return "。".join(normalizedSegments)

    def _summarizeSearchResults(self, results: Any) -> str:
        if not isinstance(results, list) or not results:
            return ""

        summaryLines: list[str] = []
        for item in results[:4]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            snippet = str(item.get("snippet") or "").strip()
            if title or snippet:
                summaryLines.append(f"{title}：{snippet}".strip("："))
        return "\n".join(summaryLines)

    def _collectImageUrls(self, state: dict[str, Any]) -> list[str]:
        imageUrls: list[str] = []
        latestImageResult = state.get("latestImageResult")
        if isinstance(latestImageResult, dict):
            storedResults = latestImageResult.get("storedResults", [])
            if isinstance(storedResults, list):
                for item in storedResults:
                    if isinstance(item, dict) and item.get("url"):
                        imageUrls.append(str(item["url"]))
            resultUrls = latestImageResult.get("resultUrls", [])
            if isinstance(resultUrls, list):
                for item in resultUrls:
                    if item:
                        imageUrls.append(str(item))

        return self._mergeUnique(imageUrls)

    def _buildImagePromptForWeb(self, parsedRequest: ParsedAgentRequest, copyResult: Any, imageUnderstanding: Any = None) -> str:
        understandingSummary = self._summarizeImageUnderstanding(imageUnderstanding)
        if isinstance(copyResult, dict):
            imagePrompt = str(copyResult.get("heroImagePrompt") or copyResult.get("imagePrompt") or "").strip()
            if imagePrompt:
                return self._mergePromptSegments(imagePrompt, understandingSummary)
            title = str(copyResult.get("title") or "").strip()
            summary = str(copyResult.get("summary") or "").strip()
            if title or summary:
                return self._mergePromptSegments(
                    f"{title}。{summary}。生成适合中文长图文网页排版的头图或配图，画面清晰，风格统一。".strip("。"),
                    understandingSummary,
                )
        return self._mergePromptSegments(
            f"{parsedRequest.combinedUserContext or parsedRequest.userInput}。生成适合长图文或公众号排版使用的配图，画面干净，信息表达清楚。".strip("。"),
            understandingSummary,
        )

    def _buildWebHtmlFallback(
        self,
        title: str,
        copyOutline: dict[str, Any],
        imageAssets: list[dict[str, Any]],
        layoutStyle: str,
    ) -> str:
        safeTitle = escape(title or "图文内容")
        safeSubtitle = escape(str(copyOutline.get("subtitle") or ""))
        safeSummary = escape(str(copyOutline.get("summary") or ""))
        rawSections = copyOutline.get("sections")
        sections: list[Any] = rawSections if isinstance(rawSections, list) else []
        heroImage = imageAssets[0] if imageAssets else None
        imageMap = {str(item.get("targetSectionId") or ""): item for item in imageAssets if item.get("targetSectionId")}
        sectionHtml = []
        for index, item in enumerate(sections):
            if not isinstance(item, dict):
                continue
            sectionId = str(item.get("sectionId") or self._buildSectionId(str(item.get("heading") or ""), index))
            heading = escape(str(item.get("heading") or ""))
            body = escape(str(item.get("body") or ""))
            if not heading and not body:
                continue
            sectionImage = imageMap.get(sectionId)
            imageFragment = ""
            if sectionImage and sectionImage.get("url"):
                imageFragment = (
                    "<figure class='section-visual'>"
                    f"<img src='{escape(str(sectionImage['url']), quote=True)}' alt='{escape(str(sectionImage.get('assetName') or heading or '配图'))}' />"
                    "</figure>"
                )
            sectionHtml.append(
                f"<section class='section'><h2>{heading or '内容小节'}</h2>{imageFragment}<p>{body}</p></section>"
            )

        heroImageHtml = ""
        if heroImage and heroImage.get("url"):
            heroImageHtml = (
                "<figure class='visual'>"
                f"<img src='{escape(str(heroImage['url']), quote=True)}' alt='{escape(str(heroImage.get('assetName') or '头图'))}' />"
                "</figure>"
            )

        bodyHtml = "\n".join(
            [
                "<!doctype html>",
                "<html lang='zh-CN'>",
                "<head>",
                "  <meta charset='UTF-8' />",
                "  <meta name='viewport' content='width=device-width, initial-scale=1.0' />",
                f"  <title>{safeTitle}</title>",
                "  <style>",
                "    :root { color-scheme: light; font-family: 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif; }",
                "    body { margin: 0; background: linear-gradient(180deg, #f7f4ee 0%, #eef3f8 100%); color: #1d2740; }",
                "    .page { max-width: 840px; margin: 0 auto; padding: 48px 20px 72px; }",
                "    .hero { padding: 40px 32px; border-radius: 28px; background: rgba(255,255,255,0.92); box-shadow: 0 24px 60px rgba(28,42,72,0.08); }",
                "    .eyebrow { display: inline-block; padding: 6px 12px; border-radius: 999px; background: rgba(17,126,102,0.12); color: #0f6f59; font-size: 12px; font-weight: 700; letter-spacing: 0.08em; }",
                "    h1 { margin: 18px 0 12px; font-size: 40px; line-height: 1.08; }",
                "    .subtitle { margin: 0; color: #51607a; font-size: 18px; }",
                "    .summary { margin-top: 18px; color: #42506a; font-size: 15px; line-height: 1.8; }",
                "    .visual, .section { margin: 18px 0 0; padding: 22px; border-radius: 24px; background: rgba(255,255,255,0.88); box-shadow: 0 18px 40px rgba(28,42,72,0.06); }",
                "    .visual img { display: block; width: 100%; border-radius: 18px; }",
                "    .section-visual { margin: 0 0 16px; }",
                "    .section-visual img { display: block; width: 100%; border-radius: 18px; }",
                "    .section h2 { margin: 0 0 12px; font-size: 24px; }",
                "    .section p { margin: 0; color: #4c5972; line-height: 1.9; font-size: 16px; white-space: pre-wrap; }",
                "    @media (max-width: 720px) { .page { padding: 24px 14px 48px; } .hero { padding: 28px 20px; border-radius: 22px; } h1 { font-size: 30px; } }",
                "  </style>",
                "</head>",
                "<body>",
                "  <main class='page'>",
                "    <section class='hero'>",
                f"      <span class='eyebrow'>{escape(layoutStyle)}</span>",
                f"      <h1>{safeTitle}</h1>",
                f"      <p class='subtitle'>{safeSubtitle}</p>",
                f"      <p class='summary'>{safeSummary}</p>",
                "    </section>",
                heroImageHtml,
                *sectionHtml,
                "  </main>",
                "</body>",
                "</html>",
            ]
        )
        return bodyHtml

    def _parseAgentInput(self, agentInput: RunAgentInput) -> ParsedAgentRequest:
        latestUserMessage = next((message for message in reversed(agentInput.messages) if message.role == "user"), None)
        if latestUserMessage is None:
            raise AppError("AG-UI 请求缺少用户消息。", statusCode=422, code="missing_user_message")

        userText, assetUrls = self._extractContent(latestUserMessage.content)
        forwardedProps = agentInput.forwardedProps or {}
        state = agentInput.state or {}
        conversationHistory: list[dict[str, str]] = []
        userTexts: list[str] = []

        for message in agentInput.messages[-8:]:
            messageText, _ = self._extractContent(message.content)
            if not messageText.strip():
                continue
            conversationHistory.append({"role": message.role, "content": messageText.strip()})
            if message.role == "user":
                userTexts.append(messageText.strip())

        return ParsedAgentRequest(
            userInput=userText or str(forwardedProps.get("userInput") or state.get("userInput") or ""),
            combinedUserContext="\n".join(userTexts).strip(),
            conversationHistory=conversationHistory,
            assetUrls=self._mergeUnique(assetUrls, forwardedProps.get("assetUrls", []), state.get("assetUrls", [])),
            referenceLinks=self._mergeUnique(forwardedProps.get("referenceLinks", []), state.get("referenceLinks", [])),
            aspectRatio=forwardedProps.get("aspectRatio") or state.get("aspectRatio"),
            imageCount=int(forwardedProps.get("imageCount") or state.get("imageCount") or self.settings.agUiDefaultImageCount),
            autoStoreResult=bool(forwardedProps.get("autoStoreResult", state.get("autoStoreResult", True))),
            outputKeyPrefix=str(forwardedProps.get("outputKeyPrefix") or state.get("outputKeyPrefix") or "generated"),
            taskPollTimeoutSeconds=forwardedProps.get("taskPollTimeoutSeconds") or state.get("taskPollTimeoutSeconds"),
        )

    def _extractContent(self, content: Any) -> tuple[str, list[str]]:
        if isinstance(content, str):
            return content, []
        if not isinstance(content, list):
            return str(content or ""), []

        textParts: list[str] = []
        assetUrls: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                textParts.append(str(part.get("text", "")))
            if part.get("type") == "image_url":
                imageUrl = part.get("image_url")
                if isinstance(imageUrl, dict) and imageUrl.get("url"):
                    assetUrls.append(str(imageUrl["url"]))
                elif isinstance(imageUrl, str):
                    assetUrls.append(imageUrl)
        return "\n".join(part for part in textParts if part).strip(), assetUrls

    def _mergeUnique(self, *groups: Any) -> list[str]:
        merged: list[str] = []
        for group in groups:
            if isinstance(group, str):
                iterable: list[Any] = [group]
            elif isinstance(group, (list, tuple, set)):
                iterable = list(group)
            else:
                continue
            for item in iterable:
                if item and item not in merged:
                    merged.append(str(item))
        return merged

    def _extractJson(self, rawContent: str) -> str:
        try:
            return rawContent[rawContent.index("{") : rawContent.rindex("}") + 1]
        except ValueError:
            return rawContent

    def _event(self, eventType: str, **payload: Any) -> dict[str, Any]:
        return {"type": eventType, **payload}
