from fastapi import APIRouter

from proagent_os.api import (
    agents,
    dashboard,
    kb,
    metrics,
    monitors,
    recommendations,
    skills,
    tasks,
    tenants,
    usage,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(tenants.router, prefix="/tenants", tags=["tenants"])
api_router.include_router(agents.router, prefix="/agents", tags=["agents"])
api_router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(kb.router, prefix="/kb", tags=["kb"])
api_router.include_router(skills.router, prefix="/skills", tags=["skills"])
api_router.include_router(monitors.router, prefix="/monitors", tags=["monitors"])
api_router.include_router(recommendations.router, prefix="/recommendations", tags=["recommendations"])
api_router.include_router(metrics.router, prefix="/metrics", tags=["metrics"])
api_router.include_router(usage.router, prefix="/usage", tags=["usage"])
api_router.include_router(dashboard.router, prefix="/dashboard", tags=["dashboard"])
