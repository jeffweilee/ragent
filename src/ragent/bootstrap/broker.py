import os
import sys

from taskiq_redis import ListQueueBroker, ListQueueSentinelBroker


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
