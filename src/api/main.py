import logging
from typing import Annotated

from fastapi import Depends, FastAPI, Request, Response
from rq import Queue
from sqlalchemy.ext.asyncio import AsyncSession

from src.api import meta
from src.api.inbound import apply_status, store_and_enqueue
from src.api.media import router as media_router
from src.channels.base import TWILIO
from src.channels.factory import get_channel
from src.config import get_settings
from src.logging_config import setup_logging
from src.storage.db import get_async_session
from src.storage.queue import get_inbound_queue
from src.tasks.config import get_collection_config

setup_logging()
logger = logging.getLogger(__name__)
# Fail at startup, with a clear message, if config/assignment_settings.yaml is invalid.
get_collection_config()

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'

app = FastAPI(title="whatsapp-speech-collector")
app.include_router(media_router)
app.include_router(meta.router)


# =======
# HELPERS
# =======
def empty_twiml() -> Response:
    return Response(content=EMPTY_TWIML, media_type="application/xml")


async def _form(request: Request) -> dict[str, str]:
    form = await request.form()
    return {key: str(value) for key, value in form.items()}


def _signature_ok(request: Request, params: dict[str, str]) -> bool:
    """Twilio's X-Twilio-Signature check (skipped if TWILIO_VALIDATE_SIGNATURE is off)."""
    settings = get_settings()
    if not settings.twilio_validate_signature:
        return True
    # Behind ngrok/nginx the URL FastAPI sees differs from the one Twilio
    # signed, so rebuild it from the public base URL.
    url = settings.public_base_url.rstrip("/") + request.url.path
    if request.url.query:
        url += f"?{request.url.query}"
    signature = request.headers.get("X-Twilio-Signature", "")
    return get_channel(TWILIO).validate_request(url, params, signature)


# ======
# ROUTES
# ======
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/whatsapp/webhook")
async def whatsapp_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_async_session)],
    queue: Annotated[Queue, Depends(get_inbound_queue)],
) -> Response:
    params = await _form(request)
    if not _signature_ok(request, params):
        logger.warning("Rejected webhook with invalid Twilio signature")
        return Response(status_code=403)

    # Store first, deduplicating on MessageSid: Twilio retries webhooks.
    await store_and_enqueue(session, queue, TWILIO, get_channel(TWILIO).parse_inbound(params))
    return empty_twiml()


@app.post("/whatsapp/status")
async def whatsapp_status(
    request: Request, session: Annotated[AsyncSession, Depends(get_async_session)]
) -> Response:
    """Twilio delivery status callbacks for messages the bot sent."""
    params = await _form(request)
    if not _signature_ok(request, params):
        logger.warning("Rejected status callback with invalid Twilio signature")
        return Response(status_code=403)

    await apply_status(session, TWILIO, get_channel(TWILIO).parse_status(params))
    return Response(status_code=204)
