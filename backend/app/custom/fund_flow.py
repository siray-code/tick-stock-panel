"""L2 route registration; dataset and service remain independently reusable."""
from contextlib import asynccontextmanager

from fastapi import APIRouter

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "official.fund-flow"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


@asynccontextmanager
async def lifespan(_app):
    from app.services.fund_flow import service

    try:
        yield
    finally:
        service.close()


def setup(registrar: BackendExtensionRegistrar) -> None:
    from app.api.fund_flow import router

    wrapper = APIRouter(lifespan=lifespan)
    wrapper.include_router(router)
    registrar.include_router(wrapper)
