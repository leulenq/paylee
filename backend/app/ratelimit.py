"""In-memory sliding-window limits. Per process: with several workers, put a shared limiter (e.g. Redis) in front."""

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float):
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        hits = self._hits[key]
        while hits and hits[0] <= now - self.window_seconds:
            hits.popleft()
        return hits

    def is_blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, time.monotonic())) >= self.limit

    def hit(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune(key, now).append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# Intake form: real requesters submit one or two bills; more than this from one address is likely abuse.
submissions = RateLimiter(limit=5, window_seconds=3600)
# Admin console: wrong passwords per address before a cool-down.
admin_failures = RateLimiter(limit=10, window_seconds=900)


def reset_all() -> None:
    submissions.reset()
    admin_failures.reset()
