from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

backendRoot = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    appName: str = Field(default="DesignCraft Agent Backend", validation_alias="APP_NAME")
    appVersion: str = Field(default="0.1.0", validation_alias="APP_VERSION")
    apiPrefix: str = Field(default="/api/v1", validation_alias="API_PREFIX")

    qiniuAccessKey: str = Field(default="", validation_alias="QINIU_ACCESS_KEY")
    qiniuSecretKey: str = Field(default="", validation_alias="QINIU_SECRET_KEY")
    qiniuBucketName: str = Field(default="design-craft", validation_alias="QINIU_BUCKET_NAME")
    qiniuBucketDomain: str = Field(
        default="tccc78x9r.hd-bkt.clouddn.com",
        validation_alias="QINIU_BUCKET_DOMAIN",
    )
    qiniuBucketScheme: str = Field(default="http", validation_alias="QINIU_BUCKET_SCHEME")
    qiniuUploadHost: str = Field(default="up.qiniup.com", validation_alias="QINIU_UPLOAD_HOST")

    qiniuMaasApiKey: str = Field(default="", validation_alias="QINIU_MAAS_API_KEY")
    qiniuMaasBaseUrl: str = Field(
        default="https://api.qnaigc.com/v1",
        validation_alias="QINIU_MAAS_BASE_URL",
    )
    qiniuChatModel: str = Field(default="deepseek-v3", validation_alias="QINIU_CHAT_MODEL")
    qiniuImageModel: str = Field(default="kling-v2-1", validation_alias="QINIU_IMAGE_MODEL")

    defaultAspectRatio: str = Field(default="16:9", validation_alias="DEFAULT_ASPECT_RATIO")
    defaultImageCount: int = Field(default=1, validation_alias="DEFAULT_IMAGE_COUNT")
    taskPollIntervalSeconds: float = Field(default=2.0, validation_alias="TASK_POLL_INTERVAL_SECONDS")
    taskPollTimeoutSeconds: int = Field(default=120, validation_alias="TASK_POLL_TIMEOUT_SECONDS")
    plannerMaxTokens: int = Field(default=1024, validation_alias="PLANNER_MAX_TOKENS")

    skillBaseDir: Path = Field(default=backendRoot / "design-skills", validation_alias="SKILL_BASE_DIR")


@lru_cache(maxsize=1)
def getSettings() -> Settings:
    return Settings()
