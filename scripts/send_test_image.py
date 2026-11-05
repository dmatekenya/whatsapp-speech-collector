"""Send one image prompt to a WhatsApp number, to test media sending.

    uv run python -m scripts.send_test_image --prompt-id I0001 --to +1XXXXXXXXXX
    uv run python -m scripts.send_test_image --prompt-id I0001 --to +1XXXXXXXXXX --caption "Describe this"
    uv run python -m scripts.send_test_image --prompt-id I0001 --to +1XXXXXXXXXX --dry-run
    uv run python -m scripts.send_test_image --prompt-id I0001 --to +1XXXXXXXXXX --channel meta

Builds a signed media URL (GET /media/prompts/{token} under PUBLIC_BASE_URL),
sends it with the channel adapter's send_media, and prints the provider's
message ID. The channel is --channel, or WHATSAPP_PROVIDER if not given (this
message answers nothing, so there's no inbound channel to follow). The API
must be running and reachable at PUBLIC_BASE_URL (e.g. via ngrok) while
Twilio or Meta fetches the image.
"""

import argparse
import sys

from sqlalchemy import select

from src.channels.base import CHANNELS
from src.channels.factory import get_channel
from src.config import get_settings
from src.importers.framework import load_config
from src.importers.participants import DEFAULT_CONFIG as PARTICIPANTS_CONFIG
from src.logging_config import mask_phone
from src.media.urls import DEFAULT_TTL_SECONDS, MediaUrlError, build_media_url
from src.registry.participants import normalize_phone
from src.storage.db import get_sync_sessionmaker
from src.storage.files import get_prompt_media_storage
from src.storage.models import Prompt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prompt-id", required=True, help="an image prompt, e.g. I0001")
    parser.add_argument("--to", required=True, help="recipient's WhatsApp number, e.g. +1XXXXXXXXXX")
    parser.add_argument("--caption", help="text shown under the image")
    parser.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS, help="URL lifetime in seconds")
    parser.add_argument("--channel", choices=CHANNELS, help="twilio or meta (default: WHATSAPP_PROVIDER)")
    parser.add_argument("--dry-run", action="store_true", help="print the URL, send nothing")
    args = parser.parse_args()

    phone = load_config(PARTICIPANTS_CONFIG).options["phone"]
    try:
        to = normalize_phone(args.to, phone["default_region"], phone["allowed_country_codes"])
    except ValueError as exc:
        parser.error(f"--to: {exc}")

    with get_sync_sessionmaker()() as session:
        prompt = session.scalar(select(Prompt).where(Prompt.prompt_id == args.prompt_id))
    if prompt is None or prompt.prompt_type != "image" or not prompt.media_path:
        parser.error(f"{args.prompt_id} isn't an image prompt with a prepared file")
    if not get_prompt_media_storage().exists(prompt.media_path):
        parser.error(f"prepared file for {args.prompt_id} is missing: {prompt.media_path}")

    try:
        url = build_media_url(prompt.prompt_id, args.ttl)
    except MediaUrlError as exc:
        sys.exit(f"Can't build the media URL: {exc}")
    print(f"media URL (valid {args.ttl} s): {url}")
    if args.dry_run:
        print(f"dry run: not sending {prompt.prompt_id} to {mask_phone(to)}")
        return

    channel = args.channel or get_settings().whatsapp_provider
    sid = get_channel(channel).send_media(to, url, caption=args.caption)
    print(f"sent {prompt.prompt_id} via {channel} to {mask_phone(to)}: message ID {sid}")


if __name__ == "__main__":
    main()
