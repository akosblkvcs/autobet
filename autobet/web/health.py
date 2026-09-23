"""The liveness probe, open because Coolify runs it inside the container."""

# pyright: reportUnusedFunction=false

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from autobet.web.context import Context


def router(context: Context) -> APIRouter:
    """Build the /healthz route."""
    api = APIRouter()

    @api.get("/healthz")
    async def healthz() -> JSONResponse:
        ok = context.telegram.healthy()

        return JSONResponse(context.status(), status_code=200 if ok else 503)

    return api
