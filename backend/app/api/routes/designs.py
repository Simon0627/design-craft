from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import getDesignService, getSkillService
from app.schemas.design import (
    DesignGenerateRequest,
    DesignGenerateResponse,
    DesignPlanRequest,
    DesignPlanResponse,
    ImageTaskStatusResponse,
)
from app.schemas.skill import SkillDescriptor
from app.services.design_agent import DesignAgentService
from app.services.skills import SkillService

router = APIRouter()


@router.get("/skills", response_model=list[SkillDescriptor])
async def listSkills(skillService: SkillService = Depends(getSkillService)) -> list[SkillDescriptor]:
    return skillService.listSkills()


@router.post("/plan", response_model=DesignPlanResponse)
async def planDesign(
    request: DesignPlanRequest,
    designService: DesignAgentService = Depends(getDesignService),
) -> DesignPlanResponse:
    plan = await designService.planDesign(request)
    return DesignPlanResponse(plan=plan)


@router.post("/generate", response_model=DesignGenerateResponse)
async def generateDesign(
    request: DesignGenerateRequest,
    designService: DesignAgentService = Depends(getDesignService),
) -> DesignGenerateResponse:
    return await designService.generateDesign(request)


@router.get("/tasks/{taskId}", response_model=ImageTaskStatusResponse)
async def getDesignTaskStatus(
    taskId: str,
    autoStoreResult: bool = Query(default=True),
    outputKeyPrefix: str = Query(default="generated"),
    designService: DesignAgentService = Depends(getDesignService),
) -> ImageTaskStatusResponse:
    return await designService.getTaskStatus(taskId, autoStoreResult, outputKeyPrefix)
