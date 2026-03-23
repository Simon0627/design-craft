from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

GenerationMode = Literal["text_to_image", "image_to_image", "multi_image_edit"]
TaskStatus = Literal["submitted", "processing", "succeed", "failed"]

allowedAspectRatios = {"16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3", "21:9"}


class DesignPlan(BaseModel):
    intentSummary: str = Field(description="对用户目标的简要归纳")
    generationMode: GenerationMode = Field(description="本次任务应该选择的生图模式")
    prompt: str = Field(description="发给七牛生图接口的最终提示词")
    aspectRatio: str = Field(default="16:9", description="建议输出比例")
    shouldUseSearch: bool = Field(default=False, description="是否需要额外搜索")
    searchQueries: list[str] = Field(default_factory=list, description="搜索查询词")
    selectedSkillNames: list[str] = Field(default_factory=list, description="建议使用的技能名")
    assetUrls: list[str] = Field(default_factory=list, description="参与本次生成的素材地址")
    referenceLinks: list[str] = Field(default_factory=list, description="用户提供的参考链接")
    notes: list[str] = Field(default_factory=list, description="补充说明")

    @field_validator("aspectRatio")
    @classmethod
    def validateAspectRatio(cls, value: str) -> str:
        if value not in allowedAspectRatios:
            raise ValueError(f"不支持的比例：{value}")
        return value


class DesignPlanRequest(BaseModel):
    userInput: str = Field(min_length=1, description="用户的设计需求")
    assetUrls: list[str] = Field(default_factory=list, description="用户上传或已存储的素材 URL")
    referenceLinks: list[str] = Field(default_factory=list, description="用户提供的参考链接")
    aspectRatio: Optional[str] = Field(default=None, description="用户期望的输出比例")
    generationMode: Optional[GenerationMode] = Field(default=None, description="可选，强制指定模式")


class DesignPlanResponse(BaseModel):
    plan: DesignPlan


class StoredObject(BaseModel):
    key: str
    url: str
    sourceUrl: str


class ImageTaskStatusResponse(BaseModel):
    taskId: str
    status: TaskStatus
    statusMessage: str
    created: Optional[int] = None
    resultUrls: list[str] = Field(default_factory=list)
    storedResults: list[StoredObject] = Field(default_factory=list)
    rawTask: Optional[dict[str, Any]] = None


class DesignGenerateRequest(DesignPlanRequest):
    imageCount: int = Field(default=1, ge=1, le=10, description="一次生成的图片数量")
    autoStoreResult: bool = Field(default=True, description="生成成功后是否自动转存到 Kodo")
    waitForResult: bool = Field(default=False, description="是否阻塞等待图片生成完成")
    taskPollTimeoutSeconds: Optional[int] = Field(default=None, ge=5, le=600)
    outputKeyPrefix: str = Field(default="generated", description="生成结果转存前缀")


class DesignGenerateResponse(ImageTaskStatusResponse):
    plan: DesignPlan
