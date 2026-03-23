<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref } from "vue";

import { streamAgentRun, uploadReferenceFile } from "@/lib/agui-client";
import type {
  AgUiEvent,
  AgUiMessage,
  AgUiMessagePart,
  ImageToolResult,
  RunAgentPayload,
  SearchResultItem,
  StoredResult,
} from "@/types/agui";

type AppView = "home" | "conversation";
type AssetStatus = "local" | "uploading" | "uploaded" | "error";
type RunStatus = "running" | "completed" | "error";

interface ComposerAsset {
  id: string;
  file: File;
  name: string;
  size: number;
  previewUrl: string;
  status: AssetStatus;
  uploadedUrl: string;
  width?: number;
  height?: number;
  errorMessage: string;
}

interface MessageFeedEntry {
  id: string;
  kind: "message";
  role: "user" | "assistant" | "system";
  content: string;
}

interface AgentFeedEntry {
  id: string;
  kind: "agent";
  category: "status" | "step" | "tool" | "thinking";
  title: string;
  summary: string;
  detail: string;
  status: RunStatus;
  collapsed: boolean;
  toolName?: string;
}

type FeedEntry = MessageFeedEntry | AgentFeedEntry;

interface PreviewImage {
  id: string;
  url: string;
  title: string;
  source: "stored" | "temporary";
}

interface PreviewBatch {
  id: string;
  runId: string;
  taskId: string;
  title: string;
  subtitle: string;
  images: PreviewImage[];
}

const aspectRatioOptions = ["1:1", "4:3", "3:4", "16:9", "9:16", "21:9"];
const defaultPrompt =
  "参考香薰机产品图，生成一张简洁、明亮、有质感的电商主图。";

const view = ref<AppView>("home");
const isRunning = ref(false);
const promptText = ref(defaultPrompt);
const aspectRatio = ref("1:1");
const composerAssets = ref<ComposerAsset[]>([]);
const feedEntries = ref<FeedEntry[]>([]);
const previewBatches = ref<PreviewBatch[]>([]);
const selectedPreviewBatchId = ref("");
const searchResults = ref<SearchResultItem[]>([]);
const threadId = ref("");
const activeRunId = ref("");
const currentThinkingEntryId = ref<string | null>(null);
const activeAssistantMessageId = ref<string | null>(null);
const pendingRequestMessages = ref<AgUiMessage[]>([]);
const runSummary = reactive({
  status: "待开始",
  latestTaskId: "",
  errorMessage: "",
});

let activeAbortController: AbortController | null = null;

const uploadedAssets = computed(() =>
  composerAssets.value.filter((asset) => asset.status === "uploaded"),
);
const hasConversationFeed = computed(() => feedEntries.value.length > 0);
const currentPreviewBatch = computed(() =>
  previewBatches.value.find((batch) => batch.id === selectedPreviewBatchId.value)
    || previewBatches.value[0]
    || null,
);
const visiblePreviewImages = computed(() => currentPreviewBatch.value?.images ?? []);

function createId(prefix: string): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function formatFileSize(size: number): string {
  if (size < 1024 * 1024) {
    return `${Math.max(1, Math.round(size / 102.4) / 10)} KB`;
  }
  return `${Math.round(size / 1024 / 102.4) / 10} MB`;
}

function toolLabel(toolName: string): string {
  if (toolName === "search_content") {
    return "内容搜索";
  }
  if (toolName === "create_image") {
    return "图片创作";
  }
  if (toolName === "store_result") {
    return "结果存储";
  }
  return toolName || "工具调用";
}

function stepLabel(stepName: string): string {
  if (stepName.startsWith("llm_decision_")) {
    return `深度思考 ${stepName.split("_").pop() ?? ""}`.trim();
  }
  return toolLabel(stepName);
}

function generationModeLabel(mode: unknown): string {
  if (mode === "image_to_image") {
    return "参考图生图";
  }
  if (mode === "multi_image_edit") {
    return "多图融合";
  }
  return "文生图";
}

function truncateText(text: string, length = 88): string {
  const normalized = text.trim().replace(/\s+/g, " ");
  if (normalized.length <= length) {
    return normalized;
  }
  return `${normalized.slice(0, length).trim()}...`;
}

function extractLines(value: string): string[] {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function addMessageEntry(
  role: "user" | "assistant" | "system",
  content: string,
  id = createId("message"),
): string {
  feedEntries.value.push({
    id,
    kind: "message",
    role,
    content,
  });
  return id;
}

function upsertAssistantMessage(messageId: string, content: string): void {
  const existing = feedEntries.value.find(
    (entry): entry is MessageFeedEntry =>
      entry.kind === "message" && entry.id === messageId,
  );
  if (existing) {
    existing.content += content;
    return;
  }
  addMessageEntry("assistant", content, messageId);
}

function addAgentEntry(entry: AgentFeedEntry): string {
  feedEntries.value.push(entry);
  return entry.id;
}

function updateAgentEntry(id: string, patch: Partial<AgentFeedEntry>): void {
  const target = feedEntries.value.find(
    (entry): entry is AgentFeedEntry =>
      entry.kind === "agent" && entry.id === id,
  );
  if (!target) {
    return;
  }
  Object.assign(target, patch);
}

function appendAgentDetail(id: string, fragment: string): void {
  const target = feedEntries.value.find(
    (entry): entry is AgentFeedEntry =>
      entry.kind === "agent" && entry.id === id,
  );
  if (!target) {
    return;
  }
  target.detail = `${target.detail}${fragment}`;
}

function findLatestRunningAgent(
  categories?: AgentFeedEntry["category"][],
): AgentFeedEntry | undefined {
  for (let index = feedEntries.value.length - 1; index >= 0; index -= 1) {
    const entry = feedEntries.value[index];
    if (!entry || entry.kind !== "agent") {
      continue;
    }
    if (entry.status !== "running") {
      continue;
    }
    if (categories && !categories.includes(entry.category)) {
      continue;
    }
    return entry;
  }
  return undefined;
}

function parseJson<T>(rawText: unknown): T | null {
  if (typeof rawText !== "string") {
    return null;
  }
  try {
    return JSON.parse(rawText) as T;
  } catch {
    return null;
  }
}

function describeToolArgs(
  toolName: string,
  payload: Record<string, unknown> | null,
  rawText: string,
): {
  summary: string;
  detail: string;
} {
  if (!payload) {
    return {
      summary: "工具参数已准备好。",
      detail: rawText,
    };
  }

  if (toolName === "search_content") {
    const query = String(payload.query ?? "");
    const count = Number(payload.count ?? 5);
    return {
      summary: `准备搜索“${query || "用户需求"}”的相关资料。`,
      detail: `关键词：${query || "未提供"}\n返回数量：最多 ${count} 条`,
    };
  }

  if (toolName === "create_image") {
    const prompt = String(payload.prompt ?? "");
    const imageCount = Number(payload.imageCount ?? 1);
    const assetUrls = Array.isArray(payload.assetUrls) ? payload.assetUrls : [];
    const aspect = String(payload.aspectRatio ?? aspectRatio.value);
    const generationMode = generationModeLabel(payload.generationMode);
    return {
      summary: `开始${generationMode}，输出 ${imageCount} 张 ${aspect} 图片。`,
      detail: `提示词：${prompt || "沿用用户原始需求"}\n参考素材：${assetUrls.length} 张\n输出比例：${aspect}\n生成方式：${generationMode}`,
    };
  }

  if (toolName === "store_result") {
    const resultUrls = Array.isArray(payload.resultUrls)
      ? payload.resultUrls
      : [];
    const outputKeyPrefix = String(payload.outputKeyPrefix ?? "generated");
    return {
      summary: `准备把 ${resultUrls.length || 1} 张生成结果转存到七牛空间。`,
      detail: `任务 ID：${String(payload.taskId ?? "未提供")}\n存储目录：${outputKeyPrefix}`,
    };
  }

  return {
    summary: "工具参数已准备好。",
    detail: rawText,
  };
}

function describeSearchResults(results: SearchResultItem[]): string {
  if (results.length === 0) {
    return "没有返回可用的文本搜索结果。";
  }

  return results
    .slice(0, 3)
    .map((item, index) => {
      const title = item.title || "未命名结果";
      const snippet = truncateText(item.snippet || "暂无摘要", 96);
      return `${index + 1}. ${title}\n${snippet}`;
    })
    .join("\n\n");
}

function describeToolResult(
  toolName: string,
  payload: Record<string, unknown> | null,
  rawText: string,
): {
  summary: string;
  detail: string;
} {
  if (!payload) {
    return {
      summary: "工具已经返回结果。",
      detail: rawText,
    };
  }

  if (toolName === "search_content") {
    const results = Array.isArray(payload.results)
      ? (payload.results as SearchResultItem[])
      : [];
    return {
      summary: `内容搜索已完成，共拿到 ${results.length} 条参考结果。`,
      detail: describeSearchResults(results),
    };
  }

  if (toolName === "create_image") {
    const taskId = String(payload.taskId ?? "");
    const resultUrls = Array.isArray(payload.resultUrls)
      ? payload.resultUrls
      : [];
    const status = String(payload.status ?? "submitted");
    if (resultUrls.length > 0) {
      return {
        summary: `图片创作完成，已拿到 ${resultUrls.length} 张结果。`,
        detail: `任务状态：${status}\n任务 ID：${taskId || "未返回"}\n当前结果：${resultUrls.length} 张，右侧预览区已同步更新。`,
      };
    }
    return {
      summary: "图片任务已提交，正在等待后端返回最终图片。",
      detail: `任务状态：${status}\n任务 ID：${taskId || "未返回"}`,
    };
  }

  if (toolName === "store_result") {
    const storedResults = Array.isArray(payload.storedResults)
      ? (payload.storedResults as StoredResult[])
      : [];
    return {
      summary: `结果存储完成，已转存 ${storedResults.length} 张图片。`,
      detail:
        storedResults.length === 0
          ? "本次没有返回可转存的图片。"
          : storedResults
              .slice(0, 3)
              .map((item, index) => `${index + 1}. ${item.key}`)
              .join("\n"),
    };
  }

  return {
    summary: "工具已经返回结果。",
    detail: rawText,
  };
}

function buildPreviewBatchTitle(index: number): string {
  return `第 ${index + 1} 轮结果`;
}

function buildPreviewBatchSubtitle(imagesCount: number, source: "stored" | "temporary"): string {
  const sourceText = source === "stored" ? "已转存" : "临时结果";
  return `${imagesCount} 张图片 · ${sourceText}`;
}

function extractFileName(path: string): string {
  const normalized = path.trim();
  if (!normalized) {
    return "已转存图片";
  }

  const segments = normalized.split("/");
  return segments[segments.length - 1] || normalized;
}

function setPreviewImages(result: ImageToolResult, runId: string): void {
  const nextImages: PreviewImage[] = [];
  const storedResults = Array.isArray(result.storedResults)
    ? result.storedResults
    : [];
  const temporaryResults = Array.isArray(result.resultUrls)
    ? result.resultUrls
    : [];

  for (const item of storedResults) {
    const stored = item as StoredResult;
    nextImages.push({
      id: createId("stored"),
      url: stored.url,
      title: extractFileName(stored.key),
      source: "stored",
    });
  }

  if (nextImages.length === 0) {
    for (const url of temporaryResults) {
      nextImages.push({
        id: createId("temp"),
        url,
        title: "临时生成结果",
        source: "temporary",
      });
    }
  }

  if (nextImages.length > 0) {
    const batchTaskId = String(result.taskId ?? "");
    const existingIndex = previewBatches.value.findIndex(
      (batch) =>
        (batchTaskId && batch.taskId === batchTaskId) ||
        (!batchTaskId && batch.runId === runId),
    );
    const nextSource = nextImages[0]?.source ?? "temporary";

    if (existingIndex >= 0) {
      const existing = previewBatches.value[existingIndex];
      if (!existing) {
        return;
      }
      existing.images = nextImages;
      existing.subtitle = buildPreviewBatchSubtitle(nextImages.length, nextSource);
      if (batchTaskId) {
        existing.taskId = batchTaskId;
      }
      selectedPreviewBatchId.value = existing.id;
    } else {
      const nextBatch: PreviewBatch = {
        id: createId("preview-batch"),
        runId,
        taskId: batchTaskId,
        title: buildPreviewBatchTitle(previewBatches.value.length),
        subtitle: buildPreviewBatchSubtitle(nextImages.length, nextSource),
        images: nextImages,
      };
      previewBatches.value.push(nextBatch);
      selectedPreviewBatchId.value = nextBatch.id;
    }
  }

  if (result.taskId) {
    runSummary.latestTaskId = result.taskId;
  }
}

function handleToolResult(toolCallId: string, content: unknown): void {
  const timelineEntry = feedEntries.value.find(
    (entry): entry is AgentFeedEntry =>
      entry.kind === "agent" && entry.id === toolCallId,
  );
  if (!timelineEntry) {
    return;
  }

  const rawText = String(content ?? "");
  const parsed = parseJson<Record<string, unknown>>(rawText);
  const description = describeToolResult(
    timelineEntry.toolName || "",
    parsed,
    rawText,
  );
  updateAgentEntry(toolCallId, {
    status: "completed",
    summary: description.summary,
    detail: description.detail,
  });

  if (
    timelineEntry.toolName === "search_content" &&
    parsed &&
    Array.isArray(parsed.results)
  ) {
    searchResults.value = parsed.results as SearchResultItem[];
  }

  if (
    (timelineEntry.toolName === "create_image" ||
      timelineEntry.toolName === "store_result") &&
    parsed
  ) {
    setPreviewImages(parsed as ImageToolResult, activeRunId.value);
  }
}

function handleStateSnapshot(snapshot: unknown): void {
  if (!snapshot || typeof snapshot !== "object") {
    return;
  }

  const state = snapshot as Record<string, unknown>;
  const latestImageResult = state.latestImageResult as
    | ImageToolResult
    | undefined;
  const latestSearch = state.latestSearch as
    | Record<string, unknown>
    | undefined;

  if (latestImageResult) {
    setPreviewImages(latestImageResult, activeRunId.value);
  }

  if (latestSearch && Array.isArray(latestSearch.results)) {
    searchResults.value = latestSearch.results as SearchResultItem[];
  }
}

function buildUserMessageParts(
  userInput: string,
  assetUrls: string[],
): AgUiMessagePart[] {
  const parts: AgUiMessagePart[] = [{ type: "text", text: userInput }];
  for (const assetUrl of assetUrls) {
    parts.push({
      type: "image_url",
      image_url: {
        url: assetUrl,
      },
    });
  }
  return parts;
}

function handleDetailsToggle(entryId: string, event: Event): void {
  const target = event.target as HTMLDetailsElement;
  updateAgentEntry(entryId, { collapsed: !target.open });
}

function resetConversationState(): void {
  activeAbortController?.abort();
  activeAbortController = null;

  for (const asset of composerAssets.value) {
    URL.revokeObjectURL(asset.previewUrl);
  }

  composerAssets.value = [];
  feedEntries.value = [];
  previewBatches.value = [];
  selectedPreviewBatchId.value = "";
  searchResults.value = [];
  pendingRequestMessages.value = [];
  threadId.value = "";
  activeRunId.value = "";
  currentThinkingEntryId.value = null;
  activeAssistantMessageId.value = null;
  isRunning.value = false;
  runSummary.status = "待开始";
  runSummary.latestTaskId = "";
  runSummary.errorMessage = "";
}

function startNewConversation(): void {
  resetConversationState();
  view.value = "home";
  promptText.value = defaultPrompt;
}

function handleFilesSelected(fileList: FileList | null): void {
  if (!fileList || fileList.length === 0) {
    return;
  }

  for (const file of Array.from(fileList)) {
    composerAssets.value.push({
      id: createId("asset"),
      file,
      name: file.name,
      size: file.size,
      previewUrl: URL.createObjectURL(file),
      status: "local",
      uploadedUrl: "",
      errorMessage: "",
    });
  }
}

function removeAsset(assetId: string): void {
  const target = composerAssets.value.find((asset) => asset.id === assetId);
  if (target) {
    URL.revokeObjectURL(target.previewUrl);
  }
  composerAssets.value = composerAssets.value.filter(
    (asset) => asset.id !== assetId,
  );
}

async function ensureUploadedAssets(): Promise<string[]> {
  const urls: string[] = [];

  for (const asset of composerAssets.value) {
    if (asset.status === "uploaded" && asset.uploadedUrl) {
      urls.push(asset.uploadedUrl);
      continue;
    }

    asset.status = "uploading";
    asset.errorMessage = "";

    try {
      const uploadResult = await uploadReferenceFile(asset.file);
      asset.status = "uploaded";
      asset.uploadedUrl = uploadResult.url;
      asset.width = uploadResult.width;
      asset.height = uploadResult.height;
      urls.push(uploadResult.url);
    } catch (error) {
      asset.status = "error";
      asset.errorMessage =
        error instanceof Error ? error.message : "上传素材失败";
      throw error;
    }
  }

  return urls;
}

function handleAgUiEvent(event: AgUiEvent): void {
  if (event.type === "RUN_STARTED") {
    runSummary.status = "运行中";
    addAgentEntry({
      id: createId("status"),
      kind: "agent",
      category: "status",
      title: "任务启动",
      summary: "已连接到 AG-UI 事件流，Agent 开始处理这轮请求。",
      detail: "当前会话已经建立，后续步骤会按线性顺序追加在下方。",
      status: "completed",
      collapsed: true,
    });
    return;
  }

  if (event.type === "STEP_STARTED") {
    const stepName = String(event.stepName ?? "");
    addAgentEntry({
      id: createId(`step-${stepName}`),
      kind: "agent",
      category: "step",
      title: stepLabel(stepName),
      summary: stepName.startsWith("llm_decision_")
        ? "顶层 LLM 正在判断下一步该用什么工具。"
        : `${stepLabel(stepName)}已经开始。`,
      detail: stepName.startsWith("llm_decision_")
        ? "这一轮会先进行判断，再决定是否搜索、生成图片或转存结果。"
        : "",
      status: "running",
      collapsed: true,
    });
    return;
  }

  if (event.type === "STEP_FINISHED") {
    const runningStep = findLatestRunningAgent(["step"]);
    if (runningStep) {
      updateAgentEntry(runningStep.id, {
        status: "completed",
        summary: runningStep.summary || `${runningStep.title}已完成。`,
      });
    }
    currentThinkingEntryId.value = null;
    return;
  }

  if (event.type === "TEXT_MESSAGE_START") {
    const messageId = String(event.messageId ?? "");
    const latestRunningStep = findLatestRunningAgent(["step"]);
    if (latestRunningStep && latestRunningStep.title.startsWith("深度思考")) {
      const thinkingId = createId("thinking");
      currentThinkingEntryId.value = thinkingId;
      addAgentEntry({
        id: thinkingId,
        kind: "agent",
        category: "thinking",
        title: "思考摘要",
        summary: "正在整理本轮决策依据。",
        detail: "",
        status: "running",
        collapsed: true,
      });
      return;
    }

    activeAssistantMessageId.value = messageId;
    upsertAssistantMessage(messageId, "");
    return;
  }

  if (event.type === "TEXT_MESSAGE_CONTENT") {
    const delta = String(event.delta ?? "");
    if (currentThinkingEntryId.value) {
      appendAgentDetail(currentThinkingEntryId.value, delta);
      const lines = extractLines(delta);
      if (lines.length > 0) {
        updateAgentEntry(currentThinkingEntryId.value, {
          summary: truncateText(lines.join(" "), 80),
        });
      }
      return;
    }

    if (activeAssistantMessageId.value) {
      upsertAssistantMessage(activeAssistantMessageId.value, delta);
    }
    return;
  }

  if (event.type === "TEXT_MESSAGE_END") {
    if (currentThinkingEntryId.value) {
      const entry = feedEntries.value.find(
        (item): item is AgentFeedEntry =>
          item.kind === "agent" && item.id === currentThinkingEntryId.value,
      );
      if (entry) {
        updateAgentEntry(entry.id, {
          status: "completed",
          summary: entry.detail
            ? truncateText(entry.detail, 80)
            : "这一轮思考已经完成。",
        });
      }
      currentThinkingEntryId.value = null;
      return;
    }

    activeAssistantMessageId.value = null;
    return;
  }

  if (event.type === "TOOL_CALL_START") {
    const toolCallId = String(event.toolCallId ?? createId("tool"));
    const toolCallName = String(event.toolCallName ?? "");
    addAgentEntry({
      id: toolCallId,
      kind: "agent",
      category: "tool",
      title: toolLabel(toolCallName),
      summary: `${toolLabel(toolCallName)}正在准备执行。`,
      detail: "",
      status: "running",
      collapsed: true,
      toolName: toolCallName,
    });
    return;
  }

  if (event.type === "TOOL_CALL_ARGS") {
    const toolCallId = String(event.toolCallId ?? "");
    const rawText = String(event.delta ?? "");
    const toolEntry = feedEntries.value.find(
      (entry): entry is AgentFeedEntry =>
        entry.kind === "agent" && entry.id === toolCallId,
    );
    if (!toolEntry) {
      return;
    }
    const parsed = parseJson<Record<string, unknown>>(rawText);
    const description = describeToolArgs(
      toolEntry.toolName || "",
      parsed,
      rawText,
    );
    updateAgentEntry(toolCallId, {
      summary: description.summary,
      detail: description.detail,
    });
    return;
  }

  if (event.type === "TOOL_CALL_RESULT") {
    handleToolResult(String(event.toolCallId ?? ""), event.content);
    return;
  }

  if (event.type === "STATE_SNAPSHOT") {
    handleStateSnapshot(event.snapshot);
    return;
  }

  if (event.type === "RUN_ERROR") {
    const message = String(event.message ?? "运行失败");
    runSummary.status = "失败";
    runSummary.errorMessage = message;
    addAgentEntry({
      id: createId("error"),
      kind: "agent",
      category: "status",
      title: "运行失败",
      summary: message,
      detail: "可以检查后端日志、环境变量或接口返回内容后重试。",
      status: "error",
      collapsed: true,
    });
    addMessageEntry("system", message);
    return;
  }

  if (event.type === "RUN_FINISHED") {
    runSummary.status = "已完成";
    return;
  }
}

async function runAgent(userInput: string): Promise<void> {
  const normalizedInput = userInput.trim();
  if (!normalizedInput || isRunning.value) {
    return;
  }

  if (!threadId.value) {
    threadId.value = createId("thread");
  }

  if (view.value === "home") {
    view.value = "conversation";
  }

  runSummary.errorMessage = "";
  runSummary.status = "准备上传素材";

  const uploadedUrls = await ensureUploadedAssets();
  const userMessageParts = buildUserMessageParts(normalizedInput, uploadedUrls);
  const userMessageId = createId("user");

  addMessageEntry("user", normalizedInput, userMessageId);
  pendingRequestMessages.value.push({
    id: userMessageId,
    role: "user",
    content: userMessageParts,
  });

  activeRunId.value = createId("run");
  runSummary.status = "等待 Agent 响应";
  isRunning.value = true;
  activeAbortController = new AbortController();

  const payload: RunAgentPayload = {
    threadId: threadId.value,
    runId: activeRunId.value,
    messages: pendingRequestMessages.value,
    state: {
      aspectRatio: aspectRatio.value,
      assetUrls: uploadedUrls,
    },
    tools: [],
    context: [],
    forwardedProps: {
      aspectRatio: aspectRatio.value,
      assetUrls: uploadedUrls,
      imageCount: 1,
      autoStoreResult: true,
      taskPollTimeoutSeconds: 120,
    },
  };

  try {
    await streamAgentRun(payload, {
      signal: activeAbortController.signal,
      onEvent: handleAgUiEvent,
    });
  } finally {
    activeAbortController = null;
    isRunning.value = false;
  }
}

async function handleSubmit(): Promise<void> {
  try {
    await runAgent(promptText.value);
    promptText.value = "";
  } catch (error) {
    const message = error instanceof Error ? error.message : "调用 AG-UI 失败";
    runSummary.status = "失败";
    runSummary.errorMessage = message;
    addAgentEntry({
      id: createId("submit-error"),
      kind: "agent",
      category: "status",
      title: "请求失败",
      summary: message,
      detail: "当前请求没有成功发给后端，可以检查服务是否启动后再试。",
      status: "error",
      collapsed: true,
    });
  }
}

function stopCurrentRun(): void {
  activeAbortController?.abort();
  activeAbortController = null;
  isRunning.value = false;
  runSummary.status = "已停止";
  addAgentEntry({
    id: createId("stopped"),
    kind: "agent",
    category: "status",
    title: "运行已停止",
    summary: "当前这轮请求被用户主动中断。",
    detail: "可以直接修改提示词后再次发起一轮新的运行。",
    status: "completed",
    collapsed: true,
  });
}

onBeforeUnmount(() => {
  resetConversationState();
});
</script>

<template>
  <div class="app-shell">
    <div class="ambient ambient-left" />
    <div class="ambient ambient-right" />

    <section v-if="view === 'home'" class="home-screen">
      <div class="hero-copy">
        <span class="eyebrow">DesignCraft Agent</span>
        <h1>把你的想法交给我。</h1>
        <p>
          你只需要描述需求、补充参考图，我会帮你整理思路、调用工具、生成图片，并把整个过程清楚地展示出来。
        </p>
      </div>

      <div class="composer-card">
        <div class="composer-toolbar">
          <label class="toolbar-button">
            上传参考图
            <input
              accept="image/*"
              class="hidden-input"
              multiple
              type="file"
              @change="
                handleFilesSelected(($event.target as HTMLInputElement).files)
              "
            />
          </label>

          <div class="ratio-group">
            <button
              v-for="option in aspectRatioOptions"
              :key="option"
              :class="['ratio-pill', { active: option === aspectRatio }]"
              type="button"
              @click="aspectRatio = option"
            >
              {{ option }}
            </button>
          </div>
        </div>

        <div v-if="composerAssets.length > 0" class="asset-grid">
          <article
            v-for="asset in composerAssets"
            :key="asset.id"
            class="asset-card"
          >
            <img
              :alt="asset.name"
              :src="asset.previewUrl"
              class="asset-preview"
            />
            <div class="asset-meta">
              <strong>{{ asset.name }}</strong>
              <span>{{ formatFileSize(asset.size) }}</span>
              <span v-if="asset.width && asset.height"
                >{{ asset.width }} × {{ asset.height }}</span
              >
              <span :class="['asset-status', asset.status]">
                {{
                  asset.status === "uploaded"
                    ? "已上传"
                    : asset.status === "uploading"
                      ? "上传中"
                      : asset.status === "error"
                        ? "上传失败"
                        : "待上传"
                }}
              </span>
              <span v-if="asset.errorMessage" class="asset-error">{{
                asset.errorMessage
              }}</span>
            </div>
            <button
              class="asset-remove"
              type="button"
              @click="removeAsset(asset.id)"
            >
              移除
            </button>
          </article>
        </div>

        <textarea
          v-model="promptText"
          class="prompt-textarea"
          placeholder="描述你要生成的设计图，或直接输入需要 Agent 完成的任务。"
          rows="5"
        />

        <div class="sender-footer">
          <div>
            <strong>当前输出比例：</strong>
            <span>{{ aspectRatio }}</span>
          </div>

          <button
            class="primary-button"
            :disabled="isRunning || !promptText.trim()"
            type="button"
            @click="handleSubmit"
          >
            进入工作区
          </button>
        </div>
      </div>
    </section>

    <section v-else class="conversation-screen">
      <main class="dialog-panel">

        <div class="status-strip">
          <div>
            <strong>{{ runSummary.status }}</strong>
          </div>
          <span v-if="runSummary.latestTaskId"
            >任务 ID：{{ runSummary.latestTaskId }}</span
          >
          <span v-if="runSummary.errorMessage" class="error-text">{{
            runSummary.errorMessage
          }}</span>
        </div>

        <div class="dialog-body">
          <div v-if="hasConversationFeed" class="feed-list">
            <template v-for="entry in feedEntries" :key="entry.id">
              <article
                v-if="entry.kind === 'message'"
                :class="['message-bubble', entry.role]"
              >
                <span class="message-role">
                  {{
                    entry.role === "assistant"
                      ? "Agent"
                      : entry.role === "system"
                        ? "系统"
                        : "你"
                  }}
                </span>
                <p>{{ entry.content }}</p>
              </article>

              <details
                v-else
                class="agent-entry"
                :open="!entry.collapsed"
                @toggle="handleDetailsToggle(entry.id, $event)"
              >
                <summary class="agent-summary">
                  <span :class="['agent-dot', entry.status]" />
                  <div class="agent-copy">
                    <p>
                      <b>{{ entry.title }}</b
                      >：{{ entry.summary }}
                    </p>
                  </div>
                  <span :class="['agent-badge', entry.status]">
                    {{
                      entry.status === "running"
                        ? "进行中"
                        : entry.status === "error"
                          ? "失败"
                          : "已完成"
                    }}
                  </span>
                </summary>
                <div class="agent-detail" v-if="entry.detail">
                  <p>{{ entry.detail }}</p>
                </div>
              </details>
            </template>
          </div>
          <p v-else class="empty-text">
            发送第一条消息后，左侧会按时间顺序串起对话、决策和工具调用。
          </p>
        </div>

        <div class="composer-card conversation-composer">
          <textarea
            v-model="promptText"
            class="prompt-textarea compact"
            placeholder="继续追问，或者换一个提示词重新生成。"
            rows="4"
          />

          <div class="sender-footer conversation-actions">
            <label class="toolbar-button">
              追加参考图
              <input
                accept="image/*"
                class="hidden-input"
                multiple
                type="file"
                @change="
                  handleFilesSelected(($event.target as HTMLInputElement).files)
                "
              />
            </label>
            <label class="ratio-select">
              <span>图片比例</span>
              <select v-model="aspectRatio" class="ratio-select-input">
                <option v-for="option in aspectRatioOptions" :key="option" :value="option">
                  {{ option }}
                </option>
              </select>
            </label>
            <button
              class="primary-button"
              :disabled="isRunning || !promptText.trim()"
              type="button"
              @click="handleSubmit"
            >
              {{ isRunning ? "运行中…" : "发送消息" }}
            </button>
          </div>
        </div>
      </main>

      <aside class="inspector-panel">
        <section class="preview-card">
          <header class="card-header">
            <h3>结果预览</h3>
            <span>{{ visiblePreviewImages.length }} 张图片</span>
          </header>

          <div v-if="previewBatches.length > 0" class="preview-history">
            <button
              v-for="batch in [...previewBatches].reverse()"
              :key="batch.id"
              :class="['history-pill', { active: batch.id === currentPreviewBatch?.id }]"
              type="button"
              @click="selectedPreviewBatchId = batch.id"
            >
              <strong>{{ batch.title }}</strong>
              <span>{{ batch.subtitle }}</span>
            </button>
          </div>

          <div v-if="currentPreviewBatch" class="preview-batch-meta">
            <strong>{{ currentPreviewBatch.title }}</strong>
            <span>{{ currentPreviewBatch.subtitle }}</span>
          </div>

          <div v-if="visiblePreviewImages.length > 0" class="preview-grid">
            <a
              v-for="image in visiblePreviewImages"
              :key="image.id"
              :href="image.url"
              class="preview-item"
              rel="noreferrer"
              target="_blank"
            >
              <img :alt="image.title" :src="image.url" />
              <div>
                <strong>{{ image.title }}</strong>
                <span>{{
                  image.source === "stored" ? "已转存" : "临时结果"
                }}</span>
              </div>
            </a>
          </div>
          <p v-else class="empty-text">
            当图片生成或转存完成后，这里会自动出现预览。
          </p>
        </section>

        <section class="reference-card">
          <header class="card-header">
            <h3>参考素材</h3>
            <span>{{ uploadedAssets.length }} 个已上传素材</span>
          </header>

          <div v-if="composerAssets.length > 0" class="asset-grid compact">
            <article
              v-for="asset in composerAssets"
              :key="asset.id"
              class="asset-card"
            >
              <img
                :alt="asset.name"
                :src="asset.previewUrl"
                class="asset-preview"
              />
              <div class="asset-meta">
                <strong>{{ asset.name }}</strong>
                <span>{{ formatFileSize(asset.size) }}</span>
                <span :class="['asset-status', asset.status]">
                  {{
                    asset.status === "uploaded"
                      ? "已上传"
                      : asset.status === "uploading"
                        ? "上传中"
                        : asset.status === "error"
                          ? "上传失败"
                          : "待上传"
                  }}
                </span>
              </div>
            </article>
          </div>
          <p v-else class="empty-text">还没有参考图，可以在左下角继续补充。</p>
        </section>

        <section class="search-card">
          <header class="card-header">
            <h3>搜索参考</h3>
            <span>{{ searchResults.length }} 条</span>
          </header>

          <div v-if="searchResults.length > 0" class="search-list">
            <a
              v-for="item in searchResults"
              :key="`${item.link}-${item.title}`"
              :href="item.link || '#'"
              class="search-item"
              rel="noreferrer"
              target="_blank"
            >
              <strong>{{ item.title || "未命名结果" }}</strong>
              <span>{{ item.source || item.link }}</span>
              <p>{{ item.snippet || "暂无摘要" }}</p>
            </a>
          </div>
          <p v-else class="empty-text">
            如果 Agent 调用了内容搜索，这里会同步展示可点击的外部参考。
          </p>
        </section>
      </aside>
    </section>
  </div>
</template>
