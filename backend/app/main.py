from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import apiRouter
from app.clients.kodo import KodoClient
from app.clients.qiniu_maas import QiniuMaaSClient
from app.core.config import getSettings
from app.core.exceptions import registerExceptionHandlers
from app.services.design_agent import DesignAgentService
from app.services.image_assets import ImageAssetService
from app.services.skills import SkillService


@asynccontextmanager
async def appLifespan(app: FastAPI):
    settings = getSettings()
    skillService = SkillService(settings.skillBaseDir)
    imageAssetService = ImageAssetService()
    kodoClient = KodoClient(settings)
    maasClient = QiniuMaaSClient(settings)
    designService = DesignAgentService(settings, maasClient, kodoClient, skillService, imageAssetService)

    app.state.settings = settings
    app.state.skillService = skillService
    app.state.imageAssetService = imageAssetService
    app.state.kodoClient = kodoClient
    app.state.maasClient = maasClient
    app.state.designService = designService

    try:
        yield
    finally:
        await imageAssetService.aclose()
        await maasClient.aclose()


def createApp() -> FastAPI:
    settings = getSettings()
    app = FastAPI(title=settings.appName, version=settings.appVersion, lifespan=appLifespan)
    app.include_router(apiRouter, prefix=settings.apiPrefix)
    registerExceptionHandlers(app)
    return app


app = createApp()
