"""Caller identity from headers. No login, per the brief."""
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User, Workspace


@dataclass
class Caller:
    user_id: str
    workspace_id: str
    role: str
    workspace_verified: bool

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


async def current_caller(
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    session: AsyncSession = Depends(get_session),
) -> Caller:
    if not x_user_id or not x_workspace_id:
        raise HTTPException(status_code=401, detail="X-User-Id and X-Workspace-Id are required")

    user = await session.get(User, x_user_id)
    workspace = await session.get(Workspace, x_workspace_id)
    if user is None or workspace is None:
        raise HTTPException(status_code=401, detail="unknown user or workspace")

    # A user may only act inside their own workspace. Claiming another one is
    # rejected outright rather than silently scoped, so rule 1 cannot be bypassed
    # by sending a different header.
    if user.workspace_id != workspace.id:
        raise HTTPException(status_code=403, detail="user does not belong to that workspace")

    return Caller(
        user_id=user.id,
        workspace_id=workspace.id,
        role=user.role,
        workspace_verified=bool(workspace.verified),
    )


async def require_admin(caller: Caller = Depends(current_caller)) -> Caller:
    if not caller.is_admin:
        # Rule 6: only workspace admins can approve or reject.
        raise HTTPException(status_code=403, detail="admin role required")
    return caller
