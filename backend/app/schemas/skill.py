from __future__ import annotations

from pydantic import BaseModel


class SkillDescriptor(BaseModel):
    name: str
    description: str
    path: str
