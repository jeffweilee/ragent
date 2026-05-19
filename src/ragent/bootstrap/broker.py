import os
import sys

from taskiq_redis import ListQueueBroker, ListQueueSentinelBroker

from ragent.middleware.taskiq_context import StructlogContextMiddleware


def _make_broker() -> ListQueueBroker | ListQueueSentinelBroker:
    mode = os.environ.get("REDIS_MODE", "standalone")
    if mode == "sentinel":
        hosts_raw = os.environ.get("REDIS_SENTINEL_HOSTS", "")
        master = os.environ.get("REDIS_BROKER_SENTINEL_MASTER", "ragent-broker")
        if not hosts_raw:
            print("REDIS_SENTINEL_HOSTS is required when REDIS_MODE=sentinel", file=sys.stderr)
            sys.exit(1)
        sentinels = [
            (h.rsplit(":", 1)[0], int(h.rsplit(":", 1)[1]))
            for h in hosts_raw.split(",")
            if h.strip()
        ]
        return ListQueueSentinelBroker(sentinels=sentinels, master_name=master)
    url = os.environ.get("REDIS_BROKER_URL", "redis://localhost:6379/0")
    return ListQueueBroker(url=url)


broker = _make_broker()
# T-APL.9 — propagate request_id / user_id across the enqueue/execute seam so
# worker logs correlate with the originating HTTP request. Registered on the
# module-level broker so BOTH the api producer process and the worker consumer
# process pick it up (they import this same module).
broker.add_middlewares(StructlogContextMiddleware())
