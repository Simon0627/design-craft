export interface AgUiEvent {
  type: string
  [key: string]: unknown
}

export interface AgUiMessagePart {
  type: 'text' | 'image_url'
  text?: string
  image_url?: {
    url: string
  }
}

export interface AgUiMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string | AgUiMessagePart[]
}

export interface RunAgentPayload {
  threadId: string
  runId: string
  parentRunId?: string
  state: Record<string, unknown>
  messages: AgUiMessage[]
  tools: Array<Record<string, unknown>>
  context: Array<Record<string, unknown>>
  forwardedProps: Record<string, unknown>
}

export interface UploadFileResponse {
  bucketName: string
  bucketDomain: string
  key: string
  fileName: string
  contentType: string
  size: number
  width?: number
  height?: number
  url: string
}

export interface SearchResultItem {
  title?: string
  link?: string
  snippet?: string
  source?: string
}

export interface StoredResult {
  key: string
  url: string
  sourceUrl: string
}

export interface ImageToolResult {
  taskId?: string
  status?: string
  resultUrls?: string[]
  storedResults?: StoredResult[]
}
