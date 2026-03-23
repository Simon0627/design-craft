from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class TextSearchResult(BaseModel):
    title: str = ""
    link: str = ""
    snippet: str = ""
    source: str = ""
    position: Optional[int] = None


class ImageSearchResult(BaseModel):
    title: str = ""
    imageUrl: str = ""
    thumbnailUrl: str = ""
    hostPageUrl: str = ""
    source: str = ""
    width: Optional[int] = None
    height: Optional[int] = None


class SearchToolOutcome(BaseModel):
    status: str = Field(default="success")
    query: str = ""
    results: list[dict[str, Any]] = Field(default_factory=list)
    reason: str = ""
