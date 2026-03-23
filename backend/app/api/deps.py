from __future__ import annotations

from fastapi import Request

from app.clients.kodo import KodoClient
from app.services.design_agent import DesignAgentService
from app.services.image_assets import ImageAssetService
from app.services.skills import SkillService


def getSkillService(request: Request) -> SkillService:
    return request.app.state.skillService


def getKodoClient(request: Request) -> KodoClient:
    return request.app.state.kodoClient


def getImageAssetService(request: Request) -> ImageAssetService:
    return request.app.state.imageAssetService


def getDesignService(request: Request) -> DesignAgentService:
    return request.app.state.designService
