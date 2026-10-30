"""Requeue inbound messages stuck in 'received' or 'failed'.

Use after a Redis or worker outage:

    uv run python -m scripts.requeue_pending --older-than-minutes 15
    uv run python -m scripts.requeue_pending --dry-run
"""

import argparse
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from src.logging_config import setup_logging
from src.storage.db import get_sync_sessionmaker
from src.storage.models import InboundMessage
from src.storage.queue import enqueue_inbound, get_inbound_queue

logger = logging.getLogger("requeue_pending")


# ============================================================
# Requeue
# ============================================================
def requeue_pending(older_than_minutes: int, dry_run: bool = False) -> int:
    # The age cutoff avoids racing the webhook (between insert and enqueue)
    # and RQ's own retries, which span about 6 minutes.
    cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
    queue = get_inbound_queue()
    requeued = 0

    with get_sync_sessionmaker()() as session:
        messages = session.scalars(
            select(InboundMessage)
            .where(
                InboundMessage.status.in_(("received", "failed")),
                InboundMessage.received_at < cutoff,
            )
            .order_by(InboundMessage.id)
        ).all()

        for message in messages:
            if dry_run:
                logger.info("Would requeue id=%s status=%s", message.id, message.status)
                continue
            enqueue_inbound(queue, message.message_sid)
            message.status = "queued"
            session.commit()
            requeued += 1
            logger.info("Requeued id=%s", message.id)

    logger.info("Found %s pending message(s), requeued %s", len(messages), requeued)
    return requeued


# ============================================================
# CLI
# ============================================================
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--older-than-minutes",
        type=int,
        default=15,
        help="only requeue messages received more than this many minutes ago (default: 15)",
    )
    parser.add_argument("--dry-run", action="store_true", help="list messages without requeuing")
    args = parser.parse_args()

    setup_logging()
    requeue_pending(args.older_than_minutes, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
