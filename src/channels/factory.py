from functools import lru_cache

from src.channels.base import ChannelAdapter
from src.channels.twilio import TwilioChannel
from src.config import get_settings


@lru_cache
def get_channel() -> ChannelAdapter:
    settings = get_settings()
    return TwilioChannel(
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
        whatsapp_number=settings.twilio_whatsapp_number,
    )
