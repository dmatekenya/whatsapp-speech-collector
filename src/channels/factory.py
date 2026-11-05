from functools import lru_cache

from src.channels.base import META, TWILIO, ChannelAdapter
from src.channels.meta import MetaChannel
from src.channels.twilio import TwilioChannel
from src.config import get_settings


@lru_cache
def get_channel(name: str | None = None) -> ChannelAdapter:
    """The adapter for a channel ("twilio" or "meta").

    Pass the channel of the inbound message being answered, so replies go out
    on the number the participant wrote to. With no name, WHATSAPP_PROVIDER
    decides (only for messages that answer nothing, e.g. test sends).
    """
    settings = get_settings()
    name = name or settings.whatsapp_provider
    if name == TWILIO:
        return TwilioChannel(
            account_sid=settings.twilio_account_sid,
            auth_token=settings.twilio_auth_token,
            whatsapp_number=settings.twilio_whatsapp_number,
        )
    if name == META:
        token = settings.meta_access_token
        return MetaChannel(
            access_token=token.get_secret_value() if token else None,
            phone_number_id=settings.meta_phone_number_id,
            graph_version=settings.meta_graph_version,
        )
    raise ValueError(f"Unknown channel {name!r}")
