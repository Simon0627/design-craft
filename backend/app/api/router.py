from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import designs, health, uploads

apiRouter = APIRouter()
apiRouter.include_router(health.router, tags=["health"])
apiRouter.include_router(uploads.router, prefix="/uploads", tags=["uploads"])
apiRouter.include_router(designs.router, prefix="/designs", tags=["designs"])
