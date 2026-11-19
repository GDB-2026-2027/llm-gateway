from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import BoundedSemaphore, Lock
from time import monotonic, sleep


class ProviderLimitExceeded(RuntimeError):
    """Provider limit cannot be respected within the allowed time window."""


@dataclass(frozen=True)
class RateLimits:
    requests_per_minute: int
    max_concurrent: int
    max_wait_s: float

    def __post_init__(self) -> None:
        if type(self.requests_per_minute) is not int or self.requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be a positive integer.")

        if type(self.max_concurrent) is not int or self.max_concurrent <= 0:
            raise ValueError("max_concurrent must be a positive integer.")
        if type(self.max_wait_s) not in (
            int,
            float,
        ) or not 0 <= self.max_wait_s < float("inf"):
            raise ValueError("max_wait_s must be a finite non-negative number.")


class _ProviderState:
    def __init__(self, max_concurrent: int):
        self.slots = BoundedSemaphore(max_concurrent)
        self.schedule_lock = Lock()
        self.next_allowed_at = 0.0


class ProviderThrottle:
    """Protect providers from too many or too frequent requests."""

    def __init__(self, limits: RateLimits):
        self.limits = limits

        self._lock = Lock()
        self._providers: dict[str, _ProviderState] = {}

    def _get_provider(self, provider_key: str) -> _ProviderState:
        if not provider_key:
            raise ValueError("provider_key cannot be empty")

        with self._lock:
            state = self._providers.get(provider_key)

            if state is None:
                state = _ProviderState(self.limits.max_concurrent)
                self._providers[provider_key] = state

            return state

    @contextmanager
    def hold(
        self,
        provider_key: str,
    ) -> Generator[None, None, None]:
        """
        Wait for permission before one backend HTTP attempt.

        A concurrency slot is held only during the scheduled
        wait and the backend call.
        """

        state = self._get_provider(provider_key)
        deadline = monotonic() + self.limits.max_wait_s

        # Reserve a provider slot before scheduling the call.
        acquired = state.slots.acquire(timeout=self.limits.max_wait_s)

        if not acquired:
            raise ProviderLimitExceeded("provider concurrency limit exceeded")

        try:
            # Space out call starts without holding the scheduling lock while waiting.
            with state.schedule_lock:
                now = monotonic()

                start_at = max(
                    now,
                    state.next_allowed_at,
                )

                delay = start_at - now

                if delay > max(0.0, deadline - now):
                    raise ProviderLimitExceeded("provider request rate limit exceeded")

                state.next_allowed_at = (
                    start_at + 60.0 / self.limits.requests_per_minute
                )

            # Keep the provider slot reserved until the call finishes.
            if delay > 0:
                sleep(delay)

            yield

        finally:
            # Release the slot even when the provider call fails.
            state.slots.release()
