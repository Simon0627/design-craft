from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml

from app.schemas.skill import SkillDescriptor

skillNamePattern = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class SkillService:
    def __init__(self, skillBaseDir: Path):
        self.skillBaseDir = Path(skillBaseDir)

    def listSkills(self) -> list[SkillDescriptor]:
        if not self.skillBaseDir.exists():
            return []

        skills: list[SkillDescriptor] = []
        for skillFile in sorted(self.skillBaseDir.glob("*/SKILL.md")):
            skill = self._loadSkill(skillFile)
            if skill is not None:
                skills.append(skill)
        return skills

    def _loadSkill(self, skillFile: Path) -> Optional[SkillDescriptor]:
        content = skillFile.read_text(encoding="utf-8")
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if not match:
            return None

        meta = yaml.safe_load(match.group(1)) or {}
        name = str(meta.get("name", "")).strip()
        description = str(meta.get("description", "")).strip()

        if not skillNamePattern.match(name):
            return None
        if not description or len(description) > 1024:
            return None

        return SkillDescriptor(name=name, description=description, path=str(skillFile))
