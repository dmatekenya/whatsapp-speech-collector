"""Queue outbound messages still pending (e.g. after a Redis outage) for sending.

    uv run python -m scripts.send_pending_outbound --older-than-minutes 5 --dry-run
    uv run python -m scripts.send_pending_outbound

Only the first pending message of each turn is queued; each one sent queues
the next, so their order is kept.
"""

import argparse
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from src.conversation.outbound import enqueue_send
from src.logging_config import setup_logging
from src.storage.db import get_sync_sessionmaker
from src.storage.models import OutboundMessage

logger = logging.getLogger("send_pending_outbound")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--older-than-minutes", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    setup_logging()
    cutoff = datetime.now(UTC) - timedelta(minutes=args.older_than_minutes)
    with get_sync_sessionmaker()() as session:
        first_pending = (
            select(OutboundMessage.inbound_message_id, func.min(OutboundMessage.sequence).label("sequence"))
            .where(OutboundMessage.status == "pending", OutboundMessage.created_at < cutoff)
            .group_by(OutboundMessage.inbound_message_id)
            .subquery()
        )
        ids = session.scalars(
            select(OutboundMessage.id)
            .join(
                first_pending,
                (OutboundMessage.inbound_message_id == first_pending.c.inbound_message_id)
                & (OutboundMessage.sequence == first_pending.c.sequence),
            )
            .where(OutboundMessage.status == "pending")
            .order_by(OutboundMessage.id)
        ).all()
    for outbound_id in ids:
        if args.dry_run:
            logger.info("Would queue outbound id=%s", outbound_id)
        elif enqueue_send(outbound_id):
            logger.info("Queued outbound id=%s", outbound_id)
    logger.info("%s pending turn(s)%s", len(ids), " (dry run)" if args.dry_run else " queued")


if __name__ == "__main__":
    main()
