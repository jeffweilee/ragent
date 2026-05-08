# Redis Expire

Redis lets every key carry an optional time-to-live (TTL) so the server
deletes it automatically when the deadline passes. Expiry is the
foundation for caches, session stores, rate limiters, and any
short-lived state.

## Configuring Expire

The basic commands:

```text
SET session:42 "..." EX 3600        # seconds
SET token:abc "..." PX 30000        # milliseconds
SET key value EXAT 1893456000       # absolute unix timestamp

EXPIRE key 60                       # set TTL on an existing key
PEXPIRE key 1500
TTL key                             # remaining seconds, or -1 / -2
PERSIST key                         # remove the TTL
```

`EXPIRE` accepts modifiers in Redis 7.0+:

- `NX` — set TTL only if the key has no TTL.
- `XX` — set TTL only if the key already has one.
- `GT` / `LT` — set TTL only if the new value is greater / less than
  the current one.

These modifiers make atomic fixed-window rate limiters straightforward:
`INCR counter` then `EXPIRE counter 60 NX` in a single pipeline.

## Best Practices

- Always set a TTL on keys that are not meant to be permanent;
  unbounded keys leak memory.
- Use millisecond precision (`PX`, `PEXPIRE`) only when you actually
  need it — second precision is cheaper to reason about.
- Pair `INCR` with `EXPIRE NX` for fixed-window counters so the TTL is
  set exactly once per window.
- Prefer absolute deadlines (`EXAT` / `PEXPIREAT`) when synchronizing
  expiry across instances.

## Troubleshooting

- `TTL key` returning `-1` means the key exists but has no expire;
  `-2` means the key does not exist.
- A key that "expired but did not delete" usually indicates that
  Redis's lazy-expire policy has not yet swept it; an explicit `GET`
  triggers immediate cleanup.
- Memory pressure with `maxmemory-policy noeviction` causes writes to
  fail when expired keys have not been reclaimed; switch to
  `allkeys-lru` or `volatile-ttl` if appropriate.
