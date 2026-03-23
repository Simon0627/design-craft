from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import apiRouter
from app.clients.kodo import KodoClient
from app.clients.qiniu_maas import QiniuMaaSClient
from app.clients.serpapi import SerpApiClient
from app.core.config import getSettings
from app.core.exceptions import registerExceptionHandlers
from app.services.agui_agent import AgUiAgentService
from app.services.design_agent import DesignAgentService
from app.services.image_assets import ImageAssetService
from app.services.search import SearchService
from app.services.skills import SkillService


@asynccontextmanager
async def appLifespan(app: FastAPI):
    settings = getSettings()
    skillService = SkillService(settings.skillBaseDir)
    imageAssetService = ImageAssetService()
    kodoClient = KodoClient(settings)
    maasClient = QiniuMaaSClient(settings)
    serpApiClient = SerpApiClient(settings)
    searchService = SearchService(serpApiClient)
    designService = DesignAgentService(settings, maasClient, kodoClient, skillService, imageAssetService)
    agUiAgentService = AgUiAgentService(settings, designService, searchService)

    app.state.settings = settings
    app.state.skillService = skillService
    app.state.imageAssetService = imageAssetService
    app.state.kodoClient = kodoClient
    app.state.maasClient = maasClient
    app.state.serpApiClient = serpApiClient
    app.state.searchService = searchService
    app.state.designService = designService
    app.state.agUiAgentService = agUiAgentService

    try:
        yield
    finally:
        await serpApiClient.aclose()
        await imageAssetService.aclose()
        await maasClient.aclose()


def createApp() -> FastAPI:
    settings = getSettings()
    app = FastAPI(title=settings.appName, version=settings.appVersion, lifespan=appLifespan)
    app.include_router(apiRouter, prefix=settings.apiPrefix)
    registerExceptionHandlers(app)
    return app


app = createApp()
