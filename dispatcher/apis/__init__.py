"""Shared infrastructure for dispatcher API clients.

Provides a lazy aiohttp session, TTL cache decorator, and DispatchError.
"""

import time
import functools
import aiohttp

# ---------------------------------------------------------------------------
# Shared aiohttp session (lazy singleton)
# ---------------------------------------------------------------------------

_session: aiohttp.ClientSession | None = None


async def get_session() -> aiohttp.ClientSession:
    """Return (and lazily create) a shared aiohttp ClientSession."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
        )
    return _session


async def close_session():
    """Close the shared session (call on shutdown)."""
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None


# ---------------------------------------------------------------------------
# TTL cache decorator for async functions
# ---------------------------------------------------------------------------

def ttl_cache(seconds: int):
    """Simple TTL cache for async functions.

    Caches based on all positional and keyword arguments.
    Not thread-safe, but fine for single-event-loop use.
    """
    def decorator(func):
        cache: dict[tuple, tuple[float, object]] = {}

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.monotonic()
            if key in cache:
                ts, val = cache[key]
                if now - ts < seconds:
                    return val
            result = await func(*args, **kwargs)
            cache[key] = (now, result)
            return result

        wrapper.cache_clear = lambda: cache.clear()
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# DispatchError — raised by API clients on recoverable failures
# ---------------------------------------------------------------------------

class DispatchError(Exception):
    """Raised when an API call fails but the dispatch was still matched.

    The dispatcher catches this and sends the message as plain text to the user.
    Claude is NOT invoked.
    """
    pass
