from __future__ import annotations

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
        self.maxToolIterations = 6

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
            "latestImageResult": None,
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
                "name": "create_image",
                "description": "当用户需要产出图片时使用。你需要给出最终生图 prompt，可选 aspectRatio、assetUrls、generationMode、imageCount。",
                "args": {
                    "prompt": "string，最终生图提示词",
                    "aspectRatio": "string，可选",
                    "assetUrls": "string[]，可选",
                    "generationMode": "text_to_image | image_to_image | multi_image_edit，可选",
                    "imageCount": "integer，可选",
                },
            },
            {
                "name": "store_result",
                "description": "当 create_image 已经生成出临时图片，需要转存到七牛空间交付时使用。",
                "args": {
                    "taskId": "string，create_image 返回的 taskId",
                    "resultUrls": "string[]，create_image 返回的临时结果 URL",
                    "outputKeyPrefix": "string，可选",
                },
            },
        ]

        messages = [
            {
                "role": "system",
                "content": (
                    "你是 DesignCraft 的顶层 Agent。"
                    "你可以自由决定是否调用工具，也可以直接给出最终答复。"
                    "你的唯一可用工具只有：ask_followup、search_content、create_image、store_result。"
                    "请基于当前上下文选择最合适的一步。"
                    "如果用户只是咨询建议，可直接 final。"
                    "如果用户要图片，通常需要 create_image。"
                    "如果已经拿到 resultUrls 且还没有 storedResults，通常需要 store_result。"
                    "如果用户信息明显不足，暂时无法稳定交付高质量结果，应优先调用 ask_followup。"
                    "ask_followup 的 question、options、inputPlaceholder 都必须是亲切自然的中文。"
                    "`thinking` 和 `finalResponse` 必须始终使用自然、亲切、简洁的中文，不要返回英文，也不要中英夹杂。"
                    "即使是思考摘要，也请直接写给用户可读的中文短句。"
                    "除非用户明确要求英文，否则不要输出英文。"
                    "`finalResponse` 必须是一句简短中文，不要使用 Markdown 超链接、不要输出 URL、不要写“点击查看”。"
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

        if toolName == "create_image":
            assetUrls = self._mergeUnique(toolArgs.get("assetUrls", []), parsedRequest.assetUrls)
            aspectRatio = toolArgs.get("aspectRatio") or parsedRequest.aspectRatio or self.settings.defaultAspectRatio
            imageCount = int(toolArgs.get("imageCount") or parsedRequest.imageCount or self.settings.defaultImageCount)
            generationMode = self._normalizeGenerationMode(toolArgs.get("generationMode"), assetUrls)
            prompt = str(toolArgs.get("prompt") or parsedRequest.combinedUserContext or parsedRequest.userInput)

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
            return resultPayload

        if toolName == "store_result":
            latestImageResult = state.get("latestImageResult") or {}
            taskId = str(toolArgs.get("taskId") or latestImageResult.get("taskId") or "")
            resultUrls = toolArgs.get("resultUrls") or latestImageResult.get("resultUrls") or []
            outputKeyPrefix = str(toolArgs.get("outputKeyPrefix") or parsedRequest.outputKeyPrefix)
            if not taskId or not resultUrls:
                raise AppError("store_result 缺少 taskId 或 resultUrls。", statusCode=422, code="invalid_store_result_args")
            storedResults = await self.designService.storeGeneratedResults(taskId, resultUrls, outputKeyPrefix)
            return {
                "taskId": taskId,
                "storedResults": [item.model_dump() for item in storedResults],
                "outputKeyPrefix": outputKeyPrefix,
            }

        raise AppError(f"不支持的工具：{toolName}", statusCode=422, code="unsupported_tool")

    def _updateStateFromToolResult(self, toolName: str, resultPayload: Any, state: dict[str, Any]) -> None:
        if toolName == "ask_followup":
            state["pendingFollowUp"] = resultPayload
            return

        if toolName == "search_content":
            state["latestSearch"] = resultPayload
            return

        if toolName == "create_image":
            state["latestImageResult"] = resultPayload
            return

        if toolName == "store_result" and isinstance(resultPayload, dict):
            latestImageResult = state.get("latestImageResult")
            if isinstance(latestImageResult, dict):
                latestImageResult["storedResults"] = resultPayload.get("storedResults", [])

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

        if actionType == "tool" and toolName not in {"ask_followup", "search_content", "create_image", "store_result"}:
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
        latestSearch = state.get("latestSearch")

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
                    "prompt": parsedRequest.combinedUserContext or parsedRequest.userInput,
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
        normalized = text.strip()
        if not normalized:
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

    def _looksLikeSearchHelpful(self, userInput: str) -> bool:
        keywords = ("搜索", "查", "调研", "参考", "案例", "卖点", "竞品", "风格")
        return any(keyword in userInput for keyword in keywords)

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

    def _mergeUnique(self, *groups: list[str]) -> list[str]:
        merged: list[str] = []
        for group in groups:
            for item in group:
                if item and item not in merged:
                    merged.append(item)
        return merged

    def _extractJson(self, rawContent: str) -> str:
        try:
            return rawContent[rawContent.index("{") : rawContent.rindex("}") + 1]
        except ValueError:
            return rawContent

    def _event(self, eventType: str, **payload: Any) -> dict[str, Any]:
        return {"type": eventType, **payload}
