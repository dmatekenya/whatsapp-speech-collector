"""Expire sent assignments past their expires_at, returning their prompts to the pool.

    uv run python -m scripts.expire_assignments

choose_next_prompt() already does this before choosing; run this on a schedule
(e.g. hourly via cron) so coverage reports stay current between requests.
"""

import argparse
import logging

from src.logging_config import setup_logging
from src.storage.db import get_sync_sessionmaker
from src.tasks.bank import expire_stale_assignments

logger = logging.getLogger("expire_assignments")


def main() -> None:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()
    setup_logging()
    with get_sync_sessionmaker()() as session:
        expired = expire_stale_assignments(session)
        session.commit()
    logger.info("Expired %s assignment(s)", expired)


if __name__ == "__main__":
    main()
