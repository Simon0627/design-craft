from __future__ import annotations

from pathlib import Path

from app.services.skills import SkillService


def writeSkill(skillDir: Path, folderName: str, content: str) -> None:
    targetDir = skillDir / folderName
    targetDir.mkdir(parents=True)
    (targetDir / "SKILL.md").write_text(content, encoding="utf-8")


def testListSkillsOnlyReturnsValidFrontmatter(tmp_path: Path) -> None:
    writeSkill(
        tmp_path,
        "valid-skill",
        """---
name: image-edit
description: 编辑和调整已有图片
---
正文
""",
    )
    writeSkill(
        tmp_path,
        "invalid-name",
        """---
name: ImageEdit
description: 这条因为命名不合法应被忽略
---
正文
""",
    )
    writeSkill(
        tmp_path,
        "missing-frontmatter",
        "没有 frontmatter",
    )

    skills = SkillService(tmp_path).listSkills()

    assert len(skills) == 1
    assert skills[0].name == "image-edit"
