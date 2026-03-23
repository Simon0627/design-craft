from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class AgUiMessage(BaseModel):
    id: str = Field(default="")
    role: str
    content: Any = None
    name: Optional[str] = None


class RunAgentInput(BaseModel):
    threadId: str
    runId: str
    parentRunId: Optional[str] = None
    state: dict[str, Any] = Field(default_factory=dict)
    messages: list[AgUiMessage] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    context: list[dict[str, Any]] = Field(default_factory=list)
    forwardedProps: dict[str, Any] = Field(default_factory=dict)


class ParsedAgentRequest(BaseModel):
    userInput: str
    combinedUserContext: str = ""
    conversationHistory: list[dict[str, str]] = Field(default_factory=list)
    assetUrls: list[str] = Field(default_factory=list)
    referenceLinks: list[str] = Field(default_factory=list)
    aspectRatio: Optional[str] = None
    imageCount: int = 1
    autoStoreResult: bool = True
    outputKeyPrefix: str = "generated"
    taskPollTimeoutSeconds: Optional[int] = None
