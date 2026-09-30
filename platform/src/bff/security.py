"""Request-level protections — equivalent of the APIM policies:
  - fragments/reject-sensitive-headers.xml
  - rate-limit-by-key (per client IP before authentication, per user after authentication)
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Iterable

# Identity-related headers are computed by the BFF only. A client that sets one is rejected
# (fail closed) instead of silently overridden, so that attempts are visible.
_FORBIDDEN_PREFIXES = ("x-client-", "x-agent-")
_FORBIDDEN_NAMES = ("x-ms-user-identity",)


def has_forbidden_header(header_names: Iterable[str]) -> bool:
    for name in header_names:
        lower = name.lower()
        if lower in _FORBIDDEN_NAMES or lower.startswith(_FORBIDDEN_PREFIXES):
            return True
    return False


class SlidingWindowLimiter:
    """In-memory sliding-window rate limiter.

    Exact for a single replica (the BFF Container App runs with maxReplicas = 1 in this POC).
    With several replicas, use a shared store (e.g. Azure Cache for Redis).
    """

    def __init__(self, max_keys: int = 50_000) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()
        self._max_keys = max_keys

    async def allow(self, key: str, limit: int, period_seconds: float = 60.0) -> bool:
        now = time.monotonic()
        async with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= period_seconds:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            if len(self._hits) > self._max_keys:
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < period_seconds}
            return True
