"""Unauthenticated instance settings the login page needs before anyone is signed in."""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.auth.dependencies import get_runtime
from app.config import Settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


class PublicSettings(BaseModel):
    """The only fields this endpoint may ever return; adding one discloses it to anyone."""

    authentication_methods: list[str]
    instance_name: str


@router.get("/public")
async def public_settings(request: Request) -> PublicSettings:
    """Return the enabled authentication methods and the instance name."""
    settings: Settings = request.app.state.settings
    runtime = get_runtime(request)
    methods = runtime.registry.names()
    if runtime.platform is not None:
        methods.append("jwt")
    return PublicSettings(
        authentication_methods=methods,
        instance_name=settings.instance_name,
    )
