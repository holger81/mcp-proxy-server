"""In-process rate limiting for the admin login endpoint (PLAN 4.4).

Token bucket per source IP (default 5 attempts / rolling 60 s) plus an
escalating lockout per IP on consecutive wrong passwords. Single process,
single admin account → per-IP is the effective target (the login form has
no username field to key a backoff on). No new dependencies; deliberately
not shared across processes/replicas.

Client IPs come from the ASGI peer (`request.client.host`), never from
`X-Forwarded-For`, so the bucket cannot be spoofed by a request header.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from typing import Callable


class LoginRateLimiter:
    def __init__(
        self,
        *,
        max_attempts: int = 5,
        window_s: float = 60.0,
        lockout_base_s: float = 60.0,
        lockout_max_s: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_s = window_s
        self.lockout_base_s = lockout_base_s
        self.lockout_max_s = lockout_max_s
        self._clock = clock
        self._lock = threading.Lock()
        self._attempts: dict[str, deque[float]] = {}
        self._streaks: dict[str, int] = {}
        self._lockouts: dict[str, float] = {}  # key → monotonic unlock time

    # -- public -------------------------------------------------------------

    def hit(self, key: str) -> float | None:
        """Register a login attempt. Returns retry-seconds (→429) or None.

        Rejected attempts (return value not None) do **not** refill the
        bucket, so a hammering client cannot keep itself permanently locked
        out past the window via 429s alone.
        """
        now = self._clock()
        with self._lock:
            until = self._lockouts.get(key)
            if until is not None:
                if until > now:
                    return math.ceil(until - now)
                del self._lockouts[key]
            q = self._attempts.get(key)
            if q is not None:
                while q and q[0] <= now - self.window_s:
                    q.popleft()
                if len(q) >= self.max_attempts:
                    return math.ceil(q[0] + self.window_s - now)
            elif len(self._attempts) > 4096:  # bounded worst case
                self._prune_locked(now)
                q = self._attempts.get(key)
            self._attempts.setdefault(key, deque()).append(now)
            return None

    def record_failure(self, key: str) -> None:
        """Wrong password: escalate consecutive-failure streak → lockout."""
        now = self._clock()
        with self._lock:
            streak = self._streaks.get(key, 0) + 1
            self._streaks[key] = streak
            if streak >= self.max_attempts:
                penalty = min(
                    self.lockout_base_s * (2 ** (streak - self.max_attempts)),
                    self.lockout_max_s,
                )
                self._lockouts[key] = now + penalty

    def record_success(self, key: str) -> None:
        """Successful login clears all state for the key."""
        with self._lock:
            self._attempts.pop(key, None)
            self._streaks.pop(key, None)
            self._lockouts.pop(key, None)

    # -- internals ------------------------------------------------------------

    def _prune_locked(self, now: float) -> None:
        for k in [k for k, q in self._attempts.items() if not q or q[-1] <= now - self.window_s]:
            del self._attempts[k]
        for k in [k for k, u in self._lockouts.items() if u <= now]:
            del self._lockouts[k]
