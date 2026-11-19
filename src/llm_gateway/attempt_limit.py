from collections.abc import Mapping
from threading import Lock


class RunAttemptLimitExceeded(RuntimeError):
    """A model has exhausted its attempt quota for a run."""


class RunAttemptLimiter:
    """Thread-safe attempt limits per (run_id, model)."""

    def __init__(
        self,
        default_max_attempts_per_model: int = 10_000,
        max_attempts_by_model: Mapping[str, int] | None = None,
    ) -> None:
        self._validate_limit(default_max_attempts_per_model)

        self.default_max_attempts_per_model = default_max_attempts_per_model

        self.max_attempts_by_model = dict(
            max_attempts_by_model if max_attempts_by_model is not None else {}
        )

        for model, limit in self.max_attempts_by_model.items():
            self._validate_model(model)
            self._validate_limit(limit)

        self._lock = Lock()
        self._attempts: dict[tuple[str, str], int] = {}

    @staticmethod
    def _validate_limit(value: int) -> None:
        if type(value) is not int or value <= 0:
            raise ValueError("attempt limit must be a positive integer")

    @staticmethod
    def _validate_model(model: str) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")

    def claim(self, run_id: str, model: str) -> int:
        """Reserve one backend attempt for this run and model."""

        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")

        self._validate_model(model)

        key = (run_id, model)

        limit = self.max_attempts_by_model.get(
            model,
            self.default_max_attempts_per_model,
        )

        with self._lock:
            used = self._attempts.get(key, 0)

            if used >= limit:
                raise RunAttemptLimitExceeded(
                    f"Run {run_id!r}: model {model!r} "
                    f"reached its limit of {limit} "
                    "backend attempts"
                )

            self._attempts[key] = used + 1
            return used + 1

    def attempts_for(self, run_id: str, model: str) -> int:
        """Return the attempts used by one model in one run."""

        with self._lock:
            return self._attempts.get(
                (run_id, model),
                0,
            )
