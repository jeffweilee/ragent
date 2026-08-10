import os
import sys

from taskiq_redis import ListQueueBroker, ListQueueSentinelBroker

from ragent.clients.redis_guard import connection_timeouts
from ragent.middleware.taskiq_context import StructlogContextMiddleware
from ragent.utility.env import parse_sentinel_hosts


def _make_broker() -> ListQueueBroker | ListQueueSentinelBroker:
    # The producer side (`kiq()` from a FastAPI handler) runs on redis.asyncio, so
    # an unreachable broker does not block the event loop the way the sync clients
    # do — but without a connect timeout it still hangs the *request* indefinitely.
    # A bounded connect makes a dead queue surface as a fast, catchable error that
    # `IngestService` degrades on (the row is already persisted UPLOADED and the
    # worker sweep re-dispatches it).
    #
    # blocking=True is load-bearing, not a tuning choice: this same broker object
    # is what the WORKER consumes with, and `listen()` parks in `brpop()` with no
    # timeout. A read timeout there kills the consumer outright and silently —
    # see `connection_timeouts` for the mechanism, and journal SRE
    # "Blocking-Read Timeout" for the outage it caused.
    timeouts = connection_timeouts(blocking=True)
    mode = os.environ.get("REDIS_MODE", "standalone")
    if mode == "sentinel":
        hosts_raw = os.environ.get("REDIS_SENTINEL_HOSTS", "")
        master = os.environ.get("REDIS_BROKER_SENTINEL_MASTER", "ragent-broker")
        if not hosts_raw:
            print("REDIS_SENTINEL_HOSTS is required when REDIS_MODE=sentinel", file=sys.stderr)
            sys.exit(1)
        sentinels = parse_sentinel_hosts(hosts_raw)
        master_pw = os.environ.get("REDIS_SENTINEL_MASTER_PASSWORD") or None
        sentinel_pw = os.environ.get("REDIS_SENTINEL_PASSWORD") or None
        return ListQueueSentinelBroker(
            sentinels=sentinels,
            master_name=master,
            password=master_pw,
            # Discovery issues ordinary commands (never a blocking pop), so it
            # keeps the full budget — bounding it is exactly what stops a hang
            # during a failover, which is when the old master goes quiet.
            sentinel_kwargs={
                **connection_timeouts(),
                **({"password": sentinel_pw} if sentinel_pw else {}),
            },
            **timeouts,
        )
    url = os.environ.get("REDIS_BROKER_URL", "redis://localhost:6379/0")
    return ListQueueBroker(url=url, **timeouts)


broker = _make_broker()
# T-APL.9 — propagate request_id / user_id across the enqueue/execute seam so
# worker logs correlate with the originating HTTP request. Registered on the
# module-level broker so BOTH the api producer process and the worker consumer
# process pick it up (they import this same module).
broker.add_middlewares(StructlogContextMiddleware())
