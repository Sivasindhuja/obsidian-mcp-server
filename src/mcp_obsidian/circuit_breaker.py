"""Small, testable circuit breaker used by the MCP tools."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class CircuitBreaker:
    """Closed -> open -> half-open circuit breaker.

    The clock is injectable so tests can advance time without sleeping.
    Only one half-open probe is allowed at a time; this prevents a burst of
    requests from hammering a service that is just recovering.
    """

    failure_threshold: int = 3
    recovery_time: float = 10.0
    clock: Callable[[], float] = time.monotonic
    failures: int = 0
    state: str = "CLOSED"
    last_failure_time: float = 0.0
    _half_open_probe_in_flight: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if self.recovery_time < 0:
            raise ValueError("recovery_time must be non-negative")
        if self.state not in {"CLOSED", "OPEN", "HALF_OPEN"}:
            raise ValueError("invalid circuit state")

    def allow_request(self) -> bool:
        """Return True if a request may be attempted."""

        if self.state == "CLOSED":
            return True

        if self.state == "OPEN":
            if self.clock() - self.last_failure_time < self.recovery_time:
                return False
            self.state = "HALF_OPEN"
            self._half_open_probe_in_flight = True
            return True

        # HALF_OPEN: allow a single probe.
        if self._half_open_probe_in_flight:
            return False
        self._half_open_probe_in_flight = True
        return True

    def record_success(self) -> None:
        """Reset the breaker after a successful request."""

        self.failures = 0
        self.state = "CLOSED"
        self._half_open_probe_in_flight = False

    def record_failure(self) -> None:
        """Record a service failure and open the breaker when appropriate."""

        self.failures += 1
        self.last_failure_time = self.clock()
        self._half_open_probe_in_flight = False

        if self.state == "HALF_OPEN" or self.failures >= self.failure_threshold:
            self.state = "OPEN"

    def record_cancelled(self) -> None:
        """Release a half-open probe slot when the caller is cancelled."""

        self._half_open_probe_in_flight = False

    def reset(self) -> None:
        """Force the breaker back to its initial closed state."""

        self.failures = 0
        self.state = "CLOSED"
        self.last_failure_time = 0.0
        self._half_open_probe_in_flight = False

    def retry_after_seconds(self) -> float:
        """Seconds until an open breaker will allow a probe."""

        if self.state != "OPEN":
            return 0.0
        remaining = self.recovery_time - (self.clock() - self.last_failure_time)
        return max(0.0, round(remaining, 3))

    def snapshot(self) -> dict:
        """Return a JSON-serialisable view for health checks and tests."""

        return {
            "state": self.state,
            "failure_count": self.failures,
            "failure_threshold": self.failure_threshold,
            "recovery_time_sec": self.recovery_time,
            "retry_after_sec": self.retry_after_seconds(),
        }
