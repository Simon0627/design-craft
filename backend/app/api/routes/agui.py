from __future__ import annotations

import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api.deps import getAgUiAgentService
from app.schemas.agui import RunAgentInput
from app.services.agui_agent import AgUiAgentService

router = APIRouter()


@router.post("/run")
async def runAgent(
    request: RunAgentInput,
    agUiAgentService: AgUiAgentService = Depends(getAgUiAgentService),
) -> StreamingResponse:
    async def eventStream() -> AsyncIterator[str]:
        async for event in agUiAgentService.run(request):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(eventStream(), media_type="text/event-stream")
