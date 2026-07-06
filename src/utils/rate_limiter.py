"""
rate_limiter.py — Thread-safe token bucket rate limiter.

Used by source adapters to stay within API rate limits.
Default: 2 requests/second with a burst capacity of 5.

See DESIGN.md §13.2 for rate limiting strategy.
"""

from __future__ import annotations

import time
from threading import Lock


class TokenBucketRateLimiter:
    """
    Token bucket algorithm.
    Tokens refill at `rate` per second up to `capacity`.
    Each acquire() call consumes one token; blocks if none available.
    """

    def __init__(self, rate: float = 2.0, capacity: float = 5.0):
        self.rate = rate
        self.capacity = capacity
        self._tokens = capacity
        self._last_check = time.monotonic()
        self._lock = Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_check
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._last_check = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
            # Not enough tokens — sleep a fraction and retry
            time.sleep(0.1)
