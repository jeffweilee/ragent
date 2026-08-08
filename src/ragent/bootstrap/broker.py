import os
import sys

from taskiq_redis import ListQueueBroker, ListQueueSentinelBroker

from ragent.middleware.taskiq_context import StructlogContextMiddleware
from ragent.utility.env import parse_sentinel_hosts


def _make_broker() -> ListQueueBroker | ListQueueSentinelBroker:
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
            sentinel_kwargs={"password": sentinel_pw} if sentinel_pw else None,
        )
    url = os.environ.get("REDIS_BROKER_URL", "redis://localhost:6379/0")
    return ListQueueBroker(url=url)


broker = _make_broker()
# T-APL.9 — propagate request_id / user_id across the enqueue/execute seam so
# worker logs correlate with the originating HTTP request. Registered on the
# module-level broker so BOTH the api producer process and the worker consumer
# process pick it up (they import this same module).
broker.add_middlewares(StructlogContextMiddleware())
