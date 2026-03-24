<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref } from "vue";

import { streamAgentRun, uploadReferenceFile } from "@/lib/agui-client";
import type {
  AgUiEvent,
  AgUiMessage,
  AgUiMessagePart,
  FollowUpRequest,
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
  originUrl: string;
}

interface PreviewBatch {
  id: string;
  roundId: string;
  title: string;
  subtitle: string;
  images: PreviewImage[];
}

interface WebPreviewDocument {
  id: string;
  title: string;
  source: "html" | "url";
  html?: string;
  url?: string;
}

interface WebPreviewBatch {
  id: string;
  roundId: string;
  title: string;
  subtitle: string;
  documents: WebPreviewDocument[];
}

interface FollowUpCardState {
  request: FollowUpRequest;
  selectedOption: string;
  inputText: string;
  active: boolean;
  submittedAnswer: string;
}

const aspectRatioOptions = ["1:1", "4:3", "3:4", "16:9", "9:16", "21:9"];
const defaultPrompt =
  "基于提供的客厅毛坯房原图，生成法式风格装修效果图，输出分辨率与参考图一致，构图 1:1 还原参考图视角";

const view = ref<AppView>("home");
const isRunning = ref(false);
const promptText = ref(defaultPrompt);
const aspectRatio = ref("");
const composerAssets = ref<ComposerAsset[]>([]);
const feedEntries = ref<FeedEntry[]>([]);
const previewBatches = ref<PreviewBatch[]>([]);
const selectedPreviewBatchId = ref("");
const webPreviewBatches = ref<WebPreviewBatch[]>([]);
const selectedWebPreviewBatchId = ref("");
const previewMode = ref<"image" | "web">("image");
const searchResults = ref<SearchResultItem[]>([]);
const threadId = ref("");
const activeRunId = ref("");
const activePreviewRoundId = ref("");
const currentThinkingEntryId = ref<string | null>(null);
const activeAssistantMessageId = ref<string | null>(null);
const pendingRequestMessages = ref<AgUiMessage[]>([]);
const followUpCards = reactive<Record<string, FollowUpCardState>>({});
const pendingToolNames = reactive<Record<string, string>>({});
const copiedWebDocumentId = ref("");
const savingPdfDocumentId = ref("");
const runSummary = reactive({
  status: "待开始",
  latestTaskId: "",
  errorMessage: "",
});

let activeAbortController: AbortController | null = null;
let copyFeedbackTimer: number | null = null;
let pdfFeedbackTimer: number | null = null;

const uploadedAssets = computed(() =>
  composerAssets.value.filter((asset) => asset.status === "uploaded"),
);
const hasReferenceAssets = computed(() => composerAssets.value.length > 0);
const hasSearchReferences = computed(() => searchResults.value.length > 0);
const hasContextPanel = computed(() => hasReferenceAssets.value || hasSearchReferences.value);
const hasConversationFeed = computed(() => feedEntries.value.length > 0);
const currentPreviewBatch = computed(() =>
  previewBatches.value.find((batch) => batch.id === selectedPreviewBatchId.value)
    || previewBatches.value[0]
    || null,
);
const currentWebPreviewBatch = computed(() =>
  webPreviewBatches.value.find((batch) => batch.id === selectedWebPreviewBatchId.value)
    || webPreviewBatches.value[0]
    || null,
);
const visiblePreviewImages = computed(() => currentPreviewBatch.value?.images ?? []);
const visibleWebDocuments = computed(() => currentWebPreviewBatch.value?.documents ?? []);
const canSubmitPrompt = computed(() => Boolean(promptText.value.trim()));

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
  if (toolName === "ask_followup") {
    return "向你追问";
  }
  if (toolName === "search_content") {
    return "内容搜索";
  }
  if (toolName === "create_copy") {
    return "文案生成";
  }
  if (toolName === "create_image") {
    return "图片创作";
  }
  if (toolName === "store_result") {
    return "结果存储";
  }
  if (toolName === "compose_web") {
    return "图文排版";
  }
  return toolName || "工具调用";
}

function stepLabel(stepName: string): string {
  if (stepName.startsWith("llm_decision_")) {
    return "深度思考";
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

function ensureAssistantContextMessage(messageId: string, content: string): void {
  const existing = pendingRequestMessages.value.find((message) => message.id === messageId);
  if (existing) {
    existing.content = content;
    return;
  }

  pendingRequestMessages.value.push({
    id: messageId,
    role: "assistant",
    content,
  });
}

function getFollowUpState(entryId: string): FollowUpCardState | null {
  return followUpCards[entryId] ?? null;
}

function setFollowUpOption(entryId: string, option: string): void {
  const followUpState = getFollowUpState(entryId);
  if (!followUpState || !followUpState.active) {
    return;
  }
  followUpState.selectedOption = option;
}

function setFollowUpInput(entryId: string, value: string): void {
  const followUpState = getFollowUpState(entryId);
  if (!followUpState || !followUpState.active) {
    return;
  }
  followUpState.inputText = value;
}

function canSubmitFollowUp(entryId: string): boolean {
  const followUpState = getFollowUpState(entryId);
  if (!followUpState || !followUpState.active || isRunning.value) {
    return false;
  }
  return Boolean(followUpState.selectedOption.trim() || followUpState.inputText.trim());
}

function shouldDeferToolEntry(toolName: string): boolean {
  return (
    toolName === "ask_followup"
    || toolName === "create_copy"
    || toolName === "create_image"
    || toolName === "compose_web"
  );
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

  if (toolName === "create_copy") {
    const brief = String(payload.brief ?? "");
    const tone = String(payload.tone ?? "亲切、可信、有设计感");
    const sections = Number(payload.sections ?? 4);
    return {
      summary: `正在整理图文内容结构，预计产出 ${sections} 个内容分节。`,
      detail: `内容方向：${brief || "沿用用户当前需求"}\n文案语气：${tone}`,
    };
  }

  if (toolName === "ask_followup") {
    const question = String(payload.question ?? "");
    const options = Array.isArray(payload.options) ? payload.options : [];
    return {
      summary: "还差一点关键信息，先和你确认一下。",
      detail: `${question || "请补充更多信息。"}\n可选项：${options.join(" / ") || "无"}`,
    };
  }

  if (toolName === "create_image") {
    const prompt = String(payload.prompt ?? "");
    const imageCount = Number(payload.imageCount ?? 1);
    const assetUrls = Array.isArray(payload.assetUrls) ? payload.assetUrls : [];
    const aspect = String(payload.aspectRatio ?? aspectRatio.value ?? "由 Agent 自行判断");
    const generationMode = generationModeLabel(payload.generationMode);
    return {
      summary: `${generationMode}进行中，预计输出 ${imageCount} 张 ${aspect} 图片。`,
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

  if (toolName === "compose_web") {
    const title = String(payload.title ?? "图文内容");
    const layoutStyle = String(payload.layoutStyle ?? "公众号长图文");
    const imageUrls = Array.isArray(payload.imageUrls) ? payload.imageUrls : [];
    return {
      summary: `正在把文案和图片排成 ${layoutStyle}。`,
      detail: `页面标题：${title}\n参与排版的图片：${imageUrls.length} 张`,
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

  if (toolName === "ask_followup") {
    const question = String(payload.question ?? "");
    const options = Array.isArray(payload.options) ? payload.options : [];
    return {
      summary: "还差一点关键信息，等你补充后我继续处理。",
      detail: `${question || "请补充更多信息。"}\n可选项：${options.join(" / ") || "无"}`,
    };
  }

  if (toolName === "create_copy") {
    const sections = Array.isArray(payload.sections)
      ? payload.sections as Array<Record<string, unknown>>
      : [];
    return {
      summary: `文案结构已经整理好了，共 ${sections.length} 个内容分节。`,
      detail:
        sections.length === 0
          ? String(payload.summary ?? "已经拿到一版图文内容结构。")
          : sections
              .slice(0, 3)
              .map((item, index) => `${index + 1}. ${String(item.heading ?? "内容小节")}`)
              .join("\n"),
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

  if (toolName === "compose_web") {
    const title = String(payload.title ?? "图文内容");
    const summary = String(payload.summary ?? "图文页面已经排版完成。");
    return {
      summary: "图文页面已经排版完成，右侧浏览区已同步更新。",
      detail: `页面标题：${title}\n${summary}`,
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

function buildPreviewBatchSubtitle(images: PreviewImage[]): string {
  const storedCount = images.filter((image) => image.source === "stored").length;
  if (storedCount === images.length) {
    return `${images.length} 张图片 · 已转存`;
  }
  if (storedCount === 0) {
    return `${images.length} 张图片 · 临时结果`;
  }
  return `${images.length} 张图片 · 部分已转存`;
}

function buildWebBatchSubtitle(documentsCount: number, source: "html" | "url"): string {
  const sourceText = source === "html" ? "即时预览" : "网页链接";
  return `${documentsCount} 个页面 · ${sourceText}`;
}

function extractFileName(path: string): string {
  const normalized = path.trim();
  if (!normalized) {
    return "已转存图片";
  }

  const segments = normalized.split("/");
  return segments[segments.length - 1] || normalized;
}

function isLikelyUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

function looksLikeHtml(value: string): boolean {
  const normalized = value.trim();
  if (!normalized) {
    return false;
  }

  if (/^<!doctype html/i.test(normalized) || /<html[\s>]/i.test(normalized)) {
    return true;
  }

  const htmlTagCount = (normalized.match(/<(div|section|article|header|footer|main|style|body|img|p|h1|h2|h3)\b/gi) || []).length;
  return htmlTagCount >= 2 && normalized.includes("</");
}

function extractHtmlFromText(value: string): string | null {
  const fencedMatch = value.match(/```(?:html)?\s*([\s\S]*?)```/i);
  if (fencedMatch && looksLikeHtml(fencedMatch[1] || "")) {
    return fencedMatch[1]!.trim();
  }

  if (looksLikeHtml(value)) {
    return value.trim();
  }

  return null;
}

function buildWebPreviewTitle(document: WebPreviewDocument, index: number): string {
  if (document.title.trim()) {
    return document.title.trim();
  }
  if (document.url) {
    return extractFileName(new URL(document.url).pathname) || `页面 ${index + 1}`;
  }
  return `页面 ${index + 1}`;
}

function normalizeWebPreviewDocument(
  document: Partial<WebPreviewDocument>,
  index: number,
): WebPreviewDocument | null {
  const html = typeof document.html === "string" ? document.html.trim() : "";
  const url = typeof document.url === "string" ? document.url.trim() : "";
  if (!html && !url) {
    return null;
  }

  return {
    id: createId("web-doc"),
    title: buildWebPreviewTitle(
      {
        id: "",
        title: typeof document.title === "string" ? document.title : "",
        source: html ? "html" : "url",
        html,
        url,
      },
      index,
    ),
    source: html ? "html" : "url",
    html: html || undefined,
    url: url || undefined,
  };
}

function getActivePreviewRoundId(): string {
  return activePreviewRoundId.value || activeRunId.value || threadId.value || createId("preview-round");
}

function addPreviewImageAsReference(image: PreviewImage): void {
  const alreadyExists = composerAssets.value.some(
    (asset) => asset.uploadedUrl === image.url || asset.previewUrl === image.url,
  );
  if (alreadyExists) {
    return;
  }

  composerAssets.value.push({
    id: createId("asset"),
    file: new File([], image.title || "参考图.png", { type: "image/png" }),
    name: image.title || "参考图.png",
    size: 0,
    previewUrl: image.url,
    status: "uploaded",
    uploadedUrl: image.url,
    errorMessage: "",
  });
}

async function writeTextToClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "true");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  document.body.removeChild(textarea);
}

async function copyWebDocumentCode(documentPreview: WebPreviewDocument): Promise<void> {
  const html = documentPreview.html?.trim();
  if (!html) {
    return;
  }

  await writeTextToClipboard(html);
  copiedWebDocumentId.value = documentPreview.id;
  if (copyFeedbackTimer) {
    window.clearTimeout(copyFeedbackTimer);
  }
  copyFeedbackTimer = window.setTimeout(() => {
    if (copiedWebDocumentId.value === documentPreview.id) {
      copiedWebDocumentId.value = "";
    }
  }, 1800);
}

async function saveWebDocumentAsPdf(documentPreview: WebPreviewDocument): Promise<void> {
  savingPdfDocumentId.value = documentPreview.id;
  if (pdfFeedbackTimer) {
    window.clearTimeout(pdfFeedbackTimer);
  }

  const printFrame = document.createElement("iframe");
  printFrame.style.position = "fixed";
  printFrame.style.right = "0";
  printFrame.style.bottom = "0";
  printFrame.style.width = "0";
  printFrame.style.height = "0";
  printFrame.style.border = "0";
  printFrame.style.opacity = "0";

  const cleanup = (): void => {
    if (printFrame.parentNode) {
      printFrame.parentNode.removeChild(printFrame);
    }
    pdfFeedbackTimer = window.setTimeout(() => {
      if (savingPdfDocumentId.value === documentPreview.id) {
        savingPdfDocumentId.value = "";
      }
    }, 1200);
  };

  await new Promise<void>((resolve) => {
    printFrame.onload = () => {
      window.setTimeout(() => {
        try {
          printFrame.contentWindow?.focus();
          printFrame.contentWindow?.print();
        } finally {
          cleanup();
          resolve();
        }
      }, 180);
    };

    if (documentPreview.html) {
      printFrame.srcdoc = documentPreview.html;
    } else if (documentPreview.url) {
      printFrame.src = documentPreview.url;
    } else {
      cleanup();
      resolve();
      return;
    }

    document.body.appendChild(printFrame);
  });
}

function collectWebPreviewDocuments(payload: unknown): WebPreviewDocument[] {
  const documents: WebPreviewDocument[] = [];

  function visit(value: unknown, depth = 0): void {
    if (depth > 3) {
      return;
    }

    if (typeof value === "string") {
      const html = extractHtmlFromText(value);
      if (html) {
        const document = normalizeWebPreviewDocument({ html }, documents.length);
        if (document) {
          documents.push(document);
        }
      }
      return;
    }

    if (Array.isArray(value)) {
      value.forEach((item) => visit(item, depth + 1));
      return;
    }

    if (!value || typeof value !== "object") {
      return;
    }

    const record = value as Record<string, unknown>;
    const htmlCandidates = [
      record.html,
      record.htmlContent,
      record.srcdoc,
      record.markup,
    ];
    for (const candidate of htmlCandidates) {
      if (typeof candidate !== "string") {
        continue;
      }
      const html = extractHtmlFromText(candidate);
      if (!html) {
        continue;
      }
      const document = normalizeWebPreviewDocument(
        {
          title: typeof record.title === "string" ? record.title : "",
          html,
          url: typeof record.url === "string" && isLikelyUrl(record.url) ? record.url : "",
        },
        documents.length,
      );
      if (document) {
        documents.push(document);
      }
      return;
    }

    const urlCandidates = [record.webUrl, record.previewUrl, record.url];
    for (const candidate of urlCandidates) {
      if (typeof candidate !== "string" || !isLikelyUrl(candidate)) {
        continue;
      }
      if (!/\.html?($|\?)/i.test(candidate) && typeof record.html !== "string" && typeof record.htmlContent !== "string") {
        continue;
      }
      const document = normalizeWebPreviewDocument(
        {
          title: typeof record.title === "string" ? record.title : "",
          url: candidate,
        },
        documents.length,
      );
      if (document) {
        documents.push(document);
      }
      return;
    }

    for (const nestedValue of Object.values(record)) {
      visit(nestedValue, depth + 1);
    }
  }

  visit(payload);
  return documents;
}

function setPreviewImages(result: ImageToolResult, roundId: string): void {
  const incomingImages: PreviewImage[] = [];
  const storedResults = Array.isArray(result.storedResults)
    ? result.storedResults
    : [];
  const temporaryResults = Array.isArray(result.resultUrls)
    ? result.resultUrls
    : [];

  for (const item of storedResults) {
    const stored = item as StoredResult;
    incomingImages.push({
      id: createId("stored"),
      url: stored.url,
      title: extractFileName(stored.key),
      source: "stored",
      originUrl: stored.sourceUrl || stored.url,
    });
  }

  if (incomingImages.length === 0) {
    for (const url of temporaryResults) {
      incomingImages.push({
        id: createId("temp"),
        url,
        title: "临时生成结果",
        source: "temporary",
        originUrl: url,
      });
    }
  }

  if (incomingImages.length > 0) {
    const existingIndex = previewBatches.value.findIndex((batch) => batch.roundId === roundId);
    if (existingIndex >= 0) {
      const existing = previewBatches.value[existingIndex];
      if (!existing) {
        return;
      }

      const mergedImages = [...existing.images];
      for (const image of incomingImages) {
        const matchedIndex = mergedImages.findIndex(
          (existingImage) => existingImage.originUrl === image.originUrl,
        );
        if (matchedIndex >= 0) {
          mergedImages[matchedIndex] = image;
        } else {
          mergedImages.push(image);
        }
      }

      existing.images = mergedImages;
      existing.subtitle = buildPreviewBatchSubtitle(mergedImages);
      selectedPreviewBatchId.value = existing.id;
    } else {
      const nextBatch: PreviewBatch = {
        id: createId("preview-batch"),
        roundId,
        title: buildPreviewBatchTitle(previewBatches.value.length),
        subtitle: buildPreviewBatchSubtitle(incomingImages),
        images: incomingImages,
      };
      previewBatches.value.push(nextBatch);
      selectedPreviewBatchId.value = nextBatch.id;
    }
  }

  if (result.taskId) {
    runSummary.latestTaskId = result.taskId;
  }
  if (incomingImages.length > 0) {
    previewMode.value = "image";
  }
}

function setWebPreviewDocuments(documents: WebPreviewDocument[], roundId: string): void {
  if (documents.length === 0) {
    return;
  }

  const finalDocument = documents[documents.length - 1];
  if (!finalDocument) {
    return;
  }
  const nextSource = finalDocument.source ?? "html";
  const nextBatch: WebPreviewBatch = {
    id: createId("web-preview-batch"),
    roundId,
    title: "最终 Web 结果",
    subtitle: buildWebBatchSubtitle(1, nextSource),
    documents: [finalDocument],
  };
  webPreviewBatches.value = [nextBatch];
  selectedWebPreviewBatchId.value = nextBatch.id;

  previewMode.value = "web";
}

function handleToolResult(toolCallId: string, content: unknown): void {
  delete pendingToolNames[toolCallId];
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
    status: timelineEntry.toolName === "ask_followup" ? "running" : "completed",
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
    setPreviewImages(parsed as ImageToolResult, getActivePreviewRoundId());
  }

  if (parsed) {
    const webDocuments = collectWebPreviewDocuments(parsed);
    if (webDocuments.length > 0) {
      setWebPreviewDocuments(webDocuments, getActivePreviewRoundId());
    }
  }

  if (timelineEntry.toolName === "ask_followup" && parsed) {
    const followUp = parsed as unknown as FollowUpRequest;
    followUpCards[toolCallId] = {
      request: followUp,
      selectedOption: "",
      inputText: "",
      active: true,
      submittedAnswer: "",
    };
    ensureAssistantContextMessage(`followup-${toolCallId}`, followUp.question);
    updateAgentEntry(toolCallId, { collapsed: false });
    runSummary.status = "等待补充";
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
    setPreviewImages(latestImageResult, getActivePreviewRoundId());
  }

  if (latestSearch && Array.isArray(latestSearch.results)) {
    searchResults.value = latestSearch.results as SearchResultItem[];
  }

  const webDocuments = collectWebPreviewDocuments(state);
  if (webDocuments.length > 0) {
    setWebPreviewDocuments(webDocuments, getActivePreviewRoundId());
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

async function submitFollowUp(entryId: string): Promise<void> {
  const followUpState = getFollowUpState(entryId);
  if (!followUpState || !followUpState.active || isRunning.value) {
    return;
  }

  const answerParts = [followUpState.selectedOption.trim(), followUpState.inputText.trim()].filter(Boolean);
  const answer = answerParts.join("；");
  if (!answer) {
    return;
  }

  const previousSummary = "已收到你的补充，我继续往下处理。";
  followUpState.active = false;
  updateAgentEntry(entryId, {
    status: "completed",
    summary: previousSummary,
    detail: `追问：${followUpState.request.question}\n你的补充：${answer}`,
    collapsed: true,
  });

  try {
    await runAgent(answer, false);
    delete followUpCards[entryId];
  } catch (error) {
    followUpState.active = true;
    updateAgentEntry(entryId, {
      status: "running",
      summary: "还差一点关键信息，补充后我就继续处理。",
      detail: `追问：${followUpState.request.question}`,
      collapsed: false,
    });
    throw error;
  }
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
  webPreviewBatches.value = [];
  selectedWebPreviewBatchId.value = "";
  previewMode.value = "image";
  searchResults.value = [];
  for (const key of Object.keys(followUpCards)) {
    delete followUpCards[key];
  }
  for (const key of Object.keys(pendingToolNames)) {
    delete pendingToolNames[key];
  }
  pendingRequestMessages.value = [];
  threadId.value = "";
  activeRunId.value = "";
  activePreviewRoundId.value = "";
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
  aspectRatio.value = "";
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

    if (activeAssistantMessageId.value) {
      const messageEntry = feedEntries.value.find(
        (entry): entry is MessageFeedEntry =>
          entry.kind === "message" && entry.id === activeAssistantMessageId.value,
      );
      if (messageEntry) {
        ensureAssistantContextMessage(messageEntry.id, messageEntry.content);
        const html = extractHtmlFromText(messageEntry.content);
        if (html) {
          const document = normalizeWebPreviewDocument({ html }, 0);
          if (document) {
            setWebPreviewDocuments([document], getActivePreviewRoundId());
          }
        }
      }
    }
    activeAssistantMessageId.value = null;
    return;
  }

  if (event.type === "TOOL_CALL_START") {
    const toolCallId = String(event.toolCallId ?? createId("tool"));
    const toolCallName = String(event.toolCallName ?? "");
    pendingToolNames[toolCallId] = toolCallName;
    if (shouldDeferToolEntry(toolCallName)) {
      return;
    }
    addAgentEntry({
      id: toolCallId,
      kind: "agent",
      category: "tool",
      title: toolLabel(toolCallName),
      summary: `${toolLabel(toolCallName)}正在准备执行。`,
      detail: "",
      status: "running",
      collapsed: toolCallName !== "ask_followup",
      toolName: toolCallName,
    });
    return;
  }

  if (event.type === "TOOL_CALL_ARGS") {
    const toolCallId = String(event.toolCallId ?? "");
    const rawText = String(event.delta ?? "");
    let toolEntry = feedEntries.value.find(
      (entry): entry is AgentFeedEntry =>
        entry.kind === "agent" && entry.id === toolCallId,
    );
    if (!toolEntry) {
      const toolCallName = pendingToolNames[toolCallId] || "";
      addAgentEntry({
        id: toolCallId,
        kind: "agent",
        category: "tool",
        title: toolLabel(toolCallName),
        summary: "",
        detail: "",
        status: "running",
        collapsed: toolCallName !== "ask_followup",
        toolName: toolCallName,
      });
      toolEntry = feedEntries.value.find(
        (entry): entry is AgentFeedEntry =>
          entry.kind === "agent" && entry.id === toolCallId,
      );
    }
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
    const result = (event.result ?? {}) as Record<string, unknown>;
    runSummary.status = result.phase === "needs_followup" ? "等待补充" : "已完成";
    return;
  }
}

async function runAgent(userInput: string, addToFeed: boolean = true): Promise<void> {
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

  if (addToFeed) {
    activePreviewRoundId.value = createId("preview-round");
    addMessageEntry("user", normalizedInput, userMessageId);
  } else if (!activePreviewRoundId.value) {
    activePreviewRoundId.value = createId("preview-round");
  }
  pendingRequestMessages.value.push({
    id: userMessageId,
    role: "user",
    content: userMessageParts,
  });

  activeRunId.value = createId("run");
  runSummary.status = "等待 Agent 响应";
  isRunning.value = true;
  activeAbortController = new AbortController();
  const nextAspectRatio = aspectRatio.value.trim();

  const payload: RunAgentPayload = {
    threadId: threadId.value,
    runId: activeRunId.value,
    messages: pendingRequestMessages.value,
    state: {
      ...(nextAspectRatio ? { aspectRatio: nextAspectRatio } : {}),
      assetUrls: uploadedUrls,
    },
    tools: [],
    context: [],
    forwardedProps: {
      ...(nextAspectRatio ? { aspectRatio: nextAspectRatio } : {}),
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
  const nextPrompt = promptText.value;
  promptText.value = "";
  try {
    await runAgent(nextPrompt);
  } catch (error) {
    if (!promptText.value.trim()) {
      promptText.value = nextPrompt;
    }
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
  if (copyFeedbackTimer) {
    window.clearTimeout(copyFeedbackTimer);
  }
  if (pdfFeedbackTimer) {
    window.clearTimeout(pdfFeedbackTimer);
  }
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
              :class="['ratio-pill', { active: !aspectRatio }]"
              type="button"
              @click="aspectRatio = ''"
            >
              由 Agent 决定
            </button>
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
            <button
              aria-label="删除参考素材"
              class="asset-remove"
              type="button"
              @click="removeAsset(asset.id)"
            >
              ×
            </button>
            <img
              :alt="asset.name"
              :src="asset.previewUrl"
              class="asset-preview"
            />
            <div class="asset-meta">
              <strong>{{ asset.name }}</strong>
              <div class="asset-meta-row">
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
              </div>
              <span v-if="asset.errorMessage" class="asset-error">{{
                asset.errorMessage
              }}</span>
            </div>
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
            <span>{{ aspectRatio || "由 Agent 自行判断" }}</span>
          </div>

          <button
            class="primary-button"
            :disabled="isRunning || !canSubmitPrompt"
            type="button"
            @click="handleSubmit"
          >
            进入工作区
          </button>
        </div>
      </div>
    </section>

    <section
      v-else
      :class="[
        'conversation-screen',
        { 'conversation-screen--wide-preview': !hasContextPanel },
      ]"
    >
      <main class="dialog-panel">

        <div class="status-strip">
          <strong>{{ runSummary.status }}</strong>
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
                <div
                  v-if="entry.toolName === 'ask_followup' && getFollowUpState(entry.id)"
                  class="follow-up-card"
                >
                  <div class="follow-up-header">
                    <strong>补充一点信息，我就继续处理</strong>
                    <span>{{ getFollowUpState(entry.id)?.request.inputPlaceholder }}</span>
                  </div>
                  <div class="follow-up-options">
                    <button
                      v-for="option in getFollowUpState(entry.id)?.request.options || []"
                      :key="option"
                      :class="[
                        'follow-up-option',
                        { active: option === getFollowUpState(entry.id)?.selectedOption },
                      ]"
                      :disabled="!getFollowUpState(entry.id)?.active || isRunning"
                      type="button"
                      @click="setFollowUpOption(entry.id, option)"
                    >
                      {{ option }}
                    </button>
                  </div>
                  <textarea
                    class="follow-up-textarea"
                    :disabled="!getFollowUpState(entry.id)?.active || isRunning"
                    :placeholder="getFollowUpState(entry.id)?.request.inputPlaceholder"
                    rows="3"
                    :value="getFollowUpState(entry.id)?.inputText || ''"
                    @input="setFollowUpInput(entry.id, ($event.target as HTMLTextAreaElement).value)"
                  />
                  <div class="follow-up-actions">
                    <span v-if="getFollowUpState(entry.id)?.submittedAnswer" class="follow-up-answer">
                      已补充：{{ getFollowUpState(entry.id)?.submittedAnswer }}
                    </span>
                    <button
                      class="secondary-button"
                      :disabled="!canSubmitFollowUp(entry.id)"
                      type="button"
                      @click="submitFollowUp(entry.id)"
                    >
                      提交补充
                    </button>
                  </div>
                </div>
              </details>
            </template>
          </div>
          <p v-else class="empty-text">
            发送第一条消息后，左侧会按时间顺序串起对话、决策和工具调用。
          </p>
        </div>

        <div>
          <textarea
            v-model="promptText"
            class="prompt-textarea compact"
            placeholder="继续追问，或者换一个提示词重新生成。"
            rows="3"
          />

          <div class="sender-footer conversation-actions">
            <label class="toolbar-button">
              参考
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
              <span>比例</span>
              <select v-model="aspectRatio" class="ratio-select-input">
                <option value="">
                  由 Agent 决定
                </option>
                <option v-for="option in aspectRatioOptions" :key="option" :value="option">
                  {{ option }}
                </option>
              </select>
            </label>
            <button
              class="primary-button"
              :disabled="isRunning || !canSubmitPrompt"
              type="button"
              @click="handleSubmit"
            >
              {{ isRunning ? "运行中…" : "发送" }}
            </button>
          </div>
        </div>
      </main>

      <section class="preview-panel">
        <section class="preview-card">
          <header class="card-header">
            <h3>结果预览</h3>
            <span>
              {{
                previewMode === "web"
                  ? `${visibleWebDocuments.length} 个页面`
                  : `${visiblePreviewImages.length} 张图片`
              }}
            </span>
          </header>

          <div
            v-if="previewBatches.length > 0 || webPreviewBatches.length > 0"
            class="preview-mode-switch"
          >
            <button
              v-if="previewBatches.length > 0"
              :class="['mode-pill', { active: previewMode === 'image' }]"
              type="button"
              @click="previewMode = 'image'"
            >
              图片结果
            </button>
            <button
              v-if="webPreviewBatches.length > 0"
              :class="['mode-pill', { active: previewMode === 'web' }]"
              type="button"
              @click="previewMode = 'web'"
            >
              Web 内容
            </button>
          </div>

          <div
            v-if="previewMode === 'image' && previewBatches.length > 1"
            class="preview-history"
          >
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

          <div v-if="previewMode === 'image' && visiblePreviewImages.length > 0" class="preview-grid">
            <article
              v-for="image in visiblePreviewImages"
              :key="image.id"
              class="preview-item"
            >
              <img :alt="image.title" :src="image.url" />
              <div>
                <strong>{{ image.title }}</strong>
                <span>{{
                  image.source === "stored" ? "已转存" : "临时结果"
                }}</span>
              </div>
              <div class="preview-item-actions">
                <button
                  class="secondary-button preview-action"
                  type="button"
                  @click="addPreviewImageAsReference(image)"
                >
                  设为参考图
                </button>
                <a
                  :href="image.url"
                  class="preview-link"
                  rel="noreferrer"
                  target="_blank"
                >
                  查看大图
                </a>
              </div>
            </article>
          </div>

          <div v-else-if="previewMode === 'web' && visibleWebDocuments.length > 0" class="web-preview-list">
            <article
              v-for="document in visibleWebDocuments"
              :key="document.id"
              class="web-preview-item"
            >
              <div class="web-preview-toolbar">
                <div class="web-preview-meta">
                  <span>{{ document.source === "html" ? "HTML 预览" : "网页地址预览" }}</span>
                </div>
                <div class="web-preview-actions">
                  <button
                    class="secondary-button preview-action"
                    :disabled="!document.html"
                    type="button"
                    @click="copyWebDocumentCode(document)"
                  >
                    {{ copiedWebDocumentId === document.id ? "已复制" : "复制 H5 代码" }}
                  </button>
                  <button
                    class="secondary-button preview-action"
                    type="button"
                    @click="saveWebDocumentAsPdf(document)"
                  >
                    {{ savingPdfDocumentId === document.id ? "正在打开..." : "保存为 PDF" }}
                  </button>
                </div>
              </div>

              <iframe
                v-if="document.html"
                :srcdoc="document.html"
                class="web-preview-frame"
                sandbox="allow-scripts allow-same-origin"
              />
              <iframe
                v-else-if="document.url"
                :src="document.url"
                class="web-preview-frame"
                sandbox="allow-scripts allow-same-origin"
              />
            </article>
          </div>

          <p v-else class="empty-text">
            {{
              previewMode === "web"
                ? "当 Agent 返回网页排版内容后，这里会自动出现可浏览的预览。"
                : "当图片生成或转存完成后，这里会自动出现预览。"
            }}
          </p>
        </section>
      </section>

      <aside v-if="hasContextPanel" class="context-panel">
        <section v-if="hasReferenceAssets" class="reference-card">
          <header class="card-header">
            <h3>参考素材</h3>
            <span>{{ uploadedAssets.length }} 个已上传素材</span>
          </header>

          <div class="asset-grid compact">
            <article
              v-for="asset in composerAssets"
              :key="asset.id"
              class="asset-card"
            >
              <button
                aria-label="删除参考素材"
                class="asset-remove"
                type="button"
                @click="removeAsset(asset.id)"
              >
                ×
              </button>
              <img
                :alt="asset.name"
                :src="asset.previewUrl"
                class="asset-preview"
              />
              <div class="asset-meta">
                <strong>{{ asset.name }}</strong>
                <div class="asset-meta-row">
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
                </div>
                <span v-if="asset.errorMessage" class="asset-error">{{
                  asset.errorMessage
                }}</span>
              </div>
            </article>
          </div>
        </section>

        <section v-if="hasSearchReferences" class="search-card">
          <header class="card-header">
            <h3>搜索参考</h3>
            <span>{{ searchResults.length }} 条</span>
          </header>

          <div class="search-list">
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
        </section>
      </aside>
    </section>
  </div>
</template>
