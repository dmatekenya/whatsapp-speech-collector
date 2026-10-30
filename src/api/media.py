"""GET /media/prompts/{token}: serve a prompt's prepared image to Twilio.

Only the prepared copy stored on the prompt (media_path, under
Settings.prompt_media_root) can be served; originals and any other file are
unreachable. Unknown, expired, tampered and otherwise unservable tokens all
get the same 404.
"""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.media.urls import MEDIA_ROUTE, verify_token
from src.storage.db import get_async_session
from src.storage.files import get_prompt_media_storage
from src.storage.models import Prompt

router = APIRouter()


# =======
# HELPERS
# =======
def _not_found() -> HTTPException:
    # One response for every failure, so callers can't tell why.
    return HTTPException(status_code=404, detail="Not Found")


def _prepared_file(media_path: str) -> Path | None:
    """The file for media_path if it's a regular file inside the prepared root."""
    storage = get_prompt_media_storage()
    try:
        root = storage.absolute_path(".").resolve(strict=True)
        # absolute_path rejects absolute paths and ".."; resolving also
        # follows symlinks, so a link pointing outside the root is caught.
        path = storage.absolute_path(media_path).resolve(strict=True)
    except (ValueError, OSError):
        return None
    if root not in path.parents or not path.is_file():
        return None
    return path


# ======
# ROUTES
# ======
@router.api_route(MEDIA_ROUTE + "/{token}", methods=["GET", "HEAD"])
async def prompt_media(
    token: str, session: Annotated[AsyncSession, Depends(get_async_session)]
) -> FileResponse:
    prompt_id = verify_token(token, get_settings().media_signing_secret.get_secret_value())
    if prompt_id is None:
        raise _not_found()
    prompt = await session.scalar(select(Prompt).where(Prompt.prompt_id == prompt_id))
    if (
        prompt is None
        or not prompt.media_path
        or not (prompt.media_content_type or "").startswith("image/")
    ):
        raise _not_found()
    path = _prepared_file(prompt.media_path)
    if path is None:
        raise _not_found()
    return FileResponse(
        path,
        media_type=prompt.media_content_type,
        # The prompt ID, never the original file name.
        filename=f"{prompt.prompt_id}{path.suffix}",
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )
