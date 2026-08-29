"""Role-protected controls for the fixed local demonstration scenario."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status

from backend.app.core.security import require_local_request, require_roles
from backend.app.schemas import DemoStreamRequest, UserIdentity

router = APIRouter(prefix="/demo", tags=["demo"])
Admin = Annotated[UserIdentity, Depends(require_roles("admin"))]
LocalRequest = Annotated[None, Depends(require_local_request)]


@router.get("/stream")
async def demo_stream_status(
    request: Request,
    _admin: Admin,
    _local: LocalRequest,
) -> dict[str, Any]:
    return await request.app.state.demo_stream_service.status()


@router.post("/stream", status_code=status.HTTP_202_ACCEPTED)
async def start_demo_stream(
    body: DemoStreamRequest,
    request: Request,
    _admin: Admin,
    _local: LocalRequest,
) -> dict[str, Any]:
    return await request.app.state.demo_stream_service.start(body.scenario)
