import type { AgUiEvent, RunAgentPayload, UploadFileResponse } from '@/types/agui'

interface StreamAgentOptions {
  signal?: AbortSignal
  onEvent: (event: AgUiEvent) => void
}

function decodeSseChunk(buffer: string): { events: AgUiEvent[]; rest: string } {
  const segments = buffer.split('\n\n')
  const rest = segments.pop() ?? ''
  const events: AgUiEvent[] = []

  for (const segment of segments) {
    const lines = segment.split('\n')
    const dataLines = lines.filter((line) => line.startsWith('data:'))
    if (dataLines.length === 0) {
      continue
    }

    const rawPayload = dataLines.map((line) => line.slice(5).trimStart()).join('\n')
    if (!rawPayload) {
      continue
    }

    try {
      events.push(JSON.parse(rawPayload) as AgUiEvent)
    } catch (error) {
      console.warn('解析 AG-UI 事件失败', error, rawPayload)
    }
  }

  return { events, rest }
}

export async function streamAgentRun(
  payload: RunAgentPayload,
  options: StreamAgentOptions,
): Promise<void> {
  const response = await fetch('/api/v1/agui/run', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(payload),
    signal: options.signal,
  })

  if (!response.ok) {
    const errorText = await response.text()
    throw new Error(errorText || `AG-UI 请求失败，状态码 ${response.status}`)
  }

  if (!response.body) {
    throw new Error('AG-UI 接口没有返回可读取的事件流。')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) {
      break
    }

    buffer += decoder.decode(value, { stream: true })
    const { events, rest } = decodeSseChunk(buffer)
    buffer = rest

    for (const event of events) {
      options.onEvent(event)
    }
  }

  const finalChunk = decoder.decode()
  if (finalChunk) {
    buffer += finalChunk
  }

  const { events } = decodeSseChunk(buffer)
  for (const event of events) {
    options.onEvent(event)
  }
}

export async function uploadReferenceFile(file: File): Promise<UploadFileResponse> {
  const formData = new FormData()
  formData.append('prefix', 'references')
  formData.append('file', file)

  const response = await fetch('/api/v1/uploads/file', {
    method: 'POST',
    body: formData,
  })

  if (!response.ok) {
    const errorText = await response.text()
    throw new Error(errorText || `上传素材失败，状态码 ${response.status}`)
  }

  return (await response.json()) as UploadFileResponse
}
