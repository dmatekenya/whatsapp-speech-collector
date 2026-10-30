from functools import lru_cache

from redis import Redis
from rq import Queue, Retry

from src.config import get_settings

INBOUND_QUEUE = "inbound"
PROCESS_INBOUND_JOB = "src.workers.jobs.process_inbound"
INBOUND_RETRY = Retry(max=3, interval=[10, 60, 300])


@lru_cache
def get_inbound_queue() -> Queue:
    # Short timeouts so a Redis outage fails fast instead of stalling the webhook.
    connection = Redis.from_url(
        get_settings().redis_url, socket_connect_timeout=2, socket_timeout=2
    )
    return Queue(INBOUND_QUEUE, connection=connection)


def enqueue_inbound(queue: Queue, message_sid: str) -> None:
    queue.enqueue(PROCESS_INBOUND_JOB, message_sid, retry=INBOUND_RETRY)
