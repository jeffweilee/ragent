# Python asyncio

`asyncio` is Python's standard library for writing single-threaded
concurrent code using coroutines. An asyncio program runs on a single
event loop that schedules coroutines cooperatively — only one runs at a
time, and each yields control via `await` while waiting for I/O.

## Configuring asyncio

The modern entry point is `asyncio.run`:

```python
import asyncio
import httpx

async def fetch(url: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        return resp.text

async def main() -> None:
    urls = ["https://example.com", "https://example.org"]
    pages = await asyncio.gather(*(fetch(u) for u in urls))
    for p in pages:
        print(len(p))

asyncio.run(main())
```

Core building blocks:

- `asyncio.run(coro)` creates a fresh event loop, runs the coroutine,
  and closes the loop on exit.
- `asyncio.gather` schedules multiple coroutines concurrently and
  collects their results.
- `asyncio.create_task` returns a `Task` that runs in the background.
- `asyncio.wait_for(coro, timeout)` enforces a per-call timeout.

## Best Practices

- Wrap every blocking call (file I/O, CPU-heavy work) in
  `asyncio.to_thread` so it does not stall the event loop.
- Always `await` or cancel a Task before exit; otherwise asyncio raises
  `Task was destroyed but it is pending`.
- Prefer one `asyncio.run` at the program entry point; nesting event
  loops is rarely correct.
- Use structured concurrency via `asyncio.TaskGroup` (Python 3.11+)
  rather than ad-hoc gather+cancel patterns.

## Troubleshooting

- `RuntimeWarning: coroutine '...' was never awaited` means a coroutine
  was created but never scheduled — usually a missing `await`.
- `RuntimeError: Event loop is closed` happens when code tries to
  schedule work after `asyncio.run` returned; restructure to keep all
  awaits inside the lifetime of the loop.
- If the loop appears to hang, check for a synchronous blocking call
  not wrapped in `to_thread`.
